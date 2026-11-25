"""CI time budgets (ci/budgets.yml): check a run's jobs against them, and keep them from loosening quietly.

Usage:
  python scripts/ci_budget.py check --repo OWNER/NAME --run-id ID --profile pr|full [--skip NAME] [--warn-only]
  python scripts/ci_budget.py ratchet --base MERGE_BASE

check: reads the run's jobs (latest attempt) from the Actions API with the run's own GITHUB_TOKEN (actions: read).
It fails when a completed job ran longer than its budget or has no budget, or when the run's running time ran
over the profile's `run` budget, and lists each slow job with its time. The run's running time is the critical
path: the longest "Changes" job plus the longest of the jobs after it, so queue time (started minus created) is
printed, never counted: it measures the runners, not the change. With --warn-only (every run but a pull
request's or a pre-pr push's: the merge queue, main, nightly) it reports the same lines as warnings and passes:
runner speed is not a reason to block a merge. When the job times cannot be read (an API error, no token) it warns
and passes in either mode: the budget is unknown, not exceeded.

ratchet: fails when ci/budgets.yml loosens any job's effective budget (budget_for: its exact name, else the first
matching pattern) compared with the merge base: a larger number, a budget taken away, or a new key or a
reordered pattern that gives a job a larger budget than it had. It is waived only when the change since the
merge base touches ci/budgets.yml alone: loosening a budget is a pull request of its own, for the maintainer to
approve.

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
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _github_api  # noqa: E402 - a sibling script, found through the line above

ROOT = Path(__file__).resolve().parents[1]
BUDGETS = "ci/budgets.yml"
SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+$")
# The jobs every other job waits for: the first stage of the critical path.
FIRST_STAGE = frozenset({"Changes"})


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
    """The budget of a job: its exact name, else the first pattern (`Tests (Python *)`) that matches."""
    if name in jobs:
        return jobs[name]
    return next((v for k, v in jobs.items() if "*" in k and fnmatch.fnmatchcase(name, k)), None)


def _seconds(start: str, end: str) -> float:
    def t(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    return (t(end) - t(start)).total_seconds()


def _judged(jobs: list[dict[str, Any]], skip: set[str]) -> list[tuple[str, float, float]]:
    """(name, ran, queued) of each completed, not skipped job with both times, outside `skip`."""
    out = []
    for job in jobs:
        name = str(job.get("name"))
        if name in skip or job.get("status") != "completed" or job.get("conclusion") == "skipped":
            continue
        if not job.get("started_at") or not job.get("completed_at"):
            continue
        ran = _seconds(job["started_at"], job["completed_at"])
        queued = _seconds(job["created_at"], job["started_at"]) if job.get("created_at") else 0.0
        out.append((name, ran, queued))
    return out


def critical_path(jobs: list[dict[str, Any]], skip: set[str]) -> float:
    """The run's running time: the longest first-stage job (Changes) plus the longest of the others, which all
    wait for it. Queue time is left out."""
    judged = _judged(jobs, skip)
    first = max((ran for name, ran, _ in judged if name in FIRST_STAGE), default=0.0)
    rest = max((ran for name, ran, _ in judged if name not in FIRST_STAGE), default=0.0)
    return first + rest


def over_budget(
    jobs: list[dict[str, Any]], budgets: dict[str, int], skip: set[str], run_budget: int | None = None
) -> tuple[list[str], list[str]]:
    """(slow, report): one line per completed job that ran over its budget or has none, plus one when the run's
    critical path ran over `run_budget`; and one report line per job. Skipped jobs, jobs still running and jobs in
    `skip` are not judged."""
    slow, report = [], []
    for name, ran, queued in _judged(jobs, skip):
        budget = budget_for(name, budgets)
        report.append(
            f"{name}: ran {ran:.0f} s (budget {budget if budget is not None else 'none'}), queued {queued:.0f} s"
        )
        if budget is None:
            slow.append(f"{name}: ran {ran:.0f} s and has no budget in {BUDGETS}")
        elif ran > budget:
            slow.append(f"{name}: ran {ran:.0f} s, over its {budget} s budget")
    if run_budget is not None:
        path = critical_path(jobs, skip)
        report.append(f"Run: {path:.0f} s of running time on the critical path (budget {run_budget})")
        if path > run_budget:
            slow.append(f"the run: {path:.0f} s on the critical path, over its {run_budget} s budget")
    return slow, report


def get_jobs(repo: str, run_id: int, get: _github_api.GetJson) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for page in range(1, 6):
        batch = get(f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}")
        batch = (batch or {}).get("jobs") or []
        jobs += batch
        if len(batch) < 100:
            break
    return jobs


def _sample(pattern: str) -> str:
    """A job name a pattern matches, to ask what budget another file gives that job."""
    return pattern.replace("*", "x").replace("?", "x")


def loosened(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Each way `new` is looser than `old`: a larger run budget, or a job whose effective budget (budget_for) grew
    or went away. The jobs asked about are every key of either file, and for each pattern a name it matches, so a
    new exact key or a broader pattern placed ahead of an old one is caught too."""
    out = []
    for profile, o in old.items():
        n = new.get(profile)
        if n is None:
            out.append(f"{profile}: the whole profile was taken away")
            continue
        if n["run"] > o["run"]:
            out.append(f"{profile}.run: {o['run']} -> {n['run']} s")
        keys = [*o["jobs"], *(k for k in n["jobs"] if k not in o["jobs"])]
        names = dict.fromkeys(name for k in keys for name in (k, _sample(k)) if name)
        for job in names:
            before, after = budget_for(job, o["jobs"]), budget_for(job, n["jobs"])
            if before is None:
                continue  # no budget before: the check failed it, so any budget now is tighter
            if after is None:
                out.append(f"{profile}.jobs[{job!r}]: the budget was taken away")
            elif after > before:
                out.append(f"{profile}.jobs[{job!r}]: {before} -> {after} s")
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


