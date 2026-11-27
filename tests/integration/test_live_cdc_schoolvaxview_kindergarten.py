"""Live contract test for cdc_schoolvaxview_kindergarten (SchoolVaxView kindergarten coverage and exemptions,
data.cdc.gov ijqb-a7ye)."""

from __future__ import annotations

import pytest

from tests.integration import _live

DS = "cdc_schoolvaxview_kindergarten"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    query = "SELECT * WHERE geography IN ('Michigan', 'United States', 'U.S. Median') LIMIT 2000"
    rows = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], {"$query": query}))
    return cat, con, sem, rows, [c["fieldName"] for c in meta["columns"]]


def test_contract_structure(live):
    _cat, con, _sem, rows, served = live
    _live.check_structure(con, rows, served)


def test_every_vaccine_label_is_catalogued(live):
    cat, _con, _sem, _rows, _ = live
    query = {"$query": "SELECT vaccine GROUP BY vaccine LIMIT 100"}
    served = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))
    assert {r["vaccine"] for r in served} <= set(cat["conditions"]["labels"])


def _codes(con) -> set[str]:
    """The estimate's declared missing codes, read from the contract."""
    (estimate,) = [p for p in _live.properties(con) if p["name"] == "coverage_estimate"]
    return set(_live.custom(estimate)["missingCodes"])


def _non_numeric(cat, field: str) -> set[str]:
    """Every distinct value of one field over the whole table that doesn't read as a number."""
    query = {"$query": f"SELECT {field} GROUP BY {field} LIMIT 10000"}
    served = {r.get(field) for r in _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))}
    odd = set()
    for v in served - {None, ""}:
        try:
            float(v)
        except ValueError:
            odd.add(v)
    return odd


def test_every_estimate_code_is_declared(live):
    """Every non-numeric estimate the publisher serves is a declared missing code (the whole table, not a slice).

    An undeclared code refuses the whole load, so this is the early warning."""
    cat, con, _sem, _rows, _ = live
    odd = _non_numeric(cat, "coverage_estimate")
    assert odd <= _codes(con), sorted(odd - _codes(con))


def test_attribute_counts_and_percents_are_numbers(live):
    """percent_surveyed and population_sample_size declare no codes, so a non-number there would refuse the load."""
    cat, _con, _sem, _rows, _ = live
    for field in ("percent_surveyed", "population_sample_size", "number_of_exemptions"):
        assert _non_numeric(cat, field) == set(), field


def test_michigan_mmr_two_ways(live):
    _cat, con, sem, rows, _ = live
    codes = _codes(con)
    want = [
        r
        for r in rows
        if r.get("geography") == "Michigan"
        and r.get("vaccine") == "MMR"
        and r.get("coverage_estimate") not in codes | {None, ""}
    ]
    latest = max(want, key=lambda r: r["year_season"])
    value = _live.duckdb_metric(
        con,
        sem,
        rows,
        "kindergarten_coverage",
        {"vaccine": "MMR", "geography": "Michigan", "year_season": latest["year_season"]},
    )
    assert value == float(latest["coverage_estimate"])


def test_michigan_any_exemption_two_ways(live):
    _cat, con, sem, rows, _ = live
    codes = _codes(con)
    want = [
        r
        for r in rows
        if r.get("geography") == "Michigan"
        and r.get("dose") == "Any Exemption"
        and r.get("coverage_estimate") not in codes | {None, ""}
    ]
    latest = max(want, key=lambda r: r["year_season"])
    value = _live.duckdb_metric(
        con,
        sem,
        rows,
        "exemption_percent",
        {
            "vaccine": "Exemption",
            "dose": "Any Exemption",
            "geography": "Michigan",
            "year_season": latest["year_season"],
        },
    )
    assert value == float(latest["coverage_estimate"])
