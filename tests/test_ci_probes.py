"""CI probes and budgets: each fixed synthetic change selects exactly the jobs it expects, and budgets only tighten."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif("MUTANT_UNDER_TEST" in os.environ, reason="reads the real repository layout")


def _load(name: str) -> ModuleType:
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    return _load("ci_probe")


@pytest.fixture(scope="module")
def budget() -> ModuleType:
    return _load("ci_budget")


PROBE_NAMES = [
    "docs-only",
    "contract-yaml",
    "sql-guard-function",
    "registry-function",
    "sql-guard-constant",
    "new-file",
]


def test_the_probe_set_is_the_agreed_one(probe: ModuleType) -> None:
    assert [p["name"] for p in probe.load()] == PROBE_NAMES


@pytest.mark.parametrize("name", PROBE_NAMES)
def test_each_probe_selects_exactly_what_it_expects(probe: ModuleType, name: str) -> None:
    p = probe.find(name)
    got = probe.predict(p)
    assert got["full"] is False, f"{name} runs everything; a probe is a small change"
    assert {k: got[k] for k in ("tests", "dbt", "mutation")} == p["expect"]
    assert 0 < p["budget"] <= 180  # the pre-PR budget: 3 minutes, push to every check done


def test_apply_makes_the_change_in_a_copy(probe: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "src" / "health_data_registry").mkdir(parents=True)
    src = ROOT / "src" / "health_data_registry" / "sql_guard.py"
    (tmp_path / "src" / "health_data_registry" / "sql_guard.py").write_text(src.read_text(encoding="utf-8"))
    path = probe.apply(probe.find("sql-guard-function"), tmp_path)
    text = (tmp_path / path).read_text(encoding="utf-8")
    assert "# ci probe" in text and text != src.read_text(encoding="utf-8")
    compile(text, path, "exec")  # still valid Python
    with pytest.raises(ValueError, match="update the probe"):
        probe.edited({"edit": "function", "path": path, "function": "no_such_function"}, tmp_path)


def _budgets(pr_tests: int = 150) -> str:
    data = {
        "pr": {
            "run": 180,
            "jobs": {"Changes": 30, "Tests (Python 3.12)": pr_tests, "Mutation tests (*)": 150},
        }
    }
    return "# a comment\n" + json.dumps(data)


def test_the_committed_budgets_fit_the_three_minute_pre_pr_target(budget: ModuleType) -> None:
    data = budget.parse_budgets((ROOT / "ci" / "budgets.yml").read_text(encoding="utf-8"))
    assert data["pr"]["run"] <= 180
    assert all(seconds <= 150 for seconds in data["pr"]["jobs"].values())


def _job(name: str, ran: int, queued: int = 5, **over: Any) -> dict[str, Any]:
    return {
        "name": name,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-11-25T10:00:00Z",
        "started_at": f"2026-11-25T10:00:{queued:02d}Z",
        "completed_at": f"2026-11-25T10:{(queued + ran) // 60:02d}:{(queued + ran) % 60:02d}Z",
        **over,
    }


def test_a_job_over_its_budget_or_without_one_fails_the_check(budget: ModuleType) -> None:
    jobs = budget.parse_budgets(_budgets())["pr"]["jobs"]
    run = [
        _job("Changes", 20),
        _job("Tests (Python 3.12)", 170),
        _job("Mutation tests (health_data_registry.sql_guard#1of1@abc)", 100),
        _job("Mystery", 5),
        _job("Checks", 999, conclusion="skipped"),
        _job("CI result", 999),
    ]
    slow, report = budget.over_budget(run, jobs, {"CI result"})
    assert slow == [
        "Tests (Python 3.12): ran 170 s, over its 150 s budget",
        "Mystery: ran 5 s and has no budget in ci/budgets.yml",
    ]
    assert any(line.startswith("Mutation tests (health_data_registry.sql_guard") for line in report)


def test_budgets_may_only_tighten(budget: ModuleType) -> None:
    old = budget.parse_budgets(_budgets(150))
    assert budget.loosened(old, budget.parse_budgets(_budgets(120))) == []
    assert budget.loosened(old, budget.parse_budgets(_budgets(200))) == [
        "pr.jobs['Tests (Python 3.12)']: 150 -> 200 s"
    ]
    smaller = budget.parse_budgets(_budgets())
    del smaller["pr"]["jobs"]["Changes"]
    assert budget.loosened(old, smaller) == ["pr.jobs['Changes']: the budget was taken away"]
