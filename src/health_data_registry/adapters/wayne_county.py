"""Wayne County (Michigan) communicable disease reports, published as PDFs.

Source: Wayne County Department of Health, Human & Veterans Services, Statistics & Reports page. Quarterly
and annual reports, and a 5-year report, each with a table of case counts by disease group and disease.

What the reports say about their own numbers (carried to the contract as caveats, never "fixed" here):

* The area is Wayne County **excluding the city of Detroit** ("Out-Wayne"); Detroit reports separately.
* Counts include confirmed and probable cases, from the Michigan Disease Surveillance System, and are
  preliminary: each report states the date the data were retrieved.
* Newer reports print counts of 1-5 as "*"; older ones print every count. A "*" is kept as suppressed,
  never filled in.
* A condition with 0 or suppressed counts in each of the previous five quarters is left out of the table,
  so the set of rows changes between reports.

Each table also carries its own arithmetic check: quarters add up to the year-to-date or annual total, and
years add up to the multi-year total. `reconcile` runs those checks on every parsed row, so a parsing slip
shows up as a failed sum instead of a wrong number. Terms of use are not stated, so these reports are
fetched at run time and never mirrored.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urljoin, urlsplit

from health_data_registry.adapters.pdf_tables import LayoutError, TableRow, TableSpec, parse_tables, read_pdf

if TYPE_CHECKING:
    from health_data_registry.adapters import Fetcher, Page
    from health_data_registry.registry import CatalogEntry

INDEX_URL = (
    "https://www.waynecountymi.gov/Government/Departments/Health-Human-Veterans-Services/Public-Health/"
    "Statistics-Reports"
)
HOST = "www.waynecountymi.gov"
# The area every row covers. Wayne County's health department serves the county outside the City of Detroit,
# which has its own health department; the quarterly and annual reports say so in their own words ("excluding
# Detroit", "Out-Wayne"). Carried on every record so no count is ever read as all of Wayne County. Whether the
# report itself states it is a separate field, `excludes_detroit` (None when the report is silent).
AREA = "Wayne County, Michigan, excluding the City of Detroit (Out-Wayne)"

PeriodKind = Literal["quarter", "year_to_date", "year", "multi_year_total", "multi_year_average"]


class ReconciliationError(ValueError):
    """A parsed row's own totals don't add up, so the parse can't be trusted."""


@dataclass(frozen=True)
class Period:
    header: str
    kind: PeriodKind
    year: int
    quarter: int | None = None  # for kind == "quarter"
    end_year: int | None = None  # for multi-year kinds

    @property
    def is_count(self) -> bool:
        return self.kind != "multi_year_average"


_QUARTER = re.compile(r"^Q([1-4])[_ ](\d{4})\b")
_QUARTER_NO_YEAR = re.compile(r"^Q([1-4])(?: \(\d{1,2}/\d{1,2}-\d{1,2}/\d{1,2}(?:/\d{4})?\))?$")
_TOTALS = re.compile(r"^Totals?$")
_YTD = re.compile(r"^YTD (\d{4})$")
_ANNUAL_TOTAL = re.compile(r"^Q1-Q4 (\d{4}) Total$")
_YEAR = re.compile(r"^(\d{4})$")
_SPAN_TOTAL = re.compile(r"^(\d{4})- ?(\d{4}) Total$")
_SPAN_AVERAGE = re.compile(r"^([1-9]\d*)-Year Average$")


def parse_period(header: str, report_year: int | None = None) -> Period:
    """What a column header means. Headers without a year ("Q1 (1/1-3/31)", "Totals") take the year from
    the report's title. Unknown headers raise: a new layout needs a reviewed rule, not a guess."""
    h = re.sub(r"\s+", " ", header).strip()
    if m := _QUARTER.match(h):
        return Period(header, "quarter", year=int(m[2]), quarter=int(m[1]))
    if (m := _QUARTER_NO_YEAR.match(h)) or _TOTALS.match(h):
        if report_year is None:
            raise LayoutError(f"column {header!r} has no year and the report title doesn't give one")
        if m:
            return Period(header, "quarter", year=report_year, quarter=int(m[1]))
        return Period(header, "year", year=report_year)
    if m := _YTD.match(h):
        return Period(header, "year_to_date", year=int(m[1]))
    if m := _ANNUAL_TOTAL.match(h):
        return Period(header, "year", year=int(m[1]))
    if m := _YEAR.match(h):
        return Period(header, "year", year=int(m[1]))
    if m := _SPAN_TOTAL.match(h):
        return Period(header, "multi_year_total", year=int(m[1]), end_year=int(m[2]))
    if m := _SPAN_AVERAGE.match(h):
        return Period(header, "multi_year_average", year=0, end_year=int(m[1]))
    raise LayoutError(f"unrecognised column header {header!r}")


