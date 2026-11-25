"""The resumable listing crawl (html_table, shape listing): a search listing, then one page per item, for days.

A listing crawl can take a day or two at a site's pace (one request every 120 s is about 47 hours for 1,300 items),
so it is built to stop and resume without losing or repeating work, and to build a snapshot only when it is whole:

- **Partitions.** One per year, or per year and session when the listing filters by session. Each has its own
  checkpoint in the lake (``crawl_state/listing/<source>/<partition>/``), never in git (hard rule 5).
- **Data first, checkpoint last.** An item's raw bytes go to the hash-addressed raw store, then its parsed records
  to its own file (written whole: temporary file, fsync, rename), and only then is the item marked done in the
  checkpoint. A crash between the two leaves the item undone, so it is fetched again; never a checkpoint that
  points at data that isn't there.
- **Complete or nothing.** A partition is complete only when the items crawled equal the listing's own total. A
  crawl that ends short (interrupted, or the listing and its items disagree) builds no snapshot: it stops with
  ``CrawlIncomplete``, its checkpoints kept, and the next run resumes where it stopped. Only ``--max-rows`` builds
  a snapshot of part of a crawl, and that snapshot is marked partial.
- **The pace survives restarts.** One crawler per host for this user (``access.HostLock``); the time of each
  request is written before it is sent (``access.PaceStore``), at least the source's ``min_interval_s``, 10 s,
  and robots.txt's delay; redirects are not followed silently: one call is one request, and a redirect is
  followed only to an item page inside the source's scope, as a request of its own.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from health_data_registry.access import (
    HostLock,
    PaceState,
    PaceStore,
    atomic_write,
    in_scope,
    normalise,
    pace_s,
)
from health_data_registry.adapters import LayoutError, Page, Read, with_document
from health_data_registry.adapters.html_table import check_item, item_records, listing_page
from health_data_registry.fetch import FetchRefused
from health_data_registry.source_config import ListingConfig
from health_data_registry.store import RawFetch, write_raw

# A listing page or item page the publisher says is gone: the listing is incomplete, not refused.
GONE = frozenset({404, 410})
LISTING_FLOOR_S = 10.0  # a listing crawl is long: every host it reads is paced, government host or not


class CrawlIncomplete(RuntimeError):
    """The crawl stopped before every partition matched its listing total; its checkpoints are kept."""

    code = "crawl_incomplete"


@dataclass
class Checkpoint:
    source: str
    partition: str
    next_page: int
    total: int | None = None
    listed: int = 0  # item links seen on the listing so far
    listing_done: bool = False
    complete: bool = False
    done: dict[str, str] = field(default_factory=dict)  # item URL -> its item file name, in crawl order


def partitions(cfg: ListingConfig) -> list[tuple[int, str | None]]:
    years = range(cfg.years.first, cfg.years.last + 1)
    sessions: list[str | None] = list(cfg.sessions) or [None]
    return [(y, s) for y in years for s in sessions]


def _partition_name(year: int, session: str | None) -> str:
    return str(year) if session is None else f"{year}-{re.sub(r'[^A-Za-z0-9_-]+', '_', session)}"


class ListingState:
    """Checkpoints and item files of one source's listing crawl, in the lake."""

    def __init__(self, lake: Path, source: str) -> None:
        self.root = lake / "crawl_state" / "listing" / source
        self.source = source

    def _dir(self, partition: str) -> Path:
        return self.root / partition

    def load(self, partition: str, first_page: int) -> Checkpoint:
        p = self._dir(partition) / "checkpoint.json"
        if not p.is_file():
            return Checkpoint(source=self.source, partition=partition, next_page=first_page)
        raw = json.loads(p.read_text(encoding="utf-8"))
        if raw.get("source") != self.source or raw.get("partition") != partition:
            raise LayoutError(f"the checkpoint {p} was written for another crawl, so it was not reused")
        return Checkpoint(**raw)

    def save(self, ck: Checkpoint) -> None:
        atomic_write(
            self._dir(ck.partition) / "checkpoint.json", json.dumps(asdict(ck), sort_keys=True) + "\n"
        )

    def save_item(self, partition: str, url: str, fetch: RawFetch, records: list[dict[str, Any]]) -> str:
        name = hashlib.sha256(url.encode()).hexdigest()[:32] + ".json"
        doc = {
            "url": url,
            "sha256": fetch.sha256,
            "retrieved_at": fetch.retrieved_at.isoformat(),
            "records": records,
        }
        atomic_write(self._dir(partition) / "items" / name, json.dumps(doc, sort_keys=True) + "\n")
        return name

    def item(self, partition: str, name: str) -> tuple[RawFetch, list[dict[str, Any]]]:
        doc = json.loads((self._dir(partition) / "items" / name).read_text(encoding="utf-8"))
        fetch = RawFetch(url=doc["url"], content=b"", sha256=doc["sha256"],
                         retrieved_at=datetime.fromisoformat(doc["retrieved_at"]))  # fmt: skip
        return fetch, list(doc["records"])


