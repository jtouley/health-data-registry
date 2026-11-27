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
from health_data_registry.source_config import HtmlTableConfig
from tests.adapters.helpers import HOST, Site, config_of, entry, published

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
        scope_paths=[r"/search/", r"/abstract/[a-z0-9-]+/"],
        min_interval_s=source.pop("min_interval_s", 120.0), **source,
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
    assert [published(p) for p in got] == [
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
    # a second run reads no item again: it only checks the listing's total, which hasn't changed
    s.calls.clear()
    assert len(run(e, s, tmp_path, clock)) == 3 and s.calls == [search(1)]


def test_a_finished_partition_opens_again_when_its_listing_grows(tmp_path: Path) -> None:
    e, clock = listing_entry(), Clock()
    run(e, site(), tmp_path, clock)
    grown = site(total=4, slugs=("a", "b", "c", "d"))
    grown.pages[search(2)] = listing_html(4, ["c", "d"])
    got = run(e, grown, tmp_path, clock)
    assert [p.records[0]["title"] for p in got] == ["A", "B", "C", "D"]
    assert grown.calls == [search(1), search(1), search(2), item("d")]  # only the new item page is read


def test_a_checkpoint_of_another_method_is_discarded(tmp_path: Path) -> None:
    e, clock = listing_entry(), Clock()
    run(e, site(), tmp_path, clock)
    other = listing_entry(listing={**LISTING, "fields": {"title": "h1.entry-title"}})  # another parse
    s = site()
    got = run(other, s, tmp_path, clock)
    assert [published(p) for p in got] == [[{"title": t}] for t in ("A", "B", "C")]
    assert s.calls == [search(1), item("a"), item("b"), search(2), item("c")]  # read again, the new way


@pytest.mark.parametrize(
    "tamper",
    [
        lambda ck: "{not json",
        lambda ck: json.dumps({**ck, "done": {item("a"): "../../../../etc/passwd"}}),
        lambda ck: json.dumps({**ck, "unknown": 1}),
    ],
)
def test_a_checkpoint_that_cannot_be_trusted_is_refused(tmp_path: Path, tamper: Any) -> None:
    e, clock = listing_entry(), Clock()
    run(e, site(), tmp_path, clock, max_rows=1)
    p = tmp_path / "crawl_state" / "listing" / "toy_source" / "2025" / "checkpoint.json"
    p.write_text(tamper(json.loads(p.read_text())))
    with pytest.raises(LayoutError, match="not reused|can't be read"):
        run(e, site(), tmp_path, clock)


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


def test_a_sample_that_stops_part_way_through_a_page_does_not_skip_its_items(tmp_path: Path) -> None:
    e, clock = listing_entry(), Clock()
    assert len(run(e, site(), tmp_path, clock, max_rows=1)) == 1
    s = site()
    got = run(e, s, tmp_path, clock)  # the full run reads b, which the sample left on page 1
    assert [p.records[0]["title"] for p in got] == ["A", "B", "C"]
    assert s.calls == [search(1), item("b"), search(2), item("c")]


def test_a_changed_total_mid_crawl_walks_the_listing_again(tmp_path: Path) -> None:
    s = site(total=4, slugs=("a", "b", "c", "d"))
    s.pages[search(1)] = listing_html(3, ["a", "b"])  # the count moves between page 1 and page 2
    s.pages[search(2)] = listing_html(4, ["c", "d"])

    def get(url: str) -> Any:
        got = s(url)
        if url == search(2):  # after page 2 is read once, page 1 shows the new count too
            s.pages[search(1)] = listing_html(4, ["a", "b"])
        return got

    got = run(listing_entry(), get, tmp_path, Clock())
    assert [p.records[0]["title"] for p in got] == ["A", "B", "C", "D"]
    assert s.calls == [search(1), item("a"), item("b"), search(2), search(1), search(2), item("c"), item("d")]


def test_a_total_that_keeps_moving_leaves_the_crawl_incomplete_not_stuck(tmp_path: Path) -> None:
    s = site()
    n = iter(range(10, 100))
    s.pages[search(2)] = listing_html(3, ["c"])

    def get(url: str) -> Any:
        if url.startswith(f"https://{HOST}/search/"):
            s.pages[url] = listing_html(next(n), ["a", "b"] if url == search(1) else ["c"])
        return s(url)

    with pytest.raises(CrawlIncomplete, match="changed its number of results"):
        run(listing_entry(), get, tmp_path, Clock())


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


def test_a_lowered_pace_takes_effect_on_the_next_run(tmp_path: Path) -> None:
    clock = Clock()
    run(listing_entry(min_interval_s=600.0), site(), tmp_path, clock, max_rows=1)
    later = Clock()
    later.now = clock.now
    run(listing_entry(), site(), tmp_path, later, max_rows=2)
    assert later.slept == [120.0, 120.0]  # the saved 600 s is not carried over


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
    assert isinstance(cfg, HtmlTableConfig) and cfg.listing is not None
    assert partitions(cfg.listing) == [(2025, "Oral"), (2025, "Poster A")]


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


# ------------------------------------------------------------------------------------------ consistency checks

YEARS = {**LISTING, "item_path_pattern": r"/abstract/[a-z]+-(\d{4})/", "item_year_group": 1}


def year_site(slugs: tuple[str, ...]) -> Site:
    return Site(
        {
            search(1): listing_html(len(slugs), list(slugs[:2])),
            search(2): listing_html(len(slugs), list(slugs[2:])),
            **{item(s): item_html(s) for s in slugs},
        }
    )


def test_an_item_of_the_year_asked_for_is_read(tmp_path: Path) -> None:
    got = run(listing_entry(listing=YEARS),
              year_site(("a-2025", "b-2025", "c-2025")), tmp_path, Clock())  # fmt: skip
    assert len(got) == 3


def test_an_item_of_another_year_is_refused(tmp_path: Path) -> None:
    e = listing_entry(listing=YEARS)
    with pytest.raises(LayoutError, match="an item of 2024; the listing ignored its year"):
        run(e, year_site(("a-2025", "b-2024", "c-2025")), tmp_path, Clock())


def test_a_year_group_the_pattern_lacks_is_refused() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="item_year_group 2"):
        listing_entry(listing={**YEARS, "item_year_group": 2})


