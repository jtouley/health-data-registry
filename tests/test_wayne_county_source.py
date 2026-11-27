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


def test_pages_indexThenInScopeReports_everyRowSaysExcludesDetroit(ds: Dataset) -> None:
    # Arrange
    site = FakeSite(_index(OLD, ANNUAL, OLD_ANNUAL, NEW), render(_report()))

    # Act
    got = list(pages(ds.entry, site))

    # Assert
    assert site.asked == [ds.entry.source.api_url, ANNUAL, NEW]
    assert got[0].records == []  # the index page: provenance only
    rows = [r for p in got for r in p.records]
    assert len(rows) == 2 * 4 * 5
    assert {r["area"] for r in rows} == {wc.AREA}
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
        pytest.param({"period_column": "Q1 2024"}, ["report"], id="grouped-by-report"),
    ],
)
def test_reportedCases_acrossReportsOrColumns_refused(
    ds: Dataset, filters: dict[str, Any], group_by: list[str]
) -> None:
    with pytest.raises(QueryRefused) as e:
        compile_metric(ds, "reported_cases", filters=filters, group_by=group_by)
    assert e.value.code == "needs_single_value"


def test_contract_everyRowFramedOutWayne_neverStored(ds: Dataset) -> None:
    assert ds.entry.licence.redistribution is False
    assert ds.entry.source.min_interval_s >= 10
    assert any(c.startswith("Excludes Detroit") for c in ds.entry.caveats)
    area = ds.contract.column("area")
    assert area.required and area.partial_note("anything") is not None
    assert ds.contract.unreliable.kind == "publisher_suppressed" and ds.contract.unreliable.threshold == 6


def test_pacer_waitsTheDeclaredGapPerHost() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    pacer = crawl.Pacer({"www.waynecountymi.gov": 10.0}, sleep=sleep, clock=lambda: now[0])
    pacer.wait(f"{BASE}/a.pdf")  # first request: no wait
    now[0] += 4.0
    pacer.wait(f"{BASE}/b.pdf")  # 4 s later: waits the other 6
    pacer.wait("https://data.cdc.gov/resource/x.json")  # another host, no gap declared
    now[0] += 11.0
    pacer.wait(f"{BASE}/c.pdf")  # more than 10 s later: no wait
    assert slept == [6.0]


def test_pacer_forRegistry_takesTheLargestGapPerHost() -> None:
    pacer = crawl.Pacer.for_registry(load_registry(ROOT))
    assert pacer.intervals == {"www.waynecountymi.gov": 10.0}


def test_fetcher_pacesEveryRequest(monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry import fetch

    calls: list[str] = []

    class Resp:
        status, content, sha256 = 200, b"x", hashlib.sha256(b"x").hexdigest()
        final_url, retrieved_at = f"{BASE}/a.pdf", datetime(2026, 11, 24, tzinfo=UTC)

    monkeypatch.setattr(fetch, "fetch", lambda url, **_: Resp())

    class Recorder(crawl.Pacer):
        def wait(self, url: str) -> None:
            calls.append(url)

    get = crawl.fetcher(load_registry(ROOT), robots=False, pacer=Recorder({}))
    get(f"{BASE}/a.pdf")
    assert calls == [f"{BASE}/a.pdf"]
