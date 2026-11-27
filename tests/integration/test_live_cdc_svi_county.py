"""Live contract test for cdc_svi_county (CDC/ATSDR SVI 2022 U.S. county file, svi.cdc.gov).

Fetches the file at test time, writes nothing to disk, and checks the contract's structural facts: the file still
has the declared columns, one row per five-digit county FIPS in the 50 states and DC, ranks between 0 and 1 or the
documented -999, and the summary flag count between 0 and 16.
"""

from __future__ import annotations

import os
import ssl
import urllib.parse
import urllib.request

import pytest

from health_data_registry.adapters.https_file import read_records
from health_data_registry.registry import load_registry
from health_data_registry.source_config import HttpsFileConfig
from tests.integration import _live

DS = "cdc_svi_county"
RANKS = ("RPL_THEME1", "RPL_THEME2", "RPL_THEME3", "RPL_THEME4", "RPL_THEMES")
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():  # type: ignore[no-untyped-def]
    entry = load_registry().get(DS).entry
    cfg = entry.source.config()
    assert isinstance(cfg, HttpsFileConfig)
    url = entry.source.api_url
    parts = urllib.parse.urlsplit(url)
    assert parts.scheme == "https" and parts.hostname == entry.source.host
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    req = urllib.request.Request(url, headers={"User-Agent": "health-data-registry-tests"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=_live.TIMEOUT_S, context=ctx) as resp:  # noqa: S310
        content = resp.read()
    header, records, _prov = read_records(entry, cfg, content)
    return header, records


def test_contract_fields_are_served(live) -> None:  # type: ignore[no-untyped-def]
    header, records = live
    _cat, con, _sem = _live.load(DS)
    added = {"document_url", "document_sha256", "document_retrieved_at"}  # the adapter's, not the publisher's
    stub = [{**r, **dict.fromkeys(added, "x")} for r in records[:500]]
    _live.check_structure(con, stub, set(header) | added)
    assert not header[0].startswith("﻿"), "the byte-order mark reached the first label"


def test_one_row_per_county_in_the_50_states_and_dc(live) -> None:  # type: ignore[no-untyped-def]
    _header, records = live
    keys = [r["STCNTY"] for r in records]
    assert len(keys) == len(set(keys)), "a county FIPS repeats"
    assert all(len(k) == 5 and k.isdigit() and k[:2] == r["ST"] for k, r in zip(keys, records, strict=True))
    assert len({r["ST"] for r in records}) == 51  # Puerto Rico is a separate database (documentation)
    assert "72" not in {r["ST"] for r in records}


def test_ranks_and_flags_are_in_their_documented_ranges(live) -> None:  # type: ignore[no-untyped-def]
    _header, records = live
    for r in records:
        for col in RANKS:
            v = r[col]
            assert v == "-999" or 0.0 <= float(v) <= 1.0, (r["STCNTY"], col, v)
        assert r["F_TOTAL"] == "-999" or 0 <= int(r["F_TOTAL"]) <= 16, (r["STCNTY"], r["F_TOTAL"])
