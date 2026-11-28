"""The three CDC Lyme disease county tables (data.cdc.gov x5j9-wybp, qtbi-xd4i, 84rx-ksgd) through the real catalog.

CDC splits Lyme disease into 1992-2007, 2008-2021 and 2022 onward because the case definitions changed, so each
table is its own source (cdc_lyme_county_1992_2007, cdc_lyme_county_2008_2021, cdc_lyme_county) and is never
joined to another into one series. Synthetic records only (hard rule 5): the counts are made up; the field names
and labels are the publisher's.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import analysis, quality, tools
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
NOW, MID, OLD = "cdc_lyme_county", "cdc_lyme_county_2008_2021", "cdc_lyme_county_1992_2007"
SOCRATA_ID = {NOW: "x5j9-wybp", MID: "qtbi-xd4i", OLD: "84rx-ksgd"}


def _rec(year: int, state: str, fips: str, status: str, sex: str, age: str, n: int | None) -> dict[str, str]:
    r = {
        "year": str(year),
        "state": state,
        "fips": fips,
        "case_status": status,
        "sex": sex,
        "age_cat_yrs": age,
    }
    if n is not None:  # Socrata leaves out a field it has no value for
        r["frequency"] = str(n)
    return r


RECORDS = {
    NOW: [
        _rec(2023, "MI", "26163", "Confirmed", "Female", "20+", 3),
        _rec(2023, "MI", "26163", "Probable", "Male", "0-19", 2),
        _rec(2023, "MI", "Suppressed", "Probable", "Suppressed", "Suppressed", 1),
        # An unknown state with a known county: counts in the nation only, never moved to Michigan by its code.
        _rec(2023, "Unknown", "26005", "Confirmed", "Unknown", "Unknown", 4),
        _rec(2023, "Suppressed", "Suppressed", "Probable", "Female", "20+", 5),
    ],
    MID: [
        _rec(2021, "OH", "39001", "Confirmed", "Female", "20+", None),
        _rec(2021, "MI", "26163", "Confirmed", "Male", "20+", 7),
        _rec(2021, "MI", "26163", "Probable", "Male", "20+", 6),
    ],
    OLD: [
        _rec(2007, "MI", "26163", "Confirmed", "Female", "20+", 8),
        _rec(1992, "PA", "42  .", "Confirmed", "Unknown", "Unknown", 9),
    ],
}


def _lake(tmp_path: Path, records: dict[str, list[dict[str, str]]]) -> tuple[Registry, Engine, Path]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    for ds, rows in records.items():
        f = tmp_path / f"{ds}.ndjson"
        write_ndjson(f, rows)
        url = f"https://data.cdc.gov/resource/{SOCRATA_ID[ds]}.json"
        build_snapshot(lake, reg.get(ds), [raw_fetch(url, rows)], [f])
    return reg, Engine(registry=reg, lake=lake), lake


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine, Path]:
    return _lake(tmp_path, RECORDS)


def _rows(out: Any) -> list[dict[str, str]]:
    text = out if isinstance(out, str) else out.render("csv")
    return list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#")))


def _table(out: Any) -> list[dict[str, Any]]:
    assert isinstance(out, dict), out
    return [dict(zip(out["columns"], r, strict=True)) for r in out["rows"]]


def test_a_state_question_about_lyme_disease_routes_to_the_three_tables() -> None:
    reg = load_registry(ROOT)
    rows = {
        r["source_id"]: r for r in _rows(tools.suggest_datasets(reg, "Lyme disease cases in Pennsylvania"))
    }
    for ds in (NOW, MID, OLD):
        assert rows[ds]["can_answer"] == "yes", rows[ds]


def test_a_question_about_other_countries_is_told_no() -> None:
    reg = load_registry(ROOT)
    rows = {
        r["source_id"]: r for r in _rows(tools.suggest_datasets(reg, "Lyme disease in countries worldwide"))
    }
    for ds in (NOW, MID, OLD):
        assert rows[ds]["can_answer"] == "no"
        assert "not country" in rows[ds]["why"]


def test_a_county_question_is_told_the_series_go_down_to_state_only() -> None:
    """No series reads by county yet (#262), so routing says no rather than a yes no tool can answer."""
    reg = load_registry(ROOT)
    rows = {
        r["source_id"]: r
        for r in _rows(tools.suggest_datasets(reg, "Lyme disease in Lehigh County, Pennsylvania"))
    }
    for ds in (NOW, MID, OLD):
        assert rows[ds]["can_answer"] == "no"
        assert "not county" in rows[ds]["why"]


def test_a_years_question_is_told_which_table_covers_it() -> None:
    reg = load_registry(ROOT)
    rows = {r["source_id"]: r for r in _rows(tools.suggest_datasets(reg, "Lyme disease in Michigan in 2015"))}
    assert rows[MID]["can_answer"] == "yes"
    assert rows[NOW]["can_answer"] == "no" and rows[OLD]["can_answer"] == "no"


def test_each_era_is_its_own_series_of_case_counts_never_joined(real: tuple[Registry, Engine, Path]) -> None:
    reg, _, _ = real
    found = tools.series_for(reg, "Lyme disease")
    by_source: dict[str, list[str]] = {}
    for ds, s in found:
        by_source.setdefault(ds.id, []).append(s.label)
        assert s.measure == "cases", s.label  # no rate, percent or estimate mixed into a disease question
    assert set(by_source) == {NOW, MID, OLD}
    assert all("2022 onward case definition" in x for x in by_source[NOW])
    assert all("2008-2021 case definition" in x for x in by_source[MID])
    # Before 2008 there was no probable case definition, so no confirmed-and-probable series.
    assert all("probable" not in x for x in by_source[OLD])


def test_a_state_adds_only_its_own_rows_and_the_nation_adds_every_row(
    real: tuple[Registry, Engine, Path],
) -> None:
    reg, eng, _ = real
    state = _table(
        tools.get_series(
            reg, eng, condition="Lyme disease", locations=["Michigan"], source_id=NOW, fmt="json"
        )
    )
    values = {r["condition"]: r["value"] for r in state}
    # Confirmed and probable, suppressed county included: 3 + 2 + 1. The unknown-state row (county 26005) stays out.
    assert values == {
        "Lyme disease cases, confirmed and probable (2022 onward case definition)": 6,
        "Lyme disease cases, confirmed only (2022 onward case definition)": 3,
    }
    nation = _table(tools.get_series(reg, eng, condition="Lyme disease", source_id=NOW, fmt="json"))
    national = [r for r in nation if str(r["location"]).startswith("United States: every reported case")]
    assert [(r["year"], r["value"]) for r in national] == [(2023, 15)]  # 3 + 2 + 1 + 4 + 5


def test_counts_are_summed_and_an_empty_count_is_never_a_zero(real: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = real
    out = _table(
        tools.get_series(reg, eng, condition="Lyme disease", locations=["Ohio"], source_id=MID, fmt="json")
    )
    assert out and all(r["value"] is None for r in out)
    assert all("no total" in r["period_status"] for r in out)
    summed = eng.query_metric(MID, "lyme_cases_all", filters={"state": ["MI"]}, group_by=["year"])
    assert "sum(" in summed["sql"]
    assert [r[:2] for r in summed["rows"]] == [[2021, 13]]


def test_the_raw_layer_keeps_every_value_as_sent(real: tuple[Registry, Engine, Path]) -> None:
    """Hard rule 15: the suppressed labels and the 1992 FIPS value outside the codebook are kept exactly as sent."""
    _, _, lake = real
    for ds, sent_rows in RECORDS.items():
        files = list((lake / "raw_records" / ds).glob("*.ndjson"))
        assert len(files) == 1
        kept = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
        for sent, row in zip(sent_rows, kept, strict=True):
            assert {k: row[k] for k in sent} == sent
    assert any(r["fips"] == "42  ." for r in RECORDS[OLD])


ERA = {NOW: (2022, None), MID: (2008, 2021), OLD: (1992, 2007)}
Row = tuple[int, str, str, str, str, str, int | None]


def _rules_on(ds: str, rows: list[Row]) -> quality.QualityReport:
    """The source contract's quality rules, run as the lake runs them, on a typed table of these rows."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (year BIGINT, state VARCHAR, fips VARCHAR, case_status VARCHAR, sex VARCHAR, "
        "age_cat_yrs VARCHAR, frequency BIGINT)"
    )
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    report = quality.run_rules(con, "t", quality.rules_for(load_registry(ROOT).get(ds).contract))
    assert not report.rule_errors, report.rule_errors
    return report


def _status(report: quality.QualityReport) -> dict[str, str]:
    return {str(r["test"]): str(r["status"]) for r in report.results}


@pytest.mark.parametrize("ds", [NOW, MID, OLD])
def test_each_table_passes_its_boundary_years_and_blocks_a_year_from_another_era(ds: str) -> None:
    first, last = ERA[ds]
    good: list[Row] = [(first, "MI", "26163", "Confirmed", "Female", "20+", 1)]
    if last is not None:
        good.append((last, "MI", "26163", "Confirmed", "Male", "0-19", 2))
    ok = _rules_on(ds, good)
    assert set(_status(ok).values()) == {"pass"}, ok.results
    assert not ok.blocking
    outside = [first - 1] + ([last + 1] if last is not None else [])
    for year in outside:
        bad = _rules_on(ds, [*good, (year, "MI", "26163", "Confirmed", "Female", "0-19", 1)])
        assert _status(bad)["year_in_this_tables_era"] == "fail", year
        assert bad.blocking, year


@pytest.mark.parametrize(
    ("ds", "row", "rule"),
    [
        (NOW, (2023, "MI", "26163", "Suspect", "Female", "20+", 1), "case_status_is_confirmed_or_probable"),
        (OLD, (2001, "MI", "26163", "Probable", "Female", "20+", 1), "case_status_is_confirmed"),
        (OLD, (1993, "PA", "42  .", "Confirmed", "Unknown", "Unknown", 1), "fips_is_five_digits_or_a_marker"),
        (
            MID,
            (2015, "suppressed", "26163", "Confirmed", "Female", "20+", 1),
            "state_is_a_postal_code_or_a_marker",
        ),
        (MID, (2015, "MI", "26163", "Confirmed", "F", "20+", 1), "sex_is_a_listed_value"),
        (NOW, (2023, "MI", "26163", "Confirmed", "Female", "65+", 1), "age_group_is_a_listed_value"),
    ],
)
def test_a_label_outside_the_codebook_warns_and_never_blocks(ds: str, row: Row, rule: str) -> None:
    first, _ = ERA[ds]
    report = _rules_on(ds, [(first, "MI", "26163", "Confirmed", "Female", "20+", 1), row])
    status = _status(report)
    assert status[rule] == "warn", report.results
    assert {k for k, v in status.items() if v != "pass"} == {rule}
    assert not report.blocking
    assert any("gave a warning" in c for c in report.caveats())


@pytest.mark.parametrize(("ds", "span"), [(NOW, "2022 onward"), (MID, "2008 to 2021"), (OLD, "1992 to 2007")])
def test_a_trend_says_it_may_cross_a_case_definition_change(ds: str, span: str) -> None:
    """No dated caseDefinitions yet: get_trend's note says the fit may mix definitions, never that one held."""
    reg = load_registry(ROOT)
    note = analysis._case_definition_note(reg.get(ds), [])
    assert note.startswith(f"{ds}: The CSTE Lyme disease case definition may have changed within {span}")
    assert "may mix definitions" in note and "assumes the definition stayed the same" not in note
    # A dated change still splits the fit, and a source with neither keeps the plain assumption.
    assert "split there" in analysis._case_definition_note(reg.get(ds), [(2011, "CSTE 2011")])
    assert "assumes the definition stayed the same" in analysis._case_definition_note(
        reg.get("cdc_haicviz_isa"), []
    )
