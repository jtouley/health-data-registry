"""https_file adapter type: one file over HTTPS, csv or xlsx, alone or as one named member of a zip.

The raw store keeps the downloaded file itself (the zip, not the extracted member); the manifest records the
member read and its own sha256. Records are keyed by the header row's labels (``header_row``, or a ``locate``
extension such as header_row_finder), every value as stored: CSV text as written, and for xlsx the cell's stored
value with its number format beside it under ``<label> (number format)``, never the display string (xlsx.py).
Footer rows the config names (``skip_footer_rows``) are left out; nothing else is dropped, renamed or computed.

Refused rather than guessed: a file whose sha256 is not the pinned ``expected_sha256``, an empty or repeated
header label, a CSV line with the wrong number of values, a merged cell in an xlsx header row, more rows than
``max_rows``, and anything archive.py or xlsx.py refuses.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Iterator

from health_data_registry.adapters import Fetcher, LayoutError, Page, Read
from health_data_registry.adapters.archive import Caps, read_member
from health_data_registry.adapters.extensions import resolved
from health_data_registry.adapters.xlsx import merged_in_row, read_sheet
from health_data_registry.registry import CatalogEntry
from health_data_registry.source_config import HttpsFileConfig

FORMAT_SUFFIX = " (number format)"


def _caps(cfg: HttpsFileConfig) -> Caps:
    return Caps(cfg.max_uncompressed_bytes, cfg.max_ratio, cfg.max_members)


def _file_bytes(entry: CatalogEntry, cfg: HttpsFileConfig, content: bytes) -> tuple[bytes, dict[str, str]]:
    if cfg.zip_member is None:
        return content, {}
    data = read_member(content, cfg.zip_member, _caps(cfg), container=cfg.format == "xlsx")
    return data, {"zip_member": cfg.zip_member, "member_sha256": hashlib.sha256(data).hexdigest()}


def _grid(
    entry: CatalogEntry, cfg: HttpsFileConfig, data: bytes
) -> tuple[list[list[str | None]], list[list[str]], list[str]]:
    """Cell values, number formats (xlsx; empty for csv) and merged ranges (xlsx)."""
    if cfg.format == "csv":
        try:
            text = data.decode(cfg.encoding)
        except UnicodeDecodeError as e:
            raise LayoutError(f"{entry.id}: the file is not {cfg.encoding} text") from e
        rows: list[list[str | None]] = []
        for row in csv.reader(io.StringIO(text, newline="")):
            rows.append(list(row))
            if len(rows) > cfg.max_rows + cfg.header_row + cfg.skip_footer_rows:
                raise LayoutError(f"{entry.id}: the file has more than {cfg.max_rows:,} rows")
        return rows, [], []
    assert cfg.sheet is not None  # noqa: S101 - the config model requires a sheet for xlsx
    cells, merged = read_sheet(
        data, cfg.sheet, _caps(cfg), max_rows=cfg.max_rows + cfg.header_row + cfg.skip_footer_rows
    )
    return [[c.value for c in r] for r in cells], [[c.number_format for c in r] for r in cells], merged


def _header_index(entry: CatalogEntry, cfg: HttpsFileConfig, grid: list[list[str | None]]) -> int:
    found = resolved(entry.source.extensions, "locate")
    if not found:
        return cfg.header_row - 1
    ext, args = found[0]
    return int(ext.run(args, [[c or "" for c in row] for row in grid]).header_row)


def read_records(
    entry: CatalogEntry, cfg: HttpsFileConfig, content: bytes
) -> tuple[list[str], list[dict[str, str | None]], dict[str, str]]:
    """The header labels, the records and the provenance of one downloaded file."""
    data, provenance = _file_bytes(entry, cfg, content)
    grid, formats, merged = _grid(entry, cfg, data)
    h = _header_index(entry, cfg, grid)
    if h >= len(grid):
        raise LayoutError(f"{entry.id}: the file has no row {h + 1} to read its header from")
    if merged_in_row(merged, h):
        raise LayoutError(
            f"{entry.id}: the header row has merged cells ({', '.join(merged_in_row(merged, h))})"
        )
    header = [(c or "").strip() for c in grid[h]]
    while header and not header[-1]:
        header.pop()  # trailing empty cells after the last label
    if not header or any(not label for label in header):
        raise LayoutError(f"{entry.id}: the header row has an empty label")
    if len(set(header)) != len(header):
        raise LayoutError(f"{entry.id}: the header row repeats a label")
    end = len(grid) - cfg.skip_footer_rows
    body = range(h + 1, end)
    if len(body) > cfg.max_rows:
        raise LayoutError(f"{entry.id}: the file has more than {cfg.max_rows:,} rows")
    records: list[dict[str, str | None]] = []
    for i in body:
        row = grid[i]
        if cfg.format == "csv" and len(row) != len(grid[h]):
            raise LayoutError(f"{entry.id}: line {i + 1} does not have one value per column")
        if any(v not in (None, "") for v in row[len(header) :]):
            raise LayoutError(f"{entry.id}: row {i + 1} has a value past the last labelled column")
        rec = {label: row[j] if j < len(row) else None for j, label in enumerate(header)}
        if cfg.format == "xlsx":
            fmts = formats[i]
            rec.update(
                {
                    label + FORMAT_SUFFIX: fmts[j] if j < len(fmts) else "General"
                    for j, label in enumerate(header)
                }
            )
        records.append(rec)
    return header, records, provenance


def _download(read_entry: CatalogEntry, cfg: HttpsFileConfig, get: Fetcher) -> tuple[bytes, Page]:
    raw = get(read_entry.source.api_url)
    if cfg.expected_sha256 is not None and raw.sha256 != cfg.expected_sha256:
        raise LayoutError(
            f"{read_entry.id}: the publisher's file changed (sha256 {raw.sha256[:12]}..., not the pinned "
            f"{cfg.expected_sha256[:12]}...); a maintainer reviews it before it is read"
        )
    return raw.content, Page(fetch=raw, records=[])


def pages(read: Read) -> Iterator[Page]:
    cfg: HttpsFileConfig = read.config
    content, page = _download(read.entry, cfg, read.get)
    header, recs, provenance = read_records(read.entry, cfg, content)
    if read.fields is not None:
        keep = set(read.fields)
        recs = [
            {k: v for k, v in r.items() if k in keep or k.removesuffix(FORMAT_SUFFIX) in keep} for r in recs
        ]
    if read.max_rows is not None:
        recs = recs[: max(0, read.max_rows)]
    yield Page(fetch=page.fetch, records=list(recs), provenance=provenance)


def publisher_fields(entry: CatalogEntry, get: Fetcher) -> set[str]:
    cfg = entry.source.config()
    assert isinstance(cfg, HttpsFileConfig)  # noqa: S101 - registered for https_file only
    content, _page = _download(entry, cfg, get)
    header, _recs, _prov = read_records(entry, cfg, content)
    return set(header) | ({h + FORMAT_SUFFIX for h in header} if cfg.format == "xlsx" else set())
