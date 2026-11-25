"""The one GitHub REST client the CI scripts share (ci_budget.py, merge_queue.py, mutation_nightly.py).

https only, and the token never leaves api.github.com's host: it is sent as an unredirected header, and any
redirect is refused rather than followed. urllib's default redirect handler copies every header but
Content-Length and Content-Type to the next request, to any host and over plain http, so a 30x would hand the
run's token to whatever the Location names. GitHub's REST endpoints these scripts call do not redirect off-host,
so a refused redirect is an error, and every caller treats an error as its safe answer.

scripts/verdict_reuse.py keeps its own copy of the same rules: CI runs main's copy of that file alone, outside
the checkout, so it cannot import this one. tests/test_github_api.py checks both behave the same.
Standard library only.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

DEFAULT_API = "https://api.github.com"

GetJson = Callable[[str], Any]
SendJson = Callable[[str, str, dict[str, Any]], Any]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the caller gets an HTTPError for the 30x, never a request to another URL."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect to {newurl!r} refused (the token stays on this host)", headers, fp
        )


_OPENER = urllib.request.build_opener(_NoRedirect)


def api_base(api_url: str | None = None) -> str:
    """The API root (GITHUB_API_URL, else api.github.com), without a trailing slash. Only https is accepted."""
    base = (api_url if api_url is not None else os.environ.get("GITHUB_API_URL", DEFAULT_API)).rstrip("/")
    if urllib.parse.urlsplit(base).scheme != "https":
        raise ValueError(f"the API URL must be https: {base!r}")
    return base


def request(
    url: str, token: str, method: str = "GET", body: dict[str, Any] | None = None
) -> urllib.request.Request:
    """A request whose Authorization header is never copied onto a redirect (add_unredirected_header)."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError(f"the API URL must be https: {url!r}")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 (https only, checked above)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    return req


def _open(req: urllib.request.Request) -> Any:
    with _OPENER.open(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def get_json(token: str, api_url: str | None = None) -> GetJson:
    """A GET for paths under the API root (`/repos/...`), authorised with `token`."""
    base = api_base(api_url)
    return lambda path: _open(request(base + path, token))


def send_json(token: str, api_url: str | None = None) -> SendJson:
    """A POST or PATCH with a JSON body, for paths under the API root."""
    base = api_base(api_url)
    return lambda method, path, body: _open(request(base + path, token, method, body))
