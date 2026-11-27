"""Run a contract's ODCS quality rules on a table: every lake snapshot, every live read (#45 item 1).

A rule comes only from the contract (hard rule 1): its id, what it measures, the bound the publisher's codebook
or a reviewed structural fact sets, a dimension and a severity. Two outcomes are kept apart:

    data failure  the rule ran and the data is outside the bound. With severity error the result is "fail",
                  with severity warn it is "warn"; either way answers from the table carry a caveat in plain
                  words, and the data is never changed.
    rule error    the rule could not run (a column it names is gone, its SQL no longer fits the table) or ran
                  and returned no number (NULL, e.g. max() over no rows): the rule, not the data, needs a
                  maintainer, so it goes to the manifest's rule_errors (a drift issue) and never becomes a caveat
                  about the numbers. The generated dbt test raises an error on the same NULL (odcs_check.sql),
                  so the read path and the gate agree.

Beside the contract's own rules, two kinds of check are generated. The table must have rows (has_rows, severity
error): an empty table passes every count and value check, and "the table has rows" is an exact structural fact.
And every column that is not required gets a fill-rate check (severity warn): an all-empty column passes every
value check, so "is anything there at all" is asked separately. A flag column (flagFor) is empty wherever its
count is published, and a column the contract marks mayBeEmpty may be empty by design, so neither gets one.

Contract SQL is untrusted text (hard rule 3 lets a model draft a contract). A sql rule must pass the same guard as
query_sql (sql_guard.guard_sql: one SELECT, allowlisted functions, the contract's own columns) when the contract
loads, is re-checked as one SELECT before it runs, and lake snapshots are checked on a read-only, locked-down
connection (staging.check_snapshot).

The same rules become named dbt tests (transform_gen.py), so the transform build and the read path check the
same things.

A run that leaves a check with no result proves nothing (issue #222). Every report carries the ids of the checks
it was asked to run (`expected_checks`); an expected check with neither a result nor a rule error, a result whose
status is not one run_rules writes, or a record with no result at all, is not evaluated, and that blocks, whatever
the check's severity. A malformed record is read as no record: nothing in it says a check passed. A contract that declares no
quality rules of its own still gets the generated checks, so its record is never empty; list_caveats says plainly
that it declares none.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import duckdb

from health_data_registry.sql_guard import SqlRefused, guard_sql, require_single_select

DIMENSIONS = frozenset(
    {"accuracy", "completeness", "conformity", "consistency", "coverage", "timeliness", "uniqueness"}
)
SEVERITIES = frozenset({"error", "warn"})
LIBRARY_METRICS = frozenset({"invalidValues", "rowCount", "nullValues"})
# ODCS v3 comparison keys and what each asks of the measured number.
COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    "mustBe": lambda v, b: v == b,
    "mustNotBe": lambda v, b: v != b,
    "mustBeGreaterThan": lambda v, b: v > b,
    "mustBeGreaterOrEqualTo": lambda v, b: v >= b,
    "mustBeLessThan": lambda v, b: v < b,
    "mustBeLessOrEqualTo": lambda v, b: v <= b,
    "mustBeBetween": lambda v, b: b[0] <= v <= b[1],
    "mustNotBeBetween": lambda v, b: not (b[0] <= v <= b[1]),
}
FILL_RATE_PREFIX = "fill_rate_"
NOT_EVALUATED = "not_evaluated"  # a value rule whose column had no values: never a pass
# The test id of a rule error that stopped the whole run (judge_snapshot could not open the file): it stands for
# every expected check, so they are not also listed as having no result.
WHOLE_RUN = "contract-rules"

HAS_ROWS = "has_rows"  # the generated volume check
EMPTY_SHARE = "emptyShare"  # the generated fill-rate checks only: the share of rows with no value, 0 to 1
GUARD_TABLE = "rule_object"  # what {object} stands for while the guard checks a sql rule


class QualityRuleInvalid(ValueError):
    """A contract quality rule is malformed (caught when the registry loads, not when data is read)."""


class NoMeasurement(ValueError):
    """A rule ran but returned no number (NULL): a rule error, never a pass and never a zero."""


class NotEvaluated(NoMeasurement):
    """A value rule found no values in its column to judge: not evaluated, which is never a pass."""


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str  # "library" or "sql"
    description: str
    dimension: str
    severity: str
    comparator: str
    bound: Any
    column: str | None = None
    metric: str | None = None
    valid_values: tuple[Any, ...] = ()
    query: str | None = None
    # The column's mayBeEmpty: an all-empty column is then expected, so a value rule on it passes rather than
    # counting as not evaluated.
    may_be_empty: bool = False

    def bound_text(self) -> str:
        words = {
            "mustBe": "should equal",
            "mustNotBe": "should not equal",
            "mustBeGreaterThan": "should be more than",
            "mustBeGreaterOrEqualTo": "should be at least",
            "mustBeLessThan": "should be less than",
            "mustBeLessOrEqualTo": "should be at most",
            "mustBeBetween": "should be between",
            "mustNotBeBetween": "should be outside",
        }[self.comparator]
        b = self.bound
        return f"{words} {b[0]:g} and {b[1]:g}" if isinstance(b, list | tuple) else f"{words} {b:g}"


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _guard_rule_sql(query: str, columns: frozenset[str], where: str) -> None:
    """A sql rule must be one read-only SELECT over {object} that query_sql's guard accepts."""
    try:
        guard_sql(query.replace("{object}", GUARD_TABLE), allowed_tables={GUARD_TABLE: columns})
    except SqlRefused as e:
        raise QualityRuleInvalid(
            f"{where}: a sql rule is one read-only SELECT over {{object}} ({e.message})"
        ) from e


