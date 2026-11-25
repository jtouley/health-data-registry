"""pdf_table: reports linked from an index page, tables rebuilt from geometry; tiny hand-written PDFs only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError

from health_data_registry import deadline
from health_data_registry.adapters import DOCUMENT_FIELDS, LayoutError, pages, publisher_fields
from health_data_registry.adapters.row_guard import RowLevelRefused
from tests.adapters.helpers import HOST, Site, entry, published
from tests.adapters.pdf_fixture import Table, render

INDEX = f"https://{HOST}/toy/data"
TABLE = Table(
    headers=("2023", "2024"),
    rows=(("Enteric", "Salmonellosis", ("1,204", "12")), ("Enteric", "Shigellosis", ("*", "3"))),
)
PDF = render(TABLE)
BLOCK = {"index_link_pattern": r"/files/reports/[a-z0-9-]+\.pdf"}
SCOPE = {"scope_paths": [r"/files/reports/[a-z0-9-]+\.pdf"]}


def index(*hrefs: str) -> bytes:
    return ("<html><body>" + "".join(f'<a href="{h}">r</a>' for h in hrefs) + "</body></html>").encode()


def report(name: str) -> str:
    return f"https://{HOST}/files/reports/{name}"


@dataclass
class Case:
    name: str
    site: dict[str, bytes]
    records: list[list[dict[str, Any]]] | None = None
    refused: type[Exception] | None = None
    match: str | None = None
    block: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=lambda: dict(SCOPE))
    calls: list[str] | None = None


EXPECTED = [
    {"Group": "Enteric", "Disease": "Salmonellosis", "2023": "1,204", "2024": "12"},
    {"Group": "Enteric", "Disease": "Shigellosis", "2023": "*", "2024": "3"},
]

CASES = [
    Case(
        "values as printed",
        {INDEX: index("/files/reports/q1-2024.pdf"), report("q1-2024.pdf"): PDF},
        [EXPECTED],
    ),
    Case(
        "links resolved, normalised and matched whole; others ignored",
        {
            INDEX: index(
                "../files/reports/a.pdf",
                "/files/reports/a.pdf",
                "/files/reports/a.pdf.html",
                "https://elsewhere.example.org/files/reports/b.pdf",
                "/files/other/c.pdf",
                "/files/reports/../../secret/d.pdf",
                "mailto:x@example.org",
            ),
            report("a.pdf"): PDF,
        },
        [EXPECTED],
        calls=[INDEX, report("a.pdf")],
    ),  # fmt: skip
    Case(
        "a link out of the source's scope is never fetched",
        {INDEX: index("/files/reports/a.pdf")},
        refused=LayoutError,
        match="no report",
        source={},
    ),
    Case(
        "too many reports: the page changed",
        {INDEX: index(*(f"/files/reports/r{i}.pdf" for i in range(3)))},
        refused=LayoutError,
        match="more than 2",
        block={"max_documents": 2},
    ),
    Case(
        "not a PDF",
        {INDEX: index("/files/reports/a.pdf"), report("a.pdf"): b"hello"},
        refused=LayoutError,
        match="could not be read",
    ),
    Case(
        "too large",
        {INDEX: index("/files/reports/a.pdf"), report("a.pdf"): PDF},
        refused=LayoutError,
        match="bytes",
        block={"max_pdf_bytes": 100},
    ),
    Case(
        "row-level table refused by the core guard",
        {
            INDEX: index("/files/reports/a.pdf"),
            report("a.pdf"): render(
                Table(headers=("Age", "Sex"), rows=(("x", "y", ("40", "1")), ("x", "z", ("41", "2"))))
            ),
        },
        refused=RowLevelRefused,
    ),
    Case(
        "a report with no table the spec describes is refused, never read as empty",
        {INDEX: index("/files/reports/a.pdf"), report("a.pdf"): PDF},
        refused=LayoutError,
        match="no table",
        block={"table": {"group_header": "Region"}},
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_pdf_table(case: Case) -> None:
    e = entry("pdf_table", {**BLOCK, **case.block}, **case.source)
    site = Site(case.site)
    if case.refused is not None:
        with pytest.raises(case.refused, match=case.match):
            list(pages(e, site))
        return
    got = list(pages(e, site))
    assert [published(p) for p in got] == case.records
    if case.calls is not None:
        assert site.calls == case.calls


def test_live_reads_newest_reports_first_and_within_its_limits() -> None:
    names = ["q1-2022.pdf", "q1-2024.pdf", "q1-2023.pdf"]
    site = Site({INDEX: index(*(f"/files/reports/{n}" for n in names)), **{report(n): PDF for n in names}})
    e = entry("pdf_table", {**BLOCK, "live": {"max_documents": 2, "order_by": "year_in_filename"}}, **SCOPE)
    list(pages(e, site, live=True))
    assert site.calls == [INDEX, report("q1-2024.pdf"), report("q1-2023.pdf")]
    site.calls.clear()
    list(pages(e, site))  # a refresh reads every report, in page order
    assert site.calls == [INDEX, *(report(n) for n in names)]


def test_live_reads_are_clamped_to_the_hosted_deadline() -> None:
    names = ["a-2024.pdf", "b-2023.pdf", "c-2022.pdf"]
    site = Site({INDEX: index(*(f"/files/reports/{n}" for n in names)), **{report(n): PDF for n in names}})
    e = entry("pdf_table", {**BLOCK, "live": {"max_documents": 3}}, **SCOPE)
    with deadline.call_deadline(25.0):  # time for one report at 20 s each, whatever the catalog allows
        list(pages(e, site, live=True))
    assert site.calls == [INDEX, report("a-2024.pdf")]


def test_max_rows_fields_and_publisher_fields() -> None:
    site = Site(
        {
            INDEX: index("/files/reports/a.pdf", "/files/reports/b.pdf"),
            report("a.pdf"): PDF,
            report("b.pdf"): PDF,
        }
    )
    e = entry("pdf_table", BLOCK, **SCOPE)
    got = list(pages(e, site, max_rows=1, fields=["Disease", "2024"]))
    assert [published(p) for p in got] == [[{"Disease": "Salmonellosis", "2024": "12"}]]
    assert publisher_fields(e, site) == {"Group", "Disease", "2023", "2024", *DOCUMENT_FIELDS}


@pytest.mark.parametrize(
    ("block", "match"),
    [
        ({**BLOCK, "max_documents": 0}, "greater than or equal to 1"),
        ({**BLOCK, "max_documents": 10_000}, "less than or equal to 200"),
        ({**BLOCK, "live": {"max_documents": 50}}, "less than or equal to 5"),
        ({"index_link_pattern": "(?<!x)a"}, "linear time"),
        ({**BLOCK, "max_document": 3}, "Extra inputs"),
    ],
)
def test_config_caps_and_patterns(block: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        entry("pdf_table", block)


def _facts_extension(monkeypatch: pytest.MonkeyPatch, run: Any) -> list[tuple[bytes, str]]:
    """Register a test-only parse extension; it records what the type gave it."""
    from health_data_registry.adapters.extensions import EXTENSIONS, Extension
    from health_data_registry.source_config import Strict

    seen: list[tuple[bytes, str]] = []

    def call(args: Any, content: bytes, url: str) -> Any:
        seen.append((content, url))
        return run(content.decode("utf-8"))

    monkeypatch.setitem(
        EXTENSIONS, "probe_facts", Extension("probe_facts", "parse", Strict, call, "test only")
    )
    return seen


FACTS_SITE = {INDEX: index("/files/reports/a.pdf"), report("a.pdf"): PDF}
FACTS_USE = [{"name": "probe_facts", "hook": "parse"}]


def test_printed_facts_are_read_from_the_text_and_added_to_each_row(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _facts_extension(monkeypatch, lambda text: [{"report_title": text.splitlines()[0]}])
    e = entry("pdf_table", BLOCK, **SCOPE, extensions=FACTS_USE)
    got = list(pages(e, Site(FACTS_SITE)))
    assert [published(p) for p in got] == [
        [{**r, "report_title": "Communicable disease report"} for r in EXPECTED]
    ]
    assert seen[0][1] == report("a.pdf")
    assert "Salmonellosis 1,204 12" in seen[0][0].decode("utf-8")  # the text in reading order, as printed
    assert "report_title" in publisher_fields(e, Site(FACTS_SITE))


@pytest.mark.parametrize(
    ("facts", "match"),
    [
        ([{"Disease": "x"}], "named like a table column"),
        ([], "0 sets of printed facts"),
        ([{"a": "1"}, {"a": "2"}], "2 sets of printed facts"),
    ],
)
def test_printed_facts_refusals(
    monkeypatch: pytest.MonkeyPatch, facts: list[dict[str, str]], match: str
) -> None:
    _facts_extension(monkeypatch, lambda _text: facts)
    e = entry("pdf_table", BLOCK, **SCOPE, extensions=FACTS_USE)
    with pytest.raises(LayoutError, match=match):
        list(pages(e, Site(FACTS_SITE)))


def test_two_extensions_reading_one_fact_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry.adapters.extensions import EXTENSIONS, Extension
    from health_data_registry.source_config import Strict

    _facts_extension(monkeypatch, lambda _text: [{"year": "2024"}])
    monkeypatch.setitem(
        EXTENSIONS, "probe_facts_two",
        Extension("probe_facts_two", "parse", Strict, lambda a, c, u: [{"year": "2023"}], "test only"),
    )  # fmt: skip
    uses = [*FACTS_USE, {"name": "probe_facts_two", "hook": "parse"}]
    e = entry("pdf_table", BLOCK, **SCOPE, extensions=uses)
    with pytest.raises(LayoutError, match="both read"):
        list(pages(e, Site(FACTS_SITE)))


TOTALS = Table(
    headers=("2023", "2024"),
    rows=(
        ("Enteric", "Salmonellosis", ("1,204", "12")),
        ("Enteric", "Shigellosis", ("*", "3")),
        ("Enteric", "Total", ("1,210", "16")),
    ),
)
RECONCILE = {"total_rows": "(?i)(sub)?totals?\\b.*", "max_failed_rows": 1}


def _one(block: dict[str, Any], pdf: bytes, **source: Any) -> list[dict[str, Any]]:
    site = Site({INDEX: index("/files/reports/a.pdf"), report("a.pdf"): pdf})
    got = list(pages(entry("pdf_table", {**BLOCK, **block}, **SCOPE, **source), site))
    return published(got[0])


def test_long_layout_gives_one_record_per_printed_cell() -> None:
    assert _one({"layout": "long"}, PDF) == [
        {"Group": "Enteric", "Disease": "Salmonellosis", "column_header": "2023", "cell_text": "1,204"},
        {"Group": "Enteric", "Disease": "Salmonellosis", "column_header": "2024", "cell_text": "12"},
        {"Group": "Enteric", "Disease": "Shigellosis", "column_header": "2023", "cell_text": "*"},
        {"Group": "Enteric", "Disease": "Shigellosis", "column_header": "2024", "cell_text": "3"},
    ]


def test_long_layout_publisher_fields_are_fixed_and_the_guard_reads_the_printed_headers() -> None:
    site = Site({INDEX: index("/files/reports/a.pdf"), report("a.pdf"): PDF})
    e = entry("pdf_table", {**BLOCK, "layout": "long"}, **SCOPE)
    assert publisher_fields(e, site) == {"Group", "Disease", "column_header", "cell_text", *DOCUMENT_FIELDS}
    row_level = render(Table(headers=("Age", "Sex"), rows=(("x", "y", ("40", "1")), ("x", "z", ("41", "2")))))
    with pytest.raises(RowLevelRefused):
        _one({"layout": "long"}, row_level)


def test_a_label_column_named_like_a_long_field_is_refused() -> None:
    with pytest.raises(ValidationError, match="long layout keeps"):
        entry("pdf_table", {**BLOCK, "layout": "long", "table": {"label_header": "cell_text"}})


def test_a_value_column_named_like_metadata_is_refused() -> None:
    clash = render(Table(headers=("2023", "failed_checks"), rows=(("G", "a", ("1", "2")),)))
    with pytest.raises(LayoutError, match="metadata"):
        _one({}, clash)


@pytest.mark.parametrize("layout", ["wide", "long"])
def test_failed_checks_are_recorded_on_the_rows_they_concern(layout: str) -> None:
    got = _one({"layout": layout, "reconcile": RECONCILE}, render(TOTALS))
    flagged = {(r["Disease"], r["failed_checks"]) for r in got}
    assert flagged == {
        ("Salmonellosis", None),
        ("Shigellosis", None),
        ("Total", "Enteric Total: 2024 is 16, but the 2 rows it totals sum to 15"),
    }
    values = {r["2024"] for r in got} if layout == "wide" else {r["cell_text"] for r in got}
    assert "16" in values  # the printed value is kept as printed; only metadata is added


def test_too_many_failing_rows_refuse_the_document() -> None:
    with pytest.raises(LayoutError, match="disagree with the table's own printed totals"):
        _one({"reconcile": {**RECONCILE, "max_failed_rows": 0}}, render(TOTALS))


def test_fields_keep_the_metadata() -> None:
    site = Site({INDEX: index("/files/reports/a.pdf"), report("a.pdf"): render(TOTALS)})
    e = entry("pdf_table", {**BLOCK, "reconcile": RECONCILE}, **SCOPE)
    got = list(pages(e, site, fields=["Disease"]))
    assert [set(r) for r in got[0].records] == [{"Disease", "failed_checks", *DOCUMENT_FIELDS}] * 3


def test_document_provenance_is_on_every_row_with_keys_stripped() -> None:
    url = report("a.pdf") + "?app_token=secret&v=2"
    site = Site({INDEX: index("/files/reports/a.pdf?app_token=secret&v=2"), url: PDF})
    got = list(pages(entry("pdf_table", BLOCK, **SCOPE), site))
    rows = got[0].records
    assert {r["document_url"] for r in rows} == {report("a.pdf") + "?v=2"}
    assert {r["document_sha256"] for r in rows} == {got[0].fetch.sha256}
    assert {r["document_retrieved_at"] for r in rows} == {"2026-11-24T12:00:00+00:00"}
