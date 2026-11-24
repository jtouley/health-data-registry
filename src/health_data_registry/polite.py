"""A polite crawler client: robots.txt first, the site's own crawl delay, and a budget that survives restarts.

Used for sites that ask crawlers to slow down (conference abstract sites such as CROI's). The rules:

- robots.txt is the first request of every run, and nothing else is requested before it is parsed.
- The delay between requests is the site's ``Crawl-delay`` for our user agent (or for ``*``), never a
  number written here; ``default_delay`` applies only when robots.txt names none.
- ``Disallow`` rules are obeyed: a disallowed link is refused before any request.
- The time of the last request is written to a state file *before* the request is sent, so a crash or a
  restart can't send two requests closer together than the delay.
- One call is one request: redirects are not followed silently (the fetcher is called with
  ``max_redirects=0``); a redirect is reported and the caller decides whether to request the new link.
- Every attempt is a row in the lake's fetch log (store.FetchLog), ok or error.

It runs only from the command line or the Python API, never inside the MCP server.
"""

from __future__ import annotations

import json
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


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class CrawlStopped(Exception):
    """The crawl stopped on purpose (robots, budget, an unreadable robots.txt). ``code`` is for programs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


Getter = Callable[[str], FetchResult]


@dataclass
class _State:
    last_request_at: float | None = None
    delay_s: float | None = None


class PoliteClient:
    """One site, one request at a time, at the pace its robots.txt asks for."""

    def __init__(
        self,
        base_url: str,
        *,
        state_path: Path,
        get_raw: Getter,
        log: FetchLog | None = None,
        clock: Clock | None = None,
        default_delay: float = 120.0,
        max_requests: int | None = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("the crawler only reads https:// sites")
        self.base = f"https://{parts.hostname}"
        self.host = parts.hostname
        self.state_path = state_path
        self._get_raw = get_raw
        self.log = log
        self.clock = clock or SystemClock()
        self.default_delay = default_delay
        self.max_requests = max_requests
        self.user_agent = user_agent
        self.requests_made = 0
        self.robots: RobotFileParser | None = None
        self.robots_text: str | None = None
        self.delay: float | None = None
        self._state = self._load_state()

    # ---------------------------------------------------------------- state

    def _load_state(self) -> _State:
        if not self.state_path.is_file():
            return _State()
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        return _State(last_request_at=raw.get("last_request_at"), delay_s=raw.get("delay_s"))

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"host": self.host, "last_request_at": self._state.last_request_at,
                        "delay_s": self._state.delay_s}) + "\n",
            encoding="utf-8",
        )  # fmt: skip
        tmp.replace(self.state_path)

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
        """Read and parse robots.txt. Always the first request of a run."""
        if self.robots is not None:
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
        delay = rp.crawl_delay(self.user_agent)
        self.robots, self.robots_text = rp, text
        self.delay = float(delay) if delay is not None else self.default_delay
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
