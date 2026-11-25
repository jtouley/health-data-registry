"""The shared fetch core: scope, links, provenance, pace (only slower), robots.txt lifetime and Request-rate, live
clamps, the 8-week stop rule, and the catalog schema's strictness and caps (ADR 0016; #118's sharp-edges review)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from health_data_registry import crawl, deadline
from health_data_registry.access import (
    Dormant,
    FailureStreaks,
    HostPace,
    ScopeRefused,
    clamp_live,
    in_scope,
    normalise,
    pace_s,
    redact_url,
)
from health_data_registry.adapters import method_digest, pages
from health_data_registry.fetch import FetchRefused
from health_data_registry.source_config import ODataConfig, match_whole, safe_pattern
from tests.adapters.helpers import HOST, Site, config_of, entry

# ------------------------------------------------------------------------------------------- links and scope


@pytest.mark.parametrize(
    ("href", "base", "want"),
    [
        ("/a/../b/c.pdf", "https://h.gov/", "https://h.gov/b/c.pdf"),
        ("c.pdf", "https://H.gov/x/y/", "https://h.gov/x/y/c.pdf"),
        ("/x/%2e%2e/secret", "https://h.gov/", "https://h.gov/secret"),
        ("/a/./b/", "https://h.gov/", "https://h.gov/a/b/"),
        ("https://h.gov/a?b=1#frag", None, "https://h.gov/a?b=1"),
        ("//h.gov//a", "https://h.gov/", "https://h.gov/a"),
    ],
)
def test_normalise(href: str, base: str | None, want: str) -> None:
    assert normalise(href, base) == want


@pytest.mark.parametrize(
    "href", ["http://h.gov/a", "https://u:p@h.gov/a", "https://h.gov:8443/a", "mailto:x@h.gov", "https://h.gov/a\\b",
             "https://[::1/a"],
)  # fmt: skip
def test_normalise_refuses(href: str) -> None:
    with pytest.raises(ScopeRefused):
        normalise(href)


def test_a_source_reaches_only_its_own_hosts_and_paths() -> None:
    e = entry("socrata", scope_paths=[r"/files/[a-z]+\.csv"])
    s = e.source
    assert in_scope(s, f"https://{HOST}/toy/data?$limit=5")  # the catalogued URL, with a query
    assert in_scope(s, f"https://{HOST}/toy/data/page2")  # a path under it
    assert in_scope(s, f"https://{HOST}/files/cases.csv")  # a declared scope path, matched whole
    assert in_scope(s, f"https://{HOST}/api/views/x.json", own_paths=["/api/views/x.json"])
    assert not in_scope(s, f"https://{HOST}/api/views/x.json")
    assert not in_scope(s, f"https://{HOST}/files/cases.csv.exe")
    assert not in_scope(s, f"https://{HOST}/toyx/data")  # a whole segment, not a prefix
    assert not in_scope(s, f"https://{HOST}/toy/data/../../files/x/../../etc")
    assert not in_scope(s, "https://another-source.example.gov/toy/data")  # another source's host
    assert not in_scope(s, "not a link")


def test_pages_refuse_an_out_of_scope_url_before_any_request() -> None:
    e = entry("odata")
    nxt = (
        '{"value": [{"Year": "2024", "Value": "1"}], "@odata.nextLink": "https://elsewhere.example.org/next"}'
    )
    site = Site({f"https://{HOST}/toy/data": nxt.encode()})
    with pytest.raises(ScopeRefused, match="outside what the catalog entry"):
        list(pages(e, site))
    assert site.calls == [f"https://{HOST}/toy/data"]


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("https://h.gov/a?$$app_token=SECRET&x=1", "https://h.gov/a?x=1"),
        ("https://h.gov/a?api_key=k&Token=t&y=2", "https://h.gov/a?y=2"),
        ("https://h.gov/a?x=1", "https://h.gov/a?x=1"),
        ("https://h.gov/a", "https://h.gov/a"),
        ("https://h.gov/a?api-key=S&x=1", "https://h.gov/a?x=1"),  # names compared folded
        ("https://h.gov/a?accessToken=S", "https://h.gov/a"),
        ("https://h.gov/f?X-Amz-Signature=S&X-Amz-Credential=C&v=2", "https://h.gov/f?v=2"),  # pre-signed
        ("https://h.gov/f?sig=S&sv=2024", "https://h.gov/f?sv=2024"),
        ("https://u:p@h.gov/a?x=1#frag", "https://h.gov/a?x=1"),  # user name, password and fragment dropped
        ("not a url", "not a url"),
    ],
)
def test_redact_url(url: str, want: str) -> None:
    assert redact_url(url) == want


def test_provenance_never_carries_a_key(tmp_path: Path, registry: Any) -> None:
    from health_data_registry.store import FetchLog, read_fetch_log
    from tests.conftest import raw_fetch

    log = FetchLog(tmp_path, "toy_cases")
    url = "https://data.cdc.gov/resource/toy0-toy0.json?$$app_token=SECRET"

    def fetch(u: str, **kw: Any) -> Any:
        r = raw_fetch(u, [])
        from health_data_registry.fetch import FetchResult

        return FetchResult(url=u, final_url=u, status=200, content=r.content, content_type=None, sha256=r.sha256,
                           retrieved_at=r.retrieved_at)  # fmt: skip

    import health_data_registry.fetch as fetch_mod

    mp = pytest.MonkeyPatch()
    mp.setattr(fetch_mod, "fetch", fetch)
    try:
        got = crawl.fetcher(registry, log, robots=False)(url)
    finally:
        mp.undo()
    assert "SECRET" not in got.url and all(
        "SECRET" not in r["url"] for r in read_fetch_log(tmp_path, "toy_cases")
    )


def test_the_url_fetched_is_the_one_checked() -> None:
    """An encoded slash can't move a ``..`` past the scope check: the check and the request see one string."""
    e = entry("https_file", {"format": "csv"}, api_url=f"https://{HOST}/reports/index.csv",
              landing_url=f"https://{HOST}/reports/")  # fmt: skip
    seen: list[str] = []

    def get(url: str) -> Any:
        seen.append(url)
        raise FetchRefused("http_error", "stop", status=404)

    from health_data_registry.adapters import scoped

    with pytest.raises(FetchRefused):
        scoped(e, get)(f"https://{HOST}/reports/a%2Fx/../../index.csv")  # a server would read /index.csv
    assert seen == [f"https://{HOST}/reports/index.csv"]  # the normalised URL the check approved
    with pytest.raises(ScopeRefused):
        scoped(e, get)(f"https://{HOST}/reports/a%2Fx/../../../admin/secret")


