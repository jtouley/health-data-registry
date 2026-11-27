"""scripts/pre_push_gate.py: a PR branch takes only a commit that went green on pre-pr/; pre-pr/ gets fast checks."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = "owner/registry"
SHA = "a" * 40
OLD = "b" * 40


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("pre_push_gate", ROOT / "scripts" / "pre_push_gate.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "pre_push_gate", mod)
    spec.loader.exec_module(mod)
    for name in (
        "PRE_COMMIT_REMOTE_BRANCH",
        "PRE_COMMIT_TO_REF",
        "PRE_COMMIT_FROM_REF",
        "PRE_COMMIT_REMOTE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    return mod


def run_record(**over: Any) -> dict[str, Any]:
    run = {
        "id": 7, "event": "push", "head_sha": SHA, "head_branch": "pre-pr/ci-x", "path": ".github/workflows/ci.yml",
        "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
        "status": "completed", "conclusion": "success",
    }  # fmt: skip
    run.update(over)
    return run


def api(*runs: dict[str, Any]) -> Any:
    def get(path: str) -> Any:
        assert path.startswith(f"/repos/{REPO}/actions/workflows/ci.yml/runs?")
        assert f"head_sha={SHA}" in path
        return {"workflow_runs": list(runs)}

    return get


@pytest.mark.parametrize(
    ("branch", "kind"),
    [
        ("refs/heads/feat/x", "pr"), ("fix/y", "pr"), ("ci/trusted-gate", "pr"), ("docs/a", "pr"),
        ("transform/b", "pr"), ("refs/heads/pre-pr/ci/x", "pre-pr"), ("pre-pr/feat-x", "pre-pr"),
        ("wip/x", "other"), ("main", "other"), ("feature/x", "other"),
    ],
)  # fmt: skip
def test_kind(gate: ModuleType, branch: str, kind: str) -> None:
    assert gate.kind(branch) == kind


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/registry.git", "git@github.com:owner/registry.git",
        "http://127.0.0.1:1234/git/owner/registry", "https://github.com/owner/registry/",
    ],
)  # fmt: skip
def test_repo_from_url(gate: ModuleType, url: str) -> None:
    assert gate.repo_from_url(url) == REPO


def test_repo_from_url_refuses_what_it_cannot_read(gate: ModuleType) -> None:
    with pytest.raises(ValueError, match="cannot tell"):
        gate.repo_from_url("")


def test_a_green_pre_pr_run_is_found(gate: ModuleType) -> None:
    assert gate.green_pre_pr_run(api(run_record()), REPO, SHA)["id"] == 7


@pytest.mark.parametrize(
    "over",
    [
        {"conclusion": "failure"}, {"conclusion": "cancelled"}, {"status": "in_progress", "conclusion": None},
        {"head_branch": "ci/x"}, {"head_sha": OLD}, {"event": "pull_request"},
        {"head_repository": {"full_name": "fork/registry"}}, {"path": ".github/workflows/codeql.yml"},
    ],
)  # fmt: skip
def test_anything_but_a_green_pre_pr_run_on_this_sha_does_not_count(
    gate: ModuleType, over: dict[str, Any]
) -> None:
    assert gate.green_pre_pr_run(api(run_record(**over)), REPO, SHA) is None


def test_a_later_green_run_counts_after_a_red_one(gate: ModuleType) -> None:
    found = gate.green_pre_pr_run(api(run_record(id=1, conclusion="failure"), run_record(id=2)), REPO, SHA)
    assert found["id"] == 2


def test_a_short_sha_is_refused(gate: ModuleType) -> None:
    with pytest.raises(ValueError, match="full lowercase"):
        gate.green_pre_pr_run(api(), REPO, "abc")


def test_runs_are_read_page_by_page_until_a_short_page(gate: ModuleType) -> None:
    pages: list[str] = []

    def get(path: str) -> Any:
        pages.append(path)
        if path.endswith("&page=1"):
            return {"workflow_runs": [run_record(id=i, conclusion="failure") for i in range(gate.PER_PAGE)]}
        return {"workflow_runs": [run_record(id=500)]}

    assert gate.green_pre_pr_run(get, REPO, SHA)["id"] == 500
    assert len(pages) == 2
    assert all("event=push" in p and f"per_page={gate.PER_PAGE}" in p for p in pages)


def test_paging_stops_at_the_page_limit(gate: ModuleType) -> None:
    calls: list[str] = []

    def get(path: str) -> Any:
        calls.append(path)
        return {"workflow_runs": [run_record(conclusion="failure")] * gate.PER_PAGE}

    assert gate.green_pre_pr_run(get, REPO, SHA) is None
    assert len(calls) == gate.MAX_PAGES
    assert gate.push_runs(lambda _: {}, REPO, SHA) == []


def test_a_run_of_another_repository_does_not_count(gate: ModuleType) -> None:
    assert gate.is_pre_pr_run(run_record(repository={"full_name": "x/y"}), REPO, SHA) is False
    assert gate.is_pre_pr_run(run_record(head_branch=None), REPO, SHA) is False
    assert gate.is_pre_pr_run(run_record(repository=None, head_repository=None), REPO, SHA) is False


def test_a_pr_push_of_a_green_commit_passes(gate: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    code = gate.main(
        ["--branch", "refs/heads/ci/x", "--sha", SHA, "--repo", REPO], get_json=api(run_record())
    )
    assert code == 0
    assert "passed CI on pre-pr/ci-x (run 7); push allowed" in capsys.readouterr().err


def test_a_pr_push_without_a_green_run_is_refused(
    gate: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code = gate.main(["--branch", "refs/heads/ci/x", "--sha", SHA, "--repo", REPO], get_json=api())
    assert code == 1
    err = capsys.readouterr().err
    assert f"Push to refs/heads/ci/x refused: {SHA[:12]} has no completed, all-green CI run" in err
    assert "push this commit to pre-pr/ci/x" in err


def test_a_pr_push_fails_closed_when_github_does_not_answer(
    gate: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(path: str) -> Any:
        raise RuntimeError("HTTP 502")

    code = gate.main(["--branch", "fix/x", "--sha", SHA, "--repo", REPO], get_json=broken)
    assert code == 1
    assert "could not ask GitHub" in capsys.readouterr().err


def test_the_repo_comes_from_the_remote_url(gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRE_COMMIT_REMOTE_BRANCH", "refs/heads/feat/x")
    monkeypatch.setenv("PRE_COMMIT_TO_REF", SHA)
    monkeypatch.setenv("PRE_COMMIT_REMOTE_URL", "https://github.com/owner/registry.git")
    assert gate.main([], get_json=api(run_record())) == 0
    monkeypatch.setenv("PRE_COMMIT_REMOTE_URL", "")
    assert gate.main([], get_json=api(run_record())) == 1


def test_deletions_other_branches_and_manual_runs_pass(gate: ModuleType) -> None:
    def never(path: str) -> Any:
        raise AssertionError("no API call expected")

    assert gate.main(["--branch", "ci/x", "--sha", "0" * 40], get_json=never) == 0
    assert gate.main(["--branch", "wip/x", "--sha", SHA], get_json=never) == 0
    assert gate.main([], get_json=never) == 0


class FakeGit:
    """git for the hook: merge-base answers a fixed commit, diff answers the given file list."""

    def __init__(self, files: str) -> None:
        self.files = files
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        return "c" * 40 + "\n" if args[0] == "merge-base" else self.files


def fake_git(files: str) -> FakeGit:
    return FakeGit(files)


def test_a_pre_pr_push_runs_the_fast_checks_on_the_changed_modules(gate: ModuleType) -> None:
    ran: list[list[str]] = []
    git = fake_git("scripts/pre_push_gate.py\nREADME.md\n")
    code = gate.main(
        ["--branch", "pre-pr/x", "--sha", SHA, "--from-sha", OLD],
        run=lambda c: ran.append(list(c)) or 0,
        run_git=git,
    )
    assert code == 0
    assert git.calls == [["diff", "--name-only", OLD, SHA, "--"]]
    assert [c[2] for c in ran] == ["ruff", "ruff", "mypy", "pytest"]
    assert ran[-1][-1] == "tests/test_pre_push_gate.py"


def test_a_new_pre_pr_branch_compares_with_origin_main(gate: ModuleType) -> None:
    git = fake_git("")
    assert (
        gate.main(
            ["--branch", "pre-pr/x", "--sha", SHA, "--from-sha", "0" * 40], run=lambda c: 0, run_git=git
        )
        == 0
    )
    assert git.calls == [["merge-base", "origin/main", SHA], ["diff", "--name-only", "c" * 40, SHA, "--"]]


def test_a_failing_fast_check_refuses_the_push(gate: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    ran: list[list[str]] = []

    def run(cmd: Sequence[str]) -> int:
        ran.append(list(cmd))
        return 3 if cmd[2] == "mypy" else 0

    assert gate.main(["--branch", "pre-pr/x", "--sha", SHA], run=run, run_git=fake_git("")) == 3
    assert len(ran) == 3
    assert "Push refused: `uv run mypy` failed" in capsys.readouterr().err


def test_git_errors_refuse_a_pre_pr_push(gate: ModuleType) -> None:
    def git(args: Sequence[str]) -> str:
        raise subprocess.CalledProcessError(128, ["git", *args])

    assert gate.main(["--branch", "pre-pr/x", "--sha", SHA], run=lambda c: 0, run_git=git) == 1


def test_affected_tests(gate: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    for name in ("test_query.py", "test_eval_scorer.py", "test_old.py"):
        (tmp_path / "tests" / name).write_text("")
    files = [
        "src/health_data_registry/query.py", "eval/scorer.py", "tests/test_old.py", "tests/test_gone.py",
        "docs/x.md", "src/health_data_registry/nothing.py", "tests/conftest.py",
    ]  # fmt: skip
    assert gate.affected_tests(files, tmp_path) == ["tests/test_old.py", "tests/test_query.py"]


def test_the_hook_is_registered_for_pre_push_only() -> None:
    # Under mutmut the suite runs in mutants/, which has no copy of the hook config; read the checkout's own.
    source = ROOT.parent if ROOT.name == "mutants" else ROOT
    config = yaml.safe_load((source / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = {h["id"]: h for repo in config["repos"] for h in repo["hooks"]}
    hook = hooks["pre-push-gate"]
    assert hook["stages"] == ["pre-push"]
    assert hook["entry"] == "uv run --frozen python scripts/pre_push_gate.py"
    assert "pre-push" in config["default_install_hook_types"]
