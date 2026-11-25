"""Fetch one catalogued source from its publisher into a lake: every attempt logged, raw bytes kept, one snapshot.

Used by `health-data-registry refresh` / `fetch` (the maintainers' lake) and by the server for sources whose
nothing else: a source whose licence forbids redistribution (WHO GHO) is never written to any lake; the server
reads it live (live.py).
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from health_data_registry.registry import Dataset, Registry
from health_data_registry.store import FetchLog, Manifest, RawFetch, build_snapshot, write_ndjson, write_raw

RETRIES = 4
FETCH_TIMEOUT_S = 180.0
ROBOTS_TIMEOUT_S = 30.0
# The most a run will wait in total to honour publishers' robots.txt crawl delays; a robots.txt asking for
# more than that is refused in plain words rather than stalling a refresh or a clinician's question.
CRAWL_DELAY_BUDGET_S = 240.0
ROBOTS_AGENT = "health-data-registry"
# What each portal's pages are. APIs answer JSON; a PDF-report source serves an HTML index and PDF reports.
_ACCEPT = {"pdf_report": "text/html, application/pdf;q=0.9, */*;q=0.8"}
_ACCEPT_DEFAULT = "application/json"

Getter = Callable[[str], RawFetch]


class LiveOnly(ValueError):
    """The source's terms forbid storing it; the server reads it live instead."""


class Robots:
    """Each publisher host's robots.txt, read once per run: which paths may be fetched and how far apart.

    A robots.txt that can't be found (404) allows everything, as the standard says (RFC 9309); one the host
    refuses to show (401, 403) disallows everything; one that can't be read (a timeout, a 5xx) is retried on the
    next fetch and nothing is assumed until then except that the fetch itself goes ahead.
    """

    def __init__(
        self,
        hosts: frozenset[str],
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        budget_s: float = CRAWL_DELAY_BUDGET_S,
        pace: Callable[[str], None] | None = None,
    ) -> None:
        self.hosts, self.sleep, self.clock, self.budget = hosts, sleep, clock, budget_s
        self.pace = pace  # the host pacing every request keeps, robots.txt included
        self.rules: dict[str, RobotFileParser] = {}
        self.last: dict[str, float] = {}
        self.waited = 0.0

    def _rules(self, host: str) -> RobotFileParser | None:
        from health_data_registry.fetch import FetchRefused, fetch

        if host in self.rules:
            return self.rules[host]
        rp = RobotFileParser()
        url = f"https://{host}/robots.txt"
        if self.pace is not None:
            self.pace(url)
        try:
            r = fetch(url, allowed_hosts=self.hosts, timeout=ROBOTS_TIMEOUT_S)
        except FetchRefused:
            return None  # can't be read now: asked again on the next fetch
        if r.status == 200:
            rp.parse(r.content.decode("utf-8", "replace").splitlines())
        elif r.status in (401, 403):
            rp.parse(["User-agent: *", "Disallow: /"])
        elif 400 <= r.status < 500:
            rp.parse([])
        else:
            return None
        self.rules[host] = rp
        return rp

    def wait_turn(self, url: str) -> None:
        """Refuse a path robots.txt disallows; otherwise wait out the host's crawl delay, within the budget."""
        from health_data_registry.fetch import FetchRefused

        host = (urlsplit(url).hostname or "").lower()
        rp = self._rules(host)
        if rp is None:
            return
        if not rp.can_fetch(ROBOTS_AGENT, url):
            raise FetchRefused(
                "robots_disallowed",
                f"The publisher's robots.txt at {host} asks automated tools not to read this address, so it was "
                "not fetched.",
            )
        delay = float(rp.crawl_delay(ROBOTS_AGENT) or 0)
        if host in self.last and delay > 0:
            due = self.last[host] + delay - self.clock()
            if due > 0:
                if self.waited + due > self.budget:
                    raise FetchRefused(
                        "crawl_delay_budget",
                        f"The publisher's robots.txt at {host} asks for {delay:g} seconds between requests, more "
                        "than this run may wait in total, so the rest was not fetched. Try again later.",
                    )
                self.sleep(due)
                self.waited += due
        self.last[host] = self.clock()


