"""Contract quality rules: parsed strictly, run on snapshots and live reads, failures apart from rule errors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import quality as q
from health_data_registry.registry import Column, RegistryError, load_registry
from health_data_registry.staging import stage_checked
from health_data_registry.store import Manifest, build_snapshot, judge_snapshot, latest_snapshot, write_ndjson
from tests.conftest import CONTRACT, RECORDS, raw_fetch, write_registry

REPO = Path(__file__).resolve().parents[1]


def doc(*column_rules: dict[str, Any], schema_rules: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    return {
        "schema": [
            {
                "quality": list(schema_rules),
                "properties": [{"name": "flag", "quality": list(column_rules)}, {"name": "n"}],
            }
        ]
    }


CODES = {
    "id": "flag_codes",
    "type": "library",
    "metric": "invalidValues",
    "arguments": {"validValues": ["-", "U"]},
    "mustBe": 0,
    "dimension": "conformity",
    "severity": "error",
    "description": "Codes from the codebook.",
}
ROWS = {
    "id": "rows",
    "type": "library",
    "metric": "rowCount",
    "mustBeBetween": [2, 3],
    "dimension": "completeness",
    "severity": "warn",
    "description": "Two or three rows.",
}
SQL = {
    "id": "n_small",
    "type": "sql",
    "query": "SELECT COUNT(*) FROM {object} WHERE n > 5",
    "mustBe": 0,
    "dimension": "accuracy",
    "severity": "error",
    "description": "No n above 5.",
}


def table(rows: list[tuple[Any, Any]]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE t (flag VARCHAR, n BIGINT)")
    if rows:
        con.executemany("INSERT INTO t VALUES (?, ?)", rows)
    return con


def test_parse_reads_schema_and_column_rules_in_order() -> None:
    rules = q.parse_rules(doc(CODES, schema_rules=(ROWS, SQL)), "x")
    assert [(r.id, r.column, r.kind, r.metric) for r in rules] == [
        ("rows", None, "library", "rowCount"),
        ("n_small", None, "sql", None),
        ("flag_codes", "flag", "library", "invalidValues"),
    ]
    assert rules[2].valid_values == ("-", "U")
    assert rules[0].bound_text() == "should be between 2 and 3"
    assert rules[1].bound_text() == "should equal 0"
    assert q.parse_rules({}, "x") == ()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"id": ""}, "x.flag: every quality rule needs an id"),
        ({"type": "custom"}, "x.flag.flag_codes: type must be library or sql"),
        ({"dimension": "vibes"}, "dimension must be one of accuracy, completeness"),
        ({"severity": "info"}, "severity must be error or warn"),
        ({"mustBe": None}, "give exactly one of mustBe"),
        ({"mustBeGreaterThan": 1}, "give exactly one of mustBe"),
        ({"description": " "}, "needs a description quoting its source"),
        ({"metric": "duplicateValues"}, "library metric must be one of invalidValues, nullValues, rowCount"),
        ({"arguments": {}}, "invalidValues needs arguments.validValues from the codebook"),
    ],
)
def test_parse_refuses_a_malformed_rule(change: dict[str, Any], message: str) -> None:
    rule = {k: v for k, v in {**CODES, **change}.items() if v is not None}
    with pytest.raises(q.QualityRuleInvalid, match=message):
        q.parse_rules(doc(rule), "x")


def test_parse_refuses_bad_sql_between_bounds_duplicates_and_reserved_ids() -> None:
    with pytest.raises(q.QualityRuleInvalid, match=r"a sql rule is one SELECT over \{object\}"):
        q.parse_rules(doc(schema_rules=({**SQL, "query": "SELECT COUNT(*) FROM t"},)), "x")
    with pytest.raises(q.QualityRuleInvalid, match=r"a sql rule is one SELECT over \{object\}"):
        q.parse_rules(doc(schema_rules=({**SQL, "query": "DELETE FROM {object}"},)), "x")
    with pytest.raises(q.QualityRuleInvalid, match=r"mustBeBetween takes \[low, high\]"):
        q.parse_rules(doc(schema_rules=({**ROWS, "mustBeBetween": 3},)), "x")
    with pytest.raises(q.QualityRuleInvalid, match="invalidValues is a column rule"):
        q.parse_rules(doc(schema_rules=(CODES,)), "x")
    with pytest.raises(q.QualityRuleInvalid, match="ids must be unique"):
        q.parse_rules(doc(CODES, schema_rules=({**ROWS, "id": "flag_codes"},)), "x")
    with pytest.raises(q.QualityRuleInvalid, match="kept for the generated checks"):
        q.parse_rules(doc({**CODES, "id": "fill_rate_flag"}), "x")
    with pytest.raises(q.QualityRuleInvalid, match="the id has_rows and ids starting fill_rate_"):
        q.parse_rules(doc({**CODES, "id": "has_rows"}), "x")


@pytest.mark.parametrize(
    "query",
    [
        "SELECT COUNT(*) FROM {object}; DROP TABLE {object}",
        "SELECT 0 FROM {object}; DELETE FROM {object} WHERE n > 0",
        "SELECT COUNT(*) FROM {object} WHERE n IN (SELECT 1); INSERT INTO {object} VALUES ('x', 1)",
        "SELECT COUNT(*) FROM read_csv('/etc/passwd'), {object}",
        "SELECT COUNT(*) FROM {object} JOIN other ON true",
        "SELECT COUNT(*) FROM {object} WHERE gone > 0",
    ],
)
def test_a_sql_rule_must_pass_the_query_sql_guard(query: str) -> None:
    """Contract SQL may be model-drafted: one read-only SELECT over the contract's own columns, or refused."""
    with pytest.raises(q.QualityRuleInvalid, match=r"a sql rule is one read-only SELECT over \{object\} \("):
        q.parse_rules(doc(schema_rules=({**SQL, "query": query},)), "x")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"mustBe": "0"}, "mustBe takes a number"),
        ({"mustBe": True}, "mustBe takes a number"),
        ({"mustBe": None, "mustBeBetween": ["1", 2]}, r"mustBeBetween takes \[low, high\], two numbers"),
        ({"mustBe": None, "mustBeBetween": [3, 1]}, "with low at most high"),
    ],
)
def test_bounds_are_numbers(change: dict[str, Any], message: str) -> None:
    rule = {k: v for k, v in {**SQL, **change}.items() if v is not None}
    with pytest.raises(q.QualityRuleInvalid, match=message):
        q.parse_rules(doc(schema_rules=(rule,)), "x")


