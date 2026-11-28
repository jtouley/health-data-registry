"""The hosted server (serve --http): a real HTTP round trip, the result cap, rate limits, deadlines, privacy."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from health_data_registry import cli, crawl, deadline, hosted, tools
from health_data_registry.deadline import DeadlineExceeded, call_deadline
from health_data_registry.fetch import PROXY_ENV, FetchRefused
from health_data_registry.hosted import (
    CallGuard,
    HostedConfig,
    Metrics,
    PrivacyFilter,
    RateLimiter,
    build_hosted,
    configure_logging,
    uvicorn_config,
)
from health_data_registry.hub import Hub
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import RawFetch
from tests.test_publish import RUN, make_lake, write_hub_registry

SECRET = "zz-private-question-0xBEEF"  # noqa: S105 - not a password: stands in for a clinician's words


def cfg(tmp_path: Path, **kw: Any) -> HostedConfig:
    base: dict[str, Any] = {
        "hub_url": "unused",
        "cache": tmp_path / "cache",
        "tool_timeout_s": 30.0,
        "max_result_chars": 140_000,
        "rate_per_session": 30,
        "rate_global": 300,
        "metrics_every_s": 3600,
    }
    return HostedConfig(**{**base, **kw})


@pytest.fixture
def reg(tmp_path: Path) -> Registry:
    return load_registry(write_hub_registry(tmp_path / "reg"))


@pytest.fixture
def hub_engine(tmp_path: Path, reg: Registry) -> Engine:
    """Reads only a hub release (a local directory stands in for hf://datasets/...); no lake."""
    from health_data_registry.publish import export_public

    lake = make_lake(tmp_path / "maint-lake", reg, tmp_path)
    out = tmp_path / "hub-release"
    export_public(reg, lake, str(out), run_date=RUN)
    h = Hub(url=str(out), cache=tmp_path / "cache")
    return Engine(registry=reg, lake=tmp_path / "no-local-lake", hub=h, cache=tmp_path / "cache")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Running:
    def __init__(self, app: Any) -> None:
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn_config(app, "127.0.0.1", self.port))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> Running:
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    stream = io.StringIO()
    configure_logging(stream)
    root.setLevel(logging.DEBUG)  # even at DEBUG, nothing about a request gets through
    yield stream
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved[0]:
        root.addHandler(h)
    root.setLevel(saved[1])


async def _session_calls(url: str) -> tuple[set[str], str, str]:
    async with streamable_http_client(url + "/mcp") as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        out = await s.call_tool("get_series", {"condition": "legionellosis", "locations": ["Michigan"]})
        series = out.content[0].text  # type: ignore[union-attr]
        await s.call_tool("suggest_datasets", {"question": f"is {SECRET} rising in my clinic?"})
        await s.call_tool("get_series", {"condition": "legionellosis", "locations": [SECRET]})
        bad = await s.call_tool("query_sql", {"sql": f"DROP TABLE {SECRET}"})
        refused = bad.content[0].text  # type: ignore[union-attr]
    return names, series, refused


def test_http_round_trip_with_the_sdk_client_and_no_logging(
    tmp_path: Path, reg: Registry, hub_engine: Engine, log_stream: io.StringIO
) -> None:
    app, state = build_hosted(cfg(tmp_path), reg, hub_engine)
    with Running(app) as srv:
        names, series, refused = asyncio.run(_session_calls(srv.url))
        health = httpx.get(srv.url + "/healthz").json()
        state.metrics.flush()
    assert {"get_series", "query_sql", "list_sources"} <= names
    assert series.startswith("location,year,value")  # numbers from the hub release, over HTTP
    assert "Traceback" not in series and "Traceback" not in refused and refused.strip()
    assert health["status"] == "ok"
    logged = log_stream.getvalue()
    assert SECRET not in logged and "DROP TABLE" not in logged and "legionellosis" not in logged
    assert "get_series|" in logged  # the counters: tool name, status, latency bucket, and nothing else
    assert state.engine.manifests["toy_cases"].origin == "hub"  # read from the hub release, not a lake


