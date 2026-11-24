"""Build the CROI abstract index, one conference year at a time: polite, resumable, CLI and API only.

    health-data-registry index-croi [--years 2022-2026] [--max-requests N]

One run reads robots.txt first, then walks each year's search listing (10 abstracts a page) and each abstract's
page, one request per crawl delay from robots.txt (120 s on croiconference.org: about 47 hours for 2025's 1,277
abstracts and 128 listing pages). After every abstract it writes a checkpoint
(lake/crawl_state/croi/year=<year>.json), so a stopped run carries on where it left off. The owner's bar is at
least five conference years (``MIN_YEARS``); the default is the five most recent.

Completeness and crash safety (owner conditions D26 and D27):

- A year is complete only when the abstracts read equal the listing's own total ("1277 Results"). A listing
  page that is empty, unreadable or changed before that total stops the run with ``incomplete_index`` and leaves
  the checkpoint where it was, so the same command carries on. The store's coverage record says which years are
  complete, and find_prior_work never reads an incomplete year as "nothing presented".
- Each abstract is written data first (both stores, each as one atomic temp-file-then-rename), checkpoint last,
  coverage after that. Rows are keyed on abstract number plus year, so a replay after a crash at any point
  rewrites at most the one abstract in flight, with no duplicates.

What is kept, and where (both are private-tier sources, see private.py):

- croi_abstracts_index: metadata only (number, year, title, authors as published, session, links, our own
  rule-based tags). Never the abstract text, never a poster.
- croi_abstracts_text: the abstract body, for short excerpts to the fellow with CROI's copyright line and link.

Raw pages are not kept anywhere (only their sha256, in the fetch log and on each row); poster PDFs and figures are
never requested, only linked. This never runs inside the MCP server.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from health_data_registry import private
from health_data_registry.adapters import croi
from health_data_registry.fetch import FetchRefused
from health_data_registry.polite import CrawlStopped, PoliteClient, https_getter
from health_data_registry.registry import Dataset, Registry
from health_data_registry.store import FetchLog

INDEX_ID = "croi_abstracts_index"
TEXT_ID = "croi_abstracts_text"
MIN_YEARS = 5  # owner rule: the index covers at least five CROI years
LATEST_HELD_YEAR = 2026  # the most recent CROI that has taken place (CROI 2026, February 2026)
DEFAULT_YEARS = tuple(range(LATEST_HELD_YEAR - MIN_YEARS + 1, LATEST_HELD_YEAR + 1))


@dataclass
class Checkpoint:
    year: int
    next_page: int = 1
    pages: int | None = None
    total: int | None = None
    pending: list[dict[str, str]] = field(default_factory=list)  # this page's abstracts not yet read
    done: list[str] = field(default_factory=list)  # abstract page links read
    complete: bool = False


def checkpoint_path(lake: Path, year: int) -> Path:
    return lake / "crawl_state" / "croi" / f"year={year}.json"


def load_checkpoint(lake: Path, year: int) -> Checkpoint:
    p = checkpoint_path(lake, year)
    if not p.is_file():
        return Checkpoint(year=year)
    return Checkpoint(**json.loads(p.read_text(encoding="utf-8")))


def save_checkpoint(lake: Path, cp: Checkpoint) -> None:
    p = checkpoint_path(lake, cp.year)
    p.parent.mkdir(parents=True, exist_ok=True)
    private.atomic_write(p, json.dumps(asdict(cp), indent=1) + "\n")


@dataclass
class RunSummary:
    year: int
    requests: int
    abstracts_read: int
    abstracts_done: int
    pages: int | None
    total: int | None
    complete: bool
    stopped: str | None = None  # why the run stopped before the year was complete, in plain words
    # budget_spent (the --max-requests limit), incomplete_index, robots_*, host_locked, fetch_refused,
    # layout_changed
    stop_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.stop_code in (None, "budget_spent")

    def lines(self) -> list[str]:
        out = [
            f"CROI {self.year}: {self.abstracts_read} abstracts read this run ({self.requests} requests); "
            f"{self.abstracts_done} of {self.total if self.total is not None else '?'} done in all"
        ]
        out.append("the year is complete" if self.complete else "run the same command again to carry on")
        if self.stopped:
            out.append(f"stopped: {self.stopped}")
        return out


def records(
    page: croi.AbstractPage, *, retrieved_at: str, sha256: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The index row (metadata and our tags) and the private text row for one abstract page."""
    index = {
        "abstract_number": page.abstract_number,
        "year": page.year,
        "title": page.title,
        "authors": page.authors,
        "session_type": page.session_type,
        "session_title": page.session_title,
        "category": page.category,
        "page_url": page.page_url,
        "poster_url": page.poster_url,
        **croi.tag_record(page.title, page.text),
        "pubmed_pmid": None,
        "retrieved_at": retrieved_at,
        "page_sha256": sha256,
    }
    text = {
        "abstract_number": page.abstract_number,
        "year": page.year,
        "abstract_text": page.text,
        "page_url": page.page_url,
        "retrieved_at": retrieved_at,
        "page_sha256": sha256,
    }
    return index, text


