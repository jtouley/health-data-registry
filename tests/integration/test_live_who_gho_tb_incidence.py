"""Live contract test for who_gho_tb_incidence (WHO GHO MDG_0000000020). WHO data is never stored."""

from __future__ import annotations

import pytest

from tests.integration import _live

DS = "who_gho_tb_incidence"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    body = _live.get_json(
        cat,
        _live.with_query(
            cat["source"]["api_url"], {"$filter": "SpatialDim in ('USA','GLOBAL') and TimeDim ge 2020"}
        ),
    )
    rows = body["value"]
    served = set().union(*(r.keys() for r in rows))  # OData returns every field, nulls included
    return cat, con, sem, rows, served


def test_contract_structure(live):
    _cat, con, _sem, rows, served = live
    _live.check_structure(con, rows, served)


@pytest.mark.parametrize("place", ["USA", "GLOBAL"])
def test_latest_incidence_with_bounds_two_ways(live, place):
    _cat, con, sem, rows, _ = live
    mine = [r for r in rows if r["SpatialDim"] == place and r["NumericValue"] is not None]
    latest = max(mine, key=lambda r: r["TimeDim"])
    loop = float(latest["NumericValue"])
    sql = _live.duckdb_metric(
        con, sem, rows, "tb_incidence_rate", {"spatial_dim": place, "time_dim": latest["TimeDim"]}
    )
    assert sql == loop
    assert latest["Low"] <= loop <= latest["High"]