def test_a_bound_getter_fetches_with_its_own_sources_capabilities(tmp_path: Path, registry: Any) -> None:
    """adapters.scoped binds a polite getter to its source: redirects stay on that source's hosts and follow its
    type's capability, never what a lookup of the URL across the catalog finds."""
    import health_data_registry.fetch as fetch_mod
    from health_data_registry.fetch import FetchResult

    calls: list[dict[str, Any]] = []

    def fake(u: str, **kw: Any) -> Any:
        calls.append({"url": u, **kw})
        return FetchResult(url=u, final_url=u, status=200, content=b"<table id='t'><tr><th>A</th></tr></table>",
                           content_type=None, sha256="0" * 64, retrieved_at=datetime(2026, 1, 1, tzinfo=UTC))  # fmt: skip

    e = entry("html_table", {"shape": "table", "table_selector": "#t"}, host="data.cdc.gov",
              api_url="https://data.cdc.gov/toy/t", landing_url="https://data.cdc.gov/toy/")  # fmt: skip
    mp = pytest.MonkeyPatch()
    mp.setattr(fetch_mod, "fetch", fake)
    try:
        get = crawl.fetcher(registry, None, robots=False)
        get.for_source(e.source)("https://data.cdc.gov/toy/t")
    finally:
        mp.undo()
    assert calls[0]["max_redirects"] == 0 and calls[0]["allowed_hosts"] == frozenset({"data.cdc.gov"})


