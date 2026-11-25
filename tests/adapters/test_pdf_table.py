"""pdf_table: reports linked from an index page, tables rebuilt from geometry; tiny hand-written PDFs only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError

from health_data_registry import deadline
from health_data_registry.adapters import LayoutError, pages, publisher_fields
from health_data_registry.adapters.row_guard import RowLevelRefused
from tests.adapters.helpers import HOST, Site, entry
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
    assert [p.records for p in got] == case.records
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
    assert [p.records for p in got] == [[{"Disease": "Salmonellosis", "2024": "12"}]]
    assert publisher_fields(e, site) == {"Group", "Disease", "2023", "2024"}


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
    assert [p.records for p in got] == [
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