def test_hosted_queries_run_in_a_child_and_a_poisoned_engine_shows_on_health(
    tmp_path: Path, reg: Registry, hub_engine: Engine, log_stream: io.StringIO
) -> None:
    from health_data_registry.isolation import Isolation

    hub_engine.isolation = Isolation()
    app, state = build_hosted(cfg(tmp_path), reg, hub_engine)
    with Running(app) as srv:
        _, series, _ = asyncio.run(_session_calls(srv.url))
        health = httpx.get(srv.url + "/healthz")
        state.guard._stuck.append(threading.Thread())  # stand-in for a worker that outlived its call
        state.guard._stuck[0].is_alive = lambda: True  # type: ignore[method-assign]
        sick = httpx.get(srv.url + "/healthz")
    assert series.startswith("location,year,value")  # the same numbers, from a child process
    assert health.status_code == 200 and health.json()["poisoned"] is False
    iso = health.json()["query_isolation"]
    assert iso["memory_limit_mb"] == 4096 and iso["poisoned"] is False
    assert sick.status_code == 503 and sick.json()["status"] == "poisoned" and sick.json()["poisoned"] is True
    assert sick.json()["poisoned_reasons"] == ["a tool call is still running past its deadline"]
    assert sick.json()["poisoned_since"] and sick.json()["restarts_after_s"] == 900
    assert health.json()["poisoned_since"] is None and health.json()["poisoned_reasons"] == []
    assert "engine poisoned: a tool call is still running past its deadline" in log_stream.getvalue()


def test_initialize_gets_an_opaque_session_id_and_rate_limits_answer_429(
    tmp_path: Path, reg: Registry, hub_engine: Engine, log_stream: io.StringIO
) -> None:
    app, _ = build_hosted(cfg(tmp_path, rate_per_session=1, rate_global=100), reg, hub_engine)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    call = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "list_caveats", "arguments": {}},
    }
    with Running(app) as srv, httpx.Client(base_url=srv.url, headers=headers) as c:
        another = c.post("/mcp", json=init).headers["mcp-session-id"]
        r = c.post("/mcp", json=init)
        sid = r.headers["mcp-session-id"]
        assert r.status_code == 200 and len(sid) == 65
        h = {"mcp-session-id": sid, "mcp-protocol-version": "2025-06-18"}
        assert c.post("/mcp", json=call, headers=h).status_code == 200
        limited = c.post("/mcp", json={**call, "id": 8}, headers=h)
        other = c.post("/mcp", json=call, headers={**h, "mcp-session-id": another})
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    body = limited.json()
    assert body["id"] == 8 and "Too many questions from this conversation" in body["error"]["message"]
    assert other.status_code == 200  # the limit is per session, not per address
    assert another not in log_stream.getvalue() and sid not in log_stream.getvalue()


def test_rate_limiter_per_session_and_overall() -> None:
    now = [0.0]
    rl = RateLimiter(per_session=2, overall=3, clock=lambda: now[0])
    assert rl.check("a") is None and rl.check("a") is None
    which, wait = rl.check("a") or ("", 0.0)
    assert which == "session" and 0 < wait <= 30
    assert rl.check("b") is None
    assert (rl.check("c") or ("", 0))[0] == "overall"  # all of claude.ai shares one address; this is the cap
    assert (rl.check(None) or ("", 0))[0] == "overall"
    now[0] = 60.0
    assert rl.check("a") is None  # refilled after a minute
    assert "everyone using the hosted server" in hosted.rate_limited_message("overall", 12.2)


def test_result_cap_replaces_a_long_answer_with_a_plain_message(tmp_path: Path) -> None:
    guard = CallGuard(cfg(tmp_path, max_result_chars=1000), Metrics())
    out = guard("get_series", lambda: "x" * 600, "csv")
    assert isinstance(out, str) and "too long to send" in out and "fewer columns" in out
    j = guard("get_series", lambda: {"rows": ["x" * 600]}, "json")
    assert isinstance(j, dict) and j["refused"] == "too_large"
    assert guard("get_series", lambda: "short", "csv") == "short"
    assert ("get_series", "too_large", "<1s") in guard.metrics.counts


