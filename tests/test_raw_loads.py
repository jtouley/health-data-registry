"""Raw is append-only, each load carries its fetch metadata, and each stored source declares its load mode."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from health_data_registry import transform_gen as tg
from health_data_registry.registry import RegistryError, load_registry
from health_data_registry.store import RawFetch, build_snapshot, keep_raw_records, load_metadata, write_ndjson
from tests.conftest import CONTRACT, RECORDS, raw_fetch, write_registry

REPO = Path(__file__).resolve().parents[1]


def with_load(value: str) -> str:
    return CONTRACT.replace(
        "  - {property: unreliableRule,",
        f"  - {{property: rawLoad, value: {value}}}\n  - {{property: unreliableRule,",
        1,
    )


def test_every_stored_source_declares_its_load_mode() -> None:
    reg = load_registry(REPO)
    for ds in tg.stored(reg):
        assert ds.contract.raw_load.declared, f"{ds.id}: declare rawLoad (snapshot or incremental)"
        assert ds.contract.raw_load.mode in ("snapshot", "incremental")
    doc = yaml.safe_load(tg.raw_sources(reg))
    assert {t["meta"]["load"] for t in doc["sources"][0]["tables"]} == {"snapshot"}


def test_raw_load_is_parsed_and_checked(tmp_path: Path) -> None:
    plain = load_registry(write_registry(tmp_path / "a")).get("toy_cases").contract.raw_load
    assert (plain.mode, plain.declared) == ("snapshot", False)
    snap = load_registry(write_registry(tmp_path / "b", contract=with_load("{mode: snapshot}"))).get(
        "toy_cases"
    )
    assert (snap.contract.raw_load.mode, snap.contract.raw_load.declared) == ("snapshot", True)
    bad = {
        "{mode: weekly}": "rawLoad needs mode: snapshot or incremental",
        "{mode: snapshot, key: [year]}": "a snapshot load takes no key or updatedField",
        "{mode: incremental, key: [year]}": "an incremental load needs key",
        "{mode: incremental, key: [nope], updatedField: ':updated_at'}": "rawLoad key names unknown column nope",
        "{mode: incremental, key: [reporting_area, cases], updatedField: ':updated_at'}": (
            "rawLoad key column cases must be required"
        ),
        # Staging keeps only the newest load, so a declared incremental load would serve only the last delta.
        "{mode: incremental, key: [reporting_area, year, condition], updatedField: ':updated_at'}": (
            "rawLoad mode incremental is declared but not supported yet"
        ),
    }
    for i, (value, message) in enumerate(bad.items()):
        with pytest.raises(RegistryError, match=message):
            load_registry(write_registry(tmp_path / f"bad{i}", contract=with_load(value)))


def test_each_full_load_keeps_its_fetch_metadata_once(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    lake.mkdir()
    fetch = raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)
    m = build_snapshot(
        lake, ds, [fetch], [page], robots_overrides=["https://data.cdc.gov/x", "https://data.cdc.gov/x"]
    )
    meta = json.loads((lake / "raw_records" / "toy_cases" / f"{m.snapshot_id}.meta.json").read_text())
    assert meta == {
        "dataset": "toy_cases",
        "snapshot_id": m.snapshot_id,
        "load": "snapshot",
        "raw_contract_version": "0.1.0",
        "fetches": [
            {"url": "https://data.cdc.gov/resource/a.json", "route": "direct", "capture_url": None,
             "retrieved_at": "2026-11-24T12:00:00+00:00", "sha256": fetch.sha256}
        ],
        "robots_overrides": ["https://data.cdc.gov/x"],
    }  # fmt: skip
    # Append-only: the same snapshot id never rewrites its records or metadata.
    before = sorted(p.read_bytes() for p in (lake / "raw_records" / "toy_cases").iterdir())
    keep_raw_records(lake, ds, m.snapshot_id, [page], {"changed": True})
    assert sorted(p.read_bytes() for p in (lake / "raw_records" / "toy_cases").iterdir()) == before
    assert not list((lake / "raw_records" / "toy_cases").glob(".*.tmp"))


def test_metadata_is_written_after_its_records_and_filled_in_for_an_older_load(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    lake.mkdir()
    fetch = raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)
    m = build_snapshot(lake, ds, [fetch], [page])
    records = lake / "raw_records" / "toy_cases"
    meta = records / f"{m.snapshot_id}.meta.json"
    meta.unlink()  # a load kept before metadata was recorded
    again = build_snapshot(lake, ds, [fetch], [page])  # the same inputs: the snapshot is not rebuilt
    assert again.snapshot_id == m.snapshot_id and json.loads(meta.read_text())["snapshot_id"] == m.snapshot_id
    # A records file that can't be read leaves no metadata behind.
    with pytest.raises(OSError):
        keep_raw_records(lake, ds, "other", [tmp_path / "missing.ndjson"], {"x": 1})
    assert not (records / "other.meta.json").exists() and not (records / "other.ndjson").exists()


def test_refresh_keeps_the_browser_capture_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry import adapters, crawl

    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    page_fetch = replace(
        raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS), capture_url="https://data.cdc.gov/capture"
    )

    page = adapters.Page(fetch=page_fetch, records=RECORDS)
    monkeypatch.setattr(adapters, "pages", lambda *a, **k: iter([page]))
    m = crawl.refresh_dataset(ds, tmp_path / "lake", lambda url: page_fetch)
    meta = json.loads(
        (tmp_path / "lake" / "raw_records" / "toy_cases" / f"{m.snapshot_id}.meta.json").read_text()
    )
    assert [(f["route"], f["capture_url"]) for f in meta["fetches"]] == [
        ("browser", "https://data.cdc.gov/capture")
    ]


def test_a_browser_capture_is_recorded_as_such(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    f = RawFetch(url="https://example.gov/view", content=b"", sha256="0" * 64,
                 retrieved_at=datetime(2026, 11, 25, tzinfo=UTC), capture_url="https://example.gov/data")  # fmt: skip
    (fetch,) = load_metadata(ds, "s", [f])["fetches"]
    assert (fetch["route"], fetch["capture_url"]) == ("browser", "https://example.gov/data")