def test_a_multi_statement_or_write_rule_never_runs_even_if_it_skipped_the_parser() -> None:
    """Defence in depth: a Rule built by hand (not through parse_rules) is re-checked before it runs."""
    con = table([("-", 1)])
    rules = [
        q.Rule("drop", "sql", "d", "consistency", "error", "mustBe", 0, query="SELECT 1 FROM {object}; DROP TABLE {object}"),
        q.Rule("write", "sql", "d", "consistency", "error", "mustBe", 0, query="DELETE FROM {object}"),
    ]  # fmt: skip
    report = q.run_rules(con, "t", rules)
    assert [e["test"] for e in report.rule_errors] == ["drop", "write"] and report.results == ()
    assert con.execute("SELECT count(*) FROM t").fetchone() == (1,)  # the table is still there, unchanged


def test_snapshot_checks_run_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry import staging

    path = str(tmp_path / "s.duckdb")
    with duckdb.connect(path) as w:
        w.execute("CREATE TABLE toy_cases AS SELECT 1 AS cases")
    seen: list[str] = []

    def probe(con: duckdb.DuckDBPyConnection, table: str, rules: Any) -> q.QualityReport:
        for stmt in (f"DROP TABLE {table}", "CREATE TABLE x (a INT)", "COPY (SELECT 1) TO 'leak.csv'"):
            with pytest.raises(duckdb.Error) as e:
                con.execute(stmt)
            seen.append(type(e.value).__name__)
        return q.QualityReport((), ())

    monkeypatch.setattr(staging, "run_rules", probe)
    contract = load_registry(write_registry(tmp_path / "reg")).get("toy_cases").contract
    assert staging.check_snapshot(path, contract, "toy_cases") == q.QualityReport((), ())
    assert len(seen) == 3
    with duckdb.connect(path, read_only=True) as r:
        assert r.execute("SELECT count(*) FROM toy_cases").fetchone() == (1,)


