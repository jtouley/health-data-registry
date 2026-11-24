"""PDF report tables: synthetic reports drawn, parsed back and compared; no real report is used."""

from __future__ import annotations

import dataclasses
import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from reportlab.pdfbase.pdfmetrics import stringWidth

from health_data_registry.adapters import pdf_tables as pt
from health_data_registry.adapters import wayne_county as wc
from tests.pdf_factory import (
    FONT,
    LABEL_WIDTH,
    LABEL_X,
    SIZE,
    SynthGroup,
    SynthReport,
    SynthRow,
    render,
    render_shapes,
)

WORDS = [
    "Hepatitis",
    "Chronic",
    "Acute",
    "(Genital)",
    "Syphilis-Primary",
    "Shiga",
    "toxin-producing",
    "E.",
    "coli",
    "Inv,",
    "Grp",
    "Rabies:",
    "Potential",
    "Exposure",
    "&",
    "PEP",
    "Legionellosis",
    "Candida",
    "auris",
    "Nontuberculous",
    "Mycobacterium",
    "(Varicella)",
    "Disease",
    "Other",
    "Meningitis",
    "Streptococcus",
]
GROUPS = ["Foodborne", "STI", "Other", "Viral Hepatitis", "Vaccine Preventable", "Meningitis", "VPD"]


def fmt(n: int) -> str:
    return f"{n:,}"


def _fits(words: list[str]) -> bool:
    # Word wraps a name inside its cell; a line never runs into the value columns.
    return stringWidth(" ".join(words), FONT, SIZE) <= LABEL_WIDTH


@st.composite
def label_lines(draw: st.DrawFn) -> tuple[str, ...]:
    first = draw(st.lists(st.sampled_from(WORDS), min_size=1, max_size=3).filter(_fits))
    if draw(st.booleans()):
        second = draw(st.lists(st.sampled_from(WORDS), min_size=1, max_size=2).filter(_fits))
        return (" ".join(first), " ".join(second))
    return (" ".join(first),)


@st.composite
def quarterly_report(draw: st.DrawFn) -> SynthReport:
    year = draw(st.integers(2015, 2030))
    two_line = draw(st.booleans())
    dates = ["(1/1-3/31)", "(4/1-6/30)", "(7/1-9/30)", "(10/1-12/31)"]
    headers = tuple((f"Q{q} {year}", dates[q - 1]) if two_line else (f"Q{q} {year}",) for q in range(1, 5))
    headers += ((f"Q1-Q4 {year}", "Total"),)
    groups = []
    for name in draw(st.lists(st.sampled_from(GROUPS), min_size=1, max_size=4, unique=True)):
        rows = []
        for _ in range(draw(st.integers(1, 6))):
            counts = draw(st.lists(st.integers(0, 20_000), min_size=4, max_size=4))
            suppress = draw(st.lists(st.booleans(), min_size=4, max_size=4))
            printed = tuple(
                "*" if s and 1 <= v <= 5 else fmt(v) for v, s in zip(counts, suppress, strict=True)
            )
            rows.append(SynthRow(label_lines=draw(label_lines()), values=(*printed, fmt(sum(counts)))))
        groups.append(SynthGroup(name=name, rows=tuple(rows)))
    return SynthReport(
        title=f"Select Reportable Disease Case Counts for {year}",
        headers=headers,
        groups=tuple(groups),
        group_style=draw(st.sampled_from(["repeat", "merged"])),
        repeat_header=draw(st.booleans()),
    )


def expected(report: SynthReport) -> list[tuple[str, str, tuple[str, ...]]]:
    return [(g.name, r.label, r.values) for g in report.groups for r in g.rows]


def parsed(rows: list[pt.TableRow]) -> list[tuple[str, str, tuple[str | None, ...]]]:
    return [(r.group, r.label, tuple(c.raw for c in r.cells)) for r in rows]


# --- round trip ----------------------------------------------------------------------------------------


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(report=quarterly_report())
def test_parse_report_syntheticReport_roundTripsEveryRow(report: SynthReport) -> None:
    # Arrange
    content = render(report)

    # Act
    rows, periods, facts = wc.parse_report(content)

    # Assert
    assert parsed(rows) == expected(report)
    assert [p.kind for p in periods] == ["quarter"] * 4 + ["year"]
    assert facts.report_year == periods[0].year


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(report=quarterly_report(), style=st.sampled_from(["repeat", "merged"]))
def test_parse_report_tableSpansPages_keepsGroupAcrossBreak(report: SynthReport, style: str) -> None:
    # Arrange: one long group forces a page break inside it
    base = report.groups[0].rows
    rows = tuple(base[i % len(base)] for i in range(70))
    long = dataclasses.replace(report, groups=(SynthGroup("Other", rows),), group_style=style)  # type: ignore[arg-type]

    # Act
    got, _, _ = wc.parse_report(render(long))

    # Assert
    assert max(r.page for r in got) > 1
    assert parsed(got) == expected(long)


def test_parse_report_trailingGrandTotal_isTotalRowAndChecked() -> None:
    # Arrange
    rows = (
        SynthRow(("Candida auris",), ("1", "2", "3", "4", "10")),
        SynthRow(("Legionellosis",), ("5", "6", "7", "8", "26")),
    )
    report = SynthReport(
        title="Select Reportable Disease Case Counts for 2023",
        headers=(("Q1",), ("Q2",), ("Q3",), ("Q4",), ("Totals",)),
        groups=(SynthGroup("Other", rows),),
        group_style="repeat",
        repeat_header=True,
        trailing_total=("6", "8", "10", "13", "37"),
    )

    # Act
    got, periods, _ = wc.parse_report(render(report))
    issues = wc.publisher_inconsistencies(got, periods)

    # Assert
    assert got[-1].trailing_total and wc.is_total(got[-1])
    assert [p.year for p in periods] == [2023] * 5
    assert issues == [
        "TOTAL (does not include subtotals): Q4 13.0 != sum of rows = 12.0",
        "TOTAL (does not include subtotals): Totals 37.0 != sum of rows = 36.0",
    ]


