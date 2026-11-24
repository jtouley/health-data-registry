"""The hosted read path: hub releases (a local directory stands in for the Hub), run-time fetches, the lake layout."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import crawl, hub
from health_data_registry.hub import Hub, HubError
from health_data_registry.publish import export_public
from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Registry, load_registry
from health_data_registry.sql_guard import SqlRefused
from health_data_registry.store import (
    FetchLog,
    build_snapshot,
    latest_snapshot,
    read_fetch_log,
    run_date_of,
    snapshot_dir,
    write_ndjson,
)
from tests.conftest import RECORDS, raw_fetch
from tests.test_publish import LISTED_TERMS, RUN, make_lake, write_hub_registry


@pytest.fixture
def reg(tmp_path: Path) -> Registry:
    return load_registry(write_hub_registry(tmp_path / "reg"))


@pytest.fixture
def released(tmp_path: Path, reg: Registry) -> Path:
    """A published release in a local directory, standing in for hf://datasets/<org>/<name>."""
    lake = make_lake(tmp_path / "maint-lake", reg, tmp_path)
    out = tmp_path / "hub-release"
    export_public(reg, lake, str(out), run_date=RUN)
    return out


def engine(reg: Registry, tmp_path: Path, hub_dir: Path | None, **kw: Any) -> Engine:
    h = Hub(url=str(hub_dir), cache=tmp_path / "cache") if hub_dir else None
    return Engine(registry=reg, lake=tmp_path / "no-lake", hub=h, cache=tmp_path / "cache", **kw)


def test_engine_reads_a_hub_release(tmp_path: Path, reg: Registry, released: Path) -> None:
    eng = engine(reg, tmp_path, released)
    assert "toy_cases" not in eng.manifests  # nothing downloaded until a question needs it
    assert eng.provenance("toy_cases")["data_from"].startswith("not loaded yet: the open data hub")
    out = eng.query_metric(
        "toy_cases", "case_count", filters={"condition": "Legionellosis"}, group_by=["year"]
    )
    assert dict((r[0], r[1]) for r in out["rows"]) == {2022: 300, 2023: 710, 2024: None}
    m = eng.manifest("toy_cases")
    assert m.origin == "hub" and m.release_id and m.release_id.startswith(RUN) and m.run_date == RUN
    rel = json.loads((released / "manifest.json").read_text())
    assert (
        m.release_files["toy_cases/data/toy_cases.parquet"]
        == rel["files"]["toy_cases/data/toy_cases.parquet"]["sha256"]
    )
    assert out["source"]["data_from"] == "hub" and out["source"]["release_id"] == m.release_id
    assert out["source"]["release_contract_version"] == "0.1.0"
    p = eng.provenance("toy_cases")
    assert p["data_from"] == f"the open data hub, release release-{RUN} ({m.release_id})"
    assert m.release_ref == f"release-{RUN}" and p["release_ref"] == f"release-{RUN}"
    assert out["source"]["release_ref"] == f"release-{RUN}"
    assert p["data_product"] == "toy-hub" and p["output_port"] == "toy_cases"
    assert p["contract_version"] == "0.1.0" and p["release_contract_version"] == "0.1.0"
    assert p["run_date"] == RUN and p["release_files"] == m.release_files
    # Only the Parquet the question needed is in the cache.
    cached = sorted(x.name for x in (tmp_path / "cache").rglob("*.parquet"))
    assert cached == ["toy_cases.parquet"]


def test_hub_read_stays_hardened(tmp_path: Path, reg: Registry, released: Path) -> None:
    eng = engine(reg, tmp_path, released)
    eng.manifest("toy_cases")
    con = eng.connect()
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n")
    for sql in (
        f"SELECT * FROM read_csv('{secret}')",
        "SELECT * FROM read_text('/etc/passwd')",
        "SELECT * FROM read_parquet('https://example.org/x.parquet')",
        f"SELECT * FROM read_parquet('{released / 'toy_cases' / 'data' / 'toy_cases.parquet'}')",
    ):
        with pytest.raises(duckdb.Error):
            con.execute(sql).fetchall()
    with pytest.raises(duckdb.Error):
        con.execute("SET enable_external_access = true")
    with pytest.raises(SqlRefused):
        eng.query_sql(f"SELECT * FROM read_csv('{secret}')")


