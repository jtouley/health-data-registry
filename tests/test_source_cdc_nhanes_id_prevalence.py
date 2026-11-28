"""cdc_nhanes_id_prevalence (NHANES infectious disease seroprevalence, data.cdc.gov v7tk-n6v3) through the catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names, labels and flags are NCHS's.
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
DS = "cdc_nhanes_id_prevalence"
HSV2 = "Herpes simplex virus type 2"
BASE = {
    "taxonomy": "Infectious diseases",
    "estimate_type": "Percent of population, crude",
    "footnote_id_list": "FN",
}


def _row(
    topic: str, grp: str, sub: str, cycle: str, est: tuple[str, str, str] | None, flag: str | None
) -> dict:
    cls = {"Total": "Total", "Sex and age group": "Multiple Characteristics"}.get(
        grp, "Demographic Characteristic"
    )
    row = {**BASE, "topic": topic, "classification": cls, "group": grp, "subgroup": sub, "time_period": cycle}
    if grp == "Sex and age group":
        row["nesting_label"] = sub.split(":")[0]
    if est:
        row |= {"estimate": est[0], "estimate_lci": est[1], "estimate_uci": est[2], "standard_error": "1.0"}
    return row if flag is None else {**row, "flag": flag}


RECORDS = [
    _row(HSV2, "Total", "20-49 years", "2015-2016", ("13.9", "11.1", "17.2"), None),
    _row(HSV2, "Total", "20-49 years", "2017-2018", ("14.6", "12.4", "17.1"), None),
    _row(HSV2, "Sex and age group", "Male: 20-29 years", "1999-2000", ("6.2", "2.2", "13"), "*"),
    _row("Herpes simplex virus type 1", "Total", "20-49 years", "2017-2018", ("48.1", "45.0", "51.2"), None),
    _row("Herpes simplex virus type 1", "Race and Hispanic origin", "Asian only, non-Hispanic", "1999-2000", None,
         "- - -"),
]  # fmt: skip


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine, Path]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(
        lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/v7tk-n6v3.json", RECORDS)], [f]
    )
    return reg, Engine(registry=reg, lake=lake), lake


def _rows(out: Any) -> list[dict[str, str]]:
    text = out if isinstance(out, str) else out.render("csv")
    return list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#")))


def test_a_plain_hsv2_seroprevalence_question_routes_here() -> None:
    rows = _rows(tools.suggest_datasets(load_registry(ROOT), "HSV-2 seroprevalence in adults"))
    # No source declares its places yet (#111), so the best a question gets is "unknown" for the place alone:
    # it is offered first, with that one open question.
    assert rows[0]["source_id"] == DS and rows[0]["can_answer"] == "unknown", rows[0]
    assert rows[0]["why"].startswith("this source doesn't declare which places it covers yet"), rows[0]
    assert ";" not in rows[0]["why"], rows[0]


def test_a_state_question_is_told_there_are_no_state_values() -> None:
    out = tools.suggest_datasets(load_registry(ROOT), "herpes simplex virus type 2 in Michigan")
    row = next(r for r in _rows(out) if r["source_id"] == DS)
    assert row["can_answer"] == "no"
    assert "not state" in row["why"]


def test_get_series_returns_nchs_interval_with_every_estimate(real: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = real
    out = tools.get_series(reg, eng, condition="HSV-2", source_id=DS, fmt="json")
    assert isinstance(out, dict), out
    at = {c: i for i, c in enumerate(out["columns"])}
    got = sorted((r[at["year"]], r[at["value"]], r[at["lower"]], r[at["upper"]]) for r in out["rows"])
    assert got == [("2015-2016", 13.9, 11.1, 17.2), ("2017-2018", 14.6, 12.4, 17.1)]
    assert {r[at["measure"]] for r in out["rows"]} == {"percent"}
    assert all("antibody" in r[at["condition"]] for r in out["rows"])


def test_a_flagged_estimate_comes_back_with_its_flag(real: tuple[Registry, Engine, Path]) -> None:
    _, eng, _ = real
    out = eng.query_metric(DS, "seroprevalence_by_subgroup", group_by=["infection", "cycle", "subgroup"])
    assert "sum(" not in out["sql"]
    at = {c: i for i, c in enumerate(out["columns"])}
    got = {r[at["subgroup"]]: (r[at["seroprevalence_by_subgroup"]], r[at["flag"]]) for r in out["rows"]}
    # The Total rows are left out; the unreliable estimate keeps its '*', and the unavailable one stays empty.
    assert got == {"Male: 20-29 years": (6.2, "*"), "Asian only, non-Hispanic": (None, "- - -")}


def test_raw_layer_keeps_the_publisher_values_as_sent(real: tuple[Registry, Engine, Path]) -> None:
    """Hard rule 15: the raw records dbt reads hold every value exactly as NCHS sent it, the absent estimate
    included (absent, never a zero)."""
    _, _, lake = real
    files = list((lake / "raw_records" / DS).glob("*.ndjson"))
    assert len(files) == 1
    kept = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    for sent, row in zip(RECORDS, kept, strict=True):
        assert {k: row[k] for k in sent} == sent
    assert kept[-1].get("estimate") in (None, "")
