"""NIS-Teen vaccination coverage (cdc_nis_teen_coverage): the real contract and semantic model on hand-written rows.

Synthetic rows only, laid out like data.cdc.gov ee48-w5t6. They pin hard rule 2 (coverage percentages are read as
published, never added up across places, groups or doses) and hard rule 15 (CDC's interval text, '60.3 to 68.3',
is kept as sent: splitting it waits for a staging extension).
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
DS = "cdc_nis_teen_coverage"
UTD = "Up-to-Date, Males and Females"


def row(
    place: str, year: str, estimate: str, ci: str | None, *, dose: str = UTD, group: str = "13-17 Years"
) -> dict[str, Any]:
    r = {"vaccine": "HPV", "dose": dose, "geography_type": "States/Local Areas", "geography": place,
         "year_season": year, "dimension_type": "Age", "dimension": group, "coverage_estimate": estimate,
         "population_sample_size": None if estimate == "NA" else "400"}  # fmt: skip
    if ci is not None:  # the API leaves the field out when CDC gives no interval
        r["_95_ci"] = ci
    return r


ROWS = [
    row("Michigan", "2023", "73.2", "68.1 to 77.7"),
    row("Michigan", "2024", "65.6", "60.3 to 70.6"),
    row("Ohio", "2024", "61.0", "55.9 to 65.9"),
    row("Michigan", "2024", "58.0", "52.0 to 63.8", group="13-15 Years"),  # overlaps 13-17 years
    row("Michigan", "2024", "80.8", "76.7 to 84.3", dose="≥1 Dose, Males and Females"),
    row("Michigan", "2022", "NA", None),  # CDC's 'not applicable'
]


@pytest.fixture
def built(tmp_path: Path) -> tuple[Registry, Engine]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "rows.ndjson"
    write_ndjson(f, ROWS)
    build_snapshot(lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/ee48-w5t6.json", ROWS)], [f])
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
    cm = compile_metric(ds, "vaccination_coverage", group_by=["place", "survey_year"])
    assert "sum(" not in cm.sql and "not summed" in cm.method


def test_get_series_reads_the_whole_age_range_and_each_dose_apart(built: tuple[Registry, Engine]) -> None:
    reg, eng = built
    out = tools.get_series(reg, eng, condition="HPV", locations=["Michigan"], fmt="json")
    got = {(r["condition"], str(r["year"])): r["value"] for r in _rows(out)}
    utd = "HPV vaccination up to date, teens 13-17 (both sexes)"
    one = "HPV vaccination, at least one dose, teens 13-17 (both sexes)"
    # 13-15 years (58.0) is never read for the 13-17 series, nor added to it; NA (2022) is no value, never zero.
    assert got == {(utd, "2022"): None, (utd, "2023"): 73.2, (utd, "2024"): 65.6, (one, "2024"): 80.8}


def test_na_is_no_value_and_the_interval_comes_back_as_sent(built: tuple[Registry, Engine]) -> None:
    _reg, eng = built
    out = tools.query_sql(
        eng,
        f"SELECT year_season, coverage_estimate, ci_95 FROM {DS} WHERE geography = 'Michigan' "
        "AND dimension = '13-17 Years' AND dose = 'Up-to-Date, Males and Females' ORDER BY year_season",
        fmt="json",
    )
    got = [(r["year_season"], r["coverage_estimate"], r["ci_95"]) for r in _rows(out)]
    assert got == [("2022", None, None), ("2023", 73.2, "68.1 to 77.7"), ("2024", 65.6, "60.3 to 70.6")]


def test_places_are_never_added(built: tuple[Registry, Engine]) -> None:
    _reg, eng = built
    with pytest.raises(QueryRefused):
        eng.query_metric(DS, "vaccination_coverage", filters={"survey_year": "2024", "dose": UTD})


def test_a_coverage_question_is_routed_here(built: tuple[Registry, Engine]) -> None:
    reg, eng = built
    out = tools.suggest_datasets(
        reg, "HPV vaccination coverage among teens in Michigan", engine=eng, fmt="json"
    )
    fits = {r["source_id"]: r["can_answer"] for r in _rows(out)}
    assert fits[DS] == "yes"