# --- loud failures -------------------------------------------------------------------------------------


def _report(values: list[tuple[str, ...]]) -> SynthReport:
    rows = tuple(SynthRow((f"Condition {chr(65 + i)}",), v) for i, v in enumerate(values))
    return SynthReport(
        title="Select Reportable Disease Case Counts for 2024",
        headers=(("Q1 2024",), ("Q2 2024",), ("Q3 2024",), ("Q4 2024",), ("Q1-Q4 2024", "Total")),
        groups=(SynthGroup("Other", rows),),
        group_style="repeat",
        repeat_header=True,
    )


@pytest.mark.parametrize(
    ("bad_rows", "outcome"),
    [
        pytest.param(0, "ok", id="none"),
        pytest.param(1, "flagged", id="one-publisher-slip"),
        pytest.param(wc.MAX_FAILED_ROWS, "flagged", id="at-limit"),
        pytest.param(wc.MAX_FAILED_ROWS + 1, "refused", id="over-limit"),
    ],
)
def test_parse_report_rowsThatDontAddUp_flaggedUpToLimitThenRefused(bad_rows: int, outcome: str) -> None:
    # Arrange: rows whose total is off by one, the rest correct
    values = [("1", "2", "3", "4", "11" if i < bad_rows else "10") for i in range(6)]
    content = render(_report(values))

    # Act / Assert
    if outcome == "refused":
        with pytest.raises(wc.ReconciliationError):
            wc.parse_report(content)
        return
    records = list(wc.records(content, report_url="https://www.waynecountymi.gov/x.pdf"))
    flagged = {r["disease"] for r in records if r["failed_checks"]}
    assert len(flagged) == bad_rows


def _geometry_without(content: bytes, drop: str) -> list[pt.PageGeometry]:
    pages = pt.read_pdf(content)
    if drop == "rules":
        return [dataclasses.replace(p, rules=()) for p in pages]
    # Remove the values of one row ("Condition B"), leaving its label behind.
    out = []
    for p in pages:
        label = next(w for w in p.words if w.text == "B")
        keep = tuple(
            w for w in p.words if not (abs(w.yc - label.yc) < 3 and pt.is_value_token(w.text, pt.TableSpec()))
        )
        out.append(dataclasses.replace(p, words=keep))
    return out


@pytest.mark.parametrize(
    ("drop", "message"),
    [
        pytest.param("rules", "no cell borders", id="no-borders"),
        pytest.param("row-values", "has a label but no values", id="dropped-row"),
    ],
)
def test_parse_tables_geometryItCantSettle_raisesLayoutError(drop: str, message: str) -> None:
    # Arrange
    content = render(_report([("1", "2", "3", "4", "10")] * 3))
    pages = _geometry_without(content, drop)

    # Act / Assert
    with pytest.raises(pt.LayoutError, match=message):
        pt.parse_tables(pages)


# --- headers, checks, facts, discovery -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "year", "want"),
    [
        ("Q2_2026", None, ("quarter", 2026, 2, None)),
        ("Q4 2024 (10/01- 12/31/2024)", None, ("quarter", 2024, 4, None)),
        ("Q3 (7/1-9/30)", 2023, ("quarter", 2023, 3, None)),
        ("YTD 2025", None, ("year_to_date", 2025, None, None)),
        ("Q1-Q4 2024 Total", None, ("year", 2024, None, None)),
        ("Totals", 2021, ("year", 2021, None, None)),
        ("2022", None, ("year", 2022, None, None)),
        ("2021- 2025 Total", None, ("multi_year_total", 2021, None, 2025)),
        ("Q1-Q4  2024\nTotal", None, ("year", 2024, None, None)),
        # The span's years come from the table's year columns later (resolve_periods); 0 until then.
        ("5-Year Average", None, ("multi_year_average", 0, None, 5)),
    ],
)
def test_parse_period_knownHeaders_meanWhatTheySay(
    header: str, year: int | None, want: tuple[object, ...]
) -> None:
    # Act
    p = wc.parse_period(header, year)

    # Assert
    assert (p.kind, p.year, p.quarter, p.end_year) == want
    assert p.header == header


@pytest.mark.parametrize(
    ("header", "year", "message"),
    [
        ("Q1 (1/1-3/31)", None, "has no year"),
        ("Totals", None, "has no year"),
        ("Rate per 100,000", 2024, "unrecognised column header"),
        ("", 2024, "unrecognised column header"),
        ("Q5 2024", None, "unrecognised column header"),
        ("0-Year Average", None, "unrecognised column header"),
    ],
)
def test_parse_period_unknownOrYearless_raises(header: str, year: int | None, message: str) -> None:
    with pytest.raises(pt.LayoutError, match=message):
        wc.parse_period(header, year)


def test_resolve_periods_yearAverage_takesSpanFromYearColumns() -> None:
    # Act
    periods = wc.resolve_periods(["2021", "2022", "2023", "3-Year Average"])

    # Assert
    assert periods[-1] == wc.Period("3-Year Average", "multi_year_average", year=2021, end_year=2023)


