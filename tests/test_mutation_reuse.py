"""scripts/mutation_reuse.py: a PR reuses only an exact, green pre-pr mutation verdict, against a fake API."""

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
BASE_SHA = "c" * 40
RUN = 101
WORKFLOW_ID = 4242
# .github/ is not copied into mutants/, where mutmut runs the suite.
reads_workflows = pytest.mark.skipif("MUTANT_UNDER_TEST" in os.environ, reason=".github/ is not in mutants/")


@pytest.fixture
def mr(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("mutation_reuse", ROOT / "scripts" / "mutation_reuse.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "mutation_reuse", mod)
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
        "name": "Mutation tests (changed modules)",
        "status": "completed",
        "conclusion": "success",
        "head_sha": SHA,
    }
    job.update(over)
    return job


def contains(ancestor: str, descendant: str) -> bool:
    """The branch is up to date: the head contains the base."""
    assert (ancestor, descendant) == (BASE_SHA, SHA)
    return True


def behind(ancestor: str, descendant: str) -> bool:
    """Main moved on after the pre-pr run: the head does not contain the base."""
    return False


class FakeApi:
    """Answers the two endpoints the script reads, and records every request."""

    def __init__(self, runs: list[dict[str, Any]], jobs: dict[int, list[dict[str, Any]]]) -> None:
        self.runs, self.jobs, self.paths = runs, jobs, []
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
            assert q["head_sha"] == SHA and q["event"] == "push"
            return {"workflow_runs": self.runs[lo:hi]}
        prefix = f"/repos/{REPO}/actions/runs/"
        assert url.path.startswith(prefix) and url.path.endswith("/jobs"), path
        assert q["filter"] == "latest"
        run_id = int(url.path[len(prefix) : -len("/jobs")])
        return {"jobs": self.jobs.get(run_id, [])[lo:hi]}


def test_a_green_pre_pr_verdict_for_the_same_sha_is_reused(mr: ModuleType) -> None:
    api = FakeApi([_run()], {RUN: [_job(name="Lint (ruff)", id=1), _job()]})
    assert mr.find_reusable(api, REPO, SHA, "main", BASE_SHA, contains) == mr.Reuse(
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
    assert mr.find_reusable(FakeApi([_run()], {RUN: [job]}), REPO, SHA, "main", BASE_SHA, contains) is None


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
    assert (
        mr.find_reusable(FakeApi([_run(**over)], {RUN: [_job()]}), REPO, SHA, "main", BASE_SHA, contains)
        is None
    )


def test_a_later_green_run_is_found_past_a_red_one(mr: ModuleType) -> None:
    api = FakeApi([_run(7), _run(8)], {7: [_job(7, conclusion="failure")], 8: [_job(8)]})
    assert mr.find_reusable(api, REPO, SHA, "main", BASE_SHA, contains) == mr.Reuse(
        run_id=8, branch="pre-pr/feature", job_id=80
    )


def test_runs_and_jobs_are_paged(mr: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr, "PER_PAGE", 2)
    runs = [_run(1, head_branch="other"), _run(2, head_branch="other"), _run(3)]
    jobs = {3: [_job(3, name="Lint (ruff)", id=1), _job(3, name="Types (mypy)", id=2), _job(3)]}
    api = FakeApi(runs, jobs)
    assert mr.find_reusable(api, REPO, SHA, "main", BASE_SHA, contains) == mr.Reuse(
        run_id=3, branch="pre-pr/feature", job_id=30
    )
    assert sum("/workflows/ci.yml/runs" in p for p in api.paths) == 2
    assert sum("/runs/3/jobs" in p for p in api.paths) == 2


def test_paging_stops_at_the_cap(mr: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr, "PER_PAGE", 1)
    api = FakeApi([_run(i, head_branch="other") for i in range(1, 20)], {})
    assert mr.find_reusable(api, REPO, SHA, "main", BASE_SHA, contains) is None
    assert len(api.paths) == 1 + mr.MAX_PAGES  # the workflow, then the capped pages of runs


def test_no_runs_means_no_reuse(mr: ModuleType) -> None:
    assert mr.find_reusable(FakeApi([], {}), REPO, SHA, "main", BASE_SHA, contains) is None


def test_a_pr_not_based_on_main_never_reuses_and_never_asks(mr: ModuleType) -> None:
    api = FakeApi([_run()], {RUN: [_job()]})
    assert mr.find_reusable(api, REPO, SHA, "release", BASE_SHA, contains) is None
    assert api.paths == []


@pytest.mark.parametrize(
    ("repo", "sha"),
    [
        ("owner/registry/../x", SHA),
        ("../..", SHA),
        ("./x", SHA),
        ("owner/.", SHA),
        ("owner/..", SHA),
        (".owner/x", SHA),
        ("owner/.registry", SHA),
        ("/registry", SHA),
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
        mr.find_reusable(FakeApi([], {}), repo, sha, "main", BASE_SHA, contains)


def test_main_prints_the_run_id_only_when_found(mr: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--repo", REPO, "--sha", SHA, "--base", "main", "--base-sha", BASE_SHA]
    assert mr.main(args, FakeApi([_run()], {RUN: [_job()]}), contains) == 0
    out = capsys.readouterr()
    assert out.out == f"{RUN}\n" and f"run {RUN}" in out.err
    assert mr.main(args, FakeApi([_run()], {RUN: [_job(conclusion="failure")]}), contains) == 0
    out = capsys.readouterr()
    assert out.out == "" and "the shards run" in out.err


def test_an_api_error_means_no_reuse(mr: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(path: str) -> Any:
        raise OSError("connection refused")

    assert (
        mr.main(["--repo", REPO, "--sha", SHA, "--base", "main", "--base-sha", BASE_SHA], broken, contains)
        == 0
    )
    out = capsys.readouterr()
    assert out.out == "" and "::warning::" in out.err and "connection refused" in out.err


def test_a_missing_token_means_no_reuse(
    mr: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert (
        mr.main(["--repo", REPO, "--sha", SHA, "--base", "main", "--base-sha", BASE_SHA], None, contains) == 0
    )
    out = capsys.readouterr()
    assert out.out == "" and "GITHUB_TOKEN" in out.err


def test_the_api_url_must_be_https(mr: ModuleType) -> None:
    with pytest.raises(ValueError, match="https"):
        mr.github_get_json("http://api.github.com", "token")
    assert callable(mr.github_get_json("https://api.github.com/", "token"))


def test_a_branch_behind_main_never_reuses_and_never_asks(mr: ModuleType) -> None:
    api = FakeApi([_run()], {RUN: [_job()]})
    assert mr.find_reusable(api, REPO, SHA, "main", BASE_SHA, behind) is None
    assert api.paths == []


def test_main_passes_the_base_sha_to_the_ancestry_check(
    mr: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, str]] = []

    def record(ancestor: str, descendant: str) -> bool:
        seen.append((ancestor, descendant))
        return False

    args = ["--repo", REPO, "--sha", SHA, "--base", "main", "--base-sha", BASE_SHA]
    assert mr.main(args, FakeApi([_run()], {RUN: [_job()]}), record) == 0
    out = capsys.readouterr()
    assert seen == [(BASE_SHA, SHA)] and out.out == "" and "behind its base" in out.err


def test_a_failing_ancestry_check_means_no_reuse(mr: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(ancestor: str, descendant: str) -> bool:
        raise RuntimeError("git merge-base failed (128): bad object")

    args = ["--repo", REPO, "--sha", SHA, "--base", "main", "--base-sha", BASE_SHA]
    assert mr.main(args, FakeApi([_run()], {RUN: [_job()]}), broken) == 0
    out = capsys.readouterr()
    assert out.out == "" and "::warning::" in out.err and "bad object" in out.err


@pytest.mark.parametrize("base_sha", ["C" * 40, "c" * 39, "main"])
def test_a_bad_base_sha_is_refused(mr: ModuleType, base_sha: str) -> None:
    with pytest.raises(ValueError, match="base"):
        mr.find_reusable(FakeApi([], {}), REPO, SHA, "main", base_sha, contains)


def test_dotted_names_that_are_not_dot_segments_are_accepted(mr: ModuleType) -> None:
    paths: list[str] = []

    def empty(path: str) -> Any:
        paths.append(path)
        return {"id": WORKFLOW_ID, "path": ".github/workflows/ci.yml", "workflow_runs": []}

    for repo in ("my.org/re.po", "o-1/_x", "o/x.."):
        assert mr.find_reusable(empty, repo, SHA, "main", BASE_SHA, lambda a, d: True) is None
    assert paths[0] == "/repos/my.org/re.po/actions/workflows/ci.yml"


def test_a_run_path_with_a_ref_suffix_still_matches(mr: ModuleType) -> None:
    run = _run(path=".github/workflows/ci.yml@refs/heads/pre-pr/feature")
    assert mr.find_reusable(
        FakeApi([run], {RUN: [_job()]}), REPO, SHA, "main", BASE_SHA, contains
    ) == mr.Reuse(run_id=RUN, branch="pre-pr/feature", job_id=RUN * 10)


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
        mr.find_reusable(api, REPO, SHA, "main", BASE_SHA, contains)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return done.stdout.strip()


def _repo_with_two_commits(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for n in ("1", "2"):
        (repo / "f").write_text(n, encoding="utf-8")
        _git(repo, "add", "f")
        _git(
            repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            n,
        )
    return repo, _git(repo, "rev-parse", "HEAD~1"), _git(repo, "rev-parse", "HEAD")


def test_git_is_ancestor_reads_the_checkout(
    mr: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, old, new = _repo_with_two_commits(tmp_path)
    monkeypatch.chdir(repo)
    assert mr.git_is_ancestor(old, new) is True  # up to date: the head contains the base
    assert mr.git_is_ancestor(new, old) is False  # behind: main moved on
    with pytest.raises(RuntimeError, match="git merge-base failed"):
        mr.git_is_ancestor("d" * 40, new)  # a commit the checkout lacks is an error, so no reuse


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
    listing = jobs["mutation-list"]
    assert listing["permissions"] == {"contents": "read", "actions": "read"}
    reuse = next(s for s in listing["steps"] if s.get("id") == "reuse")
    assert reuse["if"] == "github.event_name == 'pull_request'"
    assert reuse["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert reuse["env"]["REPO"] == "${{ github.repository }}"
    assert reuse["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert reuse["env"]["GITHUB_TOKEN"] == "${{ github.token }}"  # noqa: S105 (an expression, not a secret)
    assert "${{" not in reuse["run"]  # values reach the shell only through env, quoted
    lst = next(s for s in listing["steps"] if s.get("id") == "list")
    assert lst["env"]["REUSED_RUN"] == "${{ steps.reuse.outputs.run_id }}"
    assert listing["outputs"]["reused_run"] == "${{ steps.reuse.outputs.run_id }}"
    # Only the list job gets actions: read; the shards and the aggregate keep the read-only default.
    assert "permissions" not in jobs["mutation"] and "permissions" not in jobs["mutation-aggregate"]
    # The aggregate keeps its check name, and either reports the reused run or applies the floor.
    agg = jobs["mutation-aggregate"]
    assert agg["name"] == "Mutation tests (changed modules)"
    conds = {s.get("name"): s.get("if") for s in agg["steps"]}
    assert conds["Report the reused pre-pr verdict"] == "needs.mutation-list.outputs.reused_run != ''"
    assert conds["Apply the floor to the total"] == "needs.mutation-list.outputs.reused_run == ''"


def _reuse_step() -> dict[str, Any]:
    listing = _ci()["jobs"]["mutation-list"]
    step: dict[str, Any] = next(s for s in listing["steps"] if s.get("id") == "reuse")
    return step


def _run_reuse_step(tmp_path: Path, uv_body: str, cwd: Path | None = None) -> tuple[str, str]:
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
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out),
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_TOKEN": "dummy",
        "REPO": REPO,
        "HEAD_SHA": SHA,
        "BASE_SHA": BASE_SHA,
        "BASE_REF": "main",
    }
    done = subprocess.run(  # noqa: S603
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _reuse_step()["run"]],  # noqa: S607
        cwd=cwd or tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    args = (tmp_path / "uv-args").read_text(encoding="utf-8")
    return out.read_text(encoding="utf-8"), args


@reads_workflows
@pytest.mark.parametrize(
    ("uv_body", "expected"),
    [
        ("echo 123", "run_id=123\n"),
        ("echo ''", "run_id=\n"),
        ("exit 1", "run_id=\n"),
        ("exit 2", "run_id=\n"),
        ("echo 123; exit 1", "run_id=\n"),
        ("echo 'not a number'", "run_id=\n"),
    ],
    ids=["found", "none", "lookup-fails", "bad-args", "prints-then-fails", "garbage"],
)
def test_the_reuse_step_falls_back_to_the_shards_on_any_failure(
    tmp_path: Path, uv_body: str, expected: str
) -> None:
    out, args = _run_reuse_step(tmp_path, uv_body)
    assert out == expected
    assert f"--sha {SHA} --base main --base-sha {BASE_SHA}" in args
    assert "scripts/mutation_reuse.py" in args  # no base-branch copy outside a git checkout: the PR's own


@reads_workflows
def test_the_reuse_step_runs_the_base_branchs_copy_of_the_lookup(tmp_path: Path) -> None:
    repo, _, head = _repo_with_two_commits(tmp_path)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "mutation_reuse.py").write_text("# the base branch's copy\n", encoding="utf-8")
    _git(repo, "add", "scripts")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "s",
    )
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "scripts" / "mutation_reuse.py").write_text("# the PR's edited copy\n", encoding="utf-8")
    out, args = _run_reuse_step(tmp_path, "echo 7", cwd=repo)
    assert out == "run_id=7\n"
    copy = tmp_path / "runner_temp" / "mutation_reuse.py"
    assert f"python {copy} " in args
    assert copy.read_text(encoding="utf-8") == "# the base branch's copy\n"


@reads_workflows
def test_every_action_is_pinned_by_sha() -> None:
    for job in _ci()["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                ref = step["uses"].split("@", 1)[1]
                assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), step["uses"]
