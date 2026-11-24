"""Fetch a public page from a catalogued publisher host that answers only a real browser (Playwright Chromium).

Some government hosts (michigan.gov behind Akamai, Tableau Server dashboards that build their data session in
JavaScript) send nothing useful to a plain HTTPS client. This fetcher opens the page in headless Chromium under
the same rules as fetch.py:

- The same honest user agent (``fetch.USER_AGENT``); nothing that hides that a program is reading the page: no
  user-agent spoofing, no stealth or fingerprint plugins, no proxies of our own (the operator's egress proxy,
  ``HDR_HTTPS_PROXY``, is used as fetch.py uses it), no sign-ins, no CAPTCHA solving.
- The same host allowlist: the page and every request it makes go only to catalogued hosts over HTTPS; any
  other request is aborted, never followed. Images, fonts and media are not loaded (the data never needs them).
  Workbook downloads (.twb, .twbx) are refused.
- A block is final. A 401/403/429 answer, or an edge challenge page (Akamai "Access Denied", Cloudflare "Just a
  moment"), raises ``FetchRefused("blocked")``: it is recorded by the caller and the fetch stops; it is never
  retried another way.
- Pacing is the caller's (crawl.Robots), exactly as for fetch.py: one page per turn.
- What comes back is the same ``FetchResult``: the raw bytes of the page, or of the responses named in
  ``capture`` (e.g. Tableau's ``/bootstrapSession/``), each with its sha256 and ``retrieved_at``, for the same
  hash-addressed raw store.
"""

from __future__ import annotations

import hashlib
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


def browser_fetch(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    capture: Sequence[str] = (),
    timeout: float = 90.0,
    max_bytes: int = 50_000_000,
    playwright: PlaywrightFactory = _default_factory,
) -> BrowserResult:
    """Open ``url`` in headless Chromium under fetch.py's rules (see the module doc).

    Args:
        url: the catalogued page.
        allowed_hosts: hosts the page and its requests may reach; everything else is aborted.
        capture: substrings of response paths whose bodies are kept (e.g. "/bootstrapSession/").
        timeout: deadline for loading the page, in seconds.
        max_bytes: cap on each kept body.
        playwright: factory for the Playwright context manager (tests pass a fake).

    Raises:
        FetchRefused: "blocked" when the host refuses the client, "http_error" for another failed answer,
            "too_large", "timeout", or the link checks of fetch.validate_url.
    """
    if max_bytes < 1 or not timeout > 0:
        raise ValueError("max_bytes and timeout must be positive")
    validate_url(url, allowed_hosts)
    host = (urlsplit(url).hostname or "").lower()
    captured: list[FetchResult] = []
    aborted: list[str] = []
    oversized: list[str] = []

    def route(r: Any) -> None:
        req = r.request
        if req.resource_type in SKIPPED_RESOURCES or not request_allowed(req.url, allowed_hosts):
            if req.resource_type not in SKIPPED_RESOURCES:
                aborted.append(req.url)
            r.abort()
        else:
            r.continue_()

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
    with playwright() as pw:
        browser = pw.chromium.launch(**({"proxy": {"server": proxy}} if proxy else {}))
        try:
            context = browser.new_context(
                user_agent=USER_AGENT, accept_downloads=False, service_workers="block", locale="en-US"
            )
            page = context.new_page()
            page.route("**/*", route)
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
            deadline = time.monotonic() + min(timeout, CAPTURE_WAIT_S)
            while capture and not captured and time.monotonic() < deadline:
                page.wait_for_timeout(500)  # the data request can follow the page by a few seconds
        finally:
            browser.close()
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
    logger.info("browser fetched: host=%s bytes=%d captured=%d", host, len(main.content), len(captured))
    return BrowserResult(page=main, captured=tuple(captured))