def test_resolve_periods_averageSpanDisagreesWithYearColumns_raises() -> None:
    with pytest.raises(pt.LayoutError, match="'5-Year Average' but the table has 1 year columns"):
        wc.resolve_periods(["2021", "5-Year Average"])


def _row(*values: str | None, group: str = "G", label: str = "L") -> pt.TableRow:
    spec = pt.TableSpec()
    cells = tuple(
        pt.Cell(
            column=str(i), raw=v, value=None if v is None else pt.parse_value(v, spec)[0], suppressed=v == "*"
        )
        for i, v in enumerate(values)
    )
    return pt.TableRow(page=1, group=group, label=label, cells=cells)


@pytest.mark.parametrize(
    ("headers", "values", "problems"),
    [
        # A Q1 report: "YTD 2025" is Q1 2025 alone, not the whole of 2025.
        (["Q1_2025", "Q2_2025", "Q1_2026", "YTD 2025", "YTD 2026"], ["5", "7", "9", "5", "9"], 0),
        (["Q1_2025", "Q2_2025", "Q1_2026", "YTD 2025", "YTD 2026"], ["5", "7", "9", "12", "9"], 1),
        # A Q2 report whose Q1 of last year isn't shown: last year's YTD can't be checked.
        (["Q2_2025", "Q1_2026", "Q2_2026", "YTD 2025", "YTD 2026"], ["7", "1", "2", "999", "3"], 0),
        # Suppressed parts: the check is skipped, never guessed.
        (["Q1 2024", "Q2 2024", "Q3 2024", "Q4 2024", "Q1-Q4 2024 Total"], ["*", "2", "3", "4", "999"], 0),
        (["2021", "2022", "2021- 2022 Total", "2-Year Average"], ["3", "4", "7", "3.5"], 0),
        (["2021", "2022", "2021- 2022 Total", "2-Year Average"], ["3", "4", "7", "3.6"], 1),
        (["2021", "2022", "2021- 2022 Total", "2-Year Average"], ["3", "4", "8", "3.5"], 1),
        # An average printed without decimals is compared at that precision (3.5 printed as "4").
        (["2021", "2022", "2021- 2022 Total", "2-Year Average"], ["3", "4", "7", "4"], 0),
        (["2021", "2022", "2021- 2022 Total", "2-Year Average"], ["3", "4", "7", "5"], 1),
        # Quarter columns out of order still make a complete YTD.
        (
            ["Q2_2025", "Q1_2025", "Q2_2026", "Q1_2026", "YTD 2025", "YTD 2026"],
            ["7", "5", "2", "1", "12", "4"],
            1,
        ),
        # A skipped check (suppressed part) doesn't stop the checks after it.
        (["Q1_2025", "Q2_2025", "Q1_2026", "YTD 2025", "YTD 2026"], ["*", "7", "9", "5", "8"], 1),
    ],
)
def test_reconcile_printedTotals_checkedOnlyWhenKnowable(
    headers: list[str], values: list[str], problems: int
) -> None:
    # Arrange
    periods = wc.resolve_periods(headers)

    # Act
    got = wc.reconcile([_row(*values)], periods)

    # Assert
    assert len(got) == problems


@pytest.mark.parametrize(
    ("text", "title", "want"),
    [
        (
            "diseases in Out-Wayne County ... Retrieved on 07/21/2026.",
            "Quarterly Report, Quarter 2 2026",
            (True, "07/21/2026", 2026),
        ),
        ("in Wayne County, excluding Detroit, for", "January-December (Q1-Q4) 2024", (True, None, 2024)),
        ("in Wayne County from 2021 through 2025", "5-Year Report (2021-2025)", (None, None, None)),
    ],
)
def test_report_facts_statedOrUnknown_neverGuessed(text: str, title: str, want: tuple[object, ...]) -> None:
    f = wc.report_facts(text, title)
    assert (f.excludes_detroit, f.data_retrieved_on, f.report_year) == want


def test_discover_reports_keepsOnlyCountyReportPdfs_inOrder_once() -> None:
    # Arrange
    html = b"""
      <a href="/files/statistics-reports/2026-q2-public.pdf"><span>Q2</span></a>
      <a href="/files/publichealth/quarterly-report-2024.pdf">2024</a>
      <a href="/files/statistics-reports/2026-q2-public.pdf">dup</a>
      <a href="/files/health/rabiesfactsheet.pdf">fact sheet</a>
      <a href="https://evil.example.com/q1-report.pdf">off host</a>
      <a href="http://www.waynecountymi.gov/files/q3.pdf">not https</a>
      <a href="/files/statistics-reports/fourth-quarter-2025.pdf">Q4</a>
    """

    # Act
    got = wc.discover_reports(html)

    # Assert
    assert [u.rsplit("/", 1)[-1] for u in got] == [
        "2026-q2-public.pdf",
        "quarterly-report-2024.pdf",
        "fourth-quarter-2025.pdf",
    ]
    assert all(u.startswith("https://www.waynecountymi.gov/") for u in got)


def test_records_suppressedAndTotals_keptAsPrinted() -> None:
    # Arrange
    rows = (
        SynthRow(("Pertussis",), ("*", "6", "0", "4", "13")),
        SynthRow(("Subtotal",), ("*", "6", "0", "4", "13")),
    )
    report = dataclasses.replace(_report([]), groups=(SynthGroup("VPD", rows),))

    # Act
    recs = list(wc.records(render(report), report_url="https://www.waynecountymi.gov/r.pdf"))

    # Assert
    first = recs[0]
    assert (first["printed"], first["count"], first["suppressed"]) == ("*", None, True)
    assert {r["is_total_row"] for r in recs if r["disease"] == "Subtotal"} == {True}
    assert all(re.fullmatch(r"\d+(\.0)?", str(int(r["count"]))) for r in recs if r["count"] is not None)