def test_query_sql_downloads_on_first_use(tmp_path: Path, reg: Registry, released: Path) -> None:
    eng = engine(reg, tmp_path, released)
    out = eng.query_sql("SELECT count(*) AS n FROM toy_cases")
    assert out["rows"] == [[len(RECORDS)]] and out["source"]["data_from"] == "hub"


def test_hash_mismatch_is_refused(tmp_path: Path, reg: Registry, released: Path) -> None:
    rel = json.loads((released / "manifest.json").read_text())
    rel["files"]["toy_cases/data/toy_cases.parquet"]["sha256"] = "0" * 64
    (released / "manifest.json").write_text(json.dumps(rel))
    eng = engine(reg, tmp_path, released)
    with pytest.raises(QueryRefused) as e:
        eng.manifest("toy_cases")
    assert e.value.code == "hub_hash_mismatch" and "fingerprint" in str(e.value)
    assert not list((tmp_path / "cache").rglob("*.parquet"))  # the bad copy is not kept


def test_contract_version_mismatch_fails_closed(tmp_path: Path, reg: Registry, released: Path) -> None:
    rel = json.loads((released / "manifest.json").read_text())
    rel["sources"]["toy_cases"]["contract"]["version"] = "9.9.9"
    (released / "manifest.json").write_text(json.dumps(rel))
    eng = engine(reg, tmp_path, released)
    with pytest.raises(QueryRefused) as e:
        eng.query_metric("toy_cases", "case_count", filters={"condition": "Syphilis"})
    assert e.value.code == "contract_mismatch"
    assert "version 9.9.9 of the toy_cases contract" in str(e.value) and "reads version 0.1.0" in str(e.value)
    assert "No numbers were read" in str(e.value)
    assert not list((tmp_path / "cache").rglob("*.parquet"))  # nothing was even downloaded
    # Only a reviewed entry in the compatibility table lets a different version through.
    hub.COMPATIBLE_CONTRACTS["toy_cases"] = {"9.9.9": {"0.1.0"}}
    try:
        assert engine(reg, tmp_path, released).manifest("toy_cases").release_contract_version == "9.9.9"
    finally:
        hub.COMPATIBLE_CONTRACTS.clear()
    assert hub.contract_compatible("x", "1.0.0", "1.0.0")
    assert not hub.contract_compatible("x", "1.0.0", "1.1.0")


def test_only_immutable_releases_are_read(tmp_path: Path) -> None:
    for rev in ("main", "latest", "refs/pr/1", "release-2026-11", "abc123"):
        with pytest.raises(HubError) as e:
            Hub(url=f"hf://datasets/org/hub@{rev}", cache=tmp_path).revision()
        assert e.value.code == "not_reproducible" and "could not be reproduced" in str(e.value)
    for rev in (f"release-{RUN}", f"release-{RUN}.2", "a" * 40):
        assert Hub(url=f"hf://datasets/org/hub@{rev}", cache=tmp_path).revision() == rev
    assert hub.is_immutable_ref(f"release-{RUN}.3") and not hub.is_immutable_ref("main")


def test_unpinned_hub_resolves_newest_release(tmp_path: Path) -> None:
    from types import SimpleNamespace

    class Api:
        def __init__(self, tags: list[str]) -> None:
            self.tags = tags

        def list_repo_refs(self, repo: str, **kw: Any) -> Any:
            assert repo == "org/hub" and kw == {"repo_type": "dataset"}
            return SimpleNamespace(tags=[SimpleNamespace(name=t) for t in self.tags])

    tags = ["release-2026-11-17", f"release-{RUN}", f"release-{RUN}.2", "release-2026-11-10.5", "v1", "main"]
    h = Hub(url="hf://datasets/org/hub", cache=tmp_path, api=Api(tags))
    assert h.revision() == f"release-{RUN}.2"
    assert h._cache_dir() == tmp_path / "hub" / "org--hub" / f"release-{RUN}.2"
    with pytest.raises(HubError) as e:
        Hub(url="hf://datasets/org/hub", cache=tmp_path, api=Api(["main", "v1"])).revision()
    assert e.value.code == "no_release"
    assert str(e.value).startswith("The open data hub (hf://datasets/org/hub) has no published release yet")
    assert "Nothing was guessed" in str(e.value)

    class Down:
        def list_repo_refs(self, *a: Any, **kw: Any) -> Any:
            raise OSError("offline")

    with pytest.raises(HubError) as e:
        Hub(url="hf://datasets/org/hub", cache=tmp_path, api=Down()).revision()
    assert e.value.code == "hub_unreachable"
    assert hub.newest_release([f"release-{RUN}.10", f"release-{RUN}.9"]) == f"release-{RUN}.10"
    assert hub.newest_release([]) is None


