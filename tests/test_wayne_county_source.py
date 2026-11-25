"""The Wayne County report source wired into the registry: discovery scope, pages, the live read path, the
metrics' refusals and the pacing between requests. Reports are synthetic (tests/pdf_factory.py)."""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import crawl
from health_data_registry.adapters import pages, publisher_fields
from health_data_registry.adapters import wayne_county as wc
from health_data_registry.adapters.pdf_tables import LayoutError
from health_data_registry.live import load_live
from health_data_registry.query import QueryRefused, compile_metric
from health_data_registry.registry import Dataset, load_registry
from health_data_registry.store import RawFetch
from tests.pdf_factory import SynthGroup, SynthReport, SynthRow, render

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://www.waynecountymi.gov"
NEW = f"{BASE}/files/content/mainsite/v/10/government/statistics-reports/2026-q2-public.pdf"
ANNUAL = f"{BASE}/files/assets/mainsite/v/1/documents/publichealth/quarterly-report-2024.pdf"
OLD = f"{BASE}/files/assets/mainsite/v/1/documents/cd_quarterly_report_q1_2019.pdf"
OLD_ANNUAL = f"{BASE}/files/assets/mainsite/v/1/documents/publichealth/quarterly-report-2020.pdf"


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return load_registry(ROOT).get("wayne_cd_reports")


def _report() -> SynthReport:
    rows = (
        SynthRow(("Pertussis",), ("*", "6", "0", "4", "13")),
        SynthRow(("Varicella",), ("2", "3", "1", "4", "10")),
        SynthRow(("Subtotal",), ("*", "9", "1", "8", "23")),
    )
    other = (SynthRow(("Legionellosis",), ("7", "8", "9", "10", "34")),)
    return SynthReport(
        title="Out-Wayne County Case Counts January-December (Q1-Q4) 2024",
        headers=(("Q1 2024",), ("Q2 2024",), ("Q3 2024",), ("Q4 2024",), ("Q1-Q4 2024", "Total")),
        groups=(SynthGroup("VPD", rows), SynthGroup("Other", other)),
        group_style="repeat",
        repeat_header=True,
    )


def _fetch(url: str, body: bytes) -> RawFetch:
    return RawFetch(
        url=url,
        content=body,
        sha256=hashlib.sha256(body).hexdigest(),
        retrieved_at=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
    )


def _index(*links: str) -> bytes:
    return "".join(f'<a href="{u}">r</a>' for u in links).encode()


class FakeSite:
    def __init__(self, index: bytes, pdf: bytes) -> None:
        self.index, self.pdf, self.asked = index, pdf, []  # type: ignore[var-annotated]

    def __call__(self, url: str) -> RawFetch:
        self.asked.append(url)
        return _fetch(url, self.index if url.endswith("Statistics-Reports") else self.pdf)


@pytest.mark.parametrize(
    ("url", "want"),
    [
        pytest.param(NEW, True, id="current-file-store"),
        pytest.param(ANNUAL, True, id="annual-2024"),
        pytest.param(f"{BASE}/files/assets/x/quarterly-report-2021.pdf", True, id="annual-2021"),
        pytest.param(OLD_ANNUAL, False, id="annual-2020-old-layout"),
        pytest.param(OLD, False, id="2019-quarterly"),
        pytest.param(f"{BASE}/files/content/mainsite/x.html", False, id="not-a-pdf"),
    ],
)
def test_in_scope_onlyCheckedLayouts(url: str, want: bool) -> None:
    assert wc.in_scope(url) is want


def test_pages_indexThenInScopeReports_rowsSayExcludesDetroitWhereTheReportDoes(ds: Dataset) -> None:
    # Arrange
    site = FakeSite(_index(OLD, ANNUAL, OLD_ANNUAL, NEW), render(_report()))

    # Act
    got = list(pages(ds.entry, site))

    # Assert
    assert site.asked == [ds.entry.source.api_url, ANNUAL, NEW]
    assert got[0].records == []  # the index page: provenance only
    rows = [r for p in got for r in p.records]
    assert len(rows) == 2 * 4 * 5
    assert {r["area"] for r in rows} == {wc.AREA}  # these reports say "Out-Wayne" themselves
    assert "excluding the City of Detroit" in wc.AREA
    assert {(r["printed"], r["count"], r["suppressed"]) for r in rows if r["disease"] == "Pertussis"} >= {
        ("*", None, True)
    }


