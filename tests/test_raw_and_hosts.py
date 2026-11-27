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


def _corrupt(path: Path) -> None:
    """Cut a stored raw object short, as a disk error or a hand edit would."""
    path.write_bytes(path.read_bytes()[:-5])


def test_a_reused_raw_object_is_re_hashed_and_a_corrupt_one_refused(tmp_path: Path) -> None:
    """Issue #269: an existing raw/<sha256> is never trusted for its name, whether the reuse hands over the bytes
    again or (a streamed page) none."""
    from health_data_registry.store import RawObjectCorrupt, raw_problems, verify_raw

    good = raw_fetch("https://data.cdc.gov/x", RECORDS)
    write_raw(tmp_path, [good])
    stored = tmp_path / "raw" / good.sha256
    write_raw(tmp_path, [good, RawFetch(good.url, b"", good.sha256, good.retrieved_at)])  # intact: reused
    assert raw_problems(tmp_path, [good.sha256]) == []
    _corrupt(stored)
    for again in (good, RawFetch(good.url, b"", good.sha256, good.retrieved_at)):
        with pytest.raises(
            RawObjectCorrupt, match=f"raw/{good.sha256} no longer holds the bytes it is named"
        ):
            write_raw(tmp_path, [again])
    assert stored.read_bytes() != good.content  # refused, never silently rewritten
    with pytest.raises(RawObjectCorrupt, match="Restore it from the private raw archive"):
        verify_raw(tmp_path, [good.sha256])
    stored.unlink()
    with pytest.raises(RawObjectCorrupt, match="is missing from the lake"):  # streamed earlier, gone since
        write_raw(tmp_path, [RawFetch(good.url, b"", good.sha256, good.retrieved_at)])
    empty = RawFetch(good.url, b"", hashlib.sha256(b"").hexdigest(), good.retrieved_at)
    write_raw(tmp_path, [empty])  # a publisher's genuinely empty response is still kept
    assert (tmp_path / "raw" / empty.sha256).read_bytes() == b""
    write_raw(tmp_path, [good])  # the missing object is written again from fresh bytes
    assert stored.read_bytes() == good.content
    assert not list((tmp_path / "raw").glob("*.tmp")) and not list((tmp_path / "raw").glob(".*"))


def test_refresh_refuses_a_corrupt_raw_object_in_plain_words(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refetching the same bytes reuses raw/<sha256>; when that stored copy no longer matches, the refresh refuses
    and LATEST keeps naming the snapshot before it."""
    from health_data_registry import cli
    from health_data_registry.store import RawObjectCorrupt, latest_snapshot

    reg = load_registry(write_registry(tmp_path / "reg"))
    page = raw_fetch("https://data.cdc.gov/resource/x.json?$offset=0", RECORDS)
    lake = tmp_path / "lake"
    first = refresh_dataset(reg.get("toy_cases"), lake, lambda url: page)
    _corrupt(lake / "raw" / page.sha256)
    with pytest.raises(RawObjectCorrupt, match="no longer holds the bytes it is named for"):
        refresh_dataset(reg.get("toy_cases"), lake, lambda url: page)
    found = latest_snapshot(lake, "toy_cases")
    assert found is not None and found[1].snapshot_id == first.snapshot_id
    # Through the command a clinician-maintainer runs: a plain refusal with a next step, no stack trace.
    monkeypatch.setattr(cli, "_fetcher", lambda registry, log: lambda url: page)
    assert cli.refresh(reg, ["toy_cases"], max_rows=None, lake=lake) == 1
    err = capsys.readouterr().err
    assert err.startswith("FAIL toy_cases: the raw file raw/") and "Traceback" not in err
    assert "Restore it from the private raw archive, or delete it and refresh the source again" in err
