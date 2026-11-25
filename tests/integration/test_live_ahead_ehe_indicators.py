"""Live contract test for ahead_ehe_indicators (AHEAD's "Download All Data" workbook, ahead-files.hiv.gov).

Fetches the workbook at test time (about 50 MB), keeps it in memory only and writes nothing to disk. Checks
that the named sheets still have the contract's columns, that AHEAD's technical notes are still the ones the
contract cites, that every quality rule in the contract holds on the live table, that each place, year and
indicator is one row, and that Wayne County and Michigan are both there.
"""

from __future__ import annotations

import io
import json
import os
import ssl
import urllib.parse
import urllib.request
import zipfile
from typing import Any

import duckdb
import pytest

from health_data_registry.adapters import https_xlsx
from health_data_registry.registry import load_registry
from tests.integration import _live

DS = "ahead_ehe_indicators"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():  # type: ignore[no-untyped-def]
    entry = load_registry().get(DS).entry
    url = entry.source.api_url
    parts = urllib.parse.urlsplit(url)
    assert parts.scheme == "https" and parts.hostname == entry.source.host
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    req = urllib.request.Request(url, headers={"User-Agent": "health-data-registry-tests"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=_live.TIMEOUT_S * 3, context=ctx) as resp:  # noqa: S310
        content = resp.read((entry.source.max_bytes or 0) + 1)
    assert len(content) <= (entry.source.max_bytes or 0), "the workbook is now larger than the catalog's cap"
    return entry, content


def typed(records: list[dict[str, Any]]) -> duckdb.DuckDBPyConnection:
    """The records as the contract types them, in an in-memory database (nothing written to disk)."""
    _cat, con, _sem = _live.load(DS)
    db = duckdb.connect()
    db.execute("CREATE TABLE raw (j JSON)")
    db.executemany("INSERT INTO raw VALUES (?)", [[json.dumps(r)] for r in records])
    cols = []
    for p in _live.properties(con):
        src, typ, name = _live.custom(p)["sourceField"], p["physicalType"], p["name"]
        expr = "TRY_CAST(j->>'$.\"" + src + "\"' AS " + typ + ")"
        cols.append(f'{expr} AS "{name}"')
    db.execute(f"CREATE TABLE {DS} AS SELECT {', '.join(cols)} FROM raw")  # noqa: S608 - contract identifiers
    return db


def test_contract_fields_are_served(live) -> None:  # type: ignore[no-untyped-def]
    entry, content = live
    _cat, con, _sem = _live.load(DS)
    fields, records = https_xlsx.read_workbook(entry, content)
    _live.check_structure(con, records, fields)
    assert {r["sheet"] for r in records} == {"State totals", "Jurisdiction totals"}


def test_quality_rules_and_one_row_per_cell(live) -> None:  # type: ignore[no-untyped-def]
    entry, content = live
    _cat, con, _sem = _live.load(DS)
    db = typed(https_xlsx.records(entry, content))
    for p in _live.properties(con):
        for q in p.get("quality") or []:
            got = db.execute(q["query"].replace("{object}", DS)).fetchone()[0]
            assert got == q["mustBe"], f"{q['id']}: {got} rows break it"
    dupes = db.execute(
        f"SELECT count(*) FROM (SELECT sheet, state, county, year, indicator FROM {DS} GROUP BY ALL HAVING count(*) > 1)"  # noqa: S608, E501
    ).fetchone()[0]
    assert dupes == 0
    each = db.execute(
        f"SELECT indicator, count(DISTINCT counted), count(DISTINCT denominator_description) FROM {DS} GROUP BY 1"
    ).fetchall()  # noqa: S608, E501
    assert len(each) == 6 and all(
        a == b == 1 for _i, a, b in each
    )  # one label and one denominator per indicator


def test_michigan_and_wayne_county_are_there(live) -> None:  # type: ignore[no-untyped-def]
    entry, content = live
    _cat, con, sem = _live.load(DS)
    records = https_xlsx.records(entry, content)
    wayne = [r for r in records if r["county"] == "Wayne County" and r["state"] == "Michigan"]
    assert {r["indicator"] for r in wayne} >= {"Diagnoses", "Incidence", "Knowledge of status", "Linkage to care",
                                               "Viral suppression", "PrEP coverage"}  # fmt: skip
    sub = [r for r in records if r["state"] == "Michigan" and r["year"] == "2022"]
    state = _live.duckdb_metric(
        con, sem, sub, "hiv_diagnoses", {"sheet": "State totals", "state": "Michigan"}
    )
    county = _live.duckdb_metric(
        con, sem, sub, "hiv_diagnoses", {"sheet": "Jurisdiction totals", "county": "Wayne County"}
    )  # noqa: E501
    assert state is not None and county is not None and 0 < county < state


def test_the_cited_technical_notes_are_still_printed(live) -> None:  # type: ignore[no-untyped-def]
    """The contract cites AHEAD's notes by number; if AHEAD rewrites them, a maintainer must re-read them."""
    _entry, content = live
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        text = z.read("xl/sharedStrings.xml").decode("utf-8")
    for phrase in (
        "IX. Empty cells",
        "Data incomplete, not reported, or suppressed due to high level of uncertainty.",
        "Estimates with a RSE of &gt; 50% are statistically unreliable and not displayed.",
        "Estimates are rounded to the nearest 100 for estimates &gt; 1,000 and to the nearest 10",
        "PrEP coverage, reported as a percentage, was calculated as the number of persons who have been prescribed",
        "HIV diagnoses data by jurisdiction are based on residence at time of HIV diagnosis.",
    ):
        assert phrase in text, f"AHEAD's notes no longer say: {phrase}"
