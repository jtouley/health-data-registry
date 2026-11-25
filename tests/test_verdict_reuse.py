"""scripts/verdict_reuse.py: a PR reuses only an exact, green pre-pr CI verdict, against a fake API."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import urllib.parse
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = "owner/registry"
SHA = "a" * 40
OTHER_SHA = "b" * 40
TESTED = "c" * 40
RUN = 101
WORKFLOW_ID = 4242
# .github/ is not copied into mutants/, where mutmut runs the suite.
reads_workflows = pytest.mark.skipif("MUTANT_UNDER_TEST" in os.environ, reason=".github/ is not in mutants/")


@pytest.fixture
def mr(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("verdict_reuse", ROOT / "scripts" / "verdict_reuse.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "verdict_reuse", mod)
    spec.loader.exec_module(mod)
    return mod


def _run(run_id: int = RUN, **over: Any) -> dict[str, Any]:
    run: dict[str, Any] = {
        "id": run_id,
        "event": "push",
        "head_sha": SHA,
        "head_branch": "pre-pr/feature",
        "path": ".github/workflows/ci.yml",
        "workflow_id": WORKFLOW_ID,
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
    }
    run.update(over)
    return run


def _job(run_id: int = RUN, **over: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": run_id * 10,
        "run_id": run_id,
        "name": "CI result",
        "status": "completed",
        "conclusion": "success",
        "head_sha": SHA,
    }
    job.update(over)
    return job


def same(tested: str, head: str) -> bool:
    """The merge commit this run tests has the head's own tree."""
    assert (tested, head) == (TESTED, SHA)
    return True


def differs(tested: str, head: str) -> bool:
    """Main moved on after the pre-pr run: the tested merge has a different tree."""
    return False


class FakeApi:
    """Answers the two endpoints the script reads, and records every request."""

    def __init__(
        self, runs: list[dict[str, Any]], jobs: dict[int, list[dict[str, Any]]], sha: str = SHA
    ) -> None:
        self.runs, self.jobs, self.paths, self.sha = runs, jobs, [], sha
        self.workflow: dict[str, Any] = {"id": WORKFLOW_ID, "path": ".github/workflows/ci.yml"}

    def __call__(self, path: str) -> Any:
        self.paths.append(path)
        url = urllib.parse.urlsplit(path)
        if url.path == f"/repos/{REPO}/actions/workflows/ci.yml":
            return self.workflow
        q = dict(urllib.parse.parse_qsl(url.query))
        page, per_page = int(q["page"]), int(q["per_page"])
        lo, hi = (page - 1) * per_page, page * per_page
        if url.path == f"/repos/{REPO}/actions/workflows/ci.yml/runs":
            assert q["head_sha"] == self.sha and q["event"] == "push"
            return {"workflow_runs": self.runs[lo:hi]}
        prefix = f"/repos/{REPO}/actions/runs/"
        assert url.path.startswith(prefix) and url.path.endswith("/jobs"), path
        assert q["filter"] == "latest"
        run_id = int(url.path[len(prefix) : -len("/jobs")])
        return {"jobs": self.jobs.get(run_id, [])[lo:hi]}


def test_a_green_pre_pr_verdict_for_the_same_sha_is_reused(mr: ModuleType) -> None:
    api = FakeApi([_run()], {RUN: [_job(name="Lint (ruff)", id=1), _job()]})
    assert mr.find_reusable(api, REPO, SHA, "main", TESTED, same) == mr.Reuse(
        run_id=RUN, branch="pre-pr/feature", job_id=RUN * 10
    )


@pytest.mark.parametrize(
    "job",
    [
        _job(conclusion="failure"),
        _job(conclusion="cancelled"),
        _job(conclusion="skipped"),
        _job(status="in_progress", conclusion=None),
        _job(head_sha=OTHER_SHA),
        _job(run_id=RUN + 1),
        _job(name="Mutation tests (list changed modules)"),
        _job(name="Mutation tests (health_data_registry.tools)"),
    ],
    ids=["failed", "cancelled", "skipped", "running", "other-sha", "other-run", "list-job", "a-shard"],
)
def test_only_a_completed_successful_aggregate_job_counts(mr: ModuleType, job: dict[str, Any]) -> None:
    assert mr.find_reusable(FakeApi([_run()], {RUN: [job]}), REPO, SHA, "main", TESTED, same) is None


