"""The hosted, read-only MCP server over Streamable HTTP: the connector clinicians add in Claude, no sign-in.

`health-data-registry serve --http` runs it (a Hugging Face Docker Space in production; see docs/dev/hosting.md).
The same tools as the stdio server (server.build_server), with these rules on top:

- Reads only an immutable release of the open data hub (HDR_HUB, default HOSTED_HUB; resolved once to its
  newest release-<date> tag and kept for the life of the process) and restricted sources read live per their
  rules (live.py, memory only). Never a local lake: the engine's lake is an empty directory nothing writes to.
- Stateless Streamable HTTP (the SDK's stateless_http): no server-side session state, so a restart or a
  Space waking from sleep never breaks a client. The server still hands out an opaque MCP-Session-Id on
  initialize (the specification allows it, and clients send it back), signed with the server's key
  (HDR_SIGNING_KEY, else a per-process key); it is used only as the key for the per-session rate limit and is
  never stored with anything else or logged. A call without a session id the server signed shares one bucket
  with every other such call.
- A request body over MAX_BODY_BYTES (1 MiB; a tool call is a few KB) is refused with 413 before more is read.
- Bound to 127.0.0.1 unless told otherwise; the Host and Origin headers are checked (DNS-rebinding protection)
  whenever the bind address isn't the explicitly public 0.0.0.0 or ::.
- One deadline per tool call (HDR_TOOL_TIMEOUT_S, default 200 s, under claude.ai's 240 s) that caps every
  fetch, retry and download (deadline.py); one call reads the engine at a time.
- Every query runs in a child process with an OS memory limit and a hard kill at the deadline (isolation.py), so
  no query can outrun its deadline, exhaust the server's memory or touch the server's state. Anything that outlives
  its call anyway (a child that won't die, a tool body stuck in a library call, a hub download still running)
  poisons the engine until it ends: new questions get a plain BUSY refusal that says so, and /healthz reports it.
- Long answers come in pages (paging.py, in the tools layer, so stdio and the CLI page the same way): each page
  stays under HDR_PAGE_CHARS (default 120,000, both copies counted), carries real rows, its provenance and the
  exact call for the next page. HDR_MAX_RESULT_CHARS (default 140,000, under claude.ai's ~150,000) stays as a
  backstop only: a result over it (one row alone too long for a page) is replaced by a plain message.
- Rate limits per MCP session and overall (HDR_RATE_PER_SESSION, HDR_RATE_GLOBAL, tool calls per minute),
  never per IP address: every claude.ai user arrives from the same Anthropic address range. Over the limit,
  HTTP 429 with a plain-words JSON-RPC error and Retry-After.
- Privacy: no request body, tool argument, query string, prompt, session id or IP address is ever logged.
  uvicorn's access log is off; every other library's log record is reduced to its logger name, level and
  exception type. The only log lines with content are aggregate counters (tool name, status, latency bucket).
  No analytics, no telemetry.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import math
import os
import secrets
import sys
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from health_data_registry import __version__, tools
from health_data_registry.deadline import DeadlineExceeded, call_deadline, stragglers
from health_data_registry.isolation import POISONED
from health_data_registry.paging import MIN_PAGE_CHARS, page_budget, sign, wire_size
from health_data_registry.server import Fmt, Out

# The project's open data hub. The hosted server reads only its tagged releases.
HOSTED_HUB = "hf://datasets/jtouley/health-data-registry"
MCP_PATH = "/mcp"
SESSION_HEADER = "mcp-session-id"
METRICS_LOGGER = "health_data_registry.hosted.metrics"
GRACE_S = 10.0  # after the deadline, how long the guard waits for a fetch to notice before answering anyway
_MAX_KEYS = 10_000  # rate-limit buckets kept (least recently used dropped), so memory stays bounded
MAX_BODY_BYTES = (
    1024 * 1024
)  # a tools/call body is a few KB; anything far larger is refused before it is read
MAX_WAITING = 8  # tool calls that may queue for the engine at once; more get BUSY at once, holding no thread
ANONYMOUS = "anonymous"  # the one rate-limit bucket for calls without a session id this server signed
PUBLIC_BINDS = frozenset({"0.0.0.0", "::"})  # noqa: S104 - the explicitly public addresses (the Space)

log = logging.getLogger(METRICS_LOGGER)


def _env_float(name: str, default: float) -> float:
    """A positive, finite number from the environment, or the default when unset.

    Every one of these is a limit (a deadline, a size cap, a rate); 0, a negative number, inf or nan would turn
    the limit off or break it, so the server refuses to start rather than run without it.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number; got {raw!r}. Unset it to use {default:g}.")
    return value


