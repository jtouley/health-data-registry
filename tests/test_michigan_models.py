"""The four Michigan dashboards' semantic models, compiled and run on hand-written rows (adding-dbt-unit-test).

These load the real registry (catalog/, contracts/, semantic/) and hold synthetic records in memory, the way a live
read does (the sources are never stored). The numbers are made up; only the layout follows each dashboard. They
pin hard rule 2: a suppressed day leaves its period with no total; Detroit is never added to Michigan, nor one
condition to another; rates and shares are never summed; outbreak measles cases are never added to all cases.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from health_data_registry.live import LiveTable
from health_data_registry.query import Engine, QueryRefused, compile_metric
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import Manifest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def reg() -> Registry:
    return load_registry(ROOT)


def engine(reg: Registry, tmp_path: Path, dataset: str, records: list[dict[str, Any]]) -> Engine:
    ds = reg.get(dataset)
    m = Manifest(
        dataset=dataset, snapshot_id="live-20261124T000000Z-000000000000", contract_version=ds.contract.version,
        semantic_version=ds.semantic.version, catalog_status=ds.entry.status, retrieved_at="2026-11-24T00:00:00+00:00",
        source_urls=["https://example.gov/view"], raw_sha256=["0" * 64], row_count=len(records), licence="x",
        redistribution=False, partial=False, origin="publisher", notes=["Data from the publisher (synthetic)."],
    )  # fmt: skip
    fields = sorted({c.source_field for c in ds.contract.columns})
    rows = [{f: (None if r.get(f) is None else str(r[f])) for f in fields} for r in records]
    eng = Engine(registry=reg, lake=tmp_path / "lake", hub=None, fetch_at_run_time=False)
    eng._live[dataset] = LiveTable(records=rows, manifest=m, fetched=time.monotonic())
    return eng


# --------------------------------------------------------------------------------------------- COVID-19 archive

CASES = "AGG(Confirmed & Probable Case Count)"
DAY = "Confirmed & Probably Disease Date"


def covid(
    day: str, cases: int | None, *, flag: str | None = None, layer: str | None = "Confirmed"
) -> dict[str, Any]:
    return {"view": "TimelineCasesDashboard", "worksheet": "Epi Curve Cases by Combined", DAY: day,
            "Case Status (alias)": layer, CASES: cases, f"{CASES} flag": flag}  # fmt: skip


COVID_ROWS = [
    *(covid(f"2021-01-0{d}", 20 + d) for d in range(3, 10)),  # a clean week: 23 ... 29
    covid("2021-01-10", 30),
    covid("2021-01-11", None, flag="<16"),  # a day the dashboard shows as "<16"
    covid("2021-01-12", 40),
    # the chart's 7-day average line: no case status, never added
    *(covid(f"2021-01-{d:02d}", 1000, layer=None) for d in range(3, 13)),
]


def test_covid_a_week_with_a_suppressed_day_has_no_total(reg: Registry, tmp_path: Path) -> None:
    eng = engine(reg, tmp_path, "detroit_dhd_covid_archive_daily", COVID_ROWS)
    out = eng.query_metric("detroit_dhd_covid_archive_daily", "daily_covid19_cases",
                           period={"start": "2021-01-10", "end": "2021-01-12"})  # fmt: skip
    (row,) = out["rows"]
    got = dict(zip(out["columns"], row, strict=True))
    assert got["daily_covid19_cases"] is None and got["suppressed_rows"] == 1
    assert got["flag"] == "<16"  # the published marker, never a number


def test_covid_a_clean_week_adds_only_the_daily_bars(reg: Registry, tmp_path: Path) -> None:
    eng = engine(reg, tmp_path, "detroit_dhd_covid_archive_daily", COVID_ROWS)
    out = eng.query_metric("detroit_dhd_covid_archive_daily", "daily_covid19_cases",
                           period={"start": "2021-01-03", "end": "2021-01-09"})  # fmt: skip
    got = dict(zip(out["columns"], out["rows"][0], strict=True))
    assert got["daily_covid19_cases"] == sum(range(23, 30)) and got["suppressed_rows"] == 0  # no 7-day rows


# --------------------------------------------------------------------------------------------- epidemiology


def epi(
    sheet: str, place: str, year: int, cases: int | None, rate: float, flag: str | None = None
) -> dict[str, Any]:
    return {"view": "TB", "worksheet": sheet, "Location (alias)": place, "YEAR(Year)": year,
            "SUM(Cases) (alias)": cases, "SUM(Rate)": rate, "SUM(Cases) flag": flag}  # fmt: skip


EPI_ROWS = [
    epi("TByear", "Detroit Case Rate", 2024, 44, 6.81),
    epi("TByear", "Michigan Case Rate", 2024, 150, 1.5),  # Michigan already includes Detroit's 44
    epi("ChlamydiaYear", "Detroit Case Rate", 2024, 9000, 1400.0),
    epi("TByear", "Detroit Case Rate", 2023, None, 0.5, flag="<6"),
]


@pytest.mark.parametrize(
    "kw",
    [
        {"filters": {"condition": "TByear", "year": 2024}},  # Detroit + Michigan: Detroit counted twice
        {"filters": {"condition": "TByear", "place": ["Detroit Case Rate", "Michigan Case Rate"]}},
        {
            "filters": {"place": "Detroit Case Rate", "year": 2024}
        },  # TB + chlamydia: one number for two diseases
        {"filters": {"place": "Detroit Case Rate"}, "group_by": ["year"]},
    ],
)
def test_epi_annual_cases_are_never_added_across_places_or_conditions(
    reg: Registry, kw: dict[str, Any]
) -> None:
    with pytest.raises(QueryRefused) as e:
        compile_metric(reg.get("detroit_dhd_epi_annual"), "annual_cases", **kw)
    assert e.value.code == "needs_single_value" and "never added up across" in str(e.value)


def test_epi_annual_cases_one_row_per_place_and_condition(reg: Registry, tmp_path: Path) -> None:
    eng = engine(reg, tmp_path, "detroit_dhd_epi_annual", EPI_ROWS)
    out = eng.query_metric("detroit_dhd_epi_annual", "annual_cases", filters={"condition": "TByear", "year": 2024},
                           group_by=["place"])  # fmt: skip
    assert [list(r[:2]) for r in out["rows"]] == [["Detroit Case Rate", 44], ["Michigan Case Rate", 150]]
    out = eng.query_metric("detroit_dhd_epi_annual", "annual_cases",
                           filters={"condition": "TByear", "place": "Detroit Case Rate"}, group_by=["year"])  # fmt: skip
    got = {r[0]: dict(zip(out["columns"], r, strict=True)) for r in out["rows"]}
    assert got[2024]["annual_cases"] == 44
    assert got[2023]["annual_cases"] is None and got[2023]["suppressed_rows"] == 1
    assert got[2023]["flag"] == "<6"


def test_epi_rates_are_never_summed(reg: Registry, tmp_path: Path) -> None:
    cm = compile_metric(reg.get("detroit_dhd_epi_annual"), "annual_case_rate", filters={"condition": "TByear"},
                        group_by=["place", "year"])  # fmt: skip
    assert "sum(" not in cm.sql.lower() and "not summed" in cm.method
    eng = engine(reg, tmp_path, "detroit_dhd_epi_annual", EPI_ROWS)
    with pytest.raises(QueryRefused) as e:
        eng.query_metric(
            "detroit_dhd_epi_annual", "annual_case_rate", filters={"condition": "TByear", "year": 2024}
        )
    assert e.value.code == "would_combine_rates"


# --------------------------------------------------------------------------------------------- respiratory ED


def ed(diagnosis: str, week: str, share: float) -> dict[str, Any]:
    return {"view": "Illness", "worksheet": "Diagnoses by Week", "Diagnosis1": diagnosis, "WeekEnding Date": week,
            "SUM(Percent)": share, "SUM(Percent) flag": None}  # fmt: skip


def test_ed_shares_are_never_summed(reg: Registry, tmp_path: Path) -> None:
    rows = [
        ed("COVID", "2026-11-14", 0.0042),
        ed("RSV", "2026-11-14", 0.003),
        ed("COVID", "2026-11-07", 0.004),
    ]
    cm = compile_metric(
        reg.get("detroit_dhd_resp_ed_weekly"), "weekly_share_of_ed_visits", group_by=["week_ending"]
    )
    assert "sum(" not in cm.sql.lower()
    eng = engine(reg, tmp_path, "detroit_dhd_resp_ed_weekly", rows)
    with pytest.raises(QueryRefused) as e:  # COVID + RSV in one week: two shares, never one added share
        eng.query_metric("detroit_dhd_resp_ed_weekly", "weekly_share_of_ed_visits", group_by=["week_ending"])
    assert e.value.code == "would_combine_rates"
    out = eng.query_metric("detroit_dhd_resp_ed_weekly", "weekly_share_of_ed_visits",
                           filters={"diagnosis": "COVID"}, group_by=["week_ending"])  # fmt: skip
    assert [r[1] for r in out["rows"]] == [0.004, 0.0042]


# --------------------------------------------------------------------------------------------- measles


def measles(county: str, total: int, outbreak: int) -> dict[str, Any]:
    return {"view": "CaseMap", "worksheet": "Case By Cty Map", "County (alias)": county,
            "c_Case Indicator (alias)": "Reported Case(s)", "SUM(c_Total Cases Reported) (alias)": total,
            "SUM(c_Outbreak Cases) (alias)": outbreak}  # fmt: skip


def test_outbreak_measles_cases_are_never_added_to_all_cases(reg: Registry, tmp_path: Path) -> None:
    ds = reg.get("mdhhs_measles_county")
    outbreak = compile_metric(ds, "outbreak_measles_cases_reported")
    total = compile_metric(ds, "measles_cases_reported")
    assert '"outbreak_cases"' in outbreak.sql and '"total_cases"' not in outbreak.sql
    assert '"total_cases"' in total.sql and '"outbreak_cases"' not in total.sql
    eng = engine(reg, tmp_path, "mdhhs_measles_county",
                 [measles("Washtenaw", 30, 24), measles("St. Joseph", 7, 0), measles("Alcona", 0, 0)])  # fmt: skip
    assert eng.query_metric("mdhhs_measles_county", "measles_cases_reported")["rows"][0][0] == 37
    assert eng.query_metric("mdhhs_measles_county", "outbreak_measles_cases_reported")["rows"][0][0] == 24