@pytest.mark.parametrize(
    "over",
    [
        {"event": "pull_request"},
        {"event": "workflow_dispatch"},
        {"head_sha": OTHER_SHA},
        {"head_branch": "feature"},
        {"head_branch": "main"},
        {"head_branch": "xpre-pr/feature"},
        {"head_branch": None},
        {"path": ".github/workflows/codeql.yml"},
        {"path": ".github/workflows/codeql.yml@refs/heads/pre-pr/feature"},
        {"path": ".github/workflows/ci.yml.bak"},
        {"workflow_id": WORKFLOW_ID + 1},
        {"workflow_id": None},
        {"repository": {"full_name": "someone/fork"}},
        {"head_repository": {"full_name": "someone/fork"}},
        {"head_repository": None},
    ],
    ids=str,
)
def test_only_a_push_run_of_this_workflow_on_a_pre_pr_branch_here_counts(
    mr: ModuleType, over: dict[str, Any]
) -> None:
    assert mr.find_reusable(FakeApi([_run(**over)], {RUN: [_job()]}), REPO, SHA, "main", TESTED, same) is None


def test_a_later_green_run_is_found_past_a_red_one(mr: ModuleType) -> None:
    api = FakeApi([_run(7), _run(8)], {7: [_job(7, conclusion="failure")], 8: [_job(8)]})
    assert mr.find_reusable(api, REPO, SHA, "main", TESTED, same) == mr.Reuse(
        run_id=8, branch="pre-pr/feature", job_id=80
    )


def test_runs_and_jobs_are_paged(mr: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr, "PER_PAGE", 2)
    runs = [_run(1, head_branch="other"), _run(2, head_branch="other"), _run(3)]
    jobs = {3: [_job(3, name="Lint (ruff)", id=1), _job(3, name="Types (mypy)", id=2), _job(3)]}
    api = FakeApi(runs, jobs)
    assert mr.find_reusable(api, REPO, SHA, "main", TESTED, same) == mr.Reuse(
        run_id=3, branch="pre-pr/feature", job_id=30
    )
    assert sum("/workflows/ci.yml/runs" in p for p in api.paths) == 2
    assert sum("/runs/3/jobs" in p for p in api.paths) == 2


def test_paging_stops_at_the_cap(mr: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr, "PER_PAGE", 1)
    api = FakeApi([_run(i, head_branch="other") for i in range(1, 20)], {})
    assert mr.find_reusable(api, REPO, SHA, "main", TESTED, same) is None
    assert len(api.paths) == 1 + mr.MAX_PAGES  # the workflow, then the capped pages of runs


def test_no_runs_means_no_reuse(mr: ModuleType) -> None:
    assert mr.find_reusable(FakeApi([], {}), REPO, SHA, "main", TESTED, same) is None


def test_a_pr_not_based_on_main_never_reuses_and_never_asks(mr: ModuleType) -> None:
    api = FakeApi([_run()], {RUN: [_job()]})
    assert mr.find_reusable(api, REPO, SHA, "release", TESTED, same) is None
    assert api.paths == []


@pytest.mark.parametrize(
    ("repo", "sha"),
    [
        ("owner/registry/../x", SHA),
        ("../..", SHA),
        ("./x", SHA),
        ("owner/.", SHA),
        ("owner/..", SHA),
        ("./..", SHA),
        ("/registry", SHA),
        ("owner/", SHA),
        ("owner", SHA),
        ("owner/re?po", SHA),
        (REPO, "A" * 40),
        (REPO, "a" * 39),
        (REPO, "pre-pr/feature"),
        (REPO, SHA + "\n"),
    ],
)
def test_bad_repo_or_sha_is_refused(mr: ModuleType, repo: str, sha: str) -> None:
    with pytest.raises(ValueError):
        mr.find_reusable(FakeApi([], {}), repo, sha, "main", TESTED, same)


ARGS = ["--repo", REPO, "--sha", SHA, "--base", "main", "--tested-commit", TESTED]


