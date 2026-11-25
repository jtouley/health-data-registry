"""html_table adapter type: tables on server-rendered HTML pages, or a listing of item pages.

Shape ``table``: the one ``<table>`` the selector names on the page at ``api_url`` (an optional caption must match
exactly). The type makes one request per call; a redirect is followed, as a request of its own, only to a page
inside the source's own scope (at most ``MAX_HOPS``). Header cells give the field names, every body cell is a record value as printed (footnote markers
kept). A header cell spanning columns or rows is joined top-down with ``header_separator`` when the catalog
declares one, else refused; a body cell that spans is refused. Nested tables are not read into their parent.

Shape ``listing``: a paginated search listing and one page per item, crawled by the resumable runner in
listing.py; this module reads one listing page (its item links and its own total) and one item page (its fields,
by selector, or a ``parse`` extension). Every record carries the provenance of the page it was read from
(``document_url``, ``document_sha256``, ``document_retrieved_at``).

Every field the page has is handed on, not only the ones the contract reads (hard rule 15: the raw layer keeps what
the publisher sent; staging reads the contract's fields). Cell text is the text a browser shows: runs of
whitespace collapse to one space (html.Element.text); nothing else about a value is changed.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from health_data_registry.access import ScopeRefused, normalise, redact_url
from health_data_registry.adapters import (
    DOCUMENT_FIELDS,
    Fetcher,
    LayoutError,
    Page,
    Read,
    parse_page,
    with_document,
)
from health_data_registry.adapters.extensions import resolved
from health_data_registry.adapters.html import Element, select
from health_data_registry.fetch import FetchRefused
from health_data_registry.registry import CatalogEntry
from health_data_registry.source_config import HtmlTableConfig, ListingConfig, match_whole
from health_data_registry.store import RawFetch

MAX_SPAN = 100
MAX_HOPS = 3  # redirects followed for one table page, each a request of its own and each checked for scope


def get_following(entry: CatalogEntry, get: Fetcher, url: str) -> RawFetch:
    """``url``, following a redirect only into the source's own scope (the scoped getter checks each hop)."""
    for _hop in range(MAX_HOPS + 1):
        try:
            return get(url)
        except FetchRefused as e:
            if e.code != "too_many_redirects" or e.location is None:
                raise
            url = e.location
        except ScopeRefused as e:
            raise LayoutError(
                f"{entry.id}: the page redirects to {redact_url(url)}, outside this source's pages"
            ) from e
    raise LayoutError(f"{entry.id}: the page redirected more than {MAX_HOPS} times")


def _closest_table(el: Element) -> Element | None:
    p = el.parent
    while p is not None and p.tag != "table":
        p = p.parent
    return p


def _rows(table: Element) -> list[Element]:
    return [tr for tr in table.iter() if tr.tag == "tr" and _closest_table(tr) is table]


def _cells(tr: Element) -> list[Element]:
    return [c for c in tr.children if isinstance(c, Element) and c.tag in ("td", "th")]


def _span(cell: Element, name: str) -> int:
    try:
        n = int(cell.attrs.get(name, "1") or "1")
    except ValueError as e:
        raise LayoutError(f"a table cell has a {name} that is not a number") from e
    if not 1 <= n <= MAX_SPAN:
        raise LayoutError(f"a table cell has a {name} of {n}")
    return n


