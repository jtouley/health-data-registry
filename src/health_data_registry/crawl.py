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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from health_data_registry.registry import Dataset, Registry, Source, is_government_host

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
# The least time between two page loads on a host the access policy targets (a government host, or a host read
# with the browser): the maintainer's access policy. Other hosts keep only their robots.txt crawl delay.
MIN_INTERVAL_S = 10.0
# A host's answer that means it refuses this client: a block, final for the rest of the run.
BLOCK_STATUSES = frozenset({401, 403, 429})

Getter = Callable[[str], RawFetch]
# The path of the answer that carries a Tableau view's data.
TABLEAU_DATA = "/bootstrapSession/"


class LiveOnly(ValueError):
    """The source's terms forbid storing it; the server reads it live instead."""


class Robots:
    """Each publisher host's robots.txt, read once per run, the pace of page loads, and the hosts that blocked us.

    Access policy (the maintainer's decision, recorded in the catalog as ``source.government_host`` and accepted
    only for hosts on registry's reviewed list of government suffixes and domains):

    - A host the policy targets (a government host, or one read with the browser, ``paced_hosts``): at least
      ``min_interval_s`` (10 s) between two page loads, or the host's robots.txt crawl delay when that is longer.
      The pace is per page load: a browser load's own scripts and data request follow the page within the load.
      Fetching robots.txt counts as a request, so the first page waits its turn after it. Our own floor is not
      charged to ``budget_s``; only the extra a crawl delay asks for is.
    - Every other host: its robots.txt crawl delay, if any, charged to ``budget_s``.
    - A host that is not a government host: a path its robots.txt disallows is never fetched, by fetch.py or by
      the browser (``allowed`` is checked on each of the page's own requests).
    - A government host: a robots.txt disallow does not stop the fetch; it is logged, kept in ``overridden``,
      and the caller records it in the fetch log and the manifest. Everything else still applies.
    - A block is final on every host: a 401, 403 or 429 on robots.txt or on a page puts the host in ``blocked``,
      and nothing more is asked of it for the rest of the run. A government host's block is never overridden.

    A robots.txt that can't be found (404 and other 4xx) allows everything, as the standard says (RFC 9309); one
    that can't be read (a timeout, a 5xx) is retried on the next fetch and nothing is assumed until then except
    that the fetch itself goes ahead.
    """

    def __init__(
        self,
        hosts: frozenset[str],
        *,
        government_hosts: frozenset[str] = frozenset(),
        paced_hosts: frozenset[str] = frozenset(),
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        budget_s: float = CRAWL_DELAY_BUDGET_S,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        self.hosts, self.sleep, self.clock, self.budget = hosts, sleep, clock, budget_s
        # Checked here too, not only in the catalog model: robots.txt is switched off only for a listed host.
        self.government = frozenset(h.lower() for h in government_hosts if is_government_host(h))
        self.paced = self.government | frozenset(h.lower() for h in paced_hosts)
        self.min_interval = min_interval_s
        self.rules: dict[str, RobotFileParser] = {}
        self.last: dict[str, float] = {}
        self.waited = 0.0
        self.overridden: list[str] = []
        self.blocked: set[str] = set()

    def block(self, host: str, status: int | None) -> None:
        """Remember that ``host`` refused this client: nothing more is asked of it in this run."""
        if host not in self.blocked:
            logger.warning(
                "%s blocked this client (status %s); nothing more is fetched from it", host, status
            )
        self.blocked.add(host)

    def _refuse_if_blocked(self, host: str) -> None:
        from health_data_registry.fetch import FetchRefused

        if host in self.blocked:
            raise FetchRefused(
                "blocked",
                f"{host} refused this program's requests earlier in this run, so it is not asked again. This is "
                "recorded and not worked around.",
            )

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
        finally:
            self.last[host] = self.clock()  # robots.txt was a request too: the page waits its turn after it
        if status in BLOCK_STATUSES:
            self.block(host, status)
            raise FetchRefused(
                "blocked",
                f"{host} refused this program's request for its robots.txt (status {status}), so nothing is "
                "fetched from it in this run. This is recorded and not worked around.",
                status=status,
            )
        if status == 200:
            rp.parse(body.decode("utf-8", "replace").splitlines())
        elif 400 <= status < 500:
            rp.parse([])
        else:
            return None
        self.rules[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        """May the browser make this request? A blocked host never; a government host yes; else its robots.txt."""
        from health_data_registry.fetch import FetchRefused

        host = (urlsplit(url).hostname or "").lower()
        if host in self.blocked:
            return False
        if host in self.government:
            return True
        try:
            rp = self._rules(host)
        except FetchRefused:
            return False
        return rp is None or rp.can_fetch(ROBOTS_AGENT, url)

    def wait_turn(self, url: str) -> bool:
        """Refuse a blocked host, or a path robots.txt disallows (not on a government host); then wait out the
        host's pace. Returns True when robots.txt disallowed the path and the government-host policy overrode it.
        """
        from health_data_registry.fetch import FetchRefused

        host = (urlsplit(url).hostname or "").lower()
        self._refuse_if_blocked(host)
        rp = self._rules(host)
        overridden = False
        if rp is not None and not rp.can_fetch(ROBOTS_AGENT, url):
            if host not in self.government:
                raise FetchRefused(
                    "robots_disallowed",
                    f"The publisher's robots.txt at {host} asks automated tools not to read this address, so it "
                    "was not fetched.",
                )
            logger.info("robots.txt at %s disallows %s; fetched under the government-host policy", host, url)
            self.overridden.append(url)
            overridden = True
        crawl_delay = float((rp.crawl_delay(ROBOTS_AGENT) if rp is not None else None) or 0)
        floor = self.min_interval if host in self.paced else 0.0
        delay = max(floor, crawl_delay)
        if host in self.last and delay > 0:
            now = self.clock()
            due = self.last[host] + delay - now
            if due > 0:
                ours = max(0.0, self.last[host] + floor - now)
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
        return overridden


def policy_for(registry: Registry, *, sleep: Callable[[float], None] = time.sleep) -> Robots:
    """A new access policy for the registry's hosts: its government hosts and its browser hosts are paced."""
    return Robots(
        registry.hosts,
        government_hosts=frozenset(d.entry.source.host for d in registry if d.entry.source.government_host),
        paced_hosts=frozenset(d.entry.source.host for d in registry if d.entry.source.browser),
        sleep=sleep,
    )


_SHARED: dict[tuple[frozenset[str], frozenset[str], frozenset[str]], Robots] = {}


def shared_robots(registry: Registry) -> Robots:
    """One access policy per process for this registry's hosts, so the pace and blocks hold across datasets, runs
    and clinicians' questions (refresh of several datasets, and the server's live reads, share it)."""
    p = policy_for(registry)
    key = (p.hosts, p.government, p.paced)
    return _SHARED.setdefault(key, p)


def _source_for(registry: Registry, url: str) -> Source | None:
    """The catalog entry a URL belongs to (the longest catalogued URL it starts with), so the browser and the
    portal are decided per dataset, never for a whole host."""
    best: tuple[int, Source] | None = None
    for d in registry:
        s = d.entry.source
        for base in (s.api_url.rstrip("/"), s.landing_url.rstrip("/")):
            # a whole path segment: ".../views/Toy" is not the base of ".../views/Toy2"
            within = url == base or url[len(base) : len(base) + 1] in ("/", "?", "#")
            if url.startswith(base) and within and (best is None or len(base) > best[0]):
                best = (len(base), s)
    return best[1] if best else None


@dataclass
class PoliteGetter:
    """A getter for catalogued publisher URLs under the access policy (see ``fetcher``)."""

    get: Getter
    # URLs fetched in this getter's life although robots.txt disallowed them (the government-host policy); the
    # caller writes them into the manifest's notes.
    overridden: list[str]

    def __call__(self, url: str) -> RawFetch:
        return self.get(url)


def fetcher(
    registry: Registry,
    log: FetchLog | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    robots: Robots | None | bool = True,
    browser: Callable[..., BrowserResult] | None = None,
) -> PoliteGetter:
    """A getter for catalogued publisher URLs that paces the hosts the policy targets, honours robots.txt (by the
    access policy), stops at a block, retries slow publishers and logs every attempt. `robots=True` (the default)
    builds a new policy; the CLI and the server pass `shared_robots(registry)`; tests pass False or their own.

    A URL whose catalog entry says `browser: true` is read with the browser fetcher; for a Tableau source the
    getter returns the view's captured bootstrapSession answer (the data), not the page around it, under the
    view's own URL (the session URL it was captured from is kept in the fetch log beside it).
    """
    from health_data_registry.fetch import FetchRefused, fetch

    hosts = registry.hosts
    policy = policy_for(registry, sleep=sleep) if robots is True else robots or None
    blocked = policy.blocked if policy is not None else set()
    paced = (policy or policy_for(registry)).paced  # retries back off at the pace even without robots.txt
    overridden: list[str] = []

    def one(url: str) -> FetchResult:
        host = (urlsplit(url).hostname or "").lower()
        src = _source_for(registry, url)
        if src is None or not src.browser:
            return fetch(
                url, allowed_hosts=hosts, headers={"Accept": "application/json"}, timeout=FETCH_TIMEOUT_S
            )
        from health_data_registry.browser_fetch import browser_fetch

        tableau = src.portal == "tableau"
        run = browser or browser_fetch
        got = run(
            url,
            allowed_hosts=hosts,
            capture=[TABLEAU_DATA] if tableau else [],
            timeout=FETCH_TIMEOUT_S,
            allow=policy.allowed if policy is not None else None,
        )
        if not tableau:
            return got.page
        data = [c for c in got.captured if c.status == 200 and c.content]
        if not data:
            raise FetchRefused("no_data", f"The dashboard at {host} opened but sent no data for this view.")
        return data[-1]

    def block(host: str, status: int | None) -> None:
        if policy is not None:
            policy.block(host, status)
        else:
            blocked.add(host)

    def get(url: str) -> RawFetch:
        host = (urlsplit(url).hostname or "").lower()
        note = None
        try:
            if host in blocked:
                raise FetchRefused(
                    "blocked",
                    f"{host} refused this program's requests earlier in this run, so it is not asked again. This "
                    "is recorded and not worked around.",
                )
            if policy is not None and policy.wait_turn(url):
                overridden.append(url)
                note = "robots_overridden: robots.txt disallows this address; fetched under the government-host policy"
        except FetchRefused as e:
            if log is not None:
                log.record(url, ok=False, when=datetime.now(UTC), http_status=e.status, error=str(e))
            raise
        for attempt in range(RETRIES):
            when = datetime.now(UTC)
            try:
                r = one(url)
            except FetchRefused as e:
                if e.status in BLOCK_STATUSES and e.code != "blocked":
                    # fetch.py calls a 401/403/429 an http_error; under the access policy it is a block.
                    e = FetchRefused(
                        "blocked",
                        f"{host} refused this program's request (status {e.status}), so nothing more is fetched "
                        "from it in this run. This is recorded and not worked around.",
                        status=e.status,
                    )
                if e.code == "blocked":
                    block(host, e.status)
                if log is not None:
                    log.record(url, ok=False, when=when, http_status=e.status, error=str(e), note=note)
                # A slow or briefly unreachable publisher is retried, never sooner than the pace allows; a refused
                # URL or host, and a block, never are.
                if e.code not in ("timeout", "connect_failed") or attempt == RETRIES - 1:
                    raise e from None
                sleep(MIN_INTERVAL_S * 2**attempt if host in paced else 2 ** (attempt + 1))
                continue
            if r.status != 200:
                msg = f"the publisher answered with status {r.status} instead of the data"
                if log is not None:
                    log.record(url, ok=False, when=when, http_status=r.status, size=len(r.content), error=msg,
                               note=note)  # fmt: skip
                raise RuntimeError(f"{url}: {msg}")
            capture = r.final_url if r.final_url != url else None
            if log is not None:
                log.record(url, ok=True, when=when, http_status=r.status, size=len(r.content), sha256=r.sha256,
                           note=note, capture_url=capture)  # fmt: skip
            # The catalogued address is the source URL (hard rule 8): for a dashboard that is the view, never the
            # one-time session URL the data were captured from, which is kept beside it.
            return RawFetch(url=url, content=r.content, sha256=r.sha256, retrieved_at=r.retrieved_at,
                            capture_url=capture)  # fmt: skip
        raise RuntimeError(f"{url}: no response")  # pragma: no cover - the loop returns or raises

    return PoliteGetter(get=get, overridden=overridden)


def override_notes(get: Getter) -> list[str]:
    """Plain-words manifest notes for the addresses a getter fetched although robots.txt disallowed them."""
    urls = list(dict.fromkeys(getattr(get, "overridden", []) or []))
    if not urls:
        return []
    return [
        "Access policy: the publisher's robots.txt disallows "
        + ", ".join(urls)
        + "; it is a government host, so it was read under the maintainer's government-host policy (paced, with an "
        "honest user agent, and recorded in the fetch log)."
    ]


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
        return build_snapshot(
            lake,
            ds,
            fetches,
            files,
            partial=max_rows is not None,
            layouts=layouts,
            notes=override_notes(get),
            robots_overrides=list(getattr(get, "overridden", []) or []),
        )
