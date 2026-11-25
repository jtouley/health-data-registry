"""The nightly live publisher checks (.github/workflows/live-checks.yml): run them, retry once, keep one issue.

Usage:
  python scripts/live_checks_nightly.py run [--summary FILE] [--github-output FILE] [--report FILE]
  python scripts/live_checks_nightly.py issue --repo OWNER/NAME --run-url URL --sha SHA --job-result RESULT

run: the `network` tests in tests/integration (what `make test-network` runs), in a child pytest that records each
failed check's test id and the one-line message pytest gives for it. A failed check is run once more, and only
once; one that passes then is listed as "passed on retry" (network flakiness) and does not fail the run. A module
that cannot be collected is a failed check too, and the others still run. HDR_LAKE points at a new, empty
temporary directory; the run fails if the checks wrote anything there or changed the checkout (`git status`), since
live checks fetch at test time and write nothing (hard rule 5). The report (counts, failed ids and messages, the
ids that passed on retry) goes to the job summary and, as one line of JSON, to the step output `report`. Exit 1
when any check failed.

issue: one tracking issue, found by its label `live-checks-failing` among the open issues github-actions opened (a
title alone never identifies it). When the live job failed, or ended without a report, the issue is opened, or
its body is replaced with this run's failures and a comment links the run. When the job passed, every such issue
is closed with a comment linking the green run. Test ids and messages only: never a response body or a row.
Messages are cut to one line of at most MESSAGE_MAX characters and shown as code, so they cannot mention anyone
or render as markup.

The token is the run's own GITHUB_TOKEN (scripts/_github_api.py: https only, never sent on a redirect).
Standard library only, apart from pytest in the child run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A sibling script: Python puts this script's own directory (scripts/) first on sys.path.
import _github_api

ROOT = Path(__file__).resolve().parents[1]
TESTS = "tests/integration"
MARKER = "network"
LABEL = "live-checks-failing"
LABEL_COLOR = "b60205"
TITLE = "Live publisher checks failing"
BOT = "github-actions[bot]"
MESSAGE_MAX = 200
MAX_LISTED = 50
PER_PAGE = 100
SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+$")
RUN_URL = re.compile(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+$")
JOB_RESULTS = ("success", "failure", "cancelled", "skipped")
# Ids for failures that are not one test: the run itself, and the write check.
RUN_ID = "(pytest run)"
WRITES_ID = "(writes check)"
EXIT_MEANING = {
    2: "pytest was interrupted",
    3: "pytest hit an internal error",
    4: "pytest was called wrongly",
    5: "no live check was collected",
}


def one_line(text: object, limit: int = MESSAGE_MAX) -> str:
    """One line, no control characters or backticks, at most `limit` characters."""
    flat = " ".join(re.sub(r"[\x00-\x1f\x7f`]", " ", str(text)).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# the child pytest ------------------------------------------------------------------------------------------------


def crash_message(report: Any) -> str:
    """pytest's one-line reason for a failure: the crash line, else the last line of the report."""
    longrepr = getattr(report, "longrepr", None)
    crash = getattr(longrepr, "reprcrash", None)
    if crash is not None and getattr(crash, "message", ""):
        return one_line(str(crash.message).strip().splitlines()[0])
    lines = [ln.strip() for ln in str(longrepr or "").splitlines() if ln.strip()]
    return one_line(lines[-1].removeprefix("E").strip() if lines else "failed with no message")


class Recorder:
    """A pytest plugin: how many checks were collected, and each failure's id and one-line message."""

    def __init__(self) -> None:
        self.collected = 0
        self.failed: dict[str, str] = {}

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = len(session.items)

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            self.failed.setdefault(report.nodeid or RUN_ID, crash_message(report))

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.failed:
            self.failed.setdefault(report.nodeid, crash_message(report))