def _rule(item: Mapping[str, Any], column: str | None, where: str, columns: frozenset[str]) -> Rule:
    rid = str(item.get("id") or "").strip()
    if not rid:
        raise QualityRuleInvalid(f"{where}: every quality rule needs an id")
    where = f"{where}.{rid}"
    kind = str(item.get("type") or "")
    if kind not in ("library", "sql"):
        raise QualityRuleInvalid(f"{where}: type must be library or sql")
    dimension, severity = str(item.get("dimension") or ""), str(item.get("severity") or "")
    if dimension not in DIMENSIONS:
        raise QualityRuleInvalid(f"{where}: dimension must be one of {', '.join(sorted(DIMENSIONS))}")
    if severity not in SEVERITIES:
        raise QualityRuleInvalid(f"{where}: severity must be error or warn")
    found = [k for k in COMPARATORS if k in item]
    if len(found) != 1:
        raise QualityRuleInvalid(f"{where}: give exactly one of {', '.join(COMPARATORS)}")
    comparator, bound = found[0], item[found[0]]
    if comparator.endswith("Between"):
        if not (isinstance(bound, list) and len(bound) == 2 and all(_number(b) for b in bound)):
            raise QualityRuleInvalid(f"{where}: {comparator} takes [low, high], two numbers")
        if bound[0] > bound[1]:
            raise QualityRuleInvalid(f"{where}: {comparator} takes [low, high] with low at most high")
    elif not _number(bound):
        raise QualityRuleInvalid(f"{where}: {comparator} takes a number")
    description = str(item.get("description") or "").strip()
    if not description:
        raise QualityRuleInvalid(f"{where}: every quality rule needs a description quoting its source")
    if kind == "sql":
        query = str(item.get("query") or "")
        if "{object}" not in query or not query.lstrip().upper().startswith("SELECT"):
            raise QualityRuleInvalid(f"{where}: a sql rule is one SELECT over {{object}}")
        _guard_rule_sql(query, columns, where)
        return Rule(rid, kind, description, dimension, severity, comparator, bound, column, query=query)
    metric = str(item.get("metric") or "")
    if metric not in LIBRARY_METRICS:
        raise QualityRuleInvalid(
            f"{where}: library metric must be one of {', '.join(sorted(LIBRARY_METRICS))}"
        )
    valid: tuple[Any, ...] = ()
    if metric in ("invalidValues", "nullValues") and column is None:
        raise QualityRuleInvalid(f"{where}: {metric} is a column rule; put it under the column")
    if metric == "invalidValues":
        valid = tuple((item.get("arguments") or {}).get("validValues") or ())
        if not valid:
            raise QualityRuleInvalid(f"{where}: invalidValues needs arguments.validValues from the codebook")
    return Rule(rid, kind, description, dimension, severity, comparator, bound, column, metric, valid)


