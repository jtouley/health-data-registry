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


def _budgets(pr_tests: int = 120) -> str:
    data = {"pr": {"run": 180, "jobs": {"Changes": 20, "Tests (Python *)": pr_tests, "CI result": 30}}}
    return "# a comment\n" + json.dumps(data)


def _critical_path_budget(jobs: dict[str, int]) -> int:
    """Changes, then the longest job after it, then CI result: the sum the run budget has to hold."""
    after = [v for k, v in jobs.items() if k not in ("Changes", "CI result")]
    return jobs["Changes"] + max(after) + jobs["CI result"]


def test_the_committed_budgets_fit_the_three_minute_pre_pr_target(budget: ModuleType) -> None:
    data = budget.parse_budgets((ROOT / "ci" / "budgets.yml").read_text(encoding="utf-8"))
    assert data["pr"]["run"] <= 180
    for profile in data.values():  # every job on the critical path fitting its budget fits the run's
        assert _critical_path_budget(profile["jobs"]) <= profile["run"]
    assert not any("Mutation" in job for p in data.values() for job in p["jobs"])  # mutation runs nightly


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
        _job("Tests (Python 3.12)", 130),
        _job("Mystery", 5),
        _job("Checks", 999, conclusion="skipped"),
        _job("CI result", 999),
    ]
    slow, report = budget.over_budget(run, jobs, {"CI result"})
    assert slow == [
        "Tests (Python 3.12): ran 130 s, over its 120 s budget",
        "Mystery: ran 5 s and has no budget in ci/budgets.yml",
    ]
    assert any(line.startswith("Changes: ran 20 s") for line in report)


def test_the_run_is_judged_on_its_critical_path_without_queue_time(budget: ModuleType) -> None:
    jobs = {"Changes": 60, "Tests (Python *)": 200, "Checks": 200}
    run = [_job("Changes", 50), _job("Checks", 100, queued=40), _job("Tests (Python 3.12)", 120, queued=50)]
    assert budget.critical_path(run, set()) == 170  # 50 + max(100, 120); the queue is left out
    slow, report = budget.over_budget(run, jobs, set(), run_budget=160)
    assert slow == ["the run: 170 s on the critical path, over its 160 s budget"]
    assert report[-1] == "Run: 170 s of running time on the critical path (budget 160)"
    assert budget.over_budget(run, jobs, set(), run_budget=170)[0] == []


def _check(
    budget: ModuleType, jobs: Any, warn_only: bool, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    def get(path: str) -> Any:
        if isinstance(jobs, Exception):
            raise jobs
        assert path.startswith("/repos/o/r/actions/runs/7/jobs?filter=latest")
        return {"jobs": jobs}

    code = budget.check("o/r", 7, "pr", {"CI result"}, warn_only, get)
    return code, capsys.readouterr().out


def test_over_budget_fails_a_pr_and_only_warns_elsewhere(
    budget: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    slow = [_job("Changes", 10), _job("Tests (Python 3.12)", 999)]
    code, out = _check(budget, slow, False, capsys)
    assert code == 1 and "Over budget" in out
    code, out = _check(budget, slow, True, capsys)  # the merge queue, main, nightly
    assert code == 0 and "::warning title=Over its CI budget::Tests (Python 3.12): ran 999 s" in out
    code, out = _check(budget, [_job("Changes", 10), _job("Tests (Python 3.12)", 60)], False, capsys)
    assert code == 0 and "fits its budget" in out


@pytest.mark.parametrize("warn_only", [False, True])
def test_unreadable_job_times_warn_and_never_fail(
    budget: ModuleType, capsys: pytest.CaptureFixture[str], warn_only: bool
) -> None:
    code, out = _check(budget, OSError("api down"), warn_only, capsys)
    assert code == 0 and out.startswith("::warning title=CI budget::The job times could not be read")


def test_budgets_may_only_tighten(budget: ModuleType) -> None:
    old = budget.parse_budgets(_budgets(150))
    assert budget.loosened(old, budget.parse_budgets(_budgets(120))) == []
    assert budget.loosened(old, budget.parse_budgets(_budgets(200))) == [
        "pr.jobs['Tests (Python *)']: 150 -> 200 s",
        "pr.jobs['Tests (Python x)']: 150 -> 200 s",
    ]
    smaller = budget.parse_budgets(_budgets(150))
    del smaller["pr"]["jobs"]["Changes"]
    assert budget.loosened(old, smaller) == ["pr.jobs['Changes']: the budget was taken away"]


def _with_jobs(jobs: dict[str, int]) -> dict[str, Any]:
    return {"pr": {"run": 180, "jobs": jobs}}


def test_a_new_key_or_an_earlier_pattern_cannot_loosen_a_budget_quietly(budget: ModuleType) -> None:
    old = _with_jobs({"Changes": 20, "Tests (Python *)": 120})
    exact = _with_jobs({"Changes": 20, "Tests (Python 3.12)": 600, "Tests (Python *)": 120})
    assert "pr.jobs['Tests (Python 3.12)']: 120 -> 600 s" in budget.loosened(old, exact)
    ahead = _with_jobs({"Changes": 20, "Tests*": 9999, "Tests (Python *)": 120})
    assert "pr.jobs['Tests (Python x)']: 120 -> 9999 s" in budget.loosened(old, ahead)
    behind = _with_jobs({"Changes": 20, "Tests (Python *)": 120, "Tests*": 9999})  # never reached first
    assert budget.loosened(old, behind) == []
    new_job = _with_jobs({"Changes": 20, "Tests (Python *)": 120, "Lint": 30})  # had none: now tighter
    assert budget.loosened(old, new_job) == []