def test_pages_maxRows_stopsEarly(ds: Dataset) -> None:
    site = FakeSite(_index(ANNUAL, NEW), render(_report()))
    got = list(pages(ds.entry, site, max_rows=7))
    assert sum(len(p.records) for p in got) == 7
    assert len(site.asked) == 2  # the index and the first report only


def test_pages_tooManyReportLinks_refused(ds: Dataset) -> None:
    links = [f"{BASE}/files/content/mainsite/r{i}-q1.pdf" for i in range(wc.MAX_REPORTS + 1)]
    with pytest.raises(LayoutError, match="report links"):
        list(pages(ds.entry, FakeSite(_index(*links), b"")))


def test_pages_reportThatDoesntParse_refusesTheSource(ds: Dataset) -> None:
    broken = dataclasses.replace(_report(), headers=(("Q1 2024",), ("Mystery",), ("A",), ("B",), ("C",)))
    with pytest.raises(LayoutError):
        list(pages(ds.entry, FakeSite(_index(NEW), render(broken))))


def test_publisher_fields_areTheContractsSourceFields(ds: Dataset) -> None:
    fields = publisher_fields(ds.entry, FakeSite(_index(NEW), render(_report())))
    assert fields == {c.source_field for c in ds.contract.columns}
    assert publisher_fields(ds.entry, FakeSite(_index(OLD), b"")) == set()


def _loaded(ds: Dataset) -> duckdb.DuckDBPyConnection:
    site = FakeSite(_index(ANNUAL, NEW), render(_report()))
    recs = [
        {k: (None if v is None else str(v)) for k, v in r.items()}
        for p in pages(ds.entry, site)
        for r in p.records
    ]
    con = duckdb.connect()
    load_live(con, ds, recs)
    return con


def _run(con: duckdb.DuckDBPyConnection, ds: Dataset, metric: str, **kw: Any) -> list[tuple[Any, ...]]:
    c = compile_metric(ds, metric, **kw)
    return con.execute(c.sql, c.params).fetchall()


def test_reportedCases_oneReportOneColumn_totalsExcluded_suppressionLeavesNoTotal(ds: Dataset) -> None:
    con = _loaded(ds)
    one = {"report": NEW, "period_column": "Q1-Q4 2024 Total"}
    # Varicella 10 + Pertussis 13 + Legionellosis 34; the Subtotal row is a publisher total, left out
    assert _run(con, ds, "reported_cases", filters=one) == [(57, 0, None)]
    q1 = {"report": NEW, "period_column": "Q1 2024"}
    rows = _run(con, ds, "reported_cases", filters=q1, group_by=["disease_group"])
    assert rows == [("Other", 7, 0, None), ("VPD", None, 1, "*")]  # the printed mark, never a number


@pytest.mark.parametrize(
    ("filters", "group_by"),
    [
        pytest.param({"period_column": "Q1 2024"}, [], id="every-report"),
        pytest.param({"report": NEW}, [], id="every-column"),
        pytest.param({"report": [NEW, ANNUAL], "period_column": "Q1 2024"}, [], id="two-reports"),
    ],
)
def test_reportedCases_acrossReportsOrColumns_refused(
    ds: Dataset, filters: dict[str, Any], group_by: list[str]
) -> None:
    with pytest.raises(QueryRefused) as e:
        compile_metric(ds, "reported_cases", filters=filters, group_by=group_by)
    assert e.value.code == "needs_single_value"


def test_reportedCases_groupedByReport_oneRowEachNeverAdded(ds: Dataset) -> None:
    con = _loaded(ds)
    rows = _run(con, ds, "reported_cases", filters={"period_column": "Q1-Q4 2024 Total"}, group_by=["report"])
    assert rows == [(ANNUAL, 57, 0, None), (NEW, 57, 0, None)]


def test_contract_everyRowFramedOutWayne_neverStored(ds: Dataset) -> None:
    assert ds.entry.licence.redistribution is False
    assert ds.entry.source.government_host  # paced at crawl.MIN_INTERVAL_S; a block still stops the read
    assert any(c.startswith("Excludes Detroit") for c in ds.entry.caveats)
    area = ds.contract.column("area")
    assert area.required and area.partial_note("anything") is not None
    assert ds.contract.unreliable.kind == "publisher_suppressed" and ds.contract.unreliable.threshold == 6


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s

    def __call__(self) -> float:
        return self.now