def child(results: Path, args: list[str]) -> int:
    """Run pytest in this process with the recorder, and write what it saw to `results`."""
    import pytest

    rec = Recorder()
    code = int(pytest.main(args, plugins=[rec]))
    results.write_text(json.dumps({"exit": code, "collected": rec.collected, "failed": rec.failed}), "utf-8")
    return code


# run -------------------------------------------------------------------------------------------------------------


@dataclass
class Attempt:
    exit: int
    collected: int
    failed: dict[str, str]


@dataclass
class Report:
    collected: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    passed_on_retry: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        failed = sorted(self.failed.items())
        return {
            "collected": self.collected,
            "failed": [{"id": one_line(i, 300), "message": one_line(m)} for i, m in failed[:MAX_LISTED]],
            "failed_total": len(failed),
            "passed_on_retry": [one_line(i, 300) for i in sorted(self.passed_on_retry)[:MAX_LISTED]],
        }


def attempt(root: Path, lake: Path, tmp: Path, name: str, selection: list[str]) -> Attempt:
    """One child pytest over `selection` (paths or test ids), with the `network` marker only."""
    results = tmp / f"{name}.json"
    env = {**os.environ, "HDR_LAKE": str(lake), "PYTHONDONTWRITEBYTECODE": "1"}
    args = [
        "-m",
        MARKER,
        "-q",
        "-p",
        "no:cacheprovider",
        "--continue-on-collection-errors",
        *selection,
    ]
    cmd = [sys.executable, str(Path(__file__).resolve()), "child", "--results", str(results), "--", *args]
    done = subprocess.run(cmd, cwd=root, env=env, check=False)
    try:
        data = json.loads(results.read_text("utf-8"))
        return Attempt(
            int(data["exit"]), int(data["collected"]), {str(k): str(v) for k, v in data["failed"].items()}
        )
    except (OSError, ValueError, KeyError, TypeError):
        return Attempt(
            done.returncode, 0, {RUN_ID: f"pytest ended with exit code {done.returncode} and no results"}
        )


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def _git_status(root: Path) -> set[str]:
    out = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    return {line for line in out.splitlines() if line.strip()}


def _run_failure(a: Attempt) -> str | None:
    """Why a whole attempt failed beyond its named checks, or None."""
    if a.exit in EXIT_MEANING:
        return f"{EXIT_MEANING[a.exit]} (pytest exit code {a.exit})"
    if a.exit == 1 and not a.failed:
        return "pytest reported a failure but named no check"
    if a.exit not in (0, 1):
        return f"pytest ended with exit code {a.exit}"
    return None


def run_checks(root: Path = ROOT, tests: str = TESTS) -> Report:
    """Run the live checks, retry each failed one once, and check nothing was written."""
    before = _git_status(root)
    with tempfile.TemporaryDirectory(prefix="live-checks-") as tmp_name:
        tmp = Path(tmp_name)
        lake = tmp / "lake"
        lake.mkdir()
        first = attempt(root, lake, tmp, "first", [tests])
        report = Report(collected=first.collected)
        why = first.failed.get(RUN_ID) or _run_failure(first)
        if why:
            report.failed[RUN_ID] = why
        failed = {i: m for i, m in first.failed.items() if i != RUN_ID}
        if failed:
            # The one retry, of the failed checks only.
            second = attempt(root, lake, tmp, "retry", sorted(failed))
            again = second.failed.get(RUN_ID) or _run_failure(second)
            for test_id, message in failed.items():
                if again or test_id in second.failed:
                    report.failed[test_id] = second.failed.get(test_id, message)
                else:
                    report.passed_on_retry.append(test_id)
            # A module that failed to collect the first time runs its checks now: their failures count too.
            for test_id, message in second.failed.items():
                if test_id not in failed and test_id != RUN_ID:
                    report.failed[test_id] = message
            if again:
                report.failed.setdefault(RUN_ID, f"the retry: {again}")
        written = sorted(str(p.relative_to(lake)) for p in lake.rglob("*"))
    changed = sorted(_git_status(root) - before)
    if written or changed:
        listed = [f"lake: {p}" for p in written[:5]] + [f"checkout: {c.strip()}" for c in changed[:5]]
        report.failed[WRITES_ID] = "the live checks wrote files, which they must never do: " + "; ".join(
            listed
        )
    return report


