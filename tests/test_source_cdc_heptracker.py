"""cdc_heptracker (HepTracker, data.cdc.gov jkcx-ndu8) through the real catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names, labels and codes are the
publisher's. Every row is shown with CDC's code where no number was published, and a code is never a zero.
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
DS = "cdc_heptracker"


def _row(
    event: str, year: str, ch: str, value: str, typ: str, count: str, rate: str | None
) -> dict[str, str]:
    row = {"event": event, "year": year, "character": ch, "value": value, "type": typ, "count": count}
    return row if rate is None else {**row, "rate": rate}


RECORDS = [
    _row("Acute Hepatitis C", "2021", "Area", "Total", "Case", "4,798", "1.5"),
    _row("Acute Hepatitis C", "2022", "Area", "Total", "Case", "4,966", "1.5"),
    _row("Acute Hepatitis C", "2022", "Area", "Michigan", "Case", "210", "2.1"),
    _row("Acute Hepatitis C", "2022", "Area", "Alaska", "Case", "—", "—"),
    _row("Acute Hepatitis C", "2022", "Injection Drug Use", "Yes", "Case", "1,500", None),
    _row("Hepatitis C", "2022", "Area", "Total", "Death", "11,000", "3.3"),
    _row("Hepatitis C", "2022", "Area", "Wyoming", "Death", "S", "UR"),
]


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine, Path]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(
        lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/jkcx-ndu8.json", RECORDS)], [f]
    )
    return reg, Engine(registry=reg, lake=lake), lake


def _rows(out: Any) -> list[dict[str, str]]:
    text = out if isinstance(out, str) else out.render("csv")
    return list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#")))


def _json(out: Any) -> tuple[dict[str, int], list[list[Any]]]:
    assert isinstance(out, dict), out
    return {c: i for i, c in enumerate(out["columns"])}, out["rows"]


def test_a_plain_hepatitis_c_question_by_state_routes_here() -> None:
    rows = _rows(tools.suggest_datasets(load_registry(ROOT), "acute hepatitis C cases by state"))
    row = next(r for r in rows if r["source_id"] == DS)
    assert row["can_answer"] == "yes", row
    assert rows.index(row) < 3


def test_a_county_question_is_told_there_are_no_county_values() -> None:
    out = tools.suggest_datasets(load_registry(ROOT), "hepatitis C in Wayne County")
    row = next(r for r in _rows(out) if r["source_id"] == DS)
    assert row["can_answer"] == "no"
    assert "not county" in row["why"]


def test_a_count_is_read_whole_from_its_published_cell(real: tuple[Registry, Engine, Path]) -> None:
    _, eng, _ = real
    out = eng.query_metric(
        DS, "national_case_count", filters={"condition": "Acute Hepatitis C"}, group_by=["year"]
    )
    assert "sum(" not in out["sql"]
    at, rows = {c: i for i, c in enumerate(out["columns"])}, out["rows"]
    assert sorted((r[at["year"]], r[at["national_case_count"]]) for r in rows) == [(2021, 4798), (2022, 4966)]


def test_every_state_row_comes_back_with_its_code_and_never_a_zero(
    real: tuple[Registry, Engine, Path],
) -> None:
    reg, eng, _ = real
    out = tools.get_series(
        reg, eng, condition="Acute Hepatitis C", measure="cases", locations=["Michigan", "Alaska"],
        source_id=DS, fmt="json",
    )  # fmt: skip
    at, rows = _json(out)
    got = {r[at["location"]]: (r[at["value"]], r[at["flag"]]) for r in rows}
    assert got == {"Michigan": (210, None), "Alaska": (None, "—")}
    alaska = next(r for r in rows if r[at["location"]] == "Alaska")
    assert "not published" in alaska[at["period_status"]]


def test_deaths_are_their_own_series_never_mixed_with_cases(real: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = real
    found = {(s.label, s.measure) for _, s in tools.series_for(reg, "hepatitis c", source_id=DS)}
    assert ("Hepatitis C deaths (death certificates, NVSS), by state", "deaths") in found
    # A death series always says deaths in its name, and a case series never does.
    for label, measure in found:
        assert ("death" in label) == ("NVSS" in label), label
        assert measure != "cases" or "cases reported" in label, label
    out = tools.get_series(
        reg, eng, condition="Hepatitis C", measure="deaths", locations=["Wyoming"], source_id=DS, fmt="json"
    )
    at, rows = _json(out)
    assert [(r[at["value"]], r[at["flag"]]) for r in rows] == [(None, "S")]


def test_the_nationwide_total_is_its_own_row_never_a_sum_of_states(
    real: tuple[Registry, Engine, Path],
) -> None:
    reg, eng, _ = real
    out = tools.get_series(reg, eng, condition="Acute Hepatitis C", measure="rate", source_id=DS, fmt="json")
    at, rows = _json(out)
    national = [r for r in rows if "nationwide 'Total'" in str(r[at["location"]])]
    assert sorted((r[at["year"]], r[at["value"]]) for r in national) == [(2021, 1.5), (2022, 1.5)]
    # The state series leaves CDC's Total out: it is never shown as if it were a state.
    states = {r[at["location"]] for r in rows if r not in national}
    assert states == {"Michigan", "Alaska"}


def test_raw_layer_keeps_the_publisher_values_as_sent(real: tuple[Registry, Engine, Path]) -> None:
    """Hard rule 15: the raw records dbt reads hold every value exactly as the publisher sent it: '4,966' with its
    comma, and each code as written."""
    _, _, lake = real
    files = list((lake / "raw_records" / DS).glob("*.ndjson"))
    assert len(files) == 1
    kept = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    for sent, row in zip(RECORDS, kept, strict=True):
        assert {k: row[k] for k in sent} == sent
    assert kept[1]["count"] == "4,966" and kept[3]["count"] == "—"