def resolve_periods(headers: Sequence[str], report_year: int | None = None) -> list[Period]:
    """Periods for a table's columns. A 'N-Year Average' takes its years from the table's year columns."""
    periods = [parse_period(h, report_year) for h in headers]
    years = sorted(p.year for p in periods if p.kind == "year")
    out = []
    for p in periods:
        if p.kind == "multi_year_average":
            span = p.end_year or 0
            if len(years) != span:
                raise LayoutError(f"{p.header!r} but the table has {len(years)} year columns")
            p = Period(p.header, p.kind, year=years[0], end_year=years[-1])
        out.append(p)
    return out


def checks(periods: Sequence[Period]) -> list[tuple[Period, list[Period]]]:
    """Which printed totals each table lets us verify, as (total column, the columns it should equal)."""
    quarters = [p for p in periods if p.kind == "quarter"]
    latest = max(quarters, key=lambda p: (p.year, p.quarter or 0), default=None)
    out: list[tuple[Period, list[Period]]] = []
    for total in periods:
        if total.kind == "year_to_date" and latest is not None:
            # "YTD" runs from Q1 through the report's own quarter, in every year shown ("YTD 2025" in the
            # Q1 2026 report is Q1 2025 alone). Checkable only when all those quarters are columns.
            wanted = list(range(1, (latest.quarter or 0) + 1))
            qs = sorted(
                (p for p in quarters if p.year == total.year and p.quarter in wanted),
                key=lambda p: p.quarter or 0,
            )
            if [p.quarter for p in qs] == wanted:
                out.append((total, qs))
        elif total.kind == "year":
            qs = [p for p in quarters if p.year == total.year]
            if len(qs) == 4:
                out.append((total, qs))
        elif total.kind in ("multi_year_total", "multi_year_average"):
            ys = [p for p in periods if p.kind == "year" and total.year <= p.year <= (total.end_year or 0)]
            if ys:
                out.append((total, ys))
    return out


def reconcile(rows: Sequence[TableRow], periods: Sequence[Period]) -> list[str]:
    """Every row whose printed totals disagree with its printed parts. Checks with a suppressed or missing
    part are skipped, since the true sum is unknown."""
    problems: list[str] = []
    pairs = checks(periods)
    for row in rows:
        values = {p: c.value for p, c in zip(periods, row.cells, strict=True)}
        for total, parts in pairs:
            t, vs = values[total], [values[p] for p in parts]
            if t is None or any(v is None for v in vs):
                continue
            expected = sum(v for v in vs if v is not None)
            if total.kind == "multi_year_average":
                decimals = _decimals(row, periods, total)
                if round(expected / len(parts), decimals) != round(t, decimals):
                    problems.append(f"{row.label}: {total.header} {t} != {expected}/{len(parts)}")
            elif expected != t:
                problems.append(
                    f"{row.label}: {total.header} {t} != sum of {[p.header for p in parts]} = {expected}"
                )
    return problems


def _decimals(row: TableRow, periods: Sequence[Period], total: Period) -> int:
    raw = row.cells[list(periods).index(total)].raw or ""
    return len(raw.split(".")[1]) if "." in raw else 0


@dataclass(frozen=True)
class ReportFacts:
    """What the report states about its own data. None means the report doesn't say."""

    excludes_detroit: bool | None
    data_retrieved_on: str | None  # as printed, e.g. "07/21/2026"
    report_year: int | None  # from the title, for column headers that omit it