def test_reconcile_rowShorterThanPeriods_raises() -> None:
    # Arrange
    periods = wc.resolve_periods(["Q1 2024", "Q2 2024", "Q3 2024", "Q4 2024", "Q1-Q4 2024 Total"])

    # Act / Assert
    with pytest.raises(ValueError, match="zip"):
        wc.reconcile([_row("1", "2", "3", "4")], periods)


def test_publisher_inconsistencies_subtotals_comparedWithTheirOwnGroupOnly() -> None:
    # Arrange: group X has a subtotal and no rows; group Y's subtotal is off in Q4 and the total, and its
    # Q1 can't be checked because one of its rows is suppressed there.
    periods = wc.resolve_periods(["Q1 2024", "Q2 2024", "Q3 2024", "Q4 2024", "Q1-Q4 2024 Total"])
    rows = [
        _row("0", "0", "0", "0", "0", group="X", label="Subtotal"),
        _row("1", "2", "3", "4", "10", group="Y", label="B"),
        _row("*", "1", "1", "1", "3", group="Y", label="C"),
        _row("5", "3", "4", "6", "18", group="Y", label="Subtotal"),
    ]

    # Act
    got = wc.publisher_inconsistencies(rows, periods)

    # Assert
    assert got == [
        "Y Subtotal: Q4 2024 6.0 != sum of rows = 5.0",
        "Y Subtotal: Q1-Q4 2024 Total 18.0 != sum of rows = 13.0",
    ]


def test_discover_reports_oddLinks_filteredByFileNameOnly() -> None:
    # Arrange: bad bytes in the page, a report-like folder holding a non-report, and a bare host link
    html = (
        b'<p>caf\xe9</p><a href="/files/quarterly/rabies.pdf">x</a>'
        b'<a href="https://www.waynecountymi.gov">home</a>'
        b'<a href="/files/statistics-reports/q1-2026.pdf">Q1</a>'
    )

    # Act
    got = wc.discover_reports(html)

    # Assert
    assert got == ["https://www.waynecountymi.gov/files/statistics-reports/q1-2026.pdf"]


# --- reports end to end, synthetic ---------------------------------------------------------------------


def _five_year_report() -> SynthReport:
    return SynthReport(
        title="Case Counts, Out-Wayne County. Data Retrieved on 07/21/2026",
        headers=(("2021",), ("2022",), ("2021-2022", "Total"), ("2-Year", "Average")),
        groups=(
            SynthGroup(
                "Other",
                (
                    SynthRow(("Condition A",), ("3", "4", "7", "3.5")),
                    SynthRow(("Condition B",), ("3", "4", "8", "3.6")),
                ),
            ),
        ),
        group_style="repeat",
        repeat_header=True,
    )


def test_records_multiYearReport_everyFieldAsPrinted() -> None:
    # Arrange
    content = render(_five_year_report())

    # Act
    recs = list(wc.records(content, report_url="https://www.waynecountymi.gov/r.pdf"))

    # Assert
    common = {
        "report_url": "https://www.waynecountymi.gov/r.pdf",
        "excludes_detroit": True,
        "data_retrieved_on": "07/21/2026",
        "page": 1,
        "disease_group": "Other",
        "suppressed": False,
        "is_total_row": False,
    }
    assert recs[0] == {
        **common,
        "disease": "Condition A",
        "column": "2021",
        "period_kind": "year",
        "period_year": 2021,
        "period_quarter": None,
        "period_end_year": None,
        "printed": "3",
        "count": 3.0,
        "average": None,
        "failed_checks": None,
    }
    assert recs[3] == {
        **common,
        "disease": "Condition A",
        "column": "2-Year Average",
        "period_kind": "multi_year_average",
        "period_year": 2021,
        "period_quarter": None,
        "period_end_year": 2022,
        "printed": "3.5",
        "count": None,
        "average": 3.5,
        "failed_checks": None,
    }
    assert recs[4]["failed_checks"] == (
        "Condition B: 2021-2022 Total 8.0 != sum of ['2021', '2022'] = 7.0; "
        "Condition B: 2-Year Average 3.6 != 7.0/2"
    )


@pytest.mark.parametrize(("bad_rows", "suffix"), [(10, ""), (11, " (+1 more)"), (12, " (+2 more)")])
def test_parse_report_manyRowsDontAddUp_errorListsFirstTenThenCount(bad_rows: int, suffix: str) -> None:
    # Arrange
    content = render(_report([("1", "2", "3", "4", "11")] * bad_rows))

    # Act
    with pytest.raises(wc.ReconciliationError) as err:
        wc.parse_report(content)

    # Assert
    message = str(err.value)
    listed = message.removesuffix(suffix).split("; ")
    assert message.endswith(suffix) and len(listed) == 10
    assert all(
        re.fullmatch(r"Condition [A-L]: Q1-Q4 2024 Total 11\.0 != sum of .* = 10\.0", p) for p in listed
    )


def test_parse_report_noPages_raisesNoTable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.setattr(wc, "read_pdf", lambda _content: [])

    # Act / Assert
    with pytest.raises(pt.LayoutError, match="^no disease table found$"):
        wc.parse_report(b"")


