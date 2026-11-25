"""Find a green pre-pr CI verdict for a pull request's head commit, so CI need not run it twice.

Usage (CI, pull_request events only; see the changes job in .github/workflows/ci.yml):
  python scripts/verdict_reuse.py --repo OWNER/NAME --sha HEAD_SHA --base BASE_REF --tested-commit TESTED_SHA

The pre-PR gate (docs/dev/README.md#the-pre-pr-gate) runs CI on `pre-pr/<branch>` and then on the PR, for the same
commit. The PR's run reuses the whole pre-pr verdict ("CI result": checks, tests, dbt and mutation) when, and only
when, all of these hold (the tree-equality rule of #96):

- the PR targets main (a pre-pr run compares with origin/main, so another base changes what it checked);
- the commit this run actually tests (TESTED_SHA: `git rev-parse HEAD` after checkout, the merge commit
  refs/pull/N/merge on a `pull_request` run) has exactly the same tree as HEAD_SHA. Then the code, the tests,
  ci.yml and every script are exactly what the pre-pr run checked. This compares the trees themselves, not the
  payload's base SHA. A branch behind main, or one whose main moved on, gives a different merge tree and never
  reuses;
- a run of this repository's CI workflow (.github/workflows/ci.yml, matched by its workflow id and path) was
  triggered by a `push` to a `pre-pr/**` branch of this repository (not a fork), for exactly this head SHA;
- in that run's latest attempt, the job "CI result" completed with conclusion `success`.

A failed, cancelled, skipped or still-running verdict never counts, nor does one for another SHA. It prints the
reused run's id, or nothing when there is none. Any API, git or parsing error prints nothing and a warning: every
job then runs as usual, so an error can cost time but never skip a check.

The head SHA comes from the event payload (github.event.pull_request.head.sha) and the tested SHA from the local
checkout, never from a branch name, title or other text a contributor writes, and the token is the run's own
GITHUB_TOKEN with `actions: read`. Standard library only, so the changes job needs no project install.

CI runs the base branch's copy of this script with the PR's own ci.yml. When the two disagree on arguments (a CLI
change not yet merged), argparse exits 2, the step logs it as an argument mismatch, and everything runs: reuse is
off on that PR, never wrongly on. This copy is not the security boundary: a PR can edit ci.yml to skip its jobs,
so review of those files (CODEOWNERS) and the ruleset that pins the required check to GitHub Actions are what
guard the check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

WORKFLOW_FILE = "ci.yml"
WORKFLOW_PATH = f".github/workflows/{WORKFLOW_FILE}"
AGGREGATE_JOB = "CI result"
PRE_PR_PREFIX = "pre-pr/"
REUSE_BASE = "main"
SHA = re.compile(r"^[0-9a-f]{40}$")
# Owner and name: GitHub's characters, never empty and never the dot segments `.` or `..` (`org/.github` is fine).
REPO = re.compile(r"^(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+$")
PER_PAGE = 100
# A commit pushed to a handful of pre-pr branches has a handful of runs; stop paging long before anything absurd.
MAX_PAGES = 5

GetJson = Callable[[str], Any]
# same_tree(tested, head): True when both commits have exactly the same tree.
SameTree = Callable[[str, str], bool]


@dataclass(frozen=True)
class Reuse:
    """A green pre-pr verdict: the workflow run, its branch, and its "CI result" job."""

    run_id: int
    branch: str
    job_id: int


def find_reusable(
    get_json: GetJson, repo: str, sha: str, base: str, tested: str, same_tree: SameTree
) -> Reuse | None:
    """The newest green pre-pr CI verdict for exactly `sha` in `repo`, or None.

    None, without asking the API, unless the PR is into main and `tested` (the commit this run tests) has
    exactly the tree of `sha`.
    get_json takes an API path with its query (`/repos/...`) and returns the decoded JSON body. Raises ValueError
    on bad arguments; API and git errors propagate to the caller.
    """
    if not REPO.fullmatch(repo):
        raise ValueError(f"not a repository name: {repo!r}")
    for name, value in (("head", sha), ("tested", tested)):
        if not SHA.fullmatch(value):
            raise ValueError(f"not a full lowercase {name} commit SHA: {value!r}")
    if base != REUSE_BASE or not same_tree(tested, sha):
        return None
    workflow_id = _workflow_id(get_json, repo)
    for run in _push_runs(get_json, repo, sha):
        if not _is_pre_pr_run(run, repo, sha, workflow_id):
            continue
        job_id = _green_aggregate_job(get_json, repo, int(run["id"]), sha)
        if job_id is not None:
            return Reuse(run_id=int(run["id"]), branch=str(run["head_branch"]), job_id=job_id)
    return None


def _workflow_id(get_json: GetJson, repo: str) -> int:
    """The numeric id of this repository's CI workflow, so a run of any other workflow never matches."""
    body = get_json(f"/repos/{repo}/actions/workflows/{WORKFLOW_FILE}")
    if body.get("path") != WORKFLOW_PATH or not isinstance(body.get("id"), int):
        raise ValueError(f"unexpected workflow for {WORKFLOW_FILE}: {body.get('path')!r}")
    return int(body["id"])


