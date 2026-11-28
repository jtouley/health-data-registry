"""scripts/ci_impact.py: which CI work a change can affect. CI runs only that on a pull request; main, the merge queue
and nightly run everything."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Under mutmut the tests run in mutants/, a partial copy of the repository; scripts/ is never mutated, so skip there.
pytestmark = pytest.mark.skipif("MUTANT_UNDER_TEST" in os.environ, reason="reads the real repository layout")


@pytest.fixture(scope="module")
def ci() -> ModuleType:
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("ci_impact", ROOT / "scripts" / "ci_impact.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ci_impact"] = mod  # dataclasses look their module up by name
    spec.loader.exec_module(mod)
    return mod


def run(ci: ModuleType, *paths: str) -> dict[str, object]:
    return ci.impact(list(paths)).as_dict()  # type: ignore[no-any-return]


def test_a_readme_only_change_runs_no_dbt_and_no_pytest(ci: ModuleType) -> None:
    got = run(ci, "README.md")
    assert got["full"] is False and got["dbt"] is False and got["tests"] == "none"
    assert (
        run(ci, "README.md", "CHANGELOG.md", "docs/setup.md", "docs/adr/0002-primary-sources-only.md")[
            "tests"
        ]
        == "none"
    )


def test_helpers_md_runs_only_its_drift_test(ci: ModuleType) -> None:
    got = run(ci, "docs/helpers.md")
    assert got["tests"] == ["tests/test_workflows.py"] and got["dbt"] is False


def test_a_contract_change_runs_dbt_and_the_tests_that_read_contracts(ci: ModuleType) -> None:
    got = run(ci, "contracts/cdc_nndss_weekly.odcs.yaml")
    assert got["dbt"] is True and got["full"] is False
    assert isinstance(got["tests"], list) and "tests/test_real_contracts.py" in got["tests"]


def test_a_registry_change_runs_dbt_and_its_dependent_tests(ci: ModuleType) -> None:
    got = run(ci, "src/health_data_registry/registry.py")
    assert got["dbt"] is True
    tests = got["tests"]
    assert isinstance(tests, list)
    # Every test under tests/conftest.py, which imports the registry; never the live publisher checks.
    assert {
        "tests/test_real_contracts.py",
        "tests/test_tool_outputs.py",
        "tests/smoke/test_c_auris_journey.py",
    } <= set(tests)
    assert not any(t.startswith("tests/integration/") for t in tests)


def test_a_leaf_module_runs_only_the_tests_that_reach_it(ci: ModuleType) -> None:
    got = run(ci, "src/health_data_registry/hosted.py")
    tests = got["tests"]
    assert isinstance(tests, list) and "tests/test_hosted_units.py" in tests
    assert "tests/test_sql_guard.py" not in tests and got["dbt"] is False


@pytest.mark.parametrize(
    "path",
    [
        "weird/new_thing.txt",
        "src/health_data_registry/data.json",
        ".github/workflows/ci.yml",
        "scripts/mutate_changed.py",
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        "tests/fixtures/transform/raw.ndjson",
    ],
)
def test_an_unknown_path_or_ci_config_runs_everything(ci: ModuleType, path: str) -> None:
    got = run(ci, "README.md", path)
    assert got == {"full": True, "tests": "all", "dbt": True, "kit": True, "reason": got["reason"]}


def test_a_test_that_loads_a_script_by_path_reaches_what_the_script_imports(ci: ModuleType) -> None:
    # tests/test_crawl_doi_script.py loads scripts/crawl_doi.py from its file; the script imports doi_harvest.
    tests = run(ci, "src/health_data_registry/doi_harvest.py")["tests"]
    assert isinstance(tests, list) and "tests/test_crawl_doi_script.py" in tests


def test_a_change_to_a_test_runs_that_test(ci: ModuleType) -> None:
    tests = run(ci, "tests/test_sql_guard.py")["tests"]
    assert isinstance(tests, list) and "tests/test_sql_guard.py" in tests


def test_a_deleted_module_runs_what_named_it(ci: ModuleType) -> None:
    # Only this test file names it, so only this test file runs.
    assert run(ci, "src/health_data_registry/gone_module_xyz.py")["tests"] == ["tests/test_ci_impact.py"]


def test_full_runs_everything(ci: ModuleType) -> None:
    assert ci.impact([], full=True).as_dict()["full"] is True


def test_main_writes_the_step_outputs(
    ci: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ci, "changed_paths", lambda base: ["src/health_data_registry/hosted.py"])
    out = tmp_path / "out"
    assert ci.main(["--base", "0" * 40, "--github-output", str(out)]) == 0
    lines = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())
    assert lines["full"] == "false" and lines["dbt"] == "false"
    assert "tests/test_hosted_units.py" in json.loads(lines["tests"])
    assert "tests/test_hosted_units.py" in json.loads(lines["test_args"])  # a JSON array, never shell words
    with pytest.raises(SystemExit):
        ci.main([])  # neither --base nor --full
