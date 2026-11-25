"""scripts/mutation_nightly.py: the nightly run's base, its survivor issues, and the release gate."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
reads_workflows = pytest.mark.skipif("MUTANT_UNDER_TEST" in os.environ, reason=".github/ is not in mutants/")
SHA = "a" * 40
OLD = "b" * 40
REPO = "owner/repo"
RUN_URL = "https://github.com/owner/repo/actions/runs/42"


@pytest.fixture
def mn(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "mutation_nightly", ROOT / "scripts" / "mutation_nightly.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "mutation_nightly", mod)
    spec.loader.exec_module(mod)
    return mod


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    done = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True)  # noqa: S603,S607
    return done.stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


MODULE_V1 = "def f(x):\n    return x + 1\n\n\ndef g(x):\n    return x * 2\n"
MODULE_V2 = "def f(x):\n    return x + 2\n\n\ndef g(x):\n    return x * 2\n"
MODULE_V3 = "def f(x):\n    return x + 2\n\n\ndef g(x):\n    return x * 3\n"
PATH = "src/health_data_registry/m.py"


@pytest.fixture
def repo(tmp_path: Path, mn: ModuleType, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A main with: the workflow added (base), f changed in PR #12, g changed in a commit naming no PR."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    before = _commit(r, {"README.md": "x\n"}, "start")
    added = _commit(
        r, {".github/workflows/mutation.yml": "name: Mutation\n", PATH: MODULE_V1}, "nightly (#9)"
    )
    pr = _commit(r, {PATH: MODULE_V2}, "make f add two (#12)")
    loose = _commit(r, {PATH: MODULE_V3}, "make g triple")
    monkeypatch.setattr(mn, "ROOT", r)
    monkeypatch.setattr(mn.mc, "ROOT", r)
    return {"before": before, "added": added, "pr": pr, "loose": loose, "root": str(r)}


# base ------------------------------------------------------------------------------------------------------------


def test_the_base_is_the_last_green_runs_commit(mn: ModuleType) -> None:
    runs = {
        "workflow_runs": [
            {"head_sha": "c" * 40, "conclusion": "failure", "path": mn.WORKFLOW},
            {"head_sha": "d" * 40, "conclusion": "success", "path": ".github/workflows/other.yml"},
            {"head_sha": SHA, "conclusion": "success", "path": mn.WORKFLOW},
            {"head_sha": OLD, "conclusion": "success", "path": mn.WORKFLOW},
        ]
    }
    paths: list[str] = []
    assert mn.last_green_sha(lambda p: paths.append(p) or runs, REPO) == SHA
    assert paths[0].startswith(
        f"/repos/{REPO}/actions/workflows/mutation.yml/runs?branch=main&status=success"
    )
    assert mn.last_green_sha(lambda p: {"workflow_runs": []}, REPO) is None


def test_choose_base_never_skips_a_change(mn: ModuleType, repo: dict[str, str]) -> None:
    head = repo["loose"]
    assert mn.choose_base(repo["pr"], "", head)[0] == repo["pr"]  # the last green run
    assert mn.choose_base(None, "", head)[0] == repo["added"]  # none yet: since the workflow was added
    assert mn.choose_base("e" * 40, "", head)[0] == repo["added"]  # not on this history: the same
    assert mn.choose_base(repo["pr"], repo["before"], head)[0] == repo["before"]  # a dispatch may widen
    with pytest.raises(ValueError, match="only widen"):
        mn.choose_base(repo["added"], repo["pr"], head)  # never narrow
    with pytest.raises(ValueError, match="full commit SHA"):
        mn.choose_base(None, "main", head)


def test_the_base_command_writes_the_step_output(
    mn: ModuleType, repo: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    get = lambda p: {"workflow_runs": [{"head_sha": repo["pr"], "conclusion": "success"}]}  # noqa: E731
    assert mn.main(["base", "--repo", REPO, "--github-output", str(out)], get, lambda *a: None) == 0
    assert out.read_text(encoding="utf-8") == f"base={repo['pr']}\n"
    assert capsys.readouterr().out.strip() == repo["pr"]


# issues ----------------------------------------------------------------------------------------------------------


def _report(mn: ModuleType, survivors: list[str]) -> Any:
    return mn.mc.ShardResult(modules=["health_data_registry.m#1of1@abcdefabcdef"], survivors=survivors)


def test_survivors_go_to_the_pr_or_commit_that_last_changed_their_function(
    mn: ModuleType, repo: dict[str, str]
) -> None:
    survivors = [
        "health_data_registry.m.x_f__mutmut_1",
        "health_data_registry.m.x_g__mutmut_2",
        "health_data_registry.m.x_f__mutmut_3",
        "elsewhere.x_h__mutmut_1",
    ]
    grouped = mn.group_survivors([_report(mn, survivors)], repo["added"])
    assert grouped == {
        "Mutation survivors (origin not found)": ["elsewhere.x_h__mutmut_1"],
        "Mutation survivors from #12": [
            "health_data_registry.m.x_f__mutmut_1",
            "health_data_registry.m.x_f__mutmut_3",
        ],
        f"Mutation survivors from commit {repo['loose'][:12]}": ["health_data_registry.m.x_g__mutmut_2"],
    }
    # Since #12, only g changed: f's survivor falls back to the newest commit that changed the file.
    later = mn.group_survivors([_report(mn, ["health_data_registry.m.x_f__mutmut_1"])], repo["pr"])
    assert list(later) == [f"Mutation survivors from commit {repo['loose'][:12]}"]


def test_issues_are_updated_left_closed_or_opened(mn: ModuleType) -> None:
    grouped = {
        "Mutation survivors from #12": ["a.x_f__mutmut_1"],
        "Mutation survivors from #13": ["b"],
        "Mutation survivors from #14": ["c"],
    }
    existing = [
        {"number": 5, "title": "Mutation survivors from #12", "state": "open", "body": ""},
        {
            "number": 6,
            "title": "Mutation survivors from #13",
            "state": "closed",
            "body": "- [ ] `b` equivalent",
        },
        {"number": 7, "title": "Mutation survivors from #14", "state": "closed", "body": "- [ ] `other`"},
        {
            "number": 8,
            "title": "Mutation survivors from #14",
            "state": "open",
            "pull_request": {},
            "body": "",
        },
    ]
    sent: list[tuple[str, str, dict[str, Any]]] = []

    def send(method: str, path: str, body: dict[str, Any]) -> Any:
        sent.append((method, path, body))
        return {"number": 9}

    done = mn.sync_issues(lambda p: existing, send, REPO, grouped, RUN_URL, SHA)
    assert done == [
        "updated #5: Mutation survivors from #12",
        "left closed (already explained): Mutation survivors from #13",
        "opened #9: Mutation survivors from #14",
    ]
    assert [(m, p) for m, p, _ in sent] == [
        ("PATCH", f"/repos/{REPO}/issues/5"),
        ("POST", f"/repos/{REPO}/issues"),
    ]
    assert "`a.x_f__mutmut_1`" in sent[0][2]["body"] and RUN_URL in sent[0][2]["body"]
    assert (
        sent[1][2]["labels"] == ["mutation-survivors"]
        and sent[1][2]["title"] == "Mutation survivors from #14"
    )


def test_the_issues_command_checks_its_arguments_and_opens_nothing_without_survivors(
    mn: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["issues", "--repo", REPO, "--reports", str(tmp_path), "--base", SHA, "--run-url"]
    with pytest.raises(SystemExit):
        mn.main([*args, "http://evil.example/x"], lambda p: None, lambda *a: None)
    assert mn.main([*args, RUN_URL], lambda p: None, lambda *a: None) == 0
    assert "No survivors" in capsys.readouterr().out


# gate ------------------------------------------------------------------------------------------------------------


class Api:
    def __init__(self, runs: list[dict[str, Any]], compare: dict[str, Any], issues: list[dict[str, Any]]):
        self.runs, self.compare, self.issues = runs, compare, issues

    def __call__(self, path: str) -> Any:
        if "/actions/workflows/mutation.yml/runs?" in path:
            assert "branch=main" in path and "status=completed" in path
            return {"workflow_runs": self.runs}
        if "/compare/" in path:
            return self.compare.get(path.rsplit("/", 1)[1].split("...")[0], {"status": "diverged"})
        if "/issues?" in path:
            assert "labels=mutation-survivors" in path and "state=open" in path
            return self.issues
        raise AssertionError(path)


def _run(head: str, conclusion: str = "success") -> dict[str, Any]:
    return {
        "id": 1,
        "head_sha": head,
        "conclusion": conclusion,
        "html_url": RUN_URL,
        "path": ".github/workflows/mutation.yml",
    }


def _files(*names: str) -> dict[str, Any]:
    return {"status": "ahead", "files": [{"filename": n} for n in names]}


@pytest.mark.parametrize(
    ("runs", "compare", "issues", "ok", "says"),
    [
        ([_run(SHA)], {}, [], True, "Mutation gate passed"),
        ([_run(OLD)], {OLD: _files("docs/x.md", "README.md")}, [], True, "Mutation gate passed"),
        ([_run("c" * 40), _run(OLD)], {OLD: _files("README.md")}, [], True, "passed"),  # c: not an ancestor
        ([_run(SHA, "failure")], {}, [], False, "ended 'failure'"),
        ([_run(OLD)], {OLD: _files("src/health_data_registry/sql_guard.py")}, [], False, "changed after"),
        (
            [_run(OLD)],
            {OLD: {"status": "ahead", "files": [{"filename": "a"}] * 300}},
            [],
            False,
            "Could not tell",
        ),
        (
            [_run(SHA)],
            {},
            [{"number": 3, "title": "Mutation survivors from #12"}],
            False,
            "Open survivor issue #3",
        ),
        ([], {}, [], False, "No completed nightly mutation run"),
    ],
    ids=[
        "same-commit",
        "docs-since",
        "skips-other-branches",
        "red",
        "code-since",
        "too-many-files",
        "open-issue",
        "none",
    ],
)
def test_the_release_gate(
    mn: ModuleType,
    runs: list[dict[str, Any]],
    compare: dict[str, Any],
    issues: list[dict[str, Any]],
    ok: bool,
    says: str,
) -> None:
    passed, lines = mn.gate(Api(runs, compare, issues), REPO, SHA)
    assert passed is ok
    assert any(says in line for line in lines), lines


def test_the_gate_fails_closed_on_any_error(mn: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(path: str) -> Any:
        raise OSError("api down")

    assert mn.main(["gate", "--repo", REPO, "--sha", SHA], broken, lambda *a: None) == 1
    assert "::error::" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        mn.main(["gate", "--repo", REPO, "--sha", "main"], broken, lambda *a: None)


# wiring ----------------------------------------------------------------------------------------------------------


@reads_workflows
def test_every_release_path_waits_for_the_gate() -> None:
    wf = ROOT / ".github" / "workflows"
    for name, job in [("release.yml", "build"), ("deploy-space.yml", "deploy"), ("refresh.yml", "publish")]:
        jobs = yaml.safe_load((wf / name).read_text(encoding="utf-8"))["jobs"]
        needs = jobs[job]["needs"]
        assert "mutation-gate" in ([needs] if isinstance(needs, str) else needs), name
        gate = jobs["mutation-gate"]
        assert gate["permissions"] == {"contents": "read", "actions": "read", "issues": "read"}, name
        run = gate["steps"][-1]["run"]
        assert run == 'python3 scripts/mutation_nightly.py gate --repo "${REPO}" --sha "${SHA}"', name
        assert gate["steps"][-1]["env"]["SHA"] == "${{ github.sha }}", name


@reads_workflows
def test_the_nightly_workflow_is_main_only_and_writes_issues_in_one_job() -> None:
    data = yaml.safe_load((ROOT / ".github" / "workflows" / "mutation.yml").read_text(encoding="utf-8"))
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"]["cancel-in-progress"] is False
    jobs = data["jobs"]
    assert jobs["plan"]["if"] == "github.ref == 'refs/heads/main'"
    assert "github.ref == 'refs/heads/main'" in jobs["result"]["if"] and "always()" in jobs["result"]["if"]
    writers = [n for n, j in jobs.items() if "write" in str(j.get("permissions", {}))]
    assert writers == ["result"] and jobs["result"]["permissions"]["issues"] == "write"
    for job in jobs.values():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", ""), step  # values reach the shell only through env