def _county_policy(clock: Clock) -> crawl.Robots:
    return crawl.Robots(
        load_registry(ROOT).hosts,
        government_hosts=frozenset({"www.waynecountymi.gov"}),
        sleep=clock.sleep,
        clock=clock,
    )


def test_policy_countyHost_pacedAtTheGovernmentHostInterval() -> None:
    assert "www.waynecountymi.gov" in crawl.policy_for(load_registry(ROOT)).paced


# --- review round fixes (PR #27) -----------------------------------------------------------------------

FIVE = f"{BASE}/files/content/mainsite/v/10/government/statistics-reports/2021-2025-5-year-report.pdf"


def _five_year() -> SynthReport:
    rows = (
        SynthRow(("Pertussis",), ("2", "4", "6", "3")),
        SynthRow(("Varicella",), ("*", "7", "9", "4.5")),
    )
    return SynthReport(
        title="Select Communicable Disease Case Counts 2021-2022",
        headers=(("2021",), ("2022",), ("2021- 2022", "Total"), ("2-Year", "Average")),
        groups=(SynthGroup("VPD", rows),),
        group_style="repeat",
        repeat_header=True,
    )


def _loaded_five(ds: Dataset) -> duckdb.DuckDBPyConnection:
    site = FakeSite(_index(FIVE), render(_five_year()))
    recs = [
        {k: (None if v is None else str(v)) for k, v in r.items()}
        for p in pages(ds.entry, site)
        for r in p.records
    ]
    con = duckdb.connect()
    load_live(con, ds, recs)
    return con


def test_reportedCases_averageColumn_neverReadAsSuppressedCounts(ds: Dataset) -> None:
    con = _loaded_five(ds)
    one = {"report": FIVE, "period_column": "2-Year Average"}
    assert _run(con, ds, "reported_cases", filters=one) == [(None, 0, None)]  # no rows, no "suppressed" claim


def test_multiYearAverageCases_readAsPrinted_neverSummed(ds: Dataset) -> None:
    con = _loaded_five(ds)
    got = _run(con, ds, "multi_year_average_cases", filters={"report": FIVE}, group_by=["disease"])
    assert sorted(got) == [("Pertussis", 3.0), ("Varicella", 4.5)]


def test_yearFilter_neverReturnsAMultiYearColumn(ds: Dataset) -> None:
    con = _loaded_five(ds)
    for column in ("2021- 2022 Total", "2-Year Average"):
        one = {"report": FIVE, "period_column": column}
        assert _run(con, ds, "reported_cases", filters=one, period={"start": 2021, "end": 2021}) == [
            (None, 0, None)
        ]
    got = _run(
        con, ds, "multi_year_average_cases", filters={"report": FIVE}, period={"start": 2021, "end": 2022}
    )
    assert got == []
    year = {"report": FIVE, "period_column": "2021"}
    assert _run(con, ds, "reported_cases", filters=year, period={"start": 2021, "end": 2021}) == [
        (None, 1, "*")
    ]


def test_rows_carryTheirOwnReportsHashAndReadTime(ds: Dataset) -> None:
    site = FakeSite(_index(ANNUAL, NEW), render(_report()))
    got = list(pages(ds.entry, site))
    for page in got[1:]:
        assert {(r["report_url"], r["report_sha256"], r["report_retrieved_at"]) for r in page.records} == {
            (page.fetch.url, page.fetch.sha256, page.fetch.retrieved_at.isoformat())
        }
    con = _loaded(ds)
    assert con.execute(f"SELECT count(*) FROM {ds.id} WHERE report_retrieved_at IS NULL").fetchone() == (0,)


def test_pages_maxReports_readsTheNewestAndSaysWhichWereLeft(ds: Dataset) -> None:
    q1 = f"{BASE}/files/content/mainsite/2026-q1-public.pdf"
    q4 = f"{BASE}/files/content/mainsite/fourth-quarter-2025.pdf"
    site = FakeSite(_index(ANNUAL, q4, q1, NEW), render(_report()))
    got = list(pages(ds.entry, site, max_reports=2))
    assert site.asked == [ds.entry.source.api_url, q1, NEW]
    assert got[0].note is not None
    assert (
        "2 newest of the 4" in got[0].note
        and "quarterly-report-2024.pdf, fourth-quarter-2025.pdf" in got[0].note
    )


