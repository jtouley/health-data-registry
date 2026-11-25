"""The https_xlsx adapter and the catalog rules around it, on a tiny workbook built in memory (no public data).

The workbook is written here, part by part, in the layout AHEAD's download uses: a note on row 1, column names
on row 2, data rows, a blank row, then the publisher's notes. Every number in it is made up.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from health_data_registry.adapters import https_xlsx, pages, publisher_fields
from health_data_registry.registry import CatalogEntry, Source
from health_data_registry.store import RawFetch

URL = "https://files.example.gov/toy_all.xlsx"
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def cell(ref: str, value: object) -> str:
    """A cell in the forms a workbook stores: shared string ('s:3'), inline string ('i:text'), or a number."""
    text = str(value)
    if text.startswith("s:"):
        return f'<c r="{ref}" t="s"><v>{text[2:]}</v></c>'
    if text.startswith("i:"):
        return f'<c r="{ref}" t="inlineStr"><is><t>{text[2:]}</t></is></c>'
    if text == "":
        return f'<c r="{ref}" s="1"/>'  # a styled cell with no value: empty
    return f'<c r="{ref}"><v>{text}</v></c>'


def sheet(rows: dict[int, list[object]]) -> str:
    body = "".join(
        f'<row r="{n}">'
        + "".join(cell(f"{chr(65 + i)}{n}", v) for i, v in enumerate(vals) if v is not None)
        + "</row>"
        for n, vals in sorted(rows.items())
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{MAIN}"><sheetData>{body}</sheetData></worksheet>'


# Shared strings: plain, and one rich-text string split into runs (with a phonetic hint that is not text).
SHARED = [
    "state", "year", "indicator", "count", "percentage",  # 0-4
    "Michigan", "Diagnoses", "Viral suppression", "county", "Wayne County",  # 5-9
]  # fmt: skip
SHARED_XML = (
    f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{MAIN}">'
    + "".join(f"<si><t>{s}</t></si>" for s in SHARED)
    + "<si><r><t>Ohi</t></r><r><t>o</t></r><rPh><t>x</t></rPh></si>"  # 10: rich text "Ohio"
    + "</sst>"
)

STATES = sheet({
    1: ["i:Data for 2025 are preliminary."],
    2: ["s:0", "s:1", "s:2", "s:3", "s:4"],
    3: ["s:5", 2024, "s:6", 672, ""],
    4: ["s:5", 2024, "s:7", 13137, "73.90000000000001"],  # float noise as a workbook can store it
    5: ["s:10", 2024, "s:6", "", ""],  # an empty count: absent, never zero
    7: ["i:IX. Empty cells"],  # the notes, after the blank row 6
    8: ["i:a. Data incomplete, not reported, or suppressed."],
})  # fmt: skip
COUNTIES = sheet({
    1: ["i:Data for 2025 are preliminary."],
    2: ["s:0", "s:8", "s:1", "s:2", "s:3"],
    3: ["s:5", "s:9", 2024, "s:6", 290],
})  # fmt: skip


def workbook(parts: dict[str, str] | None = None, *, sheets: list[tuple[str, str]] | None = None) -> bytes:
    sheets = sheets or [
        ("State totals", "worksheets/sheet1.xml"),
        ("Jurisdiction totals", "worksheets/sheet2.xml"),
    ]
    files = {
        "xl/workbook.xml": f'<?xml version="1.0"?><workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>'
        + "".join(f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, (n, _) in enumerate(sheets, 1))
        + "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": f'<?xml version="1.0"?><Relationships xmlns="{PKG}">'
        + "".join(f'<Relationship Id="rId{i}" Target="{t}"/>' for i, (_, t) in enumerate(sheets, 1))
        + "</Relationships>",
        "xl/sharedStrings.xml": SHARED_XML,
        "xl/worksheets/sheet1.xml": STATES,
        "xl/worksheets/sheet2.xml": COUNTIES,
    }
    files.update(parts or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def entry(**layout: object) -> CatalogEntry:
    wb: dict[str, object] = {"sheets": ["State totals", "Jurisdiction totals"], "header_row": 2}
    wb.update(layout)
    return CatalogEntry.model_validate(
        {
            "id": "toy_xlsx", "title": "t", "agency": "a", "publisher": "p", "level": "federal",
            "research_areas": ["infectious_disease"], "years": {"start": 2024}, "geography": ["state"],
            "shape": "estimate_table", "grain": "g", "refresh": "annual",
            "licence": {"id": "public-domain-us-federal", "redistribution": True, "terms_url": "https://x.gov"},
            "citation": "c", "codebook_url": "https://x.gov", "contract": "c", "semantic_model": "s",
            "status": "draft",
            "source": {"portal": "https_xlsx", "host": "files.example.gov", "dataset": "toy",
                       "landing_url": "https://example.gov/", "api_url": URL, "max_bytes": 1000, "workbook": wb},
        }
    )  # fmt: skip


def fetcher(content: bytes):  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def get(url: str) -> RawFetch:
        calls.append(url)
        return RawFetch(url, content, hashlib.sha256(content).hexdigest(), datetime(2026, 11, 25, tzinfo=UTC))

    get.calls = calls  # type: ignore[attr-defined]
    return get


def test_named_sheets_are_read_as_stored_and_notes_are_not_rows() -> None:
    content = workbook()
    get = fetcher(content)
    got = list(pages(entry(), get))
    assert len(got) == 1 and get.calls == [URL] and got[0].fetch.content == content  # one fetch, bytes kept
    recs = got[0].records
    assert recs == [
        {"sheet": "State totals", "state": "Michigan", "year": "2024", "indicator": "Diagnoses", "count": "672",
         "percentage": None, "county": None},
        {"sheet": "State totals", "state": "Michigan", "year": "2024", "indicator": "Viral suppression",
         "count": "13137", "percentage": "73.90000000000001", "county": None},  # the stored text, unrounded
        {"sheet": "State totals", "state": "Ohio", "year": "2024", "indicator": "Diagnoses", "count": None,
         "percentage": None, "county": None},  # rich text joined; an empty count stays empty
        {"sheet": "Jurisdiction totals", "state": "Michigan", "year": "2024", "indicator": "Diagnoses",
         "count": "290", "percentage": None, "county": "Wayne County"},
    ]  # fmt: skip


def test_fields_max_rows_and_publisher_fields() -> None:
    content = workbook()
    recs = list(pages(entry(), fetcher(content), fields=["county", "count"], max_rows=2))[0].records
    assert recs == [{"count": "672", "county": None}, {"count": "13137", "county": None}]
    assert list(pages(entry(), fetcher(content), max_rows=-1))[0].records == []
    assert publisher_fields(entry(), fetcher(content)) == {
        "sheet", "state", "county", "year", "indicator", "count", "percentage"
    }  # fmt: skip
    assert https_xlsx.read_workbook(entry(sheet_field="tab"), content)[0][0] == "tab"
    only_counties = entry(sheets=["Jurisdiction totals"])
    assert [r["county"] for r in https_xlsx.records(only_counties, content)] == ["Wayne County"]


def test_header_row_is_where_the_layout_says() -> None:
    with pytest.raises(ValueError, match="column with no name"):
        https_xlsx.records(
            entry(header_row=1), workbook()
        )  # row 1 is the note: one "column", then data wider
    one_col = sheet({1: ["s:0"], 2: ["s:5"], 3: ["s:5"]})
    recs = https_xlsx.records(
        entry(header_row=1, sheets=["S"]),
        workbook({"xl/worksheets/sheet1.xml": one_col}, sheets=[("S", "worksheets/sheet1.xml")]),
    )
    assert recs == [{"sheet": "S", "state": "Michigan"}, {"sheet": "S", "state": "Michigan"}]
    with pytest.raises(ValueError, match="no header row"):
        https_xlsx.records(entry(header_row=9), workbook())


@pytest.mark.parametrize(
    "parts,match",
    [
        ({"xl/worksheets/sheet1.xml": sheet({2: ["s:0", "s:0"]})}, "repeats a column"),
        ({"xl/worksheets/sheet1.xml": sheet({2: ["s:0", None, "s:1"]})}, "has no name"),
        ({"xl/worksheets/sheet1.xml": sheet({2: ["s:0"], 3: ["s:5", 7]})}, "column with no name"),
        ({"xl/worksheets/sheet1.xml": sheet({2: ["s:0"], 3: ["s:99"]})}, "shared string the workbook lacks"),
        ({"xl/worksheets/sheet1.xml": sheet({2: ["s:0"], 3: ["s:x"]})}, "shared string the workbook lacks"),
        ({"xl/worksheets/sheet1.xml": sheet({2: ["s:0", "i:sheet"]})}, "would overwrite"),
        ({"xl/worksheets/sheet1.xml": f'<worksheet xmlns="{MAIN}"><sheetData><row r="2"><c r="R1C1"><v>1</v></c>'
          "</row></sheetData></worksheet>"}, "plain reference"),
        ({"xl/worksheets/sheet1.xml": "<worksheet><sheetData>"}, "not well-formed"),
        ({"xl/worksheets/sheet1.xml": '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><worksheet/>'},
         "DOCTYPE"),
        ({"xl/sharedStrings.xml": '<!ENTITY a SYSTEM "file:///etc/passwd">'}, "DOCTYPE or entity"),
    ],
)  # fmt: skip
def test_malformed_or_unsafe_workbooks_are_refused(parts: dict[str, str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        https_xlsx.records(entry(), workbook(parts))


def test_workbook_structure_is_checked() -> None:
    with pytest.raises(ValueError, match="not an XLSX workbook"):
        https_xlsx.records(entry(), b"state,year\nMichigan,2024\n")
    with pytest.raises(ValueError, match="no sheet named 'State totals'"):
        https_xlsx.records(entry(), workbook(sheets=[("Other", "worksheets/sheet1.xml")]))
    for target in ("../../evil.xml", "/../evil.xml", ""):
        with pytest.raises(ValueError, match="outside the workbook"):
            https_xlsx.records(entry(), workbook(sheets=[("State totals", target)]))
    with pytest.raises(ValueError, match="no part xl/worksheets/nope.xml"):
        https_xlsx.records(entry(), workbook(sheets=[("State totals", "worksheets/nope.xml")]))
    absolute = workbook(sheets=[("State totals", "/xl/worksheets/sheet1.xml"),
                                ("Jurisdiction totals", "worksheets/sheet2.xml")])  # fmt: skip
    assert len(https_xlsx.records(entry(), absolute)) == 4
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:  # no shared strings part: inline strings only
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>'
                   '<sheet name="S" r:id="rId1"/></sheets></workbook>')  # fmt: skip
        z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PKG}">'
                   '<Relationship Id="rId1" Target="worksheets/s.xml"/></Relationships>')  # fmt: skip
        z.writestr("xl/worksheets/s.xml", sheet({1: ["i:a", "i:b"], 2: ["i:x", 1.5]}))
    assert https_xlsx.records(entry(sheets=["S"], header_row=1), buf.getvalue()) == [
        {"sheet": "S", "a": "x", "b": "1.5"}
    ]


def test_a_part_that_inflates_past_the_cap_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    content = workbook()
    size = len(STATES.encode())
    monkeypatch.setattr(https_xlsx, "MAX_PART_BYTES", size - 1)
    with pytest.raises(ValueError, match="larger than"):
        https_xlsx.records(entry(sheets=["State totals"]), content)
    monkeypatch.setattr(https_xlsx, "MAX_PART_BYTES", size)
    assert len(https_xlsx.records(entry(sheets=["State totals"]), content)) == 3

    class Lying:  # a zip entry that declares a small size but opens onto more bytes than the cap
        def __init__(self, z: zipfile.ZipFile) -> None:
            self.z = z

        def getinfo(self, name: str) -> Any:
            info = self.z.getinfo(name)
            return zipfile.ZipInfo(info.filename)  # file_size 0

        def open(self, info: Any) -> Any:
            return self.z.open(info.filename)

    monkeypatch.setattr(https_xlsx, "MAX_PART_BYTES", 10)
    with zipfile.ZipFile(io.BytesIO(content)) as z, pytest.raises(ValueError, match="larger than"):
        https_xlsx._part(Lying(z), "xl/worksheets/sheet1.xml")  # type: ignore[arg-type]


def test_a_damaged_zip_entry_is_refused_in_plain_words() -> None:
    content = bytearray(workbook())
    with zipfile.ZipFile(io.BytesIO(bytes(content))) as z:
        info = z.getinfo("xl/worksheets/sheet1.xml")
        start = info.header_offset + 30 + len(info.filename) + len(info.extra)
    content[start + 5] ^= 0xFF  # flip bits inside the compressed sheet
    with pytest.raises(ValueError, match="damaged"):
        https_xlsx.records(entry(), bytes(content))


def test_cell_column_letters() -> None:
    assert [https_xlsx._column(c) for c in ("A", "B", "Z", "AA", "AZ", "BA")] == [0, 1, 25, 26, 51, 52]


def test_source_rules() -> None:
    base = {"host": "files.example.gov", "dataset": "x", "landing_url": "https://a", "api_url": "https://a"}
    wb = {"sheets": ["S"]}
    with pytest.raises(ValidationError, match="needs a workbook"):
        Source(portal="https_xlsx", **base)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="needs a workbook"):
        Source(portal="https_csv", workbook=wb, **base)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="whole-file"):
        Source(portal="socrata", max_bytes=10, **base)  # type: ignore[arg-type]
    for bad in (0, 200_000_001):
        with pytest.raises(ValidationError):
            Source(portal="https_xlsx", workbook=wb, max_bytes=bad, **base)  # type: ignore[arg-type]
    assert Source(portal="https_csv", max_bytes=200_000_000, **base).max_bytes == 200_000_000  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Source(portal="https_xlsx", workbook={"sheets": []}, **base)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Source(portal="https_xlsx", workbook={"sheets": ["S"], "header_row": 0}, **base)  # type: ignore[arg-type]
    s = Source(portal="https_xlsx", workbook=wb, **base)  # type: ignore[arg-type]
    assert s.workbook is not None and (s.workbook.header_row, s.workbook.sheet_field) == (1, "sheet")
    assert s.workbook.exclude_rows == {}


def test_excluded_rows_never_leave_the_adapter() -> None:
    """A reviewed exclusion (AHEAD's PrEP rows) drops whole rows by exact value; a vanished column stops the read."""
    content = workbook()
    kept = https_xlsx.records(entry(exclude_rows={"indicator": ["Viral suppression"]}), content)
    assert [r["indicator"] for r in kept] == ["Diagnoses", "Diagnoses", "Diagnoses"]
    both = {"indicator": ["Viral suppression"], "county": ["Wayne County"]}
    two = https_xlsx.records(entry(exclude_rows=both), content)
    assert [(r["state"], r["indicator"]) for r in two] == [("Michigan", "Diagnoses"), ("Ohio", "Diagnoses")]
    exact = https_xlsx.records(entry(exclude_rows={"indicator": ["viral suppression"]}), content)
    assert len(exact) == 4  # exact match only
    fields = https_xlsx.read_workbook(entry(exclude_rows={"indicator": ["Diagnoses"]}), content)[0]
    assert "indicator" in fields  # the column itself stays; only the named rows go
    with pytest.raises(ValueError, match="no longer has: indicatr"):
        https_xlsx.records(entry(exclude_rows={"indicatr": ["Viral suppression"]}), content)