def _env_int(name: str, default: int, minimum: int) -> int:
    """A whole number of at least `minimum` from the environment, or the default when unset; else refuse to start."""
    raw = os.environ.get(name)
    if not raw:
        return default
    value = int(raw) if raw.strip().isdigit() else 0
    if value < minimum:
        raise ValueError(
            f"{name} must be a whole number of at least {minimum:,}; got {raw!r}. Unset it to use {default:,}."
        )
    return value


def check_limits(cfg: HostedConfig) -> None:
    """Refuse to start when a page could never fit under the result cap (every long answer would be dropped)."""
    budget = page_budget()  # raises on a bad HDR_PAGE_CHARS
    if budget > cfg.max_result_chars:
        raise ValueError(
            f"HDR_PAGE_CHARS ({budget:,}) is above HDR_MAX_RESULT_CHARS ({cfg.max_result_chars:,}), so long "
            "answers would be refused instead of paged. Set HDR_PAGE_CHARS below the cap, or unset both."
        )


def default_cache_dir() -> Path:
    """HDR_CACHE_DIR; else /data (a Space's attached storage) when writable; else /tmp."""
    env = os.environ.get("HDR_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    data = Path("/data")
    if data.is_dir() and os.access(data, os.W_OK):
        return data / "health-data-registry"
    return Path("/tmp/health-data-registry")  # noqa: S108 - the Space's own scratch disk, nothing shared


@dataclass(frozen=True)
class HostedConfig:
    hub_url: str = field(default_factory=lambda: os.environ.get("HDR_HUB") or HOSTED_HUB)
    cache: Path = field(default_factory=default_cache_dir)
    tool_timeout_s: float = field(default_factory=lambda: _env_float("HDR_TOOL_TIMEOUT_S", 200.0))
    max_result_chars: int = field(
        default_factory=lambda: _env_int("HDR_MAX_RESULT_CHARS", 140_000, minimum=MIN_PAGE_CHARS)
    )
    rate_per_session: float = field(default_factory=lambda: _env_float("HDR_RATE_PER_SESSION", 30))
    rate_global: float = field(default_factory=lambda: _env_float("HDR_RATE_GLOBAL", 300))
    metrics_every_s: float = field(default_factory=lambda: _env_float("HDR_METRICS_LOG_S", 3600))


# ----------------------------------------------------------------------------------------------- counters


def latency_bucket(seconds: float) -> str:
    for limit, name in ((1, "<1s"), (5, "1-5s"), (30, "5-30s"), (120, "30-120s")):
        if seconds < limit:
            return name
    return ">120s"


class Metrics:
    """Aggregate counters only: (tool, status, latency bucket) -> count. Logged as a summary now and then."""

    def __init__(self, every_s: float = 3600, clock: Callable[[], float] = time.monotonic) -> None:
        self.counts: Counter[tuple[str, str, str]] = Counter()
        self._every = every_s
        self._clock = clock
        self._last = clock()
        self._lock = threading.Lock()

    def record(self, tool: str, status: str, seconds: float) -> None:
        with self._lock:
            self.counts[(tool, status, latency_bucket(seconds))] += 1
            due = self._clock() - self._last >= self._every
        if due:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            summary = {"|".join(k): v for k, v in sorted(self.counts.items())}
            self.counts.clear()
            self._last = self._clock()
        if summary:
            log.info("tool calls since last summary: %s", json.dumps(summary))


# ------------------------------------------------------------------------------------------- the guard


def too_large_message(size: int, limit: int) -> str:
    return (
        f"One row of this answer is too long to send in a reply (about {size:,} characters; the limit is "
        f"{limit:,}), so nothing was sent. Ask again for fewer columns or one measure, or use describe_dataset to "
        "see which columns hold long text."
    )


BUSY = (
    "The hosted server is busy answering other questions and couldn't start this one in time, so no numbers "
    "are shown. Try again in a minute."
)


class CallGuard:
    """Runs one tool body under the hosted rules (server.Guard): a deadline, one engine user, the size cap.

    The engine lock is never held past a call's deadline: when a body ignores its deadline (stuck inside a library
    call), the guard interrupts every engine connection, takes the lock back from the stuck worker and answers.
    The worker never releases a lock it no longer owns. A thread can't be killed, so until the stuck worker ends
    the engine is poisoned: every new call is refused in plain words (POISONED) before it touches the engine, so
    nothing the stuck worker does can mix with the next question. The same holds while a query child won't die
    (isolation.Isolation.poisoned) or a bounded download outlived its call (deadline.stragglers). At most
    MAX_WAITING calls queue for the engine; the rest get BUSY at once.
    """

    def __init__(self, cfg: HostedConfig, metrics: Metrics, engine: Any = None) -> None:
        self.cfg = cfg
        self.metrics = metrics
        self.engine = engine
        self._lock = threading.Lock()  # query.Engine holds one DuckDB connection and is not thread safe
        self._state = threading.Lock()  # guards _owner and _waiting
        self._owner: object | None = None  # the call that holds _lock now
        self._waiting = 0
        self._stuck: list[threading.Thread] = []  # workers that outlived their deadline and are still running

    def poisoned(self) -> bool:
        """True while anything that outlived its call is still running; it clears itself when they end."""
        with self._state:
            self._stuck = [t for t in self._stuck if t.is_alive()]
            stuck = bool(self._stuck)
        isolation = getattr(self.engine, "isolation", None)
        return stuck or stragglers() > 0 or (isolation is not None and bool(isolation.poisoned()))

    def _interrupt(self) -> None:
        """Interrupt every connection a call may be using: the engine's and its pinned engines' (later pages)."""
        engines = getattr(self.engine, "engines", None)
        for eng in engines() if callable(engines) else [self.engine]:
            con = getattr(eng, "_con", None)
            if con is not None:
                with contextlib.suppress(Exception):  # best effort; the deadline answer goes out regardless
                    con.interrupt()

    def _acquire(self, owner: object, timeout: float) -> bool:
        with self._state:
            if self._waiting >= MAX_WAITING:
                return False
            self._waiting += 1
        try:
            got = self._lock.acquire(timeout=max(0.0, timeout))
        finally:
            with self._state:
                self._waiting -= 1
        if got:
            with self._state:
                self._owner = owner
        return got

    def _release(self, owner: object) -> None:
        """Release the lock if `owner` still holds it (the guard may have taken it back already)."""
        with self._state:
            if self._owner is not owner:
                return
            self._owner = None
        self._lock.release()

    def __call__(self, tool: str, fn: Callable[[], Out], fmt: Fmt) -> Out:
        start = time.monotonic()
        budget = self.cfg.tool_timeout_s
        box: dict[str, Any] = {"status": "error"}
        owner = object()

        if self.poisoned():  # refused before any thread or lock: the earlier work may still be running
            self.metrics.record(tool, "poisoned", time.monotonic() - start)
            return tools.message(POISONED, fmt, refused="busy")

        def work() -> None:
            if not self._acquire(owner, budget - (time.monotonic() - start)):
                box["status"], box["out"] = "busy", tools.message(BUSY, fmt, refused="busy")
                return
            try:
                if self.poisoned():  # became poisoned while this call waited for the engine
                    box["status"], box["out"] = "poisoned", tools.message(POISONED, fmt, refused="busy")
                    return
                left = budget - (time.monotonic() - start)
                with call_deadline(left):
                    box["out"], box["status"] = fn(), "ok"
            except DeadlineExceeded:
                box["status"] = "timeout"
                box["out"] = tools.refusal(DeadlineExceeded(budget), fmt)
            except Exception as e:  # noqa: BLE001 - every failure becomes plain words
                box["status"] = "refused" if getattr(e, "code", None) else "error"
                box["out"] = tools.refusal(e, fmt)
            finally:
                self._release(owner)

        worker = threading.Thread(target=copy_context().run, args=(work,), daemon=True, name=f"tool-{tool}")
        worker.start()
        worker.join(budget + GRACE_S)
        if worker.is_alive():  # the body ignored its deadline (e.g. inside a library call): answer anyway
            self._interrupt()
            with self._state:
                self._stuck.append(worker)  # poisoned until it ends: no new call runs beside it
            self._release(owner)  # never let the stuck worker hold the engine for the calls behind it
            box["status"], box["out"] = "timeout", tools.refusal(DeadlineExceeded(budget), fmt)
        out: Out = box["out"]
        size = wire_size(out)
        if size > self.cfg.max_result_chars:
            box["status"] = "too_large"
            out = tools.message(too_large_message(size, self.cfg.max_result_chars), fmt, refused="too_large")
        self.metrics.record(tool, box["status"], time.monotonic() - start)
        return out


# ---------------------------------------------------------------------------------------- rate limits


class RateLimiter:
    """Token buckets: one per MCP session and one overall, in tool calls per minute (bursts up to that many).

    A call is checked against both buckets before a token is taken from either, so a refused call costs nothing
    and writes nothing; the map is trimmed on every check, so it stays under _MAX_KEYS however many session ids
    arrive. Calls without a session id the server signed share one bucket (ANONYMOUS).
    """

    def __init__(
        self, per_session: float, overall: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.per_session = per_session
        self.overall = overall
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _level(self, key: str, per_min: float, now: float) -> float:
        tokens, then = self._buckets.get(key, (per_min, now))
        return min(per_min, tokens + (now - then) * per_min / 60)

    def check(self, session: str | None) -> tuple[str, float] | None:
        """None if the call may go ahead; else (which limit, seconds to wait)."""
        with self._lock:
            try:
                now = self._clock()
                key = "s:" + session if session is not None else ANONYMOUS
                mine = self._level(key, self.per_session, now)
                if mine < 1:
                    return "session", (1 - mine) * 60 / self.per_session
                everyone = self._level("*", self.overall, now)
                if everyone < 1:
                    return "overall", (1 - everyone) * 60 / self.overall
                self._buckets[key] = (mine - 1, now)
                self._buckets["*"] = (everyone - 1, now)
                self._buckets.move_to_end(key)
                return None
            finally:
                while len(self._buckets) > _MAX_KEYS:
                    self._buckets.popitem(last=False)


def rate_limited_message(which: str, wait: float) -> str:
    who = "this conversation" if which == "session" else "everyone using the hosted server right now"
    return (
        f"Too many questions from {who} in the last minute, so this one wasn't run. Wait about "
        f"{max(1, round(wait))} seconds and ask again; nothing was lost."
    )


def new_session_id() -> str:
    """An opaque session id this server can recognise later: a random nonce, then its signature."""
    nonce = secrets.token_hex(16)
    return f"{nonce}.{sign('session', nonce)}"


def _session_id(scope: Scope) -> str | None:
    """The client's session id, only if this server issued it (signed); anything else counts as no session."""
    for k, v in scope.get("headers", []):
        if k == SESSION_HEADER.encode():
            nonce, _, sig = str(v.decode("latin-1")).partition(".")
            if len(nonce) == 32 and len(sig) == 32 and hmac.compare_digest(sig, sign("session", nonce)):
                return nonce
            return None
    return None


def _content_length(scope: Scope) -> int | None:
    for k, v in scope.get("headers", []):
        if k == b"content-length":
            raw = v.decode("latin-1").strip()
            return int(raw) if raw.isdigit() else None
    return None


TOO_LARGE_REQUEST = (
    "This request is larger than the hosted server accepts (over 1 MiB), so it wasn't read. A question to this "
    "server is a few thousand characters at most: ask it again in fewer words."
)


class HostedMiddleware:
    """Rate limits tool calls and hands out an opaque session id on initialize. Logs nothing about requests."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter, metrics: Metrics, tool_names: set[str]) -> None:
        self.app = app
        self.limiter = limiter
        self.metrics = metrics
        self.tool_names = tool_names

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") != MCP_PATH or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        declared = _content_length(scope)
        if declared is not None and declared > MAX_BODY_BYTES:
            await _request_too_large()(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:  # stop reading at the cap: nothing more is held in memory
                await _request_too_large()(scope, receive, send)
                return
            chunks.append(chunk)
            more = message.get("more_body", False)
        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        try:
            msg = json.loads(body)
        except ValueError:
            msg = None  # the SDK answers malformed bodies itself
        session = _session_id(scope)
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            params = msg.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            # A client sends any JSON here: only a known tool name is a label; anything else (a list, an
            # object) is "other" and still counts against the limit, never a crash before it.
            tool = name if isinstance(name, str) and name in self.tool_names else "other"
            limited = self.limiter.check(session)
            if limited is not None:
                which, wait = limited
                self.metrics.record(tool, "rate_limited", 0.0)
                await _too_many(msg.get("id"), which, wait)(scope, replay, send)
                return
        if isinstance(msg, dict) and msg.get("method") == "initialize" and session is None:
            new_id = new_session_id()

            async def send_with_session(message: Message) -> None:
                if message["type"] == "http.response.start" and message["status"] == 200:
                    headers = list(message.get("headers", []))
                    if not any(k.lower() == SESSION_HEADER.encode() for k, _ in headers):
                        headers.append((SESSION_HEADER.encode(), new_id.encode()))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, replay, send_with_session)
            return
        await self.app(scope, replay, send)


def _request_too_large() -> Response:
    body = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": TOO_LARGE_REQUEST}}
    return JSONResponse(body, status_code=413)


def _too_many(request_id: Any, which: str, wait: float) -> Response:
    body = {
        "jsonrpc": "2.0",
        "id": request_id if isinstance(request_id, str | int) else None,
        "error": {"code": -32000, "message": rate_limited_message(which, wait)},
    }
    return JSONResponse(body, status_code=429, headers={"Retry-After": str(max(1, round(wait)))})


# ---------------------------------------------------------------------------------------------- logging


class PrivacyFilter(logging.Filter):
    """Only the counters logger speaks freely; every other record keeps its logger, level and exception type.

    Libraries (the MCP SDK, uvicorn, httpx, pydantic) can put request content in a message or an exception
    (a validation error quotes its input), so their text never reaches the log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == METRICS_LOGGER:
            return True
        if record.levelno < logging.WARNING:
            return False
        kind = record.exc_info[1].__class__.__name__ if record.exc_info and record.exc_info[1] else None
        record.msg = f"{record.name}: {record.levelname.lower()}" + (f" ({kind})" if kind else "")
        record.msg += " - details withheld so no question or argument is ever logged"
        record.args = None
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def configure_logging(stream: Any = None) -> logging.Handler:
    """Replace every root handler with one privacy-filtered handler, and silence the libraries' own handlers."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(PrivacyFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "mcp", "httpx", "httpcore", "huggingface_hub"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    return handler


# ------------------------------------------------------------------------------------------------- app


@dataclass
class Hosted:
    """Everything one hosted process holds: the engine, the guard, the limiter, the counters, the warm state."""

    cfg: HostedConfig
    registry: Any
    engine: Any
    guard: CallGuard
    limiter: RateLimiter
    metrics: Metrics
    warm: dict[str, Any] = field(default_factory=lambda: {"done": False, "release": None})

    def warm_up(self) -> None:
        """Resolve the hub release and read its manifest, so the first question needn't wait for it."""
        try:
            hub = self.engine.hub
            if hub is not None:
                self.warm["release"] = hub.revision()
                hub.release()
            self.engine.connect()
        except Exception as e:  # noqa: BLE001 - a failed warm-up is retried by the first question
            log.info("warm start incomplete: %s", getattr(e, "code", e.__class__.__name__))
        finally:
            self.warm["done"] = True


def build_hosted(
    cfg: HostedConfig | None = None,
    registry: Any = None,
    engine: Any = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    host: str = "127.0.0.1",
) -> tuple[Any, Hosted]:
    """The ASGI app and its state. Pass `registry` and `engine` in tests; the defaults read the hub release.

    `host` is the address the server binds to: Host and Origin are checked unless it is explicitly public.
    """
    from health_data_registry.hub import Hub
    from health_data_registry.isolation import Isolation
    from health_data_registry.query import Engine
    from health_data_registry.registry import load_registry
    from health_data_registry.server import build_server

    cfg = cfg or HostedConfig()
    reg = registry or load_registry()
    if engine is None:
        cfg.cache.mkdir(parents=True, exist_ok=True)
        hub = Hub(url=cfg.hub_url, cache=cfg.cache)
        # Never a local lake: an empty directory that nothing writes to, so only the hub and live tiers answer.
        # Every query runs in a killable child process (isolation.py).
        engine = Engine(registry=reg, lake=cfg.cache / "no-local-lake", hub=hub, cache=cfg.cache,
                        isolation=Isolation())  # fmt: skip
    metrics = Metrics(cfg.metrics_every_s, clock=clock)
    guard = CallGuard(cfg, metrics, engine)
    limiter = RateLimiter(cfg.rate_per_session, cfg.rate_global, clock=clock)
    state = Hosted(cfg, reg, engine, guard, limiter, metrics)
    mcp = build_server(reg, engine, guard=guard)

    async def healthz(_: Request) -> Response:
        poisoned = guard.poisoned()
        isolation = getattr(engine, "isolation", None)
        return JSONResponse(
            {
                "status": "poisoned" if poisoned else "ok",
                "poisoned": poisoned,
                "version": __version__,
                "warm": state.warm["done"],
                "release": state.warm["release"],
                "query_isolation": isolation.status() if isolation is not None else None,
            },
            status_code=503 if poisoned else 200,
        )

    async def index(request: Request) -> Response:
        return PlainTextResponse(
            "health-data-registry: a read-only MCP server for clinicians.\n\nTo use it in Claude, add a custom "
            f"connector with this address: {connector_url(request)}\nThere is no sign-in, and your questions are "
            "never logged.\n"
        )

    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        host=host,
        transport_security=transport_security(host),
    )
    app.add_route("/healthz", healthz, methods=["GET"])
    app.add_route("/", index, methods=["GET"])
    names = {t.name for t in mcp._tool_manager.list_tools()}
    return HostedMiddleware(app, limiter, metrics, names), state


def transport_security(host: str) -> TransportSecuritySettings | None:
    """DNS-rebinding protection (Host and Origin checks) unless the bind address is explicitly public.

    None lets the SDK decide: off for 0.0.0.0 or :: (the Space, behind its own proxy), and its localhost allowlist
    for 127.0.0.1, localhost and ::1. Any other address gets an allowlist of just that address.
    """
    if host in PUBLIC_BINDS or host in ("127.0.0.1", "localhost", "::1"):
        return None
    name = f"[{host}]" if ":" in host else host
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[name, f"{name}:*"],
        allowed_origins=[f"http://{name}", f"http://{name}:*", f"https://{name}", f"https://{name}:*"],
    )