class PacedGetter:
    """One request per call at the persisted pace, a redirect followed only into the source's own item pages."""

    def __init__(
        self, read: Read, lake: Path, *, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> None:
        self.read, self.clock, self.sleep = read, clock, sleep
        self.store = PaceStore(lake, clock=clock)

    def _pace(self, host: str, saved: PaceState) -> float:
        robots = self.read.pace_for(host) if self.read.pace_for is not None else None
        return pace_s(floor=LISTING_FLOOR_S, crawl_delay=robots, request_rate=None,
                      api_interval=saved.pace_s, source_min=self.read.entry.source.min_interval_s)  # fmt: skip

    def __call__(self, url: str, *, redirects: int = 1) -> RawFetch:

        host = (normalise(url).split("/")[2]).lower()
        state = self.store.load(host)
        pace = self._pace(host, state)
        if state.last_request_at is not None:
            while (left := state.last_request_at + pace - self.clock()) > 0:
                self.sleep(left)
        self.store.save(PaceState(host=host, last_request_at=self.clock(), pace_s=pace))  # before sending
        try:
            return self.read.get(url)
        except FetchRefused as e:
            location = getattr(e, "location", None)
            if e.code != "too_many_redirects" or location is None or redirects <= 0:
                raise
            if not in_scope(self.read.entry.source, location):
                raise LayoutError(f"{url} redirects to {location}, outside this source's pages") from e
            return self(location, redirects=redirects - 1)


def crawl_pages(
    read: Read, *, clock: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None
) -> Iterator[Page]:
    """Crawl (or resume) every partition, then hand on one page per item, in crawl order.

    Raises CrawlIncomplete, with its checkpoints kept, unless every partition matched its listing total, or the
    read is a ``--max-rows`` sample (a partial snapshot).
    """
    entry = read.entry
    if read.live or read.lake is None:
        raise ValueError(
            f"{entry.id} is crawled page by page over hours by the maintainers' refresh, never read live for a question"
        )
    cfg = read.config.listing
    assert cfg is not None  # noqa: S101 - shape listing requires it
    state = ListingState(read.lake, entry.id)
    get = PacedGetter(read, read.lake, clock=clock or time.time, sleep=sleep or time.sleep)
    budget = read.max_rows
    order: list[tuple[str, str]] = []
    with HostLock(entry.source.host):
        for year, session in partitions(cfg):
            part = _partition_name(year, session)
            ck = state.load(part, cfg.first_page)
            budget = _crawl_partition(read, cfg, state, get, ck, year, session, budget)
            order += [(part, name) for name in ck.done.values()]
            if budget is not None and budget <= 0:
                break
    if read.max_rows is not None:
        order = order[: read.max_rows]
    for part, name in order:
        fetch, records = state.item(part, name)
        yield Page(fetch=fetch, records=records)


def _crawl_partition(read: Read, cfg: ListingConfig, state: ListingState, get: PacedGetter, ck: Checkpoint,
                     year: int, session: str | None, budget: int | None) -> int | None:  # fmt: skip
    """Crawl one partition from its checkpoint; returns the item budget left (None: no limit)."""
    entry = read.entry
    pages_read = 0
    while not ck.complete and not ck.listing_done:
        if budget is not None and budget - len(ck.done) <= 0:
            return 0
        url = cfg.url_template.format(host=entry.source.host, year=year, page=ck.next_page,
                                      session=quote(session or "", safe=""))  # fmt: skip
        try:
            raw = get(url)
        except FetchRefused as e:
            if e.status not in GONE or ck.total is None:
                raise
            # The listing ran out before its own total: the crawl is incomplete, not refused (checked below).
            ck.listing_done = True
            state.save(ck)
            break
        total, items = listing_page(entry, cfg, raw.content, url)
        if ck.total is not None and total != ck.total:
            raise LayoutError(
                f"{entry.id}: the {ck.partition} listing now shows {total} results, not {ck.total}"
            )
        ck.total = total
        for listed in items:
            link = listed.url
            if link in ck.done:
                continue
            if budget is not None and len(ck.done) >= budget:
                break
            try:
                item = get(link)
            except FetchRefused as e:
                if e.status not in GONE:
                    raise
                continue  # a listed item that is gone leaves the crawl incomplete; it is asked for again next run
            read_from_page = item_records(entry, cfg, item.content, link)
            check_item(entry, cfg, listed, year, read_from_page)
            records = with_document(read_from_page, item)
            write_raw(read.lake, [item])  # type: ignore[arg-type] # data first ...
            ck.done[link] = state.save_item(ck.partition, link, item, records)
            state.save(ck)  # ... checkpoint last
        ck.listed += len(items)
        ck.next_page += 1
        pages_read += 1
        ck.listing_done = not items or len(items) < cfg.page_size or ck.listed >= total
        state.save(ck)
        if pages_read > cfg.max_pages:
            raise LayoutError(f"{entry.id}: the {ck.partition} listing ran past {cfg.max_pages} pages")
    if ck.complete:
        return None if budget is None else budget - len(ck.done)
    if budget is not None and len(ck.done) >= budget:
        return 0
    if len(ck.done) != ck.total:
        found, expected = len(ck.done), ck.total
        ck.listing_done, ck.listed = False, 0  # walk the listing again next run
        ck.next_page = cfg.first_page
        state.save(ck)
        raise CrawlIncomplete(
            f"{entry.id}: the {ck.partition} listing shows {expected} results but {found} item pages were read, so no "
            "snapshot was built; the crawl resumes from its checkpoint on the next run"
        )
    ck.complete = True
    state.save(ck)
    return None if budget is None else budget - len(ck.done)
