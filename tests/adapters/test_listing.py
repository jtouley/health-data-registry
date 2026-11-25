"""The resumable listing crawl (html_table, shape listing) on a fake site: complete or nothing, data first and
checkpoint last, the pace written before each request, one crawler per host, redirects followed only into scope."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from health_data_registry.access import CrawlStopped, HostLock
from health_data_registry.adapters import LayoutError, pages
from health_data_registry.fetch import FetchRefused
from health_data_registry.listing import CrawlIncomplete, ListingState, crawl_pages, partitions
from health_data_registry.registry import CatalogEntry
from tests.adapters.helpers import HOST, Site, entry

LISTING = {
    "url_template": "https://{host}/search/?yr={year}&page={page}",
    "years": {"first": 2025, "last": 2025},
    "page_size": 2,
    "item_link_selector": "h3.entry-title > a",
    "item_path_pattern": r"/abstract/[a-z0-9-]+/",
    "results_count_selector": "p.results-count",
    "results_count_pattern": r"([\d,]+) Results",
    "fields": {"title": "h1.entry-title", "body": "div.entry-content"},
}


def listing_entry(**source: Any) -> CatalogEntry:
    return entry(
        "html_table", {"shape": "listing", "listing": source.pop("listing", LISTING)},
        scope_paths=[r"/search/", r"/abstract/[a-z0-9-]+/"], min_interval_s=120.0, **source,
    )  # fmt: skip


def listing_html(total: int, slugs: list[str]) -> bytes:
    links = "".join(f'<h3 class="entry-title"><a href="/abstract/{s}/">{s}</a></h3>' for s in slugs)
    return f'<p class="results-count">{total} Results</p>{links}'.encode()


def item_html(slug: str) -> bytes:
    return f'<h1 class="entry-title">{slug.title()}</h1><div class="entry-content">Text of {slug}.</div>'.encode()


def search(page: int, year: int = 2025) -> str:
    return f"https://{HOST}/search/?yr={year}&page={page}"


def item(slug: str) -> str:
    return f"https://{HOST}/abstract/{slug}/"


def site(total: int = 3, slugs: tuple[str, ...] = ("a", "b", "c")) -> Site:
    pages_ = {
        search(1): listing_html(total, list(slugs[:2])),
        search(2): listing_html(total, list(slugs[2:])),
    }
    return Site({**pages_, **{item(s): item_html(s) for s in slugs}})


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


@pytest.fixture(autouse=True)
def _locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HDR_CRAWL_LOCKS", str(tmp_path / "locks"))


def run(e: CatalogEntry, get: Any, lake: Path, clock: Clock, **kw: Any) -> list[Any]:
    from health_data_registry.adapters import Read

    read = Read(entry=e, config=e.source.config(), get=get, lake=lake, **kw)
    return list(crawl_pages(read, clock=clock, sleep=clock.sleep))


def test_a_complete_crawl_hands_on_every_item_in_order(tmp_path: Path) -> None:
    e, s, clock = listing_entry(), site(), Clock()
    got = run(e, s, tmp_path, clock)
    assert [p.records for p in got] == [
        [{"title": t, "body": f"Text of {t.lower()}."}] for t in ("A", "B", "C")
    ]
    assert s.calls == [search(1), item("a"), item("b"), search(2), item("c")]
    assert all(
        p.fetch.content == b"" and (tmp_path / "raw" / p.fetch.sha256).is_file() for p in got
    )  # raw kept
    assert clock.slept == [120.0] * 4  # the source's own pace, between every two requests
    ck = json.loads(
        (tmp_path / "crawl_state" / "listing" / "toy_source" / "2025" / "checkpoint.json").read_text()
    )
    assert ck["complete"] and ck["total"] == 3 and len(ck["done"]) == 3
    # a second run reads nothing again: the crawl is complete
    s.calls.clear()
    assert len(run(e, s, tmp_path, clock)) == 3 and s.calls == []


def test_through_the_core_and_the_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from health_data_registry import listing

    clock = Clock()
    monkeypatch.setattr(listing, "time", SimpleNamespace(time=clock, sleep=clock.sleep))
    got = list(pages(listing_entry(), site(), lake=tmp_path))
    assert len(got) == 3


def test_a_crash_after_the_data_but_before_the_checkpoint_is_redone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e, s, clock = listing_entry(), site(), Clock()
    real = ListingState.save

    def crash_on_b(self: ListingState, ck: Any) -> None:
        if item("b") in ck.done:
            raise KeyboardInterrupt("power cut")
        real(self, ck)

    monkeypatch.setattr(ListingState, "save", crash_on_b)
    with pytest.raises(KeyboardInterrupt):
        run(e, s, tmp_path, clock)
    monkeypatch.setattr(ListingState, "save", real)
    s.calls.clear()
    got = run(e, s, tmp_path, clock)
    assert [p.records[0]["title"] for p in got] == ["A", "B", "C"]
    assert s.calls == [search(1), item("b"), search(2), item("c")]  # a is not fetched again; b is redone


def test_an_interrupted_crawl_resumes_and_builds_nothing_meanwhile(tmp_path: Path) -> None:
    e, clock = listing_entry(), Clock()
    broken = site()
    broken.pages[item("c")] = FetchRefused("connect_failed", "the publisher could not be reached")
    with pytest.raises(FetchRefused):
        run(e, broken, tmp_path, clock)
    s = site()
    assert len(run(e, s, tmp_path, clock)) == 3
    assert s.calls == [search(2), item("c")]


def test_a_listing_that_disagrees_with_its_items_is_incomplete_and_stays_resumable(tmp_path: Path) -> None:
    e, clock = listing_entry(), Clock()
    short = site(total=4)
    with pytest.raises(CrawlIncomplete, match="shows 4 results but 3"):
        run(e, short, tmp_path, clock)
    ck = json.loads(
        (tmp_path / "crawl_state" / "listing" / "toy_source" / "2025" / "checkpoint.json").read_text()
    )
    assert not ck["complete"] and not ck["listing_done"] and ck["next_page"] == 1 and len(ck["done"]) == 3
    full = site(total=4, slugs=("a", "b", "c", "d"))
    full.pages[search(2)] = listing_html(4, ["c", "d"])
    assert len(run(e, full, tmp_path, clock)) == 4
    assert full.calls == [search(1), search(2), item("d")]


def test_max_rows_builds_a_partial_sample(tmp_path: Path) -> None:
    s = site()
    got = run(listing_entry(), s, tmp_path, Clock(), max_rows=2)
    assert len(got) == 2 and s.calls == [search(1), item("a"), item("b")]


def test_a_changed_total_mid_crawl_is_refused(tmp_path: Path) -> None:
    s = site()
    s.pages[search(2)] = listing_html(5, ["c"])
    with pytest.raises(LayoutError, match="now shows 5 results, not 3"):
        run(listing_entry(), s, tmp_path, Clock())


def test_listing_layout_refusals(tmp_path: Path) -> None:
    s = site()
    s.pages[search(1)] = b'<h3 class="entry-title"><a href="/abstract/a/">a</a></h3>'
    with pytest.raises(LayoutError, match="number of results"):
        run(listing_entry(), s, tmp_path, Clock())
    s.pages[search(1)] = listing_html(3, ["../../admin"])
    with pytest.raises(LayoutError, match="not an item page"):
        run(listing_entry(), s, tmp_path / "2", Clock())


def test_redirects_are_followed_only_into_the_sources_item_pages(tmp_path: Path) -> None:
    s = site()
    s.pages[item("b")] = FetchRefused("too_many_redirects", "moved", status=301, location=item("c"))
    got = run(listing_entry(), s, tmp_path, Clock())
    assert [p.records[0]["title"] for p in got] == ["A", "C", "C"]
    s2 = site()
    s2.pages[item("b")] = FetchRefused(
        "too_many_redirects", "moved", status=301, location="https://other.example.org/x"
    )
    with pytest.raises(LayoutError, match="outside this source's pages"):
        run(listing_entry(), s2, tmp_path / "2", Clock())


def test_the_pace_is_written_before_each_request_and_survives_a_restart(tmp_path: Path) -> None:
    e, clock = listing_entry(), Clock()
    seen: list[float] = []
    s = site()

    def get(url: str) -> Any:
        state = json.loads((tmp_path / "crawl_state" / "pace" / f"{HOST}.json").read_text())
        seen.append(state["last_request_at"])
        return s(url)

    run(e, get, tmp_path, clock, max_rows=1)
    assert seen == [1_000_000.0, 1_000_120.0]  # written before the request went out
    restarted = Clock()  # a new process: the in-memory pace is gone, the saved one is not
    restarted.now = 1_000_130.0
    run(e, site(), tmp_path, restarted, max_rows=2)
    assert restarted.slept[0] == pytest.approx(110.0)


def test_a_saved_pace_that_cannot_be_trusted_is_refused(tmp_path: Path) -> None:
    pace = tmp_path / "crawl_state" / "pace" / f"{HOST}.json"
    pace.parent.mkdir(parents=True)
    for bad, code in [({"host": "other.example.org"}, "state_host_mismatch"),
                      ({"host": HOST, "last_request_at": float("nan")}, "state_unreadable"),
                      ({"host": HOST, "last_request_at": 9e12}, "state_unreadable")]:  # fmt: skip
        pace.write_text(json.dumps(bad))
        with pytest.raises(CrawlStopped) as e:
            run(listing_entry(), site(), tmp_path, Clock())
        assert e.value.code == code
    pace.write_text("{not json")
    with pytest.raises(CrawlStopped, match="can't be read"):
        run(listing_entry(), site(), tmp_path, Clock())


def _hold(path: str, ready: Any, done: Any) -> None:
    lock = HostLock(HOST, Path(path))
    ready.set()
    done.wait(10)
    lock.release()


def test_one_crawler_per_host_across_processes(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("fork")
    ready, done = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold, args=(str(tmp_path / "locks" / f"{HOST}.lock"), ready, done))
    p.start()
    try:
        assert ready.wait(10)
        with pytest.raises(CrawlStopped, match="another crawler"):
            run(listing_entry(), site(), tmp_path, Clock())
    finally:
        done.set()
        p.join(10)
    assert len(run(listing_entry(), site(), tmp_path, Clock())) == 3  # the lock went with the process


def test_never_read_live(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="never read live"):
        list(pages(listing_entry(), site(), live=True))


def test_sessions_partition_the_crawl() -> None:
    cfg = listing_entry(listing={**LISTING, "url_template": LISTING["url_template"] + "&s={session}",
                                 "sessions": ["Oral", "Poster A"]}).source.config()  # fmt: skip
    assert partitions(cfg.listing) == [(2025, "Oral"), (2025, "Poster A")]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"url_template": "https://evil.example.org/?p={page}"}, "own host"),
        ({"url_template": "https://{host}/?p={page}&x={secret}"}, "may use only"),
        ({"url_template": "https://{host}/?y={year}"}, "{page}"),
        ({"sessions": ["Oral"]}, "sessions are listed"),
        ({"results_count_pattern": r"\d+ Results"}, "exactly 1 group"),
        ({"item_path_pattern": r"(/a+)\1"}, "linear time"),
        ({"page_size": 0}, "greater than or equal to 1"),
        ({"years": {"first": 2020, "last": 2010}}, "first to last"),
        ({"fields": {}}, "at least one field"),
    ],
)
def test_listing_config_refusals(change: dict[str, Any], match: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=match):
        listing_entry(listing={**LISTING, **change})
