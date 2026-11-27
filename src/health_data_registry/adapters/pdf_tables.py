"""Rebuild report tables from a text PDF's word positions and cell borders.

Local health departments publish case counts as PDF tables exported from Word. The text layer is good,
but wrapped disease names and merged "disease group" cells defeat line-by-line text parsing. This module
works from geometry instead:

1. Value columns are found from where the numeric tokens sit (numbers, or the publisher's suppression mark).
2. Each column's header is the header words stacked above it.
3. Row cells are the bands between the table's horizontal borders, so a wrapped name stays with its row.
4. Group cells are the bands between borders that also cross the group column, so a merged group cell
   applies to every row inside it.

Nothing here changes a value. A token is returned as printed, with its parsed number or a suppression flag,
and anything the geometry cannot settle raises `LayoutError` instead of guessing.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

# A printed value: an integer with optional thousands commas, a decimal, or a suppression mark.
_YEAR = re.compile(r"^(?:19|20)\d\d$")
_NUMBER = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
# Words that only ever appear in a table's header band, never in a data row.
_HEADER_VOCAB = re.compile(r"^(?:Q[1-4]\b.*|YTD|Total|Average|5-Year|Disease|Group|\(.*|.*\)|Q1-Q4)$", re.I)
_LINE_TOLERANCE = 2.0  # points: words whose tops differ by less than this share a line
_MAX_BORDER = 2.0  # points: a filled box thinner than this is a border, not a shaded cell
_RULE_MERGE = 2.5  # points: borders closer than this are one border (Word draws thick borders as two edges)


class LayoutError(ValueError):
    """The page's geometry doesn't match a table this parser understands; nothing is guessed."""


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class Rule:
    """A horizontal border segment."""

    y: float
    x0: float
    x1: float


@dataclass(frozen=True)
class PageGeometry:
    number: int
    width: float
    words: tuple[Word, ...]
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class Column:
    left: float
    right: float
    center: float
    header: str


@dataclass(frozen=True)
class TableSpec:
    """Where a table's label columns are and what marks a suppressed cell."""

    group_header: str = "Group"
    label_header: str = "Disease"
    suppression_marks: frozenset[str] = frozenset({"*"})
    # A grand-total row printed just below the table's last border (no border of its own) is accepted only
    # when its label matches this; any other row outside the borders is an error.
    trailing_total: re.Pattern[str] | None = None
    # Reports that print every cell (0 for none, "*" for suppressed) make a blank cell a misread.
    allow_blank: bool = False


@dataclass(frozen=True)
class Cell:
    column: str
    raw: str | None
    value: float | None
    suppressed: bool


@dataclass(frozen=True)
class TableRow:
    page: int
    group: str
    label: str
    cells: tuple[Cell, ...]
    trailing_total: bool = False  # the grand-total line below the table, outside its borders


@dataclass
class _Header:
    group_center: float
    label_center: float
    label_left: float
    columns: list[Column] = field(default_factory=list)
    data_top: float = 0.0  # top of the first data row under this header
    band_bottom: float = 0.0  # bottom of the header's lowest word


def is_value_token(text: str, spec: TableSpec) -> bool:
    return text in spec.suppression_marks or bool(_NUMBER.match(text))


def parse_value(text: str, spec: TableSpec) -> tuple[float | None, bool]:
    """(value, suppressed) for one printed token. Raises on anything that isn't a value."""
    if text in spec.suppression_marks:
        return None, True
    if not _NUMBER.match(text):
        raise LayoutError(f"not a printed value: {text!r}")
    return float(text.replace(",", "")), False


def lines(words: Iterable[Word]) -> list[list[Word]]:
    """Words grouped into visual lines, top to bottom, each line left to right."""
    out: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        if out and abs(out[-1][0].top - w.top) < _LINE_TOLERANCE:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(line, key=lambda w: w.x0) for line in out]


def merge_rules(rules: Iterable[Rule]) -> list[Rule]:
    """One rule per border: segments at (nearly) the same height that touch or overlap are joined."""
    merged: list[Rule] = []
    for r in sorted(rules, key=lambda r: (r.y, r.x0)):
        for i, m in enumerate(merged):
            if abs(m.y - r.y) < _RULE_MERGE and r.x0 <= m.x1 + 1 and r.x1 >= m.x0 - 1:
                merged[i] = Rule(y=min(m.y, r.y), x0=min(m.x0, r.x0), x1=max(m.x1, r.x1))
                break
        else:
            merged.append(r)
    return sorted(merged, key=lambda r: r.y)


