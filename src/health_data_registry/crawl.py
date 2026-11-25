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

from health_data_registry.registry import Dataset, Registry, is_government_host
from health_data_registry.store import FetchLog, Manifest, RawFetch, build_snapshot, write_ndjson, write_raw

RETRIES = 4
FETCH_TIMEOUT_S = 180.0
ROBOTS_TIMEOUT_S = 30.0
# The most a run will wait in total to honour publishers' robots.txt crawl delays; a robots.txt asking for
# more than that is refused in plain words rather than stalling a refresh or a clinician's question.
CRAWL_DELAY_BUDGET_S = 240.0
ROBOTS_AGENT = "health-data-registry"
# The longest Retry-After a run waits out (a 503 from a busy API); a longer one stops the fetch in plain words so it
# can be asked again later. A 429 is a block (BLOCK_STATUSES), never waited out.
MAX_RETRY_AFTER_S = 300.0
# A host that answers with one of these has refused this client (the owner's access policy, as in PR #19's
# crawl.Robots): nothing more is asked of it for the rest of the run. RFC 9309's "4xx means allowed" is not applied
# to them. The one exception (owner decision, 25 Nov 2026): such an answer on a government host's robots.txt is
# overridden (crawling goes on at GOVERNMENT_MIN_INTERVAL_S and the override is recorded). On a data page it
# stops the crawl on every host: access controls are never worked around.
BLOCK_STATUSES = frozenset({401, 403, 429})
GOVERNMENT_MIN_INTERVAL_S = 10.0

Getter = Callable[[str], RawFetch]


class LiveOnly(ValueError):
    """The source's terms forbid storing it; the server reads it live instead."""


class PrivateTier(LiveOnly):
    """The source is kept in its own private store, built by its own command (index-croi, harvest-doi)."""


class Robots:
    """Each publisher host's robots.txt, read once per run: which paths may be fetched and how far apart.

    A robots.txt that can't be found (404 and other 4xx) allows everything, as the standard says (RFC 9309); one
    that can't be read (a timeout, a 5xx) is retried on the next fetch and nothing is assumed until then except
    that the fetch itself goes ahead. A block is final (the owner's access policy): a 401, 403 or 429 on robots.txt
    puts the host in ``blocked``, and nothing more is asked of it for the rest of the run, except on a government
    host (``government_hosts``, only hosts on registry's reviewed list): there the answer is overridden, the host
    is paced at GOVERNMENT_MIN_INTERVAL_S or more, and ``overridden`` records "robots_overridden (robots.txt
    <status>)" for the fetch log and the manifest. The fetcher blocks a host that answers a data page with one
    of those statuses, government host or not.
    """

    def __init__(
        self,
        hosts: frozenset[str],
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        budget_s: float = CRAWL_DELAY_BUDGET_S,
        min_interval_s: Mapping[str, float] | None = None,
        government_hosts: frozenset[str] = frozenset(),
    ) -> None:
        self.hosts, self.sleep, self.clock, self.budget = hosts, sleep, clock, budget_s
        # Per-host floor on the gap between requests, from an API's own published rate limit (e.g. Crossref's
        # public pool: one list query a second); robots.txt's crawl delay wins when it asks for more.
        self.min_interval = dict(min_interval_s or {})
        self.rules: dict[str, RobotFileParser] = {}
        self.last: dict[str, float] = {}
        self.waited = 0.0
        self.blocked: set[str] = set()
        # Checked here too, not only in the catalog model: the override applies only to a listed host.
        self.government = frozenset(h.lower() for h in government_hosts if is_government_host(h))
        self.overridden: list[str] = []  # one plain line per override, for the fetch log and the manifest
        self.paced: set[str] = set()  # government hosts read under the override, at GOVERNMENT_MIN_INTERVAL_S

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
            status = r.status
        except FetchRefused as e:  # the fetcher raises for every status but 200
            if e.status is None:
                return None  # can't be read now (timeout, refused connection): asked again on the next fetch
            status = e.status
        if status in BLOCK_STATUSES and host in self.government:
            self.overridden.append(
                f"robots_overridden (robots.txt {status}): {host} refused its robots.txt; read under the "
                f"government-host policy at {GOVERNMENT_MIN_INTERVAL_S:g} s or more between requests"
            )
            self.min_interval[host] = max(self.min_interval.get(host, 0.0), GOVERNMENT_MIN_INTERVAL_S)
            self.paced.add(host)
            rp.parse([])
        elif status in BLOCK_STATUSES:
            self.blocked.add(host)
            raise FetchRefused(
                "blocked",
                f"{host} refused this program's request for its robots.txt (status {status}), so nothing is "
                "fetched from it in this run. This is recorded and not worked around.",
                status=status,
            )
        elif status == 200:
            rp.parse(r.content.decode("utf-8", "replace").splitlines())
        elif 400 <= status < 500:
            rp.parse([])  # no robots.txt (404, 410, ...): no rules
        else:
            return None  # failing (5xx): asked again on the next fetch
        self.rules[host] = rp
        return rp

    def wait_turn(self, url: str) -> None:
        """Refuse a path robots.txt disallows; otherwise wait out the host's crawl delay, within the budget."""
        from health_data_registry.fetch import FetchRefused

        host = (urlsplit(url).hostname or "").lower()
        self._refuse_if_blocked(host)
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
            now = self.clock()
            due = self.last[host] + delay - now
            if due > 0:
                # The government-host floor is our own pace, not the host's ask: it is not charged to the budget.
                ours = (
                    max(0.0, self.last[host] + GOVERNMENT_MIN_INTERVAL_S - now) if host in self.paced else 0.0
                )
                charged = due - ours
                if self.waited + charged > self.budget:
                    raise FetchRefused(
                        "crawl_delay_budget",
                        f"The publisher's robots.txt at {host} asks for {delay:g} seconds between requests, more "
                        "than this run may wait in total, so the rest was not fetched. Try again later.",
                    )
                self.sleep(due)
                self.waited += charged
        self.last[host] = self.clock()


