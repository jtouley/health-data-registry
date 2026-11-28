"""Staging of a publisher field that holds a number or a code in its place, with digit grouping (HepTracker).

Synthetic values only (hard rule 5). The flag column (customProperties inlineCodes) keeps every text that isn't a
plain number, trimmed, and the number reads it as empty: a code is never a zero, a new code never stops the load,
and no published text is lost from both columns (hard rules 1 and 4).
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from health_data_registry.registry import RegistryError, parse_contract
from health_data_registry.staging import (
    inline_flag,
    inline_number,
    stage,
    typed_select,
    ungroup,
    ungrouped_sql,
)

LOGICAL = {"BIGINT": "integer", "DOUBLE": "number", "VARCHAR": "string"}
CODES = {"—": "not reported", "U": "unavailable", "S": "suppressed"}


def _contract(
    count_props: list[dict[str, Any]] | None = None,
    *,
    flag: bool = True,
    ptype: str = "BIGINT",
    flag_props: list[dict[str, Any]] | None = None,
) -> Any:
    count = [{"property": "role", "value": "measure"}, {"property": "sourceField", "value": "count"}]
    count += count_props or [
        {"property": "missingCodes", "value": list(CODES)},
        {"property": "thousandsSeparator", "value": ","},
    ]
    props: list[dict[str, Any]] = [
        {"name": "place", "physicalType": "VARCHAR", "logicalType": "string", "required": True,
         "description": "Place.", "customProperties": [{"property": "role", "value": "dimension"}]},
        {"name": "cases", "physicalType": ptype, "logicalType": LOGICAL[ptype], "description": "Cases.",
         "customProperties": count},
    ]  # fmt: skip
    if flag:
        props.append(
            {"name": "cases_flag", "physicalType": "VARCHAR", "logicalType": "string", "description": "Code.",
             "customProperties": [{"property": "role", "value": "attribute"},
                                  {"property": "sourceField", "value": "count"},
                                  {"property": "flagFor", "value": "cases"},
                                  {"property": "flagCodes", "value": CODES}]
                                 + (flag_props if flag_props is not None
                                    else [{"property": "inlineCodes", "value": True}])}
        )  # fmt: skip
    doc = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": "toy",
        "version": "0.1.0",
        "schema": [{"name": "toy", "customProperties": [{"property": "totalRows", "value": "none"}],
                    "properties": props}],
        "customProperties": [{"property": "unreliableRule", "value": {"kind": "none", "description": "None."}}],
    }  # fmt: skip
    return parse_contract(doc, expected_id="toy")


def _stage(contract: Any, counts: list[str | None]) -> tuple[list[tuple[Any, ...]], Any]:
    con = duckdb.connect()
    con.execute("CREATE TABLE src (place VARCHAR, count VARCHAR)")
    con.executemany("INSERT INTO src VALUES (?, ?)", [[f"p{i}", c] for i, c in enumerate(counts)])
    report = stage(con, contract, "src", table="t")
    return con.execute("SELECT * FROM t ORDER BY place").fetchall(), report


def test_a_grouped_number_reads_whole_and_a_code_stays_beside_an_empty_count() -> None:
    rows, report = _stage(_contract(), ["1,234", "12,345,678", "7", "—", "S", None, " 2,000 ", " S "])
    assert rows == [
        ("p0", 1234, None),
        ("p1", 12345678, None),
        ("p2", 7, None),
        ("p3", None, "—"),
        ("p4", None, "S"),
        ("p5", None, None),
        ("p6", 2000, None),
        ("p7", None, "S"),
    ]
    assert report.unlisted_codes == {}


def test_a_new_code_is_kept_and_reported_never_a_failed_load() -> None:
    rows, report = _stage(_contract(), ["NA", "1,5", "3"])
    # "1,5" is not whole groups of three, so it is never read as 15: it is kept as published, as a code.
    assert rows == [("p0", None, "NA"), ("p1", None, "1,5"), ("p2", 3, None)]
    assert report.unlisted_codes == {"cases_flag": ("1,5", "NA")}
    assert "'1,5', 'NA'" in report.notes()[0]


def test_without_an_inline_flag_a_new_code_still_stops_the_load() -> None:
    contract = _contract(flag=False)
    assert inline_flag(contract, contract.column("cases")) is None
    with pytest.raises(duckdb.ConversionException):
        _stage(contract, ["NA"])


def test_a_fraction_in_a_count_still_stops_the_load_with_an_inline_flag() -> None:
    with pytest.raises(Exception, match="has a fraction"):
        _stage(_contract(), ["1,234.5"])


def test_a_double_keeps_its_decimals_and_drops_only_grouping() -> None:
    rows, _ = _stage(_contract(ptype="DOUBLE"), ["1,234.5", "0.07", "7.0000000000000007E-2", "UR"])
    assert rows == [("p0", 1234.5, None), ("p1", 0.07, None), ("p2", 0.07, None), ("p3", None, "UR")]
    assert "TRY_CAST" in typed_select(_contract(ptype="DOUBLE"))


@pytest.mark.parametrize(("ptype", "sep"), [("VARCHAR", ","), ("BIGINT", "."), ("BIGINT", 1)])
def test_thousands_separator_is_one_grouping_character_on_a_numeric_column(ptype: str, sep: Any) -> None:
    with pytest.raises(RegistryError, match="thousandsSeparator"):
        _contract([{"property": "thousandsSeparator", "value": sep}], flag=False, ptype=ptype)


@pytest.mark.parametrize("ptype", ["BIGINT", "DOUBLE"])
def test_text_duckdb_reads_as_a_number_but_no_publisher_means_as_one_is_a_code(ptype: str) -> None:
    texts = ["NaN", "inf", "Infinity", "-inf", "1_000", "1e40", "1" + "0" * 35]
    rows, report = _stage(_contract(ptype=ptype), texts)
    numbers = {"BIGINT": [None] * 5 + [None, None], "DOUBLE": [None] * 5 + [1e40, 1e35]}[ptype]
    # Every text lands in exactly one column: a number, or the flag as published (trimmed). Never neither.
    assert [r[1] for r in rows] == numbers
    for (_place, number, code), text in zip(rows, texts, strict=True):
        assert (number is None) == (code == text)
    assert set(report.unlisted_codes["cases_flag"]) >= {"NaN", "inf", "Infinity", "-inf", "1_000"}


def test_a_numeric_looking_missing_code_is_kept_in_the_flag_never_lost() -> None:
    rows, report = _stage(_contract([{"property": "missingCodes", "value": ["-1"]}]), ["-1", "4"])
    assert rows == [("p0", None, "-1"), ("p1", 4, None)]
    assert report.unlisted_codes == {"cases_flag": ("-1",)}


def test_the_flag_and_the_number_split_the_field_by_construction() -> None:
    contract = _contract()
    flag = contract.column("cases_flag")
    assert inline_flag(contract, contract.column("cases")) is flag
    assert inline_number(contract, flag) is contract.column("cases")
    assert inline_number(contract, contract.column("place")) is None


def test_a_flag_on_its_numbers_own_field_must_declare_inline_codes() -> None:
    with pytest.raises(RegistryError, match="declare inlineCodes"):
        _contract(flag_props=[])


def test_inline_codes_is_a_boolean() -> None:
    with pytest.raises(RegistryError, match="inlineCodes is true or false"):
        _contract(flag_props=[{"property": "inlineCodes", "value": "yes"}])


def test_inline_codes_needs_a_varchar_flag_on_the_same_field() -> None:
    doc_flag = _contract().column("cases_flag")
    assert doc_flag.inline_codes is True
    with pytest.raises(RegistryError, match="inlineCodes needs flagFor"):
        _contract(ptype="DOUBLE", flag_props=[{"property": "inlineCodes", "value": True},
                                             {"property": "sourceField", "value": "other"}])  # fmt: skip


@pytest.mark.parametrize(
    "text", ["1,234", "12,345,678.5", " 2,000 ", "1,5", "12,34", "1,2345", "NA", "-1,000"]
)
def test_the_python_reading_of_digit_grouping_matches_the_sql_one(text: str) -> None:
    # The live checks read the publisher's text with ungroup(); staging reads it with ungrouped_sql(). Same rule.
    sql = duckdb.connect().execute(f"SELECT {ungrouped_sql('?', ',')}", [text] * 3).fetchone()
    assert sql is not None and ungroup(text, ",") == sql[0]
