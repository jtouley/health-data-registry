"""Live contract test for cdc_nssp_lab_positivity (NSSP commercial laboratory data, data.cdc.gov jqxy-cyu2).

Socrata leaves a null field out of a row, and CDC suppresses small counts, so every optional value is read with
.get() and an empty one is skipped, as the contract's own rules skip it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.integration import _live

DS = "cdc_nssp_lab_positivity"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    query = "SELECT * WHERE geography_value IN ('MI', 'OH') ORDER BY mmwr_week_end DESC LIMIT 300"
    rows = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], {"$query": query}))
    return cat, con, sem, rows, meta


def _number(r: dict, field: str) -> float | None:
    v = r.get(field)
    return None if v in (None, "") else float(v)


def test_contract_structure(live):
    _cat, con, _sem, rows, meta = live
    _live.check_structure(con, rows, [c["fieldName"] for c in meta["columns"]])


def test_the_dataset_description_still_states_the_facts_the_contract_quotes(live):
    _cat, _con, _sem, _rows, meta = live
    desc = " ".join(str(meta.get("description") or "").split())
    for phrase in (
        "reported at the state level",
        "nucleic acid amplification tests",
        "fewer than 10 tests",
        "Zeros are not suppressed",
        "zip code of residence",
        "aggregated by week based on specimen collection date",
    ):
        assert phrase in desc, phrase


def test_every_virus_is_catalogued(live):
    cat, _con, _sem, _rows, _ = live
    query = {"$query": "SELECT pathogen GROUP BY pathogen LIMIT 100"}
    served = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))
    assert {r.get("pathogen") for r in served} <= set(cat["conditions"]["labels"])


def test_every_row_is_a_state(live):
    cat, _con, _sem, _rows, _ = live
    query = {"$query": "SELECT geography_type GROUP BY geography_type LIMIT 100"}
    served = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))
    assert {r.get("geography_type") for r in served} == {"state"}


def test_the_grain_is_unique(live):
    """pathogen x mmwr_week_end x geography_value is the contract's key: no repeat over the whole table."""
    cat, _con, _sem, _rows, _ = live
    query = {
        "$query": "SELECT pathogen, mmwr_week_end, geography_value, count(*) AS n "
        "GROUP BY pathogen, mmwr_week_end, geography_value HAVING count(*) > 1 LIMIT 5"
    }
    assert _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query)) == []


def test_codebook_facts_hold_on_the_latest_rows(live):
    _cat, _con, _sem, rows, _ = live
    for r in rows:
        start = dt.date.fromisoformat(r["mmwr_week_start"][:10])
        end = dt.date.fromisoformat(r["mmwr_week_end"][:10])
        assert end.weekday() == 5 and (end - start).days == 6, r
        tests, positives, pct = (_number(r, f) for f in ("test_count", "positive_count", "percent_pos"))
        for n in (tests, positives):
            assert n is None or n == 0 or n >= 10, r  # suppressed under 10, zeros kept
        if pct is not None:
            assert 0 <= pct <= 100, r
            assert tests is not None and positives is not None, r
        if tests is not None and positives is not None:
            assert positives <= tests, r


def test_latest_michigan_influenza_percent_two_ways(live):
    _cat, con, sem, rows, _ = live
    flu = [r for r in rows if r.get("pathogen") == "Influenza" and r.get("geography_value") == "MI"]
    published = [r for r in flu if _number(r, "percent_pos") is not None]
    latest = max(published, key=lambda r: r["mmwr_week_end"])
    value = _live.duckdb_metric(
        con,
        sem,
        rows,
        "weekly_percent_tests_positive",
        {"pathogen": "Influenza", "geography_value": "MI", "mmwr_week_end": latest["mmwr_week_end"][:10]},
    )
    assert value == float(latest["percent_pos"])