def test_local_hub_copy_must_be_a_tagged_release(tmp_path: Path, reg: Registry, released: Path) -> None:
    rel = json.loads((released / "manifest.json").read_text())
    (released / "manifest.json").write_text(json.dumps(rel | {"tag": "main"}))
    with pytest.raises(HubError) as e:
        Hub(url=str(released), cache=tmp_path / "c").snapshot(reg.get("toy_cases"))
    assert e.value.code == "not_a_release"


def test_cache_is_bounded(tmp_path: Path, reg: Registry, released: Path) -> None:
    cache = tmp_path / "c"
    stale = cache / "hub" / "old" / "big.parquet"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x" * 10_000)
    import os

    os.utime(stale, (1, 1))
    h = Hub(url=str(released), cache=cache, max_bytes=8_000)
    local, _ = h.snapshot(reg.get("toy_cases"))
    assert local.is_file() and not stale.exists()
    total = sum(p.stat().st_size for p in (cache / "hub").rglob("*") if p.is_file())
    assert total <= 8_000 or [p for p in (cache / "hub").rglob("*") if p.is_file()] == [local]
    assert hub.evict(tmp_path / "missing", 0, set()) == []
    keep = tmp_path / "k"
    keep.write_bytes(b"y" * 100)
    assert hub.evict(tmp_path, 0, {keep.resolve()}) and keep.exists()


def test_cache_limit_from_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("HDR_CACHE_MAX_GB", raising=False)
    assert hub.cache_max_bytes() == 5 * 1024**3
    monkeypatch.setenv("HDR_CACHE_MAX_GB", "0.5")
    assert hub.cache_max_bytes() == 512 * 1024**2


def test_hub_errors_in_plain_words(tmp_path: Path, reg: Registry, released: Path) -> None:
    h = Hub(url=str(released), cache=tmp_path / "c")
    with pytest.raises(HubError) as e:
        h.snapshot(reg.get("toy_listed"))
    assert e.value.code == "not_on_hub"
    with pytest.raises(HubError) as e:
        h.fetch("nope.json")
    assert e.value.code == "hub_missing_file"
    rel = json.loads((released / "manifest.json").read_text())
    del rel["files"]["toy_cases/data/toy_cases.parquet"]
    (released / "manifest.json").write_text(json.dumps(rel))
    with pytest.raises(HubError) as e:
        Hub(url=str(released), cache=tmp_path / "c2").snapshot(reg.get("toy_cases"))
    assert e.value.code == "hub_bad_manifest"
    (released / "manifest.json").write_text("{not json")
    with pytest.raises(HubError) as e:
        Hub(url=str(released), cache=tmp_path / "c3").release()
    assert e.value.code == "hub_bad_manifest"


def test_remote_hub_uses_hf_download(tmp_path: Path, reg: Registry, released: Path, monkeypatch: Any) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_download(repo: str, filename: str, **kw: Any) -> str:
        calls.append((repo, filename, kw))
        target = Path(kw["local_dir"]) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(released / filename, target)
        return str(target)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    h = Hub(url=f"hf://datasets/org/hub@release-{RUN}", cache=tmp_path / "c")
    eng = Engine(registry=reg, lake=tmp_path / "none", hub=h, cache=tmp_path / "c")
    assert eng.manifest("toy_cases").origin == "hub"
    assert [c[1] for c in calls] == ["manifest.json", "toy_cases/data/toy_cases.parquet"]
    assert calls[0][0] == "org/hub" and calls[0][2]["revision"] == f"release-{RUN}"
    assert calls[0][2]["repo_type"] == "dataset" and calls[0][2]["token"] is False
    assert Path(calls[0][2]["local_dir"]) == tmp_path / "c" / "hub" / "org--hub" / f"release-{RUN}"
    assert eng.manifest("toy_cases").release_ref == f"release-{RUN}"

    def broken(*a: Any, **kw: Any) -> str:
        raise OSError("offline")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", broken)
    with pytest.raises(HubError) as e:
        Hub(url=f"hf://datasets/org/other@release-{RUN}", cache=tmp_path / "d").release()
    assert e.value.code == "hub_unreachable" and "could not be reached" in str(e.value)


