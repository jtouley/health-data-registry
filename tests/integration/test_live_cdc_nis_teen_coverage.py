"""Live contract test for cdc_nis_teen_coverage (NIS-Teen vaccination coverage, data.cdc.gov ee48-w5t6)."""

from __future__ import annotations

import re

import pytest

from tests.integration import _live

DS = "cdc_nis_teen_coverage"
pytestmark = pytest.mark.network

# The form every published interval had on 2026-11-25; the contract's interval_text_form rule checks the same.
INTERVAL = re.compile(r"[0-9]+([.][0-9]+)? to [0-9]+([.][0-9]+)?")


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    rows = _live.get_json(
        cat,
        _live.with_query(
            cat["source"]["api_url"],
            {
                "$query": "SELECT * WHERE geography IN ('Michigan', 'United States') AND vaccine = 'HPV' LIMIT 2000"
            },
        ),
    )
    return cat, con, sem, rows, [c["fieldName"] for c in meta["columns"]]


def test_contract_structure(live):
    _cat, con, _sem, rows, served = live
    _live.check_structure(con, rows, served)


def test_every_vaccine_label_is_catalogued(live):
    cat, _con, _sem, _rows, _ = live
    served = _live.get_json(
        cat,
        _live.with_query(cat["source"]["api_url"], {"$query": "SELECT vaccine GROUP BY vaccine LIMIT 100"}),
    )
    assert {r["vaccine"] for r in served} <= set(cat["conditions"]["labels"])


def test_interval_is_kept_as_sent_text(live):
    _cat, _con, _sem, rows, _ = live
    intervals = [r["_95_ci"] for r in rows if r.get("_95_ci")]
    assert intervals, "no interval in the slice"
    odd = [v for v in intervals if not INTERVAL.fullmatch(v)]
    assert not odd, odd[:5]


def test_michigan_hpv_up_to_date_two_ways(live):
    _cat, con, sem, rows, _ = live
    want = [
        r
        for r in rows
        if r["geography"] == "Michigan"
        and r["dose"] == "Up-to-Date, Males and Females"
        and r["dimension"] == "13-17 Years"
        and r["coverage_estimate"] != "NA"
    ]
    latest = max(want, key=lambda r: r["year_season"])
    value = _live.duckdb_metric(
        con,
        sem,
        rows,
        "vaccination_coverage",
        {
            "vaccine": "HPV",
            "dose": "Up-to-Date, Males and Females",
            "geography": "Michigan",
            "year_season": latest["year_season"],
            "dimension_type": "Age",
            "dimension": "13-17 Years",
        },
    )
    assert value == float(latest["coverage_estimate"])
