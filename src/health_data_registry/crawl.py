"""Fetch one catalogued source from its publisher into a lake: every attempt logged, raw bytes kept, one snapshot.

Used by `health-data-registry refresh` / `fetch` (the maintainers' lake) and by the server for sources whose
nothing else: a source whose licence forbids redistribution (WHO GHO) is never written to any lake; the server
reads it live (live.py).
"""

from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from health_data_registry.registry import Dataset, Registry

if TYPE_CHECKING:
    from health_data_registry.browser_fetch import BrowserResult
    from health_data_registry.fetch import FetchResult

from health_data_registry.store import FetchLog, Manifest, RawFetch, build_snapshot, write_ndjson, write_raw

logger = logging.getLogger(__name__)

RETRIES = 4
FETCH_TIMEOUT_S = 180.0
ROBOTS_TIMEOUT_S = 30.0
# The most a run will wait in total to honour publishers' robots.txt crawl delays; a robots.txt asking for
# more than that is refused in plain words rather than stalling a refresh or a clinician's question.
CRAWL_DELAY_BUDGET_S = 240.0
ROBOTS_AGENT = "health-data-registry"
# The least time between two requests to one host, on every host (the maintainer's access policy).
MIN_INTERVAL_S = 10.0

Getter = Callable[[str], RawFetch]
# The path of the answer that carries a Tableau view's data.
TABLEAU_DATA = "/bootstrapSession/"


class LiveOnly(ValueError):
    """The source's terms forbid storing it; the server reads it live instead."""