def test_parse_report_titleYear_readFromFirstFortyWordsOnly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: 41 words above the table; the year that would name it is the 41st
    pages = pt.read_pdf(render(_report([("1", "2", "3", "4", "10")] * 2)))
    lead = [*(["word"] * 38), "Counts", "for", "2023"]
    words = tuple(pt.Word(t, x0=10.0 * i, x1=10.0 * i + 8, top=1.0, bottom=9.0) for i, t in enumerate(lead))
    pages[0] = dataclasses.replace(pages[0], words=words + pages[0].words)
    monkeypatch.setattr(wc, "read_pdf", lambda _content: pages)

    # Act
    _, _, facts = wc.parse_report(b"")

    # Assert
    assert facts.report_year is None


def test_parse_report_headerChangesOnLaterPage_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: a two-page table whose repeated header reads differently on page 2
    pages = pt.read_pdf(render(_report([("1", "2", "3", "4", "10")] * 40)))
    assert len(pages) == 2
    pages[1] = dataclasses.replace(
        pages[1],
        words=tuple(dataclasses.replace(w, text="Q5") if w.text == "Q4" else w for w in pages[1].words),
    )
    monkeypatch.setattr(wc, "read_pdf", lambda _content: pages)

    # Act / Assert
    with pytest.raises(pt.LayoutError, match="page 2: columns changed mid-table"):
        wc.parse_report(b"")


def _labels(n: int) -> list[str]:
    # Digit-free, distinct names: a digit in a label would be read as a value.
    return [f"Condition {chr(65 + i // 26)}{chr(97 + i % 26)}" for i in range(n)]


@pytest.mark.parametrize(
    ("style", "n_first"),
    [
        pytest.param("repeat", 33, id="repeat-second-group-starts-on-last-row"),
        pytest.param("merged", 33, id="merged-second-group-starts-on-last-row"),
        pytest.param("repeat", 34, id="repeat-second-group-starts-on-new-page"),
    ],
)
def test_parse_tables_groupAtPageBreak_rowsKeepTheirOwnGroup(style: str, n_first: int) -> None:
    # Arrange: 34 single-line rows fill page 1; the second group starts on its last row or on page 2
    names = _labels(38)
    a = tuple(SynthRow((n,), ("1", "2", "3", "4", "10")) for n in names[:n_first])
    b = tuple(SynthRow((n,), ("1", "2", "3", "4", "10")) for n in names[n_first:])
    report = dataclasses.replace(
        _report([]),
        groups=(SynthGroup("STI", a), SynthGroup("Other", b)),
        group_style=style,  # type: ignore[arg-type]
        repeat_header=False,
    )

    # Act
    got = pt.parse_tables(pt.read_pdf(render(report)))

    # Assert
    assert [r.page for r in got][33:35] == [1, 2]
    assert parsed(got) == expected(report)


def test_parse_tables_noBorderUnderHeader_headerBottomClosesFirstRow() -> None:
    # Arrange: drop the border between the header and the first row
    report = _report([("1", "2", "3", "4", "10")] * 3)
    pages = pt.read_pdf(render(report))
    header_bottom = sorted(pt.merge_rules(pages[0].rules), key=lambda r: r.y)[1]
    rules = tuple(r for r in pages[0].rules if abs(r.y - header_bottom.y) > 2)
    pages = [dataclasses.replace(pages[0], rules=rules)]

    # Act
    got = pt.parse_tables(pages)

    # Assert
    assert parsed(got) == expected(report)


def test_parse_tables_partialRuleInsideRow_isNotARowBorder() -> None:
    # Arrange: a two-line name in the middle row, with a short rule (not reaching the last column) drawn
    # between its first line and its values
    rows = (
        SynthRow(("Pertussis",), ("1", "2", "3", "4", "10")),
        SynthRow(("Hepatitis", "Chronic"), ("1", "2", "3", "4", "10")),
        SynthRow(("Legionellosis",), ("1", "2", "3", "4", "10")),
    )
    report = dataclasses.replace(_report([]), groups=(SynthGroup("Other", rows),))
    page = pt.read_pdf(render(report))[0]
    first = next(w for w in page.words if w.text == "Hepatitis")
    value = next(w for w in page.words if abs(w.yc - first.yc) < 8 and w.x0 > 280)
    partial = pt.Rule(y=(first.yc + value.yc) / 2, x0=LABEL_X, x1=280 + 4 * 46)
    page = dataclasses.replace(page, rules=(*page.rules, partial))

    # Act
    got = pt.parse_tables([page])

    # Assert
    assert parsed(got) == expected(report)


def _with_blanks(values: list[tuple[str, ...]]) -> list[pt.PageGeometry]:
    return pt.read_pdf(render(_report(values)))


def test_parse_tables_allowBlank_blankCellsKeptEmpty() -> None:
    # Arrange: a row with three of its five cells printed (the least that still reads as a row)
    pages = _with_blanks([("1", "2", "3", "4", "10"), ("1", "2", "3", "", ""), ("1", "2", "3", "4", "10")])

    # Act
    got = pt.parse_tables(pages, pt.TableSpec(allow_blank=True))

    # Assert
    assert [c.raw for c in got[1].cells] == ["1", "2", "3", None, None]
    assert got[1].cells[4] == pt.Cell(column="Q1-Q4 2024 Total", raw=None, value=None, suppressed=False)