def coverage_entry(cp: Checkpoint) -> dict[str, Any]:
    return {"total": cp.total, "done": len(cp.done), "complete": cp.complete}


def default_client(lake: Path, *, max_requests: int | None = None) -> PoliteClient:
    return PoliteClient(
        croi.BASE,
        state_path=lake / "crawl_state" / f"{croi.HOST}.json",
        get_raw=https_getter(croi.HOST),
        log=FetchLog(lake, INDEX_ID),
        max_requests=max_requests,
    )


def _incomplete(year: int, cp: Checkpoint, why: str) -> CrawlStopped:
    return CrawlStopped(
        "incomplete_index",
        f"CROI {year} is not complete: {len(cp.done)} of {cp.total if cp.total is not None else '?'} abstracts "
        f"read, and {why}. Nothing was marked complete; run the same command again later to carry on.",
    )


def _read_listing(client: PoliteClient, year: int, cp: Checkpoint) -> croi.Listing:
    """One listing page, or CrawlStopped("incomplete_index") when it is unreadable, changed or empty."""
    url = croi.listing_url(year, cp.next_page)
    try:
        r = client.get(url)
        listing = croi.parse_listing(r.content, url, year=year)
        if year not in listing.years:
            raise croi.LayoutError(url, f"{year} is not offered in the year filter")
    except FetchRefused as e:
        raise _incomplete(year, cp, f"listing page {cp.next_page} could not be read ({e.message})") from e
    except croi.LayoutError as e:
        raise _incomplete(
            year, cp, f"listing page {cp.next_page} was not a readable listing ({e.what})"
        ) from e
    if cp.total is not None and listing.total != cp.total:
        raise _incomplete(
            year, cp, f"the listing's total changed from {cp.total} to {listing.total} mid-crawl"
        )
    if not listing.entries and len(cp.done) < listing.total:
        raise _incomplete(year, cp, f"listing page {cp.next_page} was empty before the listing's total")
    return listing


def index_croi(
    registry: Registry,
    lake: Path,
    year: int,
    *,
    max_requests: int | None = None,
    client: PoliteClient | None = None,
) -> RunSummary:
    """Index one year. A client passed in is left open (the caller owns its lock); one made here is closed."""
    croi.check_year(year)
    ds_index, ds_text = registry.get(INDEX_ID), registry.get(TEXT_ID)
    private.require_private(ds_index)
    private.require_private(ds_text)
    own = client is None
    try:
        client = client or default_client(lake, max_requests=max_requests)
    except CrawlStopped as e:  # another crawler holds the host's lock, or the state isn't this host's
        return _not_started(lake, year, e)
    try:
        return _index_year(client, lake, year, ds_index, ds_text)
    finally:
        if own:
            client.close()


def _not_started(lake: Path, year: int, e: CrawlStopped) -> RunSummary:
    cp = load_checkpoint(lake, year)
    return RunSummary(year, 0, 0, len(cp.done), cp.pages, cp.total, cp.complete, str(e), e.code)