def test_a_null_measurement_or_a_bad_bound_is_a_rule_error_never_a_pass() -> None:
    con = table([])
    rules = [
        q.Rule("biggest", "sql", "d", "accuracy", "error", "mustBeLessOrEqualTo", 5, query="SELECT max(n) FROM {object}"),
        q.Rule("zero", "sql", "d", "accuracy", "error", "mustBe", 0, query="SELECT max(n) FROM {object}"),
        q.Rule("text", "sql", "d", "accuracy", "error", "mustBeGreaterThan", "5", query="SELECT count(*) FROM {object}"),
    ]  # fmt: skip
    report = q.run_rules(con, "t", rules)
    assert report.results == ()
    assert report.rule_errors == (
        {"test": "biggest", "error": "the check returned no number"},
        {"test": "zero", "error": "the check returned no number"},
        {"test": "text", "error": "'>' not supported between instances of 'float' and 'str'"},
    )
    assert report.blocking and report.caveats() == []


def test_run_separates_passes_failures_warnings_and_rule_errors() -> None:
    con = table([("-", 1), ("X", 9), ("U", None)])
    broken = q.Rule("broken", "sql", "Names a column that is gone.", "consistency", "error", "mustBe", 0,
                    query="SELECT COUNT(*) FROM {object} WHERE gone > 0")  # fmt: skip
    rules = (*q.parse_rules(doc(CODES, schema_rules=(ROWS, SQL)), "x"), broken)
    report = q.run_rules(con, "t", rules)
    by_id = {r["test"]: r for r in report.results}
    assert by_id["rows"] == {
        "test": "rows", "checks": "Two or three rows.", "status": "pass", "rows_affected": None,
        "dimension": "completeness", "severity": "warn", "column": None, "observed": 3.0,
        "expected": "should be between 2 and 3",
    }  # fmt: skip
    assert (by_id["n_small"]["status"], by_id["n_small"]["rows_affected"]) == ("fail", 1)
    assert (by_id["flag_codes"]["status"], by_id["flag_codes"]["observed"]) == ("fail", 1.0)
    assert [e["test"] for e in report.rule_errors] == ["broken"]
    assert "gone" in report.rule_errors[0]["error"]
    assert report.blocking
    assert report.caveats() == [
        "A data check on this source failed: No n above 5 (it should equal 0; this copy measured 1). The numbers are "
        "shown as the publisher released them; compare with the publisher's page before relying on them.",
        "A data check on this source failed: Codes from the codebook (it should equal 0; this copy measured 1). The "
        "numbers are shown as the publisher released them; compare with the publisher's page before relying on them.",
    ]
    out = report.as_dict()
    assert (
        out["tool"] == "contract-rules"
        and len(out["results"]) == 3
        and out["rule_errors"] == list(report.rule_errors)
    )


def test_a_warn_rule_warns_and_does_not_block() -> None:
    report = q.run_rules(table([("-", 1)]), "t", q.parse_rules(doc(schema_rules=(ROWS,)), "x"))
    assert [r["status"] for r in report.results] == ["warn"]
    assert not report.blocking
    assert report.caveats() == [
        "A data check on this source gave a warning: Two or three rows (it should be between 2 and 3; this copy "
        "measured 1). The numbers are shown as the publisher released them; compare with the publisher's page "
        "before relying on them."
    ]


@pytest.mark.parametrize(
    ("comparator", "bound", "value", "ok"),
    [("mustBe", 2, 2, True), ("mustNotBe", 2, 2, False), ("mustBeGreaterThan", 2, 2, False),
     ("mustBeGreaterOrEqualTo", 2, 2, True), ("mustBeLessThan", 2, 1, True), ("mustBeLessOrEqualTo", 2, 3, False),
     ("mustBeBetween", [1, 2], 2, True), ("mustNotBeBetween", [1, 2], 3, True)],
)  # fmt: skip
def test_comparators(comparator: str, bound: Any, value: Any, ok: bool) -> None:
    assert q.COMPARATORS[comparator](value, bound) is ok