def test_parse_tables_rowWithTooFewValues_raisesInsteadOfDropping() -> None:
    # Arrange: two of five cells is not a row of values, so its label is left over
    pages = _with_blanks([("1", "2", "3", "4", "10"), ("1", "2", "", "", ""), ("1", "2", "3", "4", "10")])

    # Act / Assert
    with pytest.raises(pt.LayoutError, match="'Condition B' has a label but no values"):
        pt.parse_tables(pages, pt.TableSpec(allow_blank=True))


def _trailing_report() -> SynthReport:
    rows = tuple(
        SynthRow((n,), ("1", "2", "3", "4", "10")) for n in ["Candida auris", "Legionellosis", "Rabies"]
    )
    return dataclasses.replace(
        _report([]), groups=(SynthGroup("Other", rows),), trailing_total=("3", "6", "9", "12", "30")
    )


def test_parse_tables_rowBelowTableWithoutTotalRule_raises() -> None:
    # Arrange
    pages = pt.read_pdf(render(_trailing_report()))

    # Act / Assert
    with pytest.raises(pt.LayoutError, match="sits outside the table borders"):
        pt.parse_tables(pages)


def test_parse_tables_trailingTotal_labelStopsAboveFootnote() -> None:
    # Arrange: a footnote in the label column just below the grand total's label
    page = pt.read_pdf(render(_trailing_report()))[0]
    last = next(w for w in page.words if w.text == "subtotals)")
    note = pt.Word("Source", x0=last.x0, x1=last.x0 + 25, top=last.bottom + 4, bottom=last.bottom + 12)
    page = dataclasses.replace(page, words=(*page.words, note))

    # Act
    got = pt.parse_tables([page], pt.TableSpec(trailing_total=wc.TOTAL_LABEL))

    # Assert
    assert (got[-1].label, got[-1].page, got[-1].trailing_total) == (
        "TOTAL (does not include subtotals)",
        1,
        True,
    )


@pytest.mark.parametrize(
    ("drop", "message"),
    [
        pytest.param({"Condition", "B"}, "has no label", id="label"),
        pytest.param({"Other"}, "has no group cell", id="group"),
    ],
)
def test_parse_tables_cellTextMissing_raisesLayoutError(drop: set[str], message: str) -> None:
    # Arrange: remove the middle row's label, or every group label
    page = pt.read_pdf(render(_report([("1", "2", "3", "4", "10")] * 3)))[0]
    row_b = next(w for w in page.words if w.text == "B")
    words = tuple(
        w for w in page.words if not (w.text in drop and (w.text == "Other" or abs(w.yc - row_b.yc) < 3))
    )

    # Act / Assert
    with pytest.raises(pt.LayoutError, match=message):
        pt.parse_tables([dataclasses.replace(page, words=words)])


def test_parse_tables_borderBetweenRowsMissing_raises() -> None:
    # Arrange: drop the border under the first row, so two rows of values share one cell
    page = pt.read_pdf(render(_report([("1", "2", "3", "4", "10")] * 3)))[0]
    row_a = next(w for w in page.words if w.text == "A")
    below_a = min((r for r in page.rules if r.y > row_a.bottom), key=lambda r: r.y)
    page = dataclasses.replace(page, rules=tuple(r for r in page.rules if abs(r.y - below_a.y) > 2))

    # Act / Assert
    with pytest.raises(pt.LayoutError, match="page 1: two rows of values in one cell band"):
        pt.parse_tables([page])


def _w(text: str, x0: float, top: float, width: float = 8.0) -> pt.Word:
    return pt.Word(text, x0=x0, x1=x0 + width, top=top, bottom=top + 8)


def _table_page(header: list[pt.Word], rows: list[list[pt.Word]]) -> pt.PageGeometry:
    words = [_w("Group", 72, 100, width=20), _w("Disease", 152, 100, width=28), *header]
    return pt.PageGeometry(number=1, width=612, words=tuple(words + [w for r in rows for w in r]), rules=())


@pytest.mark.parametrize(
    ("header", "rows", "message"),
    [
        pytest.param([], [[_w("Rabies", 152, 130)]], "no value columns under it", id="no-values"),
        pytest.param(
            [_w("Q1", 300, 100)],
            [[_w("Rabies", 152, 130), _w("5", 300, 130), _w("6", 306, 130)]] * 2,
            "fewer than two value columns",
            id="one-column",
        ),
        pytest.param(
            [_w("Q1", 300, 100), _w("Q2", 350, 100)],
            [[_w("2024", 300, 130, 16), _w("2025", 350, 130, 16)]],
            "no row fills most of the value columns",
            id="only-years",
        ),
        pytest.param(
            [_w("Q1", 300, 100)],
            [
                [_w("Rabies", 152, 130 + 20 * i), _w("1", 300, 130 + 20 * i), _w("2", 350, 130 + 20 * i)]
                for i in range(2)
            ],
            "value column at x=354 has no header",
            id="headerless-column",
        ),
    ],
)
def test_parse_tables_noReadableColumns_raises(
    header: list[pt.Word], rows: list[list[pt.Word]], message: str
) -> None:
    with pytest.raises(pt.LayoutError, match=message):
        pt.parse_tables([_table_page(header, rows)])


def test_parse_tables_twoValueColumns_parsed() -> None:
    # Arrange
    rows = (SynthRow(("Rabies",), ("1", "2")), SynthRow(("Pertussis",), ("3", "4")))
    report = dataclasses.replace(
        _report([]), headers=(("Q1 2024",), ("Q2 2024",)), groups=(SynthGroup("Other", rows),)
    )

    # Act
    got = pt.parse_tables(pt.read_pdf(render(report)))

    # Assert
    assert parsed(got) == expected(report)


