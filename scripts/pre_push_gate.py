"""The pre-PR gate's local half at push time: fast checks before a pre-pr push, a green verdict before a PR push.

Installed by `make hooks` as a pre-commit `pre-push` hook (.pre-commit-config.yaml). pre-commit runs it once per
pushed ref and tells it where the push goes (PRE_COMMIT_REMOTE_BRANCH, PRE_COMMIT_TO_REF, PRE_COMMIT_REMOTE_URL).

- A push to a PR branch (feat/, fix/, ci/, docs/, transform/) is refused unless that exact commit already has a
  completed, all-green run of CI (.github/workflows/ci.yml) from a push to a pre-pr/** branch of this repository.
  It asks the GitHub API through the GitHub CLI (`gh api`, with gh's own login). Any error, gh missing included,
  refuses the push: it fails closed.
- A push to pre-pr/** first runs the fast checks: ruff (lint and format), mypy, and the offline tests of the
  modules the push changes (no coverage floor, no mutation; CI runs those on the pre-pr push).
- Any other push (wip/**, a deleted branch) passes.

This hook is fast feedback only. The rule that counts is on the server: the PR-branch ruleset requires the "CI
result" check on a commit before any push may move a PR branch to it (docs/dev/README.md#the-pre-pr-gate).
Usage outside pre-commit: python scripts/pre_push_gate.py --branch ci/x --sha <sha> [--repo OWNER/NAME]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import mutation_reuse

ROOT = Path(__file__).resolve().parents[1]
PR_PREFIXES = ("feat/", "fix/", "ci/", "docs/", "transform/")
PRE_PR = "pre-pr/"
ZERO_SHA = "0" * 40
GetJson = Callable[[str], Any]
Run = Callable[[Sequence[str]], int]


def kind(branch: str) -> str:
    """'pr', 'pre-pr' or 'other' for a remote branch name (refs/heads/ is dropped)."""
    name = branch.removeprefix("refs/heads/")
    if name.startswith(PRE_PR):
        return "pre-pr"
    if name.startswith(PR_PREFIXES):
        return "pr"
    return "other"


def repo_from_url(url: str) -> str:
    """OWNER/NAME from a remote URL (https, ssh or a proxy path ending in /OWNER/NAME[.git])."""
    m = re.search(r"[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", url)
    if not m:
        raise ValueError(f"cannot tell the GitHub repository from the remote URL {url!r}")
    return f"{m[1]}/{m[2]}"


def green_pre_pr_run(get_json: GetJson, repo: str, sha: str) -> dict[str, Any] | None:
    """The newest completed, successful CI run for exactly sha from a push to pre-pr/** of repo, or None."""
    if not mutation_reuse.SHA.fullmatch(sha):
        raise ValueError(f"not a full lowercase commit SHA: {sha!r}")
    for run in mutation_reuse._push_runs(get_json, repo, sha):
        if (
            mutation_reuse._is_pre_pr_run(run, repo, sha)
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
        ):
            return run
    return None


def gh_get_json(path: str) -> Any:
    """GET an API path with the GitHub CLI (`gh api`), which brings its own login."""
    proc = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:300] or f"gh api exited {proc.returncode}")
    return json.loads(proc.stdout)


def changed_files(old: str, sha: str, run_git: Callable[[Sequence[str]], str]) -> list[str]:
    """Files the push changes: since the branch's old commit, or since its merge base with origin/main if new."""
    since = old if old and old != ZERO_SHA else run_git(["merge-base", "origin/main", sha]).strip()
    out = run_git(["diff", "--name-only", since, sha, "--"])
    return [line for line in out.splitlines() if line]


def affected_tests(files: list[str], root: Path = ROOT) -> list[str]:
    """Test modules for the changed files: changed tests/test_*.py, and tests/test_<name>.py for a changed module."""
    found = set()
    for path in files:
        p = Path(path)
        if path.startswith("tests/") and p.name.startswith("test_") and p.suffix == ".py":
            found.add(path)
        elif p.suffix == ".py" and path.startswith(("src/", "eval/", "scripts/", "tools/")):
            candidate = f"tests/test_{p.stem}.py"
            if (root / candidate).is_file():
                found.add(candidate)
    return sorted(p for p in found if (root / p).is_file())


def fast_checks(tests: list[str], run: Run) -> int:
    """ruff, ruff format, mypy, then the affected tests; stops at the first failure."""
    steps: list[list[str]] = [
        ["uv", "run", "ruff", "check", "src", "tests", "eval", "tools"],
        ["uv", "run", "ruff", "format", "--check", "src", "tests", "eval", "tools"],
        ["uv", "run", "mypy"],
    ]
    if tests:
        steps.append(
            ["uv", "run", "pytest", "-m", "not network", "-q", "-x", "-p", "no:cacheprovider", *tests]
        )
    for step in steps:
        print("==> " + " ".join(step), file=sys.stderr)
        code = run(step)
        if code != 0:
            print(
                f"Push refused: `{' '.join(step)}` failed. Fix it, commit, and push again.", file=sys.stderr
            )
            return code
    if not tests:
        print(
            "No test module matches the changed files; CI runs the whole suite on the pre-pr push.",
            file=sys.stderr,
        )
    return 0


def check_pr_push(get_json: GetJson, repo: str, branch: str, sha: str) -> int:
    try:
        run = green_pre_pr_run(get_json, repo, sha)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired, AttributeError, TypeError) as e:
        print(
            f"Push to {branch} refused: could not ask GitHub whether {sha[:12]} passed on pre-pr/ "
            f"({type(e).__name__}: {e}). Try again when GitHub answers; nothing was pushed.",
            file=sys.stderr,
        )
        return 1
    if run is None:
        print(
            f"Push to {branch} refused: {sha[:12]} has no completed, all-green CI run on a pre-pr/ branch.\n"
            f"Run `make precheck`, push this commit to pre-pr/{branch.removeprefix('refs/heads/')}, wait for every "
            "check to go green, then push the same commit here (docs/dev/README.md#the-pre-pr-gate).",
            file=sys.stderr,
        )
        return 1
    print(
        f"{sha[:12]} passed CI on {run.get('head_branch')} (run {run.get('id')}); push allowed.",
        file=sys.stderr,
    )
    return 0


def _git(args: Sequence[str]) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def _run(cmd: Sequence[str]) -> int:
    return subprocess.run(list(cmd), cwd=ROOT, check=False).returncode


def main(
    argv: list[str] | None = None,
    get_json: GetJson | None = None,
    run: Run = _run,
    run_git: Callable[[Sequence[str]], str] = _git,
) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--branch", default=os.environ.get("PRE_COMMIT_REMOTE_BRANCH", ""))
    ap.add_argument("--sha", default=os.environ.get("PRE_COMMIT_TO_REF", ""))
    ap.add_argument("--from-sha", default=os.environ.get("PRE_COMMIT_FROM_REF", ""))
    ap.add_argument("--repo", default="")
    a = ap.parse_args(argv)
    if not a.branch or not a.sha:
        print("Not run from a pre-push hook and no --branch/--sha given; nothing to check.", file=sys.stderr)
        return 0
    if a.sha == ZERO_SHA:
        return 0  # a branch deletion
    what = kind(a.branch)
    if what == "pr":
        try:
            repo = a.repo or repo_from_url(os.environ.get("PRE_COMMIT_REMOTE_URL", ""))
        except ValueError as e:
            print(f"Push to {a.branch} refused: {e}. Pass --repo OWNER/NAME.", file=sys.stderr)
            return 1
        return check_pr_push(get_json or gh_get_json, repo, a.branch, a.sha)
    if what == "pre-pr":
        try:
            files = changed_files(a.from_sha, a.sha, run_git)
        except subprocess.CalledProcessError as e:
            print(
                f"Push refused: could not list the changed files ({e}). Run git fetch origin main.",
                file=sys.stderr,
            )
            return 1
        return fast_checks(affected_tests(files), run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
