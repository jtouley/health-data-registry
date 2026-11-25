"""Fetch one catalogued source from its publisher into a lake: every attempt logged, raw bytes kept, one snapshot.

Used by `health-data-registry refresh` / `fetch` (the maintainers' lake) and by the server for sources whose
nothing else: a source whose licence forbids redistribution (WHO GHO) is never written to any lake; the server
reads it live (live.py).
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable, Mapping
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
# The longest Retry-After a run waits out (a 429 or 503 from a rate-limited API); a longer one stops the run in
# plain words so it can be run again later.
MAX_RETRY_AFTER_S = 300.0

Getter = Callable[[str], RawFetch]


class LiveOnly(ValueError):
    """The source's terms forbid storing it; the server reads it live instead."""


class PrivateTier(LiveOnly):
    """The source is kept in its own private store, built by its own command (index-croi, harvest-doi)."""


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
        min_interval_s: Mapping[str, float] | None = None,
    ) -> None:
        self.hosts, self.sleep, self.clock, self.budget = hosts, sleep, clock, budget_s
        # Per-host floor on the gap between requests, from an API's own published rate limit (e.g. Crossref's
        # public pool: one list query a second); robots.txt's crawl delay wins when it asks for more.
        self.min_interval = dict(min_interval_s or {})
        self.rules: dict[str, RobotFileParser] = {}
        self.last: dict[str, float] = {}
        self.waited = 0.0

    def _rules(self, host: str) -> RobotFileParser | None:
        from health_data_registry.fetch import FetchRefused, fetch

        if host in self.rules:
            return self.rules[host]
        rp = RobotFileParser()
        try:
            r = fetch(f"https://{host}/robots.txt", allowed_hosts=self.hosts, timeout=ROBOTS_TIMEOUT_S)
            status = r.status
        except FetchRefused as e:  # the fetcher raises for every status but 200
            if e.status is None:
                return None  # can't be read now (timeout, refused connection): asked again on the next fetch
            status = e.status
        if status == 200:
            rp.parse(r.content.decode("utf-8", "replace").splitlines())
        elif status in (401, 403):
            rp.parse(["User-agent: *", "Disallow: /"])
        elif 400 <= status < 500 and status != 429:
            rp.parse([])  # no robots.txt (404, 410, ...): no rules
        else:
            return None  # busy or failing (429, 5xx): asked again on the next fetch
        self.rules[host] = rp
        return rp

    def wait_turn(self, url: str) -> None:
        """Refuse a path robots.txt disallows; otherwise wait out the host's crawl delay, within the budget."""
        from health_data_registry.fetch import FetchRefused

        host = (urlsplit(url).hostname or "").lower()
        rp = self._rules(host)
        if rp is not None and not rp.can_fetch(ROBOTS_AGENT, url):
            raise FetchRefused(
                "robots_disallowed",
                f"The publisher's robots.txt at {host} asks automated tools not to read this address, so it was "
                "not fetched.",
            )
        # An API's own rate limit applies even while its robots.txt can't be read.
        crawl_delay = float(rp.crawl_delay(ROBOTS_AGENT) or 0) if rp is not None else 0.0
        delay = max(crawl_delay, self.min_interval.get(host, 0.0))
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


def fetcher(
    registry: Registry,
    log: FetchLog | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    robots: Robots | None | bool = True,
    max_retry_after_s: float = MAX_RETRY_AFTER_S,
) -> Getter:
    """A getter for catalogued publisher URLs that honours robots.txt, retries slow publishers and logs every
    attempt. `robots=True` (the default) reads each host's robots.txt; tests pass False or their own Robots.

    A 429 or 503 is retried after the publisher's Retry-After (or a doubling back-off when it gives none); a
    Retry-After longer than ``max_retry_after_s`` stops the fetch instead of waiting."""
    from health_data_registry.fetch import FetchRefused, fetch

    hosts = registry.hosts
    policy = Robots(hosts, sleep=sleep) if robots is True else robots or None

    def get(url: str) -> RawFetch:
        for attempt in range(RETRIES):
            # Every attempt, retries included, waits its turn: a retry never comes sooner than the crawl delay.
            if policy is not None:
                try:
                    policy.wait_turn(url)
                except FetchRefused as e:
                    if log is not None:
                        log.record(url, ok=False, when=datetime.now(UTC), error=str(e))
                    raise
            when = datetime.now(UTC)
            try:
                r = fetch(
                    url, allowed_hosts=hosts, headers={"Accept": "application/json"}, timeout=FETCH_TIMEOUT_S
                )
            except FetchRefused as e:
                if log is not None:
                    log.record(url, ok=False, when=when, http_status=e.status, error=str(e))
                # A slow, busy or briefly unreachable publisher is retried; a refused URL or host never is.
                rate_limited = e.code == "http_error" and e.status in (429, 503)
                if (
                    e.code not in ("timeout", "connect_failed") and not rate_limited
                ) or attempt == RETRIES - 1:
                    raise
                wait = e.retry_after if rate_limited and e.retry_after is not None else 2 ** (attempt + 1)
                if wait > max_retry_after_s:
                    raise FetchRefused(
                        "rate_limited",
                        f"The publisher asked us to wait {wait:.0f} seconds before asking again, longer than this "
                        "run waits, so it stopped. Run it again later.",
                        status=e.status,
                        retry_after=wait,
                    ) from e
                sleep(wait)
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

    if ds.entry.tier == "private":
        raise PrivateTier(
            f"{ds.id} is kept only in its private store (lake/private/{ds.id}/), never in the public lake; "
            + (
                "build it with `health-data-registry index-croi --year <year>`"
                if ds.entry.source.portal == "croi"
                else f"build it with `health-data-registry harvest-doi {ds.id.removesuffix('_abstracts')} --year <year>`"
                if ds.entry.source.portal == "doi"
                else "it has no crawl command yet"
            )
        )
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