def _find_header(page: PageGeometry, spec: TableSpec) -> tuple[_Header, float] | None:
    """The table header on this page, and the y below which data rows start, or None if absent."""
    groups = [w for w in page.words if w.text == spec.group_header]
    for g in groups:
        near = [w for w in page.words if abs(w.yc - g.yc) < 20 and w.text == spec.label_header]
        # The label column's header is the "Disease" word right of the group cell, not the one stacked
        # above or beside "Group" inside the group header itself.
        candidates = [w for w in near if w.x0 > g.x1 + 2]
        if candidates:
            label = min(candidates, key=lambda w: w.x0)
            group_words = [w for w in near if w.x1 <= label.x0 - 2] + [g]
            group_center = (min(w.x0 for w in group_words) + max(w.x1 for w in group_words)) / 2
            return _Header(group_center=group_center, label_center=label.xc, label_left=label.x0), g.top
    return None


def _cluster(centers: Sequence[float], gap: float) -> list[list[float]]:
    out: list[list[float]] = []
    for c in sorted(centers):
        if out and c - out[-1][-1] <= gap:
            out[-1].append(c)
        else:
            out.append([c])
    return out


def _value_lines(page: PageGeometry, spec: TableSpec, below: float, label_right: float) -> list[list[Word]]:
    rows = []
    for line in lines(w for w in page.words if w.top > below):
        values = [w for w in line if w.x0 > label_right and is_value_token(w.text, spec)]
        if len(values) < 2:
            continue
        # A header line has header words among its numbers ("Q1 2024 Q2 2024", "2024 Total"); a data row's
        # words all come before its first value, even "(Genital)" in "Chlamydia (Genital)".
        if any(w.x0 > values[0].x0 and _HEADER_VOCAB.match(w.text) for w in line):
            continue
        rows.append(line)
    return rows


