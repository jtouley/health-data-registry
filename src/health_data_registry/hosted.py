"""The hosted, read-only MCP server over Streamable HTTP: the connector clinicians add in Claude, no sign-in.

`health-data-registry serve --http` runs it (a Hugging Face Docker Space in production; see docs/dev/hosting.md).
The same tools as the stdio server (server.build_server), with these rules on top:

- Reads only an immutable release of the open data hub (HDR_HUB, default HOSTED_HUB; resolved once to its
  newest release-<date> tag and kept for the life of the process) and restricted sources read live per their
  rules (live.py, memory only). Never a local lake: the engine's lake is an empty directory nothing writes to.
- Stateless Streamable HTTP (the SDK's stateless_http): no server-side session state, so a restart or a
  Space waking from sleep never breaks a client. The server still hands out an opaque MCP-Session-Id on
  initialize (the specification allows it, and clients send it back); it is used only as the key for the
  per-session rate limit and is never stored with anything else or logged.
- One deadline per tool call (HDR_TOOL_TIMEOUT_S, default 200 s, under claude.ai's 240 s) that caps every
  fetch, retry and download (deadline.py); one call reads the engine at a time.
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

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from health_data_registry import __version__, tools
from health_data_registry.deadline import DeadlineExceeded, call_deadline
from health_data_registry.paging import page_budget, wire_size
from health_data_registry.server import Fmt, Out

# The project's open data hub. The hosted server reads only its tagged releases.
HOSTED_HUB = "hf://datasets/jtouley/health-data-registry"
MCP_PATH = "/mcp"
SESSION_HEADER = "mcp-session-id"
METRICS_LOGGER = "health_data_registry.hosted.metrics"
GRACE_S = 10.0  # after the deadline, how long the guard waits for a fetch to notice before answering anyway
_MAX_KEYS = 10_000  # rate-limit buckets kept (least recently used dropped), so memory stays bounded

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
    max_result_chars: int = field(default_factory=lambda: int(_env_float("HDR_MAX_RESULT_CHARS", 140_000)))
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
        f"This answer is too long to send in one reply (about {size:,} characters; the limit is {limit:,}), "
        "even one page at a time, so nothing was sent. Narrow your question: fewer places, a shorter span of "
        "years, one condition or one measure, or ask for a summary first."
    )


BUSY = (
    "The hosted server is busy answering other questions and couldn't start this one in time, so no numbers "
    "are shown. Try again in a minute."
)


class CallGuard:
    """Runs one tool body under the hosted rules (server.Guard): a deadline, one engine user, the size cap."""

    def __init__(self, cfg: HostedConfig, metrics: Metrics, engine: Any = None) -> None:
        self.cfg = cfg
        self.metrics = metrics
        self.engine = engine
        self._lock = threading.Lock()  # query.Engine holds one DuckDB connection and is not thread safe

    def _interrupt(self) -> None:
        con = getattr(self.engine, "_con", None)
        if con is not None:
            with contextlib.suppress(Exception):  # best effort; the deadline answer goes out regardless
                con.interrupt()

    def __call__(self, tool: str, fn: Callable[[], Out], fmt: Fmt) -> Out:
        start = time.monotonic()
        budget = self.cfg.tool_timeout_s
        box: dict[str, Any] = {"status": "error"}

        def work() -> None:
            if not self._lock.acquire(timeout=budget):
                box["status"], box["out"] = "busy", tools.message(BUSY, fmt, refused="busy")
                return
            try:
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
                self._lock.release()

        worker = threading.Thread(target=copy_context().run, args=(work,), daemon=True, name=f"tool-{tool}")
        worker.start()
        worker.join(budget + GRACE_S)
        if worker.is_alive():  # the body ignored its deadline (e.g. inside a library call): answer anyway
            self._interrupt()
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
    """Token buckets: one per MCP session and one overall, in tool calls per minute (bursts up to that many)."""

    def __init__(
        self, per_session: float, overall: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.per_session = per_session
        self.overall = overall
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _take(self, key: str, per_min: float, now: float) -> float:
        """0 if a token was taken, else seconds until one is free."""
        tokens, then = self._buckets.get(key, (per_min, now))
        tokens = min(per_min, tokens + (now - then) * per_min / 60)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            self._buckets.move_to_end(key)
            return 0.0
        self._buckets[key] = (tokens, now)
        return (1 - tokens) * 60 / per_min

    def check(self, session: str | None) -> tuple[str, float] | None:
        """None if the call may go ahead; else (which limit, seconds to wait)."""
        with self._lock:
            now = self._clock()
            if session is not None:
                wait = self._take("s:" + session, self.per_session, now)
                if wait:
                    return "session", wait
            wait = self._take("*", self.overall, now)
            if wait:
                return "overall", wait
            while len(self._buckets) > _MAX_KEYS:
                self._buckets.popitem(last=False)
            return None


def rate_limited_message(which: str, wait: float) -> str:
    who = "this conversation" if which == "session" else "everyone using the hosted server right now"
    return (
        f"Too many questions from {who} in the last minute, so this one wasn't run. Wait about "
        f"{max(1, round(wait))} seconds and ask again; nothing was lost."
    )


def _session_id(scope: Scope) -> str | None:
    for k, v in scope.get("headers", []):
        if k == SESSION_HEADER.encode():
            sid = v.decode("latin-1")
            return sid if 0 < len(sid) <= 128 and sid.isascii() and sid.isprintable() else None
    return None


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
        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
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
            tool = name if name in self.tool_names else "other"
            limited = self.limiter.check(session)
            if limited is not None:
                which, wait = limited
                self.metrics.record(tool, "rate_limited", 0.0)
                await _too_many(msg.get("id"), which, wait)(scope, replay, send)
                return
        if isinstance(msg, dict) and msg.get("method") == "initialize" and session is None:
            new_id = secrets.token_hex(16)

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
) -> tuple[Any, Hosted]:
    """The ASGI app and its state. Pass `registry` and `engine` in tests; the defaults read the hub release."""
    from health_data_registry.hub import Hub
    from health_data_registry.query import Engine
    from health_data_registry.registry import load_registry
    from health_data_registry.server import build_server

    cfg = cfg or HostedConfig()
    reg = registry or load_registry()
    if engine is None:
        cfg.cache.mkdir(parents=True, exist_ok=True)
        hub = Hub(url=cfg.hub_url, cache=cfg.cache)
        # Never a local lake: an empty directory that nothing writes to, so only the hub and live tiers answer.
        engine = Engine(registry=reg, lake=cfg.cache / "no-local-lake", hub=hub, cache=cfg.cache)
    metrics = Metrics(cfg.metrics_every_s, clock=clock)
    guard = CallGuard(cfg, metrics, engine)
    limiter = RateLimiter(cfg.rate_per_session, cfg.rate_global, clock=clock)
    state = Hosted(cfg, reg, engine, guard, limiter, metrics)
    mcp = build_server(reg, engine, guard=guard)

    async def healthz(_: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "warm": state.warm["done"],
                "release": state.warm["release"],
            }
        )

    async def index(_: Request) -> Response:
        return PlainTextResponse(
            "health-data-registry: a read-only MCP server for clinicians. Add "
            f"<this address>{MCP_PATH} as a custom connector in Claude. No sign-in; questions are never logged.\n"
        )

    app = mcp.streamable_http_app(streamable_http_path=MCP_PATH, stateless_http=True, host="0.0.0.0")  # noqa: S104 - a public server behind the Space's proxy
    app.add_route("/healthz", healthz, methods=["GET"])
    app.add_route("/", index, methods=["GET"])
    names = {t.name for t in mcp._tool_manager.list_tools()}
    return HostedMiddleware(app, limiter, metrics, names), state


def uvicorn_config(app: Any, host: str, port: int) -> Any:
    """uvicorn with no access log and no logging setup of its own (so the privacy filter sees everything)."""
    import uvicorn

    return uvicorn.Config(
        app, host=host, port=port, access_log=False, log_config=None, server_header=False, proxy_headers=False
    )


def main(host: str = "0.0.0.0", port: int = 7860) -> None:  # noqa: S104 - the Space routes its public port here
    import uvicorn

    configure_logging()
    cfg = HostedConfig()
    check_limits(cfg)
    app, state = build_hosted(cfg)
    configure_logging()  # the SDK set up its own handler while the server was built; take it back
    threading.Thread(target=state.warm_up, daemon=True, name="warm-up").start()
    log.info("serving %s on port %d", MCP_PATH, port)
    uvicorn.Server(uvicorn_config(app, host, port)).run()
