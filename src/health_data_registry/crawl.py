"""Fetch one catalogued source from its publisher into a lake: every attempt logged, raw bytes kept, one snapshot.

Used by `health-data-registry refresh` / `fetch` (the maintainers' lake) and by the server for sources whose
nothing else: a source whose licence forbids redistribution (WHO GHO) is never written to any lake; the server
reads it live (live.py).
"""

from __future__ import annotations

import logging
import random
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
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
# A host's answer that means it refuses this client: a block, final for the rest of the run. The one exception
# (owner decision, 25 Nov 2026): such an answer to a government host's robots.txt is overridden (Robots).
BLOCK_STATUSES = frozenset({401, 403})
# "Too many requests" (owner decision, 25 Nov 2026): on every host, government or not, a 429 is backed off and
# asked again (its Retry-After when given, else exponential back-off with jitter, never sooner than the host's
# pace), at most RATE_LIMIT_TRIES times; only then does the host count as blocked for the run, with its status.
RATE_LIMITED = 429
RATE_LIMIT_TRIES = 5
# The longest single wait a fetch makes for a busy publisher (a 429's or 503's Retry-After, or a back-off); a longer
# one stops the fetch in plain words so it can be asked again later.
MAX_RETRY_AFTER_S = 300.0


def backoff(
    attempt: int,
    retry_after: float | None,
    *,
    floor: float = 0.0,
    jitter: Callable[[float], float] = lambda base: random.uniform(0.0, base / 2),  # noqa: S311 - not crypto
) -> float:
    """How long to wait before asking a busy host again: its Retry-After when given, else 2, 4, 8 ... seconds plus
    up to half as much jitter; never less than ``floor`` (the host's pace)."""
    if retry_after is not None:
        return max(retry_after, floor)
    base = float(2 ** (attempt + 1))
    return max(base + jitter(base), floor)


Getter = Callable[[str], RawFetch]
# The path of the answer that carries a Tableau view's data (the tableau type declares it as browser_capture).
TABLEAU_DATA = "/bootstrapSession/"


class LiveOnly(ValueError):
    """The source's terms forbid storing it; the server reads it live instead."""


