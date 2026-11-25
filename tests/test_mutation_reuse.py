"""scripts/mutation_reuse.py: a PR reuses only an exact, green pre-pr mutation verdict, against a fake API."""

from __future__ import annotations

import importlib.util
import os
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
RUN = 101
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


class FakeApi:
    """Answers the two endpoints the script reads, and records every request."""

    def __init__(self, runs: list[dict[str, Any]], jobs: dict[int, list[dict[str, Any]]]) -> None:
        self.runs, self.jobs, self.paths = runs, jobs, []

    def __call__(self, path: str) -> Any:
        self.paths.append(path)
        url = urllib.parse.urlsplit(path)
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
    assert mr.find_reusable(api, REPO, SHA, "main") == mr.Reuse(
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
    assert mr.find_reusable(FakeApi([_run()], {RUN: [job]}), REPO, SHA, "main") is None


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
        {"repository": {"full_name": "someone/fork"}},
        {"head_repository": {"full_name": "someone/fork"}},
        {"head_repository": None},
    ],
    ids=str,
)
def test_only_a_push_run_of_this_workflow_on_a_pre_pr_branch_here_counts(
    mr: ModuleType, over: dict[str, Any]
) -> None:
    assert mr.find_reusable(FakeApi([_run(**over)], {RUN: [_job()]}), REPO, SHA, "main") is None


def test_a_later_green_run_is_found_past_a_red_one(mr: ModuleType) -> None:
    api = FakeApi([_run(7), _run(8)], {7: [_job(7, conclusion="failure")], 8: [_job(8)]})
    assert mr.find_reusable(api, REPO, SHA, "main") == mr.Reuse(run_id=8, branch="pre-pr/feature", job_id=80)


def test_runs_and_jobs_are_paged(mr: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr, "PER_PAGE", 2)
    runs = [_run(1, head_branch="other"), _run(2, head_branch="other"), _run(3)]
    jobs = {3: [_job(3, name="Lint (ruff)", id=1), _job(3, name="Types (mypy)", id=2), _job(3)]}
    api = FakeApi(runs, jobs)
    assert mr.find_reusable(api, REPO, SHA, "main") == mr.Reuse(run_id=3, branch="pre-pr/feature", job_id=30)
    assert sum("/workflows/ci.yml/runs" in p for p in api.paths) == 2
    assert sum("/runs/3/jobs" in p for p in api.paths) == 2


def test_paging_stops_at_the_cap(mr: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr, "PER_PAGE", 1)
    api = FakeApi([_run(i, head_branch="other") for i in range(1, 20)], {})
    assert mr.find_reusable(api, REPO, SHA, "main") is None
    assert len(api.paths) == mr.MAX_PAGES


def test_no_runs_means_no_reuse(mr: ModuleType) -> None:
    assert mr.find_reusable(FakeApi([], {}), REPO, SHA, "main") is None


def test_a_pr_not_based_on_main_never_reuses_and_never_asks(mr: ModuleType) -> None:
    api = FakeApi([_run()], {RUN: [_job()]})
    assert mr.find_reusable(api, REPO, SHA, "release") is None
    assert api.paths == []


@pytest.mark.parametrize(
    ("repo", "sha"),
    [
        ("owner/registry/../x", SHA),
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
        mr.find_reusable(FakeApi([], {}), repo, sha, "main")


def test_main_prints_the_run_id_only_when_found(mr: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--repo", REPO, "--sha", SHA, "--base", "main"]
    assert mr.main(args, FakeApi([_run()], {RUN: [_job()]})) == 0
    out = capsys.readouterr()
    assert out.out == f"{RUN}\n" and f"run {RUN}" in out.err
    assert mr.main(args, FakeApi([_run()], {RUN: [_job(conclusion="failure")]})) == 0
    out = capsys.readouterr()
    assert out.out == "" and "the shards run" in out.err


def test_an_api_error_means_no_reuse(mr: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(path: str) -> Any:
        raise OSError("connection refused")

    assert mr.main(["--repo", REPO, "--sha", SHA, "--base", "main"], broken) == 0
    out = capsys.readouterr()
    assert out.out == "" and "::warning::" in out.err and "connection refused" in out.err


def test_a_missing_token_means_no_reuse(
    mr: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert mr.main(["--repo", REPO, "--sha", SHA, "--base", "main"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and "GITHUB_TOKEN" in out.err


def test_the_api_url_must_be_https(mr: ModuleType) -> None:
    with pytest.raises(ValueError, match="https"):
        mr.github_get_json("http://api.github.com", "token")
    assert callable(mr.github_get_json("https://api.github.com/", "token"))


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


@reads_workflows
def test_every_action_is_pinned_by_sha() -> None:
    for job in _ci()["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                ref = step["uses"].split("@", 1)[1]
                assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), step["uses"]
