"""cdc_adult_vax_coverage (AdultVaxView BRFSS vaccination coverage among adults, data.cdc.gov aetd-68ew) through the
real catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names, labels and symbols are the
publisher's.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import tools
from health_data_registry.quality import QualityBlocked
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from health_data_registry.tools import series_for
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_adult_vax_coverage"
URL = "https://data.cdc.gov/resource/aetd-68ew.json"
PLACES = {"United States": ("HHS Regions/National", "100"), "Michigan": ("States/Local Areas", "26"),
          "Ohio": ("States/Local Areas", "39")}  # fmt: skip


def rec(vac: str, place: str, year: str, est: str, *, dose: str = "", age: str = ">=65 Years",
        group: str = "Overall", ci: str | None = None, n: str | None = "1000", county: str | None = None) -> dict[str, str]:  # fmt: skip
    kind, fips = ("Counties", county) if county else PLACES[place]
    r = {"vaccine": vac, "dose": dose, "geography_type": kind, "geography": place, "fips": fips, "year_season": year,
         "dimension_type": age, "dimension": group, "coverage_estimate": est}  # fmt: skip
    if est not in ("*", "NR"):
        r["_95_ci"] = ci or "1.0 to 99.0"
    if n and not county:
        r["population_sample_size"] = n
    return r


RECORDS = [
    rec("Pneumococcal", "United States", "2021", "68.0"),
    rec("Pneumococcal", "United States", "2022", "70.1", ci="69.5 to 70.7"),
    rec("Pneumococcal", "Michigan", "2022", "68.2"),
    rec("Pneumococcal", "Ohio", "2022", "NR"),
    rec("Pneumococcal", "Michigan", "2022", "55.0", group="Hispanic"),
    rec("Tetanus", "Michigan", "2022", "60.0", dose="Td or Tdap", age=">=18 Years"),
    rec("Tetanus", "Michigan", "2022", "31.0", dose="Tdap", age=">=18 Years"),
    rec("Zoster (Shingles)", "Michigan", "2022", "*", age=">=60 Years"),
    rec("Pneumococcal", "Champaign", "2022", "66.0", county="17019"),
    rec("Pneumococcal", "Champaign", "2022", "64.0", county="39021"),
]


def _build(tmp_path: Path, records: list[dict[str, str]]) -> tuple[Registry, Engine, Path, Any]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, records)
    m = build_snapshot(lake, reg.get(DS), [raw_fetch(URL, records)], [f])
    return reg, Engine(registry=reg, lake=lake), lake, m


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine, Path, Any]:
    return _build(tmp_path, RECORDS)


def _json(out: Any) -> tuple[dict[str, int], list[list[Any]]]:
    assert isinstance(out, dict), out
    return {c: i for i, c in enumerate(out["columns"])}, out["rows"]


def test_a_plain_disease_question_never_gets_vaccination_coverage() -> None:
    reg = load_registry(ROOT)
    for question in ("Tetanus", "tetanus in Michigan", "Pneumococcal", "Shingles", "Zoster (Shingles)"):
        assert [s.label for d, s in series_for(reg, question) if d.id == DS] == [], question


@pytest.mark.parametrize(
    ("question", "n"),
    [
        ("Tetanus vaccination", 1),
        ("Tdap vaccination", 1),
        ("Pneumococcal vaccination", 2),
        ("Shingles vaccination", 1),
    ],
)
def test_a_vaccination_question_gets_its_series(question: str, n: int) -> None:
    reg = load_registry(ROOT)
    labels = [s.label for d, s in series_for(reg, question) if d.id == DS]
    assert len(labels) == n and all("vaccination coverage" in x for x in labels), labels


def test_get_series_reads_years_as_published_and_never_counties(
    real: tuple[Registry, Engine, Path, Any],
) -> None:
    reg, eng, _, _ = real
    out = tools.get_series(
        reg, eng, condition="Pneumococcal vaccination", locations=["United States", "Michigan", "Ohio"],
        year_start=2022, year_end=2022, source_id=DS, fmt="json",
    )  # fmt: skip
    at, rows = _json(out)
    old = sorted(
        (r[at["location"]], str(r[at["year"]]), r[at["value"]])
        for r in rows
        if "65 and older" in r[at["condition"]]
    )
    # 2022 only (a text year compared whole), Overall only (the Hispanic 55.0 is a different group), and Ohio's 'NR'
    # is no value, never a zero.
    assert old == [("Michigan", "2022", 68.2), ("Ohio", "2022", None), ("United States", "2022", 70.1)]


def test_a_symbol_is_kept_as_published(real: tuple[Registry, Engine, Path, Any]) -> None:
    _, eng, _, _ = real
    at, rows = _json(
        tools.query_sql(
            eng,
            f"SELECT vaccine, coverage_estimate, estimate_as_published, ci_95 FROM {DS} WHERE coverage_estimate IS NULL "
            "ORDER BY vaccine",
            fmt="json",
        )
    )
    assert rows == [["Pneumococcal", None, "NR", None], ["Zoster (Shingles)", None, "*", None]]


def test_an_undeclared_symbol_refuses_the_load(tmp_path: Path) -> None:
    """Fail closed (#188): a symbol the contract doesn't declare is never guessed at or read as a number."""
    with pytest.raises(duckdb.ConversionException, match=r"'N/A' to DOUBLE"):
        _build(tmp_path, [*RECORDS, rec("Tetanus", "Ohio", "2022", "N/A", dose="Tdap", age=">=18 Years")])


def test_raw_layer_keeps_the_publisher_values_as_sent(real: tuple[Registry, Engine, Path, Any]) -> None:
    _, _, lake, _ = real
    (f,) = (lake / "raw_records" / DS).glob("*.ndjson")
    kept = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()]
    for sent, row in zip(RECORDS, kept, strict=True):
        assert {k: row[k] for k in sent} == sent


