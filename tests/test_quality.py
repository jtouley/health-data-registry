"""Contract quality rules: parsed strictly, run on snapshots and live reads, failures apart from rule errors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import quality as q
from health_data_registry.registry import RegistryError, load_registry
from health_data_registry.staging import stage_checked
from health_data_registry.store import build_snapshot, write_ndjson
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
    assert rules[0].bound_text() == "must be between 2 and 3"
    assert rules[1].bound_text() == "must equal 0"
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
    with pytest.raises(q.QualityRuleInvalid, match="kept for the generated fill-rate checks"):
        q.parse_rules(doc({**CODES, "id": "fill_rate_flag"}), "x")


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
        "expected": "must be between 2 and 3",
    }  # fmt: skip
    assert (by_id["n_small"]["status"], by_id["n_small"]["rows_affected"]) == ("fail", 1)
    assert (by_id["flag_codes"]["status"], by_id["flag_codes"]["observed"]) == ("fail", 1.0)
    assert [e["test"] for e in report.rule_errors] == ["broken"]
    assert "gone" in report.rule_errors[0]["error"]
    assert report.blocking
    assert report.caveats() == [
        "A data check failed (n_small): No n above 5. It must equal 0; it measured 1.",
        "A data check failed (flag_codes): Codes from the codebook. It must equal 0; it measured 1.",
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
        "A data check gave a warning (rows): Two or three rows. It must be between 2 and 3; it measured 1."
    ]


@pytest.mark.parametrize(
    ("comparator", "bound", "value", "ok"),
    [("mustBe", 2, 2, True), ("mustNotBe", 2, 2, False), ("mustBeGreaterThan", 2, 2, False),
     ("mustBeGreaterOrEqualTo", 2, 2, True), ("mustBeLessThan", 2, 1, True), ("mustBeLessOrEqualTo", 2, 3, False),
     ("mustBeBetween", [1, 2], 2, True), ("mustNotBeBetween", [1, 2], 3, True)],
)  # fmt: skip
def test_comparators(comparator: str, bound: Any, value: Any, ok: bool) -> None:
    assert q.COMPARATORS[comparator](value, bound) is ok


def test_fill_rate_flags_only_an_entirely_empty_optional_column() -> None:
    rules = q.fill_rate_rules([("flag", False), ("n", True)])
    assert [(r.id, r.column, r.severity, r.dimension) for r in rules] == [
        ("fill_rate_flag", "flag", "warn", "completeness")
    ]
    assert [r["status"] for r in q.run_rules(table([(" ", 1), (None, 2)]), "t", rules).results] == ["warn"]
    some = q.run_rules(table([(None, 1), ("-", 2)]), "t", rules).results[0]
    assert (some["status"], some["observed"], some["rows_affected"]) == ("pass", 0.5, 1)
    assert q.run_rules(table([]), "t", rules).results[0]["status"] == "pass"  # nothing to measure


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


def test_snapshots_and_live_tables_carry_their_quality_results(tmp_path: Path) -> None:
    ruled = CONTRACT.replace(
        '          - {property: missingCodes, value: ["-", "N"]}\n',
        '          - {property: missingCodes, value: ["-", "N"]}\n'
        "        quality:\n"
        "          - {id: cases_under_5000, type: sql, query: 'SELECT COUNT(*) FROM {object} WHERE cases >= 5000',"
        " mustBe: 0, dimension: accuracy, severity: error, description: Toy bound.}\n",
    )
    ds = load_registry(write_registry(tmp_path / "reg", contract=ruled)).get("toy_cases")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    lake.mkdir()
    m = build_snapshot(lake, ds, [raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)], [page])
    tests = {r["test"]: r["status"] for r in m.quality["results"]}
    assert tests == {"cases_under_5000": "fail", "fill_rate_cases": "pass", "fill_rate_rate_per_100k": "pass"}
    assert m.quality["rule_errors"] == []
    assert m.notes == ["A data check failed (cases_under_5000): Toy bound. It must equal 0; it measured 1."]
    con = duckdb.connect()
    con.execute("CREATE TABLE raw AS SELECT * FROM read_json(?, format='newline_delimited', columns={states: 'VARCHAR', "
                "year: 'VARCHAR', label: 'VARCHAR', m1: 'VARCHAR', m2: 'VARCHAR'})", [str(page)])  # fmt: skip
    staged = stage_checked(con, ds.contract, "raw", table="toy_cases", temp=True)
    assert staged.notes() == m.notes
    assert staged.quality.as_dict() == m.quality
