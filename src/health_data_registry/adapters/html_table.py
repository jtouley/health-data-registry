"""One HTML table on a publisher's page over HTTPS (e.g. UNSD's M49 overview: the table downloadTableEN).

A structural read only: the page's raw bytes are kept, and the table named by the catalog's `table_id` becomes
one record per body row, keyed by that table's own header cells, every value the cell's text with its edge
spaces trimmed. No value is computed, matched or dropped; a row with a different number of cells than the
header is refused rather than guessed into columns. Commented-out cells (<!-- ... -->) are not cells.
"""

from __future__ import annotations

from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Any

from health_data_registry.adapters import Fetcher, Page
from health_data_registry.registry import CatalogEntry


class _Table(HTMLParser):
    """Collects the header and body rows of the one table whose id is `table_id` (not nested tables)."""

    def __init__(self, table_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.table_id = table_id
        self.depth = 0  # tables open inside the wanted one, the wanted one included
        self.found = False
        self.section = ""
        self.rows: list[tuple[str, list[str]]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self.depth:
                self.depth += 1
            elif dict(attrs).get("id") == self.table_id and not self.found:
                self.found, self.depth = True, 1
            return
        if self.depth != 1:
            return
        if tag in ("thead", "tbody"):
            self.section = tag
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self.depth:
            self.depth -= 1
            return
        if self.depth != 1:
            return
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append((self.section, self.row))
            self.row = None

    def handle_data(self, data: str) -> None:
        if self.depth == 1 and self.cell is not None:
            self.cell.append(data)


def read_table(entry: CatalogEntry, content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """The header cells and the body rows of the catalogued table, each row keyed by the header."""
    table_id = str(entry.source.table_id)
    parser = _Table(table_id)
    parser.feed(content.decode(entry.source.encoding))
    parser.close()
    if not parser.found:
        raise ValueError(f"{entry.id}: the page has no table with id {table_id!r}")
    heads = [cells for section, cells in parser.rows if section == "thead"]
    if len(heads) != 1:
        raise ValueError(f"{entry.id}: table {table_id!r} must have exactly one header row")
    header = heads[0]
    if not header or len(set(header)) != len(header) or any(not h for h in header):
        raise ValueError(f"{entry.id}: table {table_id!r} has an empty or repeated column name")
    rows = []
    for n, (section, cells) in enumerate(parser.rows, start=1):
        if section == "thead":
            continue
        if len(cells) != len(header):
            raise ValueError(f"{entry.id}: row {n} of table {table_id!r} does not have one cell per column")
        rows.append(dict(zip(header, cells, strict=True)))
    return header, rows


def pages(
    entry: CatalogEntry, fetcher: Fetcher, *, max_rows: int | None = None, fields: list[str] | None = None
) -> Iterator[Page]:
    raw = fetcher(entry.source.api_url)
    _header, rows = read_table(entry, raw.content)
    recs: list[dict[str, Any]] = [dict(r) for r in rows]
    if fields:
        keep = set(fields)
        recs = [{k: v for k, v in r.items() if k in keep} for r in recs]
    if max_rows is not None:
        recs = recs[: max(0, max_rows)]
    yield Page(fetch=raw, records=recs)


def publisher_fields(entry: CatalogEntry, fetcher: Fetcher) -> set[str]:
    """The column names the table has today, for the contract drift check."""
    header, _rows = read_table(entry, fetcher(entry.source.api_url).content)
    return set(header)