def test_every_rule_passes_on_publisher_shaped_rows(real: tuple[Registry, Engine, Path, Any]) -> None:
    _, _, _, m = real
    assert {r["status"] for r in m.quality["results"]} == {"pass"}
    assert m.quality["rule_errors"] == []


def _no_dose(r: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in r.items() if k != "dose"}


def _no_interval(r: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in r.items() if k != "_95_ci"}


@pytest.mark.parametrize(
    ("rule", "row"),
    [
        ("vaccine_labels", rec("Influenza", "Ohio", "2021", "40.0")),
        ("geography_types", {**rec("Pneumococcal", "Ohio", "2021", "40.0"), "geography_type": "Tribal"}),
        ("survey_year_is_a_year", rec("Pneumococcal", "Ohio", "2021-2022", "40.0")),
        ("interval_with_every_number", _no_interval(rec("Pneumococcal", "Ohio", "2021", "41.0"))),
        ("interval_with_every_number", {**rec("Pneumococcal", "Ohio", "2021", "NR"), "_95_ci": "1.0 to 2.0"}),
        ("dose_is_never_left_out", _no_dose(rec("Pneumococcal", "Ohio", "2021", "41.0"))),
        ("dose_only_for_tetanus", rec("Pneumococcal", "Ohio", "2021", "41.0", dose="Tdap")),
    ],
)
def test_each_rule_fires_on_the_case_it_is_written_for(
    tmp_path: Path, rule: str, row: dict[str, str]
) -> None:
    """One bad row per case: its own rule warns, and every other check still passes."""
    _, _, _, m = _build(tmp_path, [*RECORDS, row])
    status = {r["test"]: r["status"] for r in m.quality["results"]}
    assert status[rule] == "warn", status
    assert {t for t, st in status.items() if st != "pass"} == {rule}, status


def test_a_percent_over_100_blocks_the_copy(tmp_path: Path) -> None:
    with pytest.raises(QualityBlocked, match="coverage_is_a_percent"):
        _build(tmp_path, [*RECORDS, rec("Pneumococcal", "Ohio", "2021", "100.5")])


def test_a_negative_percent_blocks_the_copy(tmp_path: Path) -> None:
    with pytest.raises(QualityBlocked, match="coverage_is_a_percent"):
        _build(tmp_path, [*RECORDS, rec("Pneumococcal", "Ohio", "2021", "-0.5")])


def test_a_county_question_is_answered_by_place_code_only() -> None:
    reg = load_registry(ROOT)
    q = "pneumococcal vaccination coverage by county in Michigan"
    out = tools.suggest_datasets(reg, q)
    text = out if isinstance(out, str) else out.render("csv")
    rows = list(csv.DictReader(line for line in io.StringIO(str(text)) if not line.startswith("#")))
    mine = next(r for r in rows if r.get("source_id") == DS)
    assert (
        mine["can_answer"] == "yes"
    )  # counties are published; the series read states, query_sql reads counties