def _push_runs(get_json: GetJson, repo: str, sha: str) -> list[dict[str, Any]]:
    """This workflow's push runs for `sha`, newest first (the API's order)."""
    runs: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode({"head_sha": sha, "event": "push", "per_page": PER_PAGE, "page": page})
        body = get_json(f"/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs?{query}")
        batch = body.get("workflow_runs") or []
        runs.extend(batch)
        if len(batch) < PER_PAGE:
            break
    return runs


def _is_pre_pr_run(run: dict[str, Any], repo: str, sha: str, workflow_id: int) -> bool:
    """A push run of this repository's CI workflow, on a pre-pr/** branch of this repository, for exactly `sha`.

    The API gives a run's `path` as `.github/workflows/ci.yml` (checked 25 Nov 2026 on run 45582621429); a ref
    suffix (`...ci.yml@refs/heads/x`) is allowed in case it adds one, but the workflow id must match as well.
    """
    return (
        run.get("workflow_id") == workflow_id
        and run.get("event") == "push"
        and run.get("head_sha") == sha
        and str(run.get("head_branch") or "").startswith(PRE_PR_PREFIX)
        and str(run.get("path") or "").split("@", 1)[0] == WORKFLOW_PATH
        and (run.get("repository") or {}).get("full_name") == repo
        and (run.get("head_repository") or {}).get("full_name") == repo
    )


def _green_aggregate_job(get_json: GetJson, repo: str, run_id: int, sha: str) -> int | None:
    """The id of the run's latest-attempt aggregate job when it completed successfully for `sha`, else None."""
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode({"filter": "latest", "per_page": PER_PAGE, "page": page})
        body = get_json(f"/repos/{repo}/actions/runs/{run_id}/jobs?{query}")
        jobs = body.get("jobs") or []
        for job in jobs:
            if (
                job.get("name") == AGGREGATE_JOB
                and job.get("status") == "completed"
                and job.get("conclusion") == "success"
                and job.get("head_sha") == sha
                and job.get("run_id") == run_id
            ):
                return int(job["id"])
        if len(jobs) < PER_PAGE:
            break
    return None


def _git_tree(commit: str) -> str:
    """The tree id of `commit` in the current checkout; any git failure is an error (so: no reuse)."""
    done = subprocess.run(  # noqa: S603 (fixed argv; the commit is a checked full hex SHA)
        ["git", "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}"],  # noqa: S607 (git from PATH, as in CI)
        capture_output=True,
        text=True,
        check=False,
    )
    tree = done.stdout.strip()
    if done.returncode != 0 or not SHA.fullmatch(tree):
        raise RuntimeError(
            f"git rev-parse found no tree for {commit} ({done.returncode}): {done.stderr.strip()}"
        )
    return tree


def git_same_tree(tested: str, head: str) -> bool:
    """True when the two commits have exactly the same tree in the current checkout."""
    return _git_tree(tested) == _git_tree(head)


def github_get_json(api_url: str, token: str) -> GetJson:
    """A GetJson for the GitHub REST API at api_url (https only), authorised with token."""
    if urllib.parse.urlsplit(api_url).scheme != "https":
        raise ValueError(f"the API URL must be https: {api_url!r}")
    base = api_url.rstrip("/")

    def get(path: str) -> Any:
        request = urllib.request.Request(  # noqa: S310 (https only, checked above)
            base + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https only)
            return json.load(response)

    return get


def main(
    argv: list[str] | None = None, get_json: GetJson | None = None, same_tree: SameTree = git_same_tree
) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="OWNER/NAME of this repository (github.repository)")
    ap.add_argument("--sha", required=True, help="the PR's head SHA (github.event.pull_request.head.sha)")
    ap.add_argument("--base", required=True, help="the PR's base branch (github.base_ref)")
    ap.add_argument(
        "--tested-commit",
        required=True,
        help="the commit this run tests (`git rev-parse HEAD`: the merge commit on a pull_request run)",
    )
    a = ap.parse_args(argv)
    try:
        if get_json is None:
            token = os.environ.get("GITHUB_TOKEN", "")
            if not token:
                raise ValueError("GITHUB_TOKEN is not set")
            get_json = github_get_json(os.environ.get("GITHUB_API_URL", "https://api.github.com"), token)
        found = find_reusable(get_json, a.repo, a.sha, a.base, a.tested_commit, same_tree)
    except Exception as e:  # noqa: BLE001 (any failure means: no reuse, run everything)
        print(
            f"::warning::No pre-pr verdict reused, so every job runs: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 0
    if found is None:
        print(
            f"No reusable pre-pr '{AGGREGATE_JOB}' for {a.sha} (tested {a.tested_commit}, base {a.base}: the PR"
            f" is not into {REUSE_BASE}, the tested tree differs from the head's, or no green pre-pr run);"
            " every job runs.",
            file=sys.stderr,
        )
        return 0
    print(
        f"Green '{AGGREGATE_JOB}' in run {found.run_id} ({found.branch}, job {found.job_id}) for {a.sha}.",
        file=sys.stderr,
    )
    print(found.run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
