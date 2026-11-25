"""CI time budgets (ci/budgets.yml): check a run's jobs against them, and keep them from loosening quietly.

Usage:
  python scripts/ci_budget.py check --repo OWNER/NAME --run-id ID --profile pr|full [--skip NAME]
  python scripts/ci_budget.py ratchet --base MERGE_BASE

check: reads the run's jobs (latest attempt) from the Actions API with the run's own GITHUB_TOKEN (actions: read),
and fails when a completed job ran longer than its budget, or has no budget, listing each slow job with its time.
Queue time (started minus created) is printed, never counted: it measures the runners, not the job.

ratchet: fails when ci/budgets.yml loosens a budget (a larger number, or a budget taken away) compared with the
merge base, unless the change since the merge base touches ci/budgets.yml alone: loosening a budget is a pull
request of its own, for the maintainer to approve.

Standard library only: ci/budgets.yml is JSON inside, after its comment lines.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BUDGETS = "ci/budgets.yml"
SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+$")


def parse_budgets(text: str) -> dict[str, Any]:
    """The budgets: the file without its `#` comment lines, read as JSON."""
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    data: dict[str, Any] = json.loads(body)
    for profile in data.values():
        if not isinstance(profile.get("run"), int) or not all(
            isinstance(v, int) for v in profile["jobs"].values()
        ):
            raise ValueError("every budget is a whole number of seconds")
    return data


def budget_for(name: str, jobs: dict[str, int]) -> int | None:
    """The budget of a job: its exact name, else the first pattern (`Mutation tests (*)`) that matches."""
    if name in jobs:
        return jobs[name]
    return next((v for k, v in jobs.items() if "*" in k and fnmatch.fnmatchcase(name, k)), None)


def _seconds(start: str, end: str) -> float:
    def t(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    return (t(end) - t(start)).total_seconds()


def over_budget(
    jobs: list[dict[str, Any]], budgets: dict[str, int], skip: set[str]
) -> tuple[list[str], list[str]]:
    """(slow, report): one line per completed job that ran over its budget or has none, and one report line per
    job. Skipped jobs, jobs still running and jobs in `skip` are not judged."""
    slow, report = [], []
    for job in jobs:
        name = str(job.get("name"))
        if name in skip or job.get("status") != "completed" or job.get("conclusion") == "skipped":
            continue
        if not job.get("started_at") or not job.get("completed_at"):
            continue
        ran = _seconds(job["started_at"], job["completed_at"])
        queued = _seconds(job["created_at"], job["started_at"]) if job.get("created_at") else 0.0
        budget = budget_for(name, budgets)
        report.append(
            f"{name}: ran {ran:.0f} s (budget {budget if budget is not None else 'none'}), queued {queued:.0f} s"
        )
        if budget is None:
            slow.append(f"{name}: ran {ran:.0f} s and has no budget in {BUDGETS}")
        elif ran > budget:
            slow.append(f"{name}: ran {ran:.0f} s, over its {budget} s budget")
    return slow, report


def _get_jobs(repo: str, run_id: int, token: str) -> list[dict[str, Any]]:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if urllib.parse.urlsplit(api).scheme != "https":
        raise ValueError(f"the API URL must be https: {api!r}")
    jobs: list[dict[str, Any]] = []
    for page in range(1, 6):
        url = f"{api}/repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}"
        request = urllib.request.Request(  # noqa: S310 (https only, checked above)
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https only)
            batch = json.load(response).get("jobs") or []
        jobs += batch
        if len(batch) < 100:
            break
    return jobs


def loosened(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Each budget in `new` that is looser than in `old`: a larger number, or a budget taken away."""
    out = []
    for profile, o in old.items():
        n = new.get(profile)
        if n is None:
            out.append(f"{profile}: the whole profile was taken away")
            continue
        if n["run"] > o["run"]:
            out.append(f"{profile}.run: {o['run']} -> {n['run']} s")
        for job, seconds in o["jobs"].items():
            if job not in n["jobs"]:
                out.append(f"{profile}.jobs[{job!r}]: the budget was taken away")
            elif n["jobs"][job] > seconds:
                out.append(f"{profile}.jobs[{job!r}]: {seconds} -> {n['jobs'][job]} s")
    return out


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603,S607


def ratchet(base: str) -> tuple[bool, str]:
    if not SHA.fullmatch(base):
        return False, f"not a full commit SHA: {base!r}"
    shown = _git("show", f"{base}:{BUDGETS}")
    if shown.returncode != 0:
        return True, f"{BUDGETS} is new since the merge base; nothing to compare with."
    looser = loosened(
        parse_budgets(shown.stdout), parse_budgets((ROOT / BUDGETS).read_text(encoding="utf-8"))
    )
    if not looser:
        return True, "No budget loosened."
    changed = set(_git("diff", "--name-only", "--no-renames", base, "--").stdout.split())
    if changed == {BUDGETS}:
        return (
            True,
            "Budgets loosened in a change of their own (for the maintainer to approve): " + "; ".join(looser),
        )
    return False, (
        "These budgets loosen: "
        + "; ".join(looser)
        + f". Loosen them in a pull request that changes {BUDGETS}"
        " alone, so the maintainer approves just that; or make the jobs fit."
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--repo", required=True)
    c.add_argument("--run-id", type=int, required=True)
    c.add_argument("--profile", choices=["pr", "full"], required=True)
    c.add_argument("--skip", action="append", default=[], help="a job not to judge (the one running this)")
    r = sub.add_parser("ratchet")
    r.add_argument("--base", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "ratchet":
        ok, message = ratchet(a.base)
        print(message)
        return 0 if ok else 1
    if not REPO.fullmatch(a.repo):
        ap.error(f"not a repository name: {a.repo!r}")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("GITHUB_TOKEN is not set, so the job times cannot be read. Failing.")
        return 1
    budgets = parse_budgets((ROOT / BUDGETS).read_text(encoding="utf-8"))[a.profile]["jobs"]
    slow, report = over_budget(_get_jobs(a.repo, a.run_id, token), budgets, set(a.skip))
    print("\n".join(report))
    if slow:
        print(f"\nOver budget ({BUDGETS}, profile {a.profile}):")
        print("\n".join(f"  {s}" for s in slow))
        return 1
    print(f"Every job fits its budget ({BUDGETS}, profile {a.profile}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