def _columns(page: PageGeometry, spec: TableSpec, header: _Header, header_top: float) -> None:
    candidate = _value_lines(page, spec, header_top, header.label_center)
    tokens = [
        w for line in candidate for w in line if w.x0 > header.label_center and is_value_token(w.text, spec)
    ]
    if not tokens:
        raise LayoutError(f"page {page.number}: a table header but no value columns under it")
    widths = sorted(w.x1 - w.x0 for w in tokens)
    gap = max(8.0, widths[len(widths) // 2])
    # A column has a value in most rows; a stray number elsewhere doesn't make a column.
    clusters = [c for c in _cluster([w.xc for w in tokens], gap) if len(c) >= max(1, len(candidate) // 4)]
    if len(clusters) < 2:
        raise LayoutError(f"page {page.number}: fewer than two value columns")
    centers = [sum(c) / len(c) for c in clusters]
    bounds = [(a + b) / 2 for a, b in zip(centers, centers[1:], strict=False)]
    lefts = [centers[0] - (centers[1] - centers[0]) / 2, *bounds]
    rights = [*bounds, page.width]
    # Header lines can hold numbers too (the years under "YTD"); data rows fill most columns.
    full = [line for line in candidate if _count_in(line, spec, lefts[0]) >= math.ceil(len(centers) / 2)]
    if not full:
        raise LayoutError(f"page {page.number}: no row fills most of the value columns")
    first_data_top = min(line[0].top for line in full)
    # The header band starts at the table's top border: the nearest border above the header that runs
    # across the value columns. Without one, only the header's own line and the line above it count.
    above = [
        r.y
        for r in merge_rules(page.rules)
        if r.y < header_top - 1 and r.x1 >= centers[-1] and r.x0 <= header.label_left
    ]
    band_top = max(above) if above else header_top - 15
    header_words = [
        w for w in page.words if band_top <= w.top < first_data_top - 1 and w.x0 > header.label_left
    ]
    columns = []
    for left, right, center in zip(lefts, rights, centers, strict=True):
        words = [w for w in header_words if left <= w.xc < right]
        text = " ".join(w.text for line in lines(words) for w in line)
        if not text:
            raise LayoutError(f"page {page.number}: value column at x={center:.0f} has no header")
        columns.append(Column(left=left, right=right, center=center, header=text))
    header.columns = columns
    header.data_top = first_data_top
    used = [w for w in header_words if lefts[0] <= w.xc]
    header.band_bottom = max((w.bottom for w in used), default=header_top)


def _count_in(line: Sequence[Word], spec: TableSpec, left: float) -> int:
    """Values right of `left` on a line; 0 for a line of bare years, which is a header (\"2024  2025\")."""
    values = [w for w in line if w.x0 > left and is_value_token(w.text, spec)]
    if values and all(_YEAR.match(w.text) for w in values):
        return 0
    return len(values)


def _bands(ys: Sequence[float]) -> list[tuple[float, float]]:
    return list(zip(ys, ys[1:], strict=False))


def _band_of(y: float, bands: Sequence[tuple[float, float]]) -> int | None:
    for i, (top, bottom) in enumerate(bands):
        if top <= y < bottom:
            return i
    return None


def parse_page(
    page: PageGeometry, spec: TableSpec, carried: _Header | None
) -> tuple[list[TableRow], _Header | None]:
    """Rows of the disease table on one page. `carried` is the previous page's header, for pages whose
    table continues without repeating it. Returns the rows and the header to carry to the next page."""
    found = _find_header(page, spec)
    if found is not None:
        header, header_top = found
        _columns(page, spec, header, header_top)
        below = header.data_top - 1
        virtual_top = header.band_bottom + 0.5
    elif carried is not None:
        header, below, virtual_top = carried, 0.0, 0.0
    else:
        return [], None

    value_lines = _value_lines(page, spec, below, header.columns[0].left)
    need = math.ceil(len(header.columns) / 2)
    value_lines = [line for line in value_lines if _count_in(line, spec, header.columns[0].left) >= need]
    if not value_lines:
        return [], header

    first_left = header.columns[0].left
    last_right = header.columns[-1].center
    top = min(line[0].top for line in value_lines) - 30
    bottom = max(line[0].bottom for line in value_lines) + 30
    rules = [r for r in merge_rules(page.rules) if top <= r.y <= bottom]
    row_rules = sorted({round(r.y, 1) for r in rules if r.x0 <= header.label_left and r.x1 >= last_right})
    # Some pages draw no border between the header row and the first data row (the header's shading is its
    # only edge): the header's bottom, or the page top on a continuation page, closes that first cell.
    if row_rules and min(line[0].top for line in value_lines) < row_rules[0]:
        row_rules.insert(0, virtual_top)
    if len(row_rules) < 2:
        raise LayoutError(f"page {page.number}: no cell borders around the table rows; layout changed")
    # The table's left edge is the leftmost border that runs to the value columns; borders starting there
    # also cross the group column, so they are the edges of (possibly merged) group cells.
    table_left = min(r.x0 for r in rules if r.x1 >= last_right)
    group_rules = sorted({round(r.y, 1) for r in rules if r.x0 <= table_left + 3 and r.x1 >= last_right})
    if group_rules and row_rules[0] < group_rules[0]:
        group_rules.insert(0, row_rules[0])
    row_bands = _bands(row_rules)
    group_bands = _bands(group_rules) if len(group_rules) >= 2 else row_bands

    boundary = (header.group_center + header.label_center) / 2
    label_words = [
        w for w in page.words if w.x1 <= first_left + 1 and max(top, row_rules[0]) <= w.top <= bottom
    ]
    group_text: dict[int, list[Word]] = {}
    label_text: dict[int, list[Word]] = {}
    for w in label_words:
        if w.xc < boundary:
            gi = _band_of(w.yc, group_bands)
            if gi is not None:
                group_text.setdefault(gi, []).append(w)
        else:
            ri = _band_of(w.yc, row_bands)
            if ri is not None:
                label_text.setdefault(ri, []).append(w)

    rows: list[TableRow] = []
    seen_bands: set[int] = set()
    for line in value_lines:
        y = sum(w.yc for w in line) / len(line)
        if rows and rows[-1].trailing_total:
            # The grand total closes the table: a row of values below it is outside the table, and its label
            # window would reach back over the total's own label, so it would pass as a second total.
            raise LayoutError(f"page {page.number}: a row of values at y={y:.0f} sits below the grand total")
        ri = _band_of(y, row_bands)
        if ri is None and spec.trailing_total is not None and y > row_rules[-1]:
            # Label lines sit symmetrically around the value line; take those between the last border and
            # the mirror image of that gap below the line.
            reach = y + (y - row_rules[-1])
            words = [w for w in page.words if w.x1 <= first_left + 1 and row_rules[-1] < w.yc < reach]
            label = _join(words)
            if spec.trailing_total.search(label):
                cells = _cells(line, header.columns, spec)
                rows.append(
                    TableRow(page=page.number, group="", label=label, cells=cells, trailing_total=True)
                )
                continue
        if ri is None:
            raise LayoutError(
                f"page {page.number}: a row of values at y={y:.0f} sits outside the table borders"
            )
        if ri in seen_bands:
            raise LayoutError(f"page {page.number}: two rows of values in one cell band at y={y:.0f}")
        seen_bands.add(ri)
        gi = _band_of(y, group_bands)
        group = _join(group_text.get(gi, [])) if gi is not None else ""
        label = _join(label_text.get(ri, []))
        if not label:
            raise LayoutError(f"page {page.number}: a row of values at y={y:.0f} has no label")
        rows.append(
            TableRow(page=page.number, group=group, label=label, cells=_cells(line, header.columns, spec))
        )
    # Every labelled cell between the first and last data row must have produced a row: a label left over
    # means a row of values was skipped, and a dropped row is a wrong total.
    if rows:
        first, last = min(seen_bands), max(seen_bands)
        for ri, words in sorted(label_text.items()):
            if first <= ri <= last and ri not in seen_bands:
                raise LayoutError(
                    f"page {page.number}: row {_join(words)!r} has a label but no values were read"
                )
    return rows, header


def _join(words: Sequence[Word]) -> str:
    text = " ".join(w.text for line in lines(words) for w in line)
    # A compound broken across lines after its hyphen ("Shiga toxin-" / "producing") is one word again.
    text = re.sub(r"(\w-) (?=[a-z])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _cells(line: Sequence[Word], columns: Sequence[Column], spec: TableSpec) -> tuple[Cell, ...]:
    by_column: dict[int, Word] = {}
    for w in line:
        if not is_value_token(w.text, spec) or w.x0 <= columns[0].left:
            continue
        i = min(range(len(columns)), key=lambda i: abs(columns[i].center - w.xc))
        if i in by_column:
            raise LayoutError(
                f"two values in column {columns[i].header!r}: {by_column[i].text!r}, {w.text!r}"
            )
        by_column[i] = w
    cells = []
    for i, col in enumerate(columns):
        found = by_column.get(i)
        if found is None:
            if not spec.allow_blank:
                raise LayoutError(f"no value read in column {col.header!r} at y={line[0].top:.0f}")
            cells.append(Cell(column=col.header, raw=None, value=None, suppressed=False))
        else:
            value, suppressed = parse_value(found.text, spec)
            cells.append(Cell(column=col.header, raw=found.text, value=value, suppressed=suppressed))
    return tuple(cells)


def parse_tables(pages: Sequence[PageGeometry], spec: TableSpec | None = None) -> list[TableRow]:
    """Every disease-table row across the document's pages, in order."""
    spec = spec or TableSpec()
    rows: list[TableRow] = []
    carried: _Header | None = None
    for page in pages:
        page_rows, carried = parse_page(page, spec, carried)
        if page_rows and rows and rows[-1].trailing_total:
            raise LayoutError(
                f"page {page_rows[0].page}: rows of values continue after the grand total on page {rows[-1].page}"
            )
        if rows:
            # A merged group cell split by a page break prints its label on one page only (whether or not
            # the header repeats); the rows at the top of the next page belong to the group the previous
            # page ended in.
            previous = rows[-1].group
            for i, row in enumerate(page_rows):
                if row.group or row.trailing_total:
                    break
                page_rows[i] = replace(row, group=previous)
        for row in page_rows:
            if not row.group and not row.trailing_total:
                raise LayoutError(f"page {row.page}: row {row.label!r} has no group cell")
        rows.extend(page_rows)
    return rows


def read_pdf(content: bytes) -> list[PageGeometry]:
    """Word boxes and horizontal borders for every page (the only function that touches pdfplumber)."""
    import io

    import pdfplumber

    out = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            words = tuple(
                Word(
                    text=str(w["text"]),
                    x0=float(w["x0"]),
                    x1=float(w["x1"]),
                    top=float(w["top"]),
                    bottom=float(w["bottom"]),
                )
                for w in page.extract_words(keep_blank_chars=False, use_text_flow=False)
            )
            # Borders are thin bars or drawn lines. The edges of filled boxes (Word shades the cell behind a
            # merged label) are not borders, and counting them splits merged cells.
            bars = [r for r in page.rects if float(r["height"]) <= _MAX_BORDER and float(r["width"]) > 3]
            drawn = [ln for ln in page.lines if abs(float(ln["top"]) - float(ln["bottom"])) <= _MAX_BORDER]
            rules = tuple(
                Rule(y=float(e["top"]), x0=float(e["x0"]), x1=float(e["x1"])) for e in [*bars, *drawn]
            )
            out.append(PageGeometry(number=i, width=float(page.width), words=words, rules=rules))
    return out
