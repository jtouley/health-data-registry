"""Live contract test for census_county_pop (Census Vintage 2025 county file, www2.census.gov).

Fetches the file at test time, writes nothing to disk, and checks the contract's structural facts: the pivot
keeps every value, state rows equal the sum of their counties, and the file still has the declared columns.
"""

from __future__ import annotations

import os
import ssl
import urllib.parse
import urllib.request

import pytest

from health_data_registry.adapters import https_csv
from health_data_registry.registry import load_registry
from tests.integration import _live

DS = "census_county_pop"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():  # type: ignore[no-untyped-def]
    entry = load_registry().get(DS).entry
    url = entry.source.api_url
    parts = urllib.parse.urlsplit(url)
    assert parts.scheme == "https" and parts.hostname == entry.source.host
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    req = urllib.request.Request(url, headers={"User-Agent": "health-data-registry-tests"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=_live.TIMEOUT_S, context=ctx) as resp:  # noqa: S310
        content = resp.read()
    return entry, content


def test_contract_fields_are_served(live) -> None:  # type: ignore[no-untyped-def]
    entry, content = live
    _cat, con, _sem = _live.load(DS)
    records = https_csv.records(entry, content)
    served = set(records[0])
    _live.check_structure(con, records[:500], served)


def test_pivot_keeps_every_value(live) -> None:  # type: ignore[no-untyped-def]
    entry, content = live
    header, rows = https_csv.read_rows(entry, content)
    years = [c[len("POPESTIMATE") :] for c in header if c.startswith("POPESTIMATE")]
    assert years == ["2020", "2021", "2022", "2023", "2024", "2025"]
    records = https_csv.records(entry, content)
    assert len(records) == len(rows) * len(years)
    wide = sorted(r[f"POPESTIMATE{y}"] for r in rows for y in years)
    assert sorted(r["POPESTIMATE"] for r in records) == wide


def test_states_equal_sum_of_counties_two_ways(live) -> None:  # type: ignore[no-untyped-def]
    entry, content = live
    _cat, con, sem = _live.load(DS)
    records = https_csv.records(entry, content)
    states = {r["STATE"]: r for r in records if r["SUMLEV"] == "040" and r["YEAR"] == "2025"}
    assert len(states) == 51  # 50 states and DC; no Puerto Rico in this file
    for fips, row in states.items():
        counties = sum(
            int(r["POPESTIMATE"])
            for r in records
            if r["SUMLEV"] == "050" and r["STATE"] == fips and r["YEAR"] == "2025"
        )
        assert counties == int(row["POPESTIMATE"]), row["STNAME"]
    loop = sum(int(r["POPESTIMATE"]) for r in states.values())
    sub = [r for r in records if r["YEAR"] == "2025"]
    assert _live.duckdb_metric(con, sem, sub, "national_population", {}) == loop
    mi = _live.duckdb_metric(con, sem, sub, "state_population", {"state_name": "Michigan"})
    assert mi == int(states["26"]["POPESTIMATE"])
