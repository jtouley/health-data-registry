"""The engine's provenance words, its live read's fetcher and refusal, its reconnect, and label helpers (issue #213).

Each pins a detail the nightly mutation run showed nothing else would notice: a hub link that keeps its pin, a live
read that loses its shared access policy, an old connection left open, a renamed label shown under a new name.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from health_data_registry import crawl, live
from health_data_registry.hub import Hub
from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Column, Dataset, Registry, load_registry
from health_data_registry.store import Manifest
from tests.test_publish import write_hub_registry


@pytest.fixture
def hreg(tmp_path: Path) -> Registry:
    return load_registry(write_hub_registry(tmp_path / "hreg"))


def _manifest(ds: Dataset, origin: str, release_ref: str | None = None) -> Manifest:
    return Manifest(
        dataset=ds.id, snapshot_id="20261124T000000Z-000000000000", contract_version=ds.contract.version,
        semantic_version=ds.semantic.version, catalog_status=ds.entry.status,
        retrieved_at="2026-11-24T08:30:59+00:00", source_urls=["https://data.cdc.gov/resource/a.json"],
        raw_sha256=["0" * 64], row_count=12, licence=ds.entry.licence.id, redistribution=True, partial=False,
        origin=origin, release_id="2026-11-24", release_ref=release_ref,
    )  # fmt: skip


def _engine(reg: Registry, tmp_path: Path, hub: Hub | None, m: Manifest, mp: pytest.MonkeyPatch) -> Engine:
    eng = Engine(registry=reg, lake=tmp_path / "lake", hub=hub, fetch_at_run_time=False)
    mp.setattr(eng, "connect", lambda: None)  # the manifest below is what it read
    eng.manifests[m.dataset] = m
    return eng


# ------------------------------------------------------------------------------------------ provenance


def test_provenance_links_the_hub_without_its_pin_and_pins_the_release_read(
    hreg: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = hreg.get("toy_cases")
    hub = Hub(url="hf://datasets/org/hub@release-2026-11-24", cache=tmp_path / "c")
    got = _engine(hreg, tmp_path, hub, _manifest(ds, "hub", "release-2026-11-24"), monkeypatch).provenance(
        ds.id
    )
    text = "\n".join(got["add_to_claude"])
    assert "It reads this dataset from hf://datasets/org/hub@release-2026-11-24," in text
    local = _engine(hreg, tmp_path, None, _manifest(ds, "hub", "release-2026-11-24"), monkeypatch).provenance(
        ds.id
    )
    assert "It reads this dataset from the open data hub," in "\n".join(local["add_to_claude"])


def test_provenance_names_a_live_read_to_the_minute_and_any_other_origin_as_is(
    hreg: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = hreg.get("toy_cases")
    got = _engine(hreg, tmp_path, None, _manifest(ds, "publisher"), monkeypatch).provenance(ds.id)
    assert got["data_from"] == f"{ds.entry.publisher}, read live at 2026-11-24T08:30 UTC and not stored"
    other = _engine(hreg, tmp_path, None, _manifest(ds, "a mirror"), monkeypatch).provenance(ds.id)
    assert other["data_from"] == "a mirror"


# ------------------------------------------------------------------------------------------ live reads


def test_a_live_read_uses_the_shared_access_policy_and_refuses_plainly(
    hreg: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = hreg.get("toy_listed")
    seen: list[tuple[Any, ...]] = []
    policy = object()

    def shared_robots(registry: Any) -> Any:
        seen.append(("robots", registry))
        return policy

    monkeypatch.setattr(crawl, "shared_robots", shared_robots)

    def fetcher(registry: Any, log: Any, *, robots: Any) -> Any:
        seen.append(("fetcher", registry, log, robots))
        return "a getter"

    def fetch_live(d: Dataset, get: Any) -> Any:
        seen.append(("fetch_live", d.id, get))
        raise OSError("offline")

    monkeypatch.setattr(crawl, "fetcher", fetcher)
    monkeypatch.setattr(live, "fetch_live", fetch_live)
    eng = Engine(registry=hreg, lake=tmp_path / "lake", hub=None, fetch_at_run_time=True)
    with pytest.raises(QueryRefused) as e:
        eng._obtain(ds)
    assert seen == [("robots", hreg), ("fetcher", hreg, None, policy), ("fetch_live", ds.id, "a getter")]
    assert e.value.code == "publisher_unreachable"
    assert str(e.value) == (
        f"{ds.entry.publisher} could not be reached for {ds.entry.title}, which is read live from the publisher "
        "because its terms don't allow us to keep a copy. Try again in a few minutes."
    )


def test_a_reconnect_closes_the_old_connection(tmp_path: Path, registry: Registry, lake: Path) -> None:
    eng = Engine(registry=registry, lake=lake, hub=None)
    eng.connect()
    old = eng._con
    assert old is not None
    eng._reconnect()
    assert eng._con is not None and eng._con is not old
    with pytest.raises(Exception, match="closed"):
        old.execute("SELECT 1")
    assert eng._tables() == {"toy_cases": registry.get("toy_cases").contract.column_names}


# ------------------------------------------------------------------------------------------ labels


def test_renamed_labels_continue_and_show_as_the_earlier_name() -> None:
    col = cast(Column, SimpleNamespace(renamed_labels=[("Old Area", "New Area")]))
    assert Column.canonical_label(col, " old area. ") == "NEW AREA"
    assert Column.canonical_label(col, "Elsewhere") == "ELSEWHERE"
    assert Column.display_label(col, "NEW AREA") == "New Area"
    assert Column.display_label(col, "old area") == "New Area"
    assert Column.display_label(col, " Elsewhere ") == " Elsewhere "  # any other label: as published


def test_an_uncatalogued_label_is_refused_by_name(registry: Registry) -> None:
    with pytest.raises(KeyError) as e:
        registry.get("toy_cases").condition_series("Not a condition")
    assert e.value.args == ("Not a condition",)