def _header(entry: CatalogEntry, cfg: HtmlTableConfig, rows: list[Element]) -> list[str]:
    slots: dict[tuple[int, int], str] = {}
    spans = False
    for r, tr in enumerate(rows):
        c = 0
        for cell in _cells(tr):
            while (r, c) in slots:
                c += 1
            cs, rs = _span(cell, "colspan"), _span(cell, "rowspan")
            spans = spans or cs > 1 or rs > 1
            for dr in range(rs):
                for dc in range(cs):
                    slots[(r + dr, c + dc)] = cell.text()
            c += cs
    if spans and cfg.header_separator is None:
        raise LayoutError(f"{entry.id}: the table's header has cells spanning columns or rows, and the catalog "
                          "declares no header_separator to join them")  # fmt: skip
    width = 1 + max((c for _r, c in slots), default=-1)
    labels = []
    for c in range(width):
        parts: list[str] = []
        for r in range(len(rows)):
            text = slots.get((r, c), "")
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        labels.append((cfg.header_separator or "").join(parts))
    if any(not label for label in labels) or len(set(labels)) != len(labels):
        raise LayoutError(f"{entry.id}: the table's header has an empty or repeated label")
    return labels


def read_table(
    entry: CatalogEntry, cfg: HtmlTableConfig, content: bytes
) -> tuple[list[str], list[dict[str, str]]]:
    """The labels and records of the one table the config names."""
    if cfg.table_selector is None:  # the config model requires one for shape table
        raise ValueError(f"{entry.id}: an html_table table needs a table_selector")
    doc = parse_page(content)
    found = [t for t in select(doc, cfg.table_selector) if t.tag == "table"]
    if len(found) != 1:
        raise LayoutError(
            f"{entry.id}: the page has {len(found)} tables matching {cfg.table_selector!r}, not one"
        )
    table = found[0]
    if cfg.caption is not None:
        caps = [c.text() for c in table.children if isinstance(c, Element) and c.tag == "caption"]
        if caps != [cfg.caption]:
            raise LayoutError(
                f"{entry.id}: the table's caption is {caps[0] if caps else 'missing'!r}, not {cfg.caption!r}"
            )
    rows = _rows(table)
    head = [tr for tr in rows if tr.parent is not None and tr.parent.tag == "thead"] or rows[:1]
    labels = _header(entry, cfg, head)
    records = []
    for tr in rows:
        if tr in head:
            continue
        cells = _cells(tr)
        if any(_span(c, "colspan") > 1 or _span(c, "rowspan") > 1 for c in cells):
            raise LayoutError(f"{entry.id}: a body row of the table has a cell spanning columns or rows")
        if len(cells) != len(labels):
            raise LayoutError(f"{entry.id}: a body row has {len(cells)} cells for {len(labels)} columns")
        records.append({label: cell.text() for label, cell in zip(labels, cells, strict=True)})
    return labels, records


def pages(read: Read) -> Iterator[Page]:
    cfg: HtmlTableConfig = read.config
    if cfg.shape == "listing":
        from health_data_registry.listing import crawl_pages

        yield from crawl_pages(read)
        return
    raw = get_following(read.entry, read.get, read.entry.source.api_url)
    _labels, recs = read_table(read.entry, cfg, raw.content)
    if read.max_rows is not None:
        recs = recs[: max(0, read.max_rows)]
    yield Page(fetch=raw, records=with_document(recs, raw))


def publisher_fields(entry: CatalogEntry, get: Fetcher) -> set[str]:
    cfg = entry.source.config()
    if not isinstance(cfg, HtmlTableConfig):  # registered for html_table only
        raise TypeError(f"{entry.id}: read as {entry.source.kind}, not html_table")
    if cfg.listing is not None:
        return set(cfg.listing.fields) | set(DOCUMENT_FIELDS)
    labels, _recs = read_table(entry, cfg, get_following(entry, get, entry.source.api_url).content)
    return set(labels) | set(DOCUMENT_FIELDS)


# ------------------------------------------------------------------------------------------------- listing


@dataclass(frozen=True)
class ListingItem:
    """One item link on a listing page: its normalised URL, the link's text and the year its path names."""

    url: str
    text: str
    year: int | None = None


