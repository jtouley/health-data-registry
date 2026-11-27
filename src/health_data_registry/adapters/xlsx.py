"""Read one sheet of an xlsx workbook as stored, never as displayed (hard rule 1).

An xlsx cell has a stored value and a number format; what a spreadsheet shows is the format applied to the value.
The two differ in ways that change a number: 0.123 shown as 12.3%, 45292 shown as 1/1/2024, 2.345 shown as 2.3.
This reader returns the stored value as its text in the file, and the cell's number format beside it, so the
contract can declare what a column holds and staging can type dates and percentages. A formula cell gives its
cached value; one with no cached value (a workbook never recalculated) is refused, never recalculated here.

The workbook is a zip of XML parts: it is opened through ``archive.SafeZip`` (the same size, ratio and entry caps as
any archive), and every XML part is parsed by defusedxml with DTDs, entities and external references forbidden.
A sheet is chosen by name only; a hidden sheet is refused, as is a merged cell in the header row.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from defusedxml import ElementTree as SafeET

from health_data_registry.adapters import LayoutError
from health_data_registry.adapters.archive import Caps, SafeZip

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
# ECMA-376 Part 1, 18.8.30: the number formats every workbook has without declaring them.
BUILTIN_FORMATS = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%", 11: "0.00E+00",
    12: "# ?/?", 13: "# ??/??", 14: "mm-dd-yy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy", 18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss", 22: "m/d/yy h:mm", 37: "#,##0 ;(#,##0)",
    38: "#,##0 ;[Red](#,##0)", 39: "#,##0.00;(#,##0.00)", 40: "#,##0.00;[Red](#,##0.00)", 45: "mm:ss",
    46: "[h]:mm:ss", 47: "mmss.0", 48: "##0.0E+0", 49: "@",
}  # fmt: skip
_CELL_REF = re.compile(r"^([A-Z]{1,3})(\d{1,7})$")
MAX_COLUMNS = 2_000  # a published table is far narrower; a wider sheet is refused rather than expanded
# The grid is built dense (rows x columns), so its size is capped, not only its corner: two cells at A1 and
# BXX1000000 pass both caps above yet would make two billion cells.
MAX_GRID_CELLS = 20_000_000


class XlsxRefused(LayoutError):
    """The workbook can't be read as stored without guessing; nothing was read from it."""


@dataclass(frozen=True)
class Cell:
    value: str | None  # the stored value as written in the file; None for an empty cell
    number_format: str  # the cell's number format code ("General" when it has none)


