"""The AHEAD EHE contract and semantic model, checked offline on a few made-up rows shaped like AHEAD's sheets.

No AHEAD data is in this file: every number is invented. Only the layout (sheet, state, county, indicator and
the column names) follows the published workbook, so the tests show what the contract promises: one published
cell per place, year and indicator, a state never mixed with its counties, intervals returned with their
estimates, empty cells never read as zero, and the contract's quality rules catching a broken table.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import crawl
from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "ahead_ehe_indicators"
URL = "https://ahead-files.hiv.gov/AHEAD_data_all_latest.xlsx"


def row(
    sheet: str, indicator: str, *, county: str | None = None, year: int = 2022, **v: Any
) -> dict[str, Any]:
    counted = {
        "Diagnoses": ("New diagnoses", "Total persons alive"),
        "Incidence": ("Estimated incidence", "Total persons alive"),
        "Knowledge of status": ("Living diagnosed", "Living diagnosed or undiagnosed"),
        "Linkage to care": ("At least one CD4 or VL test", "Total diagnoses"),
        "Viral suppression": ("VL below 200 copies per milliliter", "Living diagnosed"),
        "PrEP coverage": ("Number prescribed PrEP", "Number with PrEP indications"),
    }[indicator]
    out: dict[str, Any] = {
        "sheet": sheet, "state": "Michigan", "county": county, "year": str(year), "indicator": indicator,
        "metric": counted[0], "population_description": counted[1],
    }  # fmt: skip
    out.update({k: None if x is None else str(x) for k, x in v.items()})
    return out


S, J = "State totals", "Jurisdiction totals"
ROWS = [
    row(S, "Diagnoses", count=500, rate_per_100000=6.0, population=8_000_000),
    row(J, "Diagnoses", county="Wayne County", count=200, rate_per_100000=15.0, population=1_300_000),
    row(J, "Diagnoses", county="Wayne County", year=2023, count=None, population=1_300_000),  # empty: not zero
    row(S, "Incidence", count=480, count_lower_confidence_bound=300, count_upper_confidence_bound=660,
        rate_per_100000=6.0, percentage_lower_confidence_bound=3.8, percentage_upper_confidence_bound=8.3,
        relative_standard_error=19.0, population=8_000_000),
    row(J, "Incidence", county="Wayne County", count=190, count_lower_confidence_bound=40,
        count_upper_confidence_bound=340, rate_per_100000=14.6, percentage_lower_confidence_bound=3.1,
        percentage_upper_confidence_bound=26.2, relative_standard_error=40.0, population=1_300_000),
    row(J, "Knowledge of status", county="Wayne County", count=7000, percentage=87.5,
        percentage_lower_confidence_bound=80.0, percentage_upper_confidence_bound=101.2,
        relative_standard_error=4.0, population=8000),
    row(J, "Linkage to care", county="Wayne County", count=160, percentage=80.0, population=200),
    row(J, "Viral suppression", county="Wayne County", count=4500, percentage=69.2, population=6500),
    row(J, "PrEP coverage", county="Wayne County", count=1800, percentage=20.0, population=9000),
]  # fmt: skip


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "ahead.ndjson"
    write_ndjson(f, ROWS)
    build_snapshot(lake, reg.get(DS), [raw_fetch(URL, ROWS)], [f])
    return reg, Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)


def cells(eng: Engine, metric: str, **filters: Any) -> list[list[Any]]:
    out = eng.query_metric(DS, metric, filters=filters, group_by=["year"])
    return [list(r) for r in out["rows"]]


def test_one_published_cell_per_place_year_and_indicator(real: tuple[Registry, Engine]) -> None:
    _, eng = real
    wayne = {"sheet": J, "county": "Wayne County"}
    assert cells(eng, "hiv_diagnoses", **wayne) == [[2022, 200], [2023, None]]  # 2023 empty, never 0
    assert cells(eng, "hiv_diagnoses", sheet=S, state="Michigan") == [[2022, 500]]  # the state's own row
    assert cells(eng, "hiv_diagnosis_rate", **wayne)[0] == [2022, 15.0]
    assert cells(eng, "linkage_to_care", **wayne) == [[2022, 80.0]]
    assert cells(eng, "viral_suppression", **wayne) == [[2022, 69.2]]
    assert cells(eng, "indicator_denominator", indicator="Linkage to care", **wayne) == [[2022, 200]]


def test_intervals_come_back_with_their_estimates(real: tuple[Registry, Engine]) -> None:
    _, eng = real
    out = eng.query_metric(
        DS, "hiv_incidence", filters={"sheet": J, "county": "Wayne County"}, group_by=["year"]
    )
    assert out["columns"][:4] == ["year", "hiv_incidence", "lower", "upper"]
    assert [list(r[:4]) for r in out["rows"]] == [[2022, 190, 40, 340]]
    rate = cells(eng, "hiv_incidence_rate", sheet=J, county="Wayne County")
    assert [r[:4] for r in rate] == [[2022, 14.6, 3.1, 26.2]]
    kos = cells(eng, "knowledge_of_status", sheet=J, county="Wayne County")
    assert [r[:4] for r in kos] == [[2022, 87.5, 80.0, 101.2]]  # a bound above 100 is kept as published


def test_a_state_is_never_mixed_with_its_counties(real: tuple[Registry, Engine]) -> None:
    _, eng = real
    # Michigan names both the state row and Wayne County's row; without the sheet, two cells match.
    with pytest.raises(QueryRefused) as e:
        eng.query_metric(DS, "hiv_diagnoses", filters={"state": "Michigan", "year": 2022}, group_by=["state"])
    assert e.value.code == "would_combine_rates"


def test_series_pin_the_sheet_and_read_one_cell() -> None:
    reg = load_registry(ROOT)
    sem = reg.get(DS).semantic
    assert sem.series and all(s.filters.get("sheet") in (S, J) for s in sem.series)
    assert {s.location_dimension for s in sem.series if s.filters["sheet"] == S} == {"state"}
    assert {s.location_dimension for s in sem.series if s.filters["sheet"] == J} == {"county"}
    assert all(m.aggregation == "none" for m in sem.metrics)  # nothing in AHEAD is ever added up
    entry = reg.get(DS).entry
    assert entry.hosted and entry.licence.id == "public-domain-us-federal"
    assert entry.source.workbook is not None and entry.source.workbook.sheets == [S, J]
    # PrEP is withheld until the owner confirms the IQVIA terms: no rows read, no metric, no series.
    assert entry.source.workbook.exclude_rows == {"indicator": ["PrEP coverage"]}
    assert not any("prep" in m.name or "PrEP" in m.description for m in sem.metrics)
    assert not any("PrEP" in s.label for s in sem.series)


def quality(con: duckdb.DuckDBPyConnection, table: str) -> dict[str, int]:
    doc = load_registry(ROOT).get(DS).contract.raw
    out = {}
    for p in doc["schema"][0]["properties"]:
        for q in p.get("quality") or []:
            out[q["id"]] = con.execute(q["query"].replace("{object}", table)).fetchone()[0]
    return out


def test_quality_rules_pass_on_a_sound_table_and_catch_each_break(real: tuple[Registry, Engine]) -> None:
    _, eng = real
    con = eng.connect()
    assert quality(con, DS) == {
        "incidence_rounded_to_ten": 0, "diagnoses_count_filled": 1,  # the made-up empty 2023 row is caught
        "prep_percent_matches_counts": 0, "percentage_only_for_share_indicators": 0, "nothing_shown_above_rse_50": 0,
    }  # fmt: skip
    mem = con.cursor()
    mem.execute(f"CREATE TEMP TABLE t AS SELECT * FROM {DS} WHERE count IS NOT NULL")  # noqa: S608 - test table
    assert set(quality(mem, "t").values()) == {0}
    mem.execute("UPDATE t SET count = 185 WHERE indicator = 'Incidence' AND county = 'Wayne County'")
    mem.execute("UPDATE t SET percentage = 25.0 WHERE indicator = 'PrEP coverage'")
    mem.execute("UPDATE t SET percentage = 1.0 WHERE indicator = 'Diagnoses' AND county IS NULL")
    mem.execute("UPDATE t SET relative_standard_error = 55 WHERE indicator = 'Knowledge of status'")
    assert quality(mem, "t") == {
        "incidence_rounded_to_ten": 1, "diagnoses_count_filled": 0, "prep_percent_matches_counts": 1,
        "percentage_only_for_share_indicators": 1, "nothing_shown_above_rse_50": 1,
    }  # fmt: skip


def test_the_workbook_download_gets_its_reviewed_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry import fetch as fetch_mod

    reg = load_registry(ROOT)
    assert reg.byte_caps == {URL: 120_000_000}  # only sources that declare one
    seen: dict[str, dict[str, Any]] = {}

    def refuse(url: str, **kw: Any) -> Any:
        seen[url] = kw
        raise fetch_mod.FetchRefused("not_allowed", "test")

    monkeypatch.setattr(fetch_mod, "fetch", refuse)
    get = crawl.fetcher(reg, None, sleep=lambda s: None, robots=False)
    for url in (URL, "https://data.cdc.gov/resource/x.json"):
        with pytest.raises(fetch_mod.FetchRefused):
            get(url)
    assert seen[URL]["max_bytes"] == 120_000_000
    assert "max_bytes" not in seen["https://data.cdc.gov/resource/x.json"]  # the fetcher's default stands