def connector_url(request: Request) -> str:
    """The address to add in Claude, built from the request (only ever displayed, never followed)."""
    base = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https" and base.startswith("http://"):
        base = "https://" + base.removeprefix("http://")  # behind the Space's HTTPS proxy
    return base + MCP_PATH


def uvicorn_config(app: Any, host: str, port: int) -> Any:
    """uvicorn with no access log and no logging setup of its own (so the privacy filter sees everything).

    Nothing is left for uvicorn to guess: the interface is named (ASGI 3), and the app is handed over as a factory,
    so uvicorn never probes it by calling it with no arguments. A wrapper around the middleware (a profiler, a
    mutation-testing trampoline) can then never be mistaken for a factory or for an ASGI 2 app.
    """
    import uvicorn

    return uvicorn.Config(
        lambda: app, factory=True, interface="asgi3", host=host, port=port, access_log=False, log_config=None,
        server_header=False, proxy_headers=False,
    )  # fmt: skip


def main(host: str = "127.0.0.1", port: int = 7860) -> None:
    import uvicorn

    configure_logging()
    cfg = HostedConfig()
    check_limits(cfg)
    app, state = build_hosted(cfg, host=host)
    configure_logging()  # the SDK set up its own handler while the server was built; take it back
    threading.Thread(target=state.warm_up, daemon=True, name="warm-up").start()
    log.info("serving %s on port %d", MCP_PATH, port)
    uvicorn.Server(uvicorn_config(app, host, port)).run()
