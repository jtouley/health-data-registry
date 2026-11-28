"""Contract-declared dbt-expectations checks: one registry, refused when unknown, written as dbt tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import dq_checks as dq
from health_data_registry import release_gate as rg
from health_data_registry import transform_gen as tg
from health_data_registry.quality import parse_rules
from health_data_registry.registry import RegistryError, load_registry

REPO = Path(__file__).resolve().parents[1]


def _doc(
    table: list[Any] | None = None, column: list[Any] | None = None, sla: list[Any] | None = None
) -> dict:
    prop: dict[str, Any] = {"name": "cases", "physicalType": "BIGINT", "logicalType": "integer"}
    if column is not None:
        prop["quality"] = column
    doc: dict[str, Any] = {"schema": [{"name": "toy", "properties": [prop], "quality": table or []}]}
    if sla is not None:
        doc["slaProperties"] = sla
    return doc


def _rule(rid: str = "r1", **impl: Any) -> dict[str, Any]:
    return {
        "id": rid,
        "type": "custom",
        "engine": "dbt-expectations",
        "implementation": impl,
        "dimension": "consistency",
        "severity": "error",
        "description": "From the codebook.",
    }


def test_a_table_check_becomes_its_dbt_expectations_test() -> None:
    (check,) = dq.parse_checks(
        _doc([_rule(check="row_count_matches", model="staging", compare="raw")]), "toy"
    )
    assert (check.id, check.check, check.model, check.column) == ("r1", "row_count_matches", "staging", None)
    assert dq.dbt_test(check, "toy", "contracts/toy.odcs.yaml") == {
        "dbt_expectations.expect_table_row_count_to_equal_other_table": {
            "name": "toy_r1",
            "arguments": {"compare_model": "source('raw', 'toy')"},
            "config": {
                "severity": "error",
                "meta": {
                    "dimension": "consistency",
                    "rule": "r1",
                    "contract": "contracts/toy.odcs.yaml",
                    "check": "row_count_matches",
                    "checks": "From the codebook.",
                },
            },
        }
    }


def test_a_column_check_keeps_number_types_drops_an_unset_bound_and_runs_under_its_column() -> None:
    (check,) = dq.parse_checks(_doc(column=[_rule(check="values_between", model="staging", min=1)]), "toy")
    assert check.model == "staging" and check.on_column
    (spec,) = dq.dbt_test(check, "toy", "c").values()
    # Under its column dbt passes column_name itself, so the test leaves it out.
    assert spec["arguments"] == {"min_value": 1, "row_condition": '"cases" is not null'}


def test_a_total_check_sums_its_column_against_the_other_layer() -> None:
    (check,) = dq.parse_checks(
        _doc(column=[_rule(check="total_matches", model="intermediate", compare="staging")]), "toy"
    )
    assert not check.on_column  # its test takes no column_name: it runs on the model
    (spec,) = dq.dbt_test(check, "toy", "c").values()
    assert spec["arguments"] == {"expression": 'sum("cases")', "compare_model": "ref('stg_toy')"}


@pytest.mark.parametrize(
    ("unit", "value", "expected"),
    [("w", 1, ("day", 7)), ("y", 1, ("year", 1)), ("days", 3, ("day", 3)), ("month", 2, ("month", 2))],
)
def test_freshness_reads_its_cadence_from_the_sla(unit: str, value: int, expected: tuple[str, int]) -> None:
    doc = _doc([_rule(check="load_within_sla", model="staging", sla="frequency")],
               sla=[{"property": "frequency", "value": value, "unit": unit}])  # fmt: skip
    (check,) = dq.parse_checks(doc, "toy")
    (spec,) = dq.dbt_test(check, "toy", "c").values()
    assert (spec["arguments"]["datepart"], spec["arguments"]["interval"]) == expected
    assert spec["arguments"]["column_name"] == dq.SNAPSHOT_TIME_SQL


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        (_doc([_rule(check="row_count_is_nice")]), "not a registered check type"),
        (_doc([_rule(check="row_count_matches", compare="raw")]), "needs model"),
        (_doc(column=[_rule(check="total_matches", model="staging", compare="raw")]), "wrong kind"),
        (_doc([_rule(check="row_count_matches", model="staging", compare=["raw"])]), "wrong kind"),
        (_doc([_rule(check="row_count_matches", model={"x": 1}, compare="raw")]), "wrong kind"),
        (_doc(column=[_rule(check="values_between", model="staging", min=float("nan"))]), "wrong kind"),
        (_doc(column=[_rule(check="values_between", model="staging", max=float("inf"))]), "wrong kind"),
        (
            _doc([_rule("Rows Check", check="row_count_matches", model="staging", compare="raw")]),
            "lower-case",
        ),
        (
            _doc([{**_rule(check="row_count_matches", model="staging", compare="raw"), "engine": "soda"}]),
            "engine must be",
        ),
        (
            _doc(
                [{**_rule(check="row_count_matches", model="staging", compare="raw"), "implementation": "x"}]
            ),
            "implementation",
        ),
        (
            _doc([{**_rule(check="row_count_matches", model="staging", compare="raw"), "id": ""}]),
            "needs an id",
        ),
        (_doc(column=[_rule(check="row_count_matches", model="staging", compare="raw")]), "table check"),
        (_doc([_rule(check="values_between", model="staging", min=1)]), "column check"),
        (
            _doc([_rule(check="row_count_matches", model="staging", compare="raw", tolerance=1)]),
            "no parameter tolerance",
        ),
        (_doc([_rule(check="row_count_matches", model="staging", compare="lake")]), "wrong kind"),
        (_doc([_rule(check="row_count_matches", model="raw", compare="staging")]), "wrong kind"),
        (_doc(column=[_rule(check="values_between", model="staging", min="1")]), "wrong kind"),
        (_doc(column=[_rule(check="values_between", model="staging", min=True)]), "wrong kind"),
        (_doc([_rule(check="row_count_matches", model="staging")]), "needs compare"),
        (_doc(column=[_rule(check="values_between", model="staging")]), "at least one of max, min"),
        (_doc(column=[_rule(check="values_between", model="staging", min=5, max=1)]), "min is above max"),
        (_doc([_rule(check="row_count_matches", model="staging", compare="staging")]), "not with itself"),
        (
            _doc(
                [{**_rule(check="row_count_matches", model="staging", compare="raw"), "dimension": "vibes"}]
            ),
            "dimension",
        ),
        (
            _doc([{**_rule(check="row_count_matches", model="staging", compare="raw"), "severity": "info"}]),
            "severity",
        ),
        (
            _doc([{**_rule(check="row_count_matches", model="staging", compare="raw"), "description": " "}]),
            "description",
        ),
        (_doc([_rule(check="load_within_sla", model="staging", sla="frequency")]), "doesn't declare"),
        (
            _doc(
                [_rule(check="load_within_sla", model="staging", sla="frequency")],
                sla=[{"property": "frequency", "value": 1, "unit": "fortnight"}],
            ),
            "unit",
        ),  # fmt: skip
        (
            _doc(
                [_rule(check="load_within_sla", model="staging", sla="frequency")],
                sla=[{"property": "frequency", "value": 0, "unit": "w"}],
            ),
            "whole positive",
        ),  # fmt: skip
    ],
)
def test_validation_refuses_a_check_the_registry_does_not_fit(doc: dict, message: str) -> None:
    with pytest.raises(dq.CheckInvalid, match=message):
        dq.parse_checks(doc, "toy")


def test_the_read_path_skips_checks_but_their_ids_stay_unique() -> None:
    doc = _doc([_rule(check="row_count_matches", model="intermediate", compare="staging")])
    assert parse_rules(doc, "toy") == ()
    doc["schema"][0]["quality"].append(
        {
            "id": "r1",
            "type": "library",
            "metric": "rowCount",
            "mustBeGreaterThan": 0,
            "dimension": "completeness",
            "severity": "error",
            "description": "Has rows.",
        }  # fmt: skip
    )
    with pytest.raises(ValueError, match="unique"):
        parse_rules(doc, "toy")


def test_every_registered_type_names_only_known_parameter_kinds_and_placeholders() -> None:
    for name, ctype in dq.CHECK_TYPES.items():
        assert ctype.scope in ("table", "column"), name
        assert set(ctype.params.values()) <= set(dq.PARAM_KINDS), name
        assert ctype.required | ctype.one_of <= set(ctype.params), name
        assert ctype.params.get("model") == "model" and "model" in ctype.required, name
        allowed = set(ctype.params) | {"column", "dataset", "sla_datepart", "sla_interval"}
        for template in ctype.arguments.values():
            assert dq._placeholders(template) <= allowed, name


def test_the_real_contracts_declare_checks_that_the_generated_yaml_runs() -> None:
    registry = load_registry(REPO)
    files = tg.generate(registry)
    nndss = registry.get("cdc_nndss_weekly")
    names = set(tg.dq_test_severities(nndss))
    assert {"cdc_nndss_weekly_rows_raw_to_staging", "cdc_nndss_weekly_week_in_range"} <= names
    assert tg.dq_test_severities(nndss)["cdc_nndss_weekly_load_within_refresh"] == "warn"
    staging = files[tg.STAGING_DIR / "stg_cdc_nndss_weekly.yml"]
    intermediate = files[tg.INTERMEDIATE_DIR / "int_cdc_nndss_weekly.yml"]
    assert "cdc_nndss_weekly_rows_raw_to_staging" in staging and "rows_raw_to_staging" not in intermediate
    assert "cdc_nndss_weekly_week_in_range" in staging and "week_in_range" not in intermediate
    assert "contract: contracts/cdc_nndss_weekly.odcs.yaml" in staging
    assert set(tg.dq_test_severities(nndss).items()) <= set(rg.expected_from(registry).tests.items())


def _copy_repo(tmp_path: Path) -> Path:
    import shutil

    for d in ("catalog", "contracts", "semantic"):
        shutil.copytree(REPO / d, tmp_path / d)
    return tmp_path


_CHECK = {**_rule("week_ok", check="values_between", model="staging", min=1), "dimension": "conformity"}


def test_a_model_contract_may_not_declare_checks_yet(tmp_path: Path) -> None:
    import yaml

    root = _copy_repo(tmp_path)
    path = sorted((root / tg.MODEL_CONTRACTS).glob("*.odcs.yaml"))[0]
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["schema"][0]["properties"][0]["quality"] = [_CHECK]
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(RegistryError, match="source contracts only"):
        tg.model_contracts(root)


def test_a_check_on_a_source_dbt_never_builds_is_refused(tmp_path: Path) -> None:
    import yaml

    root = _copy_repo(tmp_path)
    live = next(ds for ds in load_registry(REPO) if not ds.entry.licence.redistribution)
    path = root / live.entry.contract
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["schema"][0]["properties"][0]["quality"] = [_CHECK]
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(RegistryError, match="reads stored sources"):
        load_registry(root)


def test_a_column_check_on_a_missing_column_is_refused() -> None:
    with pytest.raises(ValueError, match="no column"):
        tg._with_column_tests([{"name": "a"}], {"b": [{}]})


def test_the_build_record_keeps_every_pass_by_its_contract_rule(tmp_path: Path) -> None:
    run = {
        "metadata": {"invocation_id": "i", "generated_at": "g", "dbt_version": "1.12.7"},
        "results": [
            {"unique_id": "test.p.toy_r1.1", "status": "pass", "failures": 0},
            {"unique_id": "test.p.toy_r2.2", "status": "warn", "failures": 1},
        ],
    }
    nodes = {
        f"test.p.toy_{r}.{i}": {
            "name": f"toy_{r}",
            "resource_type": "test",
            "config": {"severity": sev, "meta": {"dimension": "timeliness", "rule": r}},
        }
        for i, r, sev in ((1, "r1", "ERROR"), (2, "r2", "warn"))
    }
    (tmp_path / "run_results.json").write_text(json.dumps(run))
    (tmp_path / "manifest.json").write_text(json.dumps({"metadata": {"invocation_id": "i"}, "nodes": nodes}))
    build = rg.read_build(tmp_path)
    assert build.passed == [
        {"test": "toy_r1", "type": "test", "severity": "error", "dimension": "timeliness", "rule": "r1"}
    ]
    assert build.tests_passed == 1 and [w["test"] for w in build.warnings] == ["toy_r2"]
    # A warning names its rule as a pass does.
    assert build.warnings[0]["rule"] == "r2"
