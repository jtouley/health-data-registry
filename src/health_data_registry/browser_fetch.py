"""Fetch a public page from a catalogued publisher host that answers only a real browser (Playwright Chromium).

Some government hosts (michigan.gov behind Akamai, Tableau Server dashboards that build their data session in
JavaScript) send nothing useful to a plain HTTPS client. This fetcher opens the page in headless Chromium with
these rules. They follow fetch.py's where a browser allows it; where it doesn't, the difference is stated here.

- The same honest user agent (``fetch.USER_AGENT``); nothing that hides that a program is reading the page: no
  user-agent spoofing, no stealth or fingerprint plugins, no proxies of our own (the operator's egress proxy,
  ``HDR_HTTPS_PROXY``, is used as fetch.py uses it), no sign-ins, no CAPTCHA solving.
- The same host allowlist, checked on every request the browser context makes (popups included): HTTPS to a
  catalogued host only; anything else is aborted, never followed. Images, fonts and media are not loaded (the
  data never needs them). Workbook downloads (.twb, .twbx) are refused. WebSockets are closed. A popup window is
  closed as soon as it opens. ``allow`` (crawl.py passes robots.txt for hosts that are not government hosts) can
  refuse more.
- Public addresses only: each host is resolved with ``fetch.public_addresses`` before the page is opened and
  before any request to it goes out, and a host that resolves to a private, loopback, link-local or metadata
  address is refused. Without an egress proxy the page's own host is pinned to the address checked here
  (Chromium's ``--host-resolver-rules``). Chromium resolves the other hosts again itself, so unlike fetch.py a
  host whose DNS answer changes between the check and the connection is not caught; the operator's egress proxy
  is the backstop for that.
- Redirects are checked after the load. A redirect does not pass through the request handler again, so every
  document and every captured answer, and each step of its redirect chain, is checked against the allowlist and
  the address check once the page has loaded; one that fails refuses the whole fetch (``redirect_refused``).
- A block is final. A 401/403/429 answer, or an edge challenge page (Akamai "Access Denied", Cloudflare "Just a
  moment"), raises ``FetchRefused("blocked")``: it is recorded by the caller and the fetch stops; it is never
  retried another way.
- Pacing is the caller's (crawl.Robots): one page load per turn. The page's own scripts and its data request
  follow the page within the same load, so the pace is between page loads, not between every request.
- What comes back is the same ``FetchResult``: the raw bytes of the page, or of the responses named in
  ``capture`` (e.g. Tableau's ``/bootstrapSession/``), each with its sha256 and ``retrieved_at``, for the same
  hash-addressed raw store.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit

from health_data_registry import fetch as _fetch
from health_data_registry.fetch import PROXY_ENV, USER_AGENT, FetchRefused, FetchResult, validate_url

logger = logging.getLogger(__name__)

SKIPPED_RESOURCES: Final = frozenset({"image", "font", "media"})
CAPTURE_WAIT_S: Final = 30.0
BLOCK_STATUSES: Final = frozenset({401, 403, 407, 429})
# Text an edge network serves instead of the page when it refuses a client. Matched on the main document only.
_CHALLENGE: Final = re.compile(
    rb"(?i)(<title>\s*Access Denied\s*</title>|Reference&#32;&#35;|errors\.edgesuite\.net|"
    rb"<title>\s*Just a moment\.\.\.\s*</title>|cf-chl-|challenges\.cloudflare\.com|<title>\s*Attention Required)"
)
_WORKBOOK: Final = re.compile(r"(?i)\.twbx?$")

PlaywrightFactory = Callable[[], AbstractContextManager[Any]]
Resolver = Callable[[str], Sequence[str]]


@dataclass(frozen=True)
class BrowserResult:
    """The page as the browser received it, and the responses captured while it loaded."""

    page: FetchResult
    captured: tuple[FetchResult, ...]


def _default_factory() -> AbstractContextManager[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - depends on the optional extra being installed
        raise FetchRefused(
            "browser_unavailable",
            "This source needs a browser to read, and none is installed here (install the 'browser' extra).",
        ) from e
    return sync_playwright()


def request_allowed(url: str, allowed_hosts: frozenset[str]) -> bool:
    """True when a request the page makes may go out: HTTPS to a catalogued host, and not a workbook file."""
    try:
        validate_url(url, allowed_hosts)
    except FetchRefused:
        return False
    return not _WORKBOOK.search(urlsplit(url).path)


def is_blocked(status: int, body: bytes) -> bool:
    """True when the host refused the client (a block or an edge challenge), rather than serving the page."""
    return status in BLOCK_STATUSES or bool(_CHALLENGE.search(body[:200_000]))


def _result(url: str, final_url: str, status: int, body: bytes, content_type: str | None) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=final_url,
        status=status,
        content=body,
        content_type=content_type,
        sha256=hashlib.sha256(body).hexdigest(),
        retrieved_at=datetime.now(UTC),
    )


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _pin_rule(host: str, address: str) -> str:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    return f"MAP {host} {'[' + str(ip) + ']' if ip.version == 6 else str(ip)}"


def browser_fetch(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    capture: Sequence[str] = (),
    timeout: float = 90.0,
    max_bytes: int = 50_000_000,
    capture_wait: float = CAPTURE_WAIT_S,
    allow: Callable[[str], bool] | None = None,
    resolve: Resolver | None = None,
    playwright: PlaywrightFactory = _default_factory,
) -> BrowserResult:
    """Open ``url`` in headless Chromium under the rules in the module doc.

    Args:
        url: the catalogued page.
        allowed_hosts: hosts the page and its requests may reach; everything else is aborted.
        capture: substrings of response paths whose bodies are kept (e.g. "/bootstrapSession/").
        timeout: deadline for loading the page, in seconds.
        max_bytes: cap on each kept body.
        capture_wait: how long to wait, after the page settles, for a captured response that has not come yet.
        allow: a further check on each request the page makes (crawl.py: robots.txt); False aborts it.
        resolve: checks a host's addresses and returns them, raising FetchRefused when one isn't public
            (default ``fetch.public_addresses``; tests pass a fake).
        playwright: factory for the Playwright context manager (tests pass a fake).

    Raises:
        FetchRefused: "blocked" when the host refuses the client, "http_error" for another failed answer,
            "redirect_refused" when the page or its data ended up somewhere the rules don't allow, "too_large",
            "timeout", "blocked_address", or the link checks of fetch.validate_url.
    """
    if max_bytes < 1 or not timeout > 0:
        raise ValueError("max_bytes and timeout must be positive")
    validate_url(url, allowed_hosts)
    host = _host(url)
    check_addresses = resolve or _fetch.public_addresses
    resolved: dict[str, Sequence[str] | None] = {}

    def public(h: str) -> bool:
        if h not in resolved:
            try:
                resolved[h] = list(check_addresses(h))
            except FetchRefused:
                resolved[h] = None
        return resolved[h] is not None

    main_addresses = check_addresses(host)  # raises before a browser is started
    resolved[host] = list(main_addresses)
    captured: list[FetchResult] = []
    aborted: list[str] = []
    refused_by_policy: list[str] = []
    strays: list[str] = []
    oversized: list[str] = []
    popups: list[Any] = []

    def route(r: Any) -> None:
        req = r.request
        if req.resource_type in SKIPPED_RESOURCES:
            r.abort()
        elif not request_allowed(req.url, allowed_hosts) or not public(_host(req.url)):
            aborted.append(req.url)
            r.abort()
        elif allow is not None and not allow(req.url):
            refused_by_policy.append(req.url)
            r.abort()
        else:
            r.continue_()

    def reached(address: str) -> bool:
        """An address the page or its data came from, checked again after the fact (redirects skip route())."""
        return request_allowed(address, allowed_hosts) and public(_host(address))

    def chain(request: Any) -> list[str]:
        urls = []
        while request is not None:
            urls.append(str(request.url))
            request = getattr(request, "redirected_from", None)
        return urls

    documents: list[FetchResult] = []

    def on_response(resp: Any) -> None:
        # Bodies are read as they arrive: once the page moves on, the browser no longer has them.
        request = resp.request
        is_document = (
            request.resource_type == "document"
            and request.is_navigation_request()
            and request.frame.parent_frame is None  # the page itself, not an embedded frame
        )
        path = urlsplit(resp.url).path
        if not is_document and not any(c in path for c in capture):
            return
        strays.extend(u for u in [resp.url, *chain(request)] if not reached(u))
        try:
            body = resp.body()
        except Exception:  # noqa: BLE001 - Playwright raises its own error types when a body is gone
            body = b""
            logger.info("browser fetch: a response from %s had no readable body", host)
        if len(body) > max_bytes:
            oversized.append(resp.url)  # raised after the load: an event handler can't stop the page
            return
        result = _result(url if is_document else resp.url, resp.url, int(resp.status), body,
                         resp.headers.get("content-type"))  # fmt: skip
        (documents if is_document else captured).append(result)

    proxy = os.environ.get(PROXY_ENV, "").strip()
    launch: dict[str, Any] = (
        {"proxy": {"server": proxy}}
        if proxy
        else {"args": [f"--host-resolver-rules={_pin_rule(host, main_addresses[0])}"]}
    )
    page: Any = None

    def on_page(p: Any) -> None:
        if page is not None and p is not page:  # a popup: never used, closed at once
            popups.append(p)
            p.close()

    with playwright() as pw:
        browser = pw.chromium.launch(**launch)
        try:
            context = browser.new_context(
                user_agent=USER_AGENT, accept_downloads=False, service_workers="block", locale="en-US"
            )
            # On the context, not the page, so a popup's requests meet the same handler.
            context.route("**/*", route)
            context.route_web_socket("**/*", lambda ws: ws.close())
            context.on("page", on_page)
            page = context.new_page()
            page.on("response", on_response)
            try:
                response = page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            except FetchRefused:
                raise
            except Exception as e:  # noqa: BLE001 - Playwright's TimeoutError and navigation errors
                if "Timeout" in type(e).__name__:
                    raise FetchRefused("timeout", f"{host} took too long to load the page.") from e
                raise FetchRefused(
                    "connect_failed", f"The browser could not load the page from {host}."
                ) from e
            deadline = time.monotonic() + min(timeout, capture_wait)
            while capture and not captured and time.monotonic() < deadline:
                page.wait_for_timeout(500)  # the data request can follow the page by a few seconds
        finally:
            browser.close()
    if strays:
        raise FetchRefused(
            "redirect_refused",
            f"The page at {host} sent the browser to an address outside the catalogued publisher hosts, so "
            "nothing from this load was kept.",
        )
    if oversized:
        raise FetchRefused("too_large", f"A response from {host} is larger than the limit; it was not kept.")
    if response is None or not documents:  # nothing reached the page
        raise FetchRefused("connect_failed", f"The browser got no answer from {host}.")
    main = documents[-1]  # after any redirect, the page that was shown
    if is_blocked(main.status, main.content) or any(is_blocked(d.status, d.content) for d in documents):
        raise FetchRefused(
            "blocked",
            f"{host} refused this program's request (its firewall or edge network blocked it), so nothing was "
            "fetched. This is recorded and not worked around.",
            status=main.status,
        )
    if main.status != 200:
        raise FetchRefused(
            "http_error", f"The publisher answered with status {main.status}.", status=main.status
        )
    if aborted:
        logger.info("browser fetch: %d requests to other hosts were not made (host=%s)", len(aborted), host)
    if refused_by_policy:
        logger.info("browser fetch: %d requests robots.txt disallows were not made (host=%s)",
                    len(refused_by_policy), host)  # fmt: skip
    if popups:
        logger.info("browser fetch: %d popup windows were closed (host=%s)", len(popups), host)
    logger.info("browser fetched: host=%s bytes=%d captured=%d", host, len(main.content), len(captured))
    return BrowserResult(page=main, captured=tuple(captured))
