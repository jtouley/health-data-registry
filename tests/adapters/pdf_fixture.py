"""Write a tiny text PDF by hand: Helvetica words at given positions and thin filled bars as cell borders.

Laid out the way a county's Word export prints a case-count table (a header band between two borders, one border
under each row), but every value is made up here. No PDF library is used to write it, so the reader is never fitted
to a particular writer's output.
"""

from __future__ import annotations

from dataclasses import dataclass

WIDTH, HEIGHT = 612.0, 792.0
SIZE, LINE, PAD, BAR = 8.0, 10.0, 4.0, 0.5
GROUP_X, LABEL_X, VALUES_X, VALUE_W = 72.0, 152.0, 280.0, 46.0


@dataclass(frozen=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, str, tuple[str, ...]], ...]  # (group, label, printed values)
    group_header: str = "Group"
    label_header: str = "Disease"
    title: str = "Communicable disease report"
    notes: tuple[str, ...] = ()  # lines printed under the title, before the table (e.g. the area it covers)
    value_w: float = VALUE_W  # each value column's width (long headers need more)


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _ops(table: Table) -> list[str]:
    w = table.value_w
    right = VALUES_X + w * len(table.headers)
    ops: list[str] = []

    def bar(y_top: float) -> None:  # y measured down from the top of the page, as a reader sees it
        ops.append(f"{GROUP_X:.2f} {HEIGHT - y_top - BAR / 2:.2f} {right - GROUP_X:.2f} {BAR:.2f} re f")

    def text(x: float, y_top: float, s: str) -> None:
        ops.append(f"BT /F1 {SIZE:g} Tf {x:.2f} {HEIGHT - y_top - SIZE:.2f} Td ({_esc(s)}) Tj ET")

    y = 60.0
    text(GROUP_X, y, table.title)
    for note in table.notes:
        y += LINE + PAD
        text(GROUP_X, y, note)
    y += 2 * LINE
    bar(y)
    text(GROUP_X + 2, y + PAD, table.group_header)
    text(LABEL_X + 2, y + PAD, table.label_header)
    for i, h in enumerate(table.headers):
        text(VALUES_X + i * w + 4, y + PAD, h)
    y += 2 * PAD + LINE
    bar(y)
    for group, label, values in table.rows:
        top = y + PAD
        text(GROUP_X + 2, top, group)
        text(LABEL_X + 2, top, label)
        for i, v in enumerate(values):
            text(VALUES_X + i * w + 4, top, v)
        y += 2 * PAD + LINE
        bar(y)
    return ops


def render(table: Table) -> bytes:
    """A one-page PDF of ``table``, with a correct cross-reference table."""
    content = "\n".join(_ops(table)).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {WIDTH:g} {HEIGHT:g}] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>".encode(),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
