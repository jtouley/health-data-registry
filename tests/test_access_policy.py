"""The access policy on robots.txt and data pages (owner decision, 25 Nov 2026) and the rate-limit refusal.

A 401, 403 or 429 on robots.txt blocks a host for the run, except on a government host (registry's reviewed list),
where the crawl goes on at 10 s or more between requests and the override is recorded in the fetch log and the
manifest. The same statuses on a data page block the host on every host, government or not.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import crawl
from health_data_registry import fetch as fetch_mod
from health_data_registry.registry import is_government_host, load_registry
from health_data_registry.store import FetchLog, read_fetch_log
from tests.conftest import CATALOG, write_registry

GOV, OTHER = "data.cdc.gov", "api.example.org"
HOSTS = frozenset({GOV, OTHER})
WHEN = datetime(2026, 11, 25, tzinfo=UTC)


def answer(url: str, body: bytes = b"[]") -> fetch_mod.FetchResult:
    return fetch_mod.FetchResult(
        url=url, final_url=url, status=200, content=body, content_type="application/json",
        sha256=hashlib.sha256(body).hexdigest(), retrieved_at=WHEN,
    )  # fmt: skip


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s

    def __call__(self) -> float:
        return self.now


def fake(
    monkeypatch: pytest.MonkeyPatch, robots: dict[str, int], pages: dict[str, Any] | None = None
) -> list[str]:
    seen: list[str] = []

    def f(url: str, **kw: Any) -> fetch_mod.FetchResult:
        seen.append(url)
        host = url.split("/")[2]
        if url.endswith("/robots.txt"):
            raise fetch_mod.FetchRefused("http_error", "no", status=robots.get(host, 404))
        page = (pages or {}).get(host)
        if isinstance(page, Exception):
            raise page
        return answer(url)

    monkeypatch.setattr(fetch_mod, "fetch", f)
    return seen


@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_refused_robots_txt_blocks_a_host_that_is_not_a_government_host(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    seen = fake(monkeypatch, {OTHER: status})
    rb = crawl.Robots(HOSTS, government_hosts=frozenset({GOV}))
    with pytest.raises(fetch_mod.FetchRefused) as e:
        rb.wait_turn(f"https://{OTHER}/a")
    assert e.value.code == "blocked" and e.value.status == status
    assert f"(status {status})" in e.value.message and "not worked around" in e.value.message
    assert rb.blocked == {OTHER} and rb.overridden == []
    with pytest.raises(fetch_mod.FetchRefused) as again:
        rb.wait_turn(f"https://{OTHER}/b")
    assert again.value.code == "blocked" and "earlier in this run" in again.value.message
    assert seen == [f"https://{OTHER}/robots.txt"]  # never asked again


@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_refused_robots_txt_on_a_government_host_is_overridden_and_paced(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    seen = fake(monkeypatch, {GOV: status})
    clock = Clock()
    rb = crawl.Robots(HOSTS, government_hosts=frozenset({GOV}), sleep=clock.sleep, clock=clock, budget_s=1)
    for _ in range(3):
        rb.wait_turn(f"https://{GOV}/resource/x.json")
    assert rb.blocked == set()
    assert rb.overridden == [
        f"robots_overridden (robots.txt {status}): {GOV} refused its robots.txt; read under the government-host "
        "policy at 10 s or more between requests"
    ]
    assert clock.slept == [10.0, 10.0]  # at least 10 s apart; our own floor, not a crawl delay
    assert seen.count(f"https://{GOV}/robots.txt") == 1


def test_the_override_is_only_for_hosts_on_the_reviewed_list(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, {OTHER: 403})
    rb = crawl.Robots(HOSTS, government_hosts=frozenset({OTHER}))  # not a government host: ignored
    assert rb.government == frozenset()
    with pytest.raises(fetch_mod.FetchRefused):
        rb.wait_turn(f"https://{OTHER}/a")
    assert is_government_host("data.cdc.gov") and is_government_host("www.waynecounty.com")
    assert is_government_host("Michigan.gov.") and not is_government_host("cdc.gov.example.org")
    assert not is_government_host("api.crossref.org")


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("host", [GOV, OTHER])
def test_a_refused_data_page_blocks_every_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, host: str, status: int
) -> None:
    reg = load_registry(write_registry(tmp_path / "reg"))
    fake(monkeypatch, {}, {host: fetch_mod.FetchRefused("http_error", "no", status=status)})
    rb = crawl.Robots(HOSTS, government_hosts=frozenset({GOV}))
    slept: list[float] = []
    get = crawl.fetcher(reg, FetchLog(tmp_path, "toy_cases"), robots=rb, sleep=slept.append, hosts=HOSTS)
    with pytest.raises(fetch_mod.FetchRefused) as e:
        get(f"https://{host}/a")
    assert e.value.code == "blocked" and e.value.status == status and slept == []  # never retried
    assert host in rb.blocked
    with pytest.raises(fetch_mod.FetchRefused, match="earlier in this run"):
        get(f"https://{host}/b")
    log = read_fetch_log(tmp_path, "toy_cases")
    assert [r["status"] for r in log] == ["error", "error"]


def test_a_refresh_records_the_override_in_the_fetch_log_and_the_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cat = CATALOG.replace("  host: data.cdc.gov\n", "  host: data.cdc.gov\n  government_host: true\n")
    reg = load_registry(write_registry(tmp_path / "reg", catalog=cat))
    assert crawl.government_hosts(reg) == {GOV}
    fake(monkeypatch, {GOV: 403})
    lake = tmp_path / "lake"
    get = crawl.fetcher(reg, FetchLog(lake, "toy_cases"), sleep=lambda s: None)
    m = crawl.refresh_dataset(reg.get("toy_cases"), lake, get)
    assert m.notes == get.overridden and m.notes[0].startswith(
        "robots_overridden (robots.txt 403): data.cdc.gov"
    )
    log = read_fetch_log(lake, "toy_cases")
    assert log[0]["url"] == "https://data.cdc.gov/robots.txt" and log[0]["error"].startswith(
        "robots_overridden"
    )
    assert log[1]["status"] == "ok"
    # A getter without a policy records nothing.
    plain = crawl.fetcher(reg, None, robots=False)
    assert plain.robots is None and plain.overridden == []


def test_government_host_is_accepted_only_on_the_reviewed_list(tmp_path: Path) -> None:
    from health_data_registry.registry import RegistryError

    cat = CATALOG.replace("host: data.cdc.gov", "host: data.example.org\n  government_host: true")
    with pytest.raises((RegistryError, ValueError), match="reviewed list of government"):
        load_registry(write_registry(tmp_path / "reg", catalog=cat))


def test_a_busy_publisher_is_waited_out_or_refused_in_plain_words(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reg = load_registry(write_registry(tmp_path / "reg"))
    calls: list[str] = []
    answers: list[Any] = []

    def f(url: str, **kw: Any) -> fetch_mod.FetchResult:
        calls.append(url)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return answer(url)

    monkeypatch.setattr(fetch_mod, "fetch", f)
    slept: list[float] = []
    get = crawl.fetcher(reg, None, robots=False, sleep=slept.append)
    answers[:] = [fetch_mod.FetchRefused("http_error", "busy", status=503, retry_after=7), "ok"]
    get("https://data.cdc.gov/a")
    assert slept == [7]
    answers[:] = [fetch_mod.FetchRefused("http_error", "busy", status=503), "ok"]
    get("https://data.cdc.gov/a")
    assert slept == [7, 2]  # no Retry-After: a doubling back-off
    answers[:] = [fetch_mod.FetchRefused("http_error", "busy", status=503, retry_after=600)]
    with pytest.raises(fetch_mod.FetchRefused) as e:
        get("https://data.cdc.gov/a")
    assert e.value.code == "rate_limited" and e.value.retry_after == 600 and e.value.status == 503
    assert e.value.message == (
        "Centers for Disease Control and Prevention is busy and asked us to wait about 10 minutes before asking "
        "again, so nothing was fetched. Ask again after that, or use the source's own page in the meantime: "
        "https://data.cdc.gov/d/toy0-toy0."
    )
    tight = crawl.fetcher(reg, None, robots=False, sleep=slept.append, max_retry_after_s=30)
    answers[:] = [fetch_mod.FetchRefused("http_error", "busy", status=503, retry_after=60)]
    with pytest.raises(fetch_mod.FetchRefused) as e2:
        tight("https://api.crossref.org/works")
    assert e2.value.message == (
        "api.crossref.org is busy and asked us to wait 60 seconds before asking again, so nothing was fetched. "
        "Ask again after that, or use the source's own page in the meantime."
    )


def test_retry_after_is_read_as_seconds_or_a_date() -> None:
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    assert fetch_mod.retry_after_seconds(None) is None
    assert fetch_mod.retry_after_seconds(" 120 ") == 120.0
    assert fetch_mod.retry_after_seconds("Wed, 25 Nov 2026 12:01:00 GMT", now=now) == 60.0
    assert fetch_mod.retry_after_seconds("Wed, 25 Nov 2026 11:00:00 GMT", now=now) == 0.0
    assert fetch_mod.retry_after_seconds("soon") is None
    assert fetch_mod.retry_after_seconds("²") is None  # a digit to isdigit(), not to float()
    assert fetch_mod.retry_after_seconds("٣") is None
    assert fetch_mod.retry_after_seconds("Wed, 25 Nov 2026 12:01:00") is None  # no time zone
