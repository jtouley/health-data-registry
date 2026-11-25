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
from health_data_registry.quality import QualityBlocked
from health_data_registry.query import Engine, QueryRefused, compile_metric
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import Manifest, build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_nis_teen_coverage"
UTD = "Up-to-Date, Males and Females"


def row(
    place: str,
    year: str,
    estimate: str,
    ci: str | None,
    *,
    dose: str = UTD,
    group: str = "13-17 Years",
    vaccine: str = "HPV",
    group_type: str = "Age",
) -> dict[str, Any]:
    r = {"vaccine": vaccine, "dose": dose, "geography_type": "States/Local Areas", "geography": place,
         "year_season": year, "dimension_type": group_type, "dimension": group, "coverage_estimate": estimate}  # fmt: skip
    if estimate != "NA":  # the API leaves a field out when it has no value (read live 2026-11-25)
        r["population_sample_size"] = "400"
    if ci is not None:
        r["_95_ci"] = ci
    return r


ROWS = [
    row("Michigan", "2023", "73.2", "68.1 to 77.7"),
    row("Michigan", "2024", "65.6", "60.3 to 70.6"),
    row("Ohio", "2024", "61.0", "55.9 to 65.9"),
    row("Michigan", "2024", "58.0", "52.0 to 63.8", group="13-15 Years"),  # overlaps 13-17 years
    row("Michigan", "2024", "80.8", "76.7 to 84.3", dose="≥1 Dose, Males and Females"),
    row("Michigan", "2022", "NA", None),  # CDC's 'not applicable'
    row("Michigan", "2024", "90.1", "86.0 to 93.1", vaccine="Tetanus", dose="≥1 Dose Tdap"),
]


def _build(tmp_path: Path, rows: list[dict[str, Any]]) -> tuple[Registry, Engine, Manifest]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "rows.ndjson"
    write_ndjson(f, rows)
    m = build_snapshot(
        lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/ee48-w5t6.json", rows)], [f]
    )
    return reg, Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False), m


@pytest.fixture
def built(tmp_path: Path) -> tuple[Registry, Engine]:
    reg, eng, _m = _build(tmp_path, ROWS)
    return reg, eng


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
    out = tools.get_series(reg, eng, condition="HPV vaccination", locations=["Michigan"], fmt="json")
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


@pytest.mark.parametrize("disease", ["tetanus", "Tetanus", "varicella", "hepatitis b", "measles", "HPV"])
def test_a_question_naming_only_the_disease_never_gets_coverage(disease: str) -> None:
    """CDC's vaccine labels include disease names ('Tetanus', 'Varicella'): a disease question gets case counts,
    never a coverage percent read as a disease figure."""
    reg = load_registry(ROOT)
    assert DS not in {ds.id for ds, _s in tools.series_for(reg, disease)}


@pytest.mark.parametrize(
    ("asked", "label"),
    [
        ("Tdap", "Tdap vaccination, at least one dose, teens 13-17"),
        ("tetanus vaccination", None),  # no series is named for tetanus: the vaccine is Tdap
        ("varicella vaccination", "Varicella vaccination, two doses or history of disease, teens 13-17"),
        ("HepB", "HepB vaccination, at least three doses, teens 13-17"),
        ("MMR", "MMR vaccination, at least two doses, teens 13-17"),
    ],
)
def test_a_vaccination_question_reaches_its_series(asked: str, label: str | None) -> None:
    got = [s.label for ds, s in tools.series_for(load_registry(ROOT), asked) if ds.id == DS]
    assert got == ([label] if label else [])


def test_every_catalogued_vaccine_has_a_named_series() -> None:
    ds = load_registry(ROOT).get(DS)
    assert not ds.semantic.generic_series
    assert {s.filters["vaccine"] for s in ds.semantic.series} == set(ds.condition_labels)


def test_a_childhood_vaccination_question_gets_teen_rows_labelled_as_teens(
    built: tuple[Registry, Engine],
) -> None:
    """The full catalog (DEV trial-d75afb70, #175): 'vaccination' in Michigan, Detroit and Wayne County reads only
    this teen source, every row says teens 13-17, and the city and county are said to have no rows."""
    reg, eng = built
    out = tools.get_series(
        reg, eng, condition="vaccination", locations=["Michigan", "Detroit", "Wayne County"], fmt="json"
    )
    rows = _rows(out)
    assert rows and {r["source_id"] for r in rows} == {DS}
    assert all("teens 13-17" in r["condition"] and r["location"] == "Michigan" for r in rows)
    assert any(
        f"{DS} has no rows for: " in n and "Detroit" in n and "Wayne County" in n for n in out["notes"]
    )


def test_a_value_outside_0_to_100_blocks_the_copy(tmp_path: Path) -> None:
    with pytest.raises(QualityBlocked, match="coverage_is_a_percent"):
        _build(tmp_path, [*ROWS, row("Ohio", "2023", "101.0", "96.0 to 100.0")])


WARN_RULES = (
    "interval_text_form",
    "dimension_type_labels",
    "pooled_years_not_in_age_rows",
    "grain_unique",
    "interval_filled_where_estimate",
)


def test_each_hand_written_warning_fires_on_the_data_it_checks(tmp_path: Path) -> None:
    """Synthetic bad rows: each rule is read by its id, never by position (gx-configure-expectations)."""
    bad = [
        row("Ohio", "2024", "61.0", "60.3-68.3"),  # the interval in another form
        row(
            "Ohio", "2024", "61.0", "55.9 to 65.9", group="Hispanic", group_type="Ethnicity"
        ),  # a new group kind
        row("Ohio", "2018-2022", "60.0", "55.0 to 65.0"),  # a pooled range among the age rows
        row("Michigan", "2024", "73.2", "68.1 to 77.7"),
        row("Michigan", "2024", "73.9", "68.8 to 78.3"),  # the same group twice
        *(
            row(f"Place {i}", "2024", "50.0", None) for i in range(10)
        ),  # an estimate and no interval, ten times
    ]
    _reg, _eng, m = _build(tmp_path, bad)
    status = {r["test"]: r["status"] for r in m.quality["results"]}
    assert m.quality["rule_errors"] == []
    assert {k: status[k] for k in WARN_RULES} == dict.fromkeys(WARN_RULES, "warn")
    assert status["coverage_is_a_percent"] == "pass"


def test_every_hand_written_rule_passes_on_rows_as_cdc_publishes_them(tmp_path: Path) -> None:
    _reg, _eng, m = _build(tmp_path, ROWS)
    status = {r["test"]: r["status"] for r in m.quality["results"]}
    assert m.quality["rule_errors"] == []
    assert {k: v for k, v in status.items() if not k.startswith("fill_rate_")} == dict.fromkeys(
        [k for k in status if not k.startswith("fill_rate_")], "pass"
    )
