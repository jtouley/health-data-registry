"""Live contract test for cdc_haicviz_mugsi (HAICViz - MuGSI, data.cdc.gov v4tm-h8pe)."""

from __future__ import annotations

from collections import Counter

import pytest

from tests.integration import _live

DS = "cdc_haicviz_mugsi"
LIMIT = 5000
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    # The whole table is small (341 rows on 2026-11-25), so the live check reads all of it, in a stable order.
    query = {"$limit": str(LIMIT), "$order": "yearname, organism, topic, viewby, series"}
    rows = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))
    assert len(rows) < LIMIT, "the table outgrew one page; page through it before trusting these checks"
    return cat, con, sem, rows, [c["fieldName"] for c in meta["columns"]]


def _enum(con, name):
    return {e["value"] for p in _live.properties(con) if p["name"] == name for e in p.get("enum") or []}


def test_contract_structure(live):
    _cat, con, _sem, rows, served = live
    _live.check_structure(con, rows, served)


def test_labels_are_the_ones_the_contract_lists(live):
    _cat, con, _sem, rows, _ = live
    for col in ("organism", "topic", "viewby"):
        assert {r.get(col) for r in rows} <= _enum(con, col), col


def test_rates_are_never_negative(live):
    _cat, _con, _sem, rows, _ = live
    assert all(float(r["value"]) >= 0 for r in rows if r.get("value") not in (None, ""))


def test_one_row_per_cell(live):
    _cat, _con, _sem, rows, _ = live
    keys = Counter(
        (r.get("yearname"), r.get("organism"), r.get("topic"), r.get("viewby"), r.get("series")) for r in rows
    )
    assert max(keys.values()) == 1


@pytest.mark.parametrize("organism", ["CRE", "CRAB"])
@pytest.mark.parametrize(
    ("metric", "topic"), [("mugsi_case_rate", "Case Rates"), ("mugsi_death_rate", "Death Rates")]
)
def test_latest_overall_rate_two_ways(live, organism, metric, topic):
    _cat, con, sem, rows, _ = live
    overall = [
        r for r in rows
        if r.get("organism") == organism and r.get("topic") == topic and r.get("viewby") == "All cases"
        and r.get("series") == "All cases" and r.get("value") not in (None, "")
    ]  # fmt: skip
    assert overall, f"no overall {topic} for {organism}"
    last = max(overall, key=lambda r: int(r["yearname"]))
    got = _live.duckdb_metric(
        con, sem, rows, metric, {"yearname": int(last["yearname"]), "organism": organism}
    )
    assert got == float(last["value"])
