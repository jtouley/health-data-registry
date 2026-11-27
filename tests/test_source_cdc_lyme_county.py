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


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine, Path]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    for ds, rows in RECORDS.items():
        f = tmp_path / f"{ds}.ndjson"
        write_ndjson(f, rows)
        url = f"https://data.cdc.gov/resource/{SOCRATA_ID[ds]}.json"
        build_snapshot(lake, reg.get(ds), [raw_fetch(url, rows)], [f])
    return reg, Engine(registry=reg, lake=lake), lake


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


def test_a_county_question_is_told_no_until_a_tool_answers_by_county() -> None:
    """The tables carry the county of residence, but every series is by state, so routing must not say yes."""
    reg = load_registry(ROOT)
    rows = {
        r["source_id"]: r
        for r in _rows(tools.suggest_datasets(reg, "Lyme disease in Lehigh County, Pennsylvania"))
    }
    for ds in (NOW, MID, OLD):
        assert rows[ds]["can_answer"] == "no", rows[ds]
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


def _rules_on(ds: str, rows: list[tuple[int, str, str, str, str, str, int | None]]) -> dict[str, Any]:
    """Run a source's real contract rules (quality.rules_for: the read path's own SQL) on a tiny typed table."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (year BIGINT, state VARCHAR, fips VARCHAR, case_status VARCHAR, sex VARCHAR, "
        "age_cat_yrs VARCHAR, frequency BIGINT)"
    )
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    report = quality.run_rules(con, "t", quality.rules_for(load_registry(ROOT).get(ds).contract))
    assert report.rule_errors == ()
    return {"blocking": report.blocking, **{r["test"]: r for r in report.results}}


ERA = {NOW: (2022, None), MID: (2008, 2021), OLD: (1992, 2007)}


@pytest.mark.parametrize("ds", [NOW, MID, OLD])
def test_each_table_rejects_a_year_from_another_era(ds: str) -> None:
    """The era rule run on data: its boundary years pass, the year either side fails and blocks the load."""
    first, last = ERA[ds]
    status = "Confirmed"
    inside = [first] + ([last] if last else [first + 5])
    ok = _rules_on(ds, [(y, "MI", "26163", status, "Female", "20+", 1) for y in inside])
    assert ok["year_in_this_tables_era"]["status"] == "pass" and not ok["blocking"]
    for out in [first - 1] + ([last + 1] if last else []):
        got = _rules_on(ds, [(y, "MI", "26163", status, "Female", "20+", 1) for y in (*inside, out)])
        assert got["year_in_this_tables_era"]["status"] == "fail", out
        assert got["year_in_this_tables_era"]["observed"] == 1  # exactly the out-of-era row
        assert got["blocking"]


@pytest.mark.parametrize("ds", [NOW, MID, OLD])
def test_a_new_label_is_a_warning_that_never_blocks(ds: str) -> None:
    year = ERA[ds][0]
    got = _rules_on(
        ds,
        [
            (year, "MI", "26163", "Confirmed", "Female", "20+", 1),
            (year, "suppressed", "42  .", "Suspect", "F", "65+", 2),
        ],
    )
    for rule in (
        "state_is_a_postal_code_or_a_marker",
        "fips_is_five_digits_or_a_marker",
        "sex_is_a_listed_value",
        "age_group_is_a_listed_value",
    ):
        assert got[rule]["status"] == "warn" and got[rule]["observed"] == 1, rule
    status_rule = next(k for k in got if k.startswith("case_status_is_"))
    assert got[status_rule]["status"] == "warn"
    assert not got["blocking"]


@pytest.fixture
def trend_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Registry, Engine]:
    """Michigan in three years of 2022 onward and three of 2008-2021; nothing in 1992-2007.

    The trends here are fitted on counts, so no Census population is loaded (an empty one stands in)."""
    monkeypatch.setattr(analysis, "populations", lambda *_a, **_k: ({}, set()))
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    rows = {
        NOW: [
            _rec(y, "MI", "26163", "Confirmed", "Female", "20+", n)
            for y, n in ((2022, 10), (2023, 12), (2024, 15))
        ],
        MID: [
            _rec(y, "MI", "26163", "Confirmed", "Female", "20+", n)
            for y, n in ((2019, 8), (2020, 9), (2021, 11))
        ],
        OLD: [_rec(2007, "OH", "39001", "Confirmed", "Female", "20+", 1)],
    }
    for ds, recs in rows.items():
        f = tmp_path / f"{ds}.ndjson"
        write_ndjson(f, recs)
        build_snapshot(
            lake, reg.get(ds), [raw_fetch(f"https://data.cdc.gov/resource/{SOCRATA_ID[ds]}.json", recs)], [f]
        )
    return reg, Engine(registry=reg, lake=lake)


def test_no_trend_is_fitted_across_the_case_definitions_inside_an_era(
    trend_lake: tuple[Registry, Engine],
) -> None:
    """CDC lists 2008, 2011 and 2017 definitions inside 2008-2021: no one percent change is fitted across them."""
    reg, eng = trend_lake
    out = analysis.get_trend(
        reg, eng, condition="Lyme disease", location="Michigan", fit_on="counts", fmt="json"
    )
    assert "rows" in out, out
    assert {r[out["columns"].index("source_id")] for r in out["rows"]} == {NOW}
    assert any(
        n.startswith("Not fitted (the case definition changed") and MID in n and "2011" in n
        for n in out["notes"]
    )
    only_mid = analysis.get_trend(
        reg, eng, condition="Lyme disease", location="Michigan", fit_on="counts", year_end=2021, fmt="json"
    )
    assert only_mid["refused"] == "case_definition_undated" and "2017" in only_mid["message"]