def listing_page(
    entry: CatalogEntry, cfg: ListingConfig, content: bytes, url: str
) -> tuple[int, list[ListingItem]]:
    """A listing page's own total and its item links (absolute, normalised), each one checked whole against the
    item path pattern. A total that can't be read, or a link outside the pattern, is refused."""
    doc = parse_page(content)
    counts = [el.text() for el in select(doc, cfg.results_count_selector)]
    totals = [m for t in counts if (m := match_whole(cfg.results_count_pattern, t)) is not None]
    if len(totals) != 1:
        raise LayoutError(f"{entry.id}: the listing at {url} does not show its number of results")
    total = int(totals[0].group(1).replace(",", ""))
    items: list[ListingItem] = []
    for a in select(doc, cfg.item_link_selector):
        try:
            link = normalise(a.attrs.get("href", ""), url)
        except ScopeRefused as e:
            raise LayoutError(f"{entry.id}: the listing at {url} links an item it can't read ({e})") from e
        m = match_whole(cfg.item_path_pattern, unquote(urlsplit(link).path))
        if m is None:
            raise LayoutError(f"{entry.id}: the listing at {url} links {link}, which is not an item page")
        year = int(m.group(cfg.item_year_group)) if cfg.item_year_group is not None else None
        if link not in [i.url for i in items]:
            items.append(ListingItem(link, a.text(), year))
    return total, items


def check_item(entry: CatalogEntry, cfg: ListingConfig, item: ListingItem, year: int,
               records: list[dict[str, Any]]) -> None:  # fmt: skip
    """Listing consistency: the item's year is the year asked for, and the listing's number for it is the number
    its own page prints. Either disagreement is refused: the listing and its pages don't describe the same item."""
    if item.year is not None and item.year != year:
        raise LayoutError(
            f"{entry.id}: the {year} listing links {item.url}, an item of {item.year}; the listing ignored its year"
        )
    rule = cfg.item_number
    if rule is None:
        return
    listed = match_whole(rule.link_text_pattern, item.text.strip())
    values = [r.get(rule.field) for r in records]
    printed = [match_whole(rule.field_pattern, str(v).strip()) for v in values if v is not None]
    page_numbers = {p.group(1) for p in printed if p is not None}
    listed_number = listed.group(1) if listed is not None else None
    if page_numbers != ({listed_number} if listed_number is not None else set()):
        shown = ", ".join(sorted(page_numbers)) or "none"
        raise LayoutError(
            f"{entry.id}: the listing numbers {item.url} {listed_number or 'not at all'}, but the page itself "
            f"prints {shown}; the listing and its pages disagree"
        )


def item_records(entry: CatalogEntry, cfg: ListingConfig, content: bytes, url: str) -> list[dict[str, Any]]:
    """One item page's record: the declared fields' text (a field the page doesn't have is empty; one it has
    twice is refused), then each ``parse`` extension's fields, in the catalog's order. Each extension reads one
    record from the page; a field two of them, or one and a selector, both read is refused. A page with none of
    the declared fields at all is refused: the site was redesigned, and a crawl of empty records is no crawl."""
    doc = parse_page(content)
    rec: dict[str, Any] = {}
    for name, selector in cfg.fields.items():
        hits = select(doc, selector)
        if len(hits) > 1:
            raise LayoutError(f"{entry.id}: {url} has {len(hits)} elements for {name}, not one")
        rec[name] = hits[0].text() if hits else None
    if cfg.fields and all(rec[name] is None for name in cfg.fields):
        raise LayoutError(
            f"{entry.id}: {url} has none of the fields the catalog names ({', '.join(cfg.fields)}); the page's "
            "layout changed"
        )
    for ext, args in resolved(entry.source.extensions, "parse"):
        got = [dict(r) for r in ext.run(args, content, url)]
        if len(got) != 1:
            raise LayoutError(f"{entry.id}: extension {ext.name} read {len(got)} records from {url}, not one")
        twice = sorted(set(got[0]) & set(rec))
        if twice:
            raise LayoutError(
                f"{entry.id}: extension {ext.name} reads {', '.join(twice)}, which is already read"
            )
        rec.update(got[0])
    return [rec]