def test_pages_maxReportsNotReached_noNote(ds: Dataset) -> None:
    got = list(pages(ds.entry, FakeSite(_index(ANNUAL, NEW), render(_report())), max_reports=2))
    assert got[0].note is None and len(got) == 3


def test_newest_tiesKeepPageOrder() -> None:
    a, b, c = (f"{BASE}/files/content/mainsite/{n}.pdf" for n in ("x-2025", "y", "z-2025"))
    assert wc.newest([a, b, c], 1) == [a]
    assert wc.newest([b, a, c], 2) == [a, c]


def _text_pdf(text: str) -> bytes:
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.save()
    return buf.getvalue()


def test_pages_pdfWithoutADiseaseTable_skippedWithANote(ds: Dataset) -> None:
    other = f"{BASE}/files/content/mainsite/overdose-quarterly-2026-q2.pdf"

    class Site(FakeSite):
        def __call__(self, url: str) -> RawFetch:
            self.asked.append(url)
            if url == other:
                return _fetch(url, _text_pdf("Overdose deaths, second quarter 2026"))
            return _fetch(url, self.index if url.endswith("Statistics-Reports") else self.pdf)

    got = list(pages(ds.entry, Site(_index(other, NEW), render(_report()))))
    assert [len(p.records) for p in got] == [0, 0, 4 * 5]
    assert got[1].note == "overdose-quarterly-2026-q2.pdf has no disease table, so it was not read."


def test_publisher_fields_checksEveryReport_aBrokenOneRaises(ds: Dataset) -> None:
    broken = dataclasses.replace(_report(), headers=(("Q1 2024",), ("Mystery",), ("A",), ("B",), ("C",)))

    class Site(FakeSite):
        def __call__(self, url: str) -> RawFetch:
            self.asked.append(url)
            body = render(broken) if url == ANNUAL else self.pdf
            return _fetch(url, self.index if url.endswith("Statistics-Reports") else body)

    with pytest.raises(LayoutError):
        publisher_fields(ds.entry, Site(_index(NEW, ANNUAL), render(_report())))


def test_contract_qualityRules_holdOnParsedReports(ds: Dataset) -> None:
    con = _loaded(ds)
    five = _loaded_five(ds)
    rules = [
        q
        for obj in ds.contract.raw["schema"]
        for q in [*obj.get("quality", []), *(r for p in obj["properties"] for r in p.get("quality", []))]
        if q.get("type") == "sql" and "mustBe" in q
    ]
    assert len(rules) >= 5
    for c in (con, five):
        for rule in rules:
            sql = rule["query"].replace("{object}", ds.id)
            assert c.execute(sql).fetchone() == (rule["mustBe"],), rule["id"]
    required = [col.name for col in ds.contract.columns if col.required]
    for col in required:
        assert con.execute(f"SELECT count(*) FROM {ds.id} WHERE {col} IS NULL").fetchone() == (0,), col


def test_contract_requiredColumns_eachHaveAFillRateCheck(ds: Dataset) -> None:
    for prop in ds.contract.raw["schema"][0]["properties"]:
        if prop.get("required"):
            metrics = {q.get("metric") for q in prop.get("quality", [])}
            assert "nullValues" in metrics, prop["name"]


