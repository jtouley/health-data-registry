"""Live contract test for nchs_resp_deaths_pct (NCHS provisional percent of deaths, data.cdc.gov 53g5-jf7x).

Socrata leaves a null field out of a row, and NCHS suppresses counts from 1 to 9, so every optional value is read
with .get() and an empty one is skipped, as the contract's own rules skip it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.integration import _live

DS = "nchs_resp_deaths_pct"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    query = "SELECT * ORDER BY weekending_date DESC LIMIT 400"
    rows = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], {"$query": query}))
    return cat, con, sem, rows, meta


def _number(r: dict, field: str) -> float | None:
    v = r.get(field)
    return None if v in (None, "") else float(v)


def _distinct(cat: dict, field: str) -> set:
    query = {"$query": f"SELECT {field} GROUP BY {field} LIMIT 100"}
    return {r.get(field) for r in _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))}


def test_contract_structure(live):
    _cat, con, _sem, rows, meta = live
    _live.check_structure(con, rows, [c["fieldName"] for c in meta["columns"]])


def test_the_column_descriptions_still_state_the_facts_the_contract_quotes(live):
    _cat, _con, _sem, _rows, meta = live
    desc = {c["fieldName"]: " ".join(str(c.get("description") or "").split()) for c in meta["columns"]}
    assert "between 1-9 are suppressed" in desc["suppressed"]
    assert "Deaths from all causes" in desc["total_deaths"]
    assert "Percent of deaths due to pathogen" in desc["percent_deaths"]
    assert meta["metadata"]["custom_fields"]["Data Quality"]["Geographic Unit of Analysis"] == "Nation"


def test_labels_and_places_are_the_ones_the_contract_lists(live):
    cat, _con, _sem, _rows, _ = live
    assert _distinct(cat, "pathogen") <= set(cat["conditions"]["labels"])
    assert _distinct(cat, "state") == {"United States"}
    assert _distinct(cat, "demographic_type") <= {"Age Group", "Race/Ethnicity", "Sex"}


def test_the_grain_is_unique(live):
    cat, _con, _sem, _rows, _ = live
    query = {
        "$query": "SELECT pathogen, weekending_date, demographic_type, demographic_values, count(*) AS n "
        "GROUP BY pathogen, weekending_date, demographic_type, demographic_values HAVING count(*) > 1 LIMIT 5"
    }
    assert _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query)) == []


def test_codebook_facts_hold_on_the_latest_rows(live):
    _cat, _con, _sem, rows, _ = live
    for r in rows:
        end = dt.date.fromisoformat(r["weekending_date"][:10])
        assert end.weekday() == 5, r
        deaths, total, pct = (_number(r, f) for f in ("deaths", "total_deaths", "percent_deaths"))
        assert deaths is None or deaths == 0 or deaths >= 10, r
        if deaths is not None and total is not None:
            assert deaths <= total, r
        if pct is not None:
            assert 0 <= pct <= 100, r
        if r.get("suppressed"):
            assert deaths is None and pct is None, r


def test_latest_covid_percent_for_65_and_older_two_ways(live):
    _cat, con, sem, rows, _ = live
    want = [
        r
        for r in rows
        if r.get("pathogen") == "COVID-19"
        and r.get("demographic_values") == "65+ years"
        and _number(r, "percent_deaths") is not None
    ]
    latest = max(want, key=lambda r: r["weekending_date"])
    value = _live.duckdb_metric(
        con,
        sem,
        rows,
        "weekly_percent_of_deaths",
        {
            "pathogen": "COVID-19",
            "demographic_type": "Age Group",
            "demographic_values": "65+ years",
            "weekending_date": latest["weekending_date"][:10],
        },
    )
    assert value == float(latest["percent_deaths"])
