"""Live contract test for cdc_nndss_weekly (NNDSS Weekly Data, data.cdc.gov x9gk-5huc)."""

from __future__ import annotations

import pytest

from tests.integration import _live

DS = "cdc_nndss_weekly"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    rows = _live.get_json(
        cat,
        _live.with_query(
            cat["source"]["api_url"],
            {
                "$query": (
                    "SELECT * WHERE upper(states) IN ('MICHIGAN','US RESIDENTS','TOTAL') AND year = '2024' AND week = 52 "
                    "AND label IN ('Legionellosis','Candida auris, clinical') LIMIT 50"
                )
            },
        ),
    )
    return cat, con, sem, rows, [c["fieldName"] for c in meta["columns"]]


def test_contract_structure(live):
    _cat, con, _sem, rows, served = live
    _live.check_structure(con, rows, served)


@pytest.mark.parametrize("label", ["Legionellosis", "Candida auris, clinical"])
def test_year_to_date_excludes_total_rows_two_ways(live, label):
    _cat, con, sem, rows, _ = live
    # Way 1: plain loop over the raw records (single jurisdictions carry location1).
    loop = sum(float(r["m3"]) for r in rows if r["label"] == label and r.get("location1") and r.get("m3"))
    # Way 2: the semantic metric compiled to DuckDB SQL over the contract's columns.
    sql = _live.duckdb_metric(
        con, sem, rows, "year_to_date_cases", {"year": 2024, "week": 52, "label": label}
    )
    assert sql == loop


def test_us_resident_total_row_two_ways(live):
    _cat, con, sem, rows, _ = live
    loop = sum(
        float(r["m3"])
        for r in rows
        if r["label"] == "Legionellosis" and r["states"].upper() == "US RESIDENTS" and r.get("m3")
    )
    sql = _live.duckdb_metric(
        con, sem, rows, "us_resident_year_to_date_cases", {"year": 2024, "week": 52, "label": "Legionellosis"}
    )
    assert sql == loop and loop > 0
