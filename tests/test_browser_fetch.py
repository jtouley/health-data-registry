"""The browser fetcher, with Playwright replaced by a fake: the same rules as fetch.py, and a block is final."""

from __future__ import annotations

import hashlib
import time
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
    sockets: int = 0  # WebSockets the page opens
    popups: int = 0  # popup windows the page opens


class FakeSocket:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record

    def close(self) -> None:
        self.record["sockets_closed"] = self.record.get("sockets_closed", 0) + 1


class FakePage:
    def __init__(self, script: Script, record: dict[str, Any], context: Any) -> None:
        self.script, self.record, self.context = script, record, context
        self.handler: Callable[[Any], None] = lambda r: None

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        assert event == "response"
        self.handler = handler

    def close(self) -> None:
        self.record["popups_closed"] = self.record.get("popups_closed", 0) + 1

    def goto(self, url: str, *, wait_until: str, timeout: float) -> Any:
        self.record["goto"] = (url, wait_until, timeout)
        for req in self.script.requests:
            self.context.router(Route(req, self.record.setdefault("routes", [])))
        for _ in range(self.script.sockets):
            self.context.socket_router(FakeSocket(self.record))
        for _ in range(self.script.popups):
            self.context.page_handler(FakePage(self.script, self.record, self.context))
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
        def __init__(self) -> None:
            self.router: Callable[[Any], None] = lambda r: None
            self.socket_router: Callable[[Any], None] = lambda ws: None
            self.page_handler: Callable[[Any], None] = lambda p: None

        def route(self, pattern: str, handler: Callable[[Any], None]) -> None:
            record["route_pattern"] = pattern
            self.router = handler

        def route_web_socket(self, pattern: str, handler: Callable[[Any], None]) -> None:
            record["socket_pattern"] = pattern
            self.socket_router = handler

        def on(self, event: str, handler: Callable[[Any], None]) -> None:
            assert event == "page"
            self.page_handler = handler

        def new_page(self) -> FakePage:
            page = FakePage(script, record, self)
            self.page_handler(page)  # Playwright announces every new page, the first one too
            return page

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
    kw.setdefault("capture_wait", 0.3)  # the fake's waits are instant: never spin for the real 30 s
    kw.setdefault("resolve", resolver())
    return bf.browser_fetch(VIEW, allowed_hosts=HOSTS, playwright=fake_playwright(script, rec), **kw)


PUBLIC = "93.184.216.34"


def resolver(
    answers: dict[str, list[str]] | None = None, seen: list[str] | None = None
) -> Callable[[str], Any]:
    """fetch.public_addresses with a fake DNS: a private answer is refused exactly as fetch.py refuses it."""
    import ipaddress

    def resolve(host: str) -> list[str]:
        if seen is not None:
            seen.append(host)
        got = (answers or {}).get(host, [PUBLIC])
        if not all(bf._fetch.is_public_address(ipaddress.ip_address(a)) for a in got):
            raise FetchRefused("blocked_address", f"{host} resolves to a private or reserved address")
        return got

    return resolve


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


@pytest.mark.parametrize(("timeout", "capture_wait"), [(0.3, 60.0), (60.0, 0.3)])
def test_the_wait_for_data_ends_at_the_shorter_of_the_two_limits(timeout: float, capture_wait: float) -> None:
    rec: dict[str, Any] = {}
    start = time.monotonic()
    got = run(Script(responses=[Resp(VIEW)]), rec, capture=["/bootstrapSession/"], timeout=timeout,
              capture_wait=capture_wait)  # fmt: skip
    assert got.captured == () and rec["waits"] >= 1 and time.monotonic() - start < 10


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
    # without a proxy, Chromium connects to the page's host only at the address checked here
    assert rec2["launch"] == {"args": [f"--host-resolver-rules=MAP dash.example.gov {PUBLIC}"]}


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


def test_the_challenge_scan_reads_exactly_the_first_200000_bytes() -> None:
    title = b"<title>Access Denied</title>"
    assert bf.is_blocked(200, b"x" * (200_000 - len(title)) + title) is True
    assert bf.is_blocked(200, b"x" * (200_001 - len(title)) + title) is False


def test_the_browser_context_and_navigation_are_exactly_as_documented() -> None:
    rec: dict[str, Any] = {}
    run(Script(responses=[Resp(VIEW)]), rec, timeout=12.5)
    assert rec["context"] == {
        "user_agent": USER_AGENT, "accept_downloads": False, "service_workers": "block", "locale": "en-US"
    }  # fmt: skip
    assert rec["goto"] == (VIEW, "networkidle", 12_500)


