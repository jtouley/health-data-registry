"""Live contract test for cdc_nhanes_id_prevalence (DQS NHANES infectious diseases, data.cdc.gov v7tk-n6v3)."""

from __future__ import annotations

from collections import Counter

import pytest

from tests.integration import _live

DS = "cdc_nhanes_id_prevalence"
LIMIT = 5000
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    query = {"$limit": str(LIMIT), "$order": "topic, time_period_id, group_id, subgroup_id"}
    rows = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))
    assert len(rows) < LIMIT, "the table outgrew one page; page through it before trusting these checks"
    return cat, con, sem, rows, meta


def test_contract_structure(live):
    _cat, con, _sem, rows, meta = live
    _live.check_structure(con, rows, [c["fieldName"] for c in meta["columns"]])


def test_licence_is_still_public_domain(live):
    *_, meta = live
    assert (meta.get("license") or {}).get("name") == "Public Domain U.S. Government"


def test_infections_flags_and_estimate_type_are_the_listed_ones(live):
    cat, _con, _sem, rows, _ = live
    assert {r.get("topic") for r in rows} <= set(cat["conditions"]["labels"])
    assert {r.get("flag") for r in rows} - {None} <= {"*", "- - -", "^", "^^"}
    assert {r.get("estimate_type") for r in rows} == {"Percent of population, crude"}


def test_each_interval_brackets_its_estimate_and_an_empty_estimate_is_flagged(live):
    _cat, _con, _sem, rows, _ = live
    for r in rows:
        if r.get("estimate") in (None, ""):
            assert r.get("flag"), r
            continue
        lo, est, hi = (float(r[k]) for k in ("estimate_lci", "estimate", "estimate_uci"))
        assert 0 <= lo <= est <= hi <= 100, r


def test_one_row_per_cell(live):
    _cat, _con, _sem, rows, _ = live
    keys = Counter((r.get("topic"), r.get("time_period"), r.get("group"), r.get("subgroup")) for r in rows)
    assert max(keys.values()) == 1


@pytest.mark.parametrize("infection", ["Herpes simplex virus type 1", "Herpes simplex virus type 2"])
def test_latest_total_estimate_two_ways(live, infection):
    _cat, con, sem, rows, _ = live
    total = [
        r for r in rows if r.get("topic") == infection and r.get("group") == "Total" and r.get("estimate")
    ]
    assert total, f"no Total estimate for {infection}"
    last = max(total, key=lambda r: r["time_period"])
    got = _live.duckdb_metric(
        con,
        sem,
        rows,
        "seroprevalence_all_adults_20_49",
        {"time_period": last["time_period"], "infection": infection},
    )
    assert got == float(last["estimate"])