class PrivateTier(LiveOnly):
    """The source is kept in its own private store, built by its own command (crawl-listing, harvest-doi)."""


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
    - A block is final: a 401 or 403 on a page, on every host, or on robots.txt, on a host that is not a
      government host, puts the host in ``blocked``, and nothing more is asked of it for the rest of the run.
    - A 429 (owner decision, 25 Nov 2026), on every host: backed off and asked again (Retry-After, else
      exponential back-off with jitter, never sooner than the host's pace), up to RATE_LIMIT_TRIES times; only a
      429 that persists counts as a refusal, handled as a 401 or 403 is.
    - A government host's robots.txt answered with 401, 403 or 429 (owner decision, 25 Nov 2026) is handled like
      a disallow under the government-host policy: the crawl goes on with the honest user agent at the host's
      pace (at least ``min_interval_s``), and ``robots_overrides`` records "robots_overridden (robots.txt
      <status>)" for the fetch log and the manifest. A block on one of its pages is never overridden.
    - ``api_intervals``: a per-host floor from an API's own published rate limit (e.g. Crossref's public pool,
      one list query a second), charged to ``budget_s`` like a crawl delay.

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
        api_intervals: Mapping[str, float] | None = None,
        jitter: Callable[[float], float] | None = None,
        source_intervals: Mapping[str, float] | None = None,
        stop_on_refused_robots: bool = False,
    ) -> None:
        self.jitter = jitter
        # A source's own min_interval_s, per host (the slowest source on a host wins): it only ever adds to the wait.
        self.source_intervals = dict(source_intervals or {})
        # ADR 0013's stop rule, still in force for the sitemap job until that record is amended: a refused robots.txt
        # stops the host even on a government host. Every other crawl overrides it there (owner decision).
        self.stop_on_refused_robots = stop_on_refused_robots
        self.read_at: dict[str, float] = {}
        self.hosts, self.sleep, self.clock, self.budget = hosts, sleep, clock, budget_s
        self.api_intervals = dict(api_intervals or {})
        # Checked here too, not only in the catalog model: robots.txt is switched off only for a listed host.
        self.government = frozenset(h.lower() for h in government_hosts if is_government_host(h))
        self.paced = self.government | frozenset(h.lower() for h in paced_hosts)
        self.min_interval = min_interval_s
        self.rules: dict[str, RobotFileParser] = {}
        self.last: dict[str, float] = {}
        self.waited = 0.0
        self.overridden: list[str] = []
        self.robots_overrides: list[str] = []  # government hosts whose refused robots.txt was overridden
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

    def _fetch_robots(self, host: str) -> tuple[int, bytes, float | None] | None:
        """One request for ``host``'s robots.txt: (status, body, Retry-After), or None when it can't be read now."""
        from health_data_registry import deadline
        from health_data_registry.fetch import FetchRefused, fetch

        try:
            r = fetch(
                f"https://{host}/robots.txt", allowed_hosts=self.hosts, timeout=deadline.cap(ROBOTS_TIMEOUT_S)
            )
        except FetchRefused as e:
            if e.status is None:
                return None  # can't be read now: asked again on the next fetch
            return e.status, b"", e.retry_after
        return r.status, r.content, None

    def _rules(self, host: str) -> RobotFileParser | None:
        from health_data_registry import deadline
        from health_data_registry.access import ROBOTS_MAX_AGE_S
        from health_data_registry.fetch import FetchRefused

        if host in self.rules and self.clock() - self.read_at.get(host, self.clock()) < ROBOTS_MAX_AGE_S:
            return self.rules[host]  # a robots.txt is used for at most 24 hours (RFC 9309, 2.4)
        rp = RobotFileParser()
        tries = 1 if self.stop_on_refused_robots else RATE_LIMIT_TRIES
        for attempt in range(tries):
            try:
                got = self._fetch_robots(host)
            finally:
                # robots.txt was a request too: the page waits its turn after it
                self.last[host] = self.clock()
            if got is None:
                return None
            status, body, retry_after = got
            if status != RATE_LIMITED or attempt == tries - 1:
                break
            floor = self.min_interval if host in self.paced else 0.0
            wait = (
                backoff(attempt, retry_after, floor=floor)
                if self.jitter is None
                else backoff(attempt, retry_after, floor=floor, jitter=self.jitter)
            )
            if wait > MAX_RETRY_AFTER_S:
                break  # longer than any wait we make: handled below like an exhausted 429
            left = deadline.remaining()
            if left is not None and left <= wait:
                raise deadline.DeadlineExceeded()  # a hosted call never waits past its deadline
            logger.info("robots.txt at %s answered 429; asking again in %.0f s", host, wait)
            self.sleep(wait)
        blocking = status in BLOCK_STATUSES or status == RATE_LIMITED
        if blocking and host in self.government and not self.stop_on_refused_robots:
            # Owner decision, 25 Nov 2026: on a government host this is handled like a disallow under the
            # government-host policy, not a block. The host is paced (it is in ``paced``); the override is recorded.
            logger.info("robots.txt at %s answered %s; read under the government-host policy", host, status)
            self.robots_overrides.append(
                f"robots_overridden (robots.txt {status}): {host} refused its robots.txt; read under the "
                f"government-host policy, at least {self.min_interval:g} s between requests"
            )
            rp.parse([])
        elif blocking:
            self.block(host, status)
            raise FetchRefused(
                "blocked",
                f"{host} refused this program's request for its robots.txt (status {status}"
                + (f", after {RATE_LIMIT_TRIES} tries with back-off" if status == RATE_LIMITED else "")
                + "), so nothing is fetched from it in this run. This is recorded and not worked around.",
                status=status,
            )
        elif status == 200:
            rp.parse(body.decode("utf-8", "replace").splitlines())
        elif 400 <= status < 500:
            rp.parse([])
        else:
            return None
        self.rules[host] = rp
        self.read_at[host] = self.clock()
        return rp

    def _delays(self, host: str, rp: RobotFileParser | None) -> tuple[float, float]:
        """(our own floor for the host, the whole wait between two of its requests)."""
        from health_data_registry.access import pace_s

        ours = max(self.min_interval if host in self.paced else 0.0, self.source_intervals.get(host, 0.0))
        rate = rp.request_rate(ROBOTS_AGENT) if rp is not None else None
        delay = pace_s(
            floor=ours,
            crawl_delay=float((rp.crawl_delay(ROBOTS_AGENT) if rp is not None else None) or 0),
            request_rate=(rate.requests, rate.seconds) if rate is not None else None,
            api_interval=self.api_intervals.get(host),
            source_min=None,
        )
        return ours, delay

    def pace_for(self, host: str) -> float:
        """The wait between two requests to ``host`` this policy enforces, from what it has read so far."""
        return self._delays(host, self.rules.get(host))[1]

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
        host's pace, within the budget. Returns True when robots.txt disallowed the path and the government-host
        policy overrode it.
        """
        from health_data_registry import deadline
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
        floor, delay = self._delays(host, rp)
        if host in self.last and delay > 0:
            now = self.clock()
            due = self.last[host] + delay - now
            if due > 0:
                left = deadline.remaining()
                if left is not None and left <= due:  # inside a hosted call: never sleep past its deadline
                    raise deadline.DeadlineExceeded()
                ours = max(0.0, self.last[host] + floor - now)
                charged = due - ours
                if self.waited + charged > self.budget:
                    raise FetchRefused(
                        "crawl_delay_budget",
                        f"The publisher's robots.txt at {host} asks for {delay:g} seconds between requests, "
                        "more than this run may wait in total, so the rest was not fetched. Try again later.",
                    )
                self.sleep(due)
                self.waited += charged
        self.last[host] = self.clock()
        return overridden


def policy_for(registry: Registry, *, sleep: Callable[[float], None] = time.sleep) -> Robots:
    """A new access policy for the registry's hosts: its government hosts and its browser hosts are paced."""
    intervals: dict[str, float] = {}
    for d in registry:
        s = d.entry.source
        if s.min_interval_s is not None:
            intervals[s.host] = max(intervals.get(s.host, 0.0), s.min_interval_s)
    return Robots(
        registry.hosts,
        government_hosts=frozenset(d.entry.source.host for d in registry if d.entry.source.government_host),
        paced_hosts=frozenset(d.entry.source.host for d in registry if d.entry.source.browser),
        sleep=sleep,
        source_intervals=intervals,
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
    if best is not None:
        return best[1]
    # A link the source found (an index page's report, a listing's item): the source whose declared scope holds it.
    from health_data_registry.access import in_scope

    return next(
        (d.entry.source for d in registry if d.entry.source.scope_paths and in_scope(d.entry.source, url)),
        None,
    )


@dataclass
class PoliteGetter:
    """A getter for catalogued publisher URLs under the access policy (see ``fetcher``)."""

    get: Getter
    # URLs fetched in this getter's life although robots.txt disallowed them (the government-host policy); the
    # caller writes them into the manifest's notes.
    overridden: list[str]
    # One line per government host whose robots.txt answered 401, 403 or 429 and was overridden (owner decision).
    robots_overrides: list[str] = field(default_factory=list)
    policy: Robots | None = None

    def __call__(self, url: str) -> RawFetch:
        return self.get(url)

    def pace_for(self, host: str) -> float:
        """The wait this getter's access policy keeps between two requests to ``host``."""
        return self.policy.pace_for(host) if self.policy is not None else MIN_INTERVAL_S


def fetcher(
    registry: Registry,
    log: FetchLog | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    robots: Robots | None | bool = True,
    browser: Callable[..., BrowserResult] | None = None,
    max_retry_after_s: float = MAX_RETRY_AFTER_S,
    hosts: frozenset[str] | None = None,
    jitter: Callable[[float], float] = lambda base: random.uniform(0.0, base / 2),  # noqa: S311 - not crypto
) -> PoliteGetter:
    """A getter for catalogued publisher URLs that paces the hosts the policy targets, honours robots.txt (by the
    access policy), stops at a block, retries slow publishers and logs every attempt. `robots=True` (the default)
    builds a new policy; the CLI and the server pass `shared_robots(registry)`; tests pass False or their own.

    A URL whose catalog entry says `browser: true` is read with the browser fetcher; for a Tableau source the
    getter returns the view's captured bootstrapSession answer (the data), not the page around it, under the
    view's own URL (the session URL it was captured from is kept in the fetch log beside it).

    A 429, on every host, is asked again after its Retry-After (else exponential back-off with ``jitter``), never
    sooner than the host's pace; after RATE_LIMIT_TRIES of them the host is blocked for the run. A 503 is retried
    after its Retry-After (or the usual back-off). A wait longer than ``max_retry_after_s`` stops the fetch in plain
    words. ``hosts`` narrows the allowlist to one dataset's hosts (a DOI harvest's enrich hosts are reachable only
    from its own fetcher).

    Inside a call deadline (deadline.call_deadline, the hosted server), every attempt is capped by what is left of
    the budget and no retry starts once the budget can't cover its back-off: the call stops with
    deadline.DeadlineExceeded instead of retrying past it.
    """
    from health_data_registry import deadline
    from health_data_registry.access import redact_url
    from health_data_registry.fetch import FetchRefused, fetch

    hosts = registry.hosts if hosts is None else hosts
    policy = policy_for(registry, sleep=sleep) if robots is True else robots or None
    blocked = policy.blocked if policy is not None else set()
    paced = (policy or policy_for(registry)).paced  # retries back off at the pace even without robots.txt
    overridden: list[str] = []
    robots_notes = policy.robots_overrides if policy is not None else []

    def one(url: str, timeout: float) -> FetchResult:
        from health_data_registry.adapters import TYPES

        host = (urlsplit(url).hostname or "").lower()
        src = _source_for(registry, url)
        kind = TYPES[src.kind] if src is not None else None
        if src is None or not src.browser:
            # A type that doesn't follow redirects makes one request per call; a redirect is reported to it.
            once = {} if kind is None or kind.follows_redirects else {"max_redirects": 0}
            return fetch(
                url,
                allowed_hosts=hosts,
                headers={"Accept": kind.accept if kind is not None else "application/json"},
                timeout=timeout,
                **once,
            )
        from health_data_registry.browser_fetch import browser_fetch

        capture = list(kind.browser_capture) if kind is not None else []
        run = browser or browser_fetch
        got = run(
            url,
            allowed_hosts=hosts,
            capture=capture,
            timeout=timeout,
            allow=policy.allowed if policy is not None else None,
        )
        if not capture:
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
        r = _get(url)
        # Provenance never carries a key or token the URL held (hard rule 8, sharp edges): the manifest, the fetch
        # log and every answer cite the address without it.
        return RawFetch(url=redact_url(r.url), content=r.content, sha256=r.sha256, retrieved_at=r.retrieved_at,
                        capture_url=redact_url(r.capture_url) if r.capture_url else None)  # fmt: skip

    def _get(url: str) -> RawFetch:
        shown = redact_url(url)
        host = (urlsplit(url).hostname or "").lower()
        note = None
        try:
            if host in blocked:
                raise FetchRefused(
                    "blocked",
                    f"{host} refused this program's requests earlier in this run, so it is not asked again. This "
                    "is recorded and not worked around.",
                )
            seen = len(robots_notes)
            if policy is not None and policy.wait_turn(url):
                overridden.append(url)
                note = "robots_overridden: robots.txt disallows this address; fetched under the government-host policy"
            for new in robots_notes[seen:]:
                note = new.split(":", 1)[0]  # "robots_overridden (robots.txt <status>)"
                if log is not None:
                    log.record(
                        f"https://{host}/robots.txt", ok=False, when=datetime.now(UTC), error=new, note=note
                    )
        except FetchRefused as e:
            if log is not None:
                log.record(shown, ok=False, when=datetime.now(UTC), http_status=e.status, error=str(e))
            raise
        slow = limited = 0  # failed attempts so far: slow or unreachable (RETRIES), too many requests (429s)
        while True:
            when = datetime.now(UTC)
            budget = deadline.cap(FETCH_TIMEOUT_S)  # raises DeadlineExceeded when nothing is left
            try:
                r = one(url, budget)
            except FetchRefused as e:
                if e.status == RATE_LIMITED:
                    # Owner decision, 25 Nov 2026: a 429 is backed off and asked again on every host; the host is
                    # blocked for the run only after RATE_LIMIT_TRIES of them, with its status recorded.
                    limited += 1
                    if log is not None:
                        log.record(shown, ok=False, when=when, http_status=e.status, error=str(e), note=note)
                    if limited >= RATE_LIMIT_TRIES:
                        block(host, e.status)
                        raise FetchRefused(
                            "blocked",
                            f"{host} kept answering that it had too many requests (status 429) after "
                            f"{RATE_LIMIT_TRIES} tries with back-off, so nothing more is fetched from it in this run. "
                            "This is recorded and not worked around.",
                            status=e.status,
                        ) from None
                    floor = MIN_INTERVAL_S if host in paced else 0.0
                    wait = backoff(limited - 1, e.retry_after, floor=floor, jitter=jitter)
                    if wait > max_retry_after_s:
                        raise FetchRefused(
                            "rate_limited", _busy(registry, url, wait), status=e.status, retry_after=wait
                        ) from None
                    left = deadline.remaining()
                    if left is not None and left <= wait:
                        raise deadline.DeadlineExceeded() from e
                    sleep(wait)
                    continue
                if e.status in BLOCK_STATUSES and e.code != "blocked":
                    # fetch.py calls a 401/403 an http_error; under the access policy it is a block.
                    e = FetchRefused(
                        "blocked",
                        f"{host} refused this program's request (status {e.status}), so nothing more is fetched "
                        "from it in this run. This is recorded and not worked around.",
                        status=e.status,
                    )
                if e.code == "blocked":
                    block(host, e.status)
                if log is not None:
                    log.record(shown, ok=False, when=when, http_status=e.status, error=str(e), note=note)
                # A slow, busy or briefly unreachable publisher is retried, never sooner than the pace allows; a
                # refused URL or host, and a block, never are.
                busy = e.code == "http_error" and e.status == 503
                slow += 1
                if (e.code not in ("timeout", "connect_failed") and not busy) or slow >= RETRIES:
                    left = deadline.remaining()
                    if e.code == "timeout" and left is not None and left <= 0:
                        raise deadline.DeadlineExceeded() from e
                    raise e from None
                wait = MIN_INTERVAL_S * 2 ** (slow - 1) if host in paced else 2.0**slow
                if busy and e.retry_after is not None:
                    wait = max(wait, e.retry_after)
                if wait > max_retry_after_s:
                    raise FetchRefused(
                        "rate_limited", _busy(registry, url, wait), status=e.status, retry_after=wait
                    ) from None
                left = deadline.remaining()
                if left is not None and left <= wait:
                    raise deadline.DeadlineExceeded() from e
                sleep(wait)
                continue
            if r.status != 200:
                msg = f"the publisher answered with status {r.status} instead of the data"
                if log is not None:
                    log.record(shown, ok=False, when=when, http_status=r.status, size=len(r.content), error=msg,
                               note=note)  # fmt: skip
                raise RuntimeError(f"{shown}: {msg}")
            capture = r.final_url if r.final_url != url else None
            if log is not None:
                log.record(shown, ok=True, when=when, http_status=r.status, size=len(r.content), sha256=r.sha256,
                           note=note, capture_url=capture)  # fmt: skip
            # The catalogued address is the source URL (hard rule 8): for a dashboard that is the view, never the
            # one-time session URL the data were captured from, which is kept beside it.
            return RawFetch(url=url, content=r.content, sha256=r.sha256, retrieved_at=r.retrieved_at,
                            capture_url=capture)  # fmt: skip

    return PoliteGetter(get=get, overridden=overridden, robots_overrides=robots_notes, policy=policy)


def _busy(registry: Registry, url: str, wait: float) -> str:
    """The plain-words refusal for a publisher that asked for a longer wait than we keep a question waiting."""
    host = (urlsplit(url).hostname or "").lower()
    from health_data_registry.adapters import capabilities

    # A type whose hosts are shared APIs (a DOI harvest's bibliographic APIs) is not the publisher: name the host.
    ds = next(
        (d for d in registry if d.entry.source.host == host and capabilities(d.entry).host_is_publisher), None
    )
    who = ds.entry.publisher if ds is not None else host
    when = f"about {round(wait / 60)} minutes" if wait > 90 else f"{wait:.0f} seconds"
    page = f": {ds.entry.source.landing_url}" if ds is not None else ""
    return (
        f"{who} is busy and asked us to wait {when} before asking again, so nothing was fetched. Ask again "
        f"after that, or use the source's own page in the meantime{page}."
    )


def override_notes(get: Getter) -> list[str]:
    """Plain-words manifest notes for what a getter read under the government-host policy: addresses robots.txt
    disallowed, and government hosts whose robots.txt answered 401, 403 or 429 (owner decision, 25 Nov 2026)."""
    notes = list(dict.fromkeys(getattr(get, "robots_overrides", []) or []))
    urls = list(dict.fromkeys(getattr(get, "overridden", []) or []))
    if urls:
        notes.append(
            "Access policy: the publisher's robots.txt disallows "
            + ", ".join(urls)
            + "; it is a government host, so it was read under the maintainer's government-host policy (paced, with "
            "an honest user agent, and recorded in the fetch log)."
        )
    return notes


def refresh_dataset(ds: Dataset, lake: Path, get: Getter, *, max_rows: int | None = None) -> Manifest:
    """Page through one source, keep its raw bytes, and build its snapshot (partial when max_rows is set).

    The 8-week stop rule (owner decision, 25 Nov 2026): a run's failure is recorded in the source's failure streak
    and a success resets it; only 8 weeks of continuous failure makes the source dormant, and a dormant source is
    not crawled until the owner reactivates it (access.FailureStreaks).
    """
    from health_data_registry.access import FailureStreaks
    from health_data_registry.adapters import capabilities

    if ds.entry.tier == "private":
        command = capabilities(ds.entry).build_command
        raise PrivateTier(
            f"{ds.id} is kept only in its private store (lake/private/{ds.id}/), never in the public lake; "
            + (
                f"build it with `{command.format(**ds.entry.source.model_dump())}`"
                if command
                else "it has no crawl command yet"
            )
        )
    if not ds.entry.licence.redistribution:
        raise LiveOnly(
            f"{ds.id} is read live from {ds.entry.publisher} only: its terms ({ds.entry.licence.id}) attach to "
            "anything derived, so no copy is ever stored"
        )
    lake.mkdir(parents=True, exist_ok=True)
    streaks = FailureStreaks(lake)
    streaks.check(ds.id, datetime.now(UTC))
    try:
        manifest = _refresh(ds, lake, get, max_rows=max_rows)
    except Exception as e:
        streaks.failed(ds.id, datetime.now(UTC), f"{type(e).__name__}: {str(e)[:200]}")
        raise
    streaks.succeeded(ds.id)
    return manifest


def _refresh(ds: Dataset, lake: Path, get: Getter, *, max_rows: int | None) -> Manifest:
    from health_data_registry.adapters import pages

    with tempfile.TemporaryDirectory() as tmp:
        fetches, files = [], []
        layouts: dict[str, str] = {}
        provenance: dict[str, str] = {}
        fields = sorted({c.source_field for c in ds.contract.columns})
        for i, page in enumerate(pages(ds.entry, get, max_rows=max_rows, fields=fields, lake=lake)):
            write_raw(lake, [page.fetch])  # keep the bytes on disk now, not in memory
            f = page.fetch
            layouts.update(page.layout)
            provenance.update(page.provenance)
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
            provenance=provenance,
        )
