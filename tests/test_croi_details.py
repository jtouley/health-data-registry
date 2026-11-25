"""Exact outputs of the CROI crawl's small pieces: the default client, the getter, run summaries, row records,
and the private tier's notes and purge lines. (Pinned exactly so mutation tests notice any change.)"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry import croi_index, fetch, naming, polite, private
from health_data_registry.adapters import croi
from health_data_registry.registry import Registry, load_registry
from tests.test_croi_index import YEAR, Clock, Site, abstract_html, make_client, page_url, poster_url, run


@pytest.fixture(scope="module")
def registry() -> Registry:
    return load_registry()


def test_default_client(tmp_path: Path) -> None:
    with croi_index.default_client(tmp_path, max_requests=7) as c:
        assert c.base == croi.BASE and c.host == croi.HOST
        assert c.state_path == tmp_path / "crawl_state" / f"{croi.HOST}.json"
        assert c.max_requests == 7
        assert c.default_delay == 120.0
        assert c.log is not None and c.log.lake == tmp_path and c.log.source == "croi_abstracts_index"
        assert c.lock.held and c.lock.path == tmp_path / "crawl-locks" / f"{croi.HOST}.lock"
        assert isinstance(c.clock, polite.SystemClock)
        assert c.user_agent == fetch.USER_AGENT
    with croi_index.default_client(tmp_path) as c:
        assert c.max_requests is None


def test_https_getter_makes_one_request_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def fake(url: str, **kw: Any) -> str:
        seen.append((url, kw))
        return "result"

    monkeypatch.setattr(fetch, "fetch", fake)
    get = polite.https_getter(croi.HOST, timeout=12.5)
    assert get(f"{croi.BASE}/x/") == "result"
    assert seen == [
        (
            f"{croi.BASE}/x/",
            {
                "allowed_hosts": frozenset({croi.HOST}),
                "timeout": 12.5,
                "max_redirects": 0,
                "headers": {"Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"},
            },
        )
    ]
    polite.https_getter(croi.HOST)(f"{croi.BASE}/y/")
    assert seen[-1][1]["timeout"] == 60.0


def test_system_clock_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(polite.time, "sleep", slept.append)
    clock = polite.SystemClock()
    clock.sleep(2.5)
    assert slept == [2.5]
    assert clock.now() == pytest.approx(polite.time.time(), abs=5)


def test_run_summary_lines() -> None:
    done = croi_index.RunSummary(2025, 16, 13, 13, 2, 13, True)
    assert done.lines() == [
        "CROI 2025: 13 abstracts read this run (16 requests); 13 of 13 done in all",
        "the year is complete",
    ]
    stopped = croi_index.RunSummary(2024, 1, 0, 0, None, None, False, "robots said no", "robots_disallow")
    assert stopped.lines() == [
        "CROI 2024: 0 abstracts read this run (1 requests); 0 of ? done in all",
        "run the same command again to carry on",
        "stopped: robots said no",
    ]
    assert not stopped.ok and done.ok


def test_records_split_metadata_from_text() -> None:
    page = croi.parse_abstract(abstract_html(0), page_url(0))
    index, text = croi_index.records(page, retrieved_at="2026-11-24T12:00:00+00:00", sha256="ab" * 32)
    assert set(index) == {
        "abstract_number", "year", "title", "authors", "session_type", "session_title", "category", "page_url",
        "poster_url", "condition_tags", "design_tags", "data_source_tags", "geography_tags", "pubmed_pmid",
        "retrieved_at", "page_sha256",
    }  # fmt: skip
    assert index["pubmed_pmid"] is None and index["poster_url"] == poster_url(0)
    assert index["retrieved_at"] == text["retrieved_at"] == "2026-11-24T12:00:00+00:00"
    assert index["page_sha256"] == text["page_sha256"] == "ab" * 32
    assert index["session_title"] == "(P-A1) Prevention" and index["title"] == page.title
    assert text == {
        "abstract_number": page.abstract_number,
        "year": YEAR,
        "abstract_text": page.text,
        "page_url": page_url(0),
        "retrieved_at": "2026-11-24T12:00:00+00:00",
        "page_sha256": "ab" * 32,
    }


def test_a_locked_host_reports_the_saved_progress(tmp_path: Path, registry: Registry) -> None:
    site, clock = Site(13), Clock()
    run(registry, tmp_path, site, clock, max_requests=5)  # 3 of 13 read, page 1 listed
    with make_client(tmp_path, site, clock):
        s = croi_index.index_croi(registry, tmp_path, YEAR)
    assert (s.year, s.requests, s.abstracts_read, s.abstracts_done, s.pages, s.total, s.complete) == (
        YEAR, 0, 0, 3, 2, 13, False,
    )  # fmt: skip
    assert s.stop_code == "host_locked" and "another crawler" in (s.stopped or "")


def test_index_years_with_its_own_client(
    tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[polite.PoliteClient] = []
    site, clock = Site(3), Clock()

    def factory(lake: Path, *, max_requests: int | None = None) -> polite.PoliteClient:
        c = make_client(lake, site, clock, max_requests=max_requests)
        made.append(c)
        return c

    monkeypatch.setattr(croi_index, "default_client", factory)
    out = croi_index.index_years(registry, tmp_path, [YEAR], max_requests=50)
    assert [s.complete for s in out] == [True]
    assert made[0].max_requests == 50 and not made[0].lock.held  # closed after the run
    assert croi_index.index_years(registry, tmp_path, []) == []
    with pytest.raises(ValueError):
        croi_index.index_years(registry, tmp_path, [1999])


def test_index_croi_closes_only_the_client_it_made(tmp_path: Path, registry: Registry) -> None:
    site, clock = Site(3), Clock()
    with make_client(tmp_path, site, clock) as c:
        croi_index.index_croi(registry, tmp_path, YEAR, client=c)
        assert c.lock.held
    with pytest.raises(ValueError):
        croi_index.index_croi(registry, tmp_path, 1999)


def test_private_notes_are_exact(registry: Registry) -> None:
    ds = registry.get("croi_abstracts_text")
    terms = private.contract_terms(ds)
    lines = private.notes(ds)
    assert lines[0] == terms["attribution"]
    assert lines[1] == f"Licence: croi-abstract-text ({ds.entry.licence.terms_url}): {terms['licenceNote']}"
    assert lines[2 : 2 + len(ds.entry.caveats)] == [f"Note: {c}" for c in ds.entry.caveats]
    assert lines[-2] == "Suppression: the publisher declares none for this source."
    assert lines[-1] == f"How to cite: {ds.entry.citation}"
    assert len(lines) == 4 + len(ds.entry.caveats)


def test_purge_lines_are_exact(tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(naming.OWNER_ENV, raising=False)
    sid = "croi_abstracts_text"
    private.area(tmp_path, sid).mkdir(parents=True)
    private.hub_cache(tmp_path, sid).mkdir(parents=True)
    assert private.purge(tmp_path, sid) == [
        f"deleted the local private area {private.area(tmp_path, sid)}",
        f"deleted the hub cache {private.hub_cache(tmp_path, sid)}",
    ]
    monkeypatch.setenv("HF_TOKEN", "hf_write")
    deleted: list[tuple[str, str]] = []

    class FakeApi:
        def __init__(self, token: str) -> None:
            self.token = token

        def delete_repo(self, repo: str, *, repo_type: str, missing_ok: bool) -> None:
            deleted.append((repo, self.token))

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    assert private.purge(tmp_path, sid, owner="org") == [
        "deleted the private hub dataset org/health-data-registry-private-croi-abstracts-text"
    ]
    assert deleted == [("org/health-data-registry-private-croi-abstracts-text", "hf_write")]
    monkeypatch.delenv("HF_TOKEN")
    assert private.purge(tmp_path, sid, owner="org") == [
        "the private hub dataset org/health-data-registry-private-croi-abstracts-text was not deleted: set "
        "HF_TOKEN to a write token and rerun"
    ]


def test_read_rows_reads_every_partition_and_skips_blank_lines(tmp_path: Path, registry: Registry) -> None:
    ds = registry.get("croi_abstracts_text")
    folder = private.area(tmp_path, ds.id)
    folder.mkdir(parents=True)
    (folder / "year=2024.jsonl").write_text('{"a": 1}\n\n{"a": 2}\n')
    (folder / "year=2025.jsonl").write_text('{"a": 3}\n')
    (folder / "notes.txt").write_text('{"a": 4}\n')
    got = private.read_rows(tmp_path, ds)
    assert got is not None and [r["a"] for r in got.rows] == [1, 2, 3]


def test_read_rows_needs_owner_and_token(
    tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = registry.get("croi_abstracts_text")
    monkeypatch.delenv(naming.OWNER_ENV, raising=False)
    monkeypatch.setenv(private.HF_TOKEN_ENV, "hf_read")
    calls: list[str] = []
    assert private.read_rows(tmp_path, ds, downloader=lambda r, t, d: calls.append(r)) is None
    monkeypatch.setenv(naming.OWNER_ENV, "org")
    monkeypatch.setenv(private.HF_TOKEN_ENV, "   ")
    assert private.read_rows(tmp_path, ds, downloader=lambda r, t, d: calls.append(r)) is None
    assert calls == []
    monkeypatch.setenv(private.HF_TOKEN_ENV, " hf_read ")
    private._hub_read_at.clear()
    tokens: list[str] = []
    assert (
        private.read_rows(tmp_path, ds, downloader=lambda r, t, d: tokens.append(t)) is None
    )  # nothing there
    assert tokens == ["hf_read"]
    with pytest.raises(private.PrivateRefused) as e:
        private.read_rows(tmp_path, registry.get("cdc_nndss_weekly"))
    assert e.value.code == "not_private_tier" and "tier is public" in str(e.value)
