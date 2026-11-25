"""Live contract test for cdc_nrevss_positivity (NREVSS percent of tests positive, data.cdc.gov seuz-s2cv)."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.integration import _live

DS = "cdc_nrevss_positivity"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    rows = _live.get_json(
        cat,
        _live.with_query(cat["source"]["api_url"], {"$query": "SELECT * ORDER BY week_end DESC LIMIT 60"}),
    )
    return cat, con, sem, rows, meta


def test_contract_structure(live):
    _cat, con, _sem, rows, meta = live
    _live.check_structure(con, rows, [c["fieldName"] for c in meta["columns"]])


def test_codebook_facts_the_contract_quotes_still_hold(live):
    """The pathogen codes and the Saturday week are the publisher's column descriptions; the rules quote them."""
    _cat, _con, _sem, rows, meta = live
    desc = {c["fieldName"]: c.get("description") or "" for c in meta["columns"]}
    assert "Saturday of the MMWR week" in desc["week_end"]
    for code in ("Influenza", "COVID-19", "RSV"):
        assert code in desc["pathogen"]
    assert {r["pathogen"] for r in rows} <= {"Influenza", "COVID-19", "RSV"}
    for r in rows:
        assert dt.date.fromisoformat(r["week_end"][:10]).weekday() == 5  # Saturday
        assert 0 <= float(r["percent_test_positivity"]) <= 100


def test_latest_rsv_percent_two_ways(live):
    _cat, con, sem, rows, _ = live
    rsv = [r for r in rows if r["pathogen"] == "RSV" and r.get("percent_test_positivity")]
    latest = max(rsv, key=lambda r: r["week_end"])
    sql = _live.duckdb_metric(
        con,
        sem,
        rows,
        "weekly_percent_tests_positive",
        {"pathogen": "RSV", "week_end": latest["week_end"][:10]},
    )
    assert sql == float(latest["percent_test_positivity"])