def test_no_hub_configured_keeps_the_old_refusal(tmp_path: Path, reg: Registry) -> None:
    eng = engine(reg, tmp_path, None)
    with pytest.raises(QueryRefused) as e:
        eng.manifest("toy_cases")
    assert e.value.code == "no_snapshot"
    assert (
        eng.provenance("toy_cases")["data_from"] == "not loaded yet: no open data hub is configured (HDR_HUB)"
    )
    assert (
        eng.provenance("toy_listed")["data_from"]
        == "not available: reading this publisher live is turned off"
    )


def test_local_lake_wins_over_hub(tmp_path: Path, reg: Registry, released: Path) -> None:
    lake = make_lake(tmp_path / "lake", reg, tmp_path)
    eng = Engine(
        registry=reg, lake=lake, hub=Hub(url=str(released), cache=tmp_path / "c"), cache=tmp_path / "c"
    )
    assert eng.manifest("toy_cases").origin == "lake"
    assert eng.provenance("toy_cases")["data_from"] == "the local lake (a maintainer's copy)"


def test_publisher_failure_is_a_plain_refusal(tmp_path: Path, reg: Registry) -> None:
    def down(url: str) -> Any:
        raise RuntimeError("HTTP 503")

    eng = engine(reg, tmp_path, None, fetch_at_run_time=True, getter=down)
    with pytest.raises(QueryRefused) as e:
        eng.manifest("toy_listed")
    assert e.value.code == "publisher_unreachable" and "503" not in str(e.value)


def test_env_defaults(tmp_path: Path, monkeypatch: Any, reg: Registry) -> None:
    assert hub.hub_from_env() is None  # HDR_HUB=off
    monkeypatch.delenv("HDR_HUB")
    assert hub.hub_from_env() == hub.DEFAULT_HUB == "hf://datasets/jtouley/health-data-registry"
    monkeypatch.setenv("HDR_HUB", " ")
    assert hub.hub_from_env() == hub.DEFAULT_HUB
    monkeypatch.setenv("HDR_HUB", str(tmp_path / "h"))
    eng = Engine(registry=reg, lake=tmp_path / "l")
    assert eng.hub is not None and eng.hub.url == str(tmp_path / "h")
    assert eng.cache == tmp_path / "cache" and eng.fetch_at_run_time is False
    monkeypatch.setenv("HDR_FETCH_AT_RUN_TIME", "1")
    monkeypatch.delenv("HDR_CACHE")
    eng = Engine(registry=reg, lake=tmp_path / "l")
    assert eng.fetch_at_run_time is True
    assert eng.cache == Path.home() / ".cache" / "health-data-registry"


# ------------------------------------------------------------------------------------------- lake layout


def test_lake_is_partitioned_by_source_and_run_date(tmp_path: Path, reg: Registry) -> None:
    lake = make_lake(tmp_path / "lake", reg, tmp_path)
    found = latest_snapshot(lake, "toy_cases")
    assert found is not None
    path, m = found
    assert path == lake / "datasets" / "toy_cases" / "run_date=2026-11-24" / m.snapshot_id
    assert run_date_of(m.snapshot_id) == "2026-11-24"


