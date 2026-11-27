"""cdc_abcs_pneumococcal (ABCs Streptococcus pneumoniae, data.cdc.gov en3s-hzsr) through the real catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names and labels are the publisher's.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import quality, tools
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_abcs_pneumococcal"
BUG = "Streptococcus pneumoniae"
RATE, COUNT, PCT = "Per 100,000 population", "Counts", "Percent"
RECORDS = [
    {"year": "2022", "value": "8.5", "units": RATE, "bacteria": BUG, "topic": "Case Rates", "viewby": "Overall"},
    {"year": "2023", "value": "9.5", "units": RATE, "bacteria": BUG, "topic": "Case Rates", "viewby": "Overall"},
    {"year": "2023", "value": "30.0", "units": RATE, "bacteria": BUG, "topic": "Case Rates", "viewby": "Age",
     "viewby2": "≥65 years old"},
    {"year": "2023", "value": "9.5", "units": RATE, "bacteria": BUG, "topic": "Case Rates", "viewby": "Age",
     "viewby2": "Overall"},
    {"year": "2023", "value": "1200", "units": COUNT, "bacteria": BUG, "topic": "Number of Cases and Deaths",
     "viewby": "ALL", "viewby2": "Total cases"},
    {"year": "2023", "value": "150", "units": COUNT, "bacteria": BUG, "topic": "Number of Cases and Deaths",
     "viewby": "ALL", "viewby2": "Number of deaths"},
    {"year": "2023", "value": "6.0", "units": PCT, "bacteria": BUG, "topic": "Syndromes", "viewby": "Meningitis"},
    {"year": "2023", "units": PCT, "bacteria": BUG, "topic": "Antibiotic resistance", "viewby": "Tetracycline3"},
]  # fmt: skip


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine, Path]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(
        lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/en3s-hzsr.json", RECORDS)], [f]
    )
    return reg, Engine(registry=reg, lake=lake), lake


def _rows(out: Any) -> list[dict[str, str]]:
    text = out if isinstance(out, str) else out.render("csv")
    return list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#")))


def test_a_plain_question_about_pneumococcal_serotypes_routes_here() -> None:
    reg = load_registry(ROOT)
    rows = _rows(tools.suggest_datasets(reg, "Streptococcus pneumoniae rates by vaccine serotype"))
    assert rows[0]["source_id"] == DS
    assert rows[0]["can_answer"] == "yes"


def test_a_state_question_is_told_the_figures_are_for_the_surveillance_areas() -> None:
    reg = load_registry(ROOT)
    out = tools.suggest_datasets(reg, "Streptococcus pneumoniae in Colorado")
    row = next(r for r in _rows(out) if r["source_id"] == DS)
    assert row["can_answer"] == "no"
    assert "goes down to surveillance areas only, not state" in row["why"]


def test_rate_and_count_are_each_read_from_one_published_cell(real: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = real
    found = {s.metric: s for _, s in tools.series_for(reg, BUG, source_id=DS)}
    assert {"ipd_case_rate", "ipd_cases", "ipd_deaths"} <= set(found)
    for metric, want in (("ipd_case_rate", [(2022, 8.5), (2023, 9.5)]), ("ipd_cases", [(2023, 1200)])):
        out = eng.query_metric(DS, metric, group_by=["year"])
        assert "sum(" not in out["sql"], metric
        at = {c: i for i, c in enumerate(out["columns"])}
        assert sorted((r[at["year"]], r[at[metric]]) for r in out["rows"]) == want, metric


def test_get_series_names_the_surveillance_areas_and_never_a_state(
    real: tuple[Registry, Engine, Path],
) -> None:
    reg, eng, _ = real
    out = tools.get_series(reg, eng, condition=BUG, measure="rate", source_id=DS, fmt="json")
    assert isinstance(out, dict), out
    at = {c: i for i, c in enumerate(out["columns"])}
    assert {r[at["location"]] for r in out["rows"]} == {
        "ABCs surveillance areas (a catchment of selected counties, not the United States)"
    }
    assert sorted((r[at["year"]], r[at["value"]]) for r in out["rows"]) == [(2022, 8.5), (2023, 9.5)]
    declined = tools.get_series(
        reg, eng, condition=BUG, locations=["United States"], source_id=DS, fmt="json"
    )
    assert isinstance(declined, dict)
    assert declined["rows"] == []


def test_raw_layer_keeps_the_publisher_values_as_sent(real: tuple[Registry, Engine, Path]) -> None:
    """Hard rule 15: the raw records dbt reads hold every value exactly as the publisher sent it, the missing
    resistance value included (absent, never a zero)."""
    _, _, lake = real
    files = list((lake / "raw_records" / DS).glob("*.ndjson"))
    assert len(files) == 1
    kept = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    for sent, row in zip(RECORDS, kept, strict=True):
        assert {k: row[k] for k in sent} == sent
    assert kept[-1].get("value") in (None, "")


def test_a_by_age_rate_leaves_out_the_publishers_overall_row(real: tuple[Registry, Engine, Path]) -> None:
    """The Age view's Overall row is a total (totalRows), so a by-age answer holds age groups only."""
    _, eng, _ = real
    out = eng.query_metric(DS, "ipd_case_rate_by_age", group_by=["year", "stratum"])
    at = {c: i for i, c in enumerate(out["columns"])}
    assert [(r[at["stratum"]], r[at["ipd_case_rate_by_age"]]) for r in out["rows"]] == [
        ("≥65 years old", 30.0)
    ]