def test_guard_deadline_and_failures_are_plain_words(tmp_path: Path) -> None:
    guard = CallGuard(cfg(tmp_path, tool_timeout_s=0.3), Metrics())

    def boom() -> str:
        raise RuntimeError("internal 0x7f")

    failed = guard("get_series", boom, "csv")
    assert isinstance(failed, str) and "0x7f" not in failed and "Something's wrong" in failed
    monkey_grace = hosted.GRACE_S
    hosted.GRACE_S = 0.1
    try:
        slow = guard("get_series", lambda: time.sleep(1) or "late", "csv")  # type: ignore[func-returns-value]
    finally:
        hosted.GRACE_S = monkey_grace
    assert isinstance(slow, str) and "more time than the hosted server allows" in slow
    assert ("get_series", "timeout", "<1s") in guard.metrics.counts
    time.sleep(1)  # let the slow body finish and release the engine


def test_busy_engine_answers_plainly(tmp_path: Path) -> None:
    guard = CallGuard(cfg(tmp_path, tool_timeout_s=0.2), Metrics())
    guard._lock.acquire()
    try:
        out = guard("list_sources", lambda: "never", "json")
    finally:
        guard._lock.release()
    assert isinstance(out, dict) and out["refused"] == "busy" and "busy" in out["message"]