def check(
    repo: str,
    run_id: int,
    profile: str,
    skip: set[str],
    warn_only: bool,
    get: Callable[[str], Any] | None = None,
) -> int:
    data = parse_budgets((ROOT / BUDGETS).read_text(encoding="utf-8"))[profile]
    try:
        if get is None:
            token = os.environ.get("GITHUB_TOKEN", "")
            if not token:
                raise ValueError("GITHUB_TOKEN is not set")
            get = _github_api.get_json(token)
        jobs = get_jobs(repo, run_id, get)
    except Exception as e:  # noqa: BLE001 (the budget is unknown, not exceeded: warn, never fail)
        print(f"::warning title=CI budget::The job times could not be read ({type(e).__name__}: {e}).")
        return 0
    slow, report = over_budget(jobs, data["jobs"], skip, data["run"])
    print("\n".join(report))
    if not slow:
        print(f"Every job, and the run, fits its budget ({BUDGETS}, profile {profile}).")
        return 0
    if warn_only:
        for s in slow:
            print(f"::warning title=Over its CI budget::{s} ({BUDGETS}, profile {profile})")
        return 0
    print(f"\nOver budget ({BUDGETS}, profile {profile}):")
    print("\n".join(f"  {s}" for s in slow))
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--repo", required=True)
    c.add_argument("--run-id", type=int, required=True)
    c.add_argument("--profile", choices=["pr", "full"], required=True)
    c.add_argument("--skip", action="append", default=[], help="a job not to judge (the one running this)")
    c.add_argument("--warn-only", action="store_true", help="report slow jobs as warnings and pass")
    r = sub.add_parser("ratchet")
    r.add_argument("--base", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "ratchet":
        ok, message = ratchet(a.base)
        print(message)
        return 0 if ok else 1
    if not REPO.fullmatch(a.repo):
        ap.error(f"not a repository name: {a.repo!r}")
    return check(a.repo, a.run_id, a.profile, set(a.skip), a.warn_only)


if __name__ == "__main__":
    sys.exit(main())
