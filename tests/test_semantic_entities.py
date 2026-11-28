"""Multi-valued (separator) dimensions and semantic entities, checked when the registry loads (#300)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from health_data_registry.query import Engine
from health_data_registry.registry import Dimension, RegistryError, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import CONTRACT, SEMANTIC, raw_fetch, write_registry

TAGGED = [
    {"states": "MICHIGAN", "year": "2023", "label": "hiv; tuberculosis", "m1": "5", "m2": "1.0"},
    {"states": "OHIO", "year": "2023", "label": "tuberculosis", "m1": "7", "m2": "1.0"},
    {"states": "OHIO", "year": "2024", "label": "hiv", "m1": "11", "m2": "1.0"},
]
MULTI = SEMANTIC.replace(
    'non_additive_reason: "each is a different condition", description: Condition}',
    'non_additive_reason: "one row can name several", separator: "; ", description: Condition}',
)


def test_a_filter_on_a_multi_valued_dimension_matches_any_of_its_values(tmp_path: Path) -> None:
    assert MULTI != SEMANTIC
    reg = load_registry(write_registry(tmp_path / "reg", semantic=MULTI))
    ds = reg.get("toy_cases")
    assert ds.semantic.dimension("condition").separator == "; "
    lake = tmp_path / "lake"
    lake.mkdir()
    nd = tmp_path / "t.ndjson"
    write_ndjson(nd, TAGGED)
    build_snapshot(lake, ds, [raw_fetch("https://data.cdc.gov/resource/toy0-toy0.json", TAGGED)], [nd])
    eng = Engine(registry=reg, lake=lake)
    out = eng.query_metric(
        "toy_cases", "case_count", filters={"condition": "tuberculosis"}, group_by=["state"]
    )
    assert sorted(tuple(r[:2]) for r in out["rows"]) == [
        ("MICHIGAN", 5),
        ("OHIO", 7),
    ]  # "hiv; tuberculosis" too
    out = eng.query_metric("toy_cases", "case_count", filters={"condition": ["hiv"], "state": "OHIO"})
    assert [r[0] for r in out["rows"]] == [11]


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"type": "time", "separator": "; "}, "only a categorical"),
        ({"type": "categorical", "separator": "; ", "non_additive": False}, "overlap"),
        ({"type": "categorical", "separator": ""}, "at least 1"),
    ],
)
def test_a_multi_valued_dimension_is_declared_soundly(fields: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        Dimension.model_validate({"name": "tags", "column": "tags", **fields})


@pytest.mark.parametrize(
    ("entities", "match"),
    [
        (
            "entities:\n  - {name: toy_row, type: primary, columns: [nope], description: d}\n",
            "not contract columns",
        ),
        ("entities:\n  - {name: toy_row, type: primary, columns: [year], description: d}\n", "primary key"),
        ("entities:\n  - {name: other, type: foreign, columns: [year], description: d}\n", "no other model"),
    ],
)
def test_entities_name_contract_keys_and_join_only_on_a_declared_primary(
    tmp_path: Path, entities: str, match: str
) -> None:
    contract = CONTRACT.replace(
        "        required: true\n        description: The state where",
        "        required: true\n        primaryKey: true\n        description: The state where",
    )
    with pytest.raises(RegistryError, match=match):
        load_registry(write_registry(tmp_path / "reg", contract=contract, semantic=SEMANTIC + entities))


def test_the_croi_stores_share_one_entity() -> None:
    reg = load_registry()
    idx, txt = reg.get("croi_abstracts_index"), reg.get("croi_abstracts_text")
    assert [(e.name, e.type) for e in idx.semantic.entities] == [("croi_abstract", "primary")]
    assert [(e.name, e.type) for e in txt.semantic.entities] == [("croi_abstract", "foreign")]
    for ds in (idx, *(reg.get(f"{c}_abstracts") for c in ("apic", "icid", "idweek", "shea"))):
        assert ds.semantic.dimension("condition").separator == "; "