def parse_rules(doc: Mapping[str, Any], where: str) -> tuple[Rule, ...]:
    """Every quality rule in an ODCS contract: on the schema object and on its columns. Ids are unique."""
    schema = (doc.get("schema") or [{}])[0]
    props = schema.get("properties") or []
    columns = frozenset(str(p.get("name")) for p in props)
    rules = [_rule(q, None, where, columns) for q in schema.get("quality") or []]
    for p in props:
        name = str(p.get("name"))
        empty_ok = any(
            isinstance(cp, Mapping) and cp.get("property") == "mayBeEmpty" and cp.get("value") is True
            for cp in p.get("customProperties") or []
        )
        rules += [
            replace(_rule(q, name, f"{where}.{name}", columns), may_be_empty=empty_ok)
            for q in p.get("quality") or []
        ]
    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        raise QualityRuleInvalid(f"{where}: quality rule ids must be unique")
    if any(i.startswith(FILL_RATE_PREFIX) or i == HAS_ROWS for i in ids):
        raise QualityRuleInvalid(
            f"{where}: the id {HAS_ROWS} and ids starting {FILL_RATE_PREFIX} are kept for the generated checks"
        )
    return tuple(rules)


def _phrase(description: str) -> str:
    """A contract column description as a phrase inside a sentence: its first sentence, no closing stop."""
    first = description.strip().split(". ")[0].strip().rstrip(".")
    return first[:1].lower() + first[1:] if first[1:2].islower() else first


def has_rows_rule() -> Rule:
    """The generated volume check: an empty table passes every other check, so it must have rows."""
    return Rule(
        id=HAS_ROWS,
        kind="library",
        description="This copy of the source has no rows, so there is nothing to answer from.",
        dimension="completeness",
        severity="error",
        comparator="mustBeGreaterThan",
        bound=0,
        metric="rowCount",
    )


def fill_rate_rules(columns: Sequence[Any]) -> tuple[Rule, ...]:
    """One fill-rate check per contract column (registry.Column) that is not required, not a flag column
    (flagFor: empty wherever its count is published) and not marked mayBeEmpty, in contract order."""
    return tuple(
        Rule(
            id=f"{FILL_RATE_PREFIX}{c.name}",
            kind="library",
            description=f"No row has a value for {_phrase(c.description)}.",
            dimension="completeness",
            severity="warn",
            comparator="mustBeLessThan",
            bound=1.0,
            column=c.name,
            metric=EMPTY_SHARE,
        )
        for c in columns
        if not c.required and c.flag_for is None and not c.may_be_empty
    )


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def measure_sql(rule: Rule, relation: str) -> str:
    """One SELECT returning the single number a rule compares, over `relation` (a quoted name or a dbt ref).

    The read path runs it on a snapshot or live table; the generated dbt test runs the same text on the model.
    """
    if rule.kind == "sql":
        return str(rule.query).replace("{object}", relation)
    if rule.metric == "rowCount":
        return f"SELECT count(*) FROM {relation}"  # noqa: S608 - quoted contract identifiers, literal codes
    c = _q(str(rule.column))
    empty = f"({c} IS NULL OR trim(CAST({c} AS VARCHAR)) = '')"
    if rule.metric == "nullValues":  # ODCS: how many rows have no value (blank text counts as empty)
        return f"SELECT count(*) FILTER (WHERE {empty}) FROM {relation}"  # noqa: S608 - quoted contract identifiers, literal codes
    if rule.metric == EMPTY_SHARE:  # the fill-rate check: the share of rows with no value; 1 means all empty
        return f"SELECT CASE WHEN count(*) = 0 THEN 0.0 ELSE count(*) FILTER (WHERE {empty}) / count(*) END FROM {relation}"  # noqa: S608 - quoted contract identifiers, literal codes
    listed = ", ".join(_lit(v) for v in rule.valid_values)
    if rule.may_be_empty:
        return f"SELECT count(*) FROM {relation} WHERE NOT {empty} AND {c} NOT IN ({listed})"  # noqa: S608 - quoted contract identifiers, literal codes
    # A value rule on a column with no values at all has nothing to judge: NULL, never a pass (NotEvaluated).
    return (
        f"SELECT CASE WHEN count(*) FILTER (WHERE NOT {empty}) = 0 THEN NULL "  # noqa: S608 - quoted contract identifiers, literal codes
        f"ELSE count(*) FILTER (WHERE NOT {empty} AND {c} NOT IN ({listed})) END FROM {relation}"
    )


def condition_sql(rule: Rule, value: str) -> str:
    """SQL that is true when `value` (the measured number) meets the rule's bound; the dbt test's pass condition."""
    b = rule.bound
    ops = {
        "mustBe": "=",
        "mustNotBe": "<>",
        "mustBeGreaterThan": ">",
        "mustBeGreaterOrEqualTo": ">=",
        "mustBeLessThan": "<",
        "mustBeLessOrEqualTo": "<=",
    }
    if rule.comparator == "mustBeBetween":
        return f"{value} BETWEEN {_lit(b[0])} AND {_lit(b[1])}"
    if rule.comparator == "mustNotBeBetween":
        return f"{value} NOT BETWEEN {_lit(b[0])} AND {_lit(b[1])}"
    return f"{value} {ops[rule.comparator]} {_lit(b)}"


