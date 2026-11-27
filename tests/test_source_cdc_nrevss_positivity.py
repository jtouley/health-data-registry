"""cdc_nrevss_positivity (NREVSS percent of tests positive, data.cdc.gov seuz-s2cv) through the real catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names and codes are the publisher's.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import quality, tools
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_nrevss_positivity"
RECORDS = [
    {"week_end": "2024-01-06T00:00:00.000", "pathogen": "COVID-19", "percent_test_positivity": "4.2"},
    {"week_end": "2024-01-06T00:00:00.000", "pathogen": "Influenza", "percent_test_positivity": "11.0"},
    {"week_end": "2024-01-06T00:00:00.000", "pathogen": "RSV", "percent_test_positivity": "7.3"},
    {"week_end": "2024-01-13T00:00:00.000", "pathogen": "RSV", "percent_test_positivity": "6.8"},
]


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine, Path]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(
        lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/seuz-s2cv.json", RECORDS)], [f]
    )
    return reg, Engine(registry=reg, lake=lake), lake


def _rows(out: Any) -> list[dict[str, str]]:
    text = out if isinstance(out, str) else out.render("csv")
    return list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#")))


def test_a_plain_question_about_rsv_test_positivity_routes_here() -> None:
    reg = load_registry(ROOT)
    rows = _rows(tools.suggest_datasets(reg, "RSV test positivity nationally"))
    assert rows[0]["source_id"] == DS
    # No source declares its places yet (#111): offered first, with that one open question.
    assert rows[0]["can_answer"] == "unknown"
    assert rows[0]["why"] == "this source doesn't declare which places it covers yet"


@pytest.mark.parametrize("question", ["RSV test positivity by region", "regional RSV test positivity"])
def test_a_region_question_is_told_the_table_is_national_only(question: str) -> None:
    reg = load_registry(ROOT)
    row = next(r for r in _rows(tools.suggest_datasets(reg, question)) if r["source_id"] == DS)
    assert row["can_answer"] == "no"
    assert "nation only, not region" in row["why"]


def test_a_who_region_question_is_not_read_as_a_us_region() -> None:
    reg = load_registry(ROOT)
    row = next(
        r
        for r in _rows(tools.suggest_datasets(reg, "tuberculosis incidence by WHO region"))
        if r["source_id"] == "who_gho_tb_incidence"
    )
    assert "region" not in row["why"], row


def test_a_state_question_is_told_the_table_is_national_only() -> None:
    reg = load_registry(ROOT)
    row = next(
        r for r in _rows(tools.suggest_datasets(reg, "RSV test positivity in Ohio")) if r["source_id"] == DS
    )
    assert row["can_answer"] == "no"
    assert "nation only" in row["why"]


def test_get_series_reads_each_published_percent_and_never_adds(real: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = real
    out = tools.get_series(reg, eng, condition="RSV", source_id=DS, fmt="json")
    assert isinstance(out, dict), out
    at = {c: i for i, c in enumerate(out["columns"])}
    got = sorted((str(r[at["year"]]), r[at["value"]]) for r in out["rows"])
    assert got == [("2024-01-06", 7.3), ("2024-01-13", 6.8)]
    assert {r[at["source_id"]] for r in out["rows"]} == {DS}


def test_a_place_is_declined_because_the_figure_is_national(real: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = real
    out = tools.get_series(reg, eng, condition="RSV", locations=["Michigan"], source_id=DS, fmt="json")
    assert isinstance(out, dict)
    assert out["rows"] == []
    assert any(
        "the United States as a whole" in n and "nothing is shown for Michigan" in n for n in out["notes"]
    )


def test_raw_layer_keeps_the_publisher_values_as_sent(real: tuple[Registry, Engine, Path]) -> None:
    """Hard rule 15: the raw records dbt reads hold every value exactly as the publisher sent it (text)."""
    _, _, lake = real
    files = list((lake / "raw_records" / DS).glob("*.ndjson"))
    assert len(files) == 1
    kept = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    for sent, row in zip(RECORDS, kept, strict=True):
        assert {k: row[k] for k in sent} == sent


GOOD = [
    ("2024-01-06", "COVID-19", 4.2),
    ("2024-01-06", "Influenza", 11.0),
    ("2024-01-06", "RSV", 7.3),
]


@pytest.mark.parametrize(
    ("rows", "rule", "status"),
    [
        ([*GOOD, ("2024-01-12", "RSV", 6.8)], "week_end_is_a_saturday", "fail"),  # a Friday
        ([*GOOD, ("2024-01-13", "RSV", 100.1)], "percent_between_0_and_100", "fail"),
        ([*GOOD, ("2024-01-13", "RSV", -0.1)], "percent_between_0_and_100", "fail"),
        ([*GOOD, ("2024-01-06", "hMPV", 2.0)], "pathogen_codes", "warn"),  # to the review queue, not a stop
        ([(w, p, None) for w, p, _ in GOOD], "some_percents_present", "fail"),
        # A renamed code with percents still leaves COVID-19 with none: counted against the codebook list only.
        ([*GOOD[1:], ("2024-01-06", "SARS-CoV-2", 4.2)], "every_virus_published", "warn"),
    ],
)
def test_each_contract_rule_catches_the_case_it_names(
    rows: list[tuple[str, str, float | None]], rule: str, status: str
) -> None:
    """Each rule is shown failing on its own bad case, and passing on good rows, not only ever passing."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t (week_end DATE, pathogen VARCHAR, percent_test_positivity DOUBLE)")
    rules = quality.rules_for(load_registry(ROOT).get(DS).contract)
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", GOOD)
    good = quality.run_rules(con, "t", rules)
    assert not good.rule_errors and {r["status"] for r in good.results} == {"pass"}, good.results
    con.execute("DELETE FROM t")
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    report = quality.run_rules(con, "t", rules)
    assert not report.rule_errors
    assert {r["test"]: r["status"] for r in report.results}[rule] == status