def col(name: str, description: str, *, required: bool = False, flag_for: str | None = None,
        may_be_empty: bool = False) -> Column:  # fmt: skip
    return Column(name, "VARCHAR", "string", description, required, "attribute", name,  # type: ignore[arg-type]
                  flag_for=flag_for, may_be_empty=may_be_empty)  # fmt: skip


def test_fill_rate_flags_only_an_entirely_empty_optional_column() -> None:
    columns = [
        col("flag", "Whether the count was suppressed. Codes from the codebook."),
        col("n", "Cases.", required=True),
        col("n_flag", "Why the count is empty.", flag_for="n"),
        col("note", "A note the publisher adds to a few rows.", may_be_empty=True),
    ]
    rules = q.fill_rate_rules(columns)
    assert [(r.id, r.column, r.severity, r.dimension) for r in rules] == [
        ("fill_rate_flag", "flag", "warn", "completeness")
    ]
    assert rules[0].description == "No row has a value for whether the count was suppressed."
    empty = q.run_rules(table([(" ", 1), (None, 2)]), "t", rules)
    assert [r["status"] for r in empty.results] == ["warn"]
    assert empty.caveats() == [
        "No row has a value for whether the count was suppressed. Answers that need it will be blank: ask for "
        "another measure, or check the publisher's page."
    ]
    some = q.run_rules(table([(None, 1), ("-", 2)]), "t", rules).results[0]
    assert (some["status"], some["observed"], some["rows_affected"]) == ("pass", 0.5, None)
    assert q.run_rules(table([]), "t", rules).results[0]["status"] == "pass"  # the volume check covers this
    assert q.fill_rate_rules([col("x", "MMWR year.")])[0].description == "No row has a value for MMWR year."


def test_an_empty_table_fails_the_volume_check() -> None:
    rule = q.has_rows_rule()
    assert (rule.id, rule.severity, rule.metric) == ("has_rows", "error", "rowCount")
    assert rule.bound_text() == "should be more than 0"
    report = q.run_rules(table([]), "t", [rule])
    assert [r["status"] for r in report.results] == ["fail"] and report.blocking
    assert report.caveats()[0].startswith(
        "A data check on this source failed: This copy of the source has no rows, so there is nothing to answer "
        "from (it should be more than 0; this copy measured 0)."
    )
    assert q.run_rules(table([("-", 1)]), "t", [rule]).results[0]["status"] == "pass"


def test_a_value_rule_with_nothing_to_judge_is_not_evaluated_and_blocks() -> None:
    """ADR 0017 data-quality review: an error-severity value rule on a column with no values is not a pass."""
    rules = q.parse_rules(doc(CODES), "x")
    report = q.run_rules(table([(None, 1), ("  ", 2)]), "t", rules)
    (result,) = report.results
    assert (result["status"], result["observed"], result["severity"]) == ("not_evaluated", None, "error")
    assert report.rule_errors == () and report.blocking  # apart from a metric error, and it blocks
    assert report.caveats() == [
        "A data check on this source could not be judged: Codes from the codebook (no row has a value in that "
        "column, so there was nothing to check). Compare with the publisher's page before relying on these numbers."
    ]
    assert q.blocked_message("x", report.as_dict()) == (
        "x: the error-severity check flag_codes could not be judged (no values to check), so this copy is not "
        "served or published and the copy before it stays. Compare the data with the publisher's page, then "
        "refresh, or have a maintainer fix the contract."
    )
    # Warn severity: reported, never blocking.
    warn = q.run_rules(table([(None, 1)]), "t", q.parse_rules(doc({**CODES, "severity": "warn"}), "x"))
    assert warn.results[0]["status"] == "not_evaluated" and not warn.blocking
    # A column the contract marks mayBeEmpty is expected to be empty: the rule passes.
    may = {"schema": [{"properties": [
        {"name": "flag", "quality": [CODES], "customProperties": [{"property": "mayBeEmpty", "value": True}]},
        {"name": "n"}]}]}  # fmt: skip
    (rule,) = q.parse_rules(may, "x")
    assert rule.may_be_empty and q.run_rules(table([(None, 1)]), "t", [rule]).results[0]["status"] == "pass"
    # One value is enough to judge.
    assert q.run_rules(table([(None, 1), ("-", 2)]), "t", rules).results[0]["status"] == "pass"


