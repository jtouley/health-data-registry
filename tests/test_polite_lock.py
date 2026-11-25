"""D28: one crawler per host (an inter-process lock), the stored host identity checked on restore, and the rest
of robots.txt compliance (Request-rate, the 24-hour robots.txt lifetime, our own user-agent group)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from health_data_registry.fetch import FetchRefused, FetchResult
from health_data_registry.polite import CrawlStopped, PoliteClient
from tests.test_polite import HOST, FakeClock, FakeSite, client

_HOLDER = """
import sys
from pathlib import Path
from health_data_registry.polite import PoliteClient

c = PoliteClient("https://www.example.org", state_path=Path(sys.argv[1]), get_raw=lambda u: None)
print("locked", flush=True)
sys.stdin.readline()  # hold the lock until the test says stop
c.close()
print("released", flush=True)
"""


def _holder(state: Path) -> subprocess.Popen[str]:
    proc = subprocess.Popen(  # noqa: S603 - this interpreter and a fixed script
        [sys.executable, "-c", _HOLDER, str(state)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "locked"
    return proc


def test_a_second_process_cannot_crawl_the_same_host(tmp_path: Path) -> None:
    holder = _holder(tmp_path / "state.json")
    clock = FakeClock()
    site = FakeSite(clock, {f"https://{HOST}/a/": b"a"})
    try:
        with pytest.raises(CrawlStopped) as e:
            client(tmp_path, site, clock)
        assert e.value.code == "host_locked"
        assert site.calls == []  # refused before any request, robots.txt included
        assert (tmp_path / "state.lock").read_text().strip() == str(holder.pid)
        assert holder.stdin is not None and holder.stdout is not None
        holder.stdin.write("stop\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "released"
        assert holder.wait(timeout=30) == 0
    finally:
        if holder.poll() is None:
            holder.kill()
    with client(tmp_path, site, clock) as c:  # free again once the other process let go
        c.get(f"https://{HOST}/a/")
    assert site.calls[-1][0] == f"https://{HOST}/a/"


def test_a_killed_process_leaves_no_stale_lock(tmp_path: Path) -> None:
    holder = _holder(tmp_path / "state.json")
    holder.kill()
    holder.wait(timeout=30)
    clock = FakeClock()
    with client(tmp_path, FakeSite(clock), clock) as c:
        assert c.lock.held
    assert not c.lock.held


def test_a_second_client_in_the_same_process_is_refused_until_closed(tmp_path: Path) -> None:
    clock = FakeClock()
    site = FakeSite(clock, {f"https://{HOST}/a/": b"a"})
    first = client(tmp_path, site, clock)
    with pytest.raises(CrawlStopped) as e:
        client(tmp_path, site, clock)
    assert e.value.code == "host_locked"
    first.close()
    first.close()  # closing twice is harmless
    with pytest.raises(CrawlStopped) as closed:
        first.get(f"https://{HOST}/a/")
    assert closed.value.code == "closed"
    assert site.calls == []
    with client(tmp_path, site, clock) as again:
        again.get(f"https://{HOST}/a/")
    assert len(site.calls) == 2


def test_state_for_another_host_is_refused_on_restore(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps({"host": "other.example.com", "last_request_at": 1.0, "delay_s": 1.0})
    )
    clock = FakeClock()
    site = FakeSite(clock)
    with pytest.raises(CrawlStopped) as e:
        client(tmp_path, site, clock)
    assert e.value.code == "state_host_mismatch"
    assert str(e.value) == (
        f"the crawl state {tmp_path / 'state.json'} was written for other.example.com, not {HOST}, so it was "
        "not reused"
    )
    assert site.calls == []
    with pytest.raises(CrawlStopped):  # still refused: the state was not overwritten
        client(tmp_path, site, clock)
    (tmp_path / "state.json").unlink()
    with client(tmp_path, site, clock) as c:  # the refused clients let go of the lock
        assert c.lock.held


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("{not json", "state_unreadable"),
        (json.dumps({"last_request_at": 1.0}), "state_host_mismatch"),
        (json.dumps(["www.example.org"]), "state_host_mismatch"),
    ],
)
def test_unreadable_or_anonymous_state_is_refused(tmp_path: Path, text: str, code: str) -> None:
    (tmp_path / "state.json").write_text(text)
    clock = FakeClock()
    with pytest.raises(CrawlStopped) as e:
        client(tmp_path, FakeSite(clock), clock)
    assert e.value.code == code


def test_state_names_its_host_and_restores_the_last_request(tmp_path: Path) -> None:
    clock = FakeClock()
    site = FakeSite(clock, {f"https://{HOST}/a/": b"a"})
    with client(tmp_path, site, clock) as c:
        c.get(f"https://{HOST}/a/")
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved == {"host": HOST, "last_request_at": site.calls[-1][1], "delay_s": 120.0}
    with client(tmp_path, site, clock) as again:
        assert again._state.last_request_at == site.calls[-1][1]
        assert again._pace() == 120.0


def test_request_rate_is_honoured_when_slower_than_crawl_delay(tmp_path: Path) -> None:
    clock = FakeClock()
    robots = b"User-agent: *\nCrawl-delay: 10\nRequest-rate: 1/60\n"
    site = FakeSite(clock, {f"https://{HOST}/a/": b"a"}, robots=robots)
    with client(tmp_path, site, clock) as c:
        c.get(f"https://{HOST}/a/")
        assert c.delay == 60.0
    assert site.calls[1][1] - site.calls[0][1] == 60.0


def test_crawl_delay_wins_when_slower_than_request_rate(tmp_path: Path) -> None:
    clock = FakeClock()
    robots = b"User-agent: *\nCrawl-delay: 90\nRequest-rate: 2/60\n"
    with client(tmp_path, FakeSite(clock, robots=robots), clock) as c:
        c.start()
        assert c.delay == 90.0


def test_robots_is_read_again_after_24_hours(tmp_path: Path) -> None:
    clock = FakeClock()
    site = FakeSite(clock, {f"https://{HOST}/a/": b"a"}, robots=b"User-agent: *\nCrawl-delay: 1\n")
    robots_url = f"https://{HOST}/robots.txt"
    with client(tmp_path, site, clock) as c:
        c.get(f"https://{HOST}/a/")
        clock.t += 23 * 3600
        c.get(f"https://{HOST}/a/")
        assert [u for u, _ in site.calls].count(robots_url) == 1
        site.pages[robots_url] = b"User-agent: *\nDisallow: /a/\n"
        clock.t += 3600
        with pytest.raises(CrawlStopped) as e:
            c.get(f"https://{HOST}/a/")
    assert e.value.code == "robots_disallow"
    assert [u for u, _ in site.calls].count(robots_url) == 2


def test_our_user_agent_group_wins_over_the_star_group(tmp_path: Path) -> None:
    clock = FakeClock()
    robots = b"User-agent: health-data-registry\nCrawl-delay: 300\n\nUser-agent: *\nCrawl-delay: 5\n"
    with client(tmp_path, FakeSite(clock, robots=robots), clock) as c:
        c.start()
        assert c.delay == 300.0


def test_a_disallow_for_our_user_agent_only_is_obeyed(tmp_path: Path) -> None:
    clock = FakeClock()
    robots = b"User-agent: health-data-registry\nDisallow: /a/\n\nUser-agent: *\nAllow: /\n"
    site = FakeSite(clock, {f"https://{HOST}/a/": b"a"}, robots=robots)
    with client(tmp_path, site, clock) as c, pytest.raises(CrawlStopped) as e:
        c.get(f"https://{HOST}/a/")
    assert e.value.code == "robots_disallow"


def test_another_host_is_refused_with_its_code(tmp_path: Path) -> None:
    clock = FakeClock()
    with client(tmp_path, FakeSite(clock), clock) as c, pytest.raises(CrawlStopped) as e:
        c.get("https://elsewhere.example.com/a/")
    assert e.value.code == "other_host"
    assert str(e.value) == f"https://elsewhere.example.com/a/ is not on {HOST}; this client reads one site"


def test_a_small_default_delay_is_used_before_robots_is_read(tmp_path: Path) -> None:
    clock = FakeClock()
    with client(tmp_path, FakeSite(clock), clock, default_delay=0.5) as c:
        assert c._pace() == 0.5


@pytest.mark.parametrize("delay", [0.0, -1.0, float("nan")])
def test_a_default_delay_that_would_hammer_the_site_is_refused(tmp_path: Path, delay: float) -> None:
    clock = FakeClock()
    with pytest.raises(ValueError, match="default_delay"):
        client(tmp_path, FakeSite(clock), clock, default_delay=delay)
    with client(tmp_path, FakeSite(clock), clock, default_delay=0.5):  # no lock left behind
        pass


def test_robots_refusing_to_show_stops_the_crawl(tmp_path: Path) -> None:
    clock = FakeClock()

    def forbidden(url: str) -> FetchResult:
        raise FetchRefused("http_error", "The publisher answered with status 403.", status=403)

    c = PoliteClient(f"https://{HOST}", state_path=tmp_path / "s.json", get_raw=forbidden, clock=clock)
    with c, pytest.raises(CrawlStopped) as e:
        c.get(f"https://{HOST}/a/")
    assert e.value.code == "robots_unreadable"
    assert c.requests_made == 1  # robots.txt only: a robots.txt the host refuses to show means stop
