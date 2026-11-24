"""The polite crawler client: robots.txt first, the robots delay with a fake clock, Disallow, restarts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from health_data_registry.fetch import FetchRefused, FetchResult
from health_data_registry.polite import CrawlStopped, PoliteClient
from health_data_registry.store import FetchLog, read_fetch_log

HOST = "www.example.org"
ROBOTS = b"User-agent: *\nCrawl-delay: 120\nDisallow: /wp-admin/\nDisallow: /private-area/\n"


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class FakeSite:
    def __init__(
        self, clock: FakeClock, pages: dict[str, bytes] | None = None, robots: bytes | None = ROBOTS
    ):
        self.clock = clock
        self.pages = {f"https://{HOST}/robots.txt": robots, **(pages or {})}
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str) -> FetchResult:
        self.calls.append((url, self.clock.now()))
        body = self.pages.get(url)
        if body is None:
            raise FetchRefused("http_error", "The publisher answered with status 404.", status=404)
        return FetchResult(
            url, url, 200, body, "text/html", hashlib.sha256(body).hexdigest(), datetime.now(UTC)
        )


def client(tmp_path: Path, site: FakeSite, clock: FakeClock, **kw: object) -> PoliteClient:
    return PoliteClient(
        f"https://{HOST}",
        state_path=tmp_path / "state.json",
        get_raw=site,
        clock=clock,
        log=FetchLog(tmp_path, "toy"),
        **kw,  # type: ignore[arg-type]
    )


def test_robots_is_the_first_request_and_the_delay_comes_from_it(tmp_path: Path) -> None:
    clock = FakeClock()
    pages = {f"https://{HOST}/a/": b"a", f"https://{HOST}/b/": b"b"}
    site = FakeSite(clock, pages, robots=b"User-agent: *\nCrawl-delay: 37\n")
    c = client(tmp_path, site, clock)
    c.get(f"https://{HOST}/a/")
    c.get(f"https://{HOST}/b/")
    urls = [u for u, _ in site.calls]
    assert urls[0] == f"https://{HOST}/robots.txt"
    assert c.delay == 37.0  # read from robots, not written in the code
    times = [t for _, t in site.calls]
    assert all(b - a >= 37.0 for a, b in zip(times, times[1:], strict=False))
    assert [r["url"] for r in read_fetch_log(tmp_path, "toy")] == urls


def test_120_second_delay_is_enforced_between_every_request(tmp_path: Path) -> None:
    clock = FakeClock()
    pages = {f"https://{HOST}/p/{i}/": b"x" for i in range(4)}
    site = FakeSite(clock, pages)
    c = client(tmp_path, site, clock)
    for i in range(4):
        c.get(f"https://{HOST}/p/{i}/")
    times = [t for _, t in site.calls]
    assert len(times) == 5
    assert [b - a for a, b in zip(times, times[1:], strict=False)] == [120.0] * 4


def test_disallowed_links_are_never_requested(tmp_path: Path) -> None:
    clock = FakeClock()
    site = FakeSite(clock, {f"https://{HOST}/private-area/x/": b"secret"})
    c = client(tmp_path, site, clock)
    with pytest.raises(CrawlStopped) as e:
        c.get(f"https://{HOST}/private-area/x/")
    assert e.value.code == "robots_disallow"
    assert [u for u, _ in site.calls] == [f"https://{HOST}/robots.txt"]


def test_other_hosts_are_refused(tmp_path: Path) -> None:
    clock = FakeClock()
    site = FakeSite(clock)
    with pytest.raises(CrawlStopped):
        client(tmp_path, site, clock).get("https://elsewhere.example.com/")
    assert site.calls == []


def test_a_restart_cannot_burst(tmp_path: Path) -> None:
    clock = FakeClock()
    site = FakeSite(clock, {f"https://{HOST}/a/": b"a"})
    client(tmp_path, site, clock).get(f"https://{HOST}/a/")
    last = site.calls[-1][1]
    clock.t += 5  # the process restarts five seconds later
    client(tmp_path, site, clock).get(f"https://{HOST}/a/")
    # the new run's robots.txt request still waited for the delay recorded by the last run
    assert site.calls[2][0].endswith("/robots.txt")
    assert site.calls[2][1] - last >= 120.0
    assert json.loads((tmp_path / "state.json").read_text())["delay_s"] == 120.0


def test_budget_stops_the_run(tmp_path: Path) -> None:
    clock = FakeClock()
    site = FakeSite(clock, {f"https://{HOST}/a/": b"a"})
    c = client(tmp_path, site, clock, max_requests=2)
    c.get(f"https://{HOST}/a/")
    with pytest.raises(CrawlStopped) as e:
        c.get(f"https://{HOST}/a/")
    assert e.value.code == "budget_spent"
    assert len(site.calls) == 2


def test_missing_robots_means_no_rules_and_the_default_delay(tmp_path: Path) -> None:
    clock = FakeClock()
    site = FakeSite(clock, {f"https://{HOST}/a/": b"a"}, robots=None)
    c = client(tmp_path, site, clock, default_delay=90.0)
    c.get(f"https://{HOST}/a/")
    assert c.delay == 90.0
    assert site.calls[1][1] - site.calls[0][1] == 90.0


def test_unreadable_robots_stops_before_anything_else(tmp_path: Path) -> None:
    clock = FakeClock()

    def broken(url: str) -> FetchResult:
        raise FetchRefused("http_error", "The publisher answered with status 503.", status=503)

    c = PoliteClient(f"https://{HOST}", state_path=tmp_path / "s.json", get_raw=broken, clock=clock)
    with pytest.raises(CrawlStopped) as e:
        c.get(f"https://{HOST}/a/")
    assert e.value.code == "robots_unreadable"
    assert c.requests_made == 1


def test_only_https_sites() -> None:
    with pytest.raises(ValueError):
        PoliteClient("http://x.org", state_path=Path("s"), get_raw=lambda u: None)  # type: ignore[arg-type,return-value]