def test_each_kept_answer_carries_its_own_address_and_type() -> None:
    got = run(
        Script(responses=[DATA, Resp(VIEW + "&x=1", request=Req(VIEW))]), capture=["/bootstrapSession/"]
    )
    assert got.page.url == VIEW and got.page.final_url == VIEW + "&x=1"
    assert got.page.content_type == "text/html"
    (data,) = got.captured
    assert data.url == data.final_url == DATA.url and data.content_type == "text/plain"


def test_a_body_of_exactly_the_limit_is_kept_and_a_limit_of_one_is_allowed() -> None:
    got = run(Script(responses=[Resp(VIEW, content=b"0123456789")]), max_bytes=10)
    assert got.page.content == b"0123456789"
    assert run(Script(responses=[Resp(VIEW, content=b"x")]), max_bytes=1).page.content == b"x"
    with pytest.raises(ValueError, match="max_bytes and timeout must be positive"):
        run(Script(), max_bytes=0)


def test_refusals_name_the_host_in_plain_words() -> None:
    with pytest.raises(FetchRefused, match="dash.example.gov took too long"):
        run(Script(goto_error=TimeoutError_("slow")))
    with pytest.raises(FetchRefused, match="could not load the page from dash.example.gov"):
        run(Script(goto_error=RuntimeError("net::ERR")))
    with pytest.raises(FetchRefused, match="A response from dash.example.gov is larger"):
        run(Script(responses=[Resp(VIEW, content=b"x" * 20)]), max_bytes=10)
    with pytest.raises(FetchRefused, match="no answer from dash.example.gov"):
        run(Script(returns_none=True))
    with pytest.raises(FetchRefused, match="answered with status 502"):
        run(Script(responses=[Resp(VIEW, status=502)]))
    with pytest.raises(FetchRefused, match="dash.example.gov refused .* not worked around"):
        run(Script(responses=[Resp(VIEW, status=403)]))


def test_a_blocking_status_on_the_final_page_is_a_block_whatever_its_body() -> None:
    with pytest.raises(FetchRefused) as e:
        run(Script(responses=[Resp(VIEW, status=429, content=b"<html>ok</html>")]))
    assert e.value.code == "blocked" and e.value.status == 429


def test_what_was_fetched_and_what_was_not_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="health_data_registry.browser_fetch")
    reqs = [
        Req("https://cdn.elsewhere.com/x.js", "script", False),
        Req("https://dash.example.gov/a.png", "image"),
    ]
    gone = Resp(DATA.url, request=DATA.request, gone=True)
    run(Script(requests=reqs, responses=[gone, Resp(VIEW)]), capture=["/bootstrapSession/"])
    msgs = [r.getMessage() for r in caplog.records]
    assert "browser fetch: a response from dash.example.gov had no readable body" in msgs
    assert "browser fetch: 1 requests to other hosts were not made (host=dash.example.gov)" in msgs
    assert "browser fetched: host=dash.example.gov bytes=15 captured=1" in msgs


def test_the_wait_stops_as_soon_as_the_data_arrive() -> None:
    rec: dict[str, Any] = {}
    run(Script(responses=[Resp(VIEW)], late=[DATA]), rec, capture=["/bootstrapSession/"], capture_wait=60.0)
    assert rec["waits"] == 1


# ----------------------------------------------------------- addresses, redirects, sockets, popups (PR 19 review)


def test_the_page_host_must_resolve_to_a_public_address_before_a_browser_starts() -> None:
    rec: dict[str, Any] = {}
    for bad in (["169.254.169.254"], ["127.0.0.1"], ["10.0.0.5"], [PUBLIC, "192.168.1.1"]):
        with pytest.raises(FetchRefused) as e:
            run(Script(responses=[Resp(VIEW)]), rec, resolve=resolver({"dash.example.gov": bad}))
        assert e.value.code == "blocked_address"
    assert "launch" not in rec


def test_each_request_host_is_resolved_and_a_private_one_is_aborted() -> None:
    rec: dict[str, Any] = {}
    seen: list[str] = []
    hosts = frozenset({"dash.example.gov", "cdn.example.gov"})
    reqs = [
        Req("https://cdn.example.gov/a.js", "script", False),
        Req("https://cdn.example.gov/b.js", "script", False),
        Req("https://dash.example.gov/c.js", "script", False),
    ]
    script = Script(requests=reqs, responses=[Resp(VIEW)])
    bf.browser_fetch(VIEW, allowed_hosts=hosts, playwright=fake_playwright(script, rec), capture_wait=0.3,
                     resolve=resolver({"cdn.example.gov": ["10.1.2.3"]}, seen))  # fmt: skip
    assert rec["routes"] == [
        ("abort", "https://cdn.example.gov/a.js"),
        ("abort", "https://cdn.example.gov/b.js"),
        ("continue", "https://dash.example.gov/c.js"),
    ]
    assert seen == ["dash.example.gov", "cdn.example.gov"]  # each host resolved once per fetch


