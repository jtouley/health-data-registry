"""Build the CROI abstract index for one conference year: polite, resumable, CLI and API only.

    health-data-registry index-croi --year 2025 [--max-requests N]

One run reads robots.txt first, then walks the year's search listing (10 abstracts a page) and each abstract's
page, one request per crawl delay from robots.txt (120 s on croiconference.org: about 47 hours for 2025's 1,277
abstracts and 128 listing pages). After every request it writes a checkpoint
(lake/crawl_state/croi/year=<year>.json), so a stopped run carries on where it left off.

What is kept, and where (both are private-tier sources, see private.py):

- croi_abstracts_index: metadata only (number, year, title, authors as published, session, links, our own
  rule-based tags). Never the abstract text, never a poster.
- croi_abstracts_text: the abstract body, for short excerpts to the fellow with CROI's copyright line and link.

Raw pages are not kept anywhere (only their sha256, in the fetch log and on each row); poster PDFs and figures are
never requested. This never runs inside the MCP server.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from health_data_registry import private
from health_data_registry.adapters import croi
from health_data_registry.fetch import FetchRefused
from health_data_registry.polite import CrawlStopped, PoliteClient, https_getter
from health_data_registry.registry import Registry
from health_data_registry.store import FetchLog

INDEX_ID = "croi_abstracts_index"
TEXT_ID = "croi_abstracts_text"


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
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(cp), indent=1) + "\n", encoding="utf-8")
    tmp.replace(p)


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
    stop_code: str | None = (
        None  # budget_spent (the --max-requests limit), robots_*, fetch_refused, layout_changed
    )

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


def index_croi(
    registry: Registry,
    lake: Path,
    year: int,
    *,
    max_requests: int | None = None,
    client: PoliteClient | None = None,
) -> RunSummary:
    croi.check_year(year)
    ds_index, ds_text = registry.get(INDEX_ID), registry.get(TEXT_ID)
    private.require_private(ds_index)
    private.require_private(ds_text)
    client = client or PoliteClient(
        croi.BASE,
        state_path=lake / "crawl_state" / f"{croi.HOST}.json",
        get_raw=https_getter(croi.HOST),
        log=FetchLog(lake, INDEX_ID),
        max_requests=max_requests,
    )
    cp = load_checkpoint(lake, year)
    read = 0
    stopped: str | None = None
    code: str | None = None
    partition = f"year={year}"
    try:
        while not cp.complete:
            if not cp.pending:
                if cp.pages is not None and cp.next_page > cp.pages:
                    cp.complete = True
                    save_checkpoint(lake, cp)
                    break
                url = croi.listing_url(year, cp.next_page)
                r = client.get(url)
                listing = croi.parse_listing(r.content, url, year=year)
                if year not in listing.years:
                    raise croi.LayoutError(url, f"{year} is not offered in the year filter")
                cp.pages, cp.total = listing.pages, listing.total
                done = set(cp.done)
                cp.pending = [asdict(e) for e in listing.entries if e.page_url not in done]
                cp.next_page += 1
                if not listing.entries:
                    cp.complete = True
                save_checkpoint(lake, cp)
                continue
            entry = croi.ListingEntry(**cp.pending[0])
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
    return RunSummary(
        year=year,
        requests=client.requests_made,
        abstracts_read=read,
        abstracts_done=len(cp.done),
        pages=cp.pages,
        total=cp.total,
        complete=cp.complete,
        stopped=stopped,
        stop_code=code,
    )


def full_year_estimate(total: int, delay_s: float) -> float:
    """Hours to index a year: one listing page per 10 abstracts plus one page per abstract, plus robots.txt."""
    requests = 1 + -(-total // croi.PER_PAGE) + total
    return requests * delay_s / 3600