def test_results_are_keyed_by_rule_id_whatever_the_order() -> None:
    """Python checks and dbt tests may come back in any order: compare them by rule id, never by position."""
    rules = q.parse_rules(doc(CODES, schema_rules=(ROWS, SQL)), "x")
    con = table([("-", 1), ("X", 9), ("U", None)])
    forward = {r["test"]: r for r in q.run_rules(con, "t", rules).results}
    backward = {r["test"]: r for r in q.run_rules(con, "t", rules[::-1]).results}
    assert forward == backward and set(forward) == {"rows", "n_small", "flag_codes"}


def test_null_values_compares_a_count() -> None:

    rule = {**CODES, "id": "few_empty", "metric": "nullValues", "mustBeLessOrEqualTo": 1}
    del rule["mustBe"]
    report = q.run_rules(table([(None, 1), ("", 2), ("-", 3)]), "t", q.parse_rules(doc(rule), "x"))
    assert (report.results[0]["observed"], report.results[0]["status"]) == (2.0, "fail")


def test_every_contract_rule_parses_with_a_dimension_and_severity() -> None:
    reg = load_registry(REPO)
    rules = [r for ds in reg for r in ds.contract.quality]
    assert rules and all(r.dimension in q.DIMENSIONS and r.severity in q.SEVERITIES for r in rules)


def test_registry_refuses_a_contract_with_a_bad_rule(tmp_path: Path) -> None:
    bad = CONTRACT.replace(
        '          - {property: missingCodes, value: ["-", "N"]}\n',
        '          - {property: missingCodes, value: ["-", "N"]}\n'
        "        quality:\n"
        "          - {id: c, type: library, metric: rowCount, mustBe: 1, dimension: vibes, severity: error,"
        " description: x}\n",
    )
    with pytest.raises(RegistryError, match="toy_cases.cases.c: dimension must be one of"):
        load_registry(write_registry(tmp_path / "reg", contract=bad))


RULED = CONTRACT.replace(
    '          - {property: missingCodes, value: ["-", "N"]}\n',
    '          - {property: missingCodes, value: ["-", "N"]}\n'
    "        quality:\n"
    "          - {id: cases_under_5000, type: sql, query: 'SELECT COUNT(*) FROM {object} WHERE cases >= 5000',"
    " mustBe: 0, dimension: accuracy, severity: error, description: Toy bound.}\n",
)


def _blocked_snapshot(tmp_path: Path, contract: str = RULED) -> tuple[Any, Path, Any]:
    """A snapshot whose error-severity rule fails: built and kept on disk, but never promoted to LATEST."""
    ds = load_registry(write_registry(tmp_path / "reg", contract=contract)).get("toy_cases")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    lake.mkdir()
    with pytest.raises(q.QualityBlocked) as e:
        build_snapshot(lake, ds, [raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)], [page])
    (manifest,) = (lake / "datasets" / "toy_cases").rglob("manifest.json")
    return ds, lake, (e.value, Manifest.read(manifest), page)