# ------------------------------------------------------------------------------------------- pace


def test_pace_only_slows() -> None:
    assert pace_s(floor=10, crawl_delay=0, request_rate=None, api_interval=None, source_min=None) == 10
    assert pace_s(floor=10, crawl_delay=None, request_rate=(1, 30), api_interval=None, source_min=None) == 30
    assert pace_s(floor=10, crawl_delay=5, request_rate=(0, 30), api_interval=1, source_min=120) == 120
    assert pace_s(floor=0, crawl_delay=None, request_rate=None, api_interval=None, source_min=None) == 0


@pytest.mark.parametrize("bad", [0, -1, 3_601, float("nan"), float("inf")])
def test_min_interval_is_bounded(bad: float) -> None:
    with pytest.raises(ValidationError):
        entry("socrata", min_interval_s=bad)


def test_robots_request_rate_source_pace_and_24_hour_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    slept: list[float] = []
    bodies = [b"User-agent: *\nRequest-rate: 1/30\n", b"User-agent: *\nCrawl-delay: 50\n"]

    def fetch(url: str, **kw: Any) -> Any:
        from health_data_registry.fetch import FetchResult

        body = bodies.pop(0)
        return FetchResult(url=url, final_url=url, status=200, content=body, content_type=None, sha256="0" * 64,
                           retrieved_at=datetime.now(UTC))  # fmt: skip

    import health_data_registry.fetch as fetch_mod

    monkeypatch.setattr(fetch_mod, "fetch", fetch)

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    r = crawl.Robots(frozenset({"a.org", "b.org"}), sleep=sleep, clock=lambda: now[0],
                     source_intervals={"b.org": 120.0})  # fmt: skip
    r.wait_turn("https://a.org/1")
    r.wait_turn("https://a.org/2")
    assert slept == [30.0, 30.0] and r.pace_for("a.org") == 30.0  # Request-rate, robots.txt counting as one
    now[0] += 24 * 3600
    r.wait_turn("https://a.org/3")  # the robots.txt copy is a day old: read again first
    assert r.pace_for("a.org") == 50.0 and not bodies
    assert (
        r.pace_for("b.org") == 120.0
    )  # a source's own pace, and it is not charged to the crawl-delay budget


def test_policy_for_takes_the_slowest_source_on_a_host(tmp_path: Path) -> None:
    from health_data_registry.registry import load_registry
    from tests.conftest import CATALOG, write_registry

    reg = load_registry(write_registry(tmp_path / "r", catalog=CATALOG.replace("  portal: socrata\n",
                                                                             "  portal: socrata\n  min_interval_s: 45\n")))  # fmt: skip
    assert crawl.policy_for(reg).pace_for("data.cdc.gov") == 45.0


def test_host_pace() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    p = HostPace(10, sleep=sleep, clock=lambda: now[0])
    p.wait("https://a.org/x")
    p.wait("https://A.org/y")
    p.wait("https://b.org/y")
    assert slept == [10.0]
    with pytest.raises(ValueError):
        HostPace(0, sleep=print, clock=float)


def test_live_reads_are_clamped_to_the_deadline() -> None:
    assert clamp_live(3, 20.0) == 3
    with deadline.call_deadline(45.0):
        assert clamp_live(3, 20.0) == 2
    with deadline.call_deadline(1.0):
        assert clamp_live(3, 20.0) == 1
    for bad in (0, -1.0):  # a zero or negative cost would switch the clamp off: refused
        with pytest.raises(ValueError, match="positive"):
            clamp_live(3, bad)


