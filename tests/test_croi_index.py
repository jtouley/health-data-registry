"""The CROI abstract index on a fake croiconference.org: tiny hand-written pages, a fake clock, no network.

Covers the owner's conditions D26 (a year is complete only at the listing's own total; an empty or failed
listing page stops with incomplete_index and stays resumable; an incomplete index never reads as "no prior
work") and D27 (data first, checkpoint last; a crash between any two writes resumes to identical rows).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import croi_index, private, tools
from health_data_registry.adapters import croi
from health_data_registry.cli import parse_years
from health_data_registry.fetch import FetchRefused, FetchResult
from health_data_registry.polite import PoliteClient
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import FetchLog

YEAR = 2025
ROBOTS = b"User-agent: *\nCrawl-delay: 120\nDisallow: /wp-admin/\n"
WHEN = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
TITLES = {
    0: "HIV incidence in Kenya: a prospective cohort",
    1: "Tuberculosis screening in South Africa",
    2: "Lenacapavir PrEP uptake in the United States",
}


@pytest.fixture(scope="module")
def registry() -> Registry:
    return load_registry()


class Clock:
    def __init__(self) -> None:
        self.t = 2_000_000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def number(i: int) -> str:
    return str(100 + i)


def page_url(i: int, year: int = YEAR) -> str:
    return f"{croi.BASE}/abstract/{5000 + i}-{year}/"


def poster_url(i: int) -> str:
    return f"{croi.BASE}/wp-content/uploads/poster-{number(i)}.pdf"


def title(i: int) -> str:
    return TITLES.get(i % 3, "") + f" ({i})"


def listing_html(
    entries: list[int], total: int, *, year: int = YEAR, years: tuple[int, ...] = (YEAR,)
) -> bytes:
    opts = "".join(f'<option value="{y}">{y}</option>' for y in years)
    items = "".join(
        f'<div class="post-entry-content"><h3 class="entry-title"><a href="{page_url(i, year)}" '
        f'title="{number(i)} – {title(i)}">{number(i)} – {title(i)}</a></h3></div>'
        for i in entries
    )
    return (
        f'<form><select name="yr"><option value="">Any year</option>{opts}</select>'
        f'<select name="st"><option value="">Any</option><option value="poster">Poster</option></select></form>'
        f'<p class="abstract-search-results-count">{total:,} Results</p>'
        f'<div id="abstract-search-results-list">{items}</div>'
    ).encode()


def abstract_html(i: int, *, shown_number: str | None = None) -> bytes:
    n = shown_number or number(i)
    return (
        f'<h1 class="entry-title">{n} – {title(i)}</h1>'
        '<article class="abstract abstract-session-type-poster abstract-category-hiv-prevention">'
        f'<div class="entry-content"><p>Background</p><p>{title(i)}. We followed people with HIV in a '
        "cohort and report incidence over two years of follow-up in clinics.</p></div></article>"
        f'<div class="abstract-sidebar-content"><h4>Abstract Number</h4><div class="abstract-sidebar-text">{n}'
        "</div></div>"
        '<div class="abstract-sidebar-content"><h4>Session Title</h4><div class="abstract-sidebar-text">'
        "(P-A1) Prevention</div></div>"
        '<div class="abstract-sidebar-content"><h4>Authors</h4><div class="abstract-sidebar-text">'
        "A. Author, B. Author</div></div>"
        f'<a title="Download Poster" href="{poster_url(i)}">Download Poster</a>'
    ).encode()


class Site:
    """croiconference.org with ``n`` abstracts for YEAR, 10 to a listing page."""

    def __init__(self, n: int = 13) -> None:
        self.calls: list[str] = []
        self.pages: dict[str, bytes | int] = {f"{croi.BASE}/robots.txt": ROBOTS}
        ids = list(range(n))
        for p in range(max(1, -(-n // croi.PER_PAGE))):
            chunk = ids[p * croi.PER_PAGE : (p + 1) * croi.PER_PAGE]
            self.pages[croi.listing_url(YEAR, p + 1)] = listing_html(chunk, n)
        for i in ids:
            self.pages[page_url(i)] = abstract_html(i)

    def __call__(self, url: str) -> FetchResult:
        self.calls.append(url)
        body = self.pages.get(url, 404)
        if isinstance(body, int):
            raise FetchRefused("http_error", f"The publisher answered with status {body}.", status=body)
        return FetchResult(url, url, 200, body, "text/html", hashlib.sha256(body).hexdigest(), WHEN)


def make_client(lake: Path, site: Site, clock: Clock, **kw: Any) -> PoliteClient:
    return PoliteClient(
        croi.BASE,
        state_path=lake / "crawl_state" / f"{croi.HOST}.json",
        get_raw=site,
        clock=clock,
        log=FetchLog(lake, croi_index.INDEX_ID),
        **kw,
    )


def run(registry: Registry, lake: Path, site: Site, clock: Clock, **kw: Any) -> croi_index.RunSummary:
    with make_client(lake, site, clock, **kw) as c:
        return croi_index.index_croi(registry, lake, YEAR, client=c)


def stored(lake: Path) -> dict[str, str]:
    """Every file the crawl keeps (rows, coverage, checkpoint), by path: the state a resume must reproduce."""
    out = {}
    for p in sorted((lake / "private").rglob("*")) + [croi_index.checkpoint_path(lake, YEAR)]:
        if p.is_file():
            out[str(p.relative_to(lake))] = p.read_text(encoding="utf-8")
    return out


def rows(lake: Path, source: str) -> list[dict[str, Any]]:
    p = lake / "private" / source / f"year={YEAR}.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------------------------------- a whole year


def test_a_year_is_indexed_and_marked_complete_at_the_listing_total(
    tmp_path: Path, registry: Registry
) -> None:
    site, clock = Site(13), Clock()
    s = run(registry, tmp_path, site, clock)
    assert (s.complete, s.stop_code, s.total, s.abstracts_done, s.abstracts_read) == (True, None, 13, 13, 13)
    assert s.ok
    assert s.requests == 1 + 2 + 13  # robots.txt, two listing pages, thirteen abstracts
    assert site.calls[0] == f"{croi.BASE}/robots.txt"
    index, text = rows(tmp_path, "croi_abstracts_index"), rows(tmp_path, "croi_abstracts_text")
    assert sorted(r["abstract_number"] for r in index) == sorted(number(i) for i in range(13))
    assert len(text) == 13
    cov = private.read_coverage(tmp_path, registry.get(croi_index.INDEX_ID))
    assert cov == {f"year={YEAR}": {"total": 13, "done": 13, "complete": True}}
    assert "the year is complete" in s.lines()


def test_text_is_only_in_the_text_store_and_posters_are_linked_never_fetched(
    tmp_path: Path, registry: Registry
) -> None:
    site, clock = Site(3), Clock()
    run(registry, tmp_path, site, clock)
    index = {r["abstract_number"]: r for r in rows(tmp_path, "croi_abstracts_index")}
    text = {r["abstract_number"]: r for r in rows(tmp_path, "croi_abstracts_text")}
    first = index[number(0)]
    assert "abstract_text" not in first
    assert first["poster_url"] == poster_url(0)
    assert first["page_url"] == page_url(0)
    assert first["authors"] == "A. Author, B. Author"
    assert first["session_type"] == "poster"
    assert first["category"] == "hiv prevention"
    assert first["page_sha256"] == hashlib.sha256(abstract_html(0)).hexdigest()
    assert "hiv" in first["condition_tags"] and "kenya" in first["geography_tags"]
    assert "cohort" in first["design_tags"]
    assert text[number(0)]["abstract_text"].startswith("Background\n")
    assert not any(u.endswith(".pdf") or "/wp-content/" in u for u in site.calls)
    assert not any(str(p).endswith(".pdf") for p in tmp_path.rglob("*"))
    assert not (tmp_path / "datasets").exists()  # nothing in the public lake area
    for p in (tmp_path / "private").rglob("*.jsonl"):
        assert p.stat().st_mode & 0o077 == 0  # private rows are readable by their owner only


def test_a_complete_year_costs_no_request(tmp_path: Path, registry: Registry) -> None:
    site, clock = Site(3), Clock()
    run(registry, tmp_path, site, clock)
    before = len(site.calls)
    s = run(registry, tmp_path, site, clock)
    assert s.complete and s.requests == 0 and s.abstracts_read == 0
    assert len(site.calls) == before


def test_a_year_with_no_abstracts_is_complete_at_zero(tmp_path: Path, registry: Registry) -> None:
    site = Site(0)
    s = run(registry, tmp_path, site, Clock())
    assert (s.complete, s.total, s.abstracts_done) == (True, 0, 0)


# ------------------------------------------------------------------------------------------- D26


def test_an_empty_listing_page_before_the_total_stops_incomplete_and_resumes(
    tmp_path: Path, registry: Registry
) -> None:
    site, clock = Site(13), Clock()
    page2 = croi.listing_url(YEAR, 2)
    good = site.pages[page2]
    site.pages[page2] = listing_html([], 13)  # the site shows "13 Results" and lists none
    s = run(registry, tmp_path, site, clock)
    assert s.stop_code == "incomplete_index" and not s.ok and not s.complete
    assert "10 of 13" in (s.stopped or "")
    cp = croi_index.load_checkpoint(tmp_path, YEAR)
    assert (cp.complete, cp.next_page, len(cp.done), cp.total) == (False, 2, 10, 13)
    cov = private.read_coverage(tmp_path, registry.get(croi_index.TEXT_ID))
    assert cov == {f"year={YEAR}": {"total": 13, "done": 10, "complete": False}}
    site.pages[page2] = good
    calls = len(site.calls)
    s = run(registry, tmp_path, site, clock)
    assert s.complete and s.abstracts_read == 3
    assert site.calls[calls:][1:] == [page2] + [page_url(i) for i in range(10, 13)]  # after robots.txt


@pytest.mark.parametrize("status", [503, 404, 403])
def test_a_failed_listing_page_stops_incomplete(tmp_path: Path, registry: Registry, status: int) -> None:
    site, clock = Site(13), Clock()
    site.pages[croi.listing_url(YEAR, 2)] = status
    s = run(registry, tmp_path, site, clock)
    assert s.stop_code == "incomplete_index"
    assert f"status {status}" in (s.stopped or "")
    cp = croi_index.load_checkpoint(tmp_path, YEAR)
    assert not cp.complete and cp.next_page == 2


def test_a_failed_first_listing_page_stops_before_any_total(tmp_path: Path, registry: Registry) -> None:
    site = Site(13)
    site.pages[croi.listing_url(YEAR, 1)] = 500
    s = run(registry, tmp_path, site, Clock())
    assert s.stop_code == "incomplete_index" and s.total is None
    assert "0 of ?" in (s.stopped or "")
    assert private.read_coverage(tmp_path, registry.get(croi_index.INDEX_ID)) is None


def test_fewer_listed_than_the_total_is_never_complete(tmp_path: Path, registry: Registry) -> None:
    site, clock = Site(13), Clock()
    site.pages[croi.listing_url(YEAR, 2)] = listing_html([10, 11], 13)  # one abstract missing from the list
    s = run(registry, tmp_path, site, clock)
    assert s.stop_code == "incomplete_index" and not s.complete
    assert "ran out of pages" in (s.stopped or "")
    assert s.abstracts_done == 12


def test_a_total_that_changes_mid_crawl_stops_incomplete(tmp_path: Path, registry: Registry) -> None:
    site, clock = Site(13), Clock()
    site.pages[croi.listing_url(YEAR, 2)] = listing_html([10, 11, 12], 14)
    s = run(registry, tmp_path, site, clock)
    assert s.stop_code == "incomplete_index"
    assert "from 13 to 14" in (s.stopped or "")


def test_a_listing_without_the_year_stops_incomplete(tmp_path: Path, registry: Registry) -> None:
    site = Site(3)
    site.pages[croi.listing_url(YEAR, 1)] = listing_html([0, 1, 2], 3, years=(2024,))
    s = run(registry, tmp_path, site, Clock())
    assert s.stop_code == "incomplete_index"
    assert "not offered" in (s.stopped or "")


def test_the_budget_stops_resumably(tmp_path: Path, registry: Registry) -> None:
    site, clock = Site(13), Clock()
    s = run(registry, tmp_path, site, clock, max_requests=5)
    assert (s.stop_code, s.ok, s.abstracts_done) == ("budget_spent", True, 3)
    assert "run the same command again to carry on" in s.lines()
    s = run(registry, tmp_path, site, clock)
    assert s.complete and s.abstracts_done == 13


def test_a_changed_abstract_page_stops_with_layout_changed(tmp_path: Path, registry: Registry) -> None:
    site = Site(3)
    site.pages[page_url(1)] = abstract_html(1, shown_number="999")
    s = run(registry, tmp_path, site, Clock())
    assert s.stop_code == "layout_changed" and s.abstracts_done == 1


def test_a_redirected_abstract_page_stops_with_fetch_refused(tmp_path: Path, registry: Registry) -> None:
    site = Site(3)

    def redirecting(url: str) -> FetchResult:
        if url == page_url(0):
            site.calls.append(url)
            raise FetchRefused("too_many_redirects", "redirected", status=301, location=f"{croi.BASE}/x/")
        return site(url)

    with make_client(tmp_path, site, Clock()) as c:
        c._get_raw = redirecting
        s = croi_index.index_croi(registry, tmp_path, YEAR, client=c)
    assert s.stop_code == "fetch_refused" and f"{croi.BASE}/x/" in (s.stopped or "")


def test_another_crawler_holding_the_host_stops_before_any_request(
    tmp_path: Path, registry: Registry
) -> None:
    site, clock = Site(3), Clock()
    with make_client(tmp_path, site, clock):
        s = croi_index.index_croi(registry, tmp_path, YEAR)  # builds its own client: refused by the lock
        years = croi_index.index_years(registry, tmp_path, [YEAR])
    assert s.stop_code == "host_locked" and not s.ok
    assert [y.stop_code for y in years] == ["host_locked"]
    assert site.calls == []


# ------------------------------------------------------------------------------------------- D26: tools


def _prior(registry: Registry, lake: Path, condition: str, **kw: Any) -> dict[str, Any]:
    out = tools.find_prior_work(registry, condition=condition, fmt="json", lake=lake, **kw)
    assert isinstance(out, dict)
    return out


def test_an_incomplete_year_never_reads_as_no_prior_work(tmp_path: Path, registry: Registry) -> None:
    site, clock = Site(13), Clock()
    run(registry, tmp_path, site, clock, max_requests=5)  # 3 of 13 read
    out = _prior(registry, tmp_path, "influenza")
    assert out.get("refused") == "index_incomplete"
    assert "No indexed CROI abstract matches" not in json.dumps(out)
    assert f"{YEAR} (3 of 13 read)" in out["message"]
    run(registry, tmp_path, site, clock)
    out = _prior(registry, tmp_path, "influenza")
    assert "refused" not in out
    assert "No indexed CROI abstract matches 'influenza'" in json.dumps(out)


def test_matches_from_an_incomplete_index_carry_the_gap(tmp_path: Path, registry: Registry) -> None:
    run(registry, tmp_path, Site(13), Clock(), max_requests=5)
    out = _prior(registry, tmp_path, "tuberculosis")
    assert out["rows"] and "not complete" in json.dumps(out)


def test_asked_for_years_that_were_never_indexed_are_incomplete(tmp_path: Path, registry: Registry) -> None:
    run(registry, tmp_path, Site(3), Clock())
    out = _prior(registry, tmp_path, "influenza", years="2021-2025")
    assert out.get("refused") == "index_incomplete"
    assert "2021 (not indexed)" in out["message"] and f"{YEAR} (" not in out["message"]
    out = _prior(registry, tmp_path, "influenza", years=str(YEAR))
    assert "refused" not in out


def test_rows_without_a_coverage_record_are_partly_indexed(tmp_path: Path, registry: Registry) -> None:
    run(registry, tmp_path, Site(3), Clock())
    (tmp_path / "private" / "croi_abstracts_index" / private.COVERAGE_FILE).unlink()
    out = _prior(registry, tmp_path, "influenza")
    assert out.get("refused") == "index_incomplete" and f"{YEAR} (partly indexed)" in out["message"]


@pytest.mark.parametrize(
    ("record", "complete"),
    [
        ({"total": 3, "done": 3, "complete": True}, True),
        ({"total": 3, "done": 2, "complete": True}, False),  # a record that claims more than it holds
        ({"total": 3, "done": 3, "complete": False}, False),
        ({"total": None, "done": 0, "complete": True}, False),
    ],
)
def test_incomplete_years_trusts_only_done_equal_to_total(record: dict[str, Any], complete: bool) -> None:
    gaps = tools.incomplete_years({f"year={YEAR}": record}, {YEAR}, None, None)
    assert (gaps == []) is complete


def test_incomplete_years_ranges() -> None:
    cov = {"year=2024": {"total": 1, "done": 1, "complete": True}}
    assert tools.incomplete_years(cov, {2024}, 2024, 2024) == []
    assert tools.incomplete_years(cov, {2024}, 2023, None) == ["2023 (not indexed)"]
    assert tools.incomplete_years(cov, {2024}, None, 2024)[0] == f"{croi.FIRST_YEAR} (not indexed)"
    assert tools.incomplete_years({"junk": {}}, set(), None, None) == []


# ------------------------------------------------------------------------------------------- D27


class Crash(BaseException):
    """The process dies here (not an Exception: nothing in the crawl may catch it)."""


def _count_writes(monkeypatch: pytest.MonkeyPatch, crash_at: int | None) -> list[str]:
    """Count every write the crawl makes (polite state, rows, checkpoint, coverage); crash right after the
    ``crash_at``-th one has landed, i.e. between it and the next write."""
    seen: list[str] = []
    real_atomic = private.atomic_write
    real_state = PoliteClient._save_state

    def after(what: str) -> None:
        seen.append(what)
        if crash_at is not None and len(seen) == crash_at:
            raise Crash(what)

    def atomic(target: Path, text: str) -> None:
        real_atomic(target, text)
        after(target.name)

    def state(self: PoliteClient) -> None:
        real_state(self)
        after("polite-state")

    monkeypatch.setattr(private, "atomic_write", atomic)
    monkeypatch.setattr(PoliteClient, "_save_state", state)
    return seen


def _until_complete(registry: Registry, lake: Path, site: Site, clock: Clock) -> None:
    for _ in range(5):
        if run(registry, lake, site, clock).complete:
            return
    raise AssertionError("the crawl did not complete")  # pragma: no cover


def test_crash_between_any_two_writes_resumes_to_identical_rows(
    tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_lake = tmp_path / "clean"
    writes = _count_writes(monkeypatch, None)
    site = Site(13)
    _until_complete(registry, clean_lake, site, Clock())
    expected, n_writes = stored(clean_lake), len(writes)
    assert n_writes > 13 * 4  # polite state, two row files and the checkpoint per abstract, and more
    assert {"year=2025.jsonl", "year=2025.json", "_coverage.json", "polite-state"} <= set(writes)
    monkeypatch.undo()
    for k in range(1, n_writes + 1):
        lake = tmp_path / f"crash-{k}"
        writes = _count_writes(monkeypatch, k)
        site, clock = Site(13), Clock()
        with pytest.raises(Crash):
            run(registry, lake, site, clock)  # the lock goes with the "process" (the client is closed)
        crashed_at = writes[-1]
        monkeypatch.undo()
        _until_complete(registry, lake, site, clock)
        assert stored(lake) == expected, f"crash after write {k} ({crashed_at})"
        fetched = [u for u in site.calls if "/abstract/" in u]
        assert len(fetched) - len(set(fetched)) <= 1, f"crash after write {k} re-read more than one abstract"
        for source in ("croi_abstracts_index", "croi_abstracts_text"):
            keys = [(r["abstract_number"], r["year"]) for r in rows(lake, source)]
            assert len(keys) == len(set(keys)) == 13
        assert not list(lake.rglob("*.part")), "no temp file is left behind"


def test_a_crash_inside_a_write_leaves_the_old_file_whole(
    tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(registry, tmp_path, Site(3), Clock())
    target = tmp_path / "private" / "croi_abstracts_index" / f"year={YEAR}.jsonl"
    before = target.read_text(encoding="utf-8")

    def dies_before_rename(self: Path, other: Path) -> Path:
        raise Crash("killed before the rename")

    monkeypatch.setattr(Path, "replace", dies_before_rename)
    with pytest.raises(Crash):
        private.write_rows(
            tmp_path,
            registry.get(croi_index.INDEX_ID),
            f"year={YEAR}",
            [rows(tmp_path, "croi_abstracts_index")[0]],
        )
    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == before
    assert not list(target.parent.glob("*.part"))


# ------------------------------------------------------------------------------------------- years


def test_default_years_meet_the_five_year_rule() -> None:
    assert len(croi_index.DEFAULT_YEARS) >= croi_index.MIN_YEARS == 5
    assert list(croi_index.DEFAULT_YEARS) == sorted(croi_index.DEFAULT_YEARS)
    assert max(croi_index.DEFAULT_YEARS) <= croi.LAST_YEAR


def test_index_years_runs_in_order_and_stops_at_the_first_unfinished_year(
    tmp_path: Path, registry: Registry
) -> None:
    site, clock = Site(3), Clock()
    with make_client(tmp_path, site, clock) as c:
        out = croi_index.index_years(registry, tmp_path, [YEAR, YEAR - 1, YEAR], client=c)
    assert [s.year for s in out] == [YEAR - 1]  # 2024's listing is missing: stopped there, 2025 not started
    assert out[0].stop_code == "incomplete_index"
    with make_client(tmp_path, site, clock) as c:
        out = croi_index.index_years(registry, tmp_path, [YEAR], client=c)
    assert [s.complete for s in out] == [True]


def test_parse_years() -> None:
    assert parse_years("2025") == [2025]
    assert parse_years("2022-2026") == [2022, 2023, 2024, 2025, 2026]
    with pytest.raises(ValueError):
        parse_years("2026-2022")
    with pytest.raises(ValueError):
        parse_years("soon")


def test_full_year_estimate() -> None:
    assert croi_index.full_year_estimate(1277, 120.0) == pytest.approx((1 + 128 + 1277) * 120 / 3600)
