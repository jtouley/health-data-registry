"""CROI as config on the crawler factory: the html_table listing crawl into its two private stores, on a fake
croiconference.org (tiny hand-written pages, a fake clock, no network).

The proof that the rewrite changed nothing a fellow sees: on the same fake site, the stores it writes are
byte-identical to what the retired per-source crawler (croi.py, croi_index.py, polite.py) wrote, kept in
tests/fixtures/croi_listing/. Also covered, as before:

- D26: a year is complete only at the listing's own total; a short or failed listing stores nothing, keeps the
  checkpoint, and resumes; an incomplete year never reads as "no prior work";
- D27: data first, checkpoint last; a crash after any single write resumes to identical stores;
- D28: one crawler per host, across processes;
- abstract text only in the text store; poster PDFs linked, never fetched; the 120 s floor.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from health_data_registry import access, listing, private, private_listing, tools
from health_data_registry.fetch import FetchRefused
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import RawFetch

INDEX, TEXT = "croi_abstracts_index", "croi_abstracts_text"
HOST = "www.croiconference.org"
BASE = f"https://{HOST}"
YEAR = 2025
PER_PAGE = 10
WHEN = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
GOLDEN = Path(__file__).parent / "fixtures" / "croi_listing"
TITLES = {
    0: "HIV incidence in Kenya: a prospective cohort",
    1: "Tuberculosis screening in South Africa",
    2: "Lenacapavir PrEP uptake in the United States",
}


@pytest.fixture(scope="module")
def registry() -> Registry:
    return load_registry()


@pytest.fixture(autouse=True)
def _locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HDR_CRAWL_LOCKS", str(tmp_path / "locks"))


class Clock:
    def __init__(self) -> None:
        self.now = 2_000_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(listing, "time", SimpleNamespace(time=c, sleep=c.sleep))
    return c


def number(i: int) -> str:
    return str(100 + i)


def page_url(i: int, year: int = YEAR) -> str:
    return f"{BASE}/abstract/{5000 + i}-{year}/"


def poster_url(i: int) -> str:
    return f"{BASE}/wp-content/uploads/poster-{number(i)}.pdf"


def title(i: int) -> str:
    return TITLES.get(i % 3, "") + f" ({i})"


def listing_url(page: int, year: int = YEAR) -> str:
    return f"{BASE}/search-abstracts/page/{page}/?aa=&at=&an=&sn=&kw=&yr={year}&st=&q=1"


def listing_html(entries: list[int], total: int, *, year: int = YEAR) -> bytes:
    items = "".join(
        f'<div class="post-entry-content"><h3 class="entry-title"><a href="{page_url(i, year)}" '
        f'title="{number(i)} – {title(i)}">{number(i)} – {title(i)}</a></h3></div>'
        for i in entries
    )
    return (
        f'<form><select name="yr"><option value="">Any year</option><option value="{year}">{year}</option>'
        "</select></form>"
        f'<p class="abstract-search-results-count">{total:,} Results</p>'
        f'<div id="abstract-search-results-list">{items}</div>'
    ).encode()


def abstract_html(i: int, *, shown_number: str | None = None, sidebar_number: str | None = None) -> bytes:
    n = shown_number or number(i)
    side = sidebar_number or n
    return (
        f'<h1 class="entry-title">{n} – {title(i)}</h1>'
        '<article class="abstract abstract-session-type-poster abstract-category-hiv-prevention">'
        f'<div class="entry-content"><p>Background</p><p>{title(i)}. We followed people with HIV in a '
        "cohort and report incidence over two years of follow-up in clinics.</p></div></article>"
        f'<div class="abstract-sidebar-content"><h4>Abstract Number</h4><div class="abstract-sidebar-text">{side}'
        "</div></div>"
        '<div class="abstract-sidebar-content"><h4>Session Title</h4><div class="abstract-sidebar-text">'
        "(P-A1) Prevention</div></div>"
        '<div class="abstract-sidebar-content"><h4>Authors</h4><div class="abstract-sidebar-text">'
        "A. Author, B. Author</div></div>"
        f'<a title="Download Poster" href="{poster_url(i)}">Download Poster</a>'
    ).encode()


class Site:
    """croiconference.org with ``n`` abstracts for YEAR, 10 to a listing page. A getter: url -> RawFetch."""

    def __init__(self, n: int = 13) -> None:
        self.calls: list[str] = []
        self.pages: dict[str, bytes | int] = {}
        ids = list(range(n))
        for p in range(max(1, -(-n // PER_PAGE))):
            self.pages[listing_url(p + 1)] = listing_html(ids[p * PER_PAGE : (p + 1) * PER_PAGE], n)
        for i in ids:
            self.pages[page_url(i)] = abstract_html(i)

    def __call__(self, url: str) -> RawFetch:
        self.calls.append(url)
        body = self.pages.get(url, 404)
        if isinstance(body, int):
            raise FetchRefused("http_error", f"The publisher answered with status {body}.", status=body)
        return RawFetch(url=url, content=body, sha256=hashlib.sha256(body).hexdigest(), retrieved_at=WHEN)


def crawl(registry: Registry, lake: Path, site: Site, years: list[int] | None = None) -> list[Any]:
    return private_listing.crawl_into_private(registry, lake, INDEX, years or [YEAR], get=site)


def stores(lake: Path) -> dict[str, bytes]:
    """The files a fellow's answers are read from: each store's rows and its coverage record."""
    out = {}
    for sid in (INDEX, TEXT):
        for name in (f"year={YEAR}.jsonl", "_coverage.json"):
            p = lake / "private" / sid / name
            if p.is_file():
                out[f"{sid}__{name}"] = p.read_bytes()
    return out


def golden(n: int) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted((GOLDEN / f"site{n}").iterdir())}


def rows(lake: Path, sid: str) -> list[dict[str, Any]]:
    p = lake / "private" / sid / f"year={YEAR}.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------------------- the same output as before


@pytest.mark.parametrize("n", [3, 13])
def test_the_stores_are_byte_identical_to_the_retired_crawler(
    tmp_path: Path, registry: Registry, clock: Clock, n: int
) -> None:
    site = Site(n)
    runs = crawl(registry, tmp_path, site)
    assert [(r.year, r.complete, r.total, r.stored, r.code) for r in runs] == [(YEAR, True, n, n, None)]
    assert stores(tmp_path) == golden(n)
    pages = -(-n // PER_PAGE)
    assert len(site.calls) == pages + n  # each listing page and each abstract once; robots.txt is the core's
    assert set(clock.slept) == {120.0}  # never faster than the catalog's 120 s floor


def test_a_complete_year_costs_no_request_and_restages_identically(
    tmp_path: Path, registry: Registry, clock: Clock
) -> None:
    site = Site(3)
    crawl(registry, tmp_path, site)
    site.calls.clear()
    (tmp_path / "private" / INDEX / f"year={YEAR}.jsonl").unlink()  # a lost store is rebuilt from kept items
    assert crawl(registry, tmp_path, site)[0].complete
    assert site.calls == []
    assert stores(tmp_path) == golden(3)


def test_text_only_in_the_text_store_and_posters_linked_never_fetched(
    tmp_path: Path, registry: Registry, clock: Clock
) -> None:
    site = Site(3)
    crawl(registry, tmp_path, site)
    index, text = rows(tmp_path, INDEX), rows(tmp_path, TEXT)
    assert all("abstract_text" not in r for r in index)
    assert all(r["abstract_text"].startswith("Background\n") for r in text)
    assert index[0]["poster_url"] == poster_url(0)
    assert not any(u.endswith(".pdf") or "/wp-content/" in u for u in site.calls)
    assert (
        not (tmp_path / "datasets").exists() and not (tmp_path / "raw").exists()
    )  # nothing in the public lake
    kept = list((tmp_path / "private" / "_crawl" / "raw").iterdir())  # the pages read, kept privately
    assert len(kept) == 3
    for p in (tmp_path / "private").rglob("*.jsonl"):
        assert p.stat().st_mode & 0o077 == 0


def test_croi_is_served_only_as_excerpts_with_links(registry: Registry) -> None:
    assert private.excerpts_only(registry.get(INDEX)) and private.excerpts_only(registry.get(TEXT))
    assert not private.excerpts_only(registry.get("cdc_nndss_weekly"))
    assert private_listing.group(registry, registry.get(TEXT)) == private_listing.group(
        registry, registry.get(INDEX)
    )  # one crawl builds both stores


# ------------------------------------------------------------------------------- D26: complete or nothing


def test_an_empty_listing_page_before_the_total_stores_nothing_and_resumes(
    tmp_path: Path, registry: Registry, clock: Clock
) -> None:
    site = Site(13)
    good = site.pages[listing_url(2)]
    site.pages[listing_url(2)] = listing_html([], 13)  # "13 Results" and none listed
    run = crawl(registry, tmp_path, site)[0]
    assert (run.complete, run.code, run.total, run.crawled) == (False, "crawl_incomplete", 13, 10)
    assert not run.ok and "run the same command again to carry on" in run.lines()
    assert not (tmp_path / "private" / INDEX / f"year={YEAR}.jsonl").exists()  # nothing of the year stored
    cov = private.read_coverage(tmp_path, registry.get(TEXT))
    assert cov is None or not cov[f"year={YEAR}"]["complete"]
    site.pages[listing_url(2)] = good
    site.calls.clear()
    assert crawl(registry, tmp_path, site)[0].complete
    assert [u for u in site.calls if "/abstract/" in u] == [page_url(i) for i in range(10, 13)]  # resumed
    assert stores(tmp_path) == golden(13)


@pytest.mark.parametrize("status", [503, 404, 403])
def test_a_failed_listing_page_stops_the_year(
    tmp_path: Path, registry: Registry, clock: Clock, status: int
) -> None:
    site = Site(13)
    site.pages[listing_url(2)] = status
    run = crawl(registry, tmp_path, site)[0]
    assert not run.complete and run.code == "fetch_refused" and f"status {status}" in (run.stopped or "")
    assert not (tmp_path / "private" / INDEX / f"year={YEAR}.jsonl").exists()


def test_a_total_that_changes_mid_crawl_stops_the_year(
    tmp_path: Path, registry: Registry, clock: Clock
) -> None:
    site = Site(13)
    site.pages[listing_url(2)] = listing_html([10, 11, 12], 14)
    run = crawl(registry, tmp_path, site)[0]
    assert not run.complete and run.code == "layout_changed" and "14" in (run.stopped or "")


def test_a_listing_of_zero_is_never_complete(tmp_path: Path, registry: Registry, clock: Clock) -> None:
    site = Site(0)
    run = crawl(registry, tmp_path, site)[0]
    assert not run.complete and run.code == "empty_listing"


def test_a_heading_and_sidebar_that_disagree_stop_the_year(
    tmp_path: Path, registry: Registry, clock: Clock
) -> None:
    site = Site(3)
    site.pages[page_url(1)] = abstract_html(1, sidebar_number="999")
    run = crawl(registry, tmp_path, site)[0]
    assert not run.complete and run.code == "layout_changed"
    assert "differs" in (run.stopped or "")
    assert not (tmp_path / "private" / INDEX / f"year={YEAR}.jsonl").exists()


def test_two_pages_with_one_number_keep_the_first_and_stay_incomplete(
    tmp_path: Path, registry: Registry, clock: Clock
) -> None:
    site = Site(3)
    site.pages[page_url(2)] = abstract_html(2, shown_number=number(1))
    run = crawl(registry, tmp_path, site)[0]
    assert (run.complete, run.code, run.collisions, run.stored) == (False, "collision", 1, 2)
    kept = {r["abstract_number"]: r["page_url"] for r in rows(tmp_path, INDEX)}
    assert kept == {number(0): page_url(0), number(1): page_url(1)}  # the first page of the number kept
    cov = private.read_coverage(tmp_path, registry.get(INDEX))
    assert cov is not None
    assert cov[f"year={YEAR}"] == {"total": 3, "done": 3, "stored": 2, "collisions": 1, "complete": False}


def test_an_incomplete_year_never_reads_as_no_prior_work(
    tmp_path: Path, registry: Registry, clock: Clock
) -> None:
    site = Site(3)
    site.pages[page_url(2)] = abstract_html(2, shown_number=number(1))
    crawl(registry, tmp_path, site)
    out = tools.find_prior_work(registry, condition="influenza", years=str(YEAR), fmt="json", lake=tmp_path)
    assert isinstance(out, dict) and out.get("refused") == "index_incomplete"
    site = Site(3)
    crawl(registry, tmp_path / "ok", site)
    out = tools.find_prior_work(
        registry, condition="influenza", years=str(YEAR), fmt="json", lake=tmp_path / "ok"
    )
    assert isinstance(out, dict) and "refused" not in out
    assert "No indexed CROI abstract matches 'influenza'" in json.dumps(out)
    out = tools.find_prior_work(registry, condition="influenza", fmt="json", lake=tmp_path / "ok")
    assert isinstance(out, dict) and out.get("refused") == "index_incomplete"  # the other default years
    assert "2022–2024 (not indexed)" in out["message"]


def test_years_outside_the_catalog_are_refused(tmp_path: Path, registry: Registry) -> None:
    with pytest.raises(ValueError, match="outside"):
        private_listing.crawl_into_private(registry, tmp_path, INDEX, [2014], get=Site(3))
    with pytest.raises(private_listing.NotAListing):
        private_listing.listing_config(registry.get("cdc_nndss_weekly").entry)
    assert private_listing.years_of(registry.get(INDEX).entry) == [2022, 2023, 2024, 2025, 2026]


# ------------------------------------------------------------------------------- D27: crash after any write


class Crash(BaseException):
    """The process dies here (not an Exception: nothing in the crawl may catch it)."""


def _count_writes(monkeypatch: pytest.MonkeyPatch, crash_at: int | None) -> list[str]:
    """Count every write the crawl makes (pace, raw page, item, checkpoint, store rows, manifest, coverage), and
    crash right after the ``crash_at``-th has landed: between it and the next."""
    seen: list[str] = []

    def after(what: str) -> None:
        seen.append(what)
        if crash_at is not None and len(seen) == crash_at:
            raise Crash(what)

    def wrap(module: Any, name: str) -> None:
        real = getattr(module, name)

        def counted(*a: Any, **kw: Any) -> Any:
            out = real(*a, **kw)
            target = a[0] if name != "write_raw" and a and isinstance(a[0], Path) else name
            after(getattr(target, "name", str(target)))
            return out

        monkeypatch.setattr(module, name, counted)

    wrap(access, "atomic_write")  # the pace store
    wrap(listing, "atomic_write")  # items and checkpoints
    wrap(listing, "write_raw")  # raw pages
    wrap(private, "atomic_write")  # store rows, manifests, coverage
    return seen


def test_a_crash_after_any_write_resumes_to_identical_stores(
    tmp_path: Path, registry: Registry, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = 13
    writes = _count_writes(monkeypatch, None)
    crawl(registry, tmp_path / "clean", Site(n))
    total = len(writes)
    assert stores(tmp_path / "clean") == golden(n)
    assert {"checkpoint.json", "_coverage.json", f"year={YEAR}.jsonl", "write_raw"} <= set(writes)
    monkeypatch.undo()
    monkeypatch.setattr(listing, "time", SimpleNamespace(time=clock, sleep=clock.sleep))
    monkeypatch.setenv("HDR_CRAWL_LOCKS", str(tmp_path / "locks"))
    for k in range(1, total + 1):
        lake = tmp_path / f"crash-{k}"
        writes = _count_writes(monkeypatch, k)
        site = Site(n)
        with pytest.raises(Crash):
            crawl(registry, lake, site)  # the host lock goes with the "process"
        where = writes[-1]
        monkeypatch.undo()
        monkeypatch.setattr(listing, "time", SimpleNamespace(time=clock, sleep=clock.sleep))
        monkeypatch.setenv("HDR_CRAWL_LOCKS", str(tmp_path / "locks"))
        for _ in range(3):
            if crawl(registry, lake, site)[0].complete:
                break
        assert stores(lake) == golden(n), f"crash after write {k} ({where})"
        fetched = [u for u in site.calls if "/abstract/" in u]
        assert len(fetched) - len(set(fetched)) <= 1, f"crash after write {k} re-read more than one abstract"
        assert not list(lake.rglob("*.part")) and not list(lake.rglob("*.tmp")), "no temp file left behind"


# ------------------------------------------------------------------------------- D28: one crawler per host

_HOLDER = """
import os, sys
from health_data_registry.access import HostLock