def test_a_failed_error_rule_blocks_latest_and_publishing(tmp_path: Path) -> None:
    ds, lake, (err, m, _page) = _blocked_snapshot(tmp_path)
    assert str(err) == (
        "toy_cases: the error-severity check cases_under_5000 failed, so this copy is not served or published and "
        "the copy before it stays. Compare the data with the publisher's page, then refresh, or have a maintainer "
        "fix the contract."
    )
    assert not (lake / "datasets" / "toy_cases" / "LATEST").exists()  # LATEST never moved
    assert latest_snapshot(lake, "toy_cases") is None
    # The same inputs again: the kept snapshot is still refused, never promoted on a second run.
    page = tmp_path / "p.ndjson"
    with pytest.raises(q.QualityBlocked):
        build_snapshot(lake, ds, [raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)], [page])
    assert not (lake / "datasets" / "toy_cases" / "LATEST").exists()
    # A rerun with a good copy already in LATEST: the early-return path for a kept snapshot leaves it there.
    (lake / "datasets" / "toy_cases" / "LATEST").write_text("20261101T000000Z-good\n")
    with pytest.raises(q.QualityBlocked, match="cases_under_5000 failed"):
        build_snapshot(lake, ds, [raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)], [page])
    assert (lake / "datasets" / "toy_cases" / "LATEST").read_text() == "20261101T000000Z-good\n"
    # A LATEST that names a blocked copy (a lake from before the rule) is refused by export-public's checks.
    (lake / "datasets" / "toy_cases" / "LATEST").write_text(m.snapshot_id + "\n")
    from health_data_registry.publish import PublishCheckFailed, export_public

    with pytest.raises(
        PublishCheckFailed, match="toy_cases: the error-severity check cases_under_5000 failed"
    ):
        export_public(load_registry(tmp_path / "reg"), lake, str(tmp_path / "out"), run_date="2026-11-25")

    record = {"tool": "contract-rules", "results": [], "rule_errors": [{"test": "a"}, {"test": "b"}]}
    assert q.blocked_message("x", record) == (
        "x: the checks a, b could not run, so this copy is not served or published and the copy before it stays. "
        "Compare the data with the publisher's page, then refresh, or have a maintainer fix the contract."
    )
    # No record at all, one no contract-rules run wrote, or one with no result, is not a pass (fail closed, #222).
    unchecked = (
        "x: no data checks have run on this copy, so it is not served or published and the copy before it "
        "stays. Refresh it, or have a maintainer check the contract."
    )
    assert q.blocked_message("x", None) == unchecked
    assert q.blocked_message("x", {}) == unchecked
    assert q.blocked_message("x", {"results": [], "rule_errors": []}) == unchecked
    assert q.blocked_message("x", {"tool": "contract-rules", "results": [], "rule_errors": []}) == unchecked


def test_a_kept_snapshot_is_judged_by_the_current_rules_not_its_stored_record(tmp_path: Path) -> None:
    """A snapshot built before the rules (no quality record), or before a rule was tightened, takes the rerun path
    on a refresh with the same bytes: promotion and export-public run the contract's rules on it again."""
    loose = load_registry(
        write_registry(tmp_path / "loose", contract=RULED.replace("severity: error", "severity: warn"))
    ).get("toy_cases")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    lake.mkdir()
    fetch = raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)
    m = build_snapshot(lake, loose, [fetch], [page])
    (manifest_path,) = (lake / "datasets" / "toy_cases").rglob("manifest.json")
    stored = json.loads(manifest_path.read_text())
    stored["quality"] = {}  # as every manifest written before the rules existed
    manifest_path.write_text(json.dumps(stored))
    latest = lake / "datasets" / "toy_cases" / "LATEST"
    latest.write_text("20261101T000000Z-good\n")
    strict = load_registry(write_registry(tmp_path / "reg", contract=RULED))
    with pytest.raises(q.QualityBlocked, match="cases_under_5000 failed"):
        build_snapshot(lake, strict.get("toy_cases"), [fetch], [page])
    assert latest.read_text() == "20261101T000000Z-good\n"  # LATEST never moved to the unchecked copy
    assert Manifest.read(manifest_path).quality == {}  # the kept snapshot is never rewritten
    latest.write_text(m.snapshot_id + "\n")
    from health_data_registry.publish import PublishCheckFailed, export_public

    with pytest.raises(
        PublishCheckFailed, match="toy_cases: the error-severity check cases_under_5000 failed"
    ):
        export_public(strict, lake, str(tmp_path / "out"), run_date="2026-11-25")
    # A snapshot whose file is gone can't be checked, so it blocks.
    (manifest_path.parent / "data.duckdb").unlink()
    judged = judge_snapshot(manifest_path.parent, strict.get("toy_cases"))
    assert judged["rule_errors"] == [
        {"test": "contract-rules", "error": "data.duckdb is missing from the snapshot"}
    ]
    assert q.blocked_message("toy_cases", judged) == (
        "toy_cases: the check contract-rules could not run, so this copy is not served or published and the copy "
        "before it stays. Compare the data with the publisher's page, then refresh, or have a maintainer fix the "
        "contract."
    )


