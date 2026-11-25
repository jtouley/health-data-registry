""".github/workflows/sealed-test.yml keeps the sealed TEST set sealed: what the workflow file must always say.

These are the rules a later edit could drop without any run failing: manual dispatch only, no token by default,
secrets only in the eval environment and only on the steps that need them, no artifacts or caches, actions
pinned by commit sha, and checkouts without credentials."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "sealed-test.yml"
SECRET_STEPS = {
    "EVAL_TEST_KEY": {
        "Check the eval environment's secrets are set",
        "Decrypt the labels into the runner's temporary directory",
    },
    "ANTHROPIC_API_KEY": {
        "Check the eval environment's secrets are set",
        "Check the driver can ask one DEV question through the server",
        "Ask every question through the server",
    },
    "EVAL_HF_TOKEN": {"Check the eval environment's secrets are set", "Fetch the ciphertext"},
}


@pytest.fixture(scope="module")
def wf() -> dict[Any, Any]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def steps(wf: dict[Any, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(job, s) for job, j in wf["jobs"].items() for s in j.get("steps", [])]


def test_manual_dispatch_only_with_the_agreed_inputs(wf: dict[Any, Any]) -> None:
    on = wf[True]  # YAML reads the key `on` as true
    assert set(on) == {"workflow_dispatch"}
    assert set(on["workflow_dispatch"]["inputs"]) == {
        "mode",
        "ciphertext",
        "commit",
        "base_commit",
        "wave",
        "reaudited_labels",
    }


def test_no_token_by_default_and_least_privilege_per_job(wf: dict[Any, Any]) -> None:
    assert wf["permissions"] == {}
    assert {job: j.get("permissions") for job, j in wf["jobs"].items()} == {
        "inputs": {"actions": "read"},
        "evaluate": {"contents": "read"},
        "label-check": {"contents": "read"},
        "use-log": {"issues": "write"},
    }


def test_only_the_eval_environment_jobs_read_secrets_and_only_on_main(wf: dict[Any, Any]) -> None:
    for job, j in wf["jobs"].items():
        uses_secrets = "secrets." in yaml.safe_dump(j)
        assert uses_secrets == (j.get("environment") == "eval"), job
        if uses_secrets:
            assert "github.ref == 'refs/heads/main'" in j["if"], job
            assert j["needs"] == "inputs", job


def test_each_secret_is_set_only_on_the_steps_that_need_it(wf: dict[Any, Any]) -> None:
    for job, j in wf["jobs"].items():
        assert "secrets." not in yaml.safe_dump(j.get("env", {})), f"{job}: no job-wide secret"
    for name, allowed in SECRET_STEPS.items():
        seen = {s.get("name") for _, s in steps(wf) if f"secrets.{name}" in yaml.safe_dump(s)}
        assert seen and seen <= allowed, (name, seen)
    every = set(re.findall(r"secrets\.(\w+)", WORKFLOW.read_text(encoding="utf-8")))
    assert every == set(SECRET_STEPS)


def test_no_artifacts_caches_or_untrusted_triggers(wf: dict[Any, Any]) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for banned in (
        "upload-artifact",
        "actions/cache",
        "pull_request_target",
        "set -x",
        'cat "$RUNNER_TEMP/sealed/test.jsonl"',
    ):
        assert banned not in text, banned
    for _, s in steps(wf):
        if str(s.get("uses", "")).startswith("astral-sh/setup-uv"):
            assert s["with"]["enable-cache"] is False


def test_actions_pinned_by_sha_and_checkouts_drop_credentials(wf: dict[Any, Any]) -> None:
    for job, s in steps(wf):
        uses = s.get("uses")
        if not uses:
            continue
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", uses), (job, uses)
        if uses.startswith("actions/checkout@"):
            assert s["with"]["persist-credentials"] is False, job


def test_the_plaintext_is_removed_whatever_happens(wf: dict[Any, Any]) -> None:
    for job in ("evaluate", "label-check"):
        last = wf["jobs"][job]["steps"][-1]
        assert last["if"] == "always()"
        assert last["run"].strip() == 'rm -rf "$RUNNER_TEMP/sealed"'


DECRYPT = "Decrypt the labels into the runner's temporary directory"
MARK = "Record that the labels are about to be decrypted"


def names(wf: dict[Any, Any], job: str) -> list[str]:
    return [s.get("name", s.get("uses", "")) for s in wf["jobs"][job]["steps"]]


def test_every_cheap_check_comes_before_the_decrypt(wf: dict[Any, Any]) -> None:
    n = names(wf, "evaluate")
    assert (
        n.index("Check the commit is on main and the driver is here")
        < n.index("Fetch the ciphertext")
        < n.index("Check which labels the ciphertext must hold")
        < n.index("Build a fresh lake of the public sources at the commit")
        < n.index("Check the driver can ask one DEV question through the server")
        < n.index(MARK)
        == n.index(DECRYPT) - 1
    )
    n = names(wf, "label-check")
    assert (
        n.index("Check both commits are on main, base before commit")
        < n.index("Fetch the ciphertext")
        < n.index(MARK)
        == n.index(DECRYPT) - 1
    )


def test_label_check_takes_commits_from_main_only(wf: dict[Any, Any]) -> None:
    step = next(
        s for s in wf["jobs"]["label-check"]["steps"] if s.get("name", "").startswith("Check both commits")
    )
    assert 'git merge-base --is-ancestor "$c" HEAD' in step["run"]
    assert 'git merge-base --is-ancestor "$BASE" "$HEAD_COMMIT"' in step["run"]


def test_the_driver_gets_the_questions_alone(wf: dict[Any, Any]) -> None:
    ask = next(
        s for s in wf["jobs"]["evaluate"]["steps"] if s.get("name") == "Ask every question through the server"
    )
    assert '--questions "$RUNNER_TEMP/sealed/questions.jsonl"' in ask["run"]
    assert "test.jsonl" not in ask["run"]


def test_the_inputs_job_checks_the_environment_is_main_only(wf: dict[Any, Any]) -> None:
    run = wf["jobs"]["inputs"]["steps"][-1]["run"]
    assert "custom_branch_policies" in run and '"branch:main"' in run


def test_use_log_runs_whenever_a_decrypt_was_started(wf: dict[Any, Any]) -> None:
    j = wf["jobs"]["use-log"]
    assert j["needs"] == ["evaluate", "label-check"]
    assert j["if"].startswith("always() && (")
    for job in ("evaluate", "label-check"):
        assert f"needs.{job}.outputs.decrypt_started == 'true'" in j["if"]
        assert wf["jobs"][job]["outputs"]["decrypt_started"] == "${{ steps.mark.outputs.decrypt_started }}"
    run = j["steps"][0]["run"]
    assert "| head" not in run
    assert run.index('issues/$issue/comments" -f body') < run.index("/lock")
    assert "run_attempt" in j["steps"][0]["env"]["RUN_URL"]
