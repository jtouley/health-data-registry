"""Shared pieces for the adapter-type tests: a catalog entry of any type, a fake getter, tiny hand-written files.

Every fixture is synthetic and written here by hand (hard rule 5): no publisher's file is used.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from health_data_registry.registry import CatalogEntry
from health_data_registry.store import RawFetch

HOST = "data.example.gov"
WHEN = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def entry(
    kind: str, block: Mapping[str, Any] | None = None, *, entry_id: str = "toy_source", **source: Any
) -> CatalogEntry:
    """A catalog entry of adapter type ``kind`` with its type block and any shared source settings."""
    src: dict[str, Any] = {
        "type": kind,
        "host": HOST,
        "dataset": "toy",
        "landing_url": f"https://{HOST}/toy/",
        "api_url": f"https://{HOST}/toy/data",
        **({kind: dict(block)} if block is not None else {}),
        **source,
    }
    return CatalogEntry.model_validate(
        {
            "id": entry_id, "title": "t", "agency": "a", "publisher": "p", "level": "county",
            "research_areas": ["hiv"], "years": {"start": 2020}, "geography": ["county"],
            "shape": "frequency_table", "grain": "g", "refresh": "annual",
            "licence": {"id": "public-domain-us-federal", "redistribution": True, "terms_url": "https://x.gov"},
            "citation": "c", "source": src, "codebook_url": "https://x.gov", "contract": "c",
            "semantic_model": "s", "status": "draft",
        }
    )  # fmt: skip


def fetch_of(url: str, content: bytes) -> RawFetch:
    return RawFetch(url=url, content=content, sha256=hashlib.sha256(content).hexdigest(), retrieved_at=WHEN)


class Site:
    """A fake publisher: URL -> bytes (or an exception to raise), recording every request."""

    def __init__(self, pages: Mapping[str, bytes | Exception]) -> None:
        self.pages = dict(pages)
        self.calls: list[str] = []

    def __call__(self, url: str) -> RawFetch:
        self.calls.append(url)
        got = self.pages.get(url)
        if got is None:
            raise AssertionError(f"unexpected request: {url}")
        if isinstance(got, Exception):
            raise got
        return fetch_of(url, got)


def zip_bytes(members: Mapping[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def xlsx_bytes(
    rows: list[list[tuple[str, Any, int] | None]],
    *,
    sheet: str = "Table 1",
    state: str | None = None,
    merged: list[str] = (),  # type: ignore[assignment]
    shared: list[str] = (),  # type: ignore[assignment]
    custom_formats: Mapping[int, str] | None = None,
    xf_formats: list[int] = (0,),  # type: ignore[assignment]
    prolog: str = "",
) -> bytes:
    """A minimal workbook. Each cell is (kind, value, style): kind n (number), s (shared string index), str,
    inlineStr, b, or f (a formula: value is (formula, cached value or None)); None is an empty cell."""

    def col(i: int) -> str:
        return chr(65 + i)

    xml_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, cell in enumerate(row):
            if cell is None:
                continue
            kind, value, style = cell
            ref = f"{col(c)}{r}"
            if kind == "f":
                formula, cached = value
                v = f"<v>{cached}</v>" if cached is not None else ""
                cells.append(f'<c r="{ref}" s="{style}"><f>{formula}</f>{v}</c>')
            elif kind == "inlineStr":
                cells.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t>{value}</t></is></c>')
            elif kind == "n":
                cells.append(f'<c r="{ref}" s="{style}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" s="{style}" t="{kind}"><v>{value}</v></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    merges = (
        f"<mergeCells>{''.join(f'<mergeCell ref="{m}"/>' for m in merged)}</mergeCells>" if merged else ""
    )
    sheet_xml = (
        f'{prolog}<worksheet xmlns="{_MAIN}"><sheetData>{"".join(xml_rows)}</sheetData>{merges}</worksheet>'
    )
    st = f' state="{state}"' if state else ""
    workbook = (
        f'<workbook xmlns="{_MAIN}" xmlns:r="{_REL}"><sheets>'
        f'<sheet name="Notes" sheetId="2" r:id="rId2"/><sheet name="{sheet}" sheetId="1" r:id="rId1"{st}/>'
        "</sheets></workbook>"
    )
    rels = (
        f'<Relationships xmlns="{_PKG}">'
        '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="worksheet" Target="worksheets/sheet2.xml"/></Relationships>'
    )
    fmts = "".join(
        f'<numFmt numFmtId="{i}" formatCode="{code}"/>' for i, code in (custom_formats or {}).items()
    )
    styles = (
        f'<styleSheet xmlns="{_MAIN}"><numFmts>{fmts}</numFmts><cellXfs>'
        + "".join(f'<xf numFmtId="{i}"/>' for i in xf_formats)
        + "</cellXfs></styleSheet>"
    )
    sst = f'<sst xmlns="{_MAIN}">' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>"
    return zip_bytes(
        {
            "xl/workbook.xml": workbook.encode(),
            "xl/_rels/workbook.xml.rels": rels.encode(),
            "xl/worksheets/sheet1.xml": sheet_xml.encode(),
            "xl/worksheets/sheet2.xml": f'<worksheet xmlns="{_MAIN}"><sheetData/></worksheet>'.encode(),
            "xl/styles.xml": styles.encode(),
            "xl/sharedStrings.xml": sst.encode(),
        }
    )