class Pacer:
    """The least time between two requests to one host, as each source's catalog entry declares
    (source.min_interval_s; 10 s for the county and federal report pages read here). Applied to every request,
    retries and robots.txt included, whether or not robots.txt asks for a delay.

    Safe to share between threads: each request reserves its slot under a lock, then sleeps outside it. The
    server keeps one per process (`process_pacer`), so two questions never reach a host back to back.
    """

    def __init__(
        self,
        intervals: dict[str, float],
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.intervals, self.sleep, self.clock = intervals, sleep, clock
        self.last: dict[str, float] = {}
        self._lock = threading.Lock()

    @classmethod
    def for_registry(cls, registry: Registry, *, sleep: Callable[[float], None] = time.sleep) -> Pacer:
        intervals: dict[str, float] = {}
        for ds in registry:
            host = ds.entry.source.host
            intervals[host] = max(intervals.get(host, 0.0), ds.entry.source.min_interval_s)
        return cls({h: s for h, s in intervals.items() if s > 0}, sleep=sleep)

    def wait(self, url: str, *, deadline: float | None = None) -> None:
        """Wait for this host's next free slot. With a `deadline` (on this pacer's clock), a slot after it is
        refused in plain words instead of waited for."""
        from health_data_registry.fetch import FetchRefused

        host = (urlsplit(url).hostname or "").lower()
        gap = self.intervals.get(host)
        if gap is None:
            return
        with self._lock:
            now = self.clock()
            slot = max(now, self.last[host] + gap) if host in self.last else now
            if deadline is not None and slot > deadline:
                raise FetchRefused(
                    "time_budget",
                    f"Reading {host} paces its requests at least {gap:g} seconds apart, and the next one would "
                    "come after the time one question may wait, so the read stopped there.",
                )
            self.last[host] = slot
        if slot > now:
            self.sleep(slot - now)


_PROCESS_PACER: Pacer | None = None
_PROCESS_LOCK = threading.Lock()


def process_pacer(registry: Registry) -> Pacer:
    """The one Pacer this process shares across every live read, so host pacing holds between questions."""
    global _PROCESS_PACER
    with _PROCESS_LOCK:
        fresh = Pacer.for_registry(registry)
        if _PROCESS_PACER is None:
            _PROCESS_PACER = fresh
        else:
            for host, gap in fresh.intervals.items():
                _PROCESS_PACER.intervals[host] = max(_PROCESS_PACER.intervals.get(host, 0.0), gap)
        return _PROCESS_PACER


def fetcher(
    registry: Registry,
    log: FetchLog | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    robots: Robots | None | bool = True,
    pacer: Pacer | None = None,
    budget_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Getter:
    """A getter for catalogued publisher URLs that honours robots.txt, retries slow publishers and logs every
    attempt. `robots=True` (the default) reads each host's robots.txt; tests pass False or their own Robots.

    `budget_s` bounds the whole read, from this getter's creation: pacing waits, request timeouts and retries all
    stop at it, and a request that can't start in time is refused in plain words (a live read per question).
    """
    from health_data_registry.fetch import FetchRefused, fetch

    hosts = registry.hosts
    pace = pacer if pacer is not None else Pacer.for_registry(registry, sleep=sleep)
    deadline = None if budget_s is None else clock() + budget_s

    def paced(url: str) -> None:
        pace.wait(url, deadline=deadline)

    policy = Robots(hosts, sleep=sleep, pace=paced) if robots is True else robots or None
    accept = {ds.entry.source.host: _ACCEPT.get(ds.entry.source.portal, _ACCEPT_DEFAULT) for ds in registry}

    def timeout_left() -> float:
        if deadline is None:
            return FETCH_TIMEOUT_S
        left = deadline - clock()
        if left <= 0:
            raise FetchRefused(
                "time_budget",
                "The publisher took longer than one question may wait, so the read stopped there.",
            )
        return min(FETCH_TIMEOUT_S, left)

    def get(url: str) -> RawFetch:
        if policy is not None:
            try:
                policy.wait_turn(url)
            except FetchRefused as e:
                if log is not None:
                    log.record(url, ok=False, when=datetime.now(UTC), error=str(e))
                raise
        for attempt in range(RETRIES):
            paced(url)
            when = datetime.now(UTC)
            host = (urlsplit(url).hostname or "").lower()
            try:
                r = fetch(
                    url,
                    allowed_hosts=hosts,
                    headers={"Accept": accept.get(host, _ACCEPT_DEFAULT)},
                    timeout=timeout_left(),
                )
            except FetchRefused as e:
                if log is not None:
                    log.record(url, ok=False, when=when, error=str(e))
                # A slow or briefly unreachable publisher is retried; a refused URL or host never is.
                if e.code not in ("timeout", "connect_failed") or attempt == RETRIES - 1:
                    raise
                sleep(2 ** (attempt + 1))
                continue
            if r.status != 200:
                msg = f"the publisher answered with status {r.status} instead of the data"
                if log is not None:
                    log.record(url, ok=False, when=when, http_status=r.status, size=len(r.content), error=msg)
                raise RuntimeError(f"{url}: {msg}")
            if log is not None:
                log.record(
                    url, ok=True, when=when, http_status=r.status, size=len(r.content), sha256=r.sha256
                )
            return RawFetch(url=r.final_url, content=r.content, sha256=r.sha256, retrieved_at=r.retrieved_at)
        raise RuntimeError(f"{url}: no response")  # pragma: no cover - the loop returns or raises

    return get


def refresh_dataset(ds: Dataset, lake: Path, get: Getter, *, max_rows: int | None = None) -> Manifest:
    """Page through one source, keep its raw bytes, and build its snapshot (partial when max_rows is set)."""
    from health_data_registry.adapters import pages

    if not ds.entry.licence.redistribution:
        raise LiveOnly(
            f"{ds.id} is read live from {ds.entry.publisher} only: its terms ({ds.entry.licence.id}) attach to "
            "anything derived, so no copy is ever stored"
        )
    lake.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        fetches, files = [], []
        fields = sorted({c.source_field for c in ds.contract.columns})
        for i, page in enumerate(pages(ds.entry, get, max_rows=max_rows, fields=fields)):
            write_raw(lake, [page.fetch])  # keep the bytes on disk now, not in memory
            f = page.fetch
            fetches.append(RawFetch(url=f.url, content=b"", sha256=f.sha256, retrieved_at=f.retrieved_at))
            nd = Path(tmp) / f"page-{i:05d}.ndjson"
            write_ndjson(nd, page.records)
            files.append(nd)
        return build_snapshot(lake, ds, fetches, files, partial=max_rows is not None)
