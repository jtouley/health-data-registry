"""The hosted server's parts one at a time: middleware, rate limits, privacy filter, guard and counters.

The round trip in test_hosted.py shows they work together; these pin each rule down exactly, so a change to one of
them (a limit off by one, a header dropped, a log line that leaks) fails a test.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import hosted
from health_data_registry.deadline import DeadlineExceeded
from health_data_registry.hosted import (
    BUSY,
    MCP_PATH,
    SESSION_HEADER,
    CallGuard,
    HostedConfig,
    HostedMiddleware,
    Metrics,
    PrivacyFilter,
    RateLimiter,
)

Scope = dict[str, Any]


def cfg(tmp_path: Path, **kw: Any) -> HostedConfig:
    base: dict[str, Any] = {"hub_url": "unused", "cache": tmp_path / "cache", "tool_timeout_s": 5.0,
                            "max_result_chars": 140_000, "rate_per_session": 30, "rate_global": 300,
                            "metrics_every_s": 3600}  # fmt: skip
    return HostedConfig(**{**base, **kw})


# ------------------------------------------------------------------------------------------ the middleware


class Inner:
    """A stand-in for the MCP app: records what reached it and answers 200 (or `status`)."""

    def __init__(self, status: int = 200, headers: list[tuple[bytes, bytes]] | None = None) -> None:
        self.calls: list[tuple[Scope, list[dict[str, Any]]]] = []
        self.status, self.headers = status, headers or []

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        got = [await receive(), await receive()]
        self.calls.append((scope, got))
        await send({"type": "http.response.start", "status": self.status, "headers": list(self.headers)})
        await send({"type": "http.response.body", "body": b"{}"})


def scope(
    method: str = "POST", path: str = MCP_PATH, session: str | None = None, kind: str = "http"
) -> Scope:
    headers = [(b"content-type", b"application/json")]
    if session is not None:
        headers.append((SESSION_HEADER.encode(), session.encode("latin-1")))
    return {"type": kind, "path": path, "method": method, "headers": headers}


def call(mw: HostedMiddleware, sc: Scope, *bodies: bytes, disconnect: bool = False) -> list[dict[str, Any]]:
    """Run one request through the middleware: the body in `bodies` chunks, then the next receive()."""
    queue: list[dict[str, Any]] = [
        {"type": "http.request", "body": b, "more_body": i < len(bodies) - 1} for i, b in enumerate(bodies)
    ]
    if disconnect:
        queue.insert(1, {"type": "http.disconnect"})
    after = {"type": "http.disconnect", "after": True}
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return queue.pop(0) if queue else after

    async def send(m: dict[str, Any]) -> None:
        sent.append(m)

    asyncio.run(mw(sc, receive, send))
    return sent


def mw_with(inner: Inner, per_session: float = 30, overall: float = 300) -> tuple[HostedMiddleware, Metrics]:
    m = Metrics()
    return HostedMiddleware(inner, RateLimiter(per_session, overall), m, {"get_series", "query_sql"}), m


def tool_call(name: Any = "get_series", rid: Any = 7) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name}}
    ).encode()


def headers_of(sent: list[dict[str, Any]]) -> dict[bytes, bytes]:
    return dict(sent[0]["headers"])


def test_other_paths_methods_and_scopes_pass_straight_through() -> None:
    for sc in (scope("GET"), scope(path="/healthz"), scope(path="/mcpx"), scope(kind="websocket"),
               scope("DELETE")):  # fmt: skip
        inner = Inner()
        mw, metrics = mw_with(inner, per_session=1, overall=1)
        for _ in range(3):  # never rate limited: only tool calls on POST /mcp count
            sent = call(mw, sc, b"x")
            assert sent[0]["status"] == 200 and SESSION_HEADER.encode() not in headers_of(sent)
        assert inner.calls[0][1][0] == {"type": "http.request", "body": b"x", "more_body": False}
        assert not metrics.counts


def test_a_trailing_slash_is_the_same_endpoint_and_the_body_is_replayed_once() -> None:
    inner = Inner()
    mw, _ = mw_with(inner)
    body = tool_call()
    sent = call(mw, scope(path=MCP_PATH + "/"), body[:10], body[10:])
    assert sent[0]["status"] == 200
    ((_, got),) = inner.calls
    assert got[0] == {"type": "http.request", "body": body, "more_body": False}  # whole body, in one piece
    assert got[1] == {"type": "http.disconnect", "after": True}  # then the real receive()


def test_a_disconnect_mid_body_stops_reading() -> None:
    inner = Inner()
    mw, _ = mw_with(inner)
    call(mw, scope(), b'{"a":', b"1}", disconnect=True)
    ((_, got),) = inner.calls
    assert got[0]["body"] == b'{"a":'  # only what arrived before the disconnect
    assert got[1] == {"type": "http.request", "body": b"1}", "more_body": False}


def test_tool_calls_are_rate_limited_with_429_retry_after_and_a_counter() -> None:
    inner = Inner()
    mw, metrics = mw_with(inner, per_session=2, overall=300)
    ok = [call(mw, scope(session="abc"), tool_call()) for _ in range(2)]
    assert all(s[0]["status"] == 200 for s in ok)
    sent = call(mw, scope(session="abc"), tool_call("query_sql", "r-9"))
    assert sent[0]["status"] == 429
    head = headers_of(sent)
    assert head[b"retry-after"] == b"30"  # 2 a minute: one token back in 30 s
    body = json.loads(sent[1]["body"])
    assert body == {"jsonrpc": "2.0", "id": "r-9", "error": {"code": -32000, "message": hosted.rate_limited_message("session", 30.0)}}  # fmt: skip
    assert len(inner.calls) == 2
    assert dict(metrics.counts) == {("query_sql", "rate_limited", "<1s"): 1}
    other = call(mw, scope(session="xyz"), tool_call("not-a-tool", [1]))  # another conversation still runs
    assert other[0]["status"] == 200
    call(mw, scope(session="xyz"), tool_call("not-a-tool", [1]))
    late = call(mw, scope(session="xyz"), tool_call({"x": 1}, {"bad": "id"}))
    assert late[0]["status"] == 429 and json.loads(late[1]["body"])["id"] is None
    assert metrics.counts[("other", "rate_limited", "<1s")] == 1


def test_the_overall_limit_covers_calls_without_a_session() -> None:
    mw, metrics = mw_with(Inner(), per_session=100, overall=1)
    assert call(mw, scope(), tool_call())[0]["status"] == 200
    sent = call(mw, scope(), json.dumps({"method": "tools/call", "id": 3, "params": "junk"}).encode())
    assert sent[0]["status"] == 429 and headers_of(sent)[b"retry-after"] == b"60"
    assert "everyone using the hosted server" in json.loads(sent[1]["body"])["error"]["message"]
    assert dict(metrics.counts) == {("other", "rate_limited", "<1s"): 1}
    # Not a tool call (a list, a bad body, another method): never counted against the limit.
    for body in (b"[1, 2]", b"{not json", json.dumps({"method": "tools/list"}).encode()):
        assert call(mw, scope(), body)[0]["status"] == 200


def test_initialize_without_a_session_gets_a_fresh_opaque_id() -> None:
    mw, _ = mw_with(Inner())
    init = json.dumps({"method": "initialize", "id": 1}).encode()
    first = headers_of(call(mw, scope(), init))[SESSION_HEADER.encode()]
    second = headers_of(call(mw, scope(), init))[SESSION_HEADER.encode()]
    assert len(first) == 32 and first != second and all(c in b"0123456789abcdef" for c in first)
    # A client that already has one keeps it; a failed initialize gets none; a header the app set is kept.
    assert SESSION_HEADER.encode() not in headers_of(call(mw, scope(session="mine"), init))
    failing, _ = mw_with(Inner(status=400))
    assert SESSION_HEADER.encode() not in headers_of(call(failing, scope(), init))
    own, _ = mw_with(Inner(headers=[(b"Mcp-Session-Id", b"from-app")]))
    head = call(own, scope(), init)[0]["headers"]
    assert head == [(b"Mcp-Session-Id", b"from-app")]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("abc", "abc"), ("x" * 128, "x" * 128), ("x" * 129, None), ("", None), ("caf\xe9", None),
     ("a\tb", None)],
)  # fmt: skip
def test_session_ids_are_short_printable_ascii(value: str, expected: str | None) -> None:
    assert hosted._session_id(scope(session=value)) == expected
    assert hosted._session_id({"type": "http"}) is None
    assert hosted._session_id(scope()) is None


# ------------------------------------------------------------------------------------------ rate limiter


def test_token_buckets_refill_at_the_rate_and_cap_at_the_burst() -> None:
    now = [0.0]
    rl = RateLimiter(per_session=6, overall=1000, clock=lambda: now[0])
    for _ in range(6):
        assert rl.check("s") is None
    assert rl.check("s") == ("session", pytest.approx(10.0))  # 6 a minute: one every 10 s
    now[0] = 5.0
    assert rl.check("s") == ("session", pytest.approx(5.0))
    now[0] = 10.0
    assert rl.check("s") is None
    now[0] = 10_000.0  # a long pause refills only up to the burst
    assert all(rl.check("s") is None for _ in range(6)) and rl.check("s") is not None
    assert rl.check(None) is None  # no session: only the overall bucket applies


def test_rate_limit_buckets_stay_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hosted, "_MAX_KEYS", 3)
    rl = RateLimiter(per_session=5, overall=1000, clock=lambda: 0.0)
    for s in "abcde":
        rl.check(s)
    assert len(rl._buckets) == 3 and "*" in rl._buckets and "s:e" in rl._buckets and "s:a" not in rl._buckets


def test_limit_words() -> None:
    assert hosted.rate_limited_message("session", 0.2) == (
        "Too many questions from this conversation in the last minute, so this one wasn't run. Wait about 1 "
        "seconds and ask again; nothing was lost."
    )
    assert "from everyone using the hosted server right now" in hosted.rate_limited_message("overall", 12.6)
    assert "Wait about 13 seconds" in hosted.rate_limited_message("overall", 12.6)
    assert hosted.too_large_message(150_000, 140_000) == (
        "This answer is too long to send in one reply (about 150,000 characters; the limit is 140,000), even one "
        "page at a time, so nothing was sent. Narrow your question: fewer places, a shorter span of years, one "
        "condition or one measure, or ask for a summary first."
    )


@pytest.mark.parametrize(
    ("seconds", "bucket"),
    [(0.0, "<1s"), (0.99, "<1s"), (1.0, "1-5s"), (4.99, "1-5s"), (5.0, "5-30s"), (30.0, "30-120s"),
     (119.9, "30-120s"), (120.0, ">120s")],
)  # fmt: skip
def test_latency_buckets(seconds: float, bucket: str) -> None:
    assert hosted.latency_bucket(seconds) == bucket


# ------------------------------------------------------------------------------------------ privacy filter


def record(name: str, level: int, msg: str, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, None) if exc else None
    return logging.LogRecord(name, level, "f.py", 1, msg, ("arg",), exc_info)


def test_privacy_filter_keeps_only_logger_level_and_exception_type() -> None:
    f = PrivacyFilter()
    counters = record(hosted.METRICS_LOGGER, logging.INFO, "tool calls: %s")
    assert f.filter(counters) is True and counters.msg == "tool calls: %s" and counters.args == ("arg",)
    assert f.filter(record("mcp", logging.INFO, "question %s")) is False
    assert f.filter(record("mcp", logging.DEBUG, "question %s")) is False
    warn = record("uvicorn.error", logging.WARNING, "secret %s")
    warn.exc_text, warn.stack_info = "secret trace", "secret stack"
    assert f.filter(warn) is True
    assert (
        warn.getMessage()
        == "uvicorn.error: warning - details withheld so no question or argument is ever logged"
    )
    assert (warn.args, warn.exc_info, warn.exc_text, warn.stack_info) == (None, None, None, None)
    err = record("mcp", logging.ERROR, "bad %s", ValueError("secret input"))
    assert f.filter(err) is True
    assert (
        err.getMessage()
        == "mcp: error (ValueError) - details withheld so no question or argument is ever logged"
    )
    assert "secret" not in err.getMessage()


def test_configure_logging_routes_everything_through_the_filter() -> None:
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    libs = {
        n: (list(logging.getLogger(n).handlers), logging.getLogger(n).propagate) for n in ("mcp", "uvicorn")
    }
    access = logging.getLogger("uvicorn.access").disabled
    try:
        logging.getLogger("mcp").addHandler(logging.NullHandler())
        logging.getLogger("uvicorn").propagate = False
        buf = io.StringIO()
        handler = hosted.configure_logging(buf)
        assert root.handlers == [handler] and root.level == logging.INFO
        assert any(isinstance(f, PrivacyFilter) for f in handler.filters)
        assert logging.getLogger("mcp").handlers == [] and logging.getLogger("uvicorn").propagate is True
        assert logging.getLogger("uvicorn.access").disabled is True
        logging.getLogger("mcp").warning("leak %s", "zz-secret")
        logging.getLogger(hosted.METRICS_LOGGER).info("counts %s", "{}")
        out = buf.getvalue()
        assert "zz-secret" not in out and "mcp: warning - details withheld" in out and "counts {}" in out
        assert " WARNING " in out and " INFO " in out
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved[0]:
            root.addHandler(h)
        root.setLevel(saved[1])
        for n, (hs, prop) in libs.items():
            logging.getLogger(n).handlers[:] = hs
            logging.getLogger(n).propagate = prop
        logging.getLogger("uvicorn.access").disabled = access


# ------------------------------------------------------------------------------------------ guard and counters


def test_guard_statuses_reach_the_counters(tmp_path: Path) -> None:
    metrics = Metrics()
    guard = CallGuard(cfg(tmp_path), metrics)
    assert guard("get_series", lambda: "a,b\n", "csv") == "a,b\n"

    class Refused(Exception):
        code = "no_snapshot"

    out = guard("get_rate", lambda: (_ for _ in ()).throw(Refused("Nothing loaded.")), "json")
    assert isinstance(out, dict) and out.get("refused") == "no_snapshot"
    guard("get_trend", lambda: (_ for _ in ()).throw(DeadlineExceeded()), "csv")
    assert dict(metrics.counts) == {
        ("get_series", "ok", "<1s"): 1, ("get_rate", "refused", "<1s"): 1, ("get_trend", "timeout", "<1s"): 1,
    }  # fmt: skip
    small = CallGuard(cfg(tmp_path, max_result_chars=10), metrics)
    assert small("x", lambda: "12345", "csv") == "12345"  # 10 characters on the wire: exactly the cap
    too = small("x", lambda: "123456", "json")
    assert too == {"message": hosted.too_large_message(12, 10), "refused": "too_large"}
    assert metrics.counts[("x", "too_large", "<1s")] == 1 and metrics.counts[("x", "ok", "<1s")] == 1


def test_guard_waits_its_budget_for_the_engine_then_says_busy(tmp_path: Path) -> None:
    guard = CallGuard(cfg(tmp_path, tool_timeout_s=0.2), Metrics())
    guard._lock.acquire()
    try:
        start = time.monotonic()
        assert guard("q", lambda: "x", "json") == {"message": BUSY, "refused": "busy"}
        assert time.monotonic() - start >= 0.2
    finally:
        guard._lock.release()
    assert guard("q", lambda: "x", "csv") == "x"  # the lock is free again after every call


def test_guard_interrupts_the_engine_when_a_body_overruns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted, "GRACE_S", 0.05)

    class Con:
        hits = 0

        def interrupt(self) -> None:
            Con.hits += 1
            raise RuntimeError("already closed")  # best effort: never reaches the clinician

    class Eng:
        _con = Con()

    guard = CallGuard(cfg(tmp_path, tool_timeout_s=0.1), Metrics(), Eng())
    out = guard("q", lambda: time.sleep(1.0) or "late", "csv")
    assert out == hosted.tools.refusal(DeadlineExceeded(0.1), "csv") and Con.hits == 1
    CallGuard(cfg(tmp_path), Metrics(), object())._interrupt()  # no connection: nothing to do


def test_metrics_flush_on_schedule_and_only_when_there_is_something(caplog: pytest.LogCaptureFixture) -> None:
    now = [0.0]
    m = Metrics(every_s=10, clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger=hosted.METRICS_LOGGER):
        m.record("a", "ok", 0)
        now[0] = 9.99
        m.record("a", "ok", 0)
        assert not caplog.records and m.counts[("a", "ok", "<1s")] == 2
        now[0] = 10.0
        m.record("b", "ok", 2)
        assert len(caplog.records) == 1 and not m.counts
        m.flush()  # nothing new: nothing logged
        assert len(caplog.records) == 1
        now[0] = 15.0
        m.record("c", "ok", 0)  # 5 s after the last summary: not due yet
        assert len(caplog.records) == 1
    assert json.loads(caplog.records[0].getMessage().split(": ", 1)[1]) == {"a|ok|<1s": 2, "b|ok|1-5s": 1}


def test_default_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HDR_CACHE_DIR", "~/hdr-cache")
    assert hosted.default_cache_dir() == Path("~/hdr-cache").expanduser()
    monkeypatch.delenv("HDR_CACHE_DIR")
    data = Path("/data")
    expected = (
        data / "health-data-registry"
        if data.is_dir() and hosted.os.access(data, hosted.os.W_OK)
        else Path("/tmp/health-data-registry")  # noqa: S108
    )
    assert hosted.default_cache_dir() == expected


def test_build_hosted_reads_only_the_hub_and_never_a_lake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from health_data_registry.registry import load_registry
    from tests.test_publish import write_hub_registry

    reg = load_registry(write_hub_registry(tmp_path / "reg"))
    c = cfg(tmp_path, hub_url="hf://datasets/x/y@release-2026-11-24", cache=tmp_path / "a" / "b")
    app, state = hosted.build_hosted(c, reg)
    assert (tmp_path / "a" / "b").is_dir()  # the cache directory is made, parents included
    eng = state.engine
    assert eng.lake == tmp_path / "a" / "b" / "no-local-lake" and not eng.lake.exists()
    assert eng.hub.url == c.hub_url and eng.cache == c.cache
    assert state.guard.engine is eng and state.guard.cfg is c and state.metrics._every == 3600
    assert (state.limiter.per_session, state.limiter.overall) == (30, 300)
    assert isinstance(app, HostedMiddleware) and {"get_series", "query_sql", "list_sources"} <= app.tool_names
    hosted.build_hosted(c, reg)  # an existing cache directory is fine
