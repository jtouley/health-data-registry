"""pdf_table adapter type: text PDFs linked from an index page, their tables rebuilt from geometry (pdf_tables.py).

The index page is the catalog entry's ``api_url``. Every link on it is resolved against the page, normalised, and
kept only when its whole path matches ``index_link_pattern`` and it is inside the source's own scope; more matching
links than ``max_documents`` means the page changed, and is refused. Each PDF's raw bytes are kept; its records are
one per table row, keyed by the printed group and label headers and each value column's printed header, every
value exactly as printed (thousands separators and suppression marks kept). What a row means (a total row, a
period, the area it covers) is decided in staging and the contract, never here.

Facts a report prints once (its title year, the area it says it covers) are read by the source's ``parse``
extensions from the document's text, as printed, and added to each of its rows; what they mean is staging's and the
contract's business.

A live read takes at most ``live.max_documents`` reports (newest first by the year in the file name, when the
catalog says so), and never more than the hosted deadline leaves time for (access.clamp_live).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from urllib.parse import unquote, urlsplit

from health_data_registry.access import ScopeRefused, clamp_live, in_scope, normalise
from health_data_registry.adapters import Fetcher, LayoutError, Page, Read
from health_data_registry.adapters.extensions import resolved
from health_data_registry.adapters.html import parse_html
from health_data_registry.adapters.pdf_tables import PageGeometry, TableSpec, lines, parse_tables, read_pdf
from health_data_registry.registry import CatalogEntry
from health_data_registry.source_config import ExtensionUse, PdfTableConfig, compile_pattern, match_whole

LIVE_SECONDS_PER_DOCUMENT = 20.0  # a paced request plus parsing: what one report costs a live read
_YEAR_IN_NAME = re.compile(r"(?<!\d)((?:19|20)\d\d)(?!\d)")


def spec_of(cfg: PdfTableConfig) -> TableSpec:
    t = cfg.table
    return TableSpec(
        group_header=t.group_header,
        label_header=t.label_header,
        suppression_marks=frozenset(t.suppression_marks),
        trailing_total=compile_pattern(t.trailing_total, ignore_case=True) if t.trailing_total else None,
        allow_blank=t.allow_blank,
        header_words=frozenset(w.lower() for w in t.header_words),
    )


def documents(entry: CatalogEntry, cfg: PdfTableConfig, index: bytes, base: str) -> list[str]:
    """The report links on the index page, in page order, each in scope and matching the pattern whole."""
    found: list[str] = []
    for el in parse_html(index).iter():
        if el.tag != "a" or "href" not in el.attrs:
            continue
        try:
            url = normalise(el.attrs["href"], base)
        except ScopeRefused:
            continue
        path = unquote(urlsplit(url).path)
        if match_whole(cfg.index_link_pattern, path) and in_scope(entry.source, url) and url not in found:
            found.append(url)
    if not found:
        raise LayoutError(
            f"{entry.id}: the index page links no report matching its pattern; the page changed"
        )
    if len(found) > cfg.max_documents:
        raise LayoutError(
            f"{entry.id}: the index page links {len(found)} reports, more than {cfg.max_documents}"
        )
    return found


def _newest_first(urls: list[str]) -> list[str]:
    def year(u: str) -> int:
        years = _YEAR_IN_NAME.findall(unquote(urlsplit(u).path.rsplit("/", 1)[-1]))
        return max((int(y) for y in years), default=0)

    return sorted(urls, key=year, reverse=True)


def document_text(geometry: Sequence[PageGeometry]) -> str:
    """The document's words as lines in reading order, top to bottom and left to right, page after page."""
    return "\n".join(" ".join(w.text for w in line) for page in geometry for line in lines(page.words))


def printed_facts(uses: Sequence[ExtensionUse], text: str, url: str) -> dict[str, str | None]:
    """What the document prints once (its title year, the area it says it covers), by the source's ``parse``
    extensions, each value as printed. Each extension is given the document's text and returns one record."""
    facts: dict[str, str | None] = {}
    for ext, args in resolved(uses, "parse"):
        got = [dict(r) for r in ext.run(args, text.encode("utf-8"), url)]
        if len(got) != 1:
            raise LayoutError(
                f"extension {ext.name} read {len(got)} sets of printed facts from {url}, not one"
            )
        for name, value in got[0].items():
            if name in facts:
                raise LayoutError(f"two extensions both read a printed fact named {name!r}")
            facts[name] = value
    return facts


def records_of(
    cfg: PdfTableConfig, content: bytes, *, uses: Sequence[ExtensionUse] = (), url: str = ""
) -> list[dict[str, str | None]]:
    spec = spec_of(cfg)
    geometry = read_pdf(content, max_bytes=cfg.max_pdf_bytes, max_pages=cfg.max_pdf_pages)
    rows = parse_tables(geometry, spec)
    if not rows:
        raise LayoutError(
            f"the report has no table headed {cfg.table.group_header!r} and {cfg.table.label_header!r}; its layout "
            "changed"
        )
    out = []
    for row in rows:
        rec: dict[str, str | None] = {
            cfg.table.group_header: row.group or None,
            cfg.table.label_header: row.label,
        }
        for cell in row.cells:
            if cell.column in rec:
                raise LayoutError(f"a value column is headed {cell.column!r}, like a label column")
            rec[cell.column] = cell.raw
        out.append(rec)
    facts = printed_facts(uses, document_text(geometry), url) if uses else {}
    clash = sorted(set(facts) & {k for r in out for k in r})
    if clash:
        raise LayoutError(f"a printed fact is named like a table column ({', '.join(clash)})")
    return [{**r, **facts} for r in out]


def pages(read: Read) -> Iterator[Page]:
    cfg: PdfTableConfig = read.config
    base = read.entry.source.api_url
    index = read.get(base)
    urls = documents(read.entry, cfg, index.content, base)
    if read.live:
        ordered = _newest_first(urls) if cfg.live.order_by == "year_in_filename" else urls
        urls = ordered[: clamp_live(cfg.live.max_documents, LIVE_SECONDS_PER_DOCUMENT)]
    left = read.max_rows
    for url in urls:
        raw = read.get(url)
        recs = records_of(cfg, raw.content, uses=read.entry.source.extensions, url=raw.url)
        if read.fields is not None:
            keep = set(read.fields)
            recs = [{k: v for k, v in r.items() if k in keep} for r in recs]
        if left is not None:
            recs, left = recs[: max(0, left)], max(0, left - len(recs))
        yield Page(fetch=raw, records=list(recs))
        if left == 0:
            return


def publisher_fields(entry: CatalogEntry, get: Fetcher) -> set[str]:
    """The labels the newest-listed report prints today, for the contract drift check."""
    cfg = entry.source.config()
    assert isinstance(cfg, PdfTableConfig)  # noqa: S101 - registered for pdf_table only
    urls = documents(entry, cfg, get(entry.source.api_url).content, entry.source.api_url)
    newest = get(urls[0])
    return {
        k for r in records_of(cfg, newest.content, uses=entry.source.extensions, url=newest.url) for k in r
    }