class Robots:
    """Each publisher host's robots.txt, read once per run, and the pace of requests to every host.

    Access policy (the maintainer's decision, recorded in the catalog as ``source.government_host``):

    - Every host: at least ``min_interval_s`` (10 s) between two requests, or the host's robots.txt crawl delay
      when that is longer. Our own floor is not charged to ``budget_s``; only the extra a crawl delay asks for
      is, so a robots.txt asking for more than a run may wait is refused in plain words.
    - A host that is not a government host: a path its robots.txt disallows is never fetched.
    - A government host (.gov, .mi.us, a county's or city's own domain, a state-run dashboard host): a robots.txt
      disallow does not stop the fetch; it is logged and kept in ``overridden``. Everything else still applies.

    A robots.txt that can't be found (404 and other 4xx) allows everything, as the standard says (RFC 9309); one
    the host refuses to show (401, 403) disallows everything; one that can't be read (a timeout, a 5xx) is retried
    on the next fetch and nothing is assumed until then except that the fetch itself goes ahead.
    """

    def __init__(
        self,
        hosts: frozenset[str],
        *,
        government_hosts: frozenset[str] = frozenset(),
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        budget_s: float = CRAWL_DELAY_BUDGET_S,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        self.hosts, self.sleep, self.clock, self.budget = hosts, sleep, clock, budget_s
        self.government = frozenset(h.lower() for h in government_hosts)
        self.min_interval = min_interval_s
        self.rules: dict[str, RobotFileParser] = {}
        self.last: dict[str, float] = {}
        self.waited = 0.0
        self.overridden: list[str] = []

    def _rules(self, host: str) -> RobotFileParser | None:
        from health_data_registry.fetch import FetchRefused, fetch

        if host in self.rules:
            return self.rules[host]
        rp = RobotFileParser()
        try:
            r = fetch(f"https://{host}/robots.txt", allowed_hosts=self.hosts, timeout=ROBOTS_TIMEOUT_S)
            status, body = r.status, r.content
        except FetchRefused as e:
            if e.status is None:
                return None  # can't be read now: asked again on the next fetch
            status, body = e.status, b""
        if status == 200:
            rp.parse(body.decode("utf-8", "replace").splitlines())
        elif status in (401, 403):
            rp.parse(["User-agent: *", "Disallow: /"])
        elif 400 <= status < 500:
            rp.parse([])
        else:
            return None
        self.rules[host] = rp
        return rp

    def wait_turn(self, url: str) -> None:
        """Refuse a path robots.txt disallows (not on a government host); then wait out the host's pace."""
        from health_data_registry.fetch import FetchRefused

        host = (urlsplit(url).hostname or "").lower()
        rp = self._rules(host)
        if rp is not None and not rp.can_fetch(ROBOTS_AGENT, url):
            if host not in self.government:
                raise FetchRefused(
                    "robots_disallowed",
                    f"The publisher's robots.txt at {host} asks automated tools not to read this address, so it "
                    "was not fetched.",
                )
            logger.info("robots.txt at %s disallows %s; fetched under the government-host policy", host, url)
            self.overridden.append(url)
        crawl_delay = float((rp.crawl_delay(ROBOTS_AGENT) if rp is not None else None) or 0)
        delay = max(self.min_interval, crawl_delay)
        if host in self.last and delay > 0:
            now = self.clock()
            due = self.last[host] + delay - now
            if due > 0:
                ours = max(0.0, self.last[host] + self.min_interval - now)
                charged = due - ours
                if self.waited + charged > self.budget:
                    raise FetchRefused(
                        "crawl_delay_budget",
                        f"The publisher's robots.txt at {host} asks for {crawl_delay:g} seconds between requests, "
                        "more than this run may wait in total, so the rest was not fetched. Try again later.",
                    )
                self.sleep(due)
                self.waited += charged
        self.last[host] = self.clock()


def fetcher(
    registry: Registry,
    log: FetchLog | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    robots: Robots | None | bool = True,
    browser: Callable[..., BrowserResult] | None = None,
) -> Getter:
    """A getter for catalogued publisher URLs that paces every host, honours robots.txt (by the access policy),
    retries slow publishers and logs every attempt. `robots=True` (the default) reads each host's robots.txt;
    tests pass False or their own Robots.

    Hosts whose catalog entry says `browser: true` are read with the browser fetcher; for a Tableau source the
    getter returns the view's captured bootstrapSession answer (the data), not the page around it.
    """
    from health_data_registry.fetch import FetchRefused, fetch

    hosts = registry.hosts
    government = frozenset(d.entry.source.host for d in registry if d.entry.source.government_host)
    browser_hosts = {d.entry.source.host: d.entry.source.portal for d in registry if d.entry.source.browser}
    policy = Robots(hosts, government_hosts=government, sleep=sleep) if robots is True else robots or None
    floor = policy.min_interval if policy is not None else MIN_INTERVAL_S

    def one(url: str) -> FetchResult:
        host = (urlsplit(url).hostname or "").lower()
        if host not in browser_hosts:
            return fetch(
                url, allowed_hosts=hosts, headers={"Accept": "application/json"}, timeout=FETCH_TIMEOUT_S
            )
        from health_data_registry.browser_fetch import browser_fetch

        tableau = browser_hosts[host] == "tableau"
        run = browser or browser_fetch
        got = run(
            url, allowed_hosts=hosts, capture=[TABLEAU_DATA] if tableau else [], timeout=FETCH_TIMEOUT_S
        )
        if not tableau:
            return got.page
        data = [c for c in got.captured if c.status == 200 and c.content]
        if not data:
            raise FetchRefused("no_data", f"The dashboard at {host} opened but sent no data for this view.")
        return data[-1]

    def get(url: str) -> RawFetch:
        if policy is not None:
            try:
                policy.wait_turn(url)
            except FetchRefused as e:
                if log is not None:
                    log.record(url, ok=False, when=datetime.now(UTC), error=str(e))
                raise
        for attempt in range(RETRIES):
            when = datetime.now(UTC)
            try:
                r = one(url)
            except FetchRefused as e:
                if log is not None:
                    log.record(url, ok=False, when=when, http_status=e.status, error=str(e))
                # A slow or briefly unreachable publisher is retried, never sooner than the pace allows; a refused
                # URL or host, and a block, never are.
                if e.code not in ("timeout", "connect_failed") or attempt == RETRIES - 1:
                    raise
                sleep(floor * 2**attempt)
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
        layouts: dict[str, str] = {}
        fields = sorted({c.source_field for c in ds.contract.columns})
        for i, page in enumerate(pages(ds.entry, get, max_rows=max_rows, fields=fields)):
            write_raw(lake, [page.fetch])  # keep the bytes on disk now, not in memory
            f = page.fetch
            layouts.update(page.layout)
            fetches.append(RawFetch(url=f.url, content=b"", sha256=f.sha256, retrieved_at=f.retrieved_at))
            nd = Path(tmp) / f"page-{i:05d}.ndjson"
            write_ndjson(nd, page.records)
            files.append(nd)
        return build_snapshot(lake, ds, fetches, files, partial=max_rows is not None, layouts=layouts)
