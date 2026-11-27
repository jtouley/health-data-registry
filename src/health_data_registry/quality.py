"""Run a contract's ODCS quality rules on a table: every lake snapshot, every live read (#45 item 1).

A rule comes only from the contract (hard rule 1): its id, what it measures, the bound the publisher's codebook
or a reviewed structural fact sets, a dimension and a severity. Two outcomes are kept apart:

    data failure  the rule ran and the data is outside the bound. With severity error the result is "fail",
                  with severity warn it is "warn"; either way answers from the table carry a caveat in plain
                  words, and the data is never changed.
    rule error    the rule could not run (a column it names is gone, its SQL no longer fits the table): the
                  rule, not the data, needs a maintainer, so it goes to the manifest's rule_errors (a drift issue)
                  and never becomes a caveat about the numbers.

Beside the contract's own rules, every column that is not required gets a fill-rate check (severity warn): an
all-empty column passes every value check, so "is anything there at all" is asked separately. That asserts
nothing about values, only that a column the contract describes is not entirely empty.

The same rules become named dbt tests (transform_gen.py), so the transform build and the read path check the
same things.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import duckdb

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
EMPTY_SHARE = "emptyShare"  # the generated fill-rate checks only: the share of rows with no value, 0 to 1


class QualityRuleInvalid(ValueError):
    """A contract quality rule is malformed (caught when the registry loads, not when data is read)."""


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

    def bound_text(self) -> str:
        words = {
            "mustBe": "equal",
            "mustNotBe": "not equal",
            "mustBeGreaterThan": "be greater than",
            "mustBeGreaterOrEqualTo": "be at least",
            "mustBeLessThan": "be less than",
            "mustBeLessOrEqualTo": "be at most",
            "mustBeBetween": "be between",
            "mustNotBeBetween": "be outside",
        }[self.comparator]
        b = self.bound
        return f"must {words} {b[0]} and {b[1]}" if isinstance(b, list | tuple) else f"must {words} {b}"


def _rule(item: Mapping[str, Any], column: str | None, where: str) -> Rule:
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
    if comparator.endswith("Between") and not (isinstance(bound, list) and len(bound) == 2):
        raise QualityRuleInvalid(f"{where}: {comparator} takes [low, high]")
    description = str(item.get("description") or "").strip()
    if not description:
        raise QualityRuleInvalid(f"{where}: every quality rule needs a description quoting its source")
    if kind == "sql":
        query = str(item.get("query") or "")
        if "{object}" not in query or not query.lstrip().upper().startswith("SELECT"):
            raise QualityRuleInvalid(f"{where}: a sql rule is one SELECT over {{object}}")
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
    rules = [_rule(q, None, where) for q in schema.get("quality") or []]
    for p in schema.get("properties") or []:
        rules += [_rule(q, str(p.get("name")), f"{where}.{p.get('name')}") for q in p.get("quality") or []]
    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        raise QualityRuleInvalid(f"{where}: quality rule ids must be unique")
    if any(i.startswith(FILL_RATE_PREFIX) for i in ids):
        raise QualityRuleInvalid(
            f"{where}: ids starting {FILL_RATE_PREFIX} are kept for the generated fill-rate checks"
        )
    return tuple(rules)


def fill_rate_rules(columns: Sequence[tuple[str, bool]]) -> tuple[Rule, ...]:
    """One fill-rate check per column that is not required: (name, required) pairs in contract order."""
    return tuple(
        Rule(
            id=f"{FILL_RATE_PREFIX}{name}",
            kind="library",
            description=f"{name} is not empty in every row (an all-empty column would pass every value check).",
            dimension="completeness",
            severity="warn",
            comparator="mustBeLessThan",
            bound=1.0,
            column=name,
            metric=EMPTY_SHARE,
        )
        for name, required in columns
        if not required
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
    return f"SELECT count(*) FROM {relation} WHERE NOT {empty} AND {c} NOT IN ({listed})"  # noqa: S608 - quoted contract identifiers, literal codes


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
    """The number a rule compares, and how many rows it affects where the number is a row count."""
    row = con.execute(measure_sql(rule, _q(table))).fetchone()
    value = float(row[0]) if row and row[0] is not None else 0.0
    counts = rule.metric in ("invalidValues", "nullValues") or (
        rule.kind == "sql" and rule.comparator == "mustBe" and rule.bound == 0
    )
    return value, int(value) if counts else None


@dataclass(frozen=True)
class QualityReport:
    results: tuple[dict[str, Any], ...]
    rule_errors: tuple[dict[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": "contract-rules",
            "results": list(self.results),
            "rule_errors": list(self.rule_errors),
        }

    def caveats(self) -> list[str]:
        """Plain-words caveats for answers: data failures only (a broken rule is a maintainer's job)."""
        out = []
        for r in self.results:
            if r["status"] == "pass":
                continue
            lead = "A data check failed" if r["status"] == "fail" else "A data check gave a warning"
            out.append(
                f"{lead} ({r['test']}): {r['checks']} It {r['expected']}; it measured {r['observed']:g}."
            )
        return out

    @property
    def blocking(self) -> bool:
        """True when an error-severity rule failed or any rule could not run."""
        return any(r["status"] == "fail" for r in self.results) or bool(self.rule_errors)


def rules_for(contract: Any) -> tuple[Rule, ...]:
    """A contract's own quality rules, then a fill-rate check for each column that is not required."""
    return (*contract.quality, *fill_rate_rules([(c.name, c.required) for c in contract.columns]))


def run_rules(con: duckdb.DuckDBPyConnection, table: str, rules: Sequence[Rule]) -> QualityReport:
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for rule in rules:
        try:
            value, rows = measure(con, table, rule)
        except duckdb.Error as e:
            errors.append({"test": rule.id, "error": str(e).splitlines()[0]})
            continue
        ok = COMPARATORS[rule.comparator](value, rule.bound)
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
    return QualityReport(tuple(results), tuple(errors))
