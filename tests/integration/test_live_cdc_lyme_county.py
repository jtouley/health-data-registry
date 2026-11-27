"""Live contract test for the three CDC Lyme disease county tables (data.cdc.gov x5j9-wybp, qtbi-xd4i, 84rx-ksgd).

Each table is read whole (the largest is about 40,000 rows) and nothing is written to disk. A field Socrata leaves
out of a row (it drops empty values) is read as empty, never as an error.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.integration import _live

pytestmark = pytest.mark.network
SOURCES = ["cdc_lyme_county", "cdc_lyme_county_2008_2021", "cdc_lyme_county_1992_2007"]
ERA = {
    "cdc_lyme_county": (2022, 9999),
    "cdc_lyme_county_2008_2021": (2008, 2021),
    "cdc_lyme_county_1992_2007": (1992, 2007),
}
LIMIT = 100_000
KEY = ("year", "state", "fips", "case_status", "sex", "age_cat_yrs")


@pytest.fixture(scope="module", params=SOURCES)
def live(request: pytest.FixtureRequest) -> Any:
    ds = request.param
    cat, con, sem = _live.load(ds)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    rows = _live.get_json(
        cat, _live.with_query(cat["source"]["api_url"], {"$order": ":id", "$limit": str(LIMIT)})
    )
    assert len(rows) < LIMIT, (
        f"{ds}: the table reached {LIMIT} rows, so this check would read only part of it"
    )
    return ds, cat, con, sem, rows, meta


def test_contract_structure(live: Any) -> None:
    _ds, _cat, con, _sem, rows, meta = live
    _live.check_structure(con, rows, [c["fieldName"] for c in meta["columns"]])


def test_the_licence_is_still_the_one_the_catalog_names(live: Any) -> None:
    _ds, cat, _con, _sem, _rows, meta = live
    assert (meta.get("license") or {}).get("name") == "Public Domain U.S. Government"
    assert cat["licence"]["id"] == "public-domain-us-federal"


def test_one_row_per_group_and_years_in_the_tables_era(live: Any) -> None:
    ds, _cat, _con, _sem, rows, _ = live
    keys = [tuple(r.get(k) for k in KEY) for r in rows]
    assert len(set(keys)) == len(keys), f"{ds}: a group of cases appears twice"
    lo, hi = ERA[ds]
    assert all(lo <= int(r["year"]) <= hi for r in rows), ds


def test_labels_are_the_codebooks_or_its_markers(live: Any) -> None:
    ds, _cat, _con, _sem, rows, _ = live
    statuses = {r.get("case_status") for r in rows}
    assert statuses <= {"Confirmed", "Probable"}, statuses
    if ds == "cdc_lyme_county_1992_2007":
        assert statuses == {"Confirmed"}  # the probable definition began in 2008
    markers = {"Suppressed", "Unknown"}
    assert all(re.fullmatch(r"[A-Z]{2}", r.get("state") or "") or r.get("state") in markers for r in rows)
    odd = [
        r for r in rows if not re.fullmatch(r"[0-9]{5}", r.get("fips") or "") and r.get("fips") not in markers
    ]
    # Only the 1992-2007 table has the three state-only values ('41  .', '42  .') the contract's warning reports.
    assert len(odd) <= (3 if ds == "cdc_lyme_county_1992_2007" else 0), odd[:5]


def test_a_states_cases_two_ways(live: Any) -> None:
    _ds, _cat, con, sem, rows, _ = live
    counted = [r for r in rows if r.get("frequency") not in (None, "")]
    latest = max(int(r["year"]) for r in counted)
    pa = [r for r in rows if r.get("state") == "PA" and int(r["year"]) == latest]
    assert pa and all(r.get("frequency") not in (None, "") for r in pa), "an empty count would leave no total"
    loop = sum(int(float(r["frequency"])) for r in pa)
    sql = _live.duckdb_metric(con, sem, rows, "lyme_cases_all", {"state": "PA", "year": latest})
    assert sql == loop


def test_the_nation_adds_every_row_suppressed_places_included(live: Any) -> None:
    _ds, _cat, con, sem, rows, _ = live
    latest = max(int(r["year"]) for r in rows)
    year_rows = [r for r in rows if int(r["year"]) == latest]
    loop = sum(int(float(r["frequency"])) for r in year_rows if r.get("frequency") not in (None, ""))
    assert _live.duckdb_metric(con, sem, rows, "lyme_cases_all", {"year": latest}) == loop
    assert any(r.get("fips") == "Suppressed" for r in year_rows)