def government_hosts(registry: Registry) -> frozenset[str]:
    """The catalog's government hosts (``source.government_host``, only hosts on registry's reviewed list)."""
    return frozenset(d.entry.source.host for d in registry if d.entry.source.government_host)


class PolicyGetter:
    """A Getter that keeps the access policy it fetched under, so a refresh can record its overrides."""

    def __init__(self, get: Getter, robots: Robots | None) -> None:
        self._get, self.robots = get, robots

    def __call__(self, url: str) -> RawFetch:
        return self._get(url)

    @property
    def overridden(self) -> list[str]:
        return list(self.robots.overridden) if self.robots is not None else []


def fetcher(
    registry: Registry,
    log: FetchLog | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    robots: Robots | None | bool = True,
    max_retry_after_s: float = MAX_RETRY_AFTER_S,
    hosts: frozenset[str] | None = None,
) -> PolicyGetter:
    """A getter for catalogued publisher URLs that honours robots.txt, retries slow publishers and logs every
    attempt. `robots=True` (the default) reads each host's robots.txt; tests pass False or their own Robots.

    A 503 is retried after the publisher's Retry-After (or a doubling back-off when it gives none); a Retry-After
    longer than ``max_retry_after_s`` stops the fetch instead of waiting. A 401, 403 or 429 on a data page blocks
    the host for the rest of the run, government host or not (BLOCK_STATUSES). ``hosts`` narrows the allowlist to
    one dataset's hosts (a DOI harvest's enrich hosts are reachable only from its own fetcher)."""
    from health_data_registry.fetch import FetchRefused, fetch

    hosts = registry.hosts if hosts is None else hosts
    policy = (
        Robots(hosts, sleep=sleep, government_hosts=government_hosts(registry))
        if robots is True
        else robots or None
    )

    def wait_turn(url: str) -> None:
        if policy is None:
            return
        seen = len(policy.overridden)
        try:
            policy.wait_turn(url)
        except FetchRefused as e:
            if log is not None:
                log.record(url, ok=False, when=datetime.now(UTC), http_status=e.status, error=str(e))
            raise
        if log is not None:
            host = (urlsplit(url).hostname or "").lower()
            for note in policy.overridden[seen:]:
                log.record(f"https://{host}/robots.txt", ok=False, when=datetime.now(UTC), error=note)

    def get(url: str) -> RawFetch:
        for attempt in range(RETRIES):
            # Every attempt, retries included, waits its turn: a retry never comes sooner than the crawl delay.
            wait_turn(url)
            when = datetime.now(UTC)
            try:
                r = fetch(
                    url, allowed_hosts=hosts, headers={"Accept": "application/json"}, timeout=FETCH_TIMEOUT_S
                )
            except FetchRefused as e:
                if log is not None:
                    log.record(url, ok=False, when=when, http_status=e.status, error=str(e))
                if e.code == "http_error" and e.status in BLOCK_STATUSES:
                    host = (urlsplit(url).hostname or "").lower()
                    if policy is not None:
                        policy.blocked.add(host)
                    raise FetchRefused(
                        "blocked",
                        f"{host} refused this program's request (status {e.status}), so nothing more is fetched "
                        "from it in this run. This is recorded and not worked around.",
                        status=e.status,
                    ) from e
                # A slow, busy or briefly unreachable publisher is retried; a refused URL or host never is.
                busy = e.code == "http_error" and e.status == 503
                if (e.code not in ("timeout", "connect_failed") and not busy) or attempt == RETRIES - 1:
                    raise
                wait = e.retry_after if busy and e.retry_after is not None else 2 ** (attempt + 1)
                if wait > max_retry_after_s:
                    raise FetchRefused(
                        "rate_limited", _busy(registry, url, wait), status=e.status, retry_after=wait
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

    return PolicyGetter(get, policy)


def _busy(registry: Registry, url: str, wait: float) -> str:
    """The plain-words refusal for a publisher that asked for a longer wait than we keep a question waiting."""
    host = (urlsplit(url).hostname or "").lower()
    # A DOI harvest's hosts are bibliographic APIs, not the conference's publisher: name the host itself.
    ds = next((d for d in registry if d.entry.source.host == host and d.entry.source.portal != "doi"), None)
    who = ds.entry.publisher if ds is not None else host
    when = f"about {round(wait / 60)} minutes" if wait > 90 else f"{wait:.0f} seconds"
    page = f": {ds.entry.source.landing_url}" if ds is not None else ""
    return (
        f"{who} is busy and asked us to wait {when} before asking again, so nothing was fetched. Ask again "
        f"after that, or use the source's own page in the meantime{page}."
    )


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
        # A government host's robots.txt that was overridden is recorded with the snapshot (owner decision).
        notes = get.overridden if isinstance(get, PolicyGetter) else []
        return build_snapshot(lake, ds, fetches, files, partial=max_rows is not None, notes=notes)
