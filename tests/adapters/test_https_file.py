"""https_file: csv, xlsx and zip members, table-driven over tiny hand-written files (no publisher's file)."""

from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from dataclasses import dataclass, field
from typing import Any

import pytest

from health_data_registry.adapters import DOCUMENT_FIELDS, LayoutError, pages, publisher_fields
from health_data_registry.adapters.archive import ArchiveRefused, Caps, SafeZip, read_member
from health_data_registry.adapters.row_guard import RowLevelRefused
from health_data_registry.adapters.xlsx import XlsxRefused
from tests.adapters.helpers import HOST, Site, entry, published, xlsx_bytes, zip_bytes

URL = f"https://{HOST}/toy/data"
CSV = b'County,Year,Cases\nWayne,2024,"1,204"\nOakland,2024,*\n'
# A workbook: header in row 1, a percent stored as 0.123, a date stored as its serial, a formula's cached value.
WB = xlsx_bytes(
    [
        [("s", 0, 0), ("s", 1, 0), ("s", 2, 0), ("s", 3, 0)],
        [("s", 4, 0), ("n", "0.123", 1), ("n", "45292", 2), ("f", ("B2*100", "12.3"), 0)],
    ],
    shared=["County", "Share", "Week", "Percent", "Wayne"],
    custom_formats={164: "0.0%"},
    xf_formats=[0, 164, 14],
)


@dataclass
class Case:
    name: str
    block: dict[str, Any]
    content: bytes
    records: list[dict[str, Any]] | None = None
    refused: type[Exception] | None = None
    match: str | None = None
    source: dict[str, Any] = field(default_factory=dict)
    max_rows: int | None = None


def _bomb() -> bytes:
    return zip_bytes({"data.csv": b"a,b\n" + b"0" * 2_000_000})


