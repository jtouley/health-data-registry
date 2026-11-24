"""One CSV file over HTTPS (e.g. the Census Bureau's keyless population estimate files on www2.census.gov).

No paging: the file is fetched once and its raw bytes are kept. Records are keyed by the file's own column
names, with every value kept as the published text. When the catalog entry declares `reshape.wide_to_long`,
a wide file (POPESTIMATE2020 ... POPESTIMATE2025) becomes one record per row and key (year): a structural
pivot of named columns that copies each value unchanged and computes nothing.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator
from typing import Any

from health_data_registry.adapters import Fetcher, Page
from health_data_registry.registry import CatalogEntry, WideToLong


def read_rows(entry: CatalogEntry, content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """The header and the rows of the file, decoded with the encoding the catalog names."""
    text = content.decode(entry.source.encoding)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = list(reader.fieldnames or [])
    if not header:
        raise ValueError(f"{entry.id}: the file has no header row")
    if len(set(header)) != len(header):
        raise ValueError(f"{entry.id}: the file repeats a column name")
    rows: list[dict[str, str]] = []
    for i, r in enumerate(reader, start=2):
        if None in r or any(v is None for v in r.values()):
            raise ValueError(f"{entry.id}: line {i} does not have one value per column")
        rows.append(r)
    return header, rows


def _keys(spec: WideToLong, header: list[str]) -> list[str]:
    """The keys (e.g. years) for which every stub has a column, in file order; refuses a ragged layout."""
    found: dict[str, set[str]] = {stub: set() for stub in spec.stubs}
    order: list[str] = []
    for col in header:
        for stub in spec.stubs:
            m = re.fullmatch(re.escape(stub) + f"({spec.key_pattern})", col)
            if m:
                found[stub].add(m.group(1))
                if m.group(1) not in order:
                    order.append(m.group(1))
    first = found[spec.stubs[0]]
    if not first:
        raise ValueError(f"no columns named {spec.stubs[0]}<key> in the file")
    if any(v != first for v in found.values()):
        raise ValueError("the stubs do not have the same keys, so the file can't be pivoted row for row")
    return order


def wide_to_long(spec: WideToLong, header: list[str], rows: list[dict[str, str]]) -> list[dict[str, str]]:
    missing = [c for c in spec.keep if c not in header]
    if missing:
        raise ValueError(f"the file no longer has the columns {', '.join(missing)}")
    if spec.key_field in header or spec.key_field in spec.stubs:
        raise ValueError(f"key_field {spec.key_field!r} would overwrite a column of the file")
    keys = _keys(spec, header)
    out: list[dict[str, str]] = []
    for r in rows:
        for k in keys:
            rec = {c: r[c] for c in spec.keep}
            rec[spec.key_field] = k
            for stub in spec.stubs:
                rec[stub] = r[f"{stub}{k}"]
            out.append(rec)
    return out


def records(entry: CatalogEntry, content: bytes) -> list[dict[str, Any]]:
    header, rows = read_rows(entry, content)
    if entry.source.reshape is None:
        return [dict(r) for r in rows]
    return [dict(r) for r in wide_to_long(entry.source.reshape.wide_to_long, header, rows)]


def pages(
    entry: CatalogEntry, fetcher: Fetcher, *, max_rows: int | None = None, fields: list[str] | None = None
) -> Iterator[Page]:
    raw = fetcher(entry.source.api_url)
    recs = records(entry, raw.content)
    if fields:
        # Only the fields the contract reads are handed on; the raw bytes keep everything.
        keep = set(fields)
        recs = [{k: v for k, v in r.items() if k in keep} for r in recs]
    if max_rows is not None:
        recs = recs[: max(0, max_rows)]
    yield Page(fetch=raw, records=recs)


def publisher_fields(entry: CatalogEntry, fetcher: Fetcher) -> set[str]:
    """The field names the file serves today (after the declared pivot), for the contract drift check."""
    header, _rows = read_rows(entry, fetcher(entry.source.api_url).content)
    if entry.source.reshape is None:
        return set(header)
    spec = entry.source.reshape.wide_to_long
    _keys(spec, header)  # refuses when the year columns are gone or ragged
    return {c for c in spec.keep if c in header} | {spec.key_field, *spec.stubs}