def test_snapshots_are_never_overwritten(tmp_path: Path, reg: Registry) -> None:
    lake = make_lake(tmp_path / "lake", reg, tmp_path)
    path, m = latest_snapshot(lake, "toy_cases") or (tmp_path, None)
    assert m is not None
    marker = path / "data.parquet"
    before = marker.stat().st_mtime_ns
    m2 = make_lake(lake, reg, tmp_path) and latest_snapshot(lake, "toy_cases")
    assert m2 and m2[1].snapshot_id == m.snapshot_id and marker.stat().st_mtime_ns == before
    # A different contract version is a different snapshot, never a rewrite.
    reg2 = load_registry(write_hub_registry(tmp_path / "reg2"))
    ds = reg2.get("toy_cases")
    object.__setattr__(ds.contract, "version", "0.2.0")
    f = tmp_path / "p.ndjson"
    write_ndjson(f, RECORDS)
    m3 = build_snapshot(lake, ds, [raw_fetch("https://data.cdc.gov/resource/toy0-toy0.json", RECORDS)], [f])
    assert m3.snapshot_id != m.snapshot_id and marker.stat().st_mtime_ns == before
    assert (lake / "datasets" / "toy_cases" / "LATEST").read_text().strip() == m3.snapshot_id


def test_old_unpartitioned_snapshots_still_read(tmp_path: Path, reg: Registry) -> None:
    lake = make_lake(tmp_path / "lake", reg, tmp_path)
    path, m = latest_snapshot(lake, "toy_cases") or (tmp_path, None)
    assert m is not None
    old = lake / "datasets" / "toy_cases" / m.snapshot_id
    shutil.move(str(path), str(old))
    assert snapshot_dir(lake, "toy_cases", m.snapshot_id) == old
    eng = Engine(registry=reg, lake=lake, hub=None)
    assert eng.manifest("toy_cases").snapshot_id == m.snapshot_id


def test_fetch_log_has_one_row_per_attempt(tmp_path: Path, reg: Registry, monkeypatch: Any) -> None:
    from health_data_registry import fetch as fetch_mod

    body = json.dumps(RECORDS).encode()
    answers: list[Any] = [
        fetch_mod.FetchRefused("timeout", "the publisher took too long to answer"),
        fetch_mod.FetchResult(
            url="https://data.cdc.gov/x",
            final_url="https://data.cdc.gov/x",
            status=200,
            content=body,
            content_type="application/json",
            sha256=hashlib.sha256(body).hexdigest(),
            retrieved_at=datetime(2026, 11, 24, tzinfo=UTC),
        ),
        fetch_mod.FetchRefused("host_not_allowed", "that host is not a catalogued publisher"),
    ]

    def fake_fetch(url: str, **kw: Any) -> Any:
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(fetch_mod, "fetch", fake_fetch)
    lake = tmp_path / "lake"
    get = crawl.fetcher(reg, FetchLog(lake, "toy_cases"), sleep=lambda s: None)
    got = get("https://data.cdc.gov/x")
    assert got.sha256 == hashlib.sha256(body).hexdigest()
    with pytest.raises(fetch_mod.FetchRefused):
        get("https://evil.example/x")
    rows = read_fetch_log(lake, "toy_cases")
    assert [r["status"] for r in rows] == ["error", "ok", "error"]
    assert rows[0]["error"] == "the publisher took too long to answer" and rows[0]["bytes"] is None
    assert rows[1]["bytes"] == len(body) and rows[1]["sha256"] == got.sha256 and rows[1]["http_status"] == 200
    assert rows[1]["error"] is None and rows[1]["url"] == "https://data.cdc.gov/x"
    assert rows[2]["url"] == "https://evil.example/x" and rows[2]["source"] == "toy_cases"
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    assert (lake / "fetch_log" / "source=toy_cases" / f"run_date={day}.jsonl").is_file()


def test_fetch_log_records_http_errors(tmp_path: Path, reg: Registry, monkeypatch: Any) -> None:
    from health_data_registry import fetch as fetch_mod

    class R:
        status = 503
        content = b"busy"
        sha256 = "x"

    monkeypatch.setattr(fetch_mod, "fetch", lambda url, **kw: R())
    get = crawl.fetcher(reg, FetchLog(tmp_path, "toy_cases"), sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="status 503"):
        get("https://data.cdc.gov/x")
    (row,) = read_fetch_log(tmp_path, "toy_cases")
    assert row["status"] == "error" and row["http_status"] == 503 and row["bytes"] == 4
    assert row["error"] == "the publisher answered with status 503 instead of the data"


def test_fetch_log_default_error_text(tmp_path: Path) -> None:
    row = FetchLog(tmp_path, "s").record("https://x", ok=False, when=datetime(2026, 1, 2, tzinfo=UTC))
    assert row["error"] == "the fetch failed for an unknown reason" and row["attempted_at"].startswith(
        "2026-01-02"
    )
    assert read_fetch_log(tmp_path, "other") == []


