"""https_file: csv, xlsx and zip members, table-driven over tiny hand-written files (no publisher's file)."""

from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from dataclasses import dataclass, field
from typing import Any

import pytest

from health_data_registry.adapters import LayoutError, pages, publisher_fields
from health_data_registry.adapters.archive import ArchiveRefused, Caps, SafeZip, read_member
from health_data_registry.adapters.row_guard import RowLevelRefused
from health_data_registry.adapters.xlsx import XlsxRefused
from tests.adapters.helpers import HOST, Site, entry, xlsx_bytes, zip_bytes

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
        assert got[0].records == case.records
    if "zip_member" in case.block:
        member = case.block["zip_member"]
        data = zipfile.ZipFile(io.BytesIO(case.content)).read(member)
        assert got[0].provenance == {"zip_member": member, "member_sha256": hashlib.sha256(data).hexdigest()}


def test_xlsx_in_zip_reads_the_same_as_alone() -> None:
    alone = list(pages(entry("https_file", {"format": "xlsx", "sheet": "Table 1"}), Site({URL: WB})))
    zipped = entry("https_file", {"format": "xlsx", "sheet": "Table 1", "zip_member": "book.xlsx"})
    inside = list(pages(zipped, Site({URL: zip_bytes({"book.xlsx": WB})})))
    assert alone[0].records == inside[0].records


def test_fields_narrow_the_records_and_keep_their_formats() -> None:
    e = entry("https_file", {"format": "xlsx", "sheet": "Table 1"})
    got = list(pages(e, Site({URL: WB}), fields=["Share"]))
    assert got[0].records == [{"Share": "0.123", "Share (number format)": "0.0%"}]


def test_publisher_fields() -> None:
    assert publisher_fields(entry("https_file", {"format": "csv"}), Site({URL: CSV})) == {
        "County",
        "Year",
        "Cases",
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
