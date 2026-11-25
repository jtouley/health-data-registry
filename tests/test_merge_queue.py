"""scripts/merge_queue.py and its wiring: a PR defers mutation shards only while main has a merge queue."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
reads_workflows = pytest.mark.skipif("MUTANT_UNDER_TEST" in os.environ, reason=".github/ is not in mutants/")


@pytest.fixture
def mq(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("merge_queue", ROOT / "scripts" / "merge_queue.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "merge_queue", mod)
    spec.loader.exec_module(mod)
    return mod


QUEUE = [{"type": "pull_request"}, {"type": "merge_queue", "parameters": {"merge_method": "SQUASH"}}]
NO_QUEUE = [{"type": "pull_request"}, {"type": "required_status_checks"}]


def _main(mq: ModuleType, capsys: pytest.CaptureFixture[str], answer: Any) -> str:
    def fetch(repo: str, branch: str, token: str) -> Any:
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert mq.main(["--repo", "owner/repo"], fetch) == 0
    out: str = capsys.readouterr().out.strip()
    return out


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (QUEUE, "true"),  # with a queue: a PR may defer
        (NO_QUEUE, "false"),  # without one: every shard runs on the PR
        ([], "false"),
        ({"message": "Not Found"}, "false"),
        (OSError("network down"), "false"),  # any doubt: nothing defers
    ],
    ids=["queue", "no-queue", "no-rules", "unexpected-answer", "api-error"],
)
def test_only_a_merge_queue_rule_lets_a_pr_defer(
    mq: ModuleType, capsys: pytest.CaptureFixture[str], answer: Any, expected: str
) -> None:
    assert _main(mq, capsys, answer) == expected


def test_a_bad_repository_name_never_defers(mq: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert mq.main(["--repo", "../x"], lambda *a: QUEUE) == 0
    assert capsys.readouterr().out.strip() == "false"


@reads_workflows
def test_the_workflow_defers_only_with_a_queue() -> None:
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    changes = jobs["changes"]
    assert changes["outputs"]["queue"] == "${{ steps.queue.outputs.queue }}"
    step = next(s for s in changes["steps"] if s.get("id") == "queue")
    assert 'if [ "${queue}" != "true" ]; then queue=false; fi' in step["run"]  # anything else: no queue
    listing = next(s for s in changes["steps"] if s.get("id") == "mutation")
    assert (
        '[ "${QUEUE}" = "true" ] && [ "${count}" -gt 6 ]' in listing["run"]
    )  # >6 shards defer only with a queue
    shard = jobs["mutation"]
    run = next(s for s in shard["steps"] if s.get("name") == "Mutation tests on one shard")["run"]
    assert 'if [ "${EVENT}" = "merge_group" ] || [ "${QUEUE}" != "true" ]; then limits=(); fi' in run
    assert "needs.changes.outputs.queue != 'true') && 20 || 3" in str(shard["timeout-minutes"])
    floor = next(s for s in jobs["result"]["steps"] if s.get("name") == "Mutation floor on the total")["run"]
    assert 'if [ "${EVENT}" = "merge_group" ] || [ "${QUEUE}" != "true" ]; then allow=(); fi' in floor