def test_tools_surface_release_and_contract(tmp_path: Path, reg: Registry, released: Path) -> None:
    from health_data_registry import tools

    eng = engine(reg, tmp_path, released)
    listed = tools.list_sources(reg, eng, fmt="json")
    assert isinstance(listed, dict)
    cols = listed["columns"]
    row = next(r for r in listed["rows"] if r[0] == "toy_cases")
    assert row[cols.index("contract_version")] == "0.1.0"
    assert row[cols.index("data_from")].startswith("not loaded yet: the open data hub")
    prov = tools.get_provenance(reg, eng, source_id="toy_cases", fmt="json")
    assert isinstance(prov, dict)
    text = " ".join(prov["notes"])
    assert "Data from: the open data hub, release " in text
    assert "data product toy-hub, port toy_cases" in text
    assert f"(run {RUN}, contract version 0.1.0)" in text and "toy_cases/data/toy_cases.parquet " in text
    desc = tools.describe_dataset(reg, "toy_cases", eng)
    assert isinstance(desc, str)
    assert "Contract: toy_cases version 0.1.0; data product toy-hub, port toy_cases" in desc
    assert "Data from: the open data hub, release " in desc and "(built with contract version 0.1.0)" in desc
    info = tools.describe_dataset(reg, "toy_cases", eng, fmt="json")
    assert isinstance(info, dict) and info["provenance"]["output_port"] == "toy_cases"
    assert tools.describe_dataset(reg, "toy_cases", fmt="json")["provenance"] is None  # type: ignore[index]


def test_fetcher_arguments_and_retries(tmp_path: Path, reg: Registry, monkeypatch: Any) -> None:
    from health_data_registry import fetch as fetch_mod

    seen: list[tuple[str, dict[str, Any]]] = []
    sleeps: list[float] = []

    def always_slow(url: str, **kw: Any) -> Any:
        seen.append((url, kw))
        raise fetch_mod.FetchRefused("connect_failed", "the publisher could not be reached")

    monkeypatch.setattr(fetch_mod, "fetch", always_slow)
    get = crawl.fetcher(reg, None, sleep=sleeps.append)
    with pytest.raises(fetch_mod.FetchRefused):
        get("https://data.cdc.gov/x")
    assert len(seen) == crawl.RETRIES == 4 and sleeps == [2, 4, 8]
    url, kw = seen[0]
    assert url == "https://data.cdc.gov/x"
    assert kw == {
        "allowed_hosts": reg.hosts,
        "headers": {"Accept": "application/json"},
        "timeout": crawl.FETCH_TIMEOUT_S,
    }
    assert crawl.FETCH_TIMEOUT_S == 180.0


def test_provenance_carries_quality_and_claude_routes(tmp_path: Path, reg: Registry, released: Path) -> None:
    rel = json.loads((released / "manifest.json").read_text())
    qual = {"tool": "dbt", "results": [{"test": "t", "checks": "c", "status": "pass", "rows_affected": 0}]}
    rel["sources"]["toy_cases"]["quality"] = qual
    (released / "manifest.json").write_text(json.dumps(rel))
    eng = engine(reg, tmp_path, released)
    assert eng.provenance("toy_cases")["quality"] == {"tool": None, "results": []}
    eng.manifest("toy_cases")
    p = eng.provenance("toy_cases")
    assert p["quality"] == qual
    assert any(line.startswith("- MCP server: add health-data-registry") for line in p["add_to_claude"])
    from health_data_registry import tools

    desc = tools.describe_dataset(reg, "toy_cases", eng)
    assert isinstance(desc, str) and "Add it to a Claude project:\n- Plugin (coming)" in desc
    prov = tools.get_provenance(reg, eng, source_id="toy_cases", fmt="json")
    assert isinstance(prov, dict) and any(
        n.startswith("Add it to a Claude project: - Plugin") for n in prov["notes"]
    )


