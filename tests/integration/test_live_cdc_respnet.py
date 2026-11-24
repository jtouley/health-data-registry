"""Live contract test for cdc_respnet (RESP-NET Rates and Clinical Data, data.cdc.gov kvib-3txy)."""

from __future__ import annotations

import pytest

from tests.integration import _live

DS = "cdc_respnet"
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
                    "SELECT * WHERE state IN ('Michigan','Overall') AND surveillance_network = 'FluSurv-NET' "
                    "AND season = '2024-25' AND date_type = 'Week Ending Date' AND rate_type = 'Observed' "
                    "AND age_category = 'Overall' AND race = 'All' AND sex = 'All' LIMIT 300"
                )
            },
        ),
    )
    return cat, con, sem, rows, [c["fieldName"] for c in meta["columns"]]


def test_contract_structure(live):
    _cat, con, _sem, rows, served = live
    _live.check_structure(con, rows, served)


def test_peak_weekly_flu_rate_michigan_two_ways(live):
    _cat, con, sem, rows, _ = live
    weekly = [
        r for r in rows if r["state"] == "Michigan" and r["data_type"] == "Weekly Rate" and r.get("estimate")
    ]
    peak = max(weekly, key=lambda r: float(r["estimate"]))
    loop = float(peak["estimate"])
    sql = _live.duckdb_metric(
        con,
        sem,
        rows,
        "weekly_hospitalization_rate",
        {
            "state": "Michigan",
            "surveillance_network": "FluSurv-NET",
            "date": peak["date"],
            "age_category": "Overall",
            "race": "All",
            "sex": "All",
        },
    )
    assert sql == loop


def test_season_end_cumulative_rate_two_ways(live):
    _cat, con, sem, rows, _ = live
    cum = [
        r
        for r in rows
        if r["state"] == "Michigan" and r["data_type"] == "Cumulative Rate" and r.get("estimate")
    ]
    last = max(cum, key=lambda r: r["date"])
    sql = _live.duckdb_metric(
        con,
        sem,
        rows,
        "cumulative_hospitalization_rate",
        {
            "state": "Michigan",
            "surveillance_network": "FluSurv-NET",
            "date": last["date"],
            "age_category": "Overall",
            "race": "All",
            "sex": "All",
        },
    )
    assert sql == float(last["estimate"])
