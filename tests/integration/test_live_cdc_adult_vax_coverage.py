"""Live contract test for cdc_adult_vax_coverage (AdultVaxView BRFSS coverage, data.cdc.gov aetd-68ew).

Socrata leaves a null field out of a row, so every optional value is read with .get(); symbols are read from the
contract's missingCodes, never hard-coded here.
"""

from __future__ import annotations

import re

import pytest

from tests.integration import _live

DS = "cdc_adult_vax_coverage"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    query = "SELECT * WHERE geography IN ('United States', 'Michigan') LIMIT 3000"
    rows = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], {"$query": query}))
    return cat, con, sem, rows, meta


def _codes(con: dict) -> set[str]:
    (est,) = [p for p in _live.properties(con) if p["name"] == "coverage_estimate"]
    return {str(c) for c in _live.custom(est)["missingCodes"]}


def _distinct(cat: dict, field: str) -> set:
    query = {"$query": f"SELECT {field} GROUP BY {field} LIMIT 5000"}
    return {r.get(field) for r in _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))}


def test_contract_structure(live):
    _cat, con, _sem, rows, meta = live
    _live.check_structure(con, rows, [c["fieldName"] for c in meta["columns"]])


def test_the_metadata_still_states_the_facts_the_contract_quotes(live):
    _cat, _con, _sem, _rows, meta = live
    desc = {c["fieldName"]: " ".join(str(c.get("description") or "").split()) for c in meta["columns"]}
    assert 'Non-numeric data are present in this column, such as "*"' in desc["coverage_estimate"]
    assert "95% confidence interval" in desc["_95_ci"]
    assert "Behavioral Risk Factor Surveillance System" in str(meta.get("description"))


def test_every_symbol_in_the_whole_table_is_declared(live):
    cat, con, _sem, _rows, _ = live
    odd = set()
    for v in _distinct(cat, "coverage_estimate") - {None, ""}:
        try:
            float(v)
        except ValueError:
            odd.add(v)
    assert odd <= _codes(con), sorted(odd - _codes(con))


def test_labels_and_years_are_the_ones_the_contract_expects(live):
    cat, _con, _sem, _rows, _ = live
    assert _distinct(cat, "vaccine") <= set(cat["conditions"]["labels"])
    assert _distinct(cat, "geography_type") <= {"HHS Regions/National", "States/Local Areas", "Counties"}
    assert all(re.fullmatch(r"\d{4}", str(y)) for y in _distinct(cat, "year_season"))


def test_the_grain_is_unique(live):
    cat, _con, _sem, _rows, _ = live
    key = "vaccine, dose, fips, year_season, dimension_type, dimension"
    query = {"$query": f"SELECT {key}, count(*) AS n GROUP BY {key} HAVING count(*) > 1 LIMIT 5"}
    assert _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query)) == []


def test_codebook_facts_hold_on_the_slice(live):
    _cat, con, _sem, rows, _ = live
    codes = _codes(con)
    for r in rows:
        v = r.get("coverage_estimate")
        if v in codes or v in (None, ""):
            assert not r.get("_95_ci"), r
            continue
        assert 0 <= float(v) <= 100, r
        assert r.get("_95_ci"), r


def test_latest_michigan_pneumococcal_two_ways(live):
    _cat, con, sem, rows, _ = live
    codes = _codes(con)
    want = [
        r
        for r in rows
        if r.get("geography") == "Michigan"
        and r.get("vaccine") == "Pneumococcal"
        and r.get("dimension_type") == ">=65 Years"
        and r.get("dimension") == "Overall"
        and r.get("coverage_estimate") not in codes | {None, ""}
    ]
    latest = max(want, key=lambda r: r["year_season"])
    value = _live.duckdb_metric(
        con,
        sem,
        rows,
        "yearly_vaccination_coverage",
        {
            "vaccine": "Pneumococcal",
            "fips": latest["fips"],
            "year_season": latest["year_season"],
            "dimension_type": ">=65 Years",
            "dimension": "Overall",
            "dose": "",
        },  # fmt: skip
    )
    assert value == float(latest["coverage_estimate"])