def measure(con: duckdb.DuckDBPyConnection, table: str, rule: Rule) -> tuple[float, int | None]:
    """The number a rule compares, and how many rows it affects where the number is a row count.

    Raises SqlRefused when the SQL is not one SELECT, and NoMeasurement when it returns no number.
    """
    sql = measure_sql(rule, _q(table))
    require_single_select(sql)  # defence in depth: the guard checked the rule when the contract loaded
    row = con.execute(sql).fetchone()
    if not row or row[0] is None:
        if rule.metric == "invalidValues":
            raise NotEvaluated("no row has a value in this column, so there was nothing to check")
        raise NoMeasurement("the check returned no number")

    value = float(row[0])
    counts = rule.metric in ("invalidValues", "nullValues") or (
        rule.kind == "sql" and rule.comparator == "mustBe" and rule.bound == 0
    )
    return value, int(value) if counts else None


def _judged(r: Mapping[str, Any]) -> bool:
    """A result with a status run_rules writes: an allowlist, so an unknown status is never read as a pass."""
    status = r.get("status")
    return status in ("pass", "fail", NOT_EVALUATED) or (status == "warn" and r.get("severity") == "warn")


@dataclass(frozen=True)
class QualityReport:
    results: tuple[dict[str, Any], ...]
    rule_errors: tuple[dict[str, str], ...]
    # The ids of every check the run was asked to make (rules_for): each must come back as a result or a rule
    # error, or the check never ran (issue #222). Stored as `expected_checks`, apart from each result's `expected`
    # (the bound the rule asks for).
    expected_checks: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": "contract-rules",
            "results": list(self.results),
            "rule_errors": list(self.rule_errors),
            "expected_checks": list(self.expected_checks),
        }

    @property
    def whole_run_error(self) -> bool:
        """The run itself could not start (the file is missing or can't be opened), so no check could run."""
        return any(e.get("test") == WHOLE_RUN for e in self.rule_errors)

    @property
    def never_ran(self) -> tuple[str, ...]:
        """Checks not evaluated, so they block: an expected check with neither a result nor a rule error, and a
        result whose status run_rules never writes. Empty when a whole-run error already stands for them all."""
        if self.whole_run_error:
            return ()
        seen = {str(r.get("test")) for r in self.results if _judged(r)}
        seen |= {str(e.get("test")) for e in self.rule_errors}
        odd = [str(r.get("test")) for r in self.results if not _judged(r)]
        return tuple(dict.fromkeys(i for i in [*self.expected_checks, *odd] if i not in seen))

    @property
    def ran_nothing(self) -> bool:
        """No result and no rule error at all. Every run has at least the volume check (has_rows), so an empty
        record means the checks never ran: it blocks, even when nothing says which checks were expected."""
        return not self.results and not self.rule_errors

    def caveats(self) -> list[str]:
        """Plain-words caveats for answers: data failures only (a broken rule is a maintainer's job).

        Each says what was found and what to do next (hard rule 12); the rule's id stays in the manifest.
        """
        out = []
        for r in self.results:
            if r["status"] == "pass" or not _judged(r):  # an unknown status blocks instead (never_ran)
                continue
            if r["status"] == NOT_EVALUATED:
                out.append(
                    f"A data check on this source could not be judged: {str(r['checks']).rstrip('.')} (no row has "
                    "a value in that column, so there was nothing to check). Compare with the publisher's page "
                    "before relying on these numbers."
                )
                continue
            if str(r["test"]).startswith(FILL_RATE_PREFIX):
                out.append(
                    f"{r['checks']} Answers that need it will be blank: ask for another measure, or check the "
                    "publisher's page."
                )
                continue
            lead = (
                "A data check on this source failed"
                if r["status"] == "fail"
                else "A data check on this source gave a warning"
            )
            out.append(
                f"{lead}: {str(r['checks']).rstrip('.')} (it {r['expected']}; this copy measured {r['observed']:g}). The numbers "
                "are shown as the publisher released them; compare with the publisher's page before relying on them."
            )
        return out

    @property
    def blocking(self) -> bool:
        """True when an error-severity rule failed or was not evaluated, any rule could not run, or a check
        never ran (an expected check with no result, or no result at all)."""
        return (
            any(
                r.get("status") == "fail"
                or (r.get("status") == NOT_EVALUATED and r.get("severity") == "error")
                for r in self.results
            )
            or bool(self.rule_errors)
            or self.unchecked
        )

    @property
    def unchecked(self) -> bool:
        """Some check never ran: an expected check has no result, or the record has none at all."""
        return self.ran_nothing or bool(self.never_ran)


