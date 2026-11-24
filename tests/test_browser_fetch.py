"""The browser fetcher, with Playwright replaced by a fake: the same rules as fetch.py, and a block is final."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from health_data_registry import browser_fetch as bf
from health_data_registry.fetch import USER_AGENT, FetchRefused

HOSTS = frozenset({"dash.example.gov"})
VIEW = "https://dash.example.gov/views/Toy/TB?:embed=y"


@dataclass
class Req:
    url: str
    resource_type: str = "document"
    navigation: bool = True
    parent: Any = None

    def is_navigation_request(self) -> bool:
        return self.navigation

    @property
    def frame(self) -> Any:
        return type("F", (), {"parent_frame": self.parent})()


@dataclass
class Resp:
    url: str
    status: int = 200
    content: bytes = b"<html>ok</html>"
    request: Req = field(default_factory=lambda: Req(VIEW))
    headers: dict[str, str] = field(default_factory=lambda: {"content-type": "text/html"})
    gone: bool = False

    def body(self) -> bytes:
        if self.gone:
            raise RuntimeError("Response.body: No resource with given identifier found")
        return self.content


class Route:
    def __init__(self, request: Req, log: list[tuple[str, str]]) -> None:
        self.request, self.log = request, log

    def abort(self) -> None:
        self.log.append(("abort", self.request.url))

    def continue_(self) -> None:
        self.log.append(("continue", self.request.url))


@dataclass
class Script:
    """What the fake page does on goto: requests it makes (routed) and responses it sees."""

    requests: list[Req] = field(default_factory=list)
    responses: list[Resp] = field(default_factory=list)
    goto_error: Exception | None = None
    returns_none: bool = False
    late: list[Resp] = field(default_factory=list)  # responses that arrive after goto returns


class FakePage:
    def __init__(self, script: Script, record: dict[str, Any]) -> None:
        self.script, self.record = script, record
        self.handler: Callable[[Any], None] = lambda r: None
        self.router: Callable[[Any], None] = lambda r: None

    def route(self, pattern: str, handler: Callable[[Any], None]) -> None:
        self.record["route_pattern"] = pattern
        self.router = handler

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        assert event == "response"
        self.handler = handler

    def goto(self, url: str, *, wait_until: str, timeout: float) -> Any:
        self.record["goto"] = (url, wait_until, timeout)
        for req in self.script.requests:
            self.router(Route(req, self.record.setdefault("routes", [])))
        if self.script.goto_error is not None:
            raise self.script.goto_error
        for r in self.script.responses:
            self.handler(r)
        return None if self.script.returns_none else (self.script.responses or [None])[-1]

    def wait_for_timeout(self, ms: float) -> None:
        self.record["waits"] = self.record.get("waits", 0) + 1
        while self.script.late:
            self.handler(self.script.late.pop(0))


def fake_playwright(script: Script, record: dict[str, Any]) -> Callable[[], Any]:
    class Context:
        def new_page(self) -> FakePage:
            return FakePage(script, record)

    class Browser:
        def new_context(self, **kw: Any) -> Context:
            record["context"] = kw
            return Context()

        def close(self) -> None:
            record["closed"] = True

    class Chromium:
        def launch(self, **kw: Any) -> Browser:
            record["launch"] = kw
            return Browser()

    @contextmanager
    def factory() -> Any:
        yield type("PW", (), {"chromium": Chromium()})()

    return factory


def run(script: Script, record: dict[str, Any] | None = None, **kw: Any) -> bf.BrowserResult:
    rec = {} if record is None else record
    return bf.browser_fetch(VIEW, allowed_hosts=HOSTS, playwright=fake_playwright(script, rec), **kw)


DATA = Resp(
    "https://dash.example.gov/vizql/w/Toy/v/TB/bootstrapSession/sessions/ABC",
    content=b"2;{}",
    request=Req("https://dash.example.gov/vizql/w/Toy/v/TB/bootstrapSession/sessions/ABC", "xhr", False),
    headers={"content-type": "text/plain"},
)


def test_page_and_captured_data_come_back_hashed() -> None:
    rec: dict[str, Any] = {}
    got = run(Script(responses=[DATA, Resp(VIEW)]), rec, capture=["/bootstrapSession/"])
    assert got.page.content == b"<html>ok</html>" and got.page.status == 200 and got.page.url == VIEW
    (data,) = got.captured
    assert data.content == b"2;{}" and data.sha256 == hashlib.sha256(b"2;{}").hexdigest()
    assert data.retrieved_at.tzinfo is not None
    assert rec["context"]["user_agent"] == USER_AGENT  # the honest user agent, nothing else
    assert rec["context"]["accept_downloads"] is False and rec["closed"] is True
    assert rec["route_pattern"] == "**/*" and rec["goto"][1] == "networkidle"


def test_uncaptured_responses_are_not_kept() -> None:
    other = Resp(
        "https://dash.example.gov/app.js", request=Req("https://dash.example.gov/app.js", "script", False)
    )
    assert run(Script(responses=[other, DATA, Resp(VIEW)])).captured == ()


def test_requests_to_other_hosts_images_and_workbooks_are_aborted() -> None:
    rec: dict[str, Any] = {}
    reqs = [
        Req("https://dash.example.gov/app.js", "script", False),
        Req("https://cdn.elsewhere.com/x.js", "script", False),
        Req("http://dash.example.gov/plain", "xhr", False),
        Req("https://dash.example.gov/logo.png", "image", False),
        Req("https://dash.example.gov/workbooks/Toy.twbx", "xhr", False),
        Req("https://dash.example.gov/workbooks/Toy.TWB", "xhr", False),
    ]
    run(Script(requests=reqs, responses=[Resp(VIEW)]), rec)
    assert rec["routes"] == [
        ("continue", "https://dash.example.gov/app.js"),
        ("abort", "https://cdn.elsewhere.com/x.js"),
        ("abort", "http://dash.example.gov/plain"),
        ("abort", "https://dash.example.gov/logo.png"),
        ("abort", "https://dash.example.gov/workbooks/Toy.twbx"),
        ("abort", "https://dash.example.gov/workbooks/Toy.TWB"),
    ]


@pytest.mark.parametrize(
    "url", ["http://dash.example.gov/x", "https://elsewhere.gov/x", "https://dash.example.gov:8443/"]
)
def test_the_page_itself_must_be_https_on_a_catalogued_host(url: str) -> None:
    rec: dict[str, Any] = {}
    with pytest.raises(FetchRefused):
        bf.browser_fetch(url, allowed_hosts=HOSTS, playwright=fake_playwright(Script(), rec))
    assert "launch" not in rec  # refused before a browser was started


@pytest.mark.parametrize("status", [401, 403, 407, 429])
def test_a_blocking_status_is_final(status: int) -> None:
    with pytest.raises(FetchRefused) as e:
        run(Script(responses=[Resp(VIEW, status=status)]))
    assert e.value.code == "blocked" and e.value.status == status
    assert "not worked around" in e.value.message


@pytest.mark.parametrize(
    "page",
    [
        b"<html><head><title>Access Denied</title></head><body>Reference&#32;&#35;18.abc</body></html>",
        b"<html><head><title>Just a moment...</title></head></html>",
        b"<script src='https://challenges.cloudflare.com/x.js'></script>",
        b"<html><head><title>Attention Required! | Cloudflare</title></head></html>",
    ],
)
def test_an_edge_challenge_page_is_a_block_even_with_status_200(page: bytes) -> None:
    with pytest.raises(FetchRefused) as e:
        run(Script(responses=[Resp(VIEW, content=page)]))
    assert e.value.code == "blocked"


def test_a_block_before_a_redirect_still_counts() -> None:
    first = Resp(VIEW, status=403, content=b"denied")
    with pytest.raises(FetchRefused) as e:
        run(Script(responses=[first, Resp("https://dash.example.gov/ok")]))
    assert e.value.code == "blocked"


def test_another_error_status_is_an_http_error() -> None:
    with pytest.raises(FetchRefused) as e:
        run(Script(responses=[Resp(VIEW, status=502)]))
    assert e.value.code == "http_error" and e.value.status == 502


def test_no_answer_at_all() -> None:
    with pytest.raises(FetchRefused) as e:
        run(Script(returns_none=True))
    assert e.value.code == "connect_failed"


def test_only_the_main_frame_is_the_page() -> None:
    framed = Resp(
        "https://dash.example.gov/frame", request=Req("https://dash.example.gov/frame", parent=object())
    )
    with pytest.raises(FetchRefused) as e:
        run(Script(responses=[framed]))  # an embedded frame's document is not the page
    assert e.value.code == "connect_failed"


class TimeoutError_(Exception):  # Playwright's TimeoutError is matched by name
    pass


TimeoutError_.__name__ = "TimeoutError"


@pytest.mark.parametrize(
    ("error", "code"), [(TimeoutError_("slow"), "timeout"), (RuntimeError("net::ERR"), "connect_failed")]
)
def test_navigation_errors(error: Exception, code: str) -> None:
    rec: dict[str, Any] = {}
    with pytest.raises(FetchRefused) as e:
        run(Script(goto_error=error), rec)
    assert e.value.code == code and rec["closed"] is True


def test_a_body_that_is_gone_is_kept_empty_not_fatal() -> None:
    got = run(Script(responses=[Resp(VIEW), Resp(DATA.url, content=b"x", request=DATA.request, gone=True)]),
              capture=["/bootstrapSession/"])  # fmt: skip
    assert got.captured[0].content == b""


def test_data_that_arrives_after_the_page_is_waited_for() -> None:
    rec: dict[str, Any] = {}
    got = run(Script(responses=[Resp(VIEW)], late=[DATA]), rec, capture=["/bootstrapSession/"])
    assert len(got.captured) == 1 and rec["waits"] >= 1


def test_no_wait_when_nothing_is_captured() -> None:
    rec: dict[str, Any] = {}
    run(Script(responses=[Resp(VIEW)]), rec)
    assert "waits" not in rec


@pytest.mark.parametrize("which", ["page", "data"])
def test_oversized_bodies_are_refused(which: str) -> None:
    big = b"x" * 50
    responses = [
        Resp(VIEW, content=big if which == "page" else b"ok"),
        Resp(DATA.url, content=big, request=DATA.request),
    ]
    with pytest.raises(FetchRefused) as e:
        run(Script(responses=responses), capture=["/bootstrapSession/"], max_bytes=10)
    assert e.value.code == "too_large"


@pytest.mark.parametrize("kw", [{"max_bytes": 0}, {"timeout": 0}])
def test_non_positive_limits(kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        run(Script(), **kw)


def test_the_operators_egress_proxy_is_used_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HDR_HTTPS_PROXY", "http://127.0.0.1:3128")
    rec: dict[str, Any] = {}
    run(Script(responses=[Resp(VIEW)]), rec)
    assert rec["launch"] == {"proxy": {"server": "http://127.0.0.1:3128"}}
    monkeypatch.delenv("HDR_HTTPS_PROXY")
    rec2: dict[str, Any] = {}
    run(Script(responses=[Resp(VIEW)]), rec2)
    assert rec2["launch"] == {}


@pytest.mark.parametrize(
    ("status", "body", "blocked"),
    [
        (200, b"<html>fine</html>", False),
        (403, b"", True),
        (200, b"x" * 300_000 + b"<title>Access Denied</title>", False),
    ],
)
def test_is_blocked(status: int, body: bytes, blocked: bool) -> None:
    assert bf.is_blocked(status, body) is blocked
