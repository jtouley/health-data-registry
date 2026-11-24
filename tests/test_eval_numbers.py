"""eval/numbers.py: numeric tokens, response tables and pools, and the derivation search, on made-up text."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from eval.numbers import (
    ALLOWED_DERIVATIONS,
    FORBIDDEN_DERIVATIONS,
    SEPARATE_SUBAREAS,
    Derivation,
    column_kind,
    is_aggregate,
    pools,
    rounds_to,
    subarea_pair,
    tables,
    tokens,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("310 cases", [("310", "value", False, True)]),
        ("1,234,567 cases", [("1,234,567", "value", False, True)]),
        ("12.5% of isolates", [("12.5", "value", True, False)]),
        ("12.5 percent", [("12.5", "value", True, False)]),
        ("in 2024", [("2024", "year", False, False)]),
        ("2024 cases", [("2024", "value", False, True)]),
        ("In 2024 cases rose", [("2024", "year", False, True)]),
        ("from 2023 the publisher reports", [("2023", "year", False, False)]),
        ("2,024 people", [("2,024", "value", False, True)]),
        ("1899 here", [("1899", "value", False, False)]),
        ("2101 here", [("2101", "value", False, False)]),
        ("1900 and 2100", [("1900", "year", False, False), ("2100", "year", False, False)]),
        ("week 45", [("45", "time", False, False)]),
        ("Years: 2011-2022.", [("2011", "year", False, False), ("2022", "year", False, False)]),
        ("the 2025-26 season", [("2025", "year", False, False)]),
        ("aged 65", [("65", "time", False, False)]),
        ("per 100,000", [("100,000", "denominator", False, False)]),
        ("H5N1 v3.2 0.1.0 5huc 20261124T22 2026-11-24 22:01", [("2026", "year", False, False)]),
        ("", []),
    ],
)
def test_tokens_kinds_and_precision(text: str, want: list[tuple[str, str, bool, bool]]) -> None:
    # Act
    got = tokens(text)
    # Assert
    assert [(t.text, t.kind, t.percent, t.count_context) for t in got] == want


def test_tokens_value_and_decimals() -> None:
    # Act
    (tok,) = tokens("rate 1,234.50 here")
    # Assert
    assert (tok.value, tok.decimals, tok.plain) == (Decimal("1234.50"), 2, "1234.50")


@pytest.mark.parametrize(
    ("value", "decimals", "target", "want"),
    [
        (Decimal("2.337"), 2, Decimal("2.34"), True),
        (Decimal("2.335"), 2, Decimal("2.34"), True),
        (Decimal("2.3449"), 2, Decimal("2.34"), True),
        (Decimal("2.346"), 2, Decimal("2.34"), False),
        (Decimal("116"), 1, Decimal("116.0"), True),
        (Decimal("116.4"), 0, Decimal("116"), True),
        (Decimal("116.6"), 0, Decimal("116"), False),
        (Decimal("310"), 0, Decimal("311"), False),
    ],
)
def test_rounds_to_precision_shown(value: Decimal, decimals: int, target: Decimal, want: bool) -> None:
    # Act / Assert
    assert rounds_to(value, decimals, target) is want


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("year", "time"), ("last_week", "time"), ("states", "place"), ("location", "place"), ("label", "series"),
        ("observation_type", "series"), ("ytd_cases", "running"), ("m3", "running"), ("m2", "maximum"),
        ("max_week", "maximum"), ("rate", "rate"), ("pct_resistant", "rate"), ("value", "count"), ("m1", "count"),
        ("weekly_cases", "count"), ("flag", "other"),
    ],
)  # fmt: skip
def test_column_kind_from_name(name: str, kind: str) -> None:
    # Act / Assert
    assert column_kind(name) == kind


def test_tables_read_csv_blocks_between_comments() -> None:
    # Arrange
    response = 'location,year,value\nToyland,2024,310\n"Toy, land",2023,"1,234"\n\n# note 52\nbad,line\nx,y\n'
    # Act
    got = tables(response)
    # Assert
    assert got[0].header == ("location", "year", "value")
    assert got[0].rows == (("Toyland", "2024", "310"), ("Toy, land", "2023", "1,234"))
    assert got[0].span == (0, 3)
    assert got[1].header == ("bad", "line")


def test_tables_skip_prose_with_commas() -> None:
    # Act / Assert
    assert tables("Places: nation, region, state.\nYears: 2022-present, weekly.") == []


def test_pools_split_values_from_times_and_args() -> None:
    # Arrange
    response = (
        "location,year,week,value,status\nToyland,2024,52,310,provisional through week 51\n# 12.5% share\n"
    )
    # Act
    got = pools([response], ['{"year_start": 2019}'])
    # Assert
    assert got.values == {Decimal(310), Decimal("12.5")}
    assert got.percents == {Decimal("12.5")}
    assert got.times == {Decimal(2024), Decimal(52), Decimal(51), Decimal(2019)}
    assert Decimal(2019) not in got.response_times
    assert got.has_any(Decimal(2019)) and not got.has_any(Decimal(7))


@pytest.mark.parametrize(
    ("place", "aggregate"),
    [
        ("TOTAL", True), ("US RESIDENTS", True), ("U.S. Residents", True), ("US TERRITORIES", True),
        ("New England", True), ("PACIFIC", True), ("United States", True), ("Michigan", False),
        ("New York City", False), ("Non-U.S. Residents", False),
    ],
)  # fmt: skip
def test_is_aggregate_knows_total_rows(place: str, aggregate: bool) -> None:
    # Act / Assert
    assert is_aggregate(place) is aggregate


@pytest.mark.parametrize(
    ("places", "mixed"),
    [
        (["NEW YORK", "NEW YORK CITY"], True),
        (["New York", "N.Y.C."], True),
        (["New York"], False),
        ([], False),
    ],
)
def test_subarea_pair(places: list[str], mixed: bool) -> None:
    # Act / Assert
    assert subarea_pair(places) is mixed


def test_derivation_lists_name_the_rubric_rules() -> None:
    # Act / Assert
    assert set(ALLOWED_DERIVATIONS) == {"sum_across_weeks", "sum_across_places"}
    assert Derivation("sum_across_weeks").allowed
    assert not any(Derivation(name).allowed for name in FORBIDDEN_DERIVATIONS)


def test_separate_subareas_match_the_contracts_partial_places() -> None:
    # Arrange: every partialPlaces entry in a contract must have its sub-area here.
    parents: set[str] = set()
    for path in (ROOT / "contracts").glob("*.odcs.yaml"):
        doc = yaml.safe_load(path.read_text())
        for obj in doc.get("schema", []):
            for prop in obj.get("properties", []):
                for custom in prop.get("customProperties", []):
                    if custom.get("property") == "partialPlaces":
                        parents |= {k.lower() for k in custom["value"] if k != "*"}
    # Act / Assert
    assert parents
    assert parents <= set(SEPARATE_SUBAREAS.values())
