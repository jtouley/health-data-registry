"""scripts/_github_api.py: https only, and the run's token never follows a redirect."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return _load("_github_api", monkeypatch)


def test_only_https_is_accepted(gh: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="https"):
        gh.api_base("http://api.github.com")
    with pytest.raises(ValueError, match="https"):
        gh.request("http://api.github.com/x", "t")
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example/api/v3/")
    assert gh.api_base() == "https://ghe.example/api/v3"
    monkeypatch.delenv("GITHUB_API_URL")
    assert gh.api_base() == "https://api.github.com"


def test_the_token_is_an_unredirected_header(gh: ModuleType) -> None:
    req = gh.request("https://api.github.com/x", "secret", "POST", {"a": 1})
    assert req.get_method() == "POST" and json.loads(req.data) == {"a": 1}
    assert req.unredirected_hdrs == {"Authorization": "Bearer secret"}
    assert "Authorization" not in req.headers and req.headers["Content-type"] == "application/json"
    assert "Authorization" not in gh.request("https://api.github.com/x", "").unredirected_hdrs


@pytest.mark.parametrize("target", ["http://api.github.com/x", "https://elsewhere.example/x"])
def test_every_redirect_is_refused(gh: ModuleType, target: str) -> None:
    req = gh.request("https://api.github.com/x", "secret")
    with pytest.raises(urllib.error.HTTPError, match="refused"):
        gh._NoRedirect().redirect_request(req, None, 302, "Found", {}, target)


def test_get_and_send_go_through_the_refusing_opener(gh: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[urllib.request.Request] = []

    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    def fake_open(req: urllib.request.Request, timeout: float) -> Response:
        seen.append(req)
        return Response(b'{"ok": true}' if req.get_method() == "GET" else b"")

    monkeypatch.setattr(gh._OPENER, "open", fake_open)
    assert gh.get_json("t", "https://api.github.com")("/repos/o/r") == {"ok": True}
    assert gh.send_json("t", "https://api.github.com")("PATCH", "/repos/o/r/issues/1", {"body": "x"}) is None
    assert [r.full_url for r in seen] == [
        "https://api.github.com/repos/o/r",
        "https://api.github.com/repos/o/r/issues/1",
    ]
    assert any(isinstance(h, gh._NoRedirect) for h in gh._OPENER.handlers)


@pytest.mark.parametrize("name", ["ci_budget", "merge_queue", "mutation_nightly"])
def test_the_ci_scripts_use_the_shared_client(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    text = (ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8")
    assert "import _github_api" in text
    assert "urlopen" not in text and '"Authorization"' not in text
