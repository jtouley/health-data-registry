"""The fetch rules every crawler shares (ADR 0016, section 4), beside crawl.Robots and crawl.fetcher.

One place for what was three policies (crawl.Robots, sitemaps.Pace with BrowserRobots, and the polite client a
listing crawl needs):

- **Scope.** A source fetches only from its own hosts, and only its catalogued URLs, paths under them, the paths
  its type owns, or paths its ``scope_paths`` allow. Every link is resolved against the page it came from and
  normalised first (no user name, no port, ``..`` resolved, percent-decoded once), and patterns match the whole
  path (``in_scope``). One source can never reach another source's host, nor inherit its robots.txt policy.
- **Pace.** The wait between two requests to a host is the longest of the policy floor (10 s on government and
  browser hosts), robots.txt's Crawl-delay or Request-rate, an API's published rate and the source's own
  ``min_interval_s``: config can only slow a crawl (``pace_s``). A long crawl writes the time of each request
  before sending it (``PaceStore``), so a crash or a restart can't send two requests closer than the pace, and a
  restored state is refused when it names another host or holds a value that isn't a time.
- **One crawler per host** for this user (``HostLock``), whatever lake or checkout it writes to.
- **robots.txt is used for at most 24 hours** (RFC 9309, 2.4) before it is read again (``ROBOTS_MAX_AGE_S``).
- **Provenance.** A URL is recorded with any key or token stripped from its query (``redact_url``).
- **Live reads** are clamped to the hosted call's deadline (``clamp_live``).
- **The 8-week stop rule** (owner decision, 25 Nov 2026; issue #75): a source stops being crawled only after 8
  weeks of continuous failure, measured in time, not runs; any success resets the streak (``FailureStreaks``).
"""

from __future__ import annotations

import json
import math
import os
import posixpath
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from health_data_registry.source_config import match_whole

if TYPE_CHECKING:
    from health_data_registry.registry import Source

ROBOTS_MAX_AGE_S: Final = 24 * 3600.0
DORMANT_AFTER: Final = timedelta(weeks=8)
LOCKS_ENV: Final = "HDR_CRAWL_LOCKS"
# Query parameters that carry a credential: never written to a manifest, a fetch log or an answer.
SECRET_PARAMS: Final = frozenset(
    {"key", "apikey", "api_key", "app_token", "token", "access_token", "auth", "signature", "sig", "password",
     "secret", "client_secret", "subscription-key"}
)  # fmt: skip


class ScopeRefused(ValueError):
    """A link outside the source's own hosts or declared paths; nothing was fetched."""

    code = "out_of_scope"