def test_main_prints_the_run_id_only_when_found(mr: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert mr.main(ARGS, FakeApi([_run()], {RUN: [_job()]}), same) == 0
    out = capsys.readouterr()
    assert out.out == f"{RUN}\n" and f"run {RUN}" in out.err
    assert mr.main(ARGS, FakeApi([_run()], {RUN: [_job(conclusion="failure")]}), same) == 0
    out = capsys.readouterr()
    assert out.out == "" and "every job runs" in out.err


def test_an_api_error_means_no_reuse(mr: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(path: str) -> Any:
        raise OSError("connection refused")

    assert mr.main(ARGS, broken, same) == 0
    out = capsys.readouterr()
    assert out.out == "" and "::warning::" in out.err and "connection refused" in out.err


def test_a_missing_token_means_no_reuse(
    mr: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert mr.main(ARGS, None, same) == 0
    out = capsys.readouterr()
    assert out.out == "" and "GITHUB_TOKEN" in out.err


def test_an_old_command_line_is_an_argument_error(mr: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    # The step tells this apart from other failures and runs every job (see the step tests below).
    with pytest.raises(SystemExit) as exc:
        mr.main(["--repo", REPO, "--sha", SHA, "--base", "main", "--base-sha", TESTED], FakeApi([], {}), same)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err or "arguments are required" in err


def test_the_api_url_must_be_https(mr: ModuleType) -> None:
    with pytest.raises(ValueError, match="https"):
        mr.github_get_json("http://api.github.com", "token")
    assert callable(mr.github_get_json("https://api.github.com/", "token"))


def test_a_different_tested_tree_never_reuses_and_never_asks(mr: ModuleType) -> None:
    api = FakeApi([_run()], {RUN: [_job()]})
    assert mr.find_reusable(api, REPO, SHA, "main", TESTED, differs) is None
    assert api.paths == []


def test_main_passes_the_tested_commit_to_the_tree_check(
    mr: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, str]] = []

    def record(tested: str, head: str) -> bool:
        seen.append((tested, head))
        return False

    assert mr.main(ARGS, FakeApi([_run()], {RUN: [_job()]}), record) == 0
    out = capsys.readouterr()
    assert seen == [(TESTED, SHA)] and out.out == "" and "tested tree differs" in out.err


def test_a_failing_tree_check_means_no_reuse(mr: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(tested: str, head: str) -> bool:
        raise RuntimeError("git rev-parse found no tree")

    assert mr.main(ARGS, FakeApi([_run()], {RUN: [_job()]}), broken) == 0
    out = capsys.readouterr()
    assert out.out == "" and "::warning::" in out.err and "no tree" in out.err


@pytest.mark.parametrize("tested", ["C" * 40, "c" * 39, "HEAD"])
def test_a_bad_tested_commit_is_refused(mr: ModuleType, tested: str) -> None:
    with pytest.raises(ValueError, match="tested"):
        mr.find_reusable(FakeApi([], {}), REPO, SHA, "main", tested, same)


def test_names_with_dots_that_are_not_dot_segments_are_accepted(mr: ModuleType) -> None:
    paths: list[str] = []

    def empty(path: str) -> Any:
        paths.append(path)
        return {"id": WORKFLOW_ID, "path": ".github/workflows/ci.yml", "workflow_runs": []}

    for repo in ("my.org/.github", ".owner/x", "owner/.registry", "o/x..", "o/...", "my.org/re.po", "o-1/_x"):
        assert mr.find_reusable(empty, repo, SHA, "main", TESTED, same) is None
    assert paths[0] == "/repos/my.org/.github/actions/workflows/ci.yml"


def test_a_run_path_with_a_ref_suffix_still_matches(mr: ModuleType) -> None:
    run = _run(path=".github/workflows/ci.yml@refs/heads/pre-pr/feature")
    assert mr.find_reusable(FakeApi([run], {RUN: [_job()]}), REPO, SHA, "main", TESTED, same) == mr.Reuse(
        run_id=RUN, branch="pre-pr/feature", job_id=RUN * 10
    )


@pytest.mark.parametrize(
    "workflow",
    [
        {"id": WORKFLOW_ID, "path": ".github/workflows/codeql.yml"},
        {"id": "4242", "path": ".github/workflows/ci.yml"},
    ],
)
def test_an_unexpected_workflow_is_an_error(mr: ModuleType, workflow: dict[str, Any]) -> None:
    api = FakeApi([_run()], {RUN: [_job()]})
    api.workflow = workflow
    with pytest.raises(ValueError, match="unexpected workflow"):
        mr.find_reusable(api, REPO, SHA, "main", TESTED, same)


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A hermetic git: no global or system config, no signing, no repository found above tmp_path."""
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    hermetic = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(tmp_path.parent),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    for name, value in hermetic.items():
        monkeypatch.setenv(name, value)
    return dict(os.environ)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _pr_history(tmp_path: Path) -> dict[str, str]:
    """main at base1; a PR head that contains base1; then main moves to base2, touching a file the head lacks.

    merge_fresh is what GitHub builds while main is still base1 (the head's own tree); merge_moved is what it builds
    once main is base2. The payload's base SHA can still say base1, which the head contains: only the tree tells.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    base1 = _commit(repo, {"f": "1"}, "base1")
    _git(repo, "checkout", "-q", "-b", "pr")
    head = _commit(repo, {"g": "1"}, "head")
    merge_fresh = _git(repo, "commit-tree", f"{head}^{{tree}}", "-p", base1, "-p", head, "-m", "merge fresh")
    _git(repo, "checkout", "-q", "main")
    base2 = _commit(repo, {"f": "2"}, "base2")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge moved", head)
    merge_moved = _git(repo, "rev-parse", "HEAD")
    return {
        "repo": str(repo),
        "base1": base1,
        "head": head,
        "base2": base2,
        "fresh": merge_fresh,
        "moved": merge_moved,
    }


def test_git_same_tree_compares_the_tested_merge_with_the_head(
    mr: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_env: dict[str, str]
) -> None:
    h = _pr_history(tmp_path)
    monkeypatch.chdir(h["repo"])
    assert mr.git_same_tree(h["fresh"], h["head"]) is True  # the merge is the head's own tree
    assert mr.git_same_tree(h["moved"], h["head"]) is False  # main moved on: a different tree
    assert mr.git_same_tree(h["head"], h["head"]) is True
    with pytest.raises(RuntimeError, match="no tree"):
        mr.git_same_tree("d" * 40, h["head"])  # a commit the checkout lacks is an error, so no reuse


def test_when_main_moved_the_verdict_is_refused_end_to_end(
    mr: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    git_env: dict[str, str],
) -> None:
    h = _pr_history(tmp_path)
    monkeypatch.chdir(h["repo"])
    head = h["head"]
    api = FakeApi([_run(head_sha=head)], {RUN: [_job(head_sha=head)]}, sha=head)
    args = ["--repo", REPO, "--sha", head, "--base", "main"]
    # Main moved after the pre-pr run, though the head still contains the old base: refused, the API never asked.
    assert mr.main([*args, "--tested-commit", h["moved"]], api) == 0
    assert capsys.readouterr().out == "" and api.paths == []
    # Main as the head already has it: the merge is the head's tree, so the green verdict is reused.
    assert mr.main([*args, "--tested-commit", h["fresh"]], api) == 0
    assert capsys.readouterr().out == f"{RUN}\n"


def _ci() -> dict[str, Any]:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pull_request_target" not in text
    data: dict[str, Any] = yaml.safe_load(text)
    return data


@reads_workflows
def test_the_workflow_wires_the_lookup_safely() -> None:
    ci = _ci()
    assert ci["permissions"] == {"contents": "read"}
    jobs = ci["jobs"]
    changes = jobs["changes"]
    assert changes["permissions"] == {"contents": "read", "actions": "read"}
    reuse = next(s for s in changes["steps"] if s.get("id") == "reuse")
    assert reuse["if"] == "github.event_name == 'pull_request' && github.base_ref == 'main'"
    assert reuse["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert reuse["env"]["REPO"] == "${{ github.repository }}"
    assert "BASE_SHA" not in reuse["env"]  # the tested tree decides, not the payload's base SHA
    assert reuse["env"]["GITHUB_TOKEN"] == "${{ github.token }}"  # noqa: S105 (an expression, not a secret)
    assert "${{" not in reuse["run"]  # values reach the shell only through env, quoted
    assert changes["outputs"]["reused_run"] == "${{ steps.reuse.outputs.run_id }}"
    # actions: read only where the API is read: the reuse lookup (changes) and the CI budget step (result).
    assert jobs["result"]["permissions"] == {"contents": "read", "actions": "read"}
    assert all("permissions" not in job for name, job in jobs.items() if name not in ("changes", "result"))
    # Every other job of the PR's run is skipped on reuse, and "CI result" names the reused run.
    for name in ("checks", "test", "mutation"):
        assert "needs.changes.outputs.reused_run == ''" in jobs[name]["if"], name
    result = jobs["result"]
    assert result["name"] == "CI result"
    assert result["env"]["REUSED_RUN"] == "${{ needs.changes.outputs.reused_run }}"


def _reuse_step() -> dict[str, Any]:
    listing = _ci()["jobs"]["changes"]
    step: dict[str, Any] = next(s for s in listing["steps"] if s.get("id") == "reuse")
    return step


def _checkout(tmp_path: Path, *, base_has_script: bool = True) -> tuple[Path, str]:
    """A checkout whose origin/main may hold the lookup script, with a PR-edited copy in the working tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    files = {"f": "1"}
    if base_has_script:
        files["scripts/verdict_reuse.py"] = "# main's copy\n"
    _commit(repo, files, "main")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    tested = _commit(repo, {"scripts/verdict_reuse.py": "# the PR's edited copy\n"}, "pr merge")
    return repo, tested


def _run_reuse_step(tmp_path: Path, uv_body: str, cwd: Path) -> tuple[str, str, str]:
    """Run the reuse step's script as Actions does (bash -e, pipefail), with a fake `uv` on PATH."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    uv = fake_bin / "uv"
    uv.write_text(f'#!/usr/bin/env bash\necho "$@" >> {tmp_path}/uv-args\n{uv_body}\n', encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out),
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_TOKEN": "dummy",
        "REPO": REPO,
        "HEAD_SHA": SHA,
        "BASE_REF": "main",
    }
    done = subprocess.run(  # noqa: S603
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _reuse_step()["run"]],  # noqa: S607
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    uv_args = tmp_path / "uv-args"
    args = uv_args.read_text(encoding="utf-8") if uv_args.exists() else ""
    return out.read_text(encoding="utf-8"), args, done.stdout + done.stderr


MISMATCH = "echo 'verdict_reuse.py: error: unrecognized arguments: --tested-commit 0' >&2; exit 2"


@reads_workflows
@pytest.mark.parametrize(
    ("uv_body", "expected", "logged"),
    [
        ("echo 123", "run_id=123\n", "Lookup: origin/main:scripts/verdict_reuse.py"),
        ("echo ''", "run_id=\n", "Lookup: origin/main"),
        ("exit 1", "run_id=\n", "lookup failed (exit 1)"),
        (MISMATCH, "run_id=\n", "::warning::Argument mismatch"),
        ("echo 123; exit 2", "run_id=\n", "lookup failed (exit 2)"),
        ("echo 123; exit 1", "run_id=\n", "lookup failed (exit 1)"),
        ("echo 'not a number'", "run_id=\n", "Unexpected lookup output"),
    ],
    ids=[
        "found",
        "none",
        "lookup-fails",
        "argument-mismatch",
        "other-exit-2",
        "prints-then-fails",
        "garbage",
    ],
)
def test_the_reuse_step_runs_mains_copy_and_falls_back_to_running_everything(
    tmp_path: Path, git_env: dict[str, str], uv_body: str, expected: str, logged: str
) -> None:
    repo, tested = _checkout(tmp_path)
    out, args, log = _run_reuse_step(tmp_path, uv_body, repo)
    assert out == expected
    assert logged in log
    copy = tmp_path / "runner_temp" / "verdict_reuse.py"
    assert f"python {copy} --repo {REPO} --sha {SHA} --base main --tested-commit {tested}" in args
    assert copy.read_text(encoding="utf-8") == "# main's copy\n"  # never the PR's edited copy


@reads_workflows
def test_with_no_copy_on_main_nothing_is_reused_and_the_lookup_never_runs(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    repo, _ = _checkout(tmp_path, base_has_script=False)
    out, args, log = _run_reuse_step(tmp_path, "echo 123", repo)
    assert out == "run_id=\n" and args == ""
    assert "origin/main has no scripts/verdict_reuse.py" in log


@reads_workflows
def test_outside_a_checkout_nothing_is_reused(tmp_path: Path, git_env: dict[str, str]) -> None:
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    out, args, _ = _run_reuse_step(tmp_path, "echo 123", bare)
    assert out == "run_id=\n" and args == ""


@reads_workflows
def test_every_action_is_pinned_by_sha() -> None:
    for job in _ci()["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                ref = step["uses"].split("@", 1)[1]
                assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), step["uses"]
