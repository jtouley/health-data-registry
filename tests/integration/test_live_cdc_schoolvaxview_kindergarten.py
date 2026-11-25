"""Live contract test for cdc_schoolvaxview_kindergarten (SchoolVaxView kindergarten coverage and exemptions,
data.cdc.gov ijqb-a7ye)."""

from __future__ import annotations

import pytest

from tests.integration import _live

DS = "cdc_schoolvaxview_kindergarten"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    query = "SELECT * WHERE geography IN ('Michigan', 'United States', 'U.S. Median') LIMIT 2000"
    rows = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], {"$query": query}))
    return cat, con, sem, rows, [c["fieldName"] for c in meta["columns"]]


def test_contract_structure(live):
    _cat, con, _sem, rows, served = live
    _live.check_structure(con, rows, served)


def test_every_vaccine_label_is_catalogued(live):
    cat, _con, _sem, _rows, _ = live
    query = {"$query": "SELECT vaccine GROUP BY vaccine LIMIT 100"}
    served = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))
    assert {r["vaccine"] for r in served} <= set(cat["conditions"]["labels"])


def test_every_estimate_code_is_declared(live):
    """Every non-numeric estimate the publisher serves is a declared missing code (the whole table, not a slice)."""
    cat, con, _sem, _rows, _ = live
    (estimate,) = [p for p in _live.properties(con) if p["name"] == "coverage_estimate"]
    codes = set(_live.custom(estimate)["missingCodes"])
    query = {"$query": "SELECT coverage_estimate GROUP BY coverage_estimate LIMIT 5000"}
    served = {
        r.get("coverage_estimate")
        for r in _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))
    }
    odd = []
    for v in served - {None, ""}:
        try:
            float(v)
        except ValueError:
            odd.append(v)
    assert set(odd) <= codes, sorted(set(odd) - codes)


def test_michigan_mmr_two_ways(live):
    _cat, con, sem, rows, _ = live
    want = [
        r
        for r in rows
        if r["geography"] == "Michigan"
        and r["vaccine"] == "MMR"
        and r["coverage_estimate"] not in ("NA", "NReq")
    ]
    latest = max(want, key=lambda r: r["year_season"])
    value = _live.duckdb_metric(
        con,
        sem,
        rows,
        "kindergarten_coverage",
        {"vaccine": "MMR", "geography": "Michigan", "year_season": latest["year_season"]},
    )
    assert value == float(latest["coverage_estimate"])


def test_michigan_any_exemption_two_ways(live):
    _cat, con, sem, rows, _ = live
    want = [r for r in rows if r["geography"] == "Michigan" and r["dose"] == "Any Exemption"]
    latest = max(want, key=lambda r: r["year_season"])
    value = _live.duckdb_metric(
        con,
        sem,
        rows,
        "exemption_percent",
        {
            "vaccine": "Exemption",
            "dose": "Any Exemption",
            "geography": "Michigan",
            "year_season": latest["year_season"],
        },
    )
    assert value == float(latest["coverage_estimate"])
