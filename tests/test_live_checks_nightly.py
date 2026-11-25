"""The nightly live publisher checks: .github/workflows/live-checks.yml and scripts/live_checks_nightly.py.

The workflow's safety rules are the ones a later edit could drop without any run failing: scheduled and manual
triggers only, a read-only token except for the one job that writes the tracking issue, no secrets, actions pinned
by commit sha, checkouts without credentials, a timeout on every job and a concurrency group that never cancels.
The script is run on tiny synthetic checks in a temporary git repository: nothing here reaches a publisher.
"""

from __future__ import annotations

import email.message
import importlib.util
import json
import re
import sys
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "live-checks.yml"
REPO = "owner/repo"
SHA = "a" * 40
RUN_URL = "https://github.com/owner/repo/actions/runs/42"
BOT = "github-actions[bot]"


@pytest.fixture
def lc(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "live_checks_nightly", ROOT / "scripts" / "live_checks_nightly.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "live_checks_nightly", mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def wf() -> dict[Any, Any]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def steps(wf: dict[Any, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(job, s) for job, j in wf["jobs"].items() for s in j.get("steps", [])]


# the workflow ----------------------------------------------------------------------------------------------------


def test_nightly_and_manual_only(wf: dict[Any, Any]) -> None:
    on = wf[True]  # YAML reads the key `on` as true
    assert set(on) == {"schedule", "workflow_dispatch"}
    assert len(on["schedule"]) == 1 and on["schedule"][0]["cron"].split()[2:] == ["*", "*", "*"]
    assert on["workflow_dispatch"] in (None, {})


def test_least_privilege_and_only_the_report_job_writes(wf: dict[Any, Any]) -> None:
    assert wf["permissions"] == {"contents": "read"}
    assert {job: j.get("permissions") for job, j in wf["jobs"].items()} == {
        "live": None,
        "report": {"contents": "read", "issues": "write"},
    }
    report = wf["jobs"]["report"]
    assert report["needs"] == "live"
    assert report["if"] == "always() && github.ref == 'refs/heads/main'"


def test_no_secrets_no_environment_and_the_token_only_on_the_issue_step(wf: dict[Any, Any]) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert all("environment" not in j for j in wf["jobs"].values())
    with_token = [(job, s.get("name")) for job, s in steps(wf) if "github.token" in yaml.safe_dump(s)]
    assert with_token == [("report", "Sync the tracking issue")]
    for job, j in wf["jobs"].items():
        assert "env" not in j, f"{job}: no job-wide env"


def test_pinned_actions_checkouts_without_credentials_and_no_cache(wf: dict[Any, Any]) -> None:
    for job, s in steps(wf):
        uses = s.get("uses")
        if not uses:
            continue
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", uses), (job, uses)
        if uses.startswith("actions/checkout@"):
            assert s["with"]["persist-credentials"] is False, job
        if uses.startswith("astral-sh/setup-uv@"):
            assert s["with"]["enable-cache"] is False, job
    text = WORKFLOW.read_text(encoding="utf-8")
    for banned in ("upload-artifact", "actions/cache", "pull_request_target", "set -x", "continue-on-error"):
        assert banned not in text, banned


def test_the_repo_pins_are_reused(wf: dict[Any, Any]) -> None:
    others = "".join(
        p.read_text("utf-8") for p in (ROOT / ".github" / "workflows").glob("*.yml") if p != WORKFLOW
    )
    for _, s in steps(wf):
        if s.get("uses"):
            assert s["uses"] in others, s["uses"]


def test_every_job_has_a_timeout_and_the_group_never_cancels(wf: dict[Any, Any]) -> None:
    assert wf["concurrency"] == {"group": "live-checks", "cancel-in-progress": False}
    for job, j in wf["jobs"].items():
        assert 0 < int(j["timeout-minutes"]) <= 30, job


def test_values_reach_the_shell_only_through_env(wf: dict[Any, Any]) -> None:
    for job, s in steps(wf):
        assert "${{" not in s.get("run", ""), (job, s.get("name"))


def test_the_live_job_runs_the_script_and_the_report_job_installs_nothing(wf: dict[Any, Any]) -> None:
    live = [s["run"] for s in wf["jobs"]["live"]["steps"] if "run" in s]
    assert live[-1].startswith("uv run --no-sync python scripts/live_checks_nightly.py run ")
    assert wf["jobs"]["live"]["outputs"] == {"report": "${{ steps.checks.outputs.report }}"}
    report = [s["run"] for s in wf["jobs"]["report"]["steps"] if "run" in s]
    assert report == [
        'python3 scripts/live_checks_nightly.py issue --repo "${REPO}" --run-url "${RUN_URL}" --sha "${SHA}"'
        ' --job-result "${LIVE_RESULT}"'
    ]
    env = wf["jobs"]["report"]["steps"][-1]["env"]
    assert env["LIVE_RESULT"] == "${{ needs.live.result }}"
    assert env["LIVE_REPORT"] == "${{ needs.live.outputs.report }}"


def test_the_live_checks_are_kept_enabled() -> None:
    drift = yaml.safe_load((ROOT / ".github" / "workflows" / "drift-check.yml").read_text("utf-8"))
    run = drift["jobs"]["keep-alive"]["steps"][-1]["run"]
    assert "live-checks.yml" in run.split("for wf in", 1)[1].split(";", 1)[0]


def test_the_automation_doc_lists_the_workflow_and_its_label(lc: ModuleType) -> None:
    doc = (ROOT / "docs" / "dev" / "automation.md").read_text("utf-8")
    assert "live-checks.yml" in doc and f"`{lc.LABEL}`" in doc


# the run ---------------------------------------------------------------------------------------------------------

CHECKS = """
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.network
COUNTS = Path(os.environ["COUNTS"])


def _count(name):
    COUNTS.mkdir(exist_ok=True)
    f = COUNTS / name
    n = int(f.read_text()) + 1 if f.exists() else 1
    f.write_text(str(n))
    return n


def test_passes():
    _count("passes")


def test_flaky():
    assert _count("flaky") > 1, "connection reset"


def test_broken():
    _count("broken")
    assert False, "source field `x` is no longer served\\nsecond line"


def test_offline_only_is_not_run():
    pass
"""


def _repo(lc: ModuleType, tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    (repo / "checks").mkdir(parents=True)
    for name, text in files.items():
        (repo / "checks" / name).write_text(text, encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    lc.git(repo, "init", "-q")
    lc.git(repo, "add", "-A")
    return repo


def test_a_failed_check_is_retried_once_and_a_passing_retry_does_not_fail(
    lc: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = tmp_path / "counts"
    monkeypatch.setenv("COUNTS", str(counts))
    # The offline test carries no network mark, so -m network leaves it out.
    repo = _repo(lc, tmp_path, {"test_live.py": CHECKS.replace("def test_offline_only", "def _not_a_test")})
    report = lc.run_checks(repo, "checks")
    assert report.collected == 3
    assert report.passed_on_retry == ["checks/test_live.py::test_flaky"]
    assert list(report.failed) == ["checks/test_live.py::test_broken"]
    msg = report.failed["checks/test_live.py::test_broken"]
    assert "no longer served" in msg and "\n" not in msg and "`" not in msg
    assert {p.name: int(p.read_text()) for p in counts.iterdir()} == {"passes": 1, "flaky": 2, "broken": 2}
    data = report.as_json()
    assert data["failed_total"] == 1 and data["failed"][0]["id"] == "checks/test_live.py::test_broken"
    text = lc.summary(report)
    assert "retried once, at most" in text and "Passed on retry" in text and "test_flaky" in text


def test_a_module_that_cannot_be_collected_fails_and_the_others_still_run(
    lc: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COUNTS", str(tmp_path / "counts"))
    ok = "import pytest\npytestmark = pytest.mark.network\n\ndef test_ok():\n    pass\n"
    repo = _repo(lc, tmp_path, {"test_ok.py": ok, "test_gone.py": "import a_module_that_does_not_exist\n"})
    report = lc.run_checks(repo, "checks")
    assert list(report.failed) == ["checks/test_gone.py"]
    assert "a_module_that_does_not_exist" in report.failed["checks/test_gone.py"]
    assert report.collected == 1 and not report.passed_on_retry


def test_no_checks_collected_fails(lc: ModuleType, tmp_path: Path) -> None:
    repo = _repo(lc, tmp_path, {"test_off.py": "def test_off():\n    pass\n"})
    report = lc.run_checks(repo, "checks")
    assert list(report.failed) == [lc.RUN_ID] and "no live check was collected" in report.failed[lc.RUN_ID]


def test_a_check_that_writes_to_the_lake_or_the_checkout_fails(lc: ModuleType, tmp_path: Path) -> None:
    writes = (
        "import os\nfrom pathlib import Path\nimport pytest\npytestmark = pytest.mark.network\n\n"
        "def test_writes():\n"
        "    (Path(os.environ['HDR_LAKE']) / 'raw').mkdir()\n"
        "    (Path(os.environ['HDR_LAKE']) / 'raw' / 'x').write_text('x')\n"
        "    Path('stray.csv').write_text('x')\n"
    )
    repo = _repo(lc, tmp_path, {"test_writes.py": writes})
    report = lc.run_checks(repo, "checks")
    assert list(report.failed) == [lc.WRITES_ID]
    assert "lake: raw/x" in report.failed[lc.WRITES_ID] and "stray.csv" in report.failed[lc.WRITES_ID]


def test_the_run_command_writes_the_summary_and_one_line_of_output(
    lc: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = lc.Report(collected=2, failed={"t.py::a": "boom"}, passed_on_retry=["t.py::b"])
    monkeypatch.setattr(lc, "run_checks", lambda: report)
    out, summ = tmp_path / "out", tmp_path / "summary"
    assert lc.main(["run", "--github-output", str(out), "--summary", str(summ)]) == 1
    (line,) = out.read_text("utf-8").splitlines()
    assert line.startswith("report=") and json.loads(line.removeprefix("report="))["failed_total"] == 1
    assert "t.py::a" in summ.read_text("utf-8")
    monkeypatch.setattr(lc, "run_checks", lambda: lc.Report(collected=2))
    assert lc.main(["run"]) == 0


# the issue -------------------------------------------------------------------------------------------------------


class Api:
    """A fake GitHub: the open issues the label query returns, and every write."""

    def __init__(self, issues: list[dict[str, Any]], label_exists: bool = True):
        self.issues, self.label_exists = issues, label_exists
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, path: str) -> Any:
        assert path.startswith(f"/repos/{REPO}/issues?")
        assert (
            "labels=live-checks-failing" in path and "state=open" in path and "creator=github-actions" in path
        )
        return self.issues

    def send(self, method: str, path: str, body: dict[str, Any]) -> Any:
        self.sent.append((method, path, body))
        if path.endswith("/labels") and self.label_exists:
            raise urllib.error.HTTPError(path, 422, "exists", email.message.Message(), None)
        return {"number": 77}


def _issue(number: int, login: str = BOT, **extra: Any) -> dict[str, Any]:
    return {"number": number, "user": {"login": login}, "labels": [{"name": "live-checks-failing"}], **extra}


FAILED = {
    "collected": 25,
    "failed": [
        {"id": "tests/integration/test_live_x.py::test_contract_structure", "message": "field gone @someone"}
    ],
    "failed_total": 1,
    "passed_on_retry": ["tests/integration/test_live_y.py::test_z"],
}


def test_a_failure_opens_one_labelled_issue(lc: ModuleType) -> None:
    api = Api([])
    done = lc.sync_issue(api.get, api.send, REPO, "failure", FAILED, RUN_URL, SHA)
    assert done == ["opened #77"]
    assert [(m, p) for m, p, _ in api.sent] == [
        ("POST", f"/repos/{REPO}/labels"),
        ("POST", f"/repos/{REPO}/issues"),
    ]
    issue = api.sent[1][2]
    assert issue["labels"] == ["live-checks-failing"] and issue["title"] == lc.TITLE
    body = issue["body"]
    assert "`tests/integration/test_live_x.py::test_contract_structure`: `field gone @someone`" in body
    assert RUN_URL in body and SHA[:12] in body and "test_live_y.py::test_z" in body
    # A mention can appear only inside a code span, where GitHub does not notify anyone.
    assert all(part.count("`") % 2 == 1 for part in body.split("@")[:-1])


def test_a_failure_updates_the_open_issue_and_ignores_others(lc: ModuleType) -> None:
    api = Api([_issue(3, login="someone"), _issue(4, pull_request={}), _issue(5), _issue(9)])
    done = lc.sync_issue(api.get, api.send, REPO, "failure", FAILED, RUN_URL, SHA)
    assert done == ["updated #5"]
    assert [(m, p) for m, p, _ in api.sent] == [
        ("PATCH", f"/repos/{REPO}/issues/5"),
        ("POST", f"/repos/{REPO}/issues/5/comments"),
    ]
    assert "test_contract_structure" in api.sent[0][2]["body"] and RUN_URL in api.sent[1][2]["body"]


def test_a_job_that_ended_without_a_report_still_opens_the_issue(lc: ModuleType) -> None:
    api = Api([], label_exists=False)
    lc.sync_issue(api.get, api.send, REPO, "cancelled", None, RUN_URL, SHA)
    body = api.sent[-1][2]["body"]
    assert "ended 'cancelled' before it reported" in body


def test_a_green_run_closes_the_issue(lc: ModuleType) -> None:
    api = Api([_issue(5), _issue(6, login="someone")])
    green = {"collected": 25, "failed": [], "failed_total": 0, "passed_on_retry": []}
    assert lc.sync_issue(api.get, api.send, REPO, "success", green, RUN_URL, SHA) == ["closed #5"]
    assert api.sent == [
        ("POST", f"/repos/{REPO}/issues/5/comments", {"body": f"Green again: {RUN_URL}. Closing."}),
        ("PATCH", f"/repos/{REPO}/issues/5", {"state": "closed", "state_reason": "completed"}),
    ]
    quiet = Api([])
    assert lc.sync_issue(quiet.get, quiet.send, REPO, "success", green, RUN_URL, SHA)[0].startswith("Every")
    assert quiet.sent == []
    skipped = Api([])
    lc.sync_issue(skipped.get, skipped.send, REPO, "skipped", None, RUN_URL, SHA)
    assert skipped.sent == []


def test_the_report_is_cleaned_again_before_it_reaches_the_issue(lc: ModuleType) -> None:
    raw = json.dumps({**FAILED, "failed": [{"id": "a`b", "message": "x\n" + "y" * 500}]})
    data = lc.parse_report(raw)
    assert data["failed"][0]["id"] == "a b" and len(data["failed"][0]["message"]) == lc.MESSAGE_MAX
    assert "\n" not in data["failed"][0]["message"]
    assert lc.parse_report("") is None and lc.parse_report('{"failed": 3}') is None


def test_the_issue_command_checks_its_arguments_and_fails_in_plain_words(
    lc: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["issue", "--repo", REPO, "--sha", SHA, "--job-result", "failure", "--run-url"]
    with pytest.raises(SystemExit):
        lc.main([*args, "http://evil.example/x"], lambda p: [], lambda *a: None)
    monkeypatch.setenv("LIVE_REPORT", json.dumps(FAILED))
    api = Api([])
    assert lc.main([*args, RUN_URL], api.get, api.send) == 0
    assert api.sent[-1][1] == f"/repos/{REPO}/issues"

    def broken(path: str) -> Any:
        raise urllib.error.URLError("down")

    assert lc.main([*args, RUN_URL], broken, api.send) == 1
    assert "::error::The tracking issue could not be updated" in capsys.readouterr().out
