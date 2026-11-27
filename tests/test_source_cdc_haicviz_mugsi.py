"""cdc_haicviz_mugsi (HAICViz MuGSI, data.cdc.gov v4tm-h8pe) through the real catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names and labels are the publisher's.
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
from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_haicviz_mugsi"
PLACE = "the Emerging Infections Program sites combined, not by state"


def _row(year: str, organism: str, topic: str, viewby: str, series: str, value: str | None) -> dict[str, str]:
    row = {"yearname": year, "organism": organism, "topic": topic, "viewby": viewby, "series": series}
    return row if value is None else {**row, "value": value}


RECORDS = [
    _row("2021", "CRE", "Case Rates", "All cases", "All cases", "6.1"),
    _row("2022", "CRE", "Case Rates", "All cases", "All cases", "6.4"),
    _row("2022", "CRE", "Death Rates", "All cases", "All cases", "0.4"),
    _row("2022", "CRE", "Case Rates", "Culture source", "All cases", "6.4"),
    _row("2022", "CRE", "Case Rates", "Culture source", "Urine", "4.1"),
    _row("2022", "CRE", "Case Rates", "Age", "80+ years", "30.2"),
    _row("2022", "CRAB", "Case Rates", "All cases", "All cases", "2.3"),
    _row("2022", "CRAB", "Death Rates", "All cases", "All cases", "0.3"),
    _row("2022", "CRAB", "Case Rates", "Sex", "Male", None),
]


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine, Path]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(
        lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/v4tm-h8pe.json", RECORDS)], [f]
    )
    return reg, Engine(registry=reg, lake=lake), lake


def _rows(out: Any) -> list[dict[str, str]]:
    text = out if isinstance(out, str) else out.render("csv")
    return list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#")))


@pytest.mark.parametrize(
    "question", ["carbapenem-resistant Enterobacterales rates", "CRAB incidence over time"]
)
def test_a_plain_question_about_cre_or_crab_routes_here(question: str) -> None:
    rows = _rows(tools.suggest_datasets(load_registry(ROOT), question))
    # No source declares its places yet (#111), so the best a question gets is "unknown" for the place alone:
    # it is offered first, with that one open question.
    assert rows[0]["source_id"] == DS and rows[0]["can_answer"] == "unknown", rows[0]
    assert rows[0]["why"].startswith("this source doesn't declare which places it covers yet"), rows[0]
    assert ";" not in rows[0]["why"], rows[0]


def test_a_state_question_is_told_there_are_no_state_values() -> None:
    out = tools.suggest_datasets(load_registry(ROOT), "CRE in Michigan")
    row = next(r for r in _rows(out) if r["source_id"] == DS)
    assert row["can_answer"] == "no"
    assert "not state" in row["why"]


def test_each_rate_is_read_from_one_published_cell(real: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = real
    found = {(s.metric, s.filters["organism"]) for _, s in tools.series_for(reg, "CRE", source_id=DS)}
    assert found == {("mugsi_case_rate", "CRE"), ("mugsi_death_rate", "CRE")}
    out = eng.query_metric(DS, "mugsi_case_rate", filters={"organism": "CRE"}, group_by=["year"])
    assert "sum(" not in out["sql"]
    at = {c: i for i, c in enumerate(out["columns"])}
    # The overall 'All cases' row only, never the culture-source 'All cases' row beside it.
    assert sorted((r[at["year"]], r[at["mugsi_case_rate"]]) for r in out["rows"]) == [
        (2021, 6.1),
        (2022, 6.4),
    ]


def test_cre_and_crab_are_never_added(real: tuple[Registry, Engine, Path]) -> None:
    _, eng, _ = real
    with pytest.raises(QueryRefused):
        eng.query_metric(DS, "mugsi_case_rate", filters={"year": 2022})


def test_by_stratum_leaves_out_the_all_cases_rows(real: tuple[Registry, Engine, Path]) -> None:
    _, eng, _ = real
    out = eng.query_metric(
        DS,
        "mugsi_case_rate_by_stratum",
        filters={"organism": "CRE", "year": 2022},
        group_by=["view", "stratum"],
    )
    at = {c: i for i, c in enumerate(out["columns"])}
    got = sorted((r[at["view"]], r[at["stratum"]], r[at["mugsi_case_rate_by_stratum"]]) for r in out["rows"])
    assert got == [("Age", "80+ years", 30.2), ("Culture source", "Urine", 4.1)]


def test_get_series_names_the_eip_sites_and_never_a_state(real: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = real
    out = tools.get_series(reg, eng, condition="CRAB", measure="rate", source_id=DS, fmt="json")
    assert isinstance(out, dict), out
    at = {c: i for i, c in enumerate(out["columns"])}
    assert {r[at["location"]] for r in out["rows"]} == {PLACE}
    assert sorted((r[at["year"]], r[at["value"]]) for r in out["rows"]) == [(2022, 0.3), (2022, 2.3)]
    declined = tools.get_series(reg, eng, condition="CRAB", locations=["Michigan"], source_id=DS, fmt="json")
    assert isinstance(declined, dict)
    assert declined["rows"] == []


def test_a_spelled_out_name_reaches_the_cre_series(real: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = real
    out = tools.get_series(
        reg, eng, condition="carbapenem-resistant Enterobacterales", source_id=DS, fmt="json"
    )
    assert isinstance(out, dict), out
    at = {c: i for i, c in enumerate(out["columns"])}
    assert {r[at["year"]] for r in out["rows"]} == {2021, 2022}


def test_raw_layer_keeps_the_publisher_values_as_sent(real: tuple[Registry, Engine, Path]) -> None:
    """Hard rule 15: the raw records dbt reads hold every value exactly as the publisher sent it, the absent rate
    included (absent, never a zero)."""
    _, _, lake = real
    files = list((lake / "raw_records" / DS).glob("*.ndjson"))
    assert len(files) == 1
    kept = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    for sent, row in zip(RECORDS, kept, strict=True):
        assert {k: row[k] for k in sent} == sent
    assert kept[-1].get("value") in (None, "")


Cell = tuple[int, str, str, str, str, float | None]
GOOD: list[Cell] = [
    (2022, "CRE", "Case Rates", "All cases", "All cases", 6.4),
    (2022, "CRE", "Death Rates", "All cases", "All cases", 0.4),
    (2022, "CRAB", "Case Rates", "All cases", "All cases", 2.3),
    (2022, "CRAB", "Death Rates", "All cases", "All cases", 0.3),
    (2022, "CRE", "Case Rates", "Culture source", "Urine", 4.1),
    (2022, "CRE", "Case Rates", "Age", "80+ years", 30.2),
]


def _swap(cell: Cell, **kw: Any) -> Cell:
    at = {"yearname": 0, "organism": 1, "topic": 2, "viewby": 3, "series": 4, "value": 5}
    out = list(cell)
    for k, v in kw.items():
        out[at[k]] = v
    return (out[0], out[1], out[2], out[3], out[4], out[5])


@pytest.mark.parametrize(
    ("rows", "rule", "status"),
    [
        ([*GOOD, (2022, "CRE", "Case Rates", "Sex", "Male", -0.5)], "rates_not_negative", "fail"),
        (
            [*GOOD, (2022, "ESBL", "Case Rates", "All cases", "All cases", 50.0)],
            "organism_is_cre_or_crab",
            "warn",
        ),
        ([*GOOD[:4], _swap(GOOD[4], topic="Incidence"), GOOD[5]], "topic_is_case_or_death_rates", "warn"),
        ([*GOOD[:4], _swap(GOOD[4], viewby="Specimen source"), GOOD[5]], "viewby_labels_known", "warn"),
        ([*GOOD[:2], _swap(GOOD[2], series="All Cases"), *GOOD[3:]], "headline_case_rates_present", "fail"),
        ([GOOD[0], _swap(GOOD[1], value=None), *GOOD[2:]], "headline_death_rates_present", "fail"),
        (
            [GOOD[0], _swap(GOOD[1], value=None), GOOD[2], _swap(GOOD[3], value=None), *GOOD[4:]],
            "death_rates_present",
            "fail",
        ),
        (
            [*GOOD, (2022, "CRE", "Case Rates", "Organism", "Urine", 1.0)],
            "one_group_per_label_in_by_group_rates",
            "warn",
        ),
        ([*GOOD, GOOD[4]], "one_row_per_year_and_stratum", "fail"),
    ],
)
def test_each_contract_rule_catches_the_case_it_names(rows: list[Cell], rule: str, status: str) -> None:
    """Each rule passes on good rows and is shown failing, as a data failure, on its own bad case."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (yearname BIGINT, organism VARCHAR, topic VARCHAR, viewby VARCHAR, series VARCHAR, "
        "value DOUBLE)"
    )
    rules = quality.rules_for(load_registry(ROOT).get(DS).contract)
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?)", GOOD)
    good = quality.run_rules(con, "t", rules)
    assert not good.rule_errors and {r["status"] for r in good.results} == {"pass"}, good.results
    con.execute("DELETE FROM t")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?)", rows)
    report = quality.run_rules(con, "t", rules)
    assert not report.rule_errors
    assert {r["test"]: r["status"] for r in report.results}[rule] == status
