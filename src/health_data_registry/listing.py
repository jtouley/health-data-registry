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
  a snapshot of part of a crawl, and that snapshot is marked partial. Every partition is crawled before the run
  says it is incomplete, and listed items the publisher says are gone (404, 410) are recorded, named in that
  message and asked for again on the next run, without walking the listing again.
- **Tied to how it was read.** A checkpoint records the method digest (adapters.method_digest) its items were read
  with; one of another method is discarded and the partition crawled again. A finished partition checks its
  listing's total once a run, and opens again when it has changed; a total that moves during a crawl restarts the
  listing walk, keeping the items already read.
- **The pace survives restarts.** One crawler per host for this user (``access.HostLock``); the time of each
  request is written before it is sent (``access.PaceStore``), at least the source's ``min_interval_s``, 10 s,
  and robots.txt's delay; redirects are not followed silently: one call is one request, and a redirect is
  followed only to an item page inside the source's scope, as a request of its own.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
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
    redact_url,
)
from health_data_registry.adapters import LayoutError, Page, Read, with_document
from health_data_registry.adapters.html_table import ListingItem, check_item, item_records, listing_page
from health_data_registry.fetch import FetchRefused
from health_data_registry.source_config import ListingConfig
from health_data_registry.store import RawFetch, write_raw

# A listing page or item page the publisher says is gone: the listing is incomplete, not refused.
GONE = frozenset({404, 410})
LISTING_FLOOR_S = 10.0  # a listing crawl is long: every host it reads is paced, government host or not


class CrawlIncomplete(RuntimeError):
    """The crawl stopped before every partition matched its listing total; its checkpoints are kept."""

    code = "crawl_incomplete"


_ITEM_FILE = re.compile(r"[0-9a-f]{32}\.json")
MAX_RESETS = (
    3  # walks of one partition's listing restarted in one run (its total changed) before it waits a run
)


@dataclass
class Checkpoint:
    source: str
    partition: str
    next_page: int
    # The method digest (adapters.method_digest) the items were read with: a checkpoint of another method is
    # discarded, so a snapshot's records are always read the way its manifest says.
    method: str = ""
    total: int | None = None
    listed: int = 0  # item links seen on the listing so far
    listing_done: bool = False
    complete: bool = False
    done: dict[str, str] = field(default_factory=dict)  # item URL -> its item file name, in crawl order
    # Listed items the publisher answered 404 or 410 for: URL -> {"status", "text", "year"}; asked for again on
    # every run, and named in the refusal, until they come back or a maintainer acts.
    gone: dict[str, dict[str, Any]] = field(default_factory=dict)


def partitions(cfg: ListingConfig) -> list[tuple[int, str | None]]:
    years = range(cfg.years.first, cfg.years.last + 1)
    sessions: list[str | None] = list(cfg.sessions) or [None]
    return [(y, s) for y in years for s in sessions]


def _partition_name(year: int, session: str | None) -> str:
    return str(year) if session is None else f"{year}-{re.sub(r'[^A-Za-z0-9_-]+', '_', session)}"


def _item_name(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32] + ".json"


class ListingState:
    """Checkpoints and item files of one source's listing crawl, in the lake.

    Restored state is checked before it is trusted, as access.PaceStore checks a saved pace: a checkpoint that
    can't be read, names another crawl, or points at an item file by any name but its URL's is refused in plain
    words; one written by another method is discarded with its items, and the partition is crawled again.
    """

    def __init__(self, lake: Path, source: str) -> None:
        self.root = lake / "crawl_state" / "listing" / source
        self.lake = lake
        self.source = source

    def _dir(self, partition: str) -> Path:
        return self.root / partition

    def load(self, partition: str, first_page: int, method: str = "") -> Checkpoint:
        p = self._dir(partition) / "checkpoint.json"
        fresh = Checkpoint(source=self.source, partition=partition, next_page=first_page, method=method)
        if not p.is_file():
            return fresh
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            ck = Checkpoint(**raw) if isinstance(raw, dict) else None
        except (ValueError, TypeError):
            ck = None
        if ck is None:
            raise LayoutError(
                f"the saved crawl checkpoint {p} can't be read, so it was not reused; delete that folder to crawl "
                "the partition again"
            )
        if ck.source != self.source or ck.partition != partition:
            raise LayoutError(f"the checkpoint {p} was written for another crawl, so it was not reused")
        bad = [
            u for u, name in ck.done.items() if not _ITEM_FILE.fullmatch(str(name)) or name != _item_name(u)
        ]
        if bad:
            raise LayoutError(
                f"the checkpoint {p} names item files that are not its items' own, so it was not reused; delete "
                "that folder to crawl the partition again"
            )
        if ck.method != method:
            shutil.rmtree(self._dir(partition))  # read another way: its items would mislabel the snapshot
            return fresh
        return ck

    def save(self, ck: Checkpoint) -> None:
        atomic_write(
            self._dir(ck.partition) / "checkpoint.json", json.dumps(asdict(ck), sort_keys=True) + "\n"
        )

    def save_item(self, partition: str, url: str, fetch: RawFetch, records: list[dict[str, Any]]) -> str:
        name = _item_name(url)
        doc = {
            "url": url,
            "sha256": fetch.sha256,
            "retrieved_at": fetch.retrieved_at.isoformat(),
            "records": records,
        }
        atomic_write(self._dir(partition) / "items" / name, json.dumps(doc, sort_keys=True) + "\n")
        return name

    def item(self, partition: str, name: str) -> tuple[RawFetch, list[dict[str, Any]]]:
        p = self._dir(partition) / "items" / name
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            fetch = RawFetch(url=str(doc["url"]), content=b"", sha256=str(doc["sha256"]),
                             retrieved_at=datetime.fromisoformat(doc["retrieved_at"]))  # fmt: skip
            records = [dict(r) for r in doc["records"]]
        except (OSError, ValueError, TypeError, KeyError) as e:
            raise LayoutError(
                f"the saved item {p} can't be read; delete its partition folder to crawl it again"
            ) from e
        if _item_name(fetch.url) != name or not (self.lake / "raw" / fetch.sha256).is_file():
            raise LayoutError(
                f"the saved item {p} does not match its URL or its raw page is missing, so it was not reused"
            )
        return fetch, records