def test_get_aligned_with_gbd_says_no_cause_is_mapped_rather_than_calling_it_lri(
    real: tuple[Registry, Engine, Path],
) -> None:
    reg, eng, _ = real
    out = tools.get_aligned_with_gbd(reg, eng, condition=BUG, fmt="json")
    assert isinstance(out, dict), out
    assert out["rows"] == []
    assert any(f"{DS}:" in n and "no GBD cause mapping" in n for n in out["notes"]), out["notes"]


def _typed(records: list[dict[str, Any]]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (year BIGINT, bacteria VARCHAR, topic VARCHAR, viewby VARCHAR, viewby2 VARCHAR, "
        "units VARCHAR, value DOUBLE)"
    )
    for r in records:
        con.execute(
            "INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?)",
            [int(r["year"]), r["bacteria"], r["topic"], r["viewby"], r.get("viewby2"), r["units"],
             float(r["value"]) if r.get("value") else None],
        )  # fmt: skip
    return con


def _statuses(records: list[dict[str, Any]]) -> dict[str, str]:
    contract = load_registry(ROOT).get(DS).contract
    report = quality.run_rules(_typed(records), "t", quality.rules_for(contract))
    assert report.rule_errors == (), report.rule_errors
    return {r["test"]: r["status"] for r in report.results}


def test_every_contract_rule_passes_on_publisher_shaped_records() -> None:
    assert set(_statuses(RECORDS).values()) == {"pass"}


@pytest.mark.parametrize(
    ("bad", "rule"),
    [
        ({"value": "120", "units": PCT, "topic": "Syndromes", "viewby": "Bacteremia"}, "percents_between_0_and_100"),
        ({"value": "-1", "units": RATE, "topic": "Death Rates", "viewby": "Overall"}, "rates_and_counts_not_negative"),
        ({"value": "1.5", "units": COUNT, "topic": "Antibiotic resistance", "viewby": "Number of isolates"},
         "counts_are_whole_numbers"),
        ({"value": "7.0", "units": RATE, "topic": "Case Rates", "viewby": "Overall"}, "one_row_per_year_and_stratum"),
        ({"value": "7.0", "units": "Percentage", "topic": "Syndromes", "viewby": "Bacteremia"}, "units_is_a_known_unit"),
        ({"value": "7.0", "units": RATE, "topic": "Case rates", "viewby": "Overall"}, "topic_is_a_known_topic"),
    ],
)  # fmt: skip
def test_each_value_rule_fails_on_the_record_it_exists_to_catch(bad: dict[str, str], rule: str) -> None:
    """A rule is only reassurance if it has been seen to fail: each bad record trips its own rule, as a data
    failure, never as a rule that could not run."""
    record = {"year": "2022", "bacteria": BUG, **bad}
    assert _statuses([*RECORDS, record])[rule] == "fail"


def test_an_all_empty_unit_fails_its_fill_rate_rule() -> None:
    """A range check passes on a unit with no values, so each unit's fill-rate rule must fail instead."""
    blanked = [{k: v for k, v in r.items() if not (k == "value" and r["units"] == PCT)} for r in RECORDS]
    got = _statuses(blanked)
    assert got["percents_between_0_and_100"] == "pass"
    assert got["percent_values_present"] == "fail"