class QualityBlocked(ValueError):
    """A snapshot failed an error-severity rule, or a rule could not run: it is never served or published."""


def expected_ids(contract: Any) -> tuple[str, ...]:
    """The id of every check a run of the contract's rules must report on (rules_for)."""
    return tuple(r.id for r in rules_for(contract))


def _report(q: Mapping[str, Any] | None, expected: Sequence[str]) -> QualityReport | None:
    """A stored quality record as a report; None when no contract-rules run wrote it or it is malformed (a list
    field that is not a list, an entry that is not a mapping), so nothing in it says a check passed.

    The checks expected are the record's own and the caller's (the contract's current rules), so a record can't
    drop a check from its own expected list and pass."""
    q = q or {}
    if not isinstance(q, Mapping) or q.get("tool") != "contract-rules":
        return None
    fields = [q.get(k) or [] for k in ("results", "rule_errors", "expected_checks")]
    if not all(isinstance(f, list | tuple) for f in fields):
        return None
    results, errors, own = fields
    if not all(isinstance(r, Mapping) for r in [*results, *errors]):
        return None
    wanted = tuple(dict.fromkeys([*(str(i) for i in own), *expected]))
    return QualityReport(tuple(results), tuple(errors), wanted)


def unchecked_message(
    dataset: str, quality: Mapping[str, Any] | None, *, expected: Sequence[str], publisher: str | None = None
) -> str | None:
    """Why a live or private table is not answered, in plain words; None when every expected check reported a
    verdict. The next step is the refusal code's own (query.NEXT_STEPS["quality_not_run"]), said once.

    The live and private paths answer a failed check with a caveat, but never a table whose checks did not all
    run: an expected check with no result, or a rule that could not run (issue #222), as the lake and publish
    paths judge the same record. run_rules reports on every rule it is given, so the no-result case is defence in
    depth; a rule error is the case that happens."""
    report = _report(quality, expected)
    if report is not None and not report.unchecked and not report.rule_errors:
        return None
    lead = f"{publisher} sent a table for {dataset}" if publisher else f"The table for {dataset} was read"
    return (
        f"{lead}, but not all of its data checks could run, so no numbers are shown for it. Other sources are "
        "not affected."
    )


NO_CHECKS_RAN = (
    "{dataset}: no data checks have run on this copy, so it is not served or published and the copy before it "
    "stays. Refresh it, or have a maintainer check the contract."
)


def blocked_message(
    dataset: str, quality: Mapping[str, Any] | None, *, expected: Sequence[str]
) -> str | None:
    """Why a snapshot's quality record (a manifest's `quality`) blocks it, in plain words; None when it doesn't.

    `expected` is required: the contract's current checks (expected_ids). A caller with no contract passes ()
    and says so, so failing closed never depends on remembering an optional argument.

    Four kinds, named apart: a failed error-severity rule (a data failure), an error-severity value rule with
    no values to judge, a rule that could not run (a metric error), and a check with no result (never
    evaluated: missing from the record, or with a status no run writes). Each leaves the copy unchecked or
    failing: it is kept for inspection, but LATEST stays where it was and export-public refuses it. A missing or
    malformed record, one no contract-rules run wrote, or one with no result at all, blocks too (#222).
    """
    report = _report(quality, expected)
    if report is None or report.ran_nothing:
        return NO_CHECKS_RAN.format(dataset=dataset)
    parts = _blocking_parts(report, quality or {})
    if not parts:
        return None
    return (
        f"{dataset}: {' and '.join(parts)}, so this copy is not served or published and the copy before it "
        "stays. Compare the data with the publisher's page, then refresh, or have a maintainer fix the contract."
    )


