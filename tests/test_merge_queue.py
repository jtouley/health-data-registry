"""scripts/merge_queue.py and its wiring: a PR narrows its run, and reuses a verdict, only with a merge queue."""

from __future__ import annotations

import importlib.util
import os
import subprocess
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
        (QUEUE, "true"),  # with a queue: a PR runs what its change can reach
        (NO_QUEUE, "false"),  # without one: a PR runs everything
        ([], "false"),
        ({"message": "Not Found"}, "false"),
        (OSError("network down"), "false"),  # any doubt: everything runs
    ],
    ids=["queue", "no-queue", "no-rules", "unexpected-answer", "api-error"],
)
def test_only_a_merge_queue_rule_lets_a_pr_narrow(
    mq: ModuleType, capsys: pytest.CaptureFixture[str], answer: Any, expected: str
) -> None:
    assert _main(mq, capsys, answer) == expected


def test_a_bad_repository_name_never_narrows(mq: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert mq.main(["--repo", "../x"], lambda *a: QUEUE) == 0
    assert capsys.readouterr().out.strip() == "false"


@reads_workflows
def test_without_a_queue_a_pr_runs_everything_and_reuses_nothing() -> None:
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    changes = jobs["changes"]
    steps = {s.get("id"): s for s in changes["steps"]}
    assert (
        'if [ "${queue}" != "true" ]; then queue=false; fi' in steps["queue"]["run"]
    )  # anything else: no queue
    order = [s.get("id") for s in changes["steps"]]
    assert order.index("queue") < order.index("reuse") < order.index("impact")
    assert "steps.queue.outputs.queue == 'true'" in steps["reuse"]["if"]
    assert steps["impact"]["env"]["QUEUE"] == "${{ steps.queue.outputs.queue }}"


def _run_impact_step(tmp_path: Path, event: str, ref: str, queue: str) -> tuple[str, str]:
    """The impact step's script under bash -e, with fake git and uv on PATH: (step outputs, uv's arguments)."""
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    run = next(s for s in jobs["changes"]["steps"] if s.get("id") == "impact")["run"]
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, body in {"git": f"echo {'a' * 40}", "uv": f'echo "$@" >> {tmp_path}/uv-args'}.items():
        (bin_ / name).write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        (bin_ / name).chmod(0o755)
    out = tmp_path / "out"
    out.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out),
        "EVENT": event,
        "REF": ref,
        "BASE_REF": "main",
        "QUEUE": queue,
    }
    done = subprocess.run(  # noqa: S603
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run],  # noqa: S607
        env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    return out.read_text(encoding="utf-8"), (tmp_path / "uv-args").read_text(encoding="utf-8")


@reads_workflows
@pytest.mark.parametrize(
    ("event", "ref", "queue", "narrowed"),
    [
        ("pull_request", "refs/pull/1/merge", "true", True),
        ("push", "refs/heads/pre-pr/x", "true", True),
        ("pull_request", "refs/pull/1/merge", "false", False),  # no queue: nothing would run the rest
        ("push", "refs/heads/pre-pr/x", "", False),
        ("merge_group", "refs/heads/gh-readonly-queue/main/x", "", False),
        ("push", "refs/heads/main", "", False),
    ],
)
def test_only_a_pr_or_pre_pr_run_with_a_queue_is_narrowed(
    tmp_path: Path, event: str, ref: str, queue: str, narrowed: bool
) -> None:
    out, uv_args = _run_impact_step(tmp_path, event, ref, queue)
    if narrowed:
        assert f"--base {'a' * 40}" in uv_args and "--full" not in uv_args
        assert 'pythons=["3.12"]\n' in out
    else:
        assert "--full" in uv_args and "--base" not in uv_args
        assert 'pythons=["3.12","3.13"]\n' in out
    is_pr = event == "pull_request" or ref.startswith("refs/heads/pre-pr/")
    assert (f"merge_base={'a' * 40}\n" if is_pr else "merge_base=\n") in out  # the ratchet still compares