lock = HostLock(sys.argv[1])
print("locked", flush=True)
sys.stdin.readline()
lock.release()
print("released", flush=True)
"""


def test_a_second_process_cannot_crawl_croi_at_the_same_time(
    tmp_path: Path, registry: Registry, clock: Clock
) -> None:
    import os

    env = {**os.environ, "HDR_CRAWL_LOCKS": str(tmp_path / "locks")}
    holder = subprocess.Popen(  # noqa: S603 - this interpreter and a fixed script
        [sys.executable, "-c", _HOLDER, HOST],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    site = Site(3)
    try:
        assert holder.stdout is not None and holder.stdin is not None
        assert holder.stdout.readline().strip() == "locked"
        run = crawl(registry, tmp_path, site)[0]
        assert (run.complete, run.code) == (False, "host_locked")
        assert site.calls == []  # refused before any request
        holder.stdin.write("stop\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "released"
        assert holder.wait(timeout=30) == 0
    finally:
        if holder.poll() is None:
            holder.kill()
    assert crawl(registry, tmp_path, site)[0].complete  # free once the other process let go
    assert stores(tmp_path) == golden(3)


def test_a_killed_crawler_leaves_no_stale_lock(tmp_path: Path, registry: Registry, clock: Clock) -> None:
    import os

    env = {**os.environ, "HDR_CRAWL_LOCKS": str(tmp_path / "locks")}
    holder = subprocess.Popen(  # noqa: S603 - this interpreter and a fixed script
        [sys.executable, "-c", _HOLDER, HOST],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
    holder.kill()
    holder.wait(timeout=30)
    assert crawl(registry, tmp_path, Site(3))[0].complete


def test_the_cli_refuses_in_plain_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from health_data_registry import cli

    monkeypatch.setenv("HDR_LAKE", str(tmp_path))
    assert cli.parse_years("2022-2026") == [2022, 2023, 2024, 2025, 2026]
    assert cli.parse_years("2025") == [2025]
    with pytest.raises(ValueError):
        cli.parse_years("2026-2022")
    assert cli.main(["crawl-listing", "cdc_nndss_weekly"]) == 1
    assert "crawl-listing stopped" in capsys.readouterr().err
    assert cli.main(["crawl-listing", INDEX, "--years", "soon"]) == 1
    assert "Give a year" in capsys.readouterr().err
    assert cli.main(["crawl-listing", INDEX, "--years", "2014"]) == 1
    assert "outside" in capsys.readouterr().err