def test_a_warn_rule_does_not_block_latest(tmp_path: Path) -> None:
    ds = load_registry(
        write_registry(tmp_path / "reg", contract=RULED.replace("severity: error", "severity: warn"))
    ).get("toy_cases")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    lake.mkdir()
    m = build_snapshot(lake, ds, [raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)], [page])
    assert latest_snapshot(lake, "toy_cases") is not None and m.notes[0].startswith(
        "A data check on this source gave a warning: Toy bound"
    )
    # A partial development sample is never published, so its failed rules are caveats only.
    partial = load_registry(write_registry(tmp_path / "reg2", contract=RULED)).get("toy_cases")
    sample = build_snapshot(
        lake, partial, [raw_fetch("https://data.cdc.gov/resource/b.json", RECORDS)], [page], partial=True
    )
    assert sample.partial and any(r["status"] == "fail" for r in sample.quality["results"])


def test_snapshots_and_live_tables_carry_their_quality_results(tmp_path: Path) -> None:
    ds, _lake, (_err, m, page) = _blocked_snapshot(tmp_path)

    tests = {r["test"]: r["status"] for r in m.quality["results"]}
    assert tests == {"has_rows": "pass", "cases_under_5000": "fail", "fill_rate_cases": "pass",
                     "fill_rate_rate_per_100k": "pass"}  # fmt: skip
    assert m.quality["rule_errors"] == []
    assert m.notes == [
        "A data check on this source failed: Toy bound (it should equal 0; this copy measured 1). The numbers are "
        "shown as the publisher released them; compare with the publisher's page before relying on them."
    ]
    con = duckdb.connect()
    con.execute("CREATE TABLE raw AS SELECT * FROM read_json(?, format='newline_delimited', columns={states: 'VARCHAR', "
                "year: 'VARCHAR', label: 'VARCHAR', m1: 'VARCHAR', m2: 'VARCHAR'})", [str(page)])  # fmt: skip
    staged = stage_checked(con, ds.contract, "raw", table="toy_cases", temp=True)
    assert staged.notes() == m.notes
    assert staged.quality.as_dict() == m.quality


# Issue #222: a run that leaves a check with no result proves nothing, so it blocks (fail closed).
NO_CHECKS_RAN = (
    "toy_cases: no data checks have run on this copy, so it is not served or published and the copy before it "
    "stays. Refresh it, or have a maintainer check the contract."
)


def _drop(test: str) -> Any:
    """A check_snapshot whose report lost one check's result, and that check from its own expected list."""
    from health_data_registry import staging

    def probe(path: str, contract: Any, table: str) -> q.QualityReport:
        real = staging.check_snapshot(path, contract, table)
        return q.QualityReport(
            tuple(r for r in real.results if r["test"] != test),
            real.rule_errors,
            tuple(i for i in real.expected if i != test),
        )

    return probe


@pytest.mark.parametrize(
    ("probe", "message"),
    [
        (lambda *_: q.QualityReport((), ()), NO_CHECKS_RAN),
        (lambda *_: q.QualityReport((), (), ("has_rows",)), NO_CHECKS_RAN),
        (
            _drop("has_rows"),
            "toy_cases: the check has_rows returned no result (never judged), so this copy is not served or "
            "published and the copy before it stays. Compare the data with the publisher's page, then refresh, or "
            "have a maintainer fix the contract.",
        ),
    ],
    ids=["empty-results", "empty-results-with-expected", "missing-rule-id"],
)
def test_checks_with_no_result_block_latest_and_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe: Any, message: str
) -> None:
    from health_data_registry import store
    from health_data_registry.publish import PublishCheckFailed, export_public

    reg = load_registry(write_registry(tmp_path / "reg"))
    ds = reg.get("toy_cases")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    lake.mkdir()
    good = build_snapshot(lake, ds, [raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)], [page])
    latest = lake / "datasets" / "toy_cases" / "LATEST"
    assert latest.read_text() == good.snapshot_id + "\n"
    monkeypatch.setattr(store, "check_snapshot", probe)
    # A fresh build whose checks came back incomplete: kept for inspection, LATEST stays on the good copy.
    with pytest.raises(q.QualityBlocked) as e:
        build_snapshot(lake, ds, [raw_fetch("https://data.cdc.gov/resource/b.json", RECORDS)], [page])
    assert str(e.value) == message and latest.read_text() == good.snapshot_id + "\n"
    # The same bytes again (the rerun path re-judges the kept copy): still blocked.
    with pytest.raises(q.QualityBlocked):
        build_snapshot(lake, ds, [raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)], [page])
    assert latest.read_text() == good.snapshot_id + "\n"
    # export-public re-judges LATEST and refuses it too.
    with pytest.raises(PublishCheckFailed, match=r"toy_cases: .*(no data checks have run|never judged)"):
        export_public(reg, lake, str(tmp_path / "out"), run_date="2026-11-25")


