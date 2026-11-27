"""Live contract test for cdc_haicviz_isa (HAICViz - iSA, data.cdc.gov ssz5-s49e)."""

from __future__ import annotations

from collections import Counter

import pytest

from tests.integration import _live

DS = "cdc_haicviz_isa"
LIMIT = 5000
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    # The whole table is small (710 rows on 2026-11-25), so the live check reads all of it, in a stable order.
    query = {"$limit": str(LIMIT), "$order": "yearname, topic, viewby, series"}
    rows = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))
    assert len(rows) < LIMIT, "the table outgrew one page; page through it before trusting these checks"
    return cat, con, sem, rows, [c["fieldName"] for c in meta["columns"]]


def _enum(con, name):
    return {e["value"] for p in _live.properties(con) if p["name"] == name for e in p.get("enum") or []}


def test_contract_structure(live):
    _cat, con, _sem, rows, served = live
    _live.check_structure(con, rows, served)


def test_topics_are_the_ones_the_contract_lists(live):
    _cat, con, _sem, rows, _ = live
    assert {r.get("topic") for r in rows} <= _enum(con, "topic")


def test_views_are_the_ones_the_contract_lists(live):
    _cat, con, _sem, rows, _ = live
    assert {r.get("viewby") for r in rows} <= _enum(con, "viewby")


def test_values_are_never_negative(live):
    _cat, _con, _sem, rows, _ = live
    assert all(float(r["value"]) >= 0 for r in rows if r.get("value") not in (None, ""))


def test_one_row_per_cell(live):
    _cat, _con, _sem, rows, _ = live
    keys = Counter((r.get("yearname"), r.get("topic"), r.get("viewby"), r.get("series")) for r in rows)
    assert max(keys.values()) == 1


def test_total_rows_are_still_labelled_as_the_contract_says(live):
    _cat, _con, _sem, rows, _ = live
    views = {
        r.get("viewby") for r in rows if r.get("topic") == "Case rates" and r.get("series") == "All cases"
    }
    assert {"All cases", "Age", "Sex", "Epi class"} <= views


@pytest.mark.parametrize(
    ("metric", "topic"), [("isa_case_rate", "Case rates"), ("isa_death_rate", "Death rates")]
)
def test_latest_overall_rate_two_ways(live, metric, topic):
    _cat, con, sem, rows, _ = live
    overall = [
        r for r in rows
        if r.get("topic") == topic and r.get("viewby") == "All cases" and r.get("series") == "All cases"
        and r.get("value") not in (None, "")
    ]  # fmt: skip
    assert overall, f"no overall {topic}"
    last = max(overall, key=lambda r: int(r["yearname"]))
    got = _live.duckdb_metric(con, sem, rows, metric, {"yearname": int(last["yearname"])})
    assert got == float(last["value"])