def test_robots_policy_can_refuse_same_host_requests() -> None:
    rec: dict[str, Any] = {}
    reqs = [Req("https://dash.example.gov/vizql/x", "xhr", False),
            Req("https://dash.example.gov/ok.js", "script", False)]  # fmt: skip
    run(Script(requests=reqs, responses=[Resp(VIEW)]), rec, allow=lambda u: "/vizql/" not in u)
    assert rec["routes"] == [
        ("abort", "https://dash.example.gov/vizql/x"),
        ("continue", "https://dash.example.gov/ok.js"),
    ]


class Hop:
    def __init__(self, url: str, before: Any = None) -> None:
        self.url, self.redirected_from = url, before


@pytest.mark.parametrize("first", ["https://evil.example.com/landing", "http://dash.example.gov/insecure"])
def test_a_redirect_chain_through_a_disallowed_address_refuses_the_load(first: str) -> None:
    # The document's request was redirected: route() never saw the first hop, so it is checked after the load.
    req = Req(VIEW)
    req.redirected_from = Hop("https://dash.example.gov/start", Hop(first))  # type: ignore[attr-defined]
    with pytest.raises(FetchRefused) as e:
        run(Script(responses=[Resp(VIEW, request=req)]))
    assert e.value.code == "redirect_refused" and "outside the catalogued publisher hosts" in e.value.message


def test_a_captured_answer_from_another_host_refuses_the_load() -> None:
    elsewhere = "https://evil.example.com/vizql/bootstrapSession/sessions/1"
    stray = Resp(elsewhere, content=b"2;{}", request=Req(elsewhere, "xhr", False))
    with pytest.raises(FetchRefused) as e:
        run(Script(responses=[stray, Resp(VIEW)]), capture=["/bootstrapSession/"])
    assert e.value.code == "redirect_refused"


def test_a_final_page_on_a_private_address_refuses_the_load() -> None:
    hosts = frozenset({"dash.example.gov", "inner.example.gov"})
    inner = "https://inner.example.gov/x"
    script = Script(responses=[Resp(inner, request=Req(inner))])
    with pytest.raises(FetchRefused) as e:
        bf.browser_fetch(VIEW, allowed_hosts=hosts, capture_wait=0.3, playwright=fake_playwright(script, {}),
                         resolve=resolver({"inner.example.gov": ["127.0.0.1"]}))  # fmt: skip
    assert e.value.code == "redirect_refused"


def test_a_same_host_redirect_chain_is_fine() -> None:
    req = Req(VIEW)
    req.redirected_from = Hop("https://dash.example.gov/views/Toy/TB")  # type: ignore[attr-defined]
    assert run(Script(responses=[Resp(VIEW, request=req)])).page.status == 200


def test_websockets_are_closed_and_popups_are_closed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="health_data_registry.browser_fetch")
    rec: dict[str, Any] = {}
    run(Script(responses=[Resp(VIEW)], sockets=2, popups=1), rec)
    assert rec["socket_pattern"] == "**/*" and rec["sockets_closed"] == 2
    assert rec["popups_closed"] == 1  # the page itself is never closed as a popup
    assert "browser fetch: 1 popup windows were closed (host=dash.example.gov)" in caplog.messages


def test_requests_robots_refused_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="health_data_registry.browser_fetch")
    reqs = [Req("https://dash.example.gov/vizql/x", "xhr", False)]
    run(Script(requests=reqs, responses=[Resp(VIEW)]), allow=lambda u: False)
    assert (
        "browser fetch: 1 requests robots.txt disallows were not made (host=dash.example.gov)"
        in caplog.messages
    )


def test_ipv6_addresses_are_pinned_in_brackets() -> None:
    assert bf._pin_rule("h.example.gov", "2606:2800:220:1::1") == "MAP h.example.gov [2606:2800:220:1::1]"
    assert bf._pin_rule("h.example.gov", "93.184.216.34") == "MAP h.example.gov 93.184.216.34"


def test_host_of_a_malformed_url_is_empty() -> None:
    assert bf._host("https://[::1") == "" and bf._host("https://Dash.Example.gov/x") == "dash.example.gov"