def summary(report: Report) -> str:
    data = report.as_json()
    lines = [
        "## Live publisher checks",
        "",
        f"{data['collected']} live checks collected; {data['failed_total']} failed. Each failed check is retried"
        " once, at most, so a passing retry (network flakiness) does not fail the run.",
        "",
    ]
    if data["failed"]:
        lines += ["**Failed** (after one retry):", ""]
        lines += [f"- `{f['id']}`: `{f['message']}`" for f in data["failed"]]
        lines.append("")
    if data["passed_on_retry"]:
        lines += ["**Passed on retry** (failed once, then passed):", ""]
        lines += [f"- `{i}`" for i in data["passed_on_retry"]]
        lines.append("")
    if not data["failed"] and not data["passed_on_retry"]:
        lines += ["Every live check passed on the first try.", ""]
    return "\n".join(lines)


# issue -----------------------------------------------------------------------------------------------------------


def parse_report(raw: str) -> dict[str, Any] | None:
    """The live job's report, cleaned again here (it reached this job as a step output), or None."""
    try:
        data = json.loads(raw)
        failed = [
            {"id": one_line(f["id"], 300), "message": one_line(f["message"])}
            for f in data["failed"][:MAX_LISTED]
        ]
        return {
            "collected": int(data["collected"]),
            "failed": failed,
            "failed_total": max(int(data["failed_total"]), len(failed)),
            "passed_on_retry": [one_line(i, 300) for i in data["passed_on_retry"][:MAX_LISTED]],
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def issue_body(report: dict[str, Any] | None, job_result: str, run_url: str, sha: str) -> str:
    if report is None or (not report["failed"] and job_result != "success"):
        listed = (
            f"The live job ended '{job_result}' before it reported which checks failed (a timeout, a cancelled run"
            " or a broken install). Read the run log."
        )
    else:
        rows = "\n".join(f"- `{f['id']}`: `{f['message']}`" for f in report["failed"])
        more = report["failed_total"] - len(report["failed"])
        listed = (
            f"Failed after one retry ({report['failed_total']} of {report['collected']} checks):\n\n{rows}"
        )
        if more > 0:
            listed += f"\n- and {more} more: see the run"
        if report["passed_on_retry"]:
            flaky = ", ".join(f"`{i}`" for i in report["passed_on_retry"])
            listed += f"\n\nPassed on retry (not counted as failed): {flaky}"
    return (
        f"The nightly live publisher checks failed on {sha[:12]}: {run_url}\n\n"
        f"{listed}\n\n"
        "A publisher may have renamed a field, added a code or moved a file. Reproduce with `make test-network`,"
        " then update the adapter, catalog entry or contract in a pull request. This issue closes itself on the"
        " next green run.\n\n"
        f"---\n_Opened by the nightly live checks (.github/workflows/live-checks.yml), label `{LABEL}`._"
    )


def tracking_issues(get: _github_api.GetJson, repo: str) -> list[dict[str, Any]]:
    """Open issues with this workflow's label that github-actions opened; oldest first."""
    query = urllib.parse.urlencode(
        {
            "labels": LABEL,
            "state": "open",
            "creator": BOT,
            "per_page": PER_PAGE,
            "sort": "created",
            "direction": "asc",
        }
    )
    found = get(f"/repos/{repo}/issues?{query}") or []
    return [
        i
        for i in found
        if "pull_request" not in i
        and (i.get("user") or {}).get("login") == BOT
        and LABEL in {str((label or {}).get("name")) for label in i.get("labels") or []}
    ]


def ensure_label(send: _github_api.SendJson, repo: str) -> None:
    body = {
        "name": LABEL,
        "color": LABEL_COLOR,
        "description": "The nightly live publisher checks are failing",
    }
    try:
        send("POST", f"/repos/{repo}/labels", body)
    except urllib.error.HTTPError as e:
        if e.code != 422:  # 422: the label exists already
            raise


def sync_issue(
    get: _github_api.GetJson,
    send: _github_api.SendJson,
    repo: str,
    job_result: str,
    report: dict[str, Any] | None,
    run_url: str,
    sha: str,
) -> list[str]:
    """Open, update or close the tracking issue; one line per action."""
    if job_result == "skipped":
        return ["The live job was skipped: no issue changed."]
    issues = tracking_issues(get, repo)
    green = job_result == "success" and report is not None and not report["failed"]
    if green:
        done = []
        for i in issues:
            number = int(i["number"])
            send(
                "POST",
                f"/repos/{repo}/issues/{number}/comments",
                {"body": f"Green again: {run_url}. Closing."},
            )
            send("PATCH", f"/repos/{repo}/issues/{number}", {"state": "closed", "state_reason": "completed"})
            done.append(f"closed #{number}")
        return done or ["Every live check passed; no tracking issue was open."]
    body = issue_body(report, job_result, run_url, sha)
    if issues:
        number = int(issues[0]["number"])
        send("PATCH", f"/repos/{repo}/issues/{number}", {"body": body})
        send("POST", f"/repos/{repo}/issues/{number}/comments", {"body": f"Failed again: {run_url}"})
        return [f"updated #{number}"]
    ensure_label(send, repo)
    made = send("POST", f"/repos/{repo}/issues", {"title": TITLE, "body": body, "labels": [LABEL]})
    return [f"opened #{(made or {}).get('number', '?')}"]


# main ------------------------------------------------------------------------------------------------------------


def main(
    argv: list[str] | None = None,
    get: _github_api.GetJson | None = None,
    send: _github_api.SendJson | None = None,
) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["child"]:
        if len(argv) < 4 or argv[1] != "--results" or argv[3] != "--":
            print("usage: live_checks_nightly.py child --results FILE -- PYTEST_ARGS", file=sys.stderr)
            return 4
        return child(Path(argv[2]), argv[4:])
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--summary", type=Path, help="append the Markdown summary here ($GITHUB_STEP_SUMMARY)")
    r.add_argument("--github-output", type=Path, help="append report=<json> here ($GITHUB_OUTPUT)")
    r.add_argument("--report", type=Path, help="also write the report JSON here")
    i = sub.add_parser("issue")
    i.add_argument("--repo", required=True)
    i.add_argument("--run-url", required=True)
    i.add_argument("--sha", required=True)
    i.add_argument("--job-result", required=True, choices=JOB_RESULTS)
    i.add_argument("--report-env", default="LIVE_REPORT", help="the environment variable holding the report")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        report = run_checks()
        line = json.dumps(report.as_json(), separators=(",", ":"))
        text = summary(report)
        print(text)
        if a.summary:
            with a.summary.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
        if a.github_output:
            with a.github_output.open("a", encoding="utf-8") as f:
                f.write(f"report={line}\n")
        if a.report:
            a.report.write_text(line + "\n", encoding="utf-8")
        return 1 if report.failed else 0
    if not REPO.fullmatch(a.repo) or not RUN_URL.fullmatch(a.run_url) or not SHA.fullmatch(a.sha):
        ap.error("--repo must be OWNER/NAME, --run-url an Actions run URL and --sha a full commit SHA")
    try:
        if get is None or send is None:
            token = os.environ.get("GITHUB_TOKEN", "")
            if not token:
                raise ValueError("GITHUB_TOKEN is not set")
            get, send = _github_api.get_json(token), _github_api.send_json(token)
        report_data = parse_report(os.environ.get(a.report_env, ""))
        print("\n".join(sync_issue(get, send, a.repo, a.job_result, report_data, a.run_url, a.sha)))
        return 0
    except Exception as e:  # fail in plain words, never a traceback
        print(f"::error::The tracking issue could not be updated ({type(e).__name__}: {one_line(e)}).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