def test_live_retries_stop_at_the_call_deadline(reg: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live WHO reads retry up to RETRIES x FETCH_TIMEOUT_S; inside a call deadline they stop at the budget."""
    asked: list[float] = []

    def slow_fetch(url: str, *, allowed_hosts: Any, headers: Any, timeout: float) -> Any:
        asked.append(timeout)
        raise FetchRefused("timeout", "The publisher took too long to answer, so the fetch was stopped.")

    monkeypatch.setattr("health_data_registry.fetch.fetch", slow_fetch)
    get = crawl.fetcher(reg, sleep=lambda s: None, robots=False)
    with call_deadline(3.0), pytest.raises(DeadlineExceeded) as e:
        get("https://data.cdc.gov/resource/x.json")
    assert len(asked) == 2 and all(t <= 3.0 for t in asked)  # never the full 180 s per attempt
    assert "more time than the hosted server allows" in str(e.value)
    with pytest.raises(FetchRefused):  # outside a deadline the maintainers' retries are unchanged
        get("https://data.cdc.gov/resource/x.json")
    assert len(asked) == 2 + crawl.RETRIES
    with call_deadline(0.0), pytest.raises(DeadlineExceeded):
        deadline.cap(1.0)
    assert deadline.remaining() is None


def test_live_deadline_reaches_the_clinician_as_plain_words(tmp_path: Path) -> None:
    reg = load_registry(write_hub_registry(tmp_path / "reg"))
    restricted = next((ds for ds in reg if not ds.entry.licence.redistribution), None)
    if restricted is None:
        pytest.skip("the synthetic registry has no restricted source")

    def get(url: str) -> RawFetch:
        raise DeadlineExceeded(200)

    eng = Engine(registry=reg, lake=tmp_path / "none", hub=None, fetch_at_run_time=True, getter=get)
    with pytest.raises(DeadlineExceeded):
        eng.manifest(restricted.id)
    assert "more time" in str(tools.refusal(DeadlineExceeded(200)))


def test_privacy_filter_withholds_library_text() -> None:
    rec = logging.LogRecord("mcp.server", logging.WARNING, __file__, 1, "bad input %s", (SECRET,), None)
    assert PrivacyFilter().filter(rec) and SECRET not in rec.getMessage()
    debug = logging.LogRecord("uvicorn.error", logging.DEBUG, __file__, 1, SECRET, None, None)
    assert not PrivacyFilter().filter(debug)
    ours = logging.LogRecord(hosted.METRICS_LOGGER, logging.INFO, __file__, 1, "counts %s", ("{}",), None)
    assert PrivacyFilter().filter(ours) and ours.getMessage() == "counts {}"


def test_cli_serve_http_reaches_the_hosted_server(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(hosted, "main", lambda host, port: seen.update(host=host, port=port))
    assert cli.main(["serve", "--http", "--port", "9999"]) == 0
    assert seen == {"host": "127.0.0.1", "port": 9999}  # local by default; the Space passes --host 0.0.0.0
    assert cli.main(["serve", "--http", "--host", "0.0.0.0"]) == 0  # noqa: S104
    assert seen == {"host": "0.0.0.0", "port": 7860}  # noqa: S104


def test_cache_dir_and_config_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HDR_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("HDR_TOOL_TIMEOUT_S", "120")
    monkeypatch.delenv("HDR_HUB", raising=False)
    c = HostedConfig()
    assert c.cache == tmp_path / "c" and c.tool_timeout_s == 120 and c.hub_url == hosted.HOSTED_HUB
    monkeypatch.delenv("HDR_CACHE_DIR")
    assert str(hosted.default_cache_dir()).startswith(("/data", "/tmp"))  # noqa: S108
    assert hosted.latency_bucket(0.5) == "<1s" and hosted.latency_bucket(500) == ">120s"


def test_metrics_summary_holds_only_counters(caplog: pytest.LogCaptureFixture) -> None:
    now = [0.0]
    m = Metrics(every_s=10, clock=lambda: now[0])
    m.record("get_series", "ok", 0.2)
    now[0] = 11
    with caplog.at_level(logging.INFO, logger=hosted.METRICS_LOGGER):
        m.record("get_rate", "refused", 3)
    line = caplog.records[-1].getMessage()
    assert json.loads(line.split(": ", 1)[1]) == {"get_rate|refused|1-5s": 1, "get_series|ok|<1s": 1}
    assert not m.counts


@pytest.mark.parametrize("raw", ["0", "-5", "inf", "nan", "fast", "-inf"])
def test_a_limit_that_would_switch_itself_off_stops_the_server(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sharp-edges: 0, negative, inf, nan or junk in a limit never reads as "no limit"; the server won't start."""
    monkeypatch.setenv("HDR_RATE_GLOBAL", raw)
    with pytest.raises(
        ValueError, match=r"^HDR_RATE_GLOBAL must be a positive number; got .*Unset it to use 300\."
    ):
        HostedConfig()
    monkeypatch.setenv("HDR_RATE_GLOBAL", "0.5")
    assert HostedConfig().rate_global == 0.5


def test_pages_must_fit_under_the_result_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HDR_PAGE_CHARS", raising=False)
    hosted.check_limits(cfg(tmp_path))  # the defaults: 120,000 per page under a 140,000 cap
    hosted.check_limits(cfg(tmp_path, max_result_chars=120_000))  # exactly the cap still fits
    with pytest.raises(
        ValueError, match=r"HDR_PAGE_CHARS \(120,000\) is above HDR_MAX_RESULT_CHARS \(119,999\)"
    ):
        hosted.check_limits(cfg(tmp_path, max_result_chars=119_999))
    monkeypatch.setenv("HDR_PAGE_CHARS", "1999")
    with pytest.raises(ValueError, match=r"at least 2,000; got '1999'"):
        hosted.check_limits(cfg(tmp_path))
    monkeypatch.setenv("HDR_PAGE_CHARS", "2000")
    hosted.check_limits(cfg(tmp_path))


def test_main_checks_the_limits_before_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HDR_PAGE_CHARS", "200000")
    monkeypatch.setattr(hosted, "configure_logging", lambda: None)  # leave other tests' logging alone
    monkeypatch.setattr(
        hosted, "build_hosted", lambda *a, **k: pytest.fail("built a server past a bad limit")
    )
    with pytest.raises(ValueError, match="above HDR_MAX_RESULT_CHARS"):
        hosted.main()


def test_uvicorn_speaks_asgi3_without_guessing() -> None:
    app = object()
    c = uvicorn_config(app, "127.0.0.1", 1)
    assert (c.interface, c.factory, c.access_log, c.log_config, c.server_header, c.proxy_headers) == (
        "asgi3", True, False, None, False, False
    )  # fmt: skip
    assert c.app() is app
    assert (c.host, c.port) == ("127.0.0.1", 1)


def test_deadline_words_and_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    tail = (
        ", so it was stopped and no numbers are shown. The publisher or the data hub may be slow right now. Try "
        "again in a few minutes, or narrow the question: one condition, fewer places or fewer years."
    )
    head = "This question needed more time than the hosted server allows for one answer"
    assert str(DeadlineExceeded(200)) == head + " (200 seconds)" + tail
    assert str(DeadlineExceeded()) == head + tail
    assert DeadlineExceeded.code == "too_slow" and isinstance(DeadlineExceeded(), TimeoutError)
    now = [100.0]
    monkeypatch.setattr(deadline.time, "monotonic", lambda: now[0])
    with call_deadline(10.0):
        assert deadline.cap(30.0) == 10.0 and deadline.cap(4.0) == 4.0
        now[0] = 109.5  # half a second left: still worth one short attempt
        assert deadline.cap(30.0) == 0.5
        now[0] = 110.0  # exactly nothing left
        with pytest.raises(DeadlineExceeded):
            deadline.cap(30.0)
    assert deadline.cap(30.0) == 30.0


# --- #270: fail closed under an egress proxy -------------------------------------------------------------------


def test_no_proxy_starts_without_the_opt_in(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=hosted.METRICS_LOGGER):
        hosted.check_egress({})
        hosted.check_egress({PROXY_ENV: "   "})  # blank reads as unset, as fetch.py reads it
    assert not caplog.records
    with caplog.at_level(logging.WARNING, logger=hosted.METRICS_LOGGER):
        hosted.check_egress({hosted.PROXY_OPT_IN_ENV: "1"})  # the opt-in alone starts, but says it is stale
    assert any("the opt-in has nothing to vouch for" in r.getMessage() for r in caplog.records)


def test_proxy_without_the_opt_in_refuses_to_start_in_plain_words() -> None:
    """sharp-edges: under a proxy the private-address check is advisory, so the hosted server fails closed."""
    with pytest.raises(hosted.StartRefused) as e:
        hosted.check_egress({PROXY_ENV: "http://proxy.internal:3128"})
    message = str(e.value)
    assert message.startswith("The hosted server did not start: HDR_HTTPS_PROXY is set")
    assert "private or loopback address" in message
    assert "Next step: unset HDR_HTTPS_PROXY" in message
    assert "HDR_PROXY_ENFORCES_ALLOWLIST=1" in message
    assert "proxy.internal" not in message  # the refusal never echoes the proxy's address


@pytest.mark.parametrize("opt_in", ["0", "false", "no", "true", "yes", " 1", "1 ", "01"])
def test_an_opt_in_other_than_exactly_1_never_reads_as_yes(opt_in: str) -> None:
    """sharp-edges: "0", "false" or a near miss must not switch the check off."""
    with pytest.raises(
        hosted.StartRefused,
        match=r"which isn't a yes \(only '1' is\)\..* Next step: unset HDR_HTTPS_PROXY, or",
    ):
        hosted.check_egress({PROXY_ENV: "http://proxy.internal:3128", hosted.PROXY_OPT_IN_ENV: opt_in})


def test_proxy_with_the_opt_in_starts_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=hosted.METRICS_LOGGER):
        hosted.check_egress({PROXY_ENV: "http://proxy.internal:3128", hosted.PROXY_OPT_IN_ENV: "1"})
    lines = [r.getMessage() for r in caplog.records]
    assert any(
        "HDR_PROXY_ENFORCES_ALLOWLIST=1 declares that the proxy at proxy.internal:3128" in line
        and "HDR_EXTRA_CA_FILE (if set) is trusted for every publisher" in line
        for line in lines
    )