class CrawlStopped(RuntimeError):
    """A long crawl stopped on purpose (another crawler holds the host, its saved pace can't be trusted)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------------------------------------- links


def normalise(href: str, base: str | None = None) -> str:
    """The absolute https link ``href`` names (relative to ``base``), in one canonical form, or ScopeRefused.

    No user name or password, no port, no fragment; ``.`` and ``..`` segments resolved; the path percent-decoded
    once and re-encoded, so ``%2e%2e`` can't hide a ``..``; the host lower case.
    """
    text = href.strip()
    url = urljoin(base, text) if base else text
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise ScopeRefused(f"{href!r} is not a valid link") from e
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ScopeRefused(f"{href!r} is not an https link")
    if parts.username is not None or parts.password is not None or port is not None:
        raise ScopeRefused(f"{href!r} has a user name, password or port in it")
    path = unquote(parts.path or "/")
    if "\\" in path or "\x00" in path:
        raise ScopeRefused(f"{href!r} has a backslash or a null in its path")
    clean = posixpath.normpath(path)
    if clean.startswith("//"):
        clean = "/" + clean.lstrip("/")
    if path.endswith("/") and clean != "/":
        clean += "/"
    return urlunsplit(
        ("https", parts.hostname.lower(), quote(clean, safe="/-._~!$&'()*+,;=:@"), parts.query, "")
    )


def _under(path: str, base: str) -> bool:
    base = base.rstrip("/")
    return path == base or path.startswith(base + "/") or (base == "" and path.startswith("/"))


def in_scope(source: Source, url: str, *, own_paths: Iterable[str] = ()) -> bool:
    """May ``source`` fetch ``url``? Only on its own hosts, and only a catalogued URL, a path under one, a path its
    type owns (``own_paths``, exact) or a path one of its ``scope_paths`` matches whole."""
    try:
        norm = urlsplit(normalise(url))
    except ScopeRefused:
        return False
    if norm.hostname not in source.hosts:
        return False
    path = unquote(norm.path)
    for catalogued in (source.api_url, source.landing_url):
        c = urlsplit(normalise(catalogued))
        if c.hostname == norm.hostname and _under(path, unquote(c.path)):
            return True
    if path in set(own_paths):
        return True
    return any(match_whole(p, path) for p in source.scope_paths)


def redact_url(url: str) -> str:
    """``url`` with the value of every credential-like query parameter removed (provenance, logs, answers)."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k.lstrip("$").lower() in SECRET_PARAMS for k, _ in pairs):
        return url
    kept = [(k, v) for k, v in pairs if k.lstrip("$").lower() not in SECRET_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


# ------------------------------------------------------------------------------------------------- pace


def pace_s(*, floor: float, crawl_delay: float | None, request_rate: tuple[int, int] | None,
           api_interval: float | None, source_min: float | None) -> float:  # fmt: skip
    """Seconds between two requests to one host: the longest of everything that asks for a wait. A source's
    ``min_interval_s`` only ever adds to the wait; a ``Crawl-delay: 0`` or a missing value never removes one."""
    asked = [floor, crawl_delay or 0.0, api_interval or 0.0, source_min or 0.0]
    if request_rate is not None and request_rate[0] > 0:
        asked.append(request_rate[1] / request_rate[0])
    return max(max(asked), 0.0)


def _seconds(v: object) -> bool:
    """None, or a finite, non-negative number (not a bool): a saved value the pacing can trust."""
    return v is None or (
        isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    )


@dataclass
class PaceState:
    host: str
    last_request_at: float | None = None  # wall clock (time.time()), so it survives a restart
    pace_s: float | None = None


class PaceStore:
    """The time of the last request to each host, written whole before the request is sent.

    Kept with the lake (``crawl_state/pace/<host>.json``). A state that names another host, or holds a value that
    isn't a finite, non-negative number, or a last request in the future, is refused: its pacing can't be trusted.
    """

    def __init__(self, root: Path, *, clock: Callable[[], float]) -> None:
        self.root = root
        self.clock = clock

    def path(self, host: str) -> Path:
        return self.root / "crawl_state" / "pace" / f"{host}.json"

    def load(self, host: str) -> PaceState:
        p = self.path(host)
        if not p.is_file():
            return PaceState(host=host)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise CrawlStopped("state_unreadable", f"the saved pace for {host} ({p}) can't be read") from e
        if not isinstance(raw, dict) or raw.get("host") != host:
            raise CrawlStopped(
                "state_host_mismatch", f"the saved pace {p} was not written for {host}, so it was not reused"
            )
        last, pace = raw.get("last_request_at"), raw.get("pace_s")
        if not (_seconds(last) and _seconds(pace)) or (last is not None and last > self.clock()):
            raise CrawlStopped(
                "state_unreadable",
                f"the saved pace {p} holds a value that isn't a time, so it was not reused",
            )
        return PaceState(host=host, last_request_at=last, pace_s=pace)

    def save(self, state: PaceState) -> None:
        atomic_write(self.path(state.host), json.dumps(asdict(state), sort_keys=True) + "\n")


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` whole or not at all: a temporary file, fsync, rename, then fsync the folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def host_lock_path(host: str) -> Path:
    """The per-user lock file for crawling ``host``: the same for every lake and checkout of this user."""
    base = os.environ.get(LOCKS_ENV, "").strip()
    if base:
        return Path(base) / f"{host}.lock"
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return root / "health-data-registry" / "locks" / f"{host}.lock"


class HostLock:
    """An exclusive, non-blocking ``flock`` on one host's lock file: one crawler per host for this user.

    The operating system drops the lock when the process ends, however it ends, so a crash leaves no stale lock.
    """

    def __init__(self, host: str, path: Path | None = None) -> None:
        import fcntl

        self.path = path or host_lock_path(host)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            raise CrawlStopped(
                "host_locked",
                f"another crawler of {host} is running (lock {self.path}), so this one did not start; wait for it "
                "to finish or stop it first",
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

    def __enter__(self) -> HostLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ------------------------------------------------------------------------------------------------- live reads


def clamp_live(count: int, per_item_s: float) -> int:
    """How many items a live read may fetch: ``count``, cut to what the hosted call's deadline leaves time for
    at ``per_item_s`` each (at least one, so the read can say in plain words that it ran out of time)."""
    from health_data_registry import deadline

    left = deadline.remaining()
    if left is None or per_item_s <= 0:
        return count
    return max(1, min(count, int(left // per_item_s)))


# ------------------------------------------------------------------------------------------------- 8-week rule


@dataclass
class Streak:
    source: str
    first_failure_at: str | None = None
    last_failure_at: str | None = None
    failed_runs: int = 0
    last_status: str | None = None

    def failing_for(self, now: datetime) -> timedelta:
        if self.first_failure_at is None:
            return timedelta(0)
        return now - datetime.fromisoformat(self.first_failure_at)

    def dormant(self, now: datetime) -> bool:
        """At least 8 weeks of continuous failure, by time, not by how many runs failed."""
        return self.first_failure_at is not None and self.failing_for(now) >= DORMANT_AFTER


class Dormant(RuntimeError):
    """The source has failed continuously for 8 weeks; it is not crawled again until the owner reactivates it."""

    code = "dormant"


class FailureStreaks:
    """Each source's failure streak, kept with the lake (``crawl_state/streaks/<source>.json``).

    Within a run nothing changes (blocks are for that run only). Across runs a failure is never permanent: each
    run tries again, a success resets the streak, and only 8 weeks of continuous failure marks the source dormant.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, source: str) -> Path:
        return self.root / "crawl_state" / "streaks" / f"{source}.json"

    def get(self, source: str) -> Streak:
        p = self.path(source)
        if not p.is_file():
            return Streak(source=source)
        raw = json.loads(p.read_text(encoding="utf-8"))
        return Streak(**{k: raw.get(k) for k in ("first_failure_at", "last_failure_at", "last_status")},
                      source=source, failed_runs=int(raw.get("failed_runs") or 0))  # fmt: skip

    def failed(self, source: str, when: datetime, status: str) -> Streak:
        s = self.get(source)
        stamp = when.astimezone(UTC).isoformat()
        s.first_failure_at = s.first_failure_at or stamp
        s.last_failure_at, s.last_status = stamp, status
        s.failed_runs += 1
        atomic_write(self.path(source), json.dumps(asdict(s), sort_keys=True) + "\n")
        return s

    def succeeded(self, source: str) -> None:
        self.path(source).unlink(missing_ok=True)

    def check(self, source: str, now: datetime) -> Streak:
        """The streak, or Dormant when the source has failed continuously for 8 weeks."""
        s = self.get(source)
        if s.dormant(now):
            raise Dormant(
                f"{source} has failed on every run since {str(s.first_failure_at)[:10]} ({s.failed_runs} runs; last: "
                f"{s.last_status}), 8 weeks or more, so it is not crawled again until the owner reactivates it "
                f"(delete {self.path(source)}). Answers keep using its last good snapshot and say it is stale."
            )
        return s


class HostPace:
    """At least ``seconds`` between two requests to one host, in memory, for a job that reads few pages."""

    def __init__(self, seconds: float, *, sleep: Callable[[float], None], clock: Callable[[], float]) -> None:
        if not seconds > 0:
            raise ValueError(f"a pace is a positive number of seconds, got {seconds!r}")
        self.seconds, self.sleep, self.clock = seconds, sleep, clock
        self.last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if host in self.last:
            due = self.last[host] + self.seconds - self.clock()
            if due > 0:
                self.sleep(due)
        self.last[host] = self.clock()