def _xml(zf: SafeZip, name: str) -> Any:
    try:
        return SafeET.fromstring(zf.read(name), forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except LayoutError:
        raise
    except Exception as e:  # noqa: BLE001 - defusedxml's and expat's errors, whatever their class: a refusal
        raise XlsxRefused(f"the workbook part {name} is not plain XML ({type(e).__name__})") from e


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _text(el: Any) -> str:
    """A string item's text: its <t>, or its rich-text runs' <t>s joined (phonetic runs left out)."""
    direct = el.find(f"{NS}t")
    if direct is not None:
        return str(direct.text or "")
    return "".join(str(t.text or "") for r in el.findall(f"{NS}r") for t in r.findall(f"{NS}t"))


def _sheet_part(zf: SafeZip, sheet: str) -> str:
    wb = _xml(zf, "xl/workbook.xml")
    found = [s for s in wb.iter(f"{NS}sheet") if s.get("name") == sheet]
    if len(found) > 1:
        raise XlsxRefused(f"the workbook has {len(found)} sheets named {sheet!r}")
    if not found:
        names = [str(s.get("name")) for s in wb.iter(f"{NS}sheet")]
        raise XlsxRefused(f"the workbook has no sheet named {sheet!r} (it has {', '.join(names)})")
    if found[0].get("state") in ("hidden", "veryHidden"):
        raise XlsxRefused(f"the sheet {sheet!r} is hidden in the workbook, so it is not read")
    rid = found[0].get(f"{REL_NS}id")
    rels = _xml(zf, "xl/_rels/workbook.xml.rels")
    target = next(
        (r.get("Target") for r in rels.iter(f"{PKG_REL_NS}Relationship") if r.get("Id") == rid), None
    )
    if not target:
        raise XlsxRefused(f"the workbook does not say where the sheet {sheet!r} is")
    path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
    if not zf.has(path):
        raise XlsxRefused(f"the workbook's sheet {sheet!r} points at a missing part")
    return path


def _int(text: object, what: str) -> int:
    """A non-negative whole number from the workbook, or a refusal (never a bare ValueError)."""
    try:
        n = int(str(text))
    except ValueError as e:
        raise XlsxRefused(f"the workbook's {what} is {text!r}, not a whole number") from e
    if n < 0:
        raise XlsxRefused(f"the workbook's {what} is {n}, below zero")
    return n


def _formats(zf: SafeZip) -> list[str]:
    """The number format code of each cell style, by style index."""
    if not zf.has("xl/styles.xml"):
        return []
    styles = _xml(zf, "xl/styles.xml")
    custom: dict[int, str] = {}
    for f in styles.iter(f"{NS}numFmt"):
        fid = _int(f.get("numFmtId"), "number format id")
        if fid in custom:
            raise XlsxRefused(f"the workbook declares number format {fid} twice")
        custom[fid] = str(f.get("formatCode"))
    xfs = styles.find(f"{NS}cellXfs")
    out = []
    for xf in xfs.findall(f"{NS}xf") if xfs is not None else []:
        fid = _int(xf.get("numFmtId") or 0, "number format id")
        out.append(custom.get(fid) or BUILTIN_FORMATS.get(fid) or f"builtin:{fid}")
    return out


def read_sheet(
    content: bytes, sheet: str, caps: Caps, *, max_rows: int
) -> tuple[list[list[Cell]], list[str]]:
    """The sheet's rows of cells (stored values with number formats), and its merged ranges ("A1:C1")."""
    zf = SafeZip(content, caps, what="the workbook")
    part = _sheet_part(zf, sheet)
    shared = (
        [_text(si) for si in _xml(zf, "xl/sharedStrings.xml").iter(f"{NS}si")]
        if zf.has("xl/sharedStrings.xml")
        else []
    )
    formats = _formats(zf)
    root = _xml(zf, part)
    rows: dict[int, dict[int, Cell]] = {}
    for row in root.iter(f"{NS}row"):
        for c in row.findall(f"{NS}c"):
            m = _CELL_REF.match(str(c.get("r") or ""))
            if m is None:
                raise XlsxRefused(f"the sheet {sheet!r} has a cell without a plain reference")
            r, col = int(m.group(2)) - 1, _col(m.group(1))
            if col >= MAX_COLUMNS:
                raise XlsxRefused(f"the sheet {sheet!r} has a cell past column {MAX_COLUMNS:,}")
            if r >= max_rows:
                raise XlsxRefused(f"the sheet {sheet!r} has more than {max_rows:,} rows")
            style = _int(c.get("s") or 0, f"style of {c.get('r')}")
            if formats and style >= len(formats):
                raise XlsxRefused(f"the sheet {sheet!r} gives {c.get('r')} a style the workbook doesn't have")
            fmt = formats[style] if style < len(formats) else "General"
            if col in rows.get(r, {}):
                raise XlsxRefused(f"the sheet {sheet!r} has two cells at {c.get('r')}")
            rows.setdefault(r, {})[col] = Cell(value=_value(c, shared, sheet), number_format=fmt)
    merged = [str(m.get("ref")) for m in root.iter(f"{NS}mergeCell")]
    width = 1 + max((c for cells in rows.values() for c in cells), default=-1)
    height = 1 + max(rows, default=-1)
    if width * height > MAX_GRID_CELLS:
        raise XlsxRefused(
            f"the sheet {sheet!r} spans {height:,} rows by {width:,} columns, more cells than a published table "
            "has; it is not read"
        )
    empty = Cell(value=None, number_format="General")
    grid = [[rows.get(r, {}).get(c, empty) for c in range(width)] for r in range(height)]
    return grid, merged


def _value(c: Any, shared: Sequence[str], sheet: str) -> str | None:
    kind = c.get("t") or "n"
    v = c.find(f"{NS}v")
    if kind == "inlineStr":
        is_ = c.find(f"{NS}is")
        return _text(is_) if is_ is not None else ""
    if c.find(f"{NS}f") is not None and v is None:
        raise XlsxRefused(
            f"the sheet {sheet!r} has a formula in {c.get('r')} with no saved value; it is never recalculated "
            "here, so the publisher's file must be saved with its values"
        )
    if v is None:
        return None
    text = str(v.text or "")
    if kind == "s":
        try:
            return shared[int(text)]
        except (ValueError, IndexError) as e:
            raise XlsxRefused(f"the sheet {sheet!r} points at a missing shared string in {c.get('r')}") from e
    return text  # n, b, e, str, d: the stored text as written


def merged_in_row(merged: Sequence[str], row: int) -> list[str]:
    """The merged ranges that touch the 0-based row ``row``."""
    hits = []
    for ref in merged:
        ends = [(_CELL_REF.match(p)) for p in ref.split(":")]
        rows = [int(m.group(2)) - 1 for m in ends if m is not None]
        if rows and min(rows) <= row <= max(rows):
            hits.append(ref)
    return hits