# ------------------------------------------------------------------------------------------- the 8-week rule

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_a_single_failure_is_never_permanent_and_success_resets(tmp_path: Path) -> None:
    s = FailureStreaks(tmp_path)
    s.failed("src", T0, "blocked (403)")
    assert s.check("src", T0 + timedelta(days=1)).failed_runs == 1
    s.succeeded("src")
    assert s.get("src").first_failure_at is None
    s.failed("src", T0 + timedelta(weeks=7), "x")
    s.check("src", T0 + timedelta(weeks=14))  # the streak restarted at week 7


def test_dormant_at_exactly_eight_weeks_by_time_not_runs(tmp_path: Path) -> None:
    s = FailureStreaks(tmp_path)
    s.failed("src", T0, "timeout")
    s.failed("src", T0 + timedelta(weeks=3), "timeout")
    s.check("src", T0 + timedelta(weeks=8) - timedelta(seconds=1))
    with pytest.raises(Dormant, match="2 runs; last: timeout"):
        s.check("src", T0 + timedelta(weeks=8))


def test_refresh_records_the_streak(tmp_path: Path, registry: Any) -> None:
    ds = registry.get("toy_cases")
    lake = tmp_path / "lake"

    def down(url: str) -> Any:
        raise FetchRefused("connect_failed", "unreachable")

    with pytest.raises(FetchRefused):
        crawl.refresh_dataset(ds, lake, down)
    assert FailureStreaks(lake).get("toy_cases").failed_runs == 1
    from tests.conftest import RECORDS, raw_fetch

    crawl.refresh_dataset(ds, lake, lambda u: raw_fetch(u, RECORDS))
    assert FailureStreaks(lake).get("toy_cases").failed_runs == 0
    streaks = FailureStreaks(lake)
    streaks.failed("toy_cases", datetime.now(UTC) - timedelta(weeks=9), "timeout")
    with pytest.raises(Dormant):
        crawl.refresh_dataset(ds, lake, lambda u: raw_fetch(u, RECORDS))


# ------------------------------------------------------------------------------------------- snapshot identity


def test_a_config_only_change_builds_a_new_snapshot(tmp_path: Path) -> None:
    from health_data_registry.registry import load_registry
    from health_data_registry.store import build_snapshot, write_ndjson
    from tests.conftest import CATALOG, RECORDS, raw_fetch, write_registry

    nd = tmp_path / "p.ndjson"
    write_ndjson(nd, RECORDS)
    lake = tmp_path / "lake"
    lake.mkdir()
    a = load_registry(write_registry(tmp_path / "a")).get("toy_cases")
    b = load_registry(write_registry(tmp_path / "b", catalog=CATALOG.replace(
        "  portal: socrata\n", "  portal: socrata\n  socrata: {page_size: 1000}\n"))).get("toy_cases")  # fmt: skip
    f = raw_fetch("https://data.cdc.gov/resource/toy0-toy0.json", RECORDS)
    ma, mb = build_snapshot(lake, a, [f], [nd]), build_snapshot(lake, b, [f], [nd])
    assert ma.snapshot_id != mb.snapshot_id and method_digest(a.entry) != method_digest(b.entry)
    assert mb.method == {"type": "socrata", "digest": method_digest(b.entry)}


def test_the_digest_covers_type_config_and_extensions() -> None:
    base = entry("https_file", {"format": "csv"})
    other = entry("https_file", {"format": "csv", "header_row": 2})
    ext = entry("https_file", {"format": "csv"},
                extensions=[{"name": "header_row_finder", "hook": "locate", "args": {"first_cell": "A"}}])  # fmt: skip
    ext2 = entry("https_file", {"format": "csv"},
                 extensions=[{"name": "header_row_finder", "hook": "locate", "args": {"first_cell": "B"}}])  # fmt: skip
    digests = {method_digest(e) for e in (base, other, ext, ext2)}
    assert len(digests) == 4 and method_digest(base) == method_digest(entry("https_file", {"format": "csv"}))


def test_the_digest_covers_the_older_types_settings_on_the_shared_block() -> None:
    a = entry("https_csv", None)
    b = entry("https_csv", None, encoding="latin-1")
    c = entry("https_csv", None, reshape={"wide_to_long": {"keep": ["S"], "stubs": ["P"], "key_field": "Y"}})
    assert len({method_digest(x) for x in (a, b, c)}) == 3


