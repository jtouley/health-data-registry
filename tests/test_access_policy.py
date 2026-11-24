"""The maintainer's access policy: robots.txt on government hosts, a 10 s pace everywhere, browser hosts.

Every branch of crawl.Robots and crawl.fetcher that the policy adds is tested here with fakes; nothing is fetched.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import crawl, geo
from health_data_registry import fetch as fetch_mod
from health_data_registry.browser_fetch import BrowserResult
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import FetchLog, read_fetch_log
from tests.test_robots import Clock, answer, fake

GOV = "dash.example.gov"
OTHER = "data.example.org"
HOSTS = frozenset({GOV, OTHER})
DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


def robots(
    monkeypatch: pytest.MonkeyPatch, rules: dict[str, Any], clock: Clock | None = None
) -> crawl.Robots:
    fake(monkeypatch, rules)
    c = clock or Clock()
    return crawl.Robots(HOSTS, government_hosts=frozenset({GOV}), sleep=c.sleep, clock=c)


def test_government_host_is_read_although_robots_disallows(monkeypatch: pytest.MonkeyPatch) -> None:
    rb = robots(monkeypatch, {GOV: DISALLOW_ALL})
    rb.wait_turn(f"https://{GOV}/views/x")
    assert rb.overridden == [f"https://{GOV}/views/x"]  # recorded, not silent


def test_other_hosts_keep_full_robots_compliance(monkeypatch: pytest.MonkeyPatch) -> None:
    rb = robots(monkeypatch, {OTHER: DISALLOW_ALL})
    with pytest.raises(fetch_mod.FetchRefused) as e:
        rb.wait_turn(f"https://{OTHER}/x")
    assert e.value.code == "robots_disallowed" and rb.overridden == []


def test_government_host_allowed_path_is_not_recorded_as_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    rb = robots(monkeypatch, {GOV: "User-agent: *\nDisallow: /private/\n"})
    rb.wait_turn(f"https://{GOV}/views/x")
    assert rb.overridden == []


def test_the_default_honours_robots_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, {GOV: DISALLOW_ALL})
    with pytest.raises(fetch_mod.FetchRefused):
        crawl.Robots(HOSTS, sleep=lambda s: None).wait_turn(f"https://{GOV}/x")


def test_ten_seconds_between_requests_to_one_host_even_without_robots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    rb = robots(monkeypatch, {GOV: 404, OTHER: 404}, clock)
    rb.wait_turn(f"https://{GOV}/a")
    rb.wait_turn(f"https://{OTHER}/a")  # another host: no wait
    clock.now += 3
    rb.wait_turn(f"https://{GOV}/b")
    assert clock.slept == [7.0] and rb.waited == 0.0  # our own floor is not charged to the budget
    assert crawl.MIN_INTERVAL_S == 10.0


def test_the_pace_holds_when_robots_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    rb = robots(monkeypatch, {GOV: fetch_mod.FetchRefused("timeout", "slow")}, clock)
    rb.wait_turn(f"https://{GOV}/a")
    rb.wait_turn(f"https://{GOV}/b")
    assert clock.slept == [10.0]


def test_a_longer_crawl_delay_wins_and_only_its_excess_is_charged(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    rb = robots(monkeypatch, {GOV: "User-agent: *\nCrawl-delay: 30\nDisallow: /\n"}, clock)
    rb.wait_turn(f"https://{GOV}/a")
    rb.wait_turn(f"https://{GOV}/b")
    assert clock.slept == [30.0] and rb.waited == 20.0


def test_a_shorter_crawl_delay_does_not_lower_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    rb = robots(monkeypatch, {GOV: "User-agent: *\nCrawl-delay: 2\n"}, clock)
    rb.wait_turn(f"https://{GOV}/a")
    rb.wait_turn(f"https://{GOV}/b")
    assert clock.slept == [10.0]


@pytest.mark.parametrize(("status", "allowed"), [(404, True), (410, True), (401, False), (403, False)])
def test_robots_status_raised_by_the_fetcher(
    monkeypatch: pytest.MonkeyPatch, status: int, allowed: bool
) -> None:
    # fetch.py raises on a non-200 answer; the status still decides what robots.txt means.
    rb = robots(monkeypatch, {OTHER: fetch_mod.FetchRefused("http_error", "x", status=status)})
    if allowed:
        rb.wait_turn(f"https://{OTHER}/x")
    else:
        with pytest.raises(fetch_mod.FetchRefused):
            rb.wait_turn(f"https://{OTHER}/x")
    assert OTHER in rb.rules


def test_robots_server_error_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    rb = robots(monkeypatch, {OTHER: fetch_mod.FetchRefused("http_error", "x", status=503)})
    rb.wait_turn(f"https://{OTHER}/x")
    assert OTHER not in rb.rules


# --------------------------------------------------------------------------------------------- fetcher


def write_registry(
    root: Path, *, portal: str = "tableau", browser: bool = True, government: bool = True
) -> Registry:
    from tests.test_hub import write_hub_registry

    write_hub_registry(root)
    tableau = (
        '\n  tableau: {views: [{view: TB, worksheets: {TByear: "' + "a" * 64 + '"}}]}'
        if portal == "tableau"
        else ""
    )
    (root / "catalog" / "toy_dash.yaml").write_text(
        f"""id: toy_dash
