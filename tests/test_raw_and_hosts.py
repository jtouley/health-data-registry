"""Raw bytes land before anything is normalised, a hash that disagrees fails loudly, and the WHO host is pinned."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from health_data_registry.crawl import refresh_dataset
from health_data_registry.fetch import FetchRefused, validate_url
from health_data_registry.registry import load_registry
from health_data_registry.store import RawFetch, write_raw
from tests.conftest import RECORDS, raw_fetch, write_registry

ROOT = Path(__file__).resolve().parents[1]


def test_raw_bytes_are_kept_even_when_normalising_fails(tmp_path: Path) -> None:
    reg = load_registry(write_registry(tmp_path / "reg"))
    bad = [{**RECORDS[0], "year": "not a year"}]
    page = raw_fetch("https://data.cdc.gov/resource/x.json?$offset=0", bad)
    lake = tmp_path / "lake"
    with pytest.raises(Exception):  # noqa: B017 - DuckDB's conversion error; the point is what stays on disk
        refresh_dataset(reg.get("toy_cases"), lake, lambda url: page, max_rows=1)
    assert (lake / "raw" / page.sha256).read_bytes() == page.content
    assert not list((lake / "datasets").glob("*/run_date=*/*/manifest.json"))  # no snapshot from it


def test_bytes_that_disagree_with_their_hash_fail_loudly(tmp_path: Path) -> None:
    good = raw_fetch("https://data.cdc.gov/x", RECORDS)
    forged = RawFetch(
        url=good.url, content=b"other bytes", sha256=good.sha256, retrieved_at=good.retrieved_at
    )
    with pytest.raises(ValueError, match="sha256 mismatch for https://data.cdc.gov/x"):
        write_raw(tmp_path, [forged])
    assert not (tmp_path / "raw" / good.sha256).exists()
    assert hashlib.sha256(good.content).hexdigest() == good.sha256


def test_who_host_is_pinned_exactly() -> None:
    reg = load_registry(ROOT)
    assert "ghoapi.azureedge.net" in reg.hosts
    assert not any("*" in h for h in reg.hosts)
    validate_url("https://ghoapi.azureedge.net/api/MDG_0000000020", reg.hosts)
    # Every redirect hop goes through validate_url, so another Azure host is refused like any other.
    for other in ("https://evil.azureedge.net/api", "https://ghoapi.azureedge.net.evil.example/api",
                  "http://ghoapi.azureedge.net/api", "https://ghoapi.azureedge.net:8443/api"):  # fmt: skip
        with pytest.raises(FetchRefused):
            validate_url(other, reg.hosts)