NUMBERED = {
    **LISTING,
    "item_number": {"link_text_pattern": r"(\d+)\. .*", "field": "title", "field_pattern": r"(\d+)\. .*"},
}


def numbered_site(listed: dict[str, str], printed: dict[str, str]) -> Site:
    def links(slugs: list[str]) -> str:
        return "".join(
            f'<h3 class="entry-title"><a href="/abstract/{s}/">{listed[s]}</a></h3>' for s in slugs
        )

    def page(s: str) -> bytes:
        return f'<h1 class="entry-title">{printed[s]}</h1><div class="entry-content">x</div>'.encode()

    slugs = list(listed)
    return Site(
        {
            search(1): f'<p class="results-count">{len(slugs)} Results</p>{links(slugs[:2])}'.encode(),
            search(2): f'<p class="results-count">{len(slugs)} Results</p>{links(slugs[2:])}'.encode(),
            **{item(s): page(s) for s in slugs},
        }
    )


@pytest.mark.parametrize(
    ("listed_b", "printed_b", "match"),
    [
        ("2. Beta", "2. Beta", None),
        ("2. Beta", "3. Beta", "numbers .* 2, but the page itself prints 3"),
        ("Beta", "2. Beta", "not at all, but the page itself prints 2"),
        ("2. Beta", "Beta", "numbers .* 2, but the page itself prints none"),
        ("Beta", "Beta", None),  # numbered on neither side: nothing to compare
    ],
)
def test_the_listing_number_is_the_number_the_page_prints(
    tmp_path: Path, listed_b: str, printed_b: str, match: str | None
) -> None:
    s = numbered_site({"a": "1. Alpha", "b": listed_b, "c": "3. Gamma"},
                      {"a": "1. Alpha", "b": printed_b, "c": "3. Gamma"})  # fmt: skip
    e = listing_entry(listing=NUMBERED)
    if match is None:
        assert len(run(e, s, tmp_path, Clock())) == 3
        return
    with pytest.raises(LayoutError, match=match):
        run(e, s, tmp_path, Clock())


def test_a_listing_page_that_is_gone_leaves_the_crawl_incomplete(tmp_path: Path) -> None:
    s = site()
    s.pages[search(2)] = FetchRefused("http_error", "The publisher answered 404.", status=404)
    with pytest.raises(CrawlIncomplete, match="shows 3 results but 2"):
        run(listing_entry(), s, tmp_path, Clock())