def test_a_record_cannot_drop_a_check_the_contract_expects() -> None:
    record = {"tool": "contract-rules", "results": [{"test": "a", "status": "pass"}], "rule_errors": [],
              "expected": ["a"]}  # fmt: skip
    assert q.blocked_message("x", record) is None
    assert "the check b returned no result" in str(q.blocked_message("x", record, expected=("a", "b")))
    assert "the check b returned no result" in str(q.sample_blocked_message("x", record, expected=("a", "b")))
    # Every check reported, as a result or a rule error: nothing is missing.
    assert not q.QualityReport(
        ({"test": "a", "status": "pass"},), ({"test": "b", "error": "x"},), ("a", "b")
    ).never_ran


def test_a_rule_error_a_data_failure_and_a_missing_result_are_named_apart() -> None:
    record = {
        "tool": "contract-rules",
        "results": [
            {
                "test": "a",
                "checks": "A.",
                "status": "fail",
                "severity": "error",
                "observed": 1.0,
                "expected": "should equal 0",
            }
        ],
        "rule_errors": [{"test": "b", "error": "column gone"}],
        "expected": ["a", "b", "c"],
    }
    assert q.blocked_message("x", record) == (
        "x: the error-severity check a failed and the check b could not run and the check c returned no result "
        "(never judged), so this copy is not served or published and the copy before it stays. Compare the data "
        "with the publisher's page, then refresh, or have a maintainer fix the contract."
    )
    report = q._report(record)
    assert report.never_ran == ("c",) and [e["test"] for e in report.rule_errors] == ["b"]
    assert [r["test"] for r in report.results if r["status"] == "fail"] == ["a"]
    # A rule error is never a caveat about the numbers; a data failure is.
    assert len(report.caveats()) == 1


def test_a_contract_with_no_rules_of_its_own_is_served_with_a_caveat(tmp_path: Path) -> None:
    from health_data_registry import tools

    reg = load_registry(write_registry(tmp_path / "reg"))
    ds = reg.get("toy_cases")
    assert ds.contract.quality == ()
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    lake.mkdir()
    m = build_snapshot(lake, ds, [raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)], [page])
    assert latest_snapshot(lake, "toy_cases") is not None  # not blocked
    assert (
        m.quality["expected"]
        == list(q.expected_ids(ds.contract))
        == [r["test"] for r in m.quality["results"]]
    )
    assert q.blocked_message("toy_cases", m.quality, expected=q.expected_ids(ds.contract)) is None
    rows = tools.list_caveats(reg, source_id="toy_cases", fmt="json")["rows"]
    assert [r[1:] for r in rows if r[1] == "data checks"] == [
        ["data checks", "No quality rules declared: the contract sets no checks of its own on these numbers, so "
         "only the general checks run (the table has rows; columns are not left empty). Compare with the "
         "publisher's page before relying on them."]
    ]  # fmt: skip
    ruled = load_registry(write_registry(tmp_path / "ruled", contract=RULED))
    rows = tools.list_caveats(ruled, source_id="toy_cases", fmt="json")["rows"]
    assert not [r for r in rows if r[1] == "data checks"]