def test_the_opt_in_warning_survives_the_privacy_filter(log_stream: io.StringIO) -> None:
    """The warning is the one content line at start-up; the real handler must let it through whole."""
    hosted.check_egress({PROXY_ENV: "http://proxy.internal:3128", hosted.PROXY_OPT_IN_ENV: "1"})
    out = log_stream.getvalue()
    assert "HDR_PROXY_ENFORCES_ALLOWLIST=1 declares that the proxy at proxy.internal:3128" in out
    assert "details withheld" not in out


@pytest.mark.parametrize(
    "proxy",
    ["socks5://proxy.internal:1080", "http://user:pw@proxy.internal:3128", "http://:3128", "http://[x"],
)
def test_a_bad_proxy_address_with_the_opt_in_stops_the_server_at_start(proxy: str) -> None:
    """sharp-edges: a proxy fetch.py would refuse is refused at start-up, not at a clinician's first question."""
    with pytest.raises(hosted.StartRefused) as e:
        hosted.check_egress({PROXY_ENV: proxy, hosted.PROXY_OPT_IN_ENV: "1"})
    message = str(e.value)
    assert message.startswith("The hosted server did not start: HDR_HTTPS_PROXY ")
    assert "Next step: set HDR_HTTPS_PROXY to http://host:port" in message
    assert "proxy.internal" not in message and "pw" not in message  # never echoes the address or credentials


