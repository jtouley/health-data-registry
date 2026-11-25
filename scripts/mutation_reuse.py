"""Find a green pre-pr mutation verdict for a pull request's head commit, so CI need not mutate it twice.

Usage (CI, pull_request events only; see the mutation-list job in .github/workflows/ci.yml):
  python scripts/mutation_reuse.py --repo OWNER/NAME --sha HEAD_SHA --base BASE_REF

The pre-PR gate (docs/dev/README.md#the-pre-pr-gate) runs CI on `pre-pr/<branch>` and then on the PR, for the same
commit. The mutation shards are by far the dearest part of a run, so the PR's run reuses the pre-pr verdict when,
and only when, all of these hold:

- the PR targets main (a pre-pr run lists the modules changed since origin/main, so another base changes the list);
- a run of this repository's CI workflow (.github/workflows/ci.yml) was triggered by a `push` to a `pre-pr/**`
  branch of this repository (not a fork), for exactly this head SHA;
- in that run's latest attempt, the job "Mutation tests (changed modules)" completed with conclusion `success`.

A failed, cancelled, skipped or still-running verdict never counts, nor does one for another SHA. It prints the
reused run's id, or nothing when there is none. Any API or parsing error prints nothing and a warning: the shards
then run as usual, so an error can cost time but never skip a check.

The head SHA comes from the event payload (github.event.pull_request.head.sha), never from a branch name, title or
other text a contributor writes, and the token is the run's own GITHUB_TOKEN with `actions: read`. Standard
library only, so the list job needs no project install.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

WORKFLOW_FILE = "ci.yml"
WORKFLOW_PATH = f".github/workflows/{WORKFLOW_FILE}"
AGGREGATE_JOB = "Mutation tests (changed modules)"
PRE_PR_PREFIX = "pre-pr/"
REUSE_BASE = "main"
SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PER_PAGE = 100
# A commit pushed to a handful of pre-pr branches has a handful of runs; stop paging long before anything absurd.
MAX_PAGES = 5

GetJson = Callable[[str], Any]


@dataclass(frozen=True)
class Reuse:
    """A green pre-pr verdict: the workflow run, its branch, and the aggregate job's check run."""

    run_id: int
    branch: str
    job_id: int


def find_reusable(get_json: GetJson, repo: str, sha: str, base: str) -> Reuse | None:
    """The newest green pre-pr mutation verdict for exactly `sha` in `repo`, or None.

    get_json takes an API path with its query (`/repos/...`) and returns the decoded JSON body. Raises ValueError
    on bad arguments; API errors propagate to the caller.
    """
    if not REPO.fullmatch(repo):
        raise ValueError(f"not a repository name: {repo!r}")
    if not SHA.fullmatch(sha):
        raise ValueError(f"not a full lowercase commit SHA: {sha!r}")
    if base != REUSE_BASE:
        return None
    for run in _push_runs(get_json, repo, sha):
        if not _is_pre_pr_run(run, repo, sha):
            continue
        job_id = _green_aggregate_job(get_json, repo, int(run["id"]), sha)
        if job_id is not None:
            return Reuse(run_id=int(run["id"]), branch=str(run["head_branch"]), job_id=job_id)
    return None


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


def _is_pre_pr_run(run: dict[str, Any], repo: str, sha: str) -> bool:
    """A push run of this repository's CI workflow, on a pre-pr/** branch of this repository, for exactly `sha`."""
    return (
        run.get("event") == "push"
        and run.get("head_sha") == sha
        and str(run.get("head_branch") or "").startswith(PRE_PR_PREFIX)
        and run.get("path") == WORKFLOW_PATH
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


def main(argv: list[str] | None = None, get_json: GetJson | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="OWNER/NAME of this repository (github.repository)")
    ap.add_argument("--sha", required=True, help="the PR's head SHA (github.event.pull_request.head.sha)")
    ap.add_argument("--base", required=True, help="the PR's base branch (github.base_ref)")
    a = ap.parse_args(argv)
    try:
        if get_json is None:
            token = os.environ.get("GITHUB_TOKEN", "")
            if not token:
                raise ValueError("GITHUB_TOKEN is not set")
            get_json = github_get_json(os.environ.get("GITHUB_API_URL", "https://api.github.com"), token)
        found = find_reusable(get_json, a.repo, a.sha, a.base)
    except Exception as e:  # noqa: BLE001 (any failure means: no reuse, run the shards)
        print(
            f"::warning::No pre-pr mutation verdict reused, so the shards run: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 0
    if found is None:
        print(
            f"No green pre-pr '{AGGREGATE_JOB}' run for {a.sha} on base {a.base}; the shards run.",
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