def test_parse_tables_tallHeader_readFromTableTopBorder() -> None:
    # Arrange: five header lines reach more than 15 points above "Disease Group"
    lines = ("Q{q}", "2024", "cases", "and", "probable")
    headers = tuple(tuple(line.format(q=q) for line in lines) for q in range(1, 5))
    report = dataclasses.replace(_report([("1", "2", "3", "4")]), headers=headers)

    # Act
    got = pt.parse_tables(pt.read_pdf(render(report)))

    # Assert
    assert [c.column for c in got[0].cells] == [f"Q{q} 2024 cases and probable" for q in range(1, 5)]


def test_parse_tables_allowBlank_rowWithFirstTwoOfThree_kept() -> None:
    # Arrange
    rows = (SynthRow(("Rabies",), ("1", "2", "3")), SynthRow(("Pertussis",), ("4", "5", "")))
    report = dataclasses.replace(
        _report([]), headers=(("Q1 2024",), ("Q2 2024",), ("Q3 2024",)), groups=(SynthGroup("Other", rows),)
    )

    # Act
    got = pt.parse_tables(pt.read_pdf(render(report)), pt.TableSpec(allow_blank=True))

    # Assert
    assert [c.raw for c in got[1].cells] == ["4", "5", None]


def test_parse_tables_pageWithoutTable_returnsNoRows() -> None:
    # Arrange
    page = pt.PageGeometry(number=1, width=612, words=(pt.Word("Notes", 72, 100, 72, 80),), rules=())

    # Act / Assert
    assert pt.parse_tables([page]) == []


def test_read_pdf_shapes_onlyThinWideBoxesAndFlatLinesAreBorders() -> None:
    # Arrange: boxes (x, y, w, h) and lines; the x position names each shape
    rects = [(10, 100, 50, 2.0), (100, 100, 3, 1.0), (200, 100, 3.5, 0.5), (300, 100, 50, 2.5)]
    lines = [(400, 100, 440, 100), (450, 100, 490, 102), (500, 100, 540, 103)]
    content = render_shapes(rects, lines, pages=2)

    # Act
    pages = pt.read_pdf(content)

    # Assert
    assert [p.number for p in pages] == [1, 2]
    assert sorted(round(r.x0) for r in pages[0].rules) == [10, 200, 400, 450]
    assert [w.text for w in pages[0].words] == ["Shapes"]


# --- geometry helpers, on hand-placed words and rules --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"), [("1,234", (1234.0, False)), ("0.5", (0.5, False)), ("*", (None, True))]
)
def test_parse_value_printedToken_valueAndSuppression(text: str, want: tuple[float | None, bool]) -> None:
    assert pt.parse_value(text, pt.TableSpec()) == want


def test_parse_value_notAValue_raises() -> None:
    with pytest.raises(pt.LayoutError, match="not a printed value: 'n/a'"):
        pt.parse_value("n/a", pt.TableSpec())


@pytest.mark.parametrize(("offset", "n_lines"), [(1.9, 1), (2.0, 2)])
def test_lines_topsWithinTolerance_shareALine(offset: float, n_lines: int) -> None:
    assert len(pt.lines([_w("a", 0, 10), _w("b", 20, 10 + offset)])) == n_lines


@pytest.mark.parametrize(
    ("rules", "want"),
    [
        pytest.param([pt.Rule(100, 10, 50), pt.Rule(101, 51, 90)], [pt.Rule(100, 10, 90)], id="gap-1-right"),
        pytest.param(
            [pt.Rule(100, 10, 50), pt.Rule(101, 52, 90)],
            [pt.Rule(100, 10, 50), pt.Rule(101, 52, 90)],
            id="gap-2-right",
        ),
        pytest.param([pt.Rule(100, 50, 90), pt.Rule(101, 10, 49)], [pt.Rule(100, 10, 90)], id="gap-1-left"),
        pytest.param(
            [pt.Rule(100, 50, 90), pt.Rule(101, 10, 48)],
            [pt.Rule(100, 50, 90), pt.Rule(101, 10, 48)],
            id="gap-2-left",
        ),
        pytest.param([pt.Rule(100, 10, 90), pt.Rule(102.4, 10, 90)], [pt.Rule(100, 10, 90)], id="thick"),
        pytest.param(
            [pt.Rule(100, 10, 90), pt.Rule(102.5, 10, 90)],
            [pt.Rule(100, 10, 90), pt.Rule(102.5, 10, 90)],
            id="two-borders",
        ),
    ],
)
def test_merge_rules_segments_joinedOnlyWhenTouching(rules: list[pt.Rule], want: list[pt.Rule]) -> None:
    assert pt.merge_rules(rules) == want


def _header_page(*words: pt.Word) -> pt.PageGeometry:
    return pt.PageGeometry(number=1, width=612, words=words, rules=())


@pytest.mark.parametrize(
    ("dy", "dx", "found"),
    [(19.9, 2.5, True), (20.0, 2.5, False), (20.5, 2.5, False), (0.0, 2.0, False)],
)
def test_find_header_labelWordNearGroupWord_foundWithinReach(dy: float, dx: float, found: bool) -> None:
    # Arrange
    group = _w("Group", 60, 100, width=20)
    page = _header_page(group, _w("Disease", group.x1 + dx, 100 + dy, width=30))

    # Act / Assert
    assert (pt._find_header(page, pt.TableSpec()) is not None) == found


def test_find_header_twoLabelCandidates_leftmostIsTheLabel() -> None:
    # Arrange
    far, near = _w("Disease", 300, 100, width=30), _w("Disease", 150, 100, width=30)
    page = _header_page(_w("Group", 60, 100, width=20), far, near)

    # Act
    found = pt._find_header(page, pt.TableSpec())

    # Assert
    assert found is not None and found[0].label_center == near.xc


