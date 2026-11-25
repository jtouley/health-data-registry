"""A polite crawler client: robots.txt first, the site's own crawl delay, and a budget that survives restarts.

Used for sites that ask crawlers to slow down (conference abstract sites such as CROI's). The rules:

- robots.txt is the first request of every run, and nothing else is requested before it is parsed.
- The delay between requests is the site's ``Crawl-delay`` (or ``Request-rate``, the slower of the two) for our
  user agent (or for ``*``), never a number written here; ``default_delay`` applies only when robots.txt names
  neither.
- ``Disallow`` rules are obeyed: a disallowed link is refused before any request.
- A robots.txt is used for at most 24 hours (RFC 9309, 2.4), then read again before the next request.
- The time of the last request is written to a state file *before* the request is sent, so a crash or a
  restart can't send two requests closer together than the delay.
- The state file names its host; a state written for another host (or one that can't be read) is refused on
  restore, never reused.
- One crawler per host for this user, whatever lake or checkout it writes to: the client takes an inter-process
  lock named after the host in a per-user folder (``$HDR_CRAWL_LOCKS``, else
  ``$XDG_STATE_HOME/health-data-registry/locks``, else ``~/.local/state/health-data-registry/locks``) and refuses
  to start while another process holds it, so two runs can't share one site's pace. The pacing state stays with
  the lake (the scheduled crawl saves and restores it).
- The state's values are checked on restore: a time or delay that isn't a finite, non-negative number, or a
  last request in the future, is refused rather than read as "no wait".
- A delay read from robots.txt is never below ``min_delay`` (by default 10 s, or ``default_delay`` if that is
  smaller; the CROI crawl sets 120 s), so a
  ``Crawl-delay: 0`` or a misconfigured robots.txt can't turn pacing off. Asking for less than the default is
  logged.
- The state file is written whole or not at all (temp file, fsync, rename, folder fsync), like every other
  crawl write.
- One call is one request: redirects are not followed silently (the fetcher is called with
  ``max_redirects=0``); a redirect is reported and the caller decides whether to request the new link.
- Every attempt is a row in the lake's fetch log (store.FetchLog), ok or error.

It runs only from the command line or the Python API, never inside the MCP server.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from health_data_registry.fetch import USER_AGENT, FetchRefused, FetchResult
from health_data_registry.store import FetchLog

ROBOTS_MISSING = frozenset({404, 410})
ROBOTS_MAX_AGE_S = 24 * 3600.0  # RFC 9309 2.4: a cached robots.txt is not used for more than 24 hours
MIN_DELAY_S = 10.0  # the floor under any delay robots.txt asks for
LOCKS_ENV = "HDR_CRAWL_LOCKS"

_log = logging.getLogger(__name__)


def host_lock_path(host: str) -> Path:
    """The per-user lock file for crawling ``host``: the same for every lake and checkout of this user."""
    base = os.environ.get(LOCKS_ENV, "").strip()
    if base:
        folder = Path(base)
    else:
        state_home = os.environ.get("XDG_STATE_HOME", "").strip()
        root = Path(state_home) if state_home else Path.home() / ".local" / "state"
        folder = root / "health-data-registry" / "locks"
    return folder / f"{host}.lock"


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class CrawlStopped(Exception):
    """The crawl stopped on purpose (robots, budget, a lock, an unreadable robots.txt). ``code`` is for programs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


Getter = Callable[[str], FetchResult]


class HostLock:
    """An inter-process lock on one host's crawl: an exclusive, non-blocking ``flock`` on a lock file.

    The operating system drops the lock when the process ends, however it ends, so a crash never leaves a stale
    lock behind. A second crawler of the same host (another process, or a second client in this one) is refused.
    """

    def __init__(self, path: Path, host: str) -> None:
        import fcntl

        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            raise CrawlStopped(
                "host_locked",
                f"another crawler of {host} is running (lock {path}), so this one did not start; wait for it to "
                "finish or stop it first",
            ) from e
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd: int | None = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        import fcntl

        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