CASES = [
    Case(
        "csv as printed",
        {"format": "csv"},
        CSV,
        [
            {"County": "Wayne", "Year": "2024", "Cases": "1,204"},
            {"County": "Oakland", "Year": "2024", "Cases": "*"},
        ],
    ),
    Case(
        "csv latin-1",
        {"format": "csv", "encoding": "latin-1"},
        "Place,N\nDoña Ana,3\n".encode("latin-1"),
        [{"Place": "Doña Ana", "N": "3"}],
    ),
    Case(
        "header row 3, footer skipped",
        {"format": "csv", "header_row": 3, "skip_footer_rows": 1},
        b"Title\n\nCounty,Cases\nWayne,4\nSource: the county\n",
        [{"County": "Wayne", "Cases": "4"}],
    ),
    Case(
        "max_rows",
        {"format": "csv"},
        CSV,
        [{"County": "Wayne", "Year": "2024", "Cases": "1,204"}],
        max_rows=1,
    ),
    Case(
        "ragged csv refused",
        {"format": "csv"},
        b"A,B\n1\n",
        refused=LayoutError,
        match="one value per column",
    ),
    Case("repeated label refused", {"format": "csv"}, b"A,A\n1,2\n", refused=LayoutError, match="repeats"),
    Case(
        "empty label refused", {"format": "csv"}, b"A,,B\n1,2,3\n", refused=LayoutError, match="empty label"
    ),
    Case("not that encoding", {"format": "csv"}, b"A\n\xff\n", refused=LayoutError, match="utf-8"),
    Case(
        "over max_rows refused",
        {"format": "csv", "max_rows": 1},
        CSV,
        refused=LayoutError,
        match="more than 1",
    ),
    Case("no header row", {"format": "csv", "header_row": 9}, CSV, refused=LayoutError, match="no row 9"),
    Case(
        "xlsx stored values and formats",
        {"format": "xlsx", "sheet": "Table 1"},
        WB,
        [
            {
                "County": "Wayne",
                "Share": "0.123",
                "Week": "45292",
                "Percent": "12.3",
                "County (number format)": "General",
                "Share (number format)": "0.0%",
                "Week (number format)": "mm-dd-yy",
                "Percent (number format)": "General",
            }
        ],
    ),
    Case(
        "zip member csv",
        {"format": "csv", "zip_member": "data/cases.csv"},
        zip_bytes({"data/cases.csv": b"A,B\n1,2\n", "readme.txt": b"x"}),
        [{"A": "1", "B": "2"}],
    ),
    Case(
        "zip member xlsx",
        {"format": "xlsx", "sheet": "Table 1", "zip_member": "book.xlsx"},
        zip_bytes({"book.xlsx": WB}),
        None,
    ),
    Case(
        "zip member missing",
        {"format": "csv", "zip_member": "nope.csv"},
        zip_bytes({"a.csv": b"A\n1\n"}),
        refused=ArchiveRefused,
        match="no entry",
    ),
    Case(
        "nested zip refused",
        {"format": "csv", "zip_member": "inner.zip"},
        zip_bytes({"inner.zip": zip_bytes({"a.csv": b"A\n"})}),
        refused=ArchiveRefused,
        match="nested",
    ),
    Case(
        "zip bomb refused",
        {"format": "csv", "zip_member": "data.csv"},
        _bomb(),
        refused=ArchiveRefused,
        match="expands",
    ),
    Case(
        "not a zip",
        {"format": "csv", "zip_member": "a.csv"},
        b"hello",
        refused=ArchiveRefused,
        match="not a readable",
    ),
    Case(
        "hidden sheet refused",
        {"format": "xlsx", "sheet": "Table 1"},
        xlsx_bytes([[("inlineStr", "A", 0)]], state="hidden"),
        refused=XlsxRefused,
        match="hidden",
    ),
    Case(
        "missing sheet refused",
        {"format": "xlsx", "sheet": "Other"},
        WB,
        refused=XlsxRefused,
        match="no sheet",
    ),
    Case(
        "formula without a saved value",
        {"format": "xlsx", "sheet": "Table 1"},
        xlsx_bytes([[("inlineStr", "A", 0)], [("f", ("1+1", None), 0)]]),
        refused=XlsxRefused,
        match="no saved value",
    ),
    Case(
        "merged header refused",
        {"format": "xlsx", "sheet": "Table 1"},
        xlsx_bytes(
            [[("inlineStr", "A", 0), ("inlineStr", "B", 0)], [("n", "1", 0), ("n", "2", 0)]], merged=["A1:B1"]
        ),
        refused=LayoutError,
        match="merged",
    ),
    Case(
        "entity in xlsx refused",
        {"format": "xlsx", "sheet": "Table 1"},
        xlsx_bytes([[("inlineStr", "A", 0)]], prolog='<!DOCTYPE x [<!ENTITY e "boom">]>'),
        refused=XlsxRefused,
        match="not plain XML",
    ),
    Case("trailing empty column kept out", {"format": "csv"}, b"A,\n1,\n", [{"A": "1"}]),
    Case(
        "value past the last label",
        {"format": "csv"},
        b"A,\n1,2\n",
        refused=LayoutError,
        match="past the last",
    ),
    Case(
        "pinned file changed",
        {"format": "csv", "expected_sha256": "0" * 64},
        CSV,
        refused=LayoutError,
        match="changed",
    ),
    Case(
        "header_row_finder",
        {"format": "csv"},
        b"Report\nnote\nCounty,N\nWayne,1\n",
        [{"County": "Wayne", "N": "1"}],
        source={
            "extensions": [{"name": "header_row_finder", "hook": "locate", "args": {"first_cell": "County"}}]
        },
    ),
    Case(
        "row-level file refused by the core guard",
        {"format": "csv"},
        b"Patient Name,Age,Sex\nX,40,F\n",
        refused=RowLevelRefused,
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_https_file(case: Case) -> None:
    e = entry("https_file", case.block, **case.source)
    site = Site({URL: case.content})
    if case.refused is not None:
        with pytest.raises(case.refused, match=case.match):
            list(pages(e, site, max_rows=case.max_rows))
        return
    got = list(pages(e, site, max_rows=case.max_rows))
    assert len(got) == 1 and got[0].fetch.content == case.content  # the raw store keeps the file as sent
    if case.records is not None:
        assert published(got[0]) == case.records
    if "zip_member" in case.block:
        member = case.block["zip_member"]
        data = zipfile.ZipFile(io.BytesIO(case.content)).read(member)
        assert got[0].provenance == {"zip_member": member, "member_sha256": hashlib.sha256(data).hexdigest()}


def test_xlsx_in_zip_reads_the_same_as_alone() -> None:
    alone = list(pages(entry("https_file", {"format": "xlsx", "sheet": "Table 1"}), Site({URL: WB})))
    zipped = entry("https_file", {"format": "xlsx", "sheet": "Table 1", "zip_member": "book.xlsx"})
    inside = list(pages(zipped, Site({URL: zip_bytes({"book.xlsx": WB})})))
    assert published(alone[0]) == published(inside[0])


def test_every_column_is_handed_on_with_its_format() -> None:
    e = entry("https_file", {"format": "xlsx", "sheet": "Table 1"})
    got = published(list(pages(e, Site({URL: WB}), fields=["Share"]))[0])
    # hard rule 15: the raw layer keeps every column the file has, not only the contract's
    assert got[0]["Share"] == "0.123" and got[0]["Share (number format)"] == "0.0%" and len(got[0]) > 2


def test_publisher_fields() -> None:
    assert publisher_fields(entry("https_file", {"format": "csv"}), Site({URL: CSV})) == {
        "County",
        "Year",
        "Cases",
        *DOCUMENT_FIELDS,
    }
    xl = publisher_fields(entry("https_file", {"format": "xlsx", "sheet": "Table 1"}), Site({URL: WB}))
    assert "Share" in xl and "Share (number format)" in xl


def _raw_zip(infos: list[tuple[zipfile.ZipInfo, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for info, data in infos:
            zf.writestr(info, data)
    return buf.getvalue()


CAPS = Caps(max_uncompressed_bytes=10_000, max_ratio=50, max_members=3)


def _encrypted() -> bytes:
    raw = bytearray(_raw_zip([(zipfile.ZipInfo("a.csv"), b"A\n")]))
    # set the encryption bit in the local header and the central directory entry
    raw[6] |= 1
    cd = raw.rfind(b"PK\x01\x02")
    raw[cd + 8] |= 1
    return bytes(raw)


def _symlink() -> bytes:
    info = zipfile.ZipInfo("link.csv")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return _raw_zip([(info, b"/etc/passwd")])


def _dup() -> bytes:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return _raw_zip([(zipfile.ZipInfo("a.csv"), b"1"), (zipfile.ZipInfo("./a.csv"), b"2")])


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (_encrypted(), "encrypted"),
        (_symlink(), "symbolic link"),
        (_dup(), "two entries"),
        (_raw_zip([(zipfile.ZipInfo("../x.csv"), b"1")]), "outside"),
        (_raw_zip([(zipfile.ZipInfo("/x.csv"), b"1")]), "outside"),
        (zip_bytes({f"{i}.csv": b"1" for i in range(4)}), "entries"),
        (zip_bytes({"a.csv": b"1" * 20_000}, compression=zipfile.ZIP_STORED), "expands to"),
    ],
    ids=["encrypted", "symlink", "duplicate", "dotdot", "absolute", "too-many", "too-big"],
)
def test_archive_refusals(content: bytes, match: str) -> None:
    with pytest.raises(ArchiveRefused, match=match):
        SafeZip(content, CAPS)


def test_archive_reads_member_and_caps_what_it_inflates() -> None:
    assert read_member(zip_bytes({"a.csv": b"A\n1\n"}), "a.csv", CAPS) == b"A\n1\n"
    z = SafeZip(zip_bytes({"a.csv": b"x" * 9_000}), Caps(10_000, 10_000, 3))
    assert len(z.read("a.csv")) == 9_000
    tight = SafeZip(zip_bytes({"a.csv": b"x" * 9_000}), Caps(10_000, 10_000, 3))
    tight.caps = Caps(
        100, 10_000, 3
    )  # a header that lies about its size gains nothing: the read is capped too
    with pytest.raises(ArchiveRefused, match="past its cap"):
        tight.read("a.csv")


# ------------------------------------------------------------------------------- fix round of #148: refusals


def _patched(part: str, old: str, new: str, wb: bytes = WB) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(wb))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name in src.namelist():
            data = src.read(name)
            if name == part:
                assert old.encode() in data
                data = data.replace(old.encode(), new.encode())
            out.writestr(name, data)
    return buf.getvalue()


SHEET = "xl/worksheets/sheet1.xml"


@pytest.mark.parametrize(
    ("content", "match"),
    [
        # two cells at the far corners: tiny on disk, two billion cells as a dense grid
        (_patched(SHEET, '<c r="D2" s="0">', '<c r="BXX999999"><v>1</v></c><c r="D2" s="0">'), "more cells"),
        (_patched(SHEET, '<c r="B2" s="1">', '<c r="B2" s="-1">'), "below zero"),
        (_patched(SHEET, '<c r="B2" s="1">', '<c r="B2" s="9">'), "style the workbook doesn't have"),
        (_patched(SHEET, '<c r="B2" s="1">', '<c r="B2" s="x">'), "not a whole number"),
        (_patched(SHEET, '<c r="C2" s="2">', '<c r="B2" s="2">'), "two cells at B2"),
        (_patched("xl/workbook.xml", 'name="Notes"', 'name="Table 1"'), "2 sheets named"),
    ],
)
def test_ambiguous_or_oversized_sheets_are_refused(content: bytes, match: str) -> None:
    e = entry("https_file", {"format": "xlsx", "sheet": "Table 1"})
    with pytest.raises(LayoutError, match=match):
        list(pages(e, Site({URL: content})))


def test_the_archive_cap_counts_every_part_read() -> None:
    from health_data_registry.adapters.archive import ArchiveRefused, Caps, SafeZip

    zf = SafeZip(zip_bytes({"a.txt": b"x" * 400, "b.txt": b"y" * 400}), Caps(1_000, 1_000, 5))
    zf.read("a.txt")
    zf.read("b.txt")
    with pytest.raises(ArchiveRefused, match="past its cap"):
        zf.read("a.txt")  # 1,200 bytes inflated in all: past the archive's cap of 1,000


def test_a_locate_extension_is_named_once_and_its_row_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError as _VE

    from health_data_registry.adapters.extensions import EXTENSIONS, Extension, Location
    from health_data_registry.source_config import Strict

    monkeypatch.setitem(
        EXTENSIONS,
        "probe_locate",
        Extension("probe_locate", "locate", Strict, lambda a, g: Location(-1), "test"),
    )
    use = {"name": "header_row_finder", "hook": "locate", "args": {"first_cell": "County"}}
    with pytest.raises(_VE, match="one locate extension at most"):  # a second would be ignored
        entry("https_file", {"format": "csv"}, extensions=[use, {"name": "probe_locate", "hook": "locate"}])
    e = entry("https_file", {"format": "csv"}, extensions=[{"name": "probe_locate", "hook": "locate"}])
    with pytest.raises(LayoutError, match="named row -1"):
        list(pages(e, Site({URL: CSV})))


def test_a_header_the_extension_cannot_find_is_a_layout_refusal() -> None:
    e = entry("https_file", {"format": "csv"},
              extensions=[{"name": "header_row_finder", "hook": "locate", "args": {"first_cell": "Region"}}])  # fmt: skip
    with pytest.raises(LayoutError, match="no row in the first"):
        list(pages(e, Site({URL: CSV})))


def test_skipped_footer_rows_are_recorded() -> None:
    e = entry("https_file", {"format": "csv", "skip_footer_rows": 1})
    got = list(pages(e, Site({URL: CSV})))
    assert got[0].provenance["skipped_footer_rows"] == "1" and len(got[0].records) == 1
