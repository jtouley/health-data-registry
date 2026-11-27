"""robots.txt and crawl delays (outside review (d)): every publisher host's rules are read once and honoured."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest

from health_data_registry import crawl
from health_data_registry import fetch as fetch_mod

HOSTS = frozenset({"data.cdc.gov", "www2.census.gov"})


def answer(url: str, status: int = 200, body: bytes = b"[]") -> fetch_mod.FetchResult:
    return fetch_mod.FetchResult(
        url=url, final_url=url, status=status, content=body, content_type="text/plain",
        sha256=hashlib.sha256(body).hexdigest(), retrieved_at=datetime(2026, 11, 24, tzinfo=UTC),
    )  # fmt: skip


def fake(monkeypatch: pytest.MonkeyPatch, robots: dict[str, Any]) -> list[str]:
    seen: list[str] = []

    def f(url: str, **kw: Any) -> fetch_mod.FetchResult:
        seen.append(url)
        if url.endswith("/robots.txt"):
            host = url.split("/")[2]
            r = robots.get(host, 404)
            if isinstance(r, Exception):
                raise r
            return answer(url, r, b"") if isinstance(r, int) else answer(url, 200, r.encode())
        return answer(url)

    monkeypatch.setattr(fetch_mod, "fetch", f)
    return seen


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s

    def __call__(self) -> float:
        return self.now


def test_disallowed_paths_are_refused_and_robots_is_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = fake(monkeypatch, {"data.cdc.gov": "User-agent: *\nDisallow: /private/\n"})
    rb = crawl.Robots(HOSTS)
    rb.wait_turn("https://data.cdc.gov/resource/x.json")
    rb.wait_turn("https://data.cdc.gov/resource/y.json")
    with pytest.raises(fetch_mod.FetchRefused) as e:
        rb.wait_turn("https://data.cdc.gov/private/z.json")
    assert e.value.code == "robots_disallowed" and "robots.txt" in str(e.value)
    assert seen == ["https://data.cdc.gov/robots.txt"]


def test_crawl_delay_is_honoured_within_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, {"data.cdc.gov": "User-agent: *\nCrawl-delay: 10\n"})
    clock = Clock()
    rb = crawl.Robots(HOSTS, sleep=clock.sleep, clock=clock, budget_s=25)
    for _ in range(3):
        rb.wait_turn("https://data.cdc.gov/a")
    assert clock.slept == [10.0, 10.0]
    with pytest.raises(fetch_mod.FetchRefused) as e:
        rb.wait_turn("https://data.cdc.gov/a")
    assert e.value.code == "crawl_delay_budget"


def test_missing_forbidden_and_unreadable_robots(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, {"data.cdc.gov": 404, "www2.census.gov": 403})
    rb = crawl.Robots(HOSTS)
    rb.wait_turn("https://data.cdc.gov/anything")  # no robots.txt: everything allowed
    with pytest.raises(fetch_mod.FetchRefused):
        rb.wait_turn("https://www2.census.gov/x.csv")  # robots.txt withheld: nothing allowed
    seen = fake(monkeypatch, {"data.cdc.gov": fetch_mod.FetchRefused("timeout", "slow")})
    rb2 = crawl.Robots(HOSTS)
    rb2.wait_turn("https://data.cdc.gov/a")
    rb2.wait_turn("https://data.cdc.gov/b")
    assert seen.count("https://data.cdc.gov/robots.txt") == 2  # not cached: asked again


def test_the_default_fetcher_checks_robots(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from health_data_registry.registry import load_registry
    from health_data_registry.store import FetchLog, read_fetch_log
    from tests.test_hub import write_hub_registry

    reg = load_registry(write_hub_registry(tmp_path / "reg"))
    fake(monkeypatch, {"data.cdc.gov": "User-agent: *\nDisallow: /\n"})
    get = crawl.fetcher(reg, FetchLog(tmp_path, "toy_cases"), sleep=lambda s: None)
    with pytest.raises(fetch_mod.FetchRefused):
        get("https://data.cdc.gov/resource/x.json")
    (row,) = read_fetch_log(tmp_path, "toy_cases")
    assert row["status"] == "error" and "robots.txt" in row["error"]


def test_crawl_delay_never_sleeps_past_a_hosted_call_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside a hosted call's deadline a crawl delay longer than what is left stops the call, without sleeping."""
    from health_data_registry.deadline import DeadlineExceeded, call_deadline

    asked: list[float] = []
    fake(monkeypatch, {"data.cdc.gov": "User-agent: *\nCrawl-delay: 5\n"})
    real = fetch_mod.fetch

    def timed(url: str, **kw: Any) -> fetch_mod.FetchResult:
        asked.append(kw["timeout"])
        return real(url, **kw)

    monkeypatch.setattr(fetch_mod, "fetch", timed)
    clock = Clock()
    rb = crawl.Robots(HOSTS, sleep=clock.sleep, clock=clock)
    with call_deadline(4.0):
        rb.wait_turn("https://data.cdc.gov/resource/x.json")
        assert asked and asked[0] <= 4.0  # the robots.txt read is capped by the deadline too
        with pytest.raises(DeadlineExceeded):
            rb.wait_turn("https://data.cdc.gov/resource/y.json")
    assert clock.slept == []
    with call_deadline(5.0), pytest.raises(DeadlineExceeded):  # exactly the delay left: still nothing sleeps
        rb.wait_turn("https://data.cdc.gov/resource/y.json")
    with call_deadline(60.0):
        rb.wait_turn("https://data.cdc.gov/resource/y.json")
    assert clock.slept == [5.0]