class PacedGetter:
    """One request per call at the persisted pace, a redirect followed only into the source's own item pages."""

    def __init__(
        self, read: Read, lake: Path, *, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> None:
        self.read, self.clock, self.sleep = read, clock, sleep
        self.store = PaceStore(lake, clock=clock)

    def _pace(self, host: str) -> float:
        """The pace today's policy asks for: the saved state gives only when the last request went out, so a
        lowered ``min_interval_s`` or Crawl-delay takes effect on the next run."""
        robots = self.read.pace_for(host) if self.read.pace_for is not None else None
        return pace_s(floor=LISTING_FLOOR_S, crawl_delay=robots, request_rate=None,
                      api_interval=None, source_min=self.read.entry.source.min_interval_s)  # fmt: skip

    def __call__(self, url: str, *, redirects: int = 1) -> RawFetch:

        host = (normalise(url).split("/")[2]).lower()
        state = self.store.load(host)
        pace = self._pace(host)
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
                raise LayoutError(
                    f"{redact_url(url)} redirects to {redact_url(location)}, outside this source's pages"
                ) from e
            return self(normalise(location), redirects=redirects - 1)


def crawl_pages(
    read: Read, *, clock: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None
) -> Iterator[Page]:
    """Crawl (or resume) every partition, then hand on one page per item, in crawl order.

    Raises CrawlIncomplete, with its checkpoints kept, unless every partition matched its listing total, or the
    read is a ``--max-rows`` sample (a partial snapshot). Every partition is crawled before it is raised, so one
    partition that can't finish never holds the others back. ``--max-rows`` counts item pages; each item page is
    one record unless a ``parse`` extension says otherwise.
    """
    from health_data_registry.adapters import method_digest

    entry = read.entry
    if read.live or read.lake is None:
        raise ValueError(
            f"{entry.id} is crawled page by page over hours by the maintainers' refresh, never read live for a question"
        )
    cfg = read.config.listing
    assert cfg is not None  # noqa: S101 - shape listing requires it
    state = ListingState(read.lake, entry.id)
    get = PacedGetter(read, read.lake, clock=clock or time.time, sleep=sleep or time.sleep)
    method = method_digest(entry)
    budget = read.max_rows
    order: list[tuple[str, str]] = []
    short: list[str] = []
    with HostLock(entry.source.host):
        for year, session in partitions(cfg):
            part = _partition_name(year, session)
            ck = state.load(part, cfg.first_page, method)
            budget, why = _crawl_partition(read, cfg, state, get, ck, year, session, budget)
            if why is not None:
                short.append(why)
            order += [(part, name) for name in ck.done.values()]
            if budget is not None and budget <= 0:
                break
    if short and read.max_rows is None:
        raise CrawlIncomplete(
            "; ".join(short)
            + ". No snapshot was built; the crawl resumes from its checkpoints on the next run"
        )
    if read.max_rows is not None:
        order = order[: read.max_rows]
    for part, name in order:
        fetch, records = state.item(part, name)
        yield Page(fetch=fetch, records=records)


def _read_item(read: Read, cfg: ListingConfig, state: ListingState, get: PacedGetter, ck: Checkpoint,
               listed: ListingItem, year: int) -> bool:  # fmt: skip
    """Fetch, check and keep one item page; False when the publisher says it is gone (recorded on the checkpoint)."""
    entry, link = read.entry, listed.url
    try:
        item = get(link)
    except FetchRefused as e:
        if e.status not in GONE:
            raise
        ck.gone[link] = {"status": e.status, "text": listed.text, "year": listed.year}
        state.save(ck)
        return False
    read_from_page = item_records(entry, cfg, item.content, link)
    check_item(entry, cfg, listed, year, read_from_page)
    records = with_document(read_from_page, item)
    write_raw(read.lake, [item])  # type: ignore[arg-type] # data first ...
    ck.done[link] = state.save_item(ck.partition, link, item, records)
    ck.gone.pop(link, None)
    state.save(ck)  # ... checkpoint last
    return True


def _restart_walk(cfg: ListingConfig, ck: Checkpoint) -> None:
    ck.listing_done, ck.complete, ck.listed, ck.next_page = False, False, 0, cfg.first_page


def _crawl_partition(read: Read, cfg: ListingConfig, state: ListingState, get: PacedGetter, ck: Checkpoint,
                     year: int, session: str | None,
                     budget: int | None) -> tuple[int | None, str | None]:  # fmt: skip
    """Crawl one partition from its checkpoint; returns the item budget left (None: no limit) and, when the
    partition ended short of its listing's total, why."""
    entry = read.entry

    def url_of(page: int) -> str:
        return cfg.url_template.format(host=entry.source.host, year=year, page=page,
                                       session=quote(session or "", safe=""))  # fmt: skip

    def left() -> int | None:
        return None if budget is None else budget - len(ck.done)

    if ck.complete and read.max_rows is None:
        # A finished partition is checked against its listing once a run (one request): items added since, or a
        # changed total, open it again; its items already read are kept.
        total, _items = listing_page(entry, cfg, get(url_of(cfg.first_page)).content, url_of(cfg.first_page))
        if total != ck.total:
            ck.total = total
            _restart_walk(cfg, ck)
            state.save(ck)
    if ck.complete:
        return left(), None
    if ck.listing_done and ck.gone and len(ck.done) + len(ck.gone) >= (ck.total or 0):
        # The walk found every item; only items that were gone are missing: ask for those again, not the listing.
        for url, seen in list(ck.gone.items()):
            if budget is not None and len(ck.done) >= budget:
                return 0, None
            _read_item(
                read,
                cfg,
                state,
                get,
                ck,
                ListingItem(url, str(seen.get("text") or ""), seen.get("year")),
                year,
            )
    pages_read = resets = 0
    while not ck.listing_done:
        if budget is not None and budget - len(ck.done) <= 0:
            return 0, None
        url = url_of(ck.next_page)
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
            # The publisher's count moved while the crawl ran (items added or withdrawn): walk the listing again
            # from its first page under the new total, keeping the items already read.
            resets += 1
            ck.total = total
            _restart_walk(cfg, ck)
            state.save(ck)
            if resets > MAX_RESETS:
                return left(), (
                    f"the {ck.partition} listing changed its number of results {resets} times during this run"
                )
            continue
        ck.total = total
        stopped = False
        for listed in items:
            if listed.url in ck.done:
                continue
            if budget is not None and len(ck.done) >= budget:
                stopped = True  # a sample stops part way through a page: the page is walked again next run
                break
            _read_item(read, cfg, state, get, ck, listed, year)
        if stopped:
            state.save(ck)
            return 0, None
        ck.listed += len(items)
        ck.next_page += 1
        pages_read += 1
        ck.listing_done = not items or len(items) < cfg.page_size or ck.listed >= total
        state.save(ck)
        if pages_read > cfg.max_pages:
            raise LayoutError(f"{entry.id}: the {ck.partition} listing ran past {cfg.max_pages} pages")
    if budget is not None and len(ck.done) >= budget:
        return 0, None
    if len(ck.done) != ck.total:
        found, expected = len(ck.done), ck.total
        if not (ck.gone and found + len(ck.gone) >= (expected or 0)):
            _restart_walk(cfg, ck)  # walk the listing again next run
        state.save(ck)
        gone = sorted(ck.gone)
        named = (
            f"; the publisher says {len(gone)} listed item page{'s are' if len(gone) > 1 else ' is'} gone: "
            + ", ".join(redact_url(u) for u in gone[:5])
            + (" and more" if len(gone) > 5 else "")
            if gone
            else ""
        )
        return (
            left(),
            f"{entry.id}: the {ck.partition} listing shows {expected} results but {found} item pages were read{named}",
        )
    ck.complete = True
    state.save(ck)
    return left(), None
