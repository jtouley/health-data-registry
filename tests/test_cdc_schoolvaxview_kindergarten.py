"""SchoolVaxView kindergarten coverage and exemptions (cdc_schoolvaxview_kindergarten): the real contract and
semantic model on hand-written rows.

Synthetic rows only, laid out like data.cdc.gov ijqb-a7ye. They pin hard rule 2 (percentages read as published,
never added up across vaccines, exemption types, states or years; the U.S. median never treated as a total) and
that a published code (NA, NReq, Nreq, NR) is no value, never zero, with the code kept beside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import tools
from health_data_registry.query import Engine, QueryRefused, compile_metric
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_schoolvaxview_kindergarten"


def row(
    place: str, year: str, vaccine: str, estimate: str, *, dose: str = "", **extra: str
) -> dict[str, Any]:
    kind = "National" if place in ("United States", "U.S. Median") else "States"
    return {"vaccine": vaccine, "dose": dose, "geography_type": kind, "geography": place, "year_season": year,
            "coverage_estimate": estimate, "survey_type": "Census", **extra}  # fmt: skip


ROWS = [
    row("Michigan", "2023-24", "MMR", "92.1", population_sample_size="110000", percent_surveyed="100"),
    row("Michigan", "2024-25", "MMR", "91.6"),
    row("Ohio", "2024-25", "MMR", "89.0"),
    row("U.S. Median", "2024-25", "MMR", "92.5"),
    row("Michigan", "2024-25", "Hepatitis B", "NReq", foot_notes="†"),
    row("Michigan", "2022-23", "MMR", "NR"),
    row("Ohio", "2023-24", "Polio", "NA"),
    row("Ohio", "2023-24", "Hepatitis B", "Nreq"),
    row("Ohio", "2022-23", "Hepatitis B", " NReq "),
    row("Michigan", "2024-25", "Exemption", "6.8", dose="Any Exemption", number_of_exemptions="7500"),
    row("Michigan", "2024-25", "Exemption", "6.5", dose="Non-Medical Exemption", number_of_exemptions="7200"),
    row("Michigan", "2024-25", "Exemption", "0.1", dose="Medical Exemption", foot_notes="<"),
    row("Michigan", "2024-25", "Varicella", "90.2", dose="2 Doses (or disease history)"),
    row("Michigan", "2024-25", "Varicella", "93.0", dose="1 Dose (or disease history)"),
]


@pytest.fixture
def built(tmp_path: Path) -> tuple[Registry, Engine]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "rows.ndjson"
    write_ndjson(f, ROWS)
    build_snapshot(lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/ijqb-a7ye.json", ROWS)], [f])
    return reg, Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)


def _rows(out: Any) -> list[dict[str, Any]]:
    return [dict(zip(out["columns"], r, strict=True)) for r in out["rows"]]


def test_no_metric_sums_a_percent() -> None:
    ds = load_registry(ROOT).get(DS)
    for name in ("kindergarten_coverage", "exemption_percent", "exemptions_reported"):
        assert ds.semantic.metric(name).aggregation == "none"
        cm = compile_metric(ds, name, group_by=["place", "school_year"])
        assert "sum(" not in cm.sql.lower() and "not summed" in cm.method


def test_codes_are_no_value_and_kept_as_published(built: tuple[Registry, Engine]) -> None:
    _reg, eng = built
    out = tools.query_sql(
        eng,
        f"SELECT year_season, vaccine, coverage_estimate, estimate_as_published FROM {DS} "
        "WHERE coverage_estimate IS NULL ORDER BY geography, year_season, vaccine",
        fmt="json",
    )
    got = [
        (r["year_season"], r["vaccine"], r["coverage_estimate"], r["estimate_as_published"])
        for r in _rows(out)
    ]
    # Every declared code, the one that differs only in case (Nreq) and a padded one: no value, code kept as sent.
    assert got == [
        ("2022-23", "MMR", None, "NR"),
        ("2024-25", "Hepatitis B", None, "NReq"),
        ("2022-23", "Hepatitis B", None, " NReq "),
        ("2023-24", "Hepatitis B", None, "Nreq"),
        ("2023-24", "Polio", None, "NA"),
    ]


@pytest.mark.parametrize("code", ["N/A", "nr", "95.0*"])
def test_an_undeclared_code_refuses_the_load(tmp_path: Path, code: str) -> None:
    """A code the contract doesn't declare is never guessed at: the whole load is refused and nothing is stored."""
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    rows = [*ROWS, row("Ohio", "2024-25", "Polio", code)]
    f = tmp_path / "rows.ndjson"
    write_ndjson(f, rows)
    with pytest.raises(duckdb.ConversionException, match="DOUBLE"):
        build_snapshot(
            lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/ijqb-a7ye.json", rows)], [f]
        )
    assert not (lake / "datasets" / DS / "LATEST").exists()


def test_get_series_reads_coverage_and_exemptions_apart(built: tuple[Registry, Engine]) -> None:
    reg, eng = built
    mmr = _rows(tools.get_series(reg, eng, condition="MMR", locations=["Michigan"], fmt="json"))
    assert {(r["year"], r["value"]) for r in mmr} == {("2022-23", None), ("2023-24", 92.1), ("2024-25", 91.6)}
    ex = _rows(tools.get_series(reg, eng, condition="Exemption", locations=["Michigan"], fmt="json"))
    # Any and non-medical exemptions come back as their own rows; the medical row is not in a named series.
    assert {(r["condition"], r["value"]) for r in ex} == {
        ("Kindergartners with any exemption", 6.8),
        ("Kindergartners with a non-medical exemption", 6.5),
    }
    var = _rows(tools.get_series(reg, eng, condition="Varicella", locations=["Michigan"], fmt="json"))
    assert [r["value"] for r in var] == [90.2]  # two doses only, never added to one dose


def test_states_and_the_median_are_never_added(built: tuple[Registry, Engine]) -> None:
    _reg, eng = built
    with pytest.raises(QueryRefused):
        eng.query_metric(DS, "kindergarten_coverage", filters={"school_year": "2024-25", "vaccine": "MMR"})


def test_a_coverage_question_is_routed_here(built: tuple[Registry, Engine]) -> None:
    reg, eng = built
    for question in (
        "kindergarten MMR vaccination coverage in Michigan",
        "vaccine exemptions among kindergartners",
    ):
        out = tools.suggest_datasets(reg, question, engine=eng, fmt="json")
        assert {r["source_id"]: r["can_answer"] for r in _rows(out)}[DS] == "yes", question