@pytest.mark.parametrize(("x1", "group_center"), [(118.0, 89.0), (119.0, 70.0)])
def test_find_header_stackedGroupWord_widensGroupCellOnlyLeftOfLabel(x1: float, group_center: float) -> None:
    # Arrange: "Disease" stacked in the group header, ending 2 (or 1) points before the label column
    stacked = pt.Word("Disease", x0=81, x1=x1, top=100, bottom=108)
    page = _header_page(_w("Group", 60, 100, width=20), stacked, _w("Disease", 120, 100, width=30))

    # Act
    found = pt._find_header(page, pt.TableSpec())

    # Assert
    assert found is not None and found[0].group_center == group_center


@pytest.mark.parametrize(("centers", "n"), [([0.0, 8.0], 1), ([0.0, 8.1], 2)])
def test_cluster_gapAtLimit_staysTogether(centers: list[float], n: int) -> None:
    assert len(pt._cluster(centers, 8.0)) == n


def test_value_lines_onlyDataLinesRightOfLabels() -> None:
    # Arrange: label column ends at x=100; data starts below y=50
    page = _header_page(
        _w("1", 120, 50),
        _w("2", 160, 50),  # at the cut-off: not below it
        _w("Type", 30, 60),
        _w("2", 60, 60),
        _w("5", 120, 60),  # one value; the other is in the label
        _w("8", 100, 70),
        _w("9", 140, 70),  # one value starts exactly at the label edge
        _w("5", 120, 80),
        _w("YTD", 140, 80),
        _w("7", 160, 80),  # a header line
        _w("Rabies", 30, 90),
        _w("3", 120, 90),
        _w("4", 160, 90),  # data
    )

    # Act
    got = pt._value_lines(page, pt.TableSpec(), below=50, label_right=100)

    # Assert
    assert [[w.text for w in line] for line in got] == [["Rabies", "3", "4"]]


@pytest.mark.parametrize(
    ("line", "want"),
    [
        pytest.param([_w("2024", 120, 0), _w("2025", 160, 0)], 0, id="bare-years"),
        pytest.param([_w("Type", 30, 0), _w("2", 60, 0), _w("5", 120, 0)], 1, id="number-in-label"),
        pytest.param([_w("5", 100, 0), _w("6", 140, 0)], 1, id="at-edge"),
    ],
)
def test_count_in_valuesRightOfEdge_counted(line: list[pt.Word], want: int) -> None:
    assert pt._count_in(line, pt.TableSpec(), 100) == want


@pytest.mark.parametrize(("y", "want"), [(0.0, 0), (10.0, 1), (19.9, 1), (20.0, None)])
def test_band_of_topInclusiveBottomExclusive(y: float, want: int | None) -> None:
    assert pt._band_of(y, [(0.0, 10.0), (10.0, 20.0)]) == want


@pytest.mark.parametrize(
    ("first", "second", "want"),
    [
        (["Shiga", "toxin-"], ["producing"], "Shiga toxin-producing"),
        (["Group", "A-"], ["Strep"], "Group A- Strep"),
    ],
)
def test_join_hyphenBreak_rejoinedOnlyBeforeLowercase(first: list[str], second: list[str], want: str) -> None:
    # Arrange
    words = [_w(t, 10 + 40 * i, 0) for i, t in enumerate(first)] + [_w(second[0], 10, 10)]

    # Act / Assert
    assert pt._join(words) == want


_COLUMNS = [pt.Column(100, 140, 120, "A"), pt.Column(140, 180, 160, "B"), pt.Column(180, 612, 200, "C")]


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(_w("7", 60, 0), id="number-in-label"),
        pytest.param(_w("note", 165, 0), id="word-in-values"),
        pytest.param(pt.Word("9", x0=100, x1=104, top=0, bottom=8), id="at-label-edge"),
    ],
)
def test_cells_tokensThatArentValues_ignored(extra: pt.Word) -> None:
    # Arrange
    line = [_w("Rabies", 20, 0), extra, _w("1", 116, 0), _w("2", 156, 0), _w("3", 196, 0)]

    # Act
    cells = pt._cells(line, _COLUMNS, pt.TableSpec())

    # Assert
    assert [c.raw for c in cells] == ["1", "2", "3"]


def test_cells_twoValuesInOneColumn_raises() -> None:
    with pytest.raises(pt.LayoutError, match="two values in column 'A': '1', '2'"):
        pt._cells([_w("1", 112, 0), _w("2", 122, 0), _w("3", 196, 0)], _COLUMNS, pt.TableSpec())


@pytest.mark.parametrize(
    ("allow_blank", "want"),
    [
        (False, None),
        (
            True,
            (
                pt.Cell("A", "1", 1.0, False),
                pt.Cell("B", None, None, False),
                pt.Cell("C", None, None, False),
            ),
        ),
    ],
)
def test_cells_blankCell_errorUnlessAllowed(allow_blank: bool, want: tuple[pt.Cell, ...] | None) -> None:
    # Arrange
    spec = pt.TableSpec(allow_blank=allow_blank)

    # Act / Assert
    if want is None:
        with pytest.raises(pt.LayoutError, match="no value read in column 'B' at y=0"):
            pt._cells([_w("1", 116, 0)], _COLUMNS, spec)
        return
    assert pt._cells([_w("1", 116, 0)], _COLUMNS, spec) == want
