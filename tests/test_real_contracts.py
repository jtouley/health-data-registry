"""The real contracts' structural facts, checked offline on tiny synthetic tables shaped like the publisher's.

dbt review 4: the NNDSS totalRows predicate decides which rows every sum drops. These unit tests load the real
registry (catalog/, contracts/, semantic/) and assert, on hand-written rows, the structure the contract states:
states, DC and New York City add up to US RESIDENTS; adding territories and non-US residents gives TOTAL; census
divisions are summary rows. The numbers are made up; only the layout follows the publisher's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry.query import Engine, QueryRefused, compile_metric
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]


def nndss(area: str, m1: Any, *, summary: bool = False, flag: str | None = None, week: int = 52,
          m3: Any = None, year: int = 2024, label: str = "Legionellosis") -> dict[str, Any]:  # fmt: skip
    return {
        "states": area, "year": str(year), "week": str(week), "label": label, "m1": m1, "m1_flag": flag,
        "m2": None, "m2_flag": None, "m3": m3, "m3_flag": flag if m3 is None else None, "m4": None,
        "m4_flag": None, "location1": None if summary else area, "location2": area if summary else None,
        "sort_order": None,
    }  # fmt: skip


ROWS = [
    nndss("MICHIGAN", "3", m3="30"),
    nndss("NEW YORK", "2", m3="20"),
    nndss("NEW YORK CITY", "4", m3="40"),
    nndss("DISTRICT OF COLUMBIA", None, flag="-"),  # no reported cases: a zero, by the codebook
    nndss("PUERTO RICO", "1", m3="10"),
    nndss("Non-U.S. Residents", "1", summary=True, m3="10"),  # its own bucket, title case as from 2025
    nndss("EAST NORTH CENTRAL", "3", summary=True, m3="30"),  # a census division: a summary row
    nndss("US RESIDENTS", "9", summary=True, m3="90"),
    nndss("US TERRITORIES", "1", summary=True, m3="10"),
    nndss("TOTAL", "11", summary=True, m3="110"),
]


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    f = tmp_path / "nndss.ndjson"
    write_ndjson(f, ROWS)
    lake.mkdir()
    build_snapshot(
        lake,
        reg.get("cdc_nndss_weekly"),
        [raw_fetch("https://data.cdc.gov/resource/x9gk-5huc.json", ROWS)],
        [f],
    )
    return reg, Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)


def one(eng: Engine, metric: str, **kw: Any) -> Any:
    out = eng.query_metric("cdc_nndss_weekly", metric, **kw)
    assert len(out["rows"]) == 1, out["rows"]
    return out["rows"][0][0]


WEEK = {"condition": "Legionellosis", "mmwr_year": 2024, "mmwr_week": 52}


def test_nndss_sum_of_non_total_rows_equals_the_total_row(real: tuple[Registry, Engine]) -> None:
    _, eng = real
    assert one(eng, "weekly_cases", filters=WEEK) == 11  # = TOTAL: states, DC, NYC, territories, non-US
    assert one(eng, "us_resident_weekly_cases", filters=WEEK) == 9  # the published US RESIDENTS row
    assert one(eng, "year_to_date_cases", filters=WEEK) == 110


def test_nndss_year_to_date_is_read_at_one_week(real: tuple[Registry, Engine]) -> None:
    _, eng = real
    with pytest.raises(QueryRefused) as e:
        eng.query_metric("cdc_nndss_weekly", "year_to_date_cases", filters={"condition": "Legionellosis"})
    assert e.value.code == "needs_single_value"


def test_nndss_no_reported_cases_reads_as_zero_with_its_flag(real: tuple[Registry, Engine]) -> None:
    _, eng = real
    out = eng.query_metric(
        "cdc_nndss_weekly", "weekly_cases", filters={**WEEK, "reporting_area": "DISTRICT OF COLUMBIA"}
    )
    assert out["rows"] == [[0, 0, None]]  # 0 cases, no empty values: '-' is not a gap
    assert "'-' as no reported cases" in out["method"]


CAURIS = ["Candida auris, clinical", "Candida auris, screening"]


@pytest.mark.parametrize(
    "metric",
    [
        "weekly_cases",
        "year_to_date_cases",
        "prior_year_to_date_cases",
        "annual_cases",
        "us_resident_weekly_cases",
        "us_resident_year_to_date_cases",
        "us_resident_annual_cases",
    ],
)
@pytest.mark.parametrize(
    ("filters", "group_by"),
    [
        pytest.param({"condition": CAURIS, "mmwr_week": 52}, [], id="clinical-plus-screening"),
        pytest.param({"mmwr_week": 52}, [], id="every-condition"),
        pytest.param({"condition": "Candida auris, clinical", "mmwr_week": 52}, ["condition"], id="grouped"),
    ],
)
def test_nndss_cauris_clinicalAndScreening_neverAddedTogether(
    metric: str, filters: dict[str, Any], group_by: list[str]
) -> None:
    ds = load_registry(ROOT).get("cdc_nndss_weekly")
    if ds.semantic.metric(metric).aggregation == "cumulative_latest":
        group_by = [
            *group_by,
            "mmwr_year",
            "reporting_area",
        ]  # read once per year and area; condition is under test
    if "condition" in group_by:
        # Grouped by condition: one row per condition, never a sum across them.
        assert "GROUP BY" in compile_metric(ds, metric, filters=filters, group_by=group_by).sql
        return
    with pytest.raises(QueryRefused) as e:
        compile_metric(ds, metric, filters=filters, group_by=group_by)
    # condition is non_additive (#49): its labels overlap or count different things, so they are never added.
    assert e.value.code in ("non_additive", "needs_single_value", "needs_grouping")
    assert "condition" in str(e.value)


def test_nndss_cauris_series_oneLabelEach_caseStatusCited() -> None:
    ds = load_registry(ROOT).get("cdc_nndss_weekly")
    series = [s for s in ds.semantic.series if s.condition.startswith("Candida auris")]
    assert {s.filters["condition"] for s in series} == set(CAURIS)
    assert all(s.filters["condition"] == s.condition for s in series)
    assert all("22-ID-05" in (s.case_status or "") and s.case_definition for s in series)
    assert (ds.contract.geography_basis or "").startswith("reporting jurisdiction by usual residence")
    assert ds.entry.licence.id == "public-domain-us-federal"


def test_needsSingleValue_message_runningTotalOnlyForItsOrderDimension() -> None:
    ds = load_registry(ROOT).get("cdc_nndss_weekly")
    with pytest.raises(QueryRefused) as e:
        compile_metric(ds, "weekly_cases", filters={"condition": CAURIS})
    assert e.value.code == "non_additive"  # a plain count: never added across conditions
    with pytest.raises(QueryRefused) as e:
        compile_metric(ds, "year_to_date_cases", filters={"condition": "Legionellosis"})
    assert str(e.value).startswith("year_to_date_cases is a running total, so it is read at one mmwr_week")