def test_waitTurn_deadline_turnAfterItRefused(monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry.fetch import FetchRefused

    _fake_fetch(monkeypatch, [])
    clock = Clock()
    policy = _county_policy(clock)
    policy.wait_turn(f"{BASE}/a.pdf", deadline=10.0)  # after robots.txt at 0: due at 10, exactly on time
    assert clock.slept == [10.0]
    with pytest.raises(FetchRefused) as e:
        policy.wait_turn(f"{BASE}/b.pdf", deadline=19.9)
    assert e.value.code == "time_budget"
    assert clock.slept == [10.0]  # refused, not waited for


def _fake_fetch(monkeypatch: pytest.MonkeyPatch, seen: list[tuple[str, dict[str, Any]]]) -> None:
    from health_data_registry import fetch

    class Resp:
        status, content, sha256 = 200, b"x", hashlib.sha256(b"x").hexdigest()
        final_url, retrieved_at = f"{BASE}/a.pdf", datetime(2026, 11, 24, tzinfo=UTC)

    def f(url: str, **kw: Any) -> Resp:
        seen.append((url, kw))
        return Resp()

    monkeypatch.setattr(fetch, "fetch", f)


def test_fetcher_pdfReportsAskedAsHtmlAndPdf_apisAsJson(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    _fake_fetch(monkeypatch, seen)
    get = crawl.fetcher(load_registry(ROOT), robots=False)
    get(f"{BASE}/files/content/mainsite/2026-q2-public.pdf")  # a report: not under a catalogued URL
    assert "application/pdf" in seen[-1][1]["headers"]["Accept"]
    get("https://data.cdc.gov/resource/x.json")
    assert seen[-1][1]["headers"]["Accept"] == "application/json"


def test_fetcher_budget_boundsTimeoutsAndRefusesPastIt(monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry.fetch import FetchRefused

    seen: list[tuple[str, dict[str, Any]]] = []
    _fake_fetch(monkeypatch, seen)
    clock = Clock()
    policy = crawl.Robots(load_registry(ROOT).hosts, sleep=clock.sleep, clock=clock)
    get = crawl.fetcher(load_registry(ROOT), robots=policy, budget_s=50.0)
    clock.now = 20.0
    get("https://data.cdc.gov/resource/a.json")
    assert seen[-1][1]["timeout"] == 30.0
    clock.now = 50.0
    with pytest.raises(FetchRefused) as e:
        get("https://data.cdc.gov/resource/b.json")
    assert e.value.code == "time_budget"
    unbounded = crawl.fetcher(load_registry(ROOT), robots=False)
    unbounded(f"{BASE}/c.pdf")
    assert seen[-1][1]["timeout"] == crawl.FETCH_TIMEOUT_S


def test_fetcher_budget_retryBackoffPastItRefused(monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry import fetch
    from health_data_registry.fetch import FetchRefused

    def slow(url: str, **kw: Any) -> Any:
        raise FetchRefused("timeout", "slow")

    monkeypatch.setattr(fetch, "fetch", slow)
    clock = Clock()
    policy = crawl.Robots(frozenset(), sleep=clock.sleep, clock=clock)
    get = crawl.fetcher(load_registry(ROOT), robots=policy, sleep=clock.sleep, budget_s=1.0)
    with pytest.raises(FetchRefused) as e:
        get("https://data.cdc.gov/resource/a.json")
    assert e.value.code == "time_budget" and clock.slept == []


def test_liveRead_capsReportsAndKeepsTheNote(ds: Dataset) -> None:
    from health_data_registry import live

    links = [f"{BASE}/files/content/mainsite/r-{2020 + i}-q1.pdf" for i in range(live.LIVE_MAX_REPORTS + 2)]
    site = FakeSite(_index(*links), render(_report()))
    table = live.fetch_live(ds, site)
    assert len(site.asked) == 1 + live.LIVE_MAX_REPORTS
    assert any("newest" in n for n in table.manifest.notes)


@pytest.mark.parametrize(
    ("error", "code", "words"),
    [
        pytest.param(LayoutError("x"), "publisher_layout_unreadable", "report layout", id="layout"),
        pytest.param(wc.ReconciliationError("x"), "publisher_layout_unreadable", "report layout", id="sums"),
        pytest.param("robots_disallowed", "publisher_disallows_reading", "asks automated tools", id="robots"),
        pytest.param("blocked", "publisher_blocked", "refused this program", id="blocked"),
        pytest.param("time_budget", "publisher_too_slow", "longer than one question", id="slow"),
        pytest.param(RuntimeError("x"), "publisher_unreachable", "could not be reached", id="other"),
    ],
)
def test_liveRefusal_saysWhatHappenedAndWhatToDo(ds: Dataset, error: Any, code: str, words: str) -> None:
    from health_data_registry.fetch import FetchRefused
    from health_data_registry.query import live_refusal

    e = FetchRefused(error, "m") if isinstance(error, str) else error
    got = live_refusal(ds, e)
    assert got.code == code and words in str(got)
    if code != "publisher_unreachable":
        assert "few minutes" not in str(got) and ds.entry.source.landing_url in str(got)