def test_build_hosted_checks_the_egress_when_it_builds_the_live_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sharp-edges: an entry point that calls build_hosted directly still gets the egress check."""
    monkeypatch.setenv(PROXY_ENV, "http://proxy.internal:3128")
    monkeypatch.delenv(hosted.PROXY_OPT_IN_ENV, raising=False)
    with pytest.raises(hosted.StartRefused, match="HDR_HTTPS_PROXY is set"):
        hosted.build_hosted()


def test_main_refuses_a_proxy_before_building_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROXY_ENV, "http://proxy.internal:3128")
    monkeypatch.delenv(hosted.PROXY_OPT_IN_ENV, raising=False)
    monkeypatch.setattr(hosted, "configure_logging", lambda: None)  # leave other tests' logging alone
    monkeypatch.setattr(hosted, "build_hosted", lambda *a, **k: pytest.fail("built a server behind a proxy"))
    with pytest.raises(hosted.StartRefused):
        hosted.main()


def test_main_starts_behind_a_proxy_with_the_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROXY_ENV, "http://proxy.internal:3128")
    monkeypatch.setenv(hosted.PROXY_OPT_IN_ENV, "1")
    monkeypatch.setattr(hosted, "configure_logging", lambda: None)

    class Built(Exception):
        pass

    def build(*a: Any, **k: Any) -> Any:
        raise Built  # got past the egress check to building the server; stop before serving

    monkeypatch.setattr(hosted, "build_hosted", build)
    with pytest.raises(Built):
        hosted.main()


def test_cli_serve_http_prints_the_refusal_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(PROXY_ENV, "http://proxy.internal:3128")
    monkeypatch.delenv(hosted.PROXY_OPT_IN_ENV, raising=False)
    monkeypatch.setattr(hosted, "configure_logging", lambda: None)
    monkeypatch.setattr(hosted, "build_hosted", lambda *a, **k: pytest.fail("built a server behind a proxy"))
    assert cli.main(["serve", "--http"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("The hosted server did not start:") and "Traceback" not in err


def test_local_serve_is_unchanged_behind_a_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stdio server and the CLI fetches keep fetch.py's proxy mode (with its warning); only --http refuses."""
    from health_data_registry import server

    monkeypatch.setenv(PROXY_ENV, "http://proxy.internal:3128")
    monkeypatch.delenv(hosted.PROXY_OPT_IN_ENV, raising=False)
    called: list[bool] = []
    monkeypatch.setattr(server, "main", lambda: called.append(True))
    assert cli.main(["serve"]) == 0 and called == [True]


def test_cli_serve_http_prints_a_bad_limit_plainly_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hard rule 12: every startup refusal of serve --http is plain words, never a traceback."""
    monkeypatch.delenv(PROXY_ENV, raising=False)
    monkeypatch.delenv(hosted.PROXY_OPT_IN_ENV, raising=False)
    monkeypatch.setenv("HDR_PAGE_CHARS", "999999")
    monkeypatch.setattr(hosted, "configure_logging", lambda: None)
    monkeypatch.setattr(
        hosted, "build_hosted", lambda *a, **k: pytest.fail("built a server past a bad limit")
    )
    assert cli.main(["serve", "--http"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("The hosted server did not start: HDR_PAGE_CHARS (999,999) is above")
    assert "Traceback" not in err
