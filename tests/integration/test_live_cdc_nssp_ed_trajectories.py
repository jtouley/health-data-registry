"""Live contract test for cdc_nssp_ed_trajectories (NSSP ED visit trajectories, data.cdc.gov rdmq-nq56)."""

from __future__ import annotations

from collections import defaultdict

import pytest

from tests.integration import _live

DS = "cdc_nssp_ed_trajectories"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    latest = _live.get_json(
        cat, _live.with_query(cat["source"]["api_url"], {"$query": "SELECT max(week_end) AS w"})
    )[0]["w"]
    rows = _live.get_json(
        cat,
        _live.with_query(
            cat["source"]["api_url"],
            {
                "$query": "SELECT * WHERE geography IN ('Michigan', 'United States') "
                f"AND week_end = '{latest}' LIMIT 500"
            },
        ),
    )
    return cat, con, sem, rows, meta


def test_contract_structure(live):
    _cat, con, _sem, rows, meta = live
    _live.check_structure(con, rows, [c["fieldName"] for c in meta["columns"]])


def test_description_still_says_hsa_values_are_reported_for_each_county(live):
    """The HSA rule quotes the dataset description; if CDC rewords it, the rule needs a fresh look."""
    *_, meta = live
    assert "reported for each county in the HSA" in meta["description"]
    assert "percentage of emergency department patient visits" in meta["description"]


def test_state_and_national_rows_and_shared_hsa_values(live):
    _cat, _con, _sem, rows, _ = live
    totals = [r for r in rows if r["county"] == "All"]
    assert {r["trend_source"] for r in totals} == {"State", "United States"}
    assert {r["geography"] for r in totals} == {"Michigan", "United States"}
    by_hsa: dict[str, set[tuple[str | None, ...]]] = defaultdict(set)
    for r in rows:
        if r["trend_source"] == "HSA":
            by_hsa[r["hsa_nci_id"]].add(
                tuple(
                    r.get(k)
                    for k in ("percent_visits_covid", "percent_visits_influenza", "percent_visits_rsv")
                )
            )
        for k in (
            "percent_visits_covid",
            "percent_visits_influenza",
            "percent_visits_rsv",
            "percent_visits_combined",
        ):
            if r.get(k):
                assert 0 <= float(r[k]) <= 100
    assert by_hsa, "no Michigan HSA rows in the slice"
    assert all(len(v) == 1 for v in by_hsa.values()), "counties of one HSA carry different values"


def test_michigan_rsv_percent_two_ways(live):
    _cat, con, sem, rows, _ = live
    state = next(r for r in rows if r["geography"] == "Michigan" and r["trend_source"] == "State")
    sql = _live.duckdb_metric(
        con,
        sem,
        rows,
        "rsv_percent_of_ed_visits",
        {"geography": "Michigan", "week_end": state["week_end"][:10]},
    )
    assert sql == float(state["percent_visits_rsv"])
