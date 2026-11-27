"""cdc_haicviz_isa (HAICViz iSA, data.cdc.gov ssz5-s49e) through the real catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names and labels are the publisher's.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import tools
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_haicviz_isa"
BUG = "Invasive Staphylococcus aureus"
PLACE = "the Emerging Infections Program sites combined, not by state"


def _row(year: str, topic: str, viewby: str, series: str, value: str | None) -> dict[str, str]:
    row = {"yearname": year, "topic": topic, "viewby": viewby, "series": series}
    return row if value is None else {**row, "value": value}


RECORDS = [
    _row("2020", "Case rates", "All cases", "All cases", "20.1"),
    _row("2021", "Case rates", "All cases", "All cases", "19.0"),
    _row("2021", "Death rates", "All cases", "All cases", "3.2"),
    _row("2021", "Case rates", "Age", "All cases", "19.0"),
    _row("2021", "Case rates", "Age", "> 65 years", "55.0"),
    _row("2021", "Case rates", "Epi class", "Hospital-onset", "2.5"),
    _row("2021", "Case rates", "Dialysis", "Dialysis", "1900.0"),
    _row("2021", "Syndromes", "Syndromes", "Non-bloodstream infection", "0.12"),
    _row("2021", "Case rates", "Age", "1 year", None),
]


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine, Path]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(
        lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/ssz5-s49e.json", RECORDS)], [f]
    )
    return reg, Engine(registry=reg, lake=lake), lake


def _rows(out: Any) -> list[dict[str, str]]:
    text = out if isinstance(out, str) else out.render("csv")
    return list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#")))


def test_a_plain_question_about_invasive_staph_aureus_routes_here() -> None:
    rows = _rows(tools.suggest_datasets(load_registry(ROOT), "invasive Staphylococcus aureus rates"))
    row = next(r for r in rows if r["source_id"] == DS)
    assert row["can_answer"] == "yes", row
    assert rows.index(row) < 3


def test_a_state_question_is_told_there_are_no_state_values() -> None:
    out = tools.suggest_datasets(load_registry(ROOT), "invasive Staphylococcus aureus in Michigan")
    row = next(r for r in _rows(out) if r["source_id"] == DS)
    assert row["can_answer"] == "no"
    assert "not state" in row["why"]


def test_each_rate_is_read_from_one_published_cell(real: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = real
    found = {s.metric for _, s in tools.series_for(reg, BUG, source_id=DS)}
    assert found == {"isa_case_rate", "isa_death_rate"}
    out = eng.query_metric(DS, "isa_case_rate", group_by=["year"])
    assert "sum(" not in out["sql"]
    at = {c: i for i, c in enumerate(out["columns"])}
    # The overall 'All cases' row only, never the Age view's 'All cases' row beside it.
    assert sorted((r[at["year"]], r[at["isa_case_rate"]]) for r in out["rows"]) == [
        (2020, 20.1),
        (2021, 19.0),
    ]


def test_by_stratum_leaves_out_totals_dialysis_and_shares(real: tuple[Registry, Engine, Path]) -> None:
    _, eng, _ = real
    out = eng.query_metric(
        DS, "isa_case_rate_by_stratum", filters={"year": 2021}, group_by=["view", "stratum"]
    )
    at = {c: i for i, c in enumerate(out["columns"])}
    got = sorted((r[at["view"]], r[at["stratum"]], r[at["isa_case_rate_by_stratum"]]) for r in out["rows"])
    assert got == [("Age", "1 year", None), ("Age", "> 65 years", 55.0), ("Epi class", "Hospital-onset", 2.5)]
    # The empty 1-year cell says so, never a blank read as a value.
    assert {r[at["stratum"]]: r[at["not_published"]] for r in out["rows"]}["1 year"] is True


def test_get_series_names_the_eip_sites_and_never_a_state(real: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = real
    out = tools.get_series(
        reg, eng, condition="Staphylococcus aureus", measure="rate", source_id=DS, fmt="json"
    )
    assert isinstance(out, dict), out
    at = {c: i for i, c in enumerate(out["columns"])}
    assert {r[at["location"]] for r in out["rows"]} == {PLACE}
    got = sorted((r[at["year"]], r[at["value"]]) for r in out["rows"])
    assert got == [(2020, 20.1), (2021, 3.2), (2021, 19.0)]
    declined = tools.get_series(reg, eng, condition=BUG, locations=["Ohio"], source_id=DS, fmt="json")
    assert isinstance(declined, dict)
    assert declined["rows"] == []


def test_raw_layer_keeps_the_publisher_values_as_sent(real: tuple[Registry, Engine, Path]) -> None:
    """Hard rule 15: the raw records dbt reads hold every value exactly as the publisher sent it, the absent value
    included (absent, never a zero)."""
    _, _, lake = real
    files = list((lake / "raw_records" / DS).glob("*.ndjson"))
    assert len(files) == 1
    kept = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    for sent, row in zip(RECORDS, kept, strict=True):
        assert {k: row[k] for k in sent} == sent
    assert kept[-1].get("value") in (None, "")
