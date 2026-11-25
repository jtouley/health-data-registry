"""One XLSX workbook over HTTPS (e.g. AHEAD's "Download All Data" file on ahead-files.hiv.gov).

No paging: the file is fetched once and its raw bytes are kept. Only the sheets the catalog's `workbook`
layout names are read. Each sheet's column names are on its header row; data rows follow, up to the first
blank row, after which publishers put their notes. Every value is the cell's text exactly as stored in the
file (a number as written, e.g. "13.8"; a shared string resolved to its text); an empty cell is absent (None).

Read with the standard library only. A workbook is a zip of XML parts, so the reader is defensive: it reads
only the parts it needs, refuses a part that inflates past MAX_PART_BYTES, refuses any XML with a DOCTYPE
(no entities, no external references), and refuses part paths that leave the workbook.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
import zlib
from collections.abc import Iterator
from typing import Any
from xml.etree import ElementTree as ET

from health_data_registry.adapters import Fetcher, Page
from health_data_registry.registry import CatalogEntry, Workbook

MAX_PART_BYTES = 64_000_000

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CELL_REF = re.compile(r"([A-Z]{1,3})([0-9]+)")


def _part(z: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = z.getinfo(name)
    except KeyError:
        raise ValueError(f"the workbook has no part {name}") from None
    if info.file_size > MAX_PART_BYTES:
        raise ValueError(f"the workbook part {name} is larger than {MAX_PART_BYTES:,} bytes; it was not read")
    try:
        with z.open(info) as fh:
            data = fh.read(MAX_PART_BYTES + 1)
    except (zipfile.BadZipFile, zlib.error, EOFError):
        raise ValueError(f"the workbook part {name} is damaged (it does not match its checksum)") from None
    if len(data) > MAX_PART_BYTES:
        raise ValueError(f"the workbook part {name} is larger than {MAX_PART_BYTES:,} bytes; it was not read")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError(f"the workbook part {name} declares a DOCTYPE or entity; it was not read")
    return data


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(_part(z, name))  # noqa: S314 - bounded size, DOCTYPE and entities refused above
    except ET.ParseError as e:
        raise ValueError(f"the workbook part {name} is not well-formed XML: {e}") from None


def _sheet_paths(z: zipfile.ZipFile) -> dict[str, str]:
    """Each sheet's name and the zip path of its XML part, from the workbook and its relationships."""
    rels = {
        r.get("Id", ""): r.get("Target", "")
        for r in _xml(z, "xl/_rels/workbook.xml.rels").iter(f"{_PKG}Relationship")
    }
    out: dict[str, str] = {}
    for s in _xml(z, "xl/workbook.xml").iter(f"{_MAIN}sheet"):
        target = rels.get(s.get(f"{_REL}id", ""), "")
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(f"xl/{target}")
        if not target or ".." in path.split("/"):
            raise ValueError(f"the workbook's sheet {s.get('name')!r} points outside the workbook")
        out[s.get("name", "")] = path
    return out


def _text(el: ET.Element | None) -> str:
    """A shared or inline string: its own text, or its rich-text runs joined (phonetic hints left out)."""
    if el is None:
        return ""
    own = el.find(f"{_MAIN}t")
    if own is not None:
        return own.text or ""
    return "".join(t.text or "" for r in el.findall(f"{_MAIN}r") for t in r.findall(f"{_MAIN}t"))


def _shared(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    return [_text(si) for si in _xml(z, "xl/sharedStrings.xml").findall(f"{_MAIN}si")]


def _column(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _cell(c: ET.Element, shared: list[str]) -> str | None:
    kind = c.get("t", "n")
    if kind == "inlineStr":
        return _text(c.find(f"{_MAIN}is"))
    v = c.find(f"{_MAIN}v")
    if v is None or v.text is None:
        return None
    if kind == "s":
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            raise ValueError(f"cell {c.get('r')} names a shared string the workbook lacks") from None
    return v.text  # a number, boolean, formula result or error, exactly as stored


def _rows(z: zipfile.ZipFile, path: str, shared: list[str]) -> dict[int, dict[int, str]]:
    """The sheet's non-empty cells by row number and column index."""
    rows: dict[int, dict[int, str]] = {}
    for row in _xml(z, path).iter(f"{_MAIN}row"):
        cells: dict[int, str] = {}
        for c in row.findall(f"{_MAIN}c"):
            m = _CELL_REF.fullmatch(c.get("r", ""))
            if m is None:
                raise ValueError(f"{path}: a cell has no plain reference (e.g. B3); the layout can't be read")
            value = _cell(c, shared)
            if value is not None and value.strip() != "":
                cells[_column(m.group(1))] = value
        if cells:
            rows[int(row.get("r", "0"))] = cells
    return rows


def _header(name: str, cells: dict[int, str]) -> list[str]:
    width = max(cells) + 1 if cells else 0
    header = [cells.get(i, "").strip() for i in range(width)]
    if not header:
        raise ValueError(f"sheet {name!r} has no header row where the layout says")
    if "" in header:
        raise ValueError(f"sheet {name!r}: a column in the header row has no name")
    if len(set(header)) != len(header):
        raise ValueError(f"sheet {name!r} repeats a column name")
    return header


def read_sheet(
    layout: Workbook, z: zipfile.ZipFile, paths: dict[str, str], shared: list[str], name: str
) -> tuple[list[str], list[dict[str, Any]]]:
    """One sheet's header and its data rows (up to the first blank row), each value as stored."""
    if name not in paths:
        raise ValueError(f"the workbook has no sheet named {name!r} (it has {', '.join(paths)})")
    rows = _rows(z, paths[name], shared)
    header = _header(name, rows.get(layout.header_row, {}))
    if layout.sheet_field in header:
        raise ValueError(f"sheet_field {layout.sheet_field!r} would overwrite a column of sheet {name!r}")
    out: list[dict[str, Any]] = []
    r = layout.header_row + 1
    while r in rows:
        cells = rows[r]
        if max(cells) >= len(header):
            raise ValueError(f"sheet {name!r} row {r} has a value in a column with no name")
        out.append({h: cells.get(i) for i, h in enumerate(header)})
        r += 1
    return header, out


def _open(entry: CatalogEntry, content: bytes) -> tuple[Workbook, zipfile.ZipFile]:
    layout = entry.source.workbook
    if layout is None:  # pragma: no cover - the catalog model requires a layout for https_xlsx
        raise ValueError(f"{entry.id}: no workbook layout in the catalog entry")
    try:
        return layout, zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise ValueError(f"{entry.id}: the file is not an XLSX workbook") from None


def read_workbook(entry: CatalogEntry, content: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    """Every named sheet's rows, each with the sheet's name, and the union of their column names."""
    layout, z = _open(entry, content)
    with z:
        paths, shared = _sheet_paths(z), _shared(z)
        fields: list[str] = [layout.sheet_field]
        records: list[dict[str, Any]] = []
        for name in layout.sheets:
            header, rows = read_sheet(layout, z, paths, shared, name)
            fields += [h for h in header if h not in fields]
            records += [{layout.sheet_field: name, **r} for r in rows]
    return fields, [{f: r.get(f) for f in fields} for r in records]


def records(entry: CatalogEntry, content: bytes) -> list[dict[str, Any]]:
    return read_workbook(entry, content)[1]


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
    """The column names the named sheets serve today, plus the sheet field, for the contract drift check."""
    return set(read_workbook(entry, fetcher(entry.source.api_url).content)[0])