def test_a_corrupt_failure_streak_is_refused_in_plain_words(tmp_path: Path) -> None:
    from health_data_registry.access import CrawlStopped, FailureStreaks

    s = FailureStreaks(tmp_path)
    for bad in ("{not json", '{"failed_runs": "x"}', '{"first_failure_at": "2026-01-01T00:00:00"}'):
        s.path("toy").parent.mkdir(parents=True, exist_ok=True)
        s.path("toy").write_text(bad)
        with pytest.raises(CrawlStopped, match="can't be read"):
            s.get("toy")


# ------------------------------------------------------------------------------------------- the schema


@pytest.mark.parametrize(
    ("source", "match"),
    [
        ({"portal": "socrata"}, "once"),  # both portal and type
        ({"type": None}, "once"),  # neither
        ({"odata": {"max_pages": 3}}, "applies only to type odata"),
        ({"encoding": "latin-1"}, "applies only to type https_csv"),
        ({"socrata": {"page_size": 0}}, "greater than or equal to 1"),
        ({"socrata": {"page_size": 60_000}}, "less than or equal to 50000"),
        ({"socrata": {"page_sise": 10}}, "Extra inputs"),
        ({"government_host": True, "host": "example.com"}, "reviewed list"),
        ({"scope_paths": [r"(a)\1"]}, "linear time"),
        ({"scope_paths": ["("]}, "linear time"),
    ],
)
def test_source_schema_refusals(source: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        entry("socrata", **source)


@pytest.mark.parametrize(
    ("block", "match"),
    [
        (None, "settings in a https_file block"),
        ({"format": "xlsx"}, "names its sheet"),
        ({"format": "csv", "sheet": "x"}, "names its sheet"),
        ({"format": "xlsx", "sheet": "x", "encoding": "latin-1"}, "csv only"),
        ({"format": "csv", "encoding": "klingon"}, "not a text encoding"),
        ({"format": "csv", "zip_member": "../x.csv"}, "plain relative path"),
        ({"format": "csv", "zip_member": "/x.csv"}, "plain relative path"),
        ({"format": "csv", "zip_memeber": "x.csv"}, "Extra inputs"),
        ({"format": "csv", "max_ratio": 0}, "greater than or equal to 1"),
        ({"format": "csv", "max_uncompressed_bytes": 10**12}, "less than or equal to"),
        ({"format": "csv", "expected_sha256": "abc"}, "pattern"),
    ],
)
def test_https_file_schema_refusals(block: dict[str, Any] | None, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        entry("https_file", block)


def test_portal_is_read_as_its_type() -> None:
    e = entry("socrata", type=None, portal="who_gho", host="ghoapi.example.org")
    assert e.source.kind == "odata" and config_of(e, ODataConfig).max_pages == 200


@pytest.mark.parametrize(("pattern", "ok"), [(r"/a/[a-z]+\.pdf", True), (r"(a|b)+", True), (r"(a+)+", True),
                                             (r"(a)\1", False), (r"(?<!\d)x", False), (r"(?=a)a", False)])  # fmt: skip
def test_safe_pattern(pattern: str, ok: bool) -> None:
    """RE2 runs nested repetition in linear time, and refuses what it can't (back-references, look-arounds)."""
    if ok:
        assert safe_pattern(pattern) == pattern
    else:
        with pytest.raises(ValueError, match="linear time"):
            safe_pattern(pattern)


def test_patterns_match_in_linear_time_on_a_crafted_input() -> None:
    """The classic catastrophic-backtracking pattern returns at once on RE2."""
    import time

    started = time.monotonic()
    assert match_whole(r"(a+)+$", "a" * 2000 + "!") is None
    assert time.monotonic() - started < 1.0
    assert match_whole(r"/x/(\d+)", "/x/" + "1" * 3000) is None  # past MAX_MATCH_INPUT: never matched