def _index_year(
    client: PoliteClient, lake: Path, year: int, ds_index: Dataset, ds_text: Dataset
) -> RunSummary:
    cp = load_checkpoint(lake, year)
    start_requests = client.requests_made
    read = 0
    stopped: str | None = None
    code: str | None = None
    partition = f"year={year}"
    try:
        while not cp.complete:
            if not cp.pending:
                if cp.total is not None and len(cp.done) == cp.total:
                    cp.complete = True  # the abstracts read equal the listing's own total, and only then
                    save_checkpoint(lake, cp)
                    break
                if cp.pages is not None and cp.next_page > cp.pages:
                    raise _incomplete(year, cp, "the listing ran out of pages before its total")
                listing = _read_listing(client, year, cp)
                cp.pages, cp.total = listing.pages, listing.total
                done = set(cp.done)
                cp.pending = [asdict(e) for e in listing.entries if e.page_url not in done]
                cp.next_page += 1
                save_checkpoint(lake, cp)
                continue
            entry = croi.ListingEntry(**cp.pending[0])
            if croi.page_year(entry.page_url) != year:  # an abstract page, never a poster PDF or figure
                raise croi.LayoutError(entry.page_url, "a listed link that is not a CROI abstract page")
            r = client.get(entry.page_url)
            page = croi.parse_abstract(r.content, entry.page_url)
            if page.abstract_number != entry.abstract_number or page.year != year:
                raise croi.LayoutError(
                    entry.page_url,
                    f"the listing says abstract {entry.abstract_number} ({year}) but the page says "
                    f"{page.abstract_number} ({page.year})",
                )
            when = r.retrieved_at.astimezone(UTC).isoformat(timespec="seconds")
            index_row, text_row = records(page, retrieved_at=when, sha256=r.sha256)
            # Data first, each store one atomic rename; the checkpoint last (D27).
            private.write_rows(lake, ds_index, partition, [index_row])
            private.write_rows(lake, ds_text, partition, [text_row])
            cp.done.append(entry.page_url)
            cp.pending.pop(0)
            read += 1
            save_checkpoint(lake, cp)
    except CrawlStopped as e:
        stopped, code = str(e), e.code
    except FetchRefused as e:
        where = f" (it pointed to {e.location})" if e.location else ""
        stopped, code = f"CROI's site answered in an unexpected way: {e.message}{where}", "fetch_refused"
    except croi.LayoutError as e:
        stopped, code = str(e), "layout_changed"
    if cp.total is not None or read:  # after the checkpoint: coverage never claims more than is stored
        for ds in (ds_index, ds_text):
            private.write_coverage(lake, ds, {partition: coverage_entry(cp)})
    return RunSummary(
        year=year,
        requests=client.requests_made - start_requests,
        abstracts_read=read,
        abstracts_done=len(cp.done),
        pages=cp.pages,
        total=cp.total,
        complete=cp.complete,
        stopped=stopped,
        stop_code=code,
    )


def index_years(
    registry: Registry,
    lake: Path,
    years: Iterable[int] = DEFAULT_YEARS,
    *,
    max_requests: int | None = None,
    client: PoliteClient | None = None,
) -> list[RunSummary]:
    """Index several years in order with one client (one lock, one pace), stopping at the first year that
    doesn't finish. Years already complete cost no request."""
    wanted = sorted({croi.check_year(y) for y in years})
    own = client is None
    try:
        client = client or default_client(lake, max_requests=max_requests)
    except CrawlStopped as e:
        return [_not_started(lake, wanted[0], e)] if wanted else []
    out: list[RunSummary] = []
    try:
        for y in wanted:
            s = index_croi(registry, lake, y, client=client)
            out.append(s)
            if not s.complete:
                break
    finally:
        if own:
            client.close()
    return out


def full_year_estimate(total: int, delay_s: float) -> float:
    """Hours to index a year: one listing page per 10 abstracts plus one page per abstract, plus robots.txt."""
    requests = 1 + -(-total // croi.PER_PAGE) + total
    return requests * delay_s / 3600
