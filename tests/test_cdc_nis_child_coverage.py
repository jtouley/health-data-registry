"""NIS-Child vaccination coverage (cdc_nis_child_coverage): the real contract and semantic model on hand-written rows.

Synthetic rows only, laid out like data.cdc.gov fhky-rtsk. They pin hard rule 2 (coverage percentages are read as
published, never added up across places, ages, doses or birth cohorts) and hard rule 15 (CDC's interval text,
'92.2 to 96.1', is kept as sent: splitting it waits for a staging extension, #172).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry import tools
from health_data_registry.query import Engine, QueryRefused, compile_metric
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_nis_child_coverage"
MMR = "≥1 Dose MMR"


def row(
    place: str,
    year: str,
    estimate: str,
    ci: str | None,
    *,
    vaccine: str = MMR,
    dose: str = "",
    age: str = "24 Months",
) -> dict[str, Any]:
    r = {"vaccine": vaccine, "dose": dose, "geography_type": "States/Local Areas", "geography": place,
         "year_season": year, "dimension_type": "Age", "dimension": age, "coverage_estimate": estimate,
         "population_sample_size": None if estimate == "NA" else "300"}  # fmt: skip
    if ci is not None:  # the API leaves the field out when CDC gives no interval
        r["_95_ci"] = ci
    return r


ROWS = [
    row("Michigan", "2021", "91.2", "85.6 to 95.2"),
    row("Michigan", "2022", "93.4", "87.9 to 97.0"),
    row("Michigan", "2021-2022", "92.4", "88.6 to 95.3"),  # a two-year cohort beside its single years
    row("Michigan", "2022", "95.0", "91.0 to 97.5", age="35 Months"),  # includes coverage by 24 months
    row("Ohio", "2022", "90.1", "86.0 to 93.4"),
    row("Michigan", "2022", "80.5", "74.9 to 85.2", vaccine="DTaP", dose="≥4 Doses"),
    row("Michigan", "2022", "93.9", "89.1 to 96.8", vaccine="DTaP", dose="≥3 Doses"),
    row("Michigan", "2020", "NA", None),
]


@pytest.fixture
def built(tmp_path: Path) -> tuple[Registry, Engine]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "rows.ndjson"
    write_ndjson(f, ROWS)
    build_snapshot(lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/fhky-rtsk.json", ROWS)], [f])
    return reg, Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)


def _rows(out: Any) -> list[dict[str, Any]]:
    return [dict(zip(out["columns"], r, strict=True)) for r in out["rows"]]


def test_interval_is_kept_as_published_text() -> None:
    col = load_registry(ROOT).get(DS).contract.column("ci_95")
    assert (col.physical_type, col.source_field, col.role) == ("VARCHAR", "_95_ci", "attribute")


def test_coverage_is_read_as_published_never_summed() -> None:
    ds = load_registry(ROOT).get(DS)
    metric = ds.semantic.metric("vaccination_coverage")
    assert metric.aggregation == "none"
    assert metric.uncertainty == "none" and "ci_95" in (metric.uncertainty_reason or "")
    assert ds.semantic.dimension("birth_year").non_additive  # cohorts include their single years
    cm = compile_metric(ds, "vaccination_coverage", group_by=["place", "birth_year"])
    assert "sum(" not in cm.sql and "not summed" in cm.method


def test_get_series_reads_one_dose_by_24_months(built: tuple[Registry, Engine]) -> None:
    reg, eng = built
    out = tools.get_series(reg, eng, condition=MMR, locations=["Michigan"], fmt="json")
    got = {str(r["year"]): r["value"] for r in _rows(out)}
    # By 35 months (95.0) is never read for the 24-month series; the cohort keeps its own label; NA is no value.
    assert got == {"2020": None, "2021": 91.2, "2021-2022": 92.4, "2022": 93.4}
    dtap = tools.get_series(reg, eng, condition="DTaP", locations=["Michigan"], fmt="json")
    assert [(r["year"], r["value"]) for r in _rows(dtap)] == [("2022", 80.5)]  # four doses, never three added


def test_the_interval_comes_back_as_sent(built: tuple[Registry, Engine]) -> None:
    _reg, eng = built
    out = tools.query_sql(
        eng,
        f"SELECT year_season, coverage_estimate, ci_95 FROM {DS} WHERE geography = 'Michigan' "
        "AND vaccine = '≥1 Dose MMR' AND dimension = '24 Months' ORDER BY year_season",
        fmt="json",
    )
    got = [(r["year_season"], r["coverage_estimate"], r["ci_95"]) for r in _rows(out)]
    assert got == [
        ("2020", None, None),
        ("2021", 91.2, "85.6 to 95.2"),
        ("2021-2022", 92.4, "88.6 to 95.3"),
        ("2022", 93.4, "87.9 to 97.0"),
    ]


def test_places_are_never_added(built: tuple[Registry, Engine]) -> None:
    _reg, eng = built
    with pytest.raises(QueryRefused):
        eng.query_metric(DS, "vaccination_coverage", filters={"birth_year": "2022", "vaccine": MMR})


def test_a_coverage_question_is_routed_here(built: tuple[Registry, Engine]) -> None:
    reg, eng = built
    question = "MMR vaccination coverage among young children in Michigan"
    out = tools.suggest_datasets(reg, question, engine=eng, fmt="json")
    assert {r["source_id"]: r["can_answer"] for r in _rows(out)}[DS] == "yes"
