"""The html_table adapter and the | delimiter of https_csv, on tiny hand-written pages and files (no public data)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from health_data_registry.adapters import html_table, https_csv, pages, publisher_fields
from health_data_registry.registry import CatalogEntry
from health_data_registry.store import RawFetch

PAGE = """<html><body>
<table id="other"><thead><tr><td>Nope</td></tr></thead><tbody><tr><td>x</td></tr></tbody></table>
<table id = "codes" class="compact">
  <thead><tr><td>Country or Area</td><td>M49 Code</td><td>ISO-alpha3 Code</td><!--<td>Hidden</td>--></tr></thead>
  <tbody>
    <tr><td>Toyland</td><td>001</td><td>TOY</td><!--<td>gone</td>--></tr>
    <tr><td>  Land  of
       Make &amp; Believe </td><td>002</td><td></td></tr>
    <tr><td>Nested <table><tr><td>inner</td></tr></table>cell</td><td>003</td><td>NST</td></tr>
  </tbody>
</table></body></html>"""


def entry(portal: str = "html_table", **source: Any) -> CatalogEntry:
    base: dict[str, Any] = {
        "portal": portal,
        "host": "unstats.un.org",
        "dataset": "toy",
        "landing_url": "https://unstats.un.org/toy/",
        "api_url": "https://unstats.un.org/toy/page/",
    }
    if portal == "html_table":
        base["table_id"] = "codes"
    base.update(source)
    return CatalogEntry.model_validate(
        {
            "id": "toy_codes", "title": "t", "agency": "a", "publisher": "p", "level": "international",
            "research_areas": ["reference"], "years": {"start": 2020}, "geography": ["country"],
            "shape": "registry", "grain": "g", "refresh": "irregular",
            "licence": {"id": "UN-terms", "redistribution": False, "terms_url": "https://www.un.org/terms"},
            "citation": "c", "source": base, "codebook_url": "https://unstats.un.org/x", "contract": "c",
            "semantic_model": "s", "status": "draft",
        }
    )  # fmt: skip


def fetcher(content: bytes):  # type: ignore[no-untyped-def]
    def get(url: str) -> RawFetch:
        return RawFetch(url, content, hashlib.sha256(content).hexdigest(), datetime(2026, 11, 25, tzinfo=UTC))

    return get


def test_reads_only_the_named_table_cell_text_as_published() -> None:
    header, rows = html_table.read_table(entry(), PAGE.encode())
    assert header == ["Country or Area", "M49 Code", "ISO-alpha3 Code"]
    assert rows == [
        {"Country or Area": "Toyland", "M49 Code": "001", "ISO-alpha3 Code": "TOY"},
        {"Country or Area": "Land of Make & Believe", "M49 Code": "002", "ISO-alpha3 Code": ""},
        {"Country or Area": "Nested cell", "M49 Code": "003", "ISO-alpha3 Code": "NST"},
    ]
    (page,) = list(pages(entry(), fetcher(PAGE.encode()), fields=["M49 Code", "ISO-alpha3 Code"], max_rows=2))
    assert page.records == [
        {"M49 Code": "001", "ISO-alpha3 Code": "TOY"},
        {"M49 Code": "002", "ISO-alpha3 Code": ""},
    ]
    assert page.fetch.content == PAGE.encode()  # the raw bytes are kept unchanged
    (everything,) = list(html_table.pages(entry(), fetcher(PAGE.encode())))
    assert len(everything.records) == 3
    (none,) = list(html_table.pages(entry(), fetcher(PAGE.encode()), max_rows=-1))
    assert none.records == []
    assert publisher_fields(entry(), fetcher(PAGE.encode())) == {
        "Country or Area",
        "M49 Code",
        "ISO-alpha3 Code",
    }


@pytest.mark.parametrize(
    ("page", "message"),
    [
        ("<table id='x'></table>", "has no table with id 'codes'"),
        ("<table id='codes'><tbody><tr><td>a</td></tr></tbody></table>", "exactly one header row"),
        ("<table id='codes'><thead><tr><td>a</td></tr><tr><td>b</td></tr></thead></table>", "exactly one header row"),
        ("<table id='codes'><thead><tr><td>a</td><td>a</td></tr></thead></table>", "empty or repeated column"),
        ("<table id='codes'><thead><tr><td>a</td><td> </td></tr></thead></table>", "empty or repeated column"),
        ("<table id='codes'><thead><tr></tr></thead></table>", "empty or repeated column"),
        ("<table id='codes'><thead><tr><td>a</td><td>b</td></tr></thead><tbody><tr><td>1</td></tr></tbody></table>",
         "row 2 of table 'codes' does not have one cell per column"),
    ],
)  # fmt: skip
def test_refuses_a_table_it_cannot_read_row_for_row(page: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        html_table.read_table(entry(), page.encode())


def test_only_the_first_table_with_the_id_is_read() -> None:
    two = PAGE + "<table id='codes'><thead><tr><td>Other</td></tr></thead></table>"
    header, _rows = html_table.read_table(entry(), two.encode())
    assert header[0] == "Country or Area"


def test_catalog_rules_for_table_id_and_delimiter() -> None:
    with pytest.raises(ValidationError, match="names the id of the table it reads"):
        entry(table_id=None)
    with pytest.raises(ValidationError, match="names the id of the table it reads"):
        entry("https_csv", table_id="codes")
    with pytest.raises(ValidationError, match="apply only to the https_csv portal"):
        entry(delimiter="|")
    for bad in ("||", "", '"', "\n"):
        with pytest.raises(ValidationError, match="delimiter is one character"):
            entry("https_csv", delimiter=bad)
    assert entry("https_csv", delimiter="|").source.delimiter == "|"
    assert entry("https_csv").source.delimiter == ","


def test_https_csv_reads_a_pipe_delimited_code_file() -> None:
    text = b"STATE|STATEFP|STATE_NAME\nMI|26|Michigan\nNY|36|New York, the state\n"
    header, rows = https_csv.read_rows(entry("https_csv", delimiter="|"), text)
    assert header == ["STATE", "STATEFP", "STATE_NAME"]
    assert rows[1] == {"STATE": "NY", "STATEFP": "36", "STATE_NAME": "New York, the state"}
    with pytest.raises(ValueError, match="does not have one value per column"):
        https_csv.read_rows(entry("https_csv"), b"A|B\n1|2,3\n")
