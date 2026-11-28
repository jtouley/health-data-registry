"""The maintainer's access policy: robots.txt on government hosts, a 10 s pace everywhere, browser hosts.

Every branch of crawl.Robots and crawl.fetcher that the policy adds is tested here with fakes; nothing is fetched.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import crawl
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
    rb.wait_turn(f"https://{GOV}/a")  # robots.txt was a request: the page waits its 10 s after it
    rb.wait_turn(f"https://{OTHER}/a")  # a host the policy doesn't target: no floor, no wait
    clock.now += 3
    rb.wait_turn(f"https://{GOV}/b")
    assert clock.slept == [10.0, 7.0] and rb.waited == 0.0  # our own floor is not charged to the budget
    assert crawl.MIN_INTERVAL_S == 10.0


def test_the_floor_applies_only_to_the_hosts_the_policy_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, {})
    clock = Clock()
    browser_host = "maps.example.org"
    rb = crawl.Robots(HOSTS | {browser_host}, government_hosts=frozenset({GOV}),
                      paced_hosts=frozenset({browser_host}), sleep=clock.sleep, clock=clock)  # fmt: skip
    for _ in range(3):
        rb.wait_turn(f"https://{OTHER}/a")
    assert clock.slept == []  # a plain open-data host: only its own crawl delay, if any
    rb.wait_turn(f"https://{browser_host}/a")
    rb.wait_turn(f"https://{browser_host}/b")
    assert clock.slept == [10.0, 10.0] and rb.paced == {GOV, browser_host}


def test_a_government_flag_off_the_reviewed_list_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, {OTHER: DISALLOW_ALL})
    rb = crawl.Robots(HOSTS, government_hosts=frozenset({OTHER}), sleep=lambda s: None)
    assert rb.government == frozenset()
    with pytest.raises(fetch_mod.FetchRefused) as e:
        rb.wait_turn(f"https://{OTHER}/x")
    assert e.value.code == "robots_disallowed"


def test_the_pace_holds_when_robots_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    rb = robots(monkeypatch, {GOV: fetch_mod.FetchRefused("timeout", "slow")}, clock)
    rb.wait_turn(f"https://{GOV}/a")
    rb.wait_turn(f"https://{GOV}/b")
    assert clock.slept == [10.0, 10.0]


def test_a_longer_crawl_delay_wins_and_only_its_excess_is_charged(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    rb = robots(monkeypatch, {GOV: "User-agent: *\nCrawl-delay: 30\nDisallow: /\n"}, clock)
    rb.wait_turn(f"https://{GOV}/a")
    rb.wait_turn(f"https://{GOV}/b")
    assert clock.slept == [30.0, 30.0] and rb.waited == 40.0


def test_a_shorter_crawl_delay_does_not_lower_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    rb = robots(monkeypatch, {GOV: "User-agent: *\nCrawl-delay: 2\n"}, clock)
    rb.wait_turn(f"https://{GOV}/a")
    rb.wait_turn(f"https://{GOV}/b")
    assert clock.slept == [10.0, 10.0]


@pytest.mark.parametrize(("status", "allowed"), [(404, True), (410, True)])
def test_robots_status_raised_by_the_fetcher(
    monkeypatch: pytest.MonkeyPatch, status: int, allowed: bool
) -> None:
    # fetch.py raises on a non-200 answer; the status still decides what robots.txt means.
    rb = robots(monkeypatch, {OTHER: fetch_mod.FetchRefused("http_error", "x", status=status)})
    rb.wait_turn(f"https://{OTHER}/x")
    assert OTHER in rb.rules


@pytest.mark.parametrize("host", [OTHER])
@pytest.mark.parametrize("status", [401, 403])  # a 429 is backed off first (tests/test_robots_owner_rule.py)
def test_a_block_on_robots_txt_is_final_on_a_host_that_is_not_a_government_host(
    monkeypatch: pytest.MonkeyPatch, host: str, status: int
) -> None:
    # On a government host the owner's rule of 25 Nov 2026 overrides it instead (tests/test_robots_owner_rule.py).
    seen = fake(monkeypatch, {host: fetch_mod.FetchRefused("http_error", "x", status=status)})
    rb = crawl.Robots(HOSTS, government_hosts=frozenset({GOV}), sleep=lambda s: None)
    with pytest.raises(fetch_mod.FetchRefused) as e:
        rb.wait_turn(f"https://{host}/x")
    assert e.value.code == "blocked" and e.value.status == status and "not worked around" in e.value.message
    assert host in rb.blocked and rb.overridden == []
    with pytest.raises(fetch_mod.FetchRefused, match="earlier in this run"):
        rb.wait_turn(f"https://{host}/y")  # remembered: robots.txt is not asked again, nor the page
    assert not rb.allowed(f"https://{host}/z")
    assert seen == [f"https://{host}/robots.txt"]


def test_allowed_applies_robots_to_the_browsers_own_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, {OTHER: "User-agent: *\nDisallow: /vizql/\n", GOV: DISALLOW_ALL})
    rb = robots(monkeypatch, {OTHER: "User-agent: *\nDisallow: /vizql/\n", GOV: DISALLOW_ALL})
    assert rb.allowed(f"https://{OTHER}/views/x") and not rb.allowed(f"https://{OTHER}/vizql/boot")
    assert rb.allowed(f"https://{GOV}/vizql/boot")  # a government host: the policy reads it anyway
    rb2 = robots(monkeypatch, {OTHER: fetch_mod.FetchRefused("timeout", "slow")})
    assert rb2.allowed(f"https://{OTHER}/vizql/boot")  # unreadable robots.txt: nothing assumed


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
    # Hard rule 8: the source URL is the catalogued view, never the one-time session URL; that is kept beside it.
    assert got.content == b"2;{}" and got.url == VIEW and got.capture_url == data.url
    url, kw = browser.calls[0]
    # every hop stays on this source's own hosts, never another catalogued source's
    assert url == VIEW and kw["capture"] == [crawl.TABLEAU_DATA] and kw["allowed_hosts"] == frozenset({GOV})
    assert kw["allow"] is None  # robots=False: no policy to apply to the page's own requests
    (row,) = read_fetch_log(tmp_path, "toy_dash")
    assert row["status"] == "ok" and row["url"] == VIEW and row["capture_url"] == data.url


def test_a_manifest_from_a_tableau_source_cites_the_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from health_data_registry import adapters
    from health_data_registry.live import fetch_live

    reg = write_registry(tmp_path / "reg")
    data = result(f"https://{GOV}/vizql/bootstrapSession/sessions/1", b"2;{}")
    get = crawl.fetcher(reg, None, sleep=lambda s: None, robots=False, browser=FakeBrowser([data]))

    def one_page(entry: Any, fetcher: Any, **kw: Any) -> Any:
        yield adapters.Page(fetch=fetcher(VIEW), records=[])

    monkeypatch.setattr(adapters, "pages", one_page)
    assert fetch_live(reg.get("toy_dash"), get).manifest.source_urls == [VIEW]


def test_a_dashboard_read_is_kept_in_memory_for_a_stated_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from health_data_registry import adapters, live

    reg = write_registry(tmp_path / "reg")
    data = result(f"https://{GOV}/vizql/bootstrapSession/sessions/1", b"2;{}")
    get = crawl.fetcher(reg, None, sleep=lambda s: None, robots=False, browser=FakeBrowser([data]))

    def one_page(entry: Any, fetcher: Any, **kw: Any) -> Any:
        yield adapters.Page(fetch=fetcher(VIEW), records=[])

    monkeypatch.setattr(adapters, "pages", one_page)
    table = live.fetch_live(reg.get("toy_dash"), get)
    assert table.dashboard and live.ttl_s(reg.get("toy_dash")) == live.DASHBOARD_TTL_S == 3600.0
    note = table.manifest.notes[1]
    assert note.startswith(
        "Read live from the publisher at 2026-11-24 00:00 UTC; a dashboard takes a few minutes"
    )
    assert "never on disk) for up to 60 minutes" in note
    assert table.fresh(table.fetched + 3599) and not table.fresh(table.fetched + 3601)
    plain = live.LiveTable(records=[], manifest=table.manifest, fetched=0.0)
    assert plain.fresh(599) and not plain.fresh(600)  # other live sources keep the 10-minute limit
    assert "several lookups read the publisher once" in live.kept_note(
        reg.get("toy_cases"), "2026-11-24T00:00"
    )
    assert "up to 10 minutes" in live.kept_note(reg.get("toy_cases"), "2026-11-24T00:00")


def test_the_browser_gets_the_policys_robots_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = write_registry(tmp_path / "reg")
    fake(monkeypatch, {})
    policy = crawl.policy_for(reg, sleep=lambda s: None)
    data = result(f"https://{GOV}/vizql/bootstrapSession/sessions/1", b"2;{}")
    browser = FakeBrowser([data])
    crawl.fetcher(reg, None, sleep=lambda s: None, robots=policy, browser=browser)(VIEW)
    assert browser.calls[0][1]["allow"] == policy.allowed


@pytest.mark.parametrize("status", [401, 403])  # a 429 is backed off first (tests/test_robots_owner_rule.py)
def test_a_plain_http_error_403_is_a_block_and_is_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    reg = write_registry(tmp_path / "reg", browser=False)
    calls: list[str] = []

    def refuse(url: str, **kw: Any) -> Any:
        calls.append(url)
        raise fetch_mod.FetchRefused("http_error", "x", status=status)

    monkeypatch.setattr(fetch_mod, "fetch", refuse)
    sleeps: list[float] = []
    get = crawl.fetcher(reg, FetchLog(tmp_path, "toy_dash"), sleep=sleeps.append, robots=False)
    with pytest.raises(fetch_mod.FetchRefused) as e:
        get(VIEW)
    assert e.value.code == "blocked" and e.value.status == status and sleeps == []
    with pytest.raises(fetch_mod.FetchRefused, match="earlier in this run"):
        get(f"https://{GOV}/views/Toy/Other")  # another view on the blocked host: not asked
    assert calls == [VIEW]
    rows = read_fetch_log(tmp_path, "toy_dash")
    assert [r["status"] for r in rows] == ["error", "error"] and rows[0]["http_status"] == status
    assert "nothing more is fetched" in rows[0]["error"] and "not asked again" in rows[1]["error"]


def test_a_block_is_shared_through_the_policy(tmp_path: Path) -> None:
    reg = write_registry(tmp_path / "reg")
    policy = crawl.Robots(reg.hosts, sleep=lambda s: None)
    policy.rules[GOV] = __import__("urllib.robotparser").robotparser.RobotFileParser()
    policy.rules[GOV].parse([])
    browser = FakeBrowser([], fetch_mod.FetchRefused("blocked", "edge", status=403))
    with pytest.raises(fetch_mod.FetchRefused):
        crawl.fetcher(reg, None, robots=policy, browser=browser)(VIEW)
    assert GOV in policy.blocked
    with pytest.raises(fetch_mod.FetchRefused, match="earlier in this run"):
        crawl.fetcher(reg, None, robots=policy, browser=browser)(VIEW)  # a new getter, the same policy
    assert len(browser.calls) == 1


def _source(host: str, government: bool) -> dict[str, Any]:
    return {"portal": "https_csv", "host": host, "dataset": "x", "landing_url": f"https://{host}/",
            "api_url": f"https://{host}/x.csv", "government_host": government}  # fmt: skip


@pytest.mark.parametrize(
    "host",
    ["dash.example.gov", "codtableau.detroitmi.gov", "mdhhstableau-public.michigan.gov", "www.co.wayne.mi.us",
     "data.waynecounty.com", "oakgov.com", "www.macombgov.org", "health.army.mil"],
)  # fmt: skip
def test_government_host_is_accepted_for_reviewed_suffixes_and_domains(host: str) -> None:
    from health_data_registry.registry import Source, is_government_host

    assert is_government_host(host) and is_government_host(host.upper() + ".")
    assert Source.model_validate(_source(host, True)).government_host is True


@pytest.mark.parametrize(
    "host",
    ["data.example.org", "public.tableau.com", "evilgov.com", "michigan.gov.example.com", "notoakgov.com"],
)
def test_government_host_is_refused_for_any_other_host(host: str) -> None:
    from pydantic import ValidationError

    from health_data_registry.registry import Source, is_government_host

    assert not is_government_host(host)
    with pytest.raises(ValidationError, match="reviewed list of government suffixes"):
        Source.model_validate(_source(host, True))
    assert Source.model_validate(_source(host, False)).government_host is False


def test_entries_on_one_host_must_agree_on_the_government_flag(tmp_path: Path) -> None:
    from dataclasses import replace

    from health_data_registry.registry import RegistryError, _check_hosts_agree

    ds = write_registry(tmp_path / "reg").get("toy_dash")
    src = ds.entry.source.model_copy(update={"government_host": False})
    other = replace(ds, entry=ds.entry.model_copy(update={"id": "toy_dash2", "source": src}))
    _check_hosts_agree([ds, ds])
    with pytest.raises(
        RegistryError, match="toy_dash2 and toy_dash are both on dash.example.gov but disagree"
    ):
        _check_hosts_agree([ds, other])


def test_shared_robots_is_one_policy_per_process(tmp_path: Path) -> None:
    reg = write_registry(tmp_path / "reg")
    assert crawl.shared_robots(reg) is crawl.shared_robots(reg)
    assert crawl.shared_robots(reg).paced == {GOV}


def test_browser_and_portal_are_decided_per_dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = write_registry(tmp_path / "reg")  # toy_dash on GOV is a browser source under /views/Toy
    monkeypatch.setattr(fetch_mod, "fetch", lambda url, **kw: answer(url, 200, b"plain"))
    browser = FakeBrowser([result(f"https://{GOV}/vizql/bootstrapSession/sessions/1", b"2;{}")])
    get = crawl.fetcher(reg, None, sleep=lambda s: None, robots=False, browser=browser)
    assert get(f"https://{GOV}/other/file.csv").content == b"plain"  # not under a browser entry's URL
    assert get(f"https://{GOV}/views/Toy2/x").content == b"plain"  # a different workbook, not a prefix match
    assert browser.calls == []
    assert get(VIEW).content == b"2;{}" and len(browser.calls) == 1


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
    get = crawl.fetcher(reg, FetchLog(tmp_path, "toy_dash"), sleep=lambda s: None)
    get(VIEW)  # government host: fetched although disallowed
    with pytest.raises(fetch_mod.FetchRefused):
        get("https://data.cdc.gov/resource/x.json")  # not flagged: robots.txt honoured
    assert VIEW in seen
    # The override is kept with the data: in the fetch log and, through the getter, in the manifest's notes.
    assert get.overridden == [VIEW]
    ok = [r for r in read_fetch_log(tmp_path, "toy_dash") if r["status"] == "ok"]
    assert ok[0]["url"] == VIEW and ok[0]["note"].startswith("robots_overridden")
    (note,) = crawl.override_notes(get)
    assert VIEW in note and "government-host policy" in note
    assert crawl.override_notes(lambda u: None) == []  # type: ignore[arg-type,return-value]


def test_not_flagged_government_host_keeps_robots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = write_registry(tmp_path / "reg", browser=False, government=False)
    fake(monkeypatch, {GOV: DISALLOW_ALL})
    with pytest.raises(fetch_mod.FetchRefused):
        crawl.fetcher(reg, None, sleep=lambda s: None)(VIEW)