_EXCLUDES_DETROIT = re.compile(r"(excluding (the city of )?Detroit|Out-Wayne)", re.I)
_RETRIEVED = re.compile(r"Retrieved on (\d{1,2}/\d{1,2}/\d{4})", re.I)
# Title forms seen: "Case Counts for 2023", "January-December (Q1-Q4) 2024", "Quarter 2 2026".
_TITLE_YEAR = re.compile(r"(?:Counts for|\(Q1-Q4\)|Quarter [1-4])\s+(\d{4})", re.I)
MAX_FAILED_ROWS = 2
TOTAL_LABEL = re.compile(r"\b(?:sub)?totals?\b", re.I)


def report_facts(text: str, title: str) -> ReportFacts:
    retrieved = _RETRIEVED.search(text)
    years = {int(y) for y in _TITLE_YEAR.findall(title)}
    return ReportFacts(
        excludes_detroit=True if _EXCLUDES_DETROIT.search(text) else None,
        data_retrieved_on=retrieved[1] if retrieved else None,
        report_year=years.pop() if len(years) == 1 else None,
    )


def parse_report(content: bytes) -> tuple[list[TableRow], list[Period], ReportFacts]:
    """The disease table of one report, checked against its own totals, and what the report says about
    its area and data date."""
    pages = read_pdf(content)
    text = " ".join(w.text for page in pages for w in page.words)
    title = (
        " ".join(w.text for w in sorted(pages[0].words, key=lambda w: (w.top, w.x0))[:40]) if pages else ""
    )
    facts = report_facts(text, title)
    rows = parse_tables(pages, TableSpec(trailing_total=TOTAL_LABEL))
    if not rows:
        raise LayoutError("no disease table found")
    headers = [c.column for c in rows[0].cells]
    for row in rows:
        if [c.column for c in row.cells] != headers:
            raise LayoutError(f"page {row.page}: columns changed mid-table")
    periods = resolve_periods(headers, facts.report_year)
    # Rows that don't add up: one or two are the publisher's own slips (the 2022 report prints Influenza Q3
    # as 103 in the disease row and 113 in its subtotal). Those rows stay, flagged by `failed_checks`. More
    # than that means this parser misread the table, so nothing from the report is used.
    problems = reconcile([r for r in rows if not is_total(r)], periods)
    if len(problems) > MAX_FAILED_ROWS:
        raise ReconciliationError(
            "; ".join(problems[:10]) + (f" (+{len(problems) - 10} more)" if len(problems) > 10 else "")
        )
    return rows, periods, facts


def failed_checks(row: TableRow, periods: Sequence[Period]) -> list[str]:
    """This row's own sums that don't add up (empty when it reconciles or can't be checked)."""
    return reconcile([row], periods)


def is_total(row: TableRow) -> bool:
    return bool(TOTAL_LABEL.search(row.label))


def publisher_inconsistencies(rows: Sequence[TableRow], periods: Sequence[Period]) -> list[str]:
    """Subtotal rows whose own sums disagree, and subtotals that don't equal their group's rows."""
    totals = [r for r in rows if is_total(r)]
    problems = reconcile(totals, periods)
    for t in totals:
        # A group's subtotal covers its rows; the grand total below the table covers every disease row.
        members = [r for r in rows if not is_total(r) and (t.trailing_total or r.group == t.group)]
        if not members:
            continue
        for i, period in enumerate(periods):
            parts = [m.cells[i].value for m in members]
            total = t.cells[i].value
            if total is not None and all(v is not None for v in parts):
                expected = sum(v for v in parts if v is not None)
                if expected != total:
                    problems.append(
                        f"{t.group} {t.label}".strip()
                        + f": {period.header} {total} != sum of rows = {expected}"
                    )
    return problems


