"""html_table (shape table) and the HTML reader, table-driven over tiny hand-written pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import ValidationError

from health_data_registry.adapters import DOCUMENT_FIELDS, LayoutError, pages, publisher_fields
from health_data_registry.adapters.html import SelectorError, parse_html, parse_selector, select

# Imported by name, not only reached through adapters.TYPES' lazy loader, so mutation testing maps these tests to it.
from health_data_registry.adapters.html_table import read_table
from health_data_registry.adapters.row_guard import RowLevelRefused
from tests.adapters.helpers import HOST, Site, entry, published

URL = f"https://{HOST}/toy/data"


def page(table: str, extra: str = "") -> bytes:
    return f"<html><body><p>Intro</p>{extra}{table}<script>var x = '<td>';</script></body></html>".encode()


SIMPLE = page(
    '<table id="cases"><caption>Reported cases by county</caption>'
    "<thead><tr><th>County</th><th>Cases</th></tr></thead>"
    "<tbody><tr><td>Wayne</td><td>1,204<sup>a</sup></td></tr><tr><td>Oakland</td><td>*</td></tr></tbody></table>"
)
SPANNED = page(
    '<table id="cases"><thead><tr><th rowspan="2">County</th><th colspan="2">Cases</th></tr>'
    "<tr><th>2023</th><th>2024</th></tr></thead><tbody><tr><td>Wayne</td><td>3</td><td>4</td></tr></tbody></table>"
)


@dataclass
class Case:
    name: str
    block: dict[str, Any]
    content: bytes
    records: list[dict[str, str]] | None = None
    refused: type[Exception] | None = None
    match: str | None = None


CASES = [
    Case(
        "as printed, footnotes kept",
        {"shape": "table", "table_selector": "table#cases", "caption": "Reported cases by county"},
        SIMPLE,
        [{"County": "Wayne", "Cases": "1,204a"}, {"County": "Oakland", "Cases": "*"}],
    ),
    Case(
        "caption must match",
        {"shape": "table", "table_selector": "table#cases", "caption": "Deaths"},
        SIMPLE,
        refused=LayoutError,
        match="caption",
    ),
    Case(
        "selector matches two tables",
        {"shape": "table", "table_selector": "table"},
        page("<table><tr><th>A</th></tr></table>", "<table><tr><th>B</th></tr></table>"),
        refused=LayoutError,
        match="2 tables",
    ),
    Case(
        "selector matches none",
        {"shape": "table", "table_selector": "table#gone"},
        SIMPLE,
        refused=LayoutError,
        match="0 tables",
    ),
    Case(
        "spanning header joined",
        {"shape": "table", "table_selector": "#cases", "header_separator": " / "},
        SPANNED,
        [{"County": "Wayne", "Cases / 2023": "3", "Cases / 2024": "4"}],
    ),
    Case(
        "spanning header without separator",
        {"shape": "table", "table_selector": "#cases"},
        SPANNED,
        refused=LayoutError,
        match="header_separator",
    ),
    Case(
        "spanning body cell refused",
        {"shape": "table", "table_selector": "table"},
        page('<table><tr><th>A</th><th>B</th></tr><tr><td colspan="2">x</td></tr></table>'),
        refused=LayoutError,
        match="spanning",
    ),
    Case(
        "short row refused",
        {"shape": "table", "table_selector": "table"},
        page("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td></tr></table>"),
        refused=LayoutError,
        match="1 cells for 2",
    ),
    Case(
        "no thead: first row is the header; nested table not read into it",
        {"shape": "table", "table_selector": "table.outer"},
        page(
            '<table class="outer"><tr><th>A</th><th>B</th></tr>'
            "<tr><td>1</td><td><table><tr><td>inner</td></tr></table></td></tr></table>"
        ),
        [{"A": "1", "B": "inner"}],
    ),
    Case(
        "repeated label refused",
        {"shape": "table", "table_selector": "table"},
        page("<table><tr><th>A</th><th>A</th></tr></table>"),
        refused=LayoutError,
        match="repeated",
    ),
    Case(
        "bad span refused",
        {"shape": "table", "table_selector": "table"},
        page('<table><tr><th colspan="x">A</th></tr></table>'),
        refused=LayoutError,
        match="not a number",
    ),
    Case(
        "row-level table refused by the core guard",
        {"shape": "table", "table_selector": "table"},
        page(
            "<table><tr><th>Name</th><th>Age</th><th>Sex</th></tr><tr><td>X</td><td>4</td><td>F</td></tr></table>"
        ),
        refused=RowLevelRefused,
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_html_table(case: Case) -> None:
    e = entry("html_table", case.block)
    site = Site({URL: case.content})
    if case.refused is not None:
        with pytest.raises(case.refused, match=case.match):
            list(pages(e, site))
        return
    got = list(pages(e, site))
    assert published(got[0]) == case.records and got[0].fetch.content == case.content


def test_fields_max_rows_and_publisher_fields() -> None:
    e = entry("html_table", {"shape": "table", "table_selector": "#cases"})
    assert published(list(pages(e, Site({URL: SIMPLE}), fields=["Cases"], max_rows=1))[0]) == [
        {"Cases": "1,204a"}
    ]
    assert publisher_fields(e, Site({URL: SIMPLE})) == {"County", "Cases", *DOCUMENT_FIELDS}


@pytest.mark.parametrize(
    "selector",
    ["table", "#x", ".a.b", "div.entry > h1", "ul li", "a[href]", 'td[data-x="1"]', "h1, h2", "*.x"],
)
def test_supported_selectors_parse(selector: str) -> None:
    assert parse_selector(selector)


@pytest.mark.parametrize("selector", ["", "a >", "> a", "a > > b", "a:hover", "a + b", "a ~ b", "a[x^=y]"])
def test_unsupported_selectors_refused(selector: str) -> None:
    with pytest.raises(SelectorError):
        parse_selector(selector)


def test_unsupported_selector_refused_at_catalog_load() -> None:
    with pytest.raises(ValidationError, match="supported selector"):
        entry("html_table", {"shape": "table", "table_selector": "table:first-child"})


def test_select_semantics() -> None:
    doc = parse_html(
        b'<div class="entry a"><h1 id="t">Title <b>here</b></h1><p>x<br>y</p></div>'
        b'<section><div><h1>Other</h1></div></section><ul><li data-k="1">one</li><li>two</li></ul>'
    )
    assert [el.text() for el in select(doc, "div.entry > h1")] == ["Title here"]
    assert [el.text() for el in select(doc, "section h1")] == ["Other"]
    assert [el.text() for el in select(doc, "section > h1")] == []
    assert [el.text() for el in select(doc, 'li[data-k="1"]')] == ["one"]
    assert [el.text() for el in select(doc, "li[data-k]")] == ["one"]
    assert [el.text() for el in select(doc, "h1, li")] == ["Title here", "Other", "one", "two"]
    assert [el.text() for el in select(doc, "#t")] == ["Title here"]
    assert [el.text() for el in select(doc, ".a.entry")] == ["Title here x y"]


@pytest.mark.parametrize(
    ("block", "match"),
    [
        ({"shape": "table"}, "table_selector"),
        ({"shape": "listing", "table_selector": "t"}, "listing"),
        ({"shape": "table", "table_selector": "t", "extra": 1}, "Extra inputs"),
    ],
)
def test_config_refusals(block: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        entry("html_table", block)


def test_read_table_alone_gives_the_labels_and_records() -> None:
    e = entry(
        "html_table",
        {"shape": "table", "table_selector": "table#cases", "caption": "Reported cases by county"},
    )
    labels, records = read_table(e, e.source.config(), SIMPLE)  # type: ignore[arg-type]
    assert labels == list(records[0]) and records == published(list(pages(e, Site({URL: SIMPLE})))[0])