title: Toy dashboard
agency: City health department
publisher: City health department
level: city
research_areas: [infectious_disease]
years: {{start: 2019}}
geography: [city]
shape: rate_table
grain: One row per place and year.
refresh: annual
licence: {{id: no-terms-stated, redistribution: false, terms_url: "https://{GOV}/"}}
citation: City.
source:
  portal: {portal}
  host: {GOV}
  dataset: Toy
  landing_url: https://{GOV}/views/Toy/Main
  api_url: https://{GOV}/views/Toy
  government_host: {str(government).lower()}
  browser: {str(browser).lower()}{tableau}
codebook_url: https://{GOV}/
contract: contracts/toy_cases.odcs.yaml
semantic_model: semantic/toy_cases.yaml
status: draft
""",
        encoding="utf-8",
    )
    sem = (root / "semantic" / "toy_cases.yaml").read_text(encoding="utf-8")
    (root / "semantic" / "toy_dash.yaml").write_text(sem.replace("dataset: toy_cases", "dataset: toy_dash"))
    con = (root / "contracts" / "toy_cases.odcs.yaml").read_text(encoding="utf-8")
    (root / "contracts" / "toy_dash.odcs.yaml").write_text(con.replace("toy_cases", "toy_dash"))
    text = (root / "catalog" / "toy_dash.yaml").read_text().replace("toy_cases", "toy_dash")
    (root / "catalog" / "toy_dash.yaml").write_text(text)
    return load_registry(root)


def result(url: str, body: bytes, status: int = 200) -> fetch_mod.FetchResult:
    return fetch_mod.FetchResult(url=url, final_url=url, status=status, content=body, content_type=None,
                                 sha256=hashlib.sha256(body).hexdigest(),
                                 retrieved_at=datetime(2026, 11, 24, tzinfo=UTC))  # fmt: skip


class FakeBrowser:
    def __init__(self, captured: list[fetch_mod.FetchResult], error: Exception | None = None) -> None:
        self.captured, self.error, self.calls = captured, error, []  # type: ignore[var-annotated]

    def __call__(self, url: str, **kw: Any) -> BrowserResult:
        self.calls.append((url, kw))
        if self.error:
            raise self.error
        return BrowserResult(page=result(url, b"<html>"), captured=tuple(self.captured))


VIEW = f"https://{GOV}/views/Toy/TB?:embed=y"


def test_tableau_view_returns_its_bootstrap_session(tmp_path: Path) -> None:
    reg = write_registry(tmp_path / "reg")
    data = result(f"https://{GOV}/vizql/bootstrapSession/sessions/1", b"2;{}")
    browser = FakeBrowser([result(data.url, b"", 500), data])
    get = crawl.fetcher(
        reg, FetchLog(tmp_path, "toy_dash"), sleep=lambda s: None, robots=False, browser=browser
    )
    got = get(VIEW)
    assert got.content == b"2;{}" and got.url == data.url
    url, kw = browser.calls[0]
    assert url == VIEW and kw["capture"] == [crawl.TABLEAU_DATA] and kw["allowed_hosts"] == reg.hosts
    assert read_fetch_log(tmp_path, "toy_dash")[0]["status"] == "ok"


def test_tableau_view_without_data_is_refused(tmp_path: Path) -> None:
    reg = write_registry(tmp_path / "reg")
    get = crawl.fetcher(reg, None, sleep=lambda s: None, robots=False, browser=FakeBrowser([]))
    with pytest.raises(fetch_mod.FetchRefused) as e:
        get(VIEW)
    assert e.value.code == "no_data"


def test_a_browser_host_that_is_not_tableau_returns_the_page(tmp_path: Path) -> None:
    reg = write_registry(tmp_path / "reg", portal="https_csv")
    browser = FakeBrowser([])
    got = crawl.fetcher(reg, None, sleep=lambda s: None, robots=False, browser=browser)(VIEW)
    assert got.content == b"<html>" and browser.calls[0][1]["capture"] == []


def test_a_block_is_logged_and_never_retried(tmp_path: Path) -> None:
    reg = write_registry(tmp_path / "reg")
    browser = FakeBrowser([], fetch_mod.FetchRefused("blocked", "blocked by the edge", status=403))
    sleeps: list[float] = []
    get = crawl.fetcher(
        reg, FetchLog(tmp_path, "toy_dash"), sleep=sleeps.append, robots=False, browser=browser
    )
    with pytest.raises(fetch_mod.FetchRefused):
        get(VIEW)
    assert len(browser.calls) == 1 and sleeps == []
    (row,) = read_fetch_log(tmp_path, "toy_dash")
    assert row["status"] == "error" and row["http_status"] == 403 and "blocked" in row["error"]


def test_a_slow_browser_host_is_retried_at_the_pace(tmp_path: Path) -> None:
    reg = write_registry(tmp_path / "reg")
    browser = FakeBrowser([], fetch_mod.FetchRefused("timeout", "slow"))
    sleeps: list[float] = []
    get = crawl.fetcher(reg, None, sleep=sleeps.append, robots=False, browser=browser)
    with pytest.raises(fetch_mod.FetchRefused):
        get(VIEW)
    assert sleeps == [10, 20, 40]


def test_plain_hosts_still_use_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = write_registry(tmp_path / "reg", browser=False)
    monkeypatch.setattr(fetch_mod, "fetch", lambda url, **kw: answer(url, 200, b"plain"))
    got = crawl.fetcher(reg, None, sleep=lambda s: None, robots=False)(VIEW)
    assert got.content == b"plain"


def test_the_default_fetcher_applies_the_government_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = write_registry(tmp_path / "reg", browser=False)
    seen = fake(monkeypatch, {GOV: DISALLOW_ALL, "data.cdc.gov": DISALLOW_ALL})
    get = crawl.fetcher(reg, None, sleep=lambda s: None)
    get(VIEW)  # government host: fetched although disallowed
    with pytest.raises(fetch_mod.FetchRefused):
        get("https://data.cdc.gov/resource/x.json")  # not flagged: robots.txt honoured
    assert VIEW in seen


def test_not_flagged_government_host_keeps_robots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = write_registry(tmp_path / "reg", browser=False, government=False)
    fake(monkeypatch, {GOV: DISALLOW_ALL})
    with pytest.raises(fetch_mod.FetchRefused):
        crawl.fetcher(reg, None, sleep=lambda s: None)(VIEW)


# --------------------------------------------------------------------------------------------- places


def test_michigan_counties_and_detroit_resolve() -> None:
    counties = [p for p in geo.LOCAL_PLACES if p.level == "county"]
    assert len(counties) == 83 and len({p.fips for p in counties}) == 83
    assert [p.fips for p in counties] == [f"26{n:03d}" for n in range(1, 166, 2)]
    wayne = geo.local_place("Wayne County")
    assert wayne is not None and wayne.fips == "26163" and wayne.gbd is None and wayne.state == "MI"
    for label in ("Wayne", "WAYNE COUNTY", "Wayne Co.", "Wayne County, MI", "26163"):
        assert geo.local_place(label) == wayne
    for label in ("St. Joseph", "Saint Joseph County", "St Joseph", "st. clair county"):
        p = geo.local_place(label)
        assert p is not None and p.name.startswith("St. ")
    detroit = geo.local_place("City of Detroit")
    assert (
        detroit is not None and detroit.level == "city" and detroit.fips == "2622000" and detroit.gbd is None
    )
    assert geo.local_place("Detroit") == geo.local_place("detroit city") == detroit


def test_state_and_nation_keep_their_gbd_names_and_counties_never_key_to_gbd() -> None:
    mi = geo.local_place("Michigan")
    assert mi is not None and mi.gbd == "Michigan" and mi.level == "state"
    assert geo.place("United States").gbd == "United States"  # type: ignore[union-attr]
    assert geo.place("Detroit") is None and geo.place("Wayne County") is None  # only GBD locations are keyed
    assert geo.local_place("Wayne County (excluding Detroit)") is None  # part of a county is not the county
    assert geo.local_place("Detroit Case Rate") is None  # a legend label, not a place name
