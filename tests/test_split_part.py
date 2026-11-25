"""customProperties.splitPart: one part of a field that packs several values into one text (NIS's 95% CI)."""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from health_data_registry.registry import Contract, RegistryError, parse_contract
from health_data_registry.staging import PublisherDataRefused, stage, typed_select
from health_data_registry.transform_gen import _columns

SPLIT = {"separator": " to ", "part": 1, "parts": 2}


def _contract(lower: dict[str, Any] | None = None, upper: dict[str, Any] | None = None) -> Contract:
    def col(
        name: str, split: dict[str, Any] | None, extra: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        props: list[dict[str, Any]] = [
            {"property": "role", "value": "measure"},
            {"property": "sourceField", "value": "_95_ci"},
            *(extra or []),
        ]
        if split is not None:
            props.append({"property": "splitPart", "value": split})
        return {
            "name": name,
            "physicalType": "DOUBLE",
            "logicalType": "number",
            "description": f"The {name} of the published 95% confidence interval.",
            "customProperties": props,
        }

    doc = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": "toy_cov",
        "version": "0.1.0",
        "status": "draft",
        "customProperties": [
            {"property": "unreliableRule", "value": {"kind": "none", "description": "None (toy)."}}
        ],
        "schema": [
            {
                "name": "toy_cov",
                "customProperties": [{"property": "totalRows", "value": "none"}],
                "properties": [
                    {
                        "name": "place",
                        "physicalType": "VARCHAR",
                        "logicalType": "string",
                        "required": True,
                        "description": "Place.",
                        "customProperties": [{"property": "role", "value": "dimension"}],
                    },
                    col(
                        "ci_lower",
                        SPLIT if lower is None else lower,
                        [{"property": "missingCodes", "value": ["NA"]}],
                    ),
                    col("ci_upper", {**SPLIT, "part": 2} if upper is None else upper),
                ],
            }
        ],
    }
    return parse_contract(doc, expected_id="toy_cov")


def _stage(
    contract: Contract, values: list[str | None]
) -> tuple[duckdb.DuckDBPyConnection, list[tuple[Any, ...]]]:
    con = duckdb.connect()
    con.execute("CREATE TABLE src (place VARCHAR, _95_ci VARCHAR)")
    con.executemany("INSERT INTO src VALUES (?, ?)", [(f"p{i}", v) for i, v in enumerate(values)])
    stage(con, contract, "src", table="toy_cov", present={"place", "_95_ci"})
    return con, con.execute("SELECT place, ci_lower, ci_upper FROM toy_cov ORDER BY place").fetchall()


def test_both_bounds_come_from_one_published_field() -> None:
    c = _contract()
    assert c.column("ci_lower").split_part == (" to ", 1, 2)
    assert c.column("ci_upper").split_part == (" to ", 2, 2)
    assert c.column("place").split_part is None


def test_values_parse_exactly() -> None:
    _, rows = _stage(_contract(), ["60.3 to 68.3", " 0.4 to 100.0 ", None, ""])
    assert rows == [("p0", 60.3, 68.3), ("p1", 0.4, 100.0), ("p2", None, None), ("p3", None, None)]


@pytest.mark.parametrize("bad", ["60.3-68.3", "1 to 2 to 3", "NA"])
def test_a_value_that_does_not_split_stops_the_load(bad: str) -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE src (place VARCHAR, _95_ci VARCHAR)")
    con.execute("INSERT INTO src VALUES ('p', ?)", [bad])
    # The lower column blanks NA as a missing code; the upper column has no missing codes, so NA stops the load.
    with pytest.raises(
        PublisherDataRefused,
        match="^toy_cov: the publisher's data has a value in _95_ci that doesn't split, as the contract says it "
        "does, into 2 parts$",
    ):
        stage(con, _contract(), "src", table="toy_cov")
    assert not con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'toy_cov'").fetchone()[0]  # type: ignore[index]


def test_the_split_is_in_the_typed_projection() -> None:
    sql = typed_select(_contract())
    assert "split_part(" in sql and "string_split(" in sql and "' to '" in sql


@pytest.mark.parametrize(
    ("split", "message"),
    [
        ("x", "splitPart must be {separator, part, parts}"),
        ({"separator": " to ", "part": 1}, "splitPart must be {separator, part, parts}"),
        ({**SPLIT, "extra": 1}, "splitPart must be {separator, part, parts}"),
        ({**SPLIT, "separator": "  "}, "separator must be visible text"),
        ({**SPLIT, "separator": 3}, "separator must be visible text"),
        ({**SPLIT, "separator": "'"}, "separator must be visible text"),
        ({**SPLIT, "part": "1"}, "part and parts must be whole numbers"),
        ({**SPLIT, "parts": True}, "part and parts must be whole numbers"),
        ({**SPLIT, "part": 0}, "a part from 1 to parts"),
        ({**SPLIT, "part": 3}, "a part from 1 to parts"),
        ({**SPLIT, "part": 1, "parts": 1}, "parts of at least 2"),
    ],
)
def test_a_malformed_split_is_refused(split: Any, message: str) -> None:
    with pytest.raises(
        RegistryError, match=f"^toy_cov.ci_lower: .*{message.replace('{', '[{]').replace('}', '[}]')}"
    ):
        _contract(lower=split)


def test_a_flag_column_keeps_its_codes_whole() -> None:
    doc_contract = _contract()
    raw = doc_contract.raw
    props = raw["schema"][0]["properties"]
    props[1]["customProperties"].append({"property": "flagFor", "value": "ci_upper"})
    props[1]["customProperties"] = [
        p for p in props[1]["customProperties"] if p["property"] != "missingCodes"
    ]
    with pytest.raises(RegistryError, match="^toy_cov.ci_lower: a flag column keeps its codes whole"):
        parse_contract(raw, expected_id="toy_cov")


def test_dbt_staging_meta_names_the_split_only_where_declared() -> None:
    cols = {c["name"]: c for c in _columns(_contract(), "stg_toy_cov")}
    assert cols["ci_upper"]["meta"]["split_part"] == {"separator": " to ", "part": 2, "parts": 2}
    assert "split_part" not in cols["place"]["meta"]
