"""Live contract test for who_gho_hiv_incidence (WHO GHO SDGHIV). WHO data is never stored.

OData returns every field, nulls included, but each check reads a field with .get() so a field WHO stops sending
fails on the structure check with its name, never with a bare KeyError elsewhere.
"""

from __future__ import annotations

import pytest

from tests.integration import _live

DS = "who_gho_hiv_incidence"
pytestmark = pytest.mark.network
PLACES = ("ZAF", "KEN", "GLOBAL")


@pytest.fixture(scope="module")
def live():  # type: ignore[no-untyped-def]
    cat, con, sem = _live.load(DS)
    flt = "SpatialDim in ('ZAF','KEN','GLOBAL','USA') and TimeDim ge 2018"
    body = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], {"$filter": flt}))
    rows = body["value"]
    assert "@odata.nextLink" not in body, "the test slice spans more than one page"
    served = set().union(*(r.keys() for r in rows))
    return cat, con, sem, rows, served


def test_contract_structure(live) -> None:  # type: ignore[no-untyped-def]
    _cat, con, _sem, rows, served = live
    _live.check_structure(con, rows, served)


def test_sex_codes_and_grain(live) -> None:  # type: ignore[no-untyped-def]
    _cat, _con, _sem, rows, _ = live
    assert {r.get("Dim1") for r in rows} <= {"SEX_BTSX", "SEX_FMLE", "SEX_MLE", "SEX_NOA"}
    keys = [(r.get("SpatialDim"), r.get("TimeDim"), r.get("Dim1")) for r in rows]
    assert len(set(keys)) == len(keys)
    assert {r.get("IndicatorCode") for r in rows} == {"SDGHIV"}


def test_no_us_row_so_nothing_is_answered_for_the_united_states(live) -> None:  # type: ignore[no-untyped-def]
    _cat, _con, _sem, rows, _ = live
    assert not [r for r in rows if r.get("SpatialDim") == "USA"], (
        "WHO now publishes the US: update the caveat"
    )


@pytest.mark.parametrize("place", PLACES)
def test_latest_both_sexes_estimate_with_bounds_two_ways(live, place) -> None:  # type: ignore[no-untyped-def]
    _cat, con, sem, rows, _ = live
    mine = [
        r
        for r in rows
        if r.get("SpatialDim") == place and r.get("Dim1") == "SEX_BTSX" and r.get("NumericValue") is not None
    ]
    assert mine, f"no both-sexes estimate for {place}"
    latest = max(mine, key=lambda r: r["TimeDim"])
    loop = float(latest["NumericValue"])
    sql = _live.duckdb_metric(
        con, sem, rows, "hiv_incidence_rate", {"spatial_dim": place, "time_dim": latest["TimeDim"]}
    )
    assert sql == loop
    if latest.get("Low") is not None and latest.get("High") is not None:
        assert latest["Low"] <= loop <= latest["High"]