def _files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def test_restricted_source_is_live_only_and_never_written(
    tmp_path: Path, reg: Registry, monkeypatch: Any
) -> None:
    from health_data_registry import live

    urls: list[str] = []

    def get(url: str) -> Any:
        urls.append(url)
        return raw_fetch(url, RECORDS)

    before = _files(tmp_path)
    eng = engine(reg, tmp_path, None, fetch_at_run_time=True, getter=get)
    assert eng.provenance("toy_listed")["data_from"] == (
        "Centers for Disease Control and Prevention, read live when asked (never stored)"
    )
    out = eng.query_metric("toy_listed", "case_count", filters={"condition": "Syphilis"})
    assert out["rows"][0][0] == 1000
    attribution = out["caveats"][0]
    assert attribution.startswith(
        "Data from Centers for Disease Control and Prevention, used under CC BY-NC-SA 3.0 IGO"
    )
    assert (
        LISTED_TERMS in attribution
        and "not stored" in attribution
        and "Cite as: CDC. Toy table." in attribution
    )
    assert out["method"].startswith(attribution)
    assert out["source"]["data_from"] == "publisher" and out["source"]["snapshot_id"].startswith("live-")
    sql = eng.query_sql("SELECT count(*) AS n FROM toy_listed")
    assert sql["rows"] == [[len(RECORDS)]] and sql["caveats"][0] == attribution
    assert len(urls) == 1  # one in-memory read serves the question's several lookups
    # Nothing derived from the restricted source is ever written: no lake, cache, Parquet or DuckDB file.
    assert _files(tmp_path) == before
    assert eng.provenance("toy_listed")["data_from"].endswith("UTC and not stored")
    # Past the in-memory limit, the publisher is read again.
    monkeypatch.setattr(live, "LIVE_TTL_S", -1.0)
    eng.manifest("toy_listed")
    assert len(urls) == 2 and _files(tmp_path) == before


def test_maintainer_lake_never_holds_restricted_data(tmp_path: Path, reg: Registry) -> None:
    from health_data_registry.crawl import LiveOnly, refresh_dataset

    with pytest.raises(LiveOnly, match="read live from"):
        refresh_dataset(reg.get("toy_listed"), tmp_path / "lake", lambda u: raw_fetch(u, RECORDS))
    assert not (tmp_path / "lake").exists()
    # A stray restricted snapshot in a lake is ignored by the server.
    lake = tmp_path / "lake2"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(lake, reg.get("toy_listed"), [raw_fetch("https://data.cdc.gov/x", RECORDS)], [f])
    eng = Engine(registry=reg, lake=lake, hub=None, cache=tmp_path / "c", fetch_at_run_time=False)
    with pytest.raises(QueryRefused):
        eng.manifest("toy_listed")


