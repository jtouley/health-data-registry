"""Draw synthetic case-count reports as PDFs, laid out the ways local health departments' Word exports are.

Tests generate a table here, render it, parse it back, and compare. No real report is used, so the parser
is never fitted to a particular file. Layout choices mirror what real reports do:

* the group label repeated on every row, or printed once in a merged cell (with Word's shading box
  behind it, which must not be mistaken for a border);
* disease names wrapped over two lines, centred on their row of values;
* one- or two-line column headers, a header repeated on each page or not, a group split by a page break;
* thin filled bars as borders, as Word draws them.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Literal

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

FONT = "Helvetica"
SIZE = 8.0
LINE = 10.0  # line height
PAD = 4.0
BAR = 0.5  # border thickness


@dataclass(frozen=True)
class SynthRow:
    label_lines: tuple[str, ...]  # the disease name as printed, one entry per line
    values: tuple[str, ...]  # printed tokens, one per value column

    @property
    def label(self) -> str:
        return " ".join(self.label_lines)


@dataclass(frozen=True)
class SynthGroup:
    name: str
    rows: tuple[SynthRow, ...]


@dataclass(frozen=True)
class SynthReport:
    title: str
    headers: tuple[tuple[str, ...], ...]  # per value column, its header lines
    groups: tuple[SynthGroup, ...]
    group_style: Literal["repeat", "merged"]
    repeat_header: bool
    trailing_total: tuple[str, ...] | None = None  # values of a grand-total line below the table


GROUP_X, LABEL_X, VALUES_X, VALUE_W = 72.0, 152.0, 280.0, 46.0
LABEL_WIDTH = VALUES_X - LABEL_X - 8
TOP_MARGIN, BOTTOM_MARGIN = 60.0, 60.0


def render(report: SynthReport) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    right = VALUES_X + VALUE_W * len(report.headers)
    c.setFont(FONT, SIZE)
    y = height - TOP_MARGIN  # reportlab measures from the bottom; we move down the page

    def bar(y_: float, x0: float, x1: float) -> None:
        c.rect(x0, y_ - BAR / 2, x1 - x0, BAR, stroke=0, fill=1)

    def text(x: float, y_: float, s: str, *, centre_in: float | None = None) -> None:
        if centre_in is not None:
            x = x + (centre_in - c.stringWidth(s, FONT, SIZE)) / 2
        c.drawString(x, y_ - SIZE, s)  # y_ is the top of the line

    def header(y_: float) -> float:
        nlines = max(len(h) for h in report.headers)
        bar(y_, GROUP_X, right)
        mid = y_ - PAD - (nlines * LINE) / 2 + LINE / 2
        text(GROUP_X + 2, mid, "Disease Group")
        text(LABEL_X + 2, mid, "Disease")
        for i, lines in enumerate(report.headers):
            for j, line in enumerate(lines):
                text(VALUES_X + i * VALUE_W, y_ - PAD - j * LINE, line, centre_in=VALUE_W)
        bottom = y_ - 2 * PAD - nlines * LINE
        bar(bottom, GROUP_X, right)
        return bottom

    text(GROUP_X, y, report.title)
    y = header(y - 2 * LINE)
    for group in report.groups:
        block_top = y
        label_drawn = False
        rows_on_page: list[tuple[float, float]] = []
        for k, row in enumerate(group.rows):
            row_h = 2 * PAD + LINE * max(1, len(row.label_lines))
            if y - row_h < BOTTOM_MARGIN:
                if report.group_style == "merged" and rows_on_page and not label_drawn:
                    _merged_label(c, group.name, block_top, y)
                    label_drawn = True
                bar(y, GROUP_X, right)
                c.showPage()
                c.setFont(FONT, SIZE)
                y = height - TOP_MARGIN
                y = header(y) if report.repeat_header else (bar(y, GROUP_X, right), y)[1]
                block_top, rows_on_page = y, []
            centre = y - row_h / 2
            for j, line in enumerate(row.label_lines):
                text(LABEL_X + 2, centre + LINE * (len(row.label_lines) / 2 - j), line)
            for i, v in enumerate(row.values):
                text(VALUES_X + i * VALUE_W, centre + LINE / 2, v, centre_in=VALUE_W)
            if report.group_style == "repeat":
                text(GROUP_X + 2, centre + LINE / 2, group.name)
            y -= row_h
            rows_on_page.append((y + row_h, y))
            last = k == len(group.rows) - 1
            # Inside a merged group only the label and value columns get a border; the group cell spans.
            bar(y, GROUP_X if (last or report.group_style == "repeat") else LABEL_X, right)
        if report.group_style == "merged" and not label_drawn:
            _merged_label(c, group.name, block_top, y)
    if report.trailing_total is not None:
        centre = y - PAD - LINE
        text(LABEL_X - 30, centre + LINE, "TOTAL (does not include")
        for i, v in enumerate(report.trailing_total):
            text(VALUES_X + i * VALUE_W, centre + LINE / 2, v, centre_in=VALUE_W)
        text(LABEL_X - 30, centre, "subtotals)")
    c.save()
    return buf.getvalue()


def _merged_label(c: canvas.Canvas, name: str, top: float, bottom: float) -> None:
    centre = (top + bottom) / 2
    # Word's shading box behind a merged cell's text: filled, a text line tall. Not a border.
    c.saveState()
    c.setFillGray(0.9)
    c.rect(GROUP_X + 1, centre - LINE / 2 - 1, LABEL_X - GROUP_X - 2, LINE + 2, stroke=0, fill=1)
    c.restoreState()
    c.drawString(GROUP_X + 4, centre + LINE / 2 - SIZE, name)


def render_shapes(
    rects: list[tuple[float, float, float, float]],
    lines: list[tuple[float, float, float, float]],
    pages: int = 1,
) -> bytes:
    """Pages holding only filled boxes (x, y, width, height) and stroked lines (x0, y0, x1, y1), the same on
    each page, for checking which shapes count as borders."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for _ in range(pages):
        c.setFont(FONT, SIZE)
        c.drawString(GROUP_X, 700, "Shapes")
        for x, y, w, h in rects:
            c.rect(x, y, w, h, stroke=0, fill=1)
        for x0, y0, x1, y1 in lines:
            c.line(x0, y0, x1, y1)
        c.showPage()
    c.save()
    return buf.getvalue()