def records(content: bytes, *, report_url: str) -> Iterator[dict[str, Any]]:
    """One record per printed cell, keyed by the publisher's own labels. Nothing is renamed or filled."""
    rows, periods, facts = parse_report(content)
    for row in rows:
        total_row = is_total(row)
        failed = failed_checks(row, periods)
        for period, cell in zip(periods, row.cells, strict=True):
            yield {
                "report_url": report_url,
                "area": AREA,
                "excludes_detroit": facts.excludes_detroit,
                "data_retrieved_on": facts.data_retrieved_on,
                "page": row.page,
                "disease_group": row.group,
                "disease": row.label,
                "column": period.header,
                "period_kind": period.kind,
                "period_year": period.year,
                "period_quarter": period.quarter,
                "period_end_year": period.end_year,
                "printed": cell.raw,
                "count": cell.value if period.is_count else None,
                "average": cell.value if not period.is_count else None,
                "suppressed": cell.suppressed,
                "is_total_row": total_row,
                "failed_checks": "; ".join(failed) or None,
            }


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


_REPORT_NAME = re.compile(r"(quarter|q[1-4]|5-year|cd-)", re.I)


def discover_reports(index_html: bytes, *, base_url: str = INDEX_URL) -> list[str]:
    """Report PDF links on the index page, on the county's own host, in page order, without duplicates.

    File names are irregular ("2026-q2-public.pdf", "fourth-quarter-2025.pdf", "quarterly-report-2024.pdf"),
    so this only filters to plausible report PDFs; what each one is comes from its own table headers.
    """
    parser = _Links()
    parser.feed(index_html.decode("utf-8", errors="replace"))
    out: list[str] = []
    for href in parser.hrefs:
        url = urljoin(base_url, href)
        parts = urlsplit(url)
        name = parts.path.rsplit("/", 1)[-1]
        on_host = parts.scheme == "https" and parts.hostname == HOST
        if on_host and name.lower().endswith(".pdf") and _REPORT_NAME.search(name) and url not in out:
            out.append(url)
    return out


# Reports in the layouts the parser has been checked on (2021 onward): the annual reports
# (quarterly-report-2021.pdf ... ) and everything under the county's current file store, /files/content/,
# where the 2025-on quarterly reports and the 2021-2025 5-year report sit. Older reports (2014-2020) use
# other layouts and are not read; a report in scope that doesn't parse refuses the whole source.
_IN_SCOPE_PATH = re.compile(r"^/files/content/mainsite/.+\.pdf$", re.I)
_IN_SCOPE_ANNUAL = re.compile(r"/quarterly-report-20(?:2[1-9]|[3-9]\d)\.pdf$", re.I)
MAX_REPORTS = 40  # more report links than this means the page changed; refuse rather than fetch them all


def in_scope(url: str) -> bool:
    """A report in a layout the parser is checked against (see _IN_SCOPE_PATH)."""
    path = urlsplit(url).path
    return bool(_IN_SCOPE_PATH.match(path) or _IN_SCOPE_ANNUAL.search(path))


def pages(entry: CatalogEntry, fetcher: Fetcher, *, max_rows: int | None = None) -> Iterator[Page]:
    """The index page (kept for provenance, no records), then each in-scope report with its records.

    Fetched at run time only: the county states no terms of use, so nothing is stored (catalog
    licence.redistribution is false). A report in scope that can't be parsed raises: no partial table.
    """
    from health_data_registry.adapters import Page

    index = fetcher(entry.source.api_url)
    yield Page(fetch=index, records=[])
    urls = [u for u in discover_reports(index.content, base_url=entry.source.api_url) if in_scope(u)]
    if len(urls) > MAX_REPORTS:
        raise LayoutError(f"{len(urls)} report links on the index page; expected at most {MAX_REPORTS}")
    seen = 0
    for url in urls:
        raw = fetcher(url)
        recs = list(records(raw.content, report_url=url))
        if max_rows is not None:
            recs = recs[: max(0, max_rows - seen)]
        seen += len(recs)
        yield Page(fetch=raw, records=recs)
        if max_rows is not None and seen >= max_rows:
            return


def publisher_fields(entry: CatalogEntry, fetcher: Fetcher) -> set[str]:
    """The fields the newest in-scope report yields today (the contract drift check)."""
    index = fetcher(entry.source.api_url)
    urls = [u for u in discover_reports(index.content, base_url=entry.source.api_url) if in_scope(u)]
    if not urls:
        return set()
    first = next(records(fetcher(urls[0]).content, report_url=urls[0]), None)
    return set(first) if first else set()