@dataclass
class _State:
    last_request_at: float | None = None
    delay_s: float | None = None


class PoliteClient:
    """One site, one request at a time, at the pace its robots.txt asks for.

    The client holds its host's lock from construction until ``close()`` (or the end of a ``with`` block).
    """

    def __init__(
        self,
        base_url: str,
        *,
        state_path: Path,
        get_raw: Getter,
        log: FetchLog | None = None,
        clock: Clock | None = None,
        default_delay: float = 120.0,
        min_delay: float | None = None,
        max_requests: int | None = None,
        user_agent: str = USER_AGENT,
        lock_path: Path | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("the crawler only reads https:// sites")
        if not default_delay > 0:  # 0 or less (or NaN) would let a site without a Crawl-delay be hammered
            raise ValueError(f"default_delay must be a positive number of seconds, got {default_delay!r}")
        if min_delay is None:
            min_delay = min(MIN_DELAY_S, default_delay)
        if not 0 < min_delay <= default_delay:
            raise ValueError(f"min_delay must be positive and at most default_delay, got {min_delay!r}")
        self.base = f"https://{parts.hostname}"
        self.host = parts.hostname
        self.state_path = state_path
        self._get_raw = get_raw
        self.log = log
        self.clock = clock or SystemClock()
        self.default_delay = default_delay
        self.min_delay = min_delay
        self.max_requests = max_requests
        self.user_agent = user_agent
        self.requests_made = 0
        self.robots: RobotFileParser | None = None
        self.robots_text: str | None = None
        self.robots_read_at: float | None = None
        self.delay: float | None = None
        self.lock = HostLock(lock_path or host_lock_path(self.host), self.host)
        try:
            self._state = self._load_state()
        except BaseException:
            self.lock.release()
            raise

    def close(self) -> None:
        """Release the host's lock; the client makes no more requests after this."""
        self.lock.release()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- state

    def _load_state(self) -> _State:
        if not self.state_path.is_file():
            return _State()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise CrawlStopped(
                "state_unreadable",
                f"the crawl state {self.state_path} can't be read, so its pacing can't be trusted; delete it to "
                "start over at the default delay",
            ) from e
        stored = raw.get("host") if isinstance(raw, dict) else None
        if stored != self.host:
            raise CrawlStopped(
                "state_host_mismatch",
                f"the crawl state {self.state_path} was written for {stored or 'no host'}, not {self.host}, so "
                "it was not reused",
            )
        last, delay = raw.get("last_request_at"), raw.get("delay_s")
        bad = [k for k, v in (("last_request_at", last), ("delay_s", delay)) if not _seconds(v)]
        if not bad and last is not None and last > self.clock.now():
            bad = ["last_request_at (in the future)"]
        if bad:
            raise CrawlStopped(
                "state_unreadable",
                f"the crawl state {self.state_path} has a value that isn't a time or delay ({', '.join(bad)}), so "
                "its pacing can't be trusted; delete it to start over at the default delay",
            )
        return _State(last_request_at=last, delay_s=delay)

    def _save_state(self) -> None:
        from health_data_registry import private

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        private.atomic_write(
            self.state_path,
            json.dumps({"host": self.host, "last_request_at": self._state.last_request_at,
                        "delay_s": self._state.delay_s}) + "\n",
        )  # fmt: skip

    # ---------------------------------------------------------------- pacing

    def _pace(self) -> float:
        """The delay in force: robots' once read, else the last run's robots value, else the default."""
        if self.delay is not None:
            return self.delay
        return max(self._state.delay_s or 0.0, self.default_delay)

    def _wait_turn(self) -> None:
        last = self._state.last_request_at
        if last is None:
            return
        due = last + self._pace()
        while (left := due - self.clock.now()) > 0:
            self.clock.sleep(left)

    def _request(self, url: str) -> FetchResult:
        if not self.lock.held:
            raise CrawlStopped("closed", f"this crawler of {self.host} was closed and makes no more requests")
        if self.max_requests is not None and self.requests_made >= self.max_requests:
            raise CrawlStopped(
                "budget_spent", f"stopped after {self.requests_made} requests, the limit set for this run"
            )
        self._wait_turn()
        sent = self.clock.now()
        self._state.last_request_at = sent  # written before sending: a restart can't burst
        self._save_state()
        self.requests_made += 1
        when = datetime.fromtimestamp(sent, UTC)
        try:
            r = self._get_raw(url)
        except FetchRefused as e:
            if self.log is not None:
                self.log.record(url, ok=False, when=when, http_status=e.status, error=e.message)
            raise
        if self.log is not None:
            self.log.record(
                url, ok=True, when=when, http_status=r.status, size=len(r.content), sha256=r.sha256
            )
        return r

    # ---------------------------------------------------------------- robots

    def start(self) -> None:
        """Read and parse robots.txt: the first request of a run, and again once the copy is 24 hours old."""
        if (
            self.robots is not None
            and self.robots_read_at is not None
            and self.clock.now() - self.robots_read_at < ROBOTS_MAX_AGE_S
        ):
            return
        url = f"{self.base}/robots.txt"
        rp = RobotFileParser(url)
        try:
            r = self._request(url)
            text = r.content.decode("utf-8", errors="replace")
        except FetchRefused as e:
            if e.status not in ROBOTS_MISSING:
                raise CrawlStopped(
                    "robots_unreadable",
                    f"{self.host}'s robots.txt could not be read ({e.message}), so nothing else was requested",
                ) from e
            text = ""  # no robots.txt: no rules, the default delay
        rp.parse(text.splitlines())
        self.robots, self.robots_text, self.robots_read_at = rp, text, self.clock.now()
        asked = robots_delay(rp, self.user_agent, self.default_delay)
        if asked < self.default_delay:
            _log.warning(
                "%s's robots.txt asks for %.1f s between requests, less than the default %.1f s; using %.1f s",
                self.host, asked, self.default_delay, max(asked, self.min_delay),
            )  # fmt: skip
        self.delay = max(asked, self.min_delay)
        self._state.delay_s = self.delay
        self._save_state()

    def allowed(self, url: str) -> bool:
        self.start()
        assert self.robots is not None  # noqa: S101 - set by start()
        return self.robots.can_fetch(self.user_agent, url)

    def get(self, url: str) -> FetchResult:
        """One request, after robots.txt, at the site's pace. Raises CrawlStopped for a disallowed link."""
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != self.host:
            raise CrawlStopped("other_host", f"{url} is not on {self.host}; this client reads one site")
        if not self.allowed(url):
            raise CrawlStopped("robots_disallow", f"{self.host}'s robots.txt asks crawlers not to read {url}")
        return self._request(url)


def _seconds(v: object) -> bool:
    """None, or a finite, non-negative number (not a bool): a value the pacing can trust."""
    if v is None:
        return True
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def robots_delay(rp: RobotFileParser, user_agent: str, default: float) -> float:
    """Seconds between requests that robots.txt asks of this user agent: the slower of Crawl-delay and
    Request-rate, or ``default`` when it states neither."""
    asked: list[float] = []
    delay = rp.crawl_delay(user_agent)
    if delay is not None:
        asked.append(float(delay))
    rate = rp.request_rate(user_agent)
    if rate is not None and rate.requests > 0:
        asked.append(rate.seconds / rate.requests)
    return max(asked) if asked else default


def https_getter(host: str, *, timeout: float = 60.0) -> Getter:
    """The project fetcher for one host, one request per call (no redirects followed), HTML accepted."""
    from health_data_registry.fetch import fetch

    hosts = frozenset({host})

    def get(url: str) -> FetchResult:
        return fetch(
            url,
            allowed_hosts=hosts,
            timeout=timeout,
            max_redirects=0,
            headers={"Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"},
        )

    return get