def test_cli_refresh_skips_restricted(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    from health_data_registry import cli

    root = write_hub_registry(tmp_path / "reg")
    monkeypatch.setenv("HDR_REGISTRY", str(root))
    monkeypatch.setenv("HDR_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(cli, "_fetcher", lambda reg, log=None: lambda url: raw_fetch(url, RECORDS))
    assert cli.main(["refresh", "--all"]) == 0
    out = capsys.readouterr().out
    assert "skip toy_listed: toy_listed is read live from" in out and "ok   toy_cases" in out
    assert not (tmp_path / "lake" / "datasets" / "toy_listed").exists()


def test_a_bad_live_table_is_refused_for_that_source_only(
    tmp_path: Path, reg: Registry, released: Path
) -> None:
    bad = [{**RECORDS[0], "year": "not a year"}]
    calls: list[str] = []

    def get(url: str) -> Any:
        calls.append(url)
        return raw_fetch(url, bad if len(calls) == 1 else RECORDS)

    eng = engine(reg, tmp_path, released, fetch_at_run_time=True, getter=get)
    with pytest.raises(QueryRefused) as e:
        eng.manifest("toy_listed")
    assert e.value.code == "publisher_bad_table" and "Other sources are not affected" in str(e.value)
    # The hub source still answers, and the next question reads the publisher afresh.
    assert eng.manifest("toy_cases").origin == "hub"
    assert eng.manifest("toy_listed").origin == "publisher" and len(calls) == 2


def test_cache_never_evicts_a_file_a_view_reads(tmp_path: Path, reg: Registry, released: Path) -> None:
    eng = engine(reg, tmp_path, released)
    eng.manifest("toy_cases")
    file = eng._hub_files["toy_cases"][0]
    eng.hub.max_bytes = 1  # every later download tries to empty the cache
    eng.hub.fetch("toy_listed/README.md")
    assert file.is_file()
    eng._reconnect()
    assert eng.query_sql("SELECT count(*) AS n FROM toy_cases")["rows"] == [[len(RECORDS)]]


def test_a_cached_file_removed_elsewhere_is_downloaded_again(
    tmp_path: Path, reg: Registry, released: Path
) -> None:
    eng = engine(reg, tmp_path, released)
    eng.manifest("toy_cases")
    eng._hub_files["toy_cases"][0].unlink()
    eng._reconnect()
    assert "toy_cases" not in eng._hub_files
    assert eng.query_sql("SELECT count(*) AS n FROM toy_cases")["rows"] == [[len(RECORDS)]]


@pytest.mark.parametrize("value", ["5GB", "0", "-1", "0.4", "nan", "inf", "abc"])
def test_a_mistyped_cache_limit_falls_back_to_the_default(monkeypatch: Any, value: str, caplog: Any) -> None:
    monkeypatch.setenv("HDR_CACHE_MAX_GB", value)
    assert hub.cache_max_bytes() == hub.DEFAULT_CACHE_MAX_BYTES
    assert "HDR_CACHE_MAX_GB" in caplog.text


def test_live_tables_are_never_loaded_into_a_file(tmp_path: Path, reg: Registry) -> None:
    from health_data_registry.live import load_live

    con = duckdb.connect(str(tmp_path / "x.duckdb"))
    with pytest.raises(ValueError, match="never written to disk"):
        load_live(con, reg.get("toy_listed"), RECORDS)
    mem = duckdb.connect(":memory:")
    with pytest.raises(duckdb.Error):
        load_live(mem, reg.get("toy_listed"), [{**RECORDS[0], "year": "x"}])
    tables = mem.execute("SELECT table_name FROM duckdb_tables()").fetchall()
    assert tables == []  # nothing half-loaded is left behind


def test_every_engine_connection_is_locked_down(tmp_path: Path, reg: Registry, released: Path) -> None:
    def get(url: str) -> Any:
        return raw_fetch(url, RECORDS)

    eng = engine(reg, tmp_path, released, fetch_at_run_time=True, getter=get)

    def locked() -> bool:
        row = (
            eng.connect()
            .execute(
                "SELECT current_setting('enable_external_access') = false AND current_setting('lock_configuration')"
            )
            .fetchone()
        )
        return bool(row and row[0])

    assert locked()  # empty
    eng.manifest("toy_cases")  # hub view: a reconnect
    assert locked()
    eng.manifest("toy_listed")  # live table: another reconnect
    assert locked()


def test_eviction_orders_by_last_use_not_access_time(tmp_path: Path) -> None:
    import os

    old, new = tmp_path / "old.parquet", tmp_path / "new.parquet"
    old.write_bytes(b"x" * 10)
    new.write_bytes(b"y" * 10)
    os.utime(old, (2_000_000_000, 1))  # read recently on a noatime mount: only atime moved
    os.utime(new, (1, 1_000_000))
    assert hub.evict(tmp_path, 10, set()) == [old]


def test_query_sql_across_sources_keeps_every_source(tmp_path: Path, reg: Registry, released: Path) -> None:
    """Outside review (c): a query over two tables carries both sources' provenance, licences and reviews."""
    from health_data_registry import tools

    eng = engine(reg, tmp_path, released, fetch_at_run_time=True, getter=lambda u: raw_fetch(u, RECORDS))
    out = tools.query_sql(eng, "SELECT year FROM toy_cases UNION ALL SELECT year FROM toy_listed", fmt="json")
    assert isinstance(out, dict) and "rows" in out, out
    prov = [n for n in out["notes"] if n.startswith("Provenance for ")]
    assert [p.split(":")[0] for p in prov] == ["Provenance for toy_cases", "Provenance for toy_listed"]
    assert all("raw file sha256 " in p for p in prov)  # live reads hash the bytes fetched, too
    assert all("licence " in p and "cite as: " in p and "contract_status: draft" in p for p in prov)
    assert any(n.startswith("toy_cases: this dataset's contract is a draft") for n in out["notes"])