def sample_blocked_message(
    dataset: str, quality: Mapping[str, Any] | None, *, expected: Sequence[str]
) -> str | None:
    """Why a development sample (refresh --max-rows) fails a blocking check, in plain words; None when it doesn't.

    A sample is kept as the local copy so a maintainer can work with it, and it is never published, but a failed
    blocking check still fails the command that built it: the build never fails open (issue #173).
    """
    report = _report(quality, expected)
    if report is None or report.ran_nothing:
        lead = f"{dataset}: no data checks have run on this development sample."
    else:
        parts = _blocking_parts(report, quality or {})
        if not parts:
            return None
        lead = (
            f"{dataset}: {' and '.join(parts)} on this development sample, so the refresh failed, although the "
            "sample was kept as the local copy (it holds only the first --max-rows rows)."
        )
    return (
        f"{lead} The sample stays as the local copy for development only and is never published. Compare the "
        "data with the publisher's page, then refresh without --max-rows, or have a maintainer fix the contract."
    )


def _blocking_parts(report: QualityReport, q: Mapping[str, Any]) -> list[str]:
    """What blocks a report, one plain phrase per kind; empty when nothing does."""
    if not report.blocking:  # the one definition of what blocks, for a fresh build and a rerun alike
        return []
    results = report.results
    failed = [str(r.get("test")) for r in results if r.get("status") == "fail"]
    unjudged = [
        str(r.get("test"))
        for r in results
        if r.get("status") == NOT_EVALUATED and r.get("severity") == "error"
    ]
    parts = []
    if failed:
        parts.append(f"the error-severity check{'s' if len(failed) > 1 else ''} {', '.join(failed)} failed")
    if unjudged:
        parts.append(
            f"the error-severity check{'s' if len(unjudged) > 1 else ''} {', '.join(unjudged)} could not be "
            "judged (no values to check)"
        )
    whole = [e for e in report.rule_errors if e.get("test") == WHOLE_RUN]
    broken = [str(e.get("test")) for e in report.rule_errors if e.get("test") != WHOLE_RUN]
    if whole:
        parts.append(
            f"the data checks could not run at all ({str(whole[0].get('error') or 'no reason given')})"
        )
    if broken:
        parts.append(f"the check{'s' if len(broken) > 1 else ''} {', '.join(broken)} could not run")
    # A check the record itself never listed was added to the contract after this copy was checked; one it
    # listed, or a result with a status no run writes, simply has no result.
    listed = set(map(str, q.get("expected_checks") or ()))
    odd = {str(r.get("test")) for r in results if not _judged(r)}
    missing = report.never_ran
    stale = [i for i in missing if listed and i not in listed and i not in odd]
    lost = [i for i in missing if i not in stale]
    if lost:
        parts.append(f"{_checks(lost)} no result")
    if stale:
        parts.append(f"{_checks(stale)} no result (this copy was checked against an older contract)")
    return parts


def _checks(ids: Sequence[str]) -> str:
    """'the check a has' or 'the checks a, b have'."""
    return f"the check {ids[0]} has" if len(ids) == 1 else f"the checks {', '.join(ids)} have"


def rules_for(contract: Any) -> tuple[Rule, ...]:
    """The volume check, the contract's own quality rules, then the fill-rate checks (fill_rate_rules)."""
    return (has_rows_rule(), *contract.quality, *fill_rate_rules(contract.columns))


def run_rules(con: duckdb.DuckDBPyConnection, table: str, rules: Sequence[Rule]) -> QualityReport:
    """Every rule, each to a result or a rule error; the report names them all as expected."""
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for rule in rules:
        try:
            value, rows = measure(con, table, rule)
            ok = COMPARATORS[rule.comparator](value, rule.bound)
        except (
            NotEvaluated
        ):  # not a pass, and not a metric error: a result of its own that blocks (error rules)
            results.append(
                {
                    "test": rule.id,
                    "checks": rule.description,
                    "status": NOT_EVALUATED,
                    "rows_affected": None,
                    "dimension": rule.dimension,
                    "severity": rule.severity,
                    "column": rule.column,
                    "observed": None,
                    "expected": rule.bound_text(),
                }
            )
            continue
        except (duckdb.Error, SqlRefused, NoMeasurement, TypeError) as e:
            errors.append({"test": rule.id, "error": (str(e).splitlines() or [type(e).__name__])[0]})
            continue
        results.append(
            {
                "test": rule.id,
                "checks": rule.description,
                "status": "pass" if ok else ("fail" if rule.severity == "error" else "warn"),
                "rows_affected": rows,
                "dimension": rule.dimension,
                "severity": rule.severity,
                "column": rule.column,
                "observed": value,
                "expected": rule.bound_text(),
            }
        )
    return QualityReport(tuple(results), tuple(errors), tuple(rule.id for rule in rules))