def test_a_listed_item_that_is_gone_leaves_the_crawl_incomplete(tmp_path: Path) -> None:
    s = site()
    clock = Clock()
    s.pages[item("b")] = FetchRefused("http_error", "The publisher answered 410.", status=410)
    with pytest.raises(
        CrawlIncomplete, match=r"shows 3 results but 2 .*1 listed item page is gone: .*abstract/b/"
    ):
        run(listing_entry(), s, tmp_path, clock)
    s.calls.clear()
    with pytest.raises(CrawlIncomplete):  # still gone: only that page is asked for, not the whole listing
        run(listing_entry(), s, tmp_path, clock)
    assert s.calls == [item("b")]
    s.pages[item("b")] = item_html("b")  # back again: the next run reads it and completes
    s.calls.clear()
    assert len(run(listing_entry(), s, tmp_path, clock)) == 3
    assert s.calls == [item("b")]


def test_a_partition_that_cannot_finish_does_not_hold_back_the_others(tmp_path: Path) -> None:
    two_years = {**LISTING, "years": {"first": 2024, "last": 2025}}
    s = Site(
        {
            search(1, 2024): listing_html(2, ["a"]),  # says 2, links 1
            search(1, 2025): listing_html(1, ["c"]),
            item("a"): item_html("a"),
            item("c"): item_html("c"),
        }
    )
    with pytest.raises(CrawlIncomplete, match="2024 listing shows 2 results but 1"):
        run(listing_entry(listing=two_years), s, tmp_path, Clock())
    assert item("c") in s.calls  # 2025 was crawled before the run said it was incomplete


def test_other_refusals_still_stop_the_crawl(tmp_path: Path) -> None:
    s = site()
    s.pages[search(1)] = FetchRefused("http_error", "The publisher answered 404.", status=404)
    with pytest.raises(FetchRefused):  # the first listing page: nothing shows the listing exists
        run(listing_entry(), s, tmp_path, Clock())
    s2 = site()
    s2.pages[item("a")] = FetchRefused("blocked", "blocked", status=403)
    with pytest.raises(FetchRefused):
        run(listing_entry(), s2, tmp_path / "2", Clock())


# --------------------------------------------------------------------------------- combined parse extensions


def test_parse_extensions_combine_in_declared_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry.adapters.extensions import EXTENSIONS, Extension
    from health_data_registry.adapters.html_table import item_records
    from health_data_registry.source_config import Strict

    order: list[str] = []

    def ext(name: str, out: list[dict[str, str]]) -> Extension:
        def run_(args: Any, content: bytes, url: str) -> list[dict[str, str]]:
            order.append(name)
            return out

        return Extension(name, "parse", Strict, run_, "test only")

    monkeypatch.setitem(EXTENSIONS, "probe_one", ext("probe_one", [{"session": "Oral"}]))
    monkeypatch.setitem(EXTENSIONS, "probe_two", ext("probe_two", [{"track": "Clinical"}]))
    monkeypatch.setitem(EXTENSIONS, "probe_title", ext("probe_title", [{"title": "again"}]))
    monkeypatch.setitem(EXTENSIONS, "probe_many", ext("probe_many", [{"x": "1"}, {"x": "2"}]))
    uses = [{"name": "probe_two", "hook": "parse"}, {"name": "probe_one", "hook": "parse"}]
    e = listing_entry(extensions=uses)
    cfg = config_of(e, HtmlTableConfig).listing
    assert cfg is not None
    recs = item_records(e, cfg, item_html("a"), item("a"))
    assert recs == [{"title": "A", "body": "Text of a.", "track": "Clinical", "session": "Oral"}]
    assert order == ["probe_two", "probe_one"]
    clash = listing_entry(extensions=[{"name": "probe_title", "hook": "parse"}])
    with pytest.raises(LayoutError, match="title, which is already read"):
        item_records(clash, cfg, item_html("a"), item("a"))
    many = listing_entry(extensions=[{"name": "probe_many", "hook": "parse"}])
    with pytest.raises(LayoutError, match="read 2 records"):
        item_records(many, cfg, item_html("a"), item("a"))
