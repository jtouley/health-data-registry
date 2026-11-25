"""Semantic-model entities (declared joins between models) and multi-valued dimensions (fix round, PR #30)."""

from __future__ import annotations

from pathlib import Path

import pytest

from health_data_registry import tools
from health_data_registry.query import QueryRefused, compile_metric
from health_data_registry.registry import Registry, RegistryError, load_registry
from tests.conftest import CONTRACT, SEMANTIC, write_registry

MULTI = SEMANTIC.replace(
    "{name: condition, column: condition, type: categorical, non_additive: true, "
    'non_additive_reason: "each is a different condition", description: Condition}',
    '{name: condition, column: condition, type: categorical, separator: "; ", non_additive: true, '
    'non_additive_reason: "each is a different condition", description: Conditions}',
)


@pytest.fixture(scope="module")
def real() -> Registry:
    return load_registry()


def test_the_croi_join_is_declared_on_both_models(real: Registry) -> None:
    index, text = real.get("croi_abstracts_index").semantic, real.get("croi_abstracts_text").semantic
    assert [(e.name, e.type, e.columns) for e in index.entities] == [
        ("croi_abstract", "primary", ["abstract_number", "year"])
    ]
    assert [(e.name, e.type, e.columns) for e in text.entities] == [
        ("croi_abstract", "foreign", ["abstract_number", "year"])
    ]
    for index_id, text_id in tools.TEXT_SOURCE.items():  # every join find_prior_work makes is declared
        names = {e.name for e in real.get(index_id).semantic.entities if e.type == "primary"}
        assert names & {e.name for e in real.get(text_id).semantic.entities if e.type == "foreign"}


def test_croi_tags_are_multi_valued_and_year_is_categorical(real: Registry) -> None:
    sem = real.get("croi_abstracts_index").semantic
    assert {d.name for d in sem.dimensions if d.separator == "; "} == {
        "condition", "design", "data_source", "geography",
    }  # fmt: skip
    assert sem.dimension("year").type == "categorical"


def test_a_multi_valued_dimension_is_never_grouped_or_filtered(tmp_path: Path) -> None:
    sem = MULTI.split("series:")[0] + "series: []\n"
    ds = load_registry(write_registry(tmp_path / "r", semantic=sem)).get("toy_cases")
    for kw in ({"group_by": ["condition"]}, {"filters": {"condition": "hiv"}}):
        with pytest.raises(QueryRefused) as e:
            compile_metric(ds, "case_count", **kw)  # type: ignore[arg-type]
        assert e.value.code == "multi_valued_dimension" and "several values" in str(e.value)
    # Other dimensions are not refused as multi-valued. (A sum by state alone is still refused, by hard rule 2
    # (#49): it would add across conditions, which can neither be pinned nor grouped here.)
    with pytest.raises(QueryRefused) as other:
        compile_metric(ds, "case_count", group_by=["state"])
    assert other.value.code == "non_additive"


@pytest.mark.parametrize(
    ("semantic", "message"),
    [
        (MULTI, "multi-valued dimension condition"),  # a series filters on it
        (
            SEMANTIC.replace(
                "{name: year, column: year, type: time, non_additive: false, description: MMWR year}",
                '{name: year, column: year, type: time, separator: ";", non_additive: false, '
                "description: MMWR year}",
            ),
            "only a categorical dimension",
        ),
        (
            SEMANTIC.replace(
                "{name: condition, column: condition, type: categorical, non_additive: true, "
                'non_additive_reason: "each is a different condition", description: Condition}',
                '{name: condition, column: condition, type: categorical, separator: " ", non_additive: true, '
                'non_additive_reason: "each is a different condition", description: C}',
            ),
            "only a categorical dimension",
        ),
        (
            SEMANTIC.replace(
                "dimensions:",
                "entities:\n  - {name: a, type: primary, columns: [year]}\n"
                "  - {name: b, type: primary, columns: [year]}\ndimensions:",
            ),
            "at most one primary entity",
        ),
        (
            SEMANTIC.replace(
                "dimensions:", "entities:\n  - {name: a, type: primary, columns: [nope]}\ndimensions:"
            ),
            "entity a uses unknown column nope",
        ),
        (
            SEMANTIC.replace(
                "dimensions:", "entities:\n  - {name: a, type: primary, columns: [year]}\ndimensions:"
            ),
            "primary entity a is year but the contract's primary key is not declared",
        ),
        (
            SEMANTIC.replace(
                "dimensions:", "entities:\n  - {name: a, type: foreign, columns: [year]}\ndimensions:"
            ),
            "foreign entity a names no other model's primary entity",
        ),
    ],
)
def test_bad_semantic_models_are_refused(tmp_path: Path, semantic: str, message: str) -> None:
    with pytest.raises(RegistryError, match=message):
        load_registry(write_registry(tmp_path / "r", semantic=semantic))


def test_a_metric_may_not_read_a_multi_valued_dimension(tmp_path: Path) -> None:
    sem = MULTI.split("series:")[0].replace(
        "aggregation: sum, exclude_total_rows: true}",
        "aggregation: sum, exclude_total_rows: true, single_value_of: [condition]}",
    )
    with pytest.raises(RegistryError, match="multi-valued dimension condition"):
        load_registry(write_registry(tmp_path / "r", semantic=sem + "series: []\n"))


def test_a_primary_entity_matches_the_contract_key_and_foreign_ones_match_its_width(tmp_path: Path) -> None:
    contract = CONTRACT.replace("      - name: year\n", "      - name: year\n        primaryKey: true\n", 1)
    assert contract != CONTRACT
    root = write_registry(
        tmp_path / "r",
        contract=contract,
        semantic=SEMANTIC.replace(
            "dimensions:", "entities:\n  - {name: k, type: primary, columns: [year]}\ndimensions:"
        ),
    )
    reg = load_registry(root)
    assert reg.get("toy_cases").semantic.entities[0].name == "k"
    for d in ("catalog", "contracts", "semantic"):
        src = (root / d / ("toy_cases.yaml" if d != "contracts" else "toy_cases.odcs.yaml")).read_text()
        name = "toy_other.yaml" if d != "contracts" else "toy_other.odcs.yaml"
        (root / d / name).write_text(src.replace("toy_cases", "toy_other"))
    other = root / "semantic" / "toy_other.yaml"
    other.write_text(
        other.read_text().replace(
            "{name: k, type: primary, columns: [year]}",
            "{name: k, type: foreign, columns: [year, condition]}",
        )
    )
    with pytest.raises(RegistryError, match="foreign entity k has 2 columns but toy_cases's has 1"):
        load_registry(root)
    other.write_text(other.read_text().replace("columns: [year, condition]", "columns: [year]"))
    assert load_registry(root).get("toy_other").semantic.entities[0].type == "foreign"
