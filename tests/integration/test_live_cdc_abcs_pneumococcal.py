"""Live contract test for cdc_abcs_pneumococcal (ABCs Streptococcus pneumoniae, data.cdc.gov en3s-hzsr)."""

from __future__ import annotations

import pytest

from tests.integration import _live

DS = "cdc_abcs_pneumococcal"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    # The whole table is small (under 2,000 rows), so the live check reads all of it.
    rows = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], {"$limit": "5000"}))
    # A full page means the table outgrew one read: fail loudly rather than check an arbitrary partial page.
    assert len(rows) < 5000, (
        "the live table reached the 5,000-row page; page through it with $order and $offset"
    )
    return cat, con, sem, rows, meta


def test_contract_structure(live):
    _cat, con, _sem, rows, meta = live
    _live.check_structure(con, rows, [c["fieldName"] for c in meta["columns"]])


def test_one_bacterium_and_the_documented_units(live):
    _cat, _con, _sem, rows, _ = live
    assert {r["bacteria"] for r in rows} == {"Streptococcus pneumoniae"}
    assert {r["units"] for r in rows} <= {"Per 100,000 population", "Percent", "Counts"}
    for r in rows:
        if r.get("value") is None:
            continue
        v = float(r["value"])
        assert v >= 0
        if r["units"] == "Percent":
            assert v <= 100
        if r["units"] == "Counts":
            assert v == int(v)


def test_total_rows_are_still_labelled_as_the_contract_says(live):
    _cat, _con, _sem, rows, _ = live
    assert any(r["topic"] == "Case Rates" and r["viewby"] == "Overall" for r in rows)
    assert {"Total cases", "Number of deaths"} <= {r.get("viewby2") for r in rows}
    assert "All Serotypes" in {r.get("viewby2") for r in rows if r["topic"] == "Serotypes"}


def test_latest_overall_case_rate_two_ways(live):
    _cat, con, sem, rows, _ = live
    overall = [r for r in rows if r["topic"] == "Case Rates" and r["viewby"] == "Overall" and r.get("value")]
    last = max(overall, key=lambda r: int(r["year"]))
    sql = _live.duckdb_metric(con, sem, rows, "ipd_case_rate", {"year": int(last["year"])})
    assert sql == float(last["value"])


def test_latest_case_count_two_ways(live):
    _cat, con, sem, rows, _ = live
    total = [r for r in rows if r.get("viewby2") == "Total cases" and r.get("value")]
    last = max(total, key=lambda r: int(r["year"]))
    sql = _live.duckdb_metric(con, sem, rows, "ipd_cases", {"year": int(last["year"])})
    assert sql == float(last["value"])
