"""scripts/check_space.py: after a deploy the Space runs the new commit, is healthy and answers, against fakes."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = "owner/registry-mcp"
HOST = "owner-registry-mcp.hf.space"
COMMIT = "a" * 40
OLD = "b" * 40
VERSION = "1.2.3"
SOURCES = "source_id,title,publisher\ncdc_x,X,CDC\n"


@pytest.fixture
def cs(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_space", ROOT / "scripts" / "check_space.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "check_space", mod)
    spec.loader.exec_module(mod)
    return mod


def _runtime(
    stage: str = "RUNNING", sha: str = COMMIT, domain: str = HOST, dstage: str = "READY"
) -> dict[str, Any]:
    return {"stage": stage, "sha": sha, "domains": [{"domain": domain, "stage": dstage}]}


def _sse(message: dict[str, Any]) -> str:
    return f"event: message\ndata: {json.dumps(message)}\n\n"


def _answer(text: str = SOURCES, is_error: bool = False) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
    }


class Fake:
    """The Hub API, the Space's /healthz and /mcp, and a clock that moves only when the check sleeps."""

    def __init__(
        self,
        runtimes: list[Any] | None = None,
        health: list[Any] | None = None,
        answer: Any = None,
        info: dict[str, Any] | None = None,
    ) -> None:
        self.runtimes = runtimes if runtimes is not None else [_runtime()]
        self.health = health if health is not None else [{"status": "ok", "version": VERSION}]
        self.answer = _sse(_answer()) if answer is None else answer
        self.info = {"sha": COMMIT} if info is None else info
        self.now = 0.0
        self.sleeps: list[float] = []
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    @staticmethod
    def _next(queue: list[Any]) -> Any:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def get_json(self, url: str) -> Any:
        if url == f"https://huggingface.co/api/spaces/{REPO}":
            return self.info
        if url == f"https://huggingface.co/api/spaces/{REPO}/runtime":
            return self._next(self.runtimes)
        if url == f"https://{HOST}/healthz":
            return self._next(self.health)
        raise AssertionError(url)

    def post(self, url: str, body: bytes, headers: dict[str, str]) -> str:
        self.posts.append((url, json.loads(body), headers))
        if isinstance(self.answer, Exception):
            raise self.answer
        return str(self.answer)

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _check(cs: ModuleType, fake: Fake, timeout: float = 100, interval: float = 10) -> str:
    return str(
        cs.check_space(
            fake.get_json,
            fake.post,
            REPO,
            VERSION,
            timeout=timeout,
            interval=interval,
            clock=fake.clock,
            sleep=fake.sleep,
        )
    )


def test_a_healthy_space_running_the_new_commit_passes(cs: ModuleType) -> None:
    fake = Fake()
    assert _check(cs, fake) == HOST
    assert fake.sleeps == []
    url, request, headers = fake.posts[0]
    assert url == f"https://{HOST}/mcp"
    assert request["method"] == "tools/call" and request["params"] == {
        "name": "list_sources",
        "arguments": {},
    }
    assert headers["Accept"] == "application/json, text/event-stream"
    assert headers["Content-Type"] == "application/json"


def test_it_waits_while_the_old_commit_serves_and_the_new_one_builds(cs: ModuleType) -> None:
    fake = Fake(runtimes=[_runtime(sha=OLD), _runtime(stage="BUILDING"), OSError("blip"), _runtime()])
    assert _check(cs, fake) == HOST
    assert fake.sleeps == [10, 10, 10]


def test_an_old_commits_error_is_not_the_new_deploys_failure(cs: ModuleType) -> None:
    fake = Fake(runtimes=[_runtime(stage="RUNTIME_ERROR", sha=OLD), _runtime()])
    assert _check(cs, fake) == HOST


@pytest.mark.parametrize("stage", ["BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR", "NO_APP_FILE"])
def test_a_build_or_start_error_on_the_new_commit_fails_at_once(cs: ModuleType, stage: str) -> None:
    fake = Fake(runtimes=[_runtime(stage=stage)])
    with pytest.raises(cs.SpaceCheckFailed, match=f"stopped with {stage} on the deployed commit {COMMIT}"):
        _check(cs, fake)
    assert fake.sleeps == []


def test_a_space_that_never_starts_times_out_with_what_it_last_saw(cs: ModuleType) -> None:
    fake = Fake(runtimes=[_runtime(stage="BUILDING")])
    with pytest.raises(
        cs.SpaceCheckFailed, match=r"did not start .* in time \(last seen: stage BUILDING on commit"
    ):
        _check(cs, fake, timeout=30)
    assert fake.sleeps == [10, 10, 10]


def test_a_runtime_that_cannot_be_read_times_out_too(cs: ModuleType) -> None:
    fake = Fake(runtimes=[ValueError("bad json")])
    with pytest.raises(cs.SpaceCheckFailed, match=r"last seen: the runtime could not be read \(ValueError\)"):
        _check(cs, fake, timeout=10)


@pytest.mark.parametrize(
    "runtime",
    [
        _runtime(dstage="PENDING"),
        _runtime(domain="evil.example.com"),
        _runtime(domain="a.b.hf.space"),
        {"stage": "RUNNING", "sha": COMMIT},
    ],
)
def test_only_a_ready_hf_space_host_is_contacted(cs: ModuleType, runtime: dict[str, Any]) -> None:
    fake = Fake(runtimes=[runtime])
    with pytest.raises(cs.SpaceCheckFailed, match="running, but no ready"):
        _check(cs, fake, timeout=10)
    assert fake.posts == []


def test_it_waits_for_healthz_to_report_the_new_version(cs: ModuleType) -> None:
    fake = Fake(
        health=[
            OSError("refused"),
            {"status": "ok", "version": "1.2.2"},
            {"status": "ok", "version": VERSION},
        ]
    )
    assert _check(cs, fake) == HOST
    assert fake.sleeps == [10, 10]


@pytest.mark.parametrize(
    ("health", "seen"),
    [
        ({"status": "ok", "version": "1.2.2"}, "status 'ok', version '1.2.2'"),
        ({"status": "starting", "version": VERSION}, "status 'starting'"),
        (OSError("refused"), r"no healthy answer \(OSError\)"),
    ],
)
def test_an_unhealthy_space_fails_with_the_rollback_step(cs: ModuleType, health: Any, seen: str) -> None:
    fake = Fake(health=[health])
    with pytest.raises(cs.SpaceCheckFailed, match=seen) as e:
        _check(cs, fake, timeout=20)
    assert "Roll back the Space" in str(e.value)
    assert fake.posts == []


def test_a_plain_json_answer_is_read_too(cs: ModuleType) -> None:
    assert _check(cs, Fake(answer=json.dumps(_answer()))) == HOST


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (_sse(_answer(is_error=True)), "answered list_sources with an error"),
        (_sse({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602}}), "answered list_sources with an error"),
        (_sse(_answer(text="Sorry, something went wrong")), "not the sources table"),
        (_sse({"jsonrpc": "2.0", "id": 1, "result": {"content": []}}), "not the sources table"),
        ("event: ping\n\n", r"did not answer the MCP question list_sources \(ValueError\)"),
        ("data: [1, 2]\n", r"did not answer .* \(ValueError\)"),
        ("<html>proxy error</html>", r"did not answer .* \(ValueError\)"),
        ("{not json", r"did not answer .* \(JSONDecodeError\)"),
        (OSError("reset"), r"did not answer .* \(OSError\)"),
    ],
)
def test_a_wrong_answer_to_the_known_good_question_fails(cs: ModuleType, answer: Any, message: str) -> None:
    with pytest.raises(cs.SpaceCheckFailed, match=message):
        _check(cs, Fake(answer=answer))


def test_the_sse_answer_is_the_last_data_line(cs: ModuleType) -> None:
    text = 'data: {"id": 0}\n\n' + _sse(_answer())
    assert cs.mcp_result(text)["result"]["content"][0]["text"] == SOURCES


@pytest.mark.parametrize(("repo", "version"), [("not a repo", VERSION), (REPO, "v1.2.3"), (REPO, "1.2")])
def test_bad_arguments_are_refused_before_any_request(cs: ModuleType, repo: str, version: str) -> None:
    fake = Fake(info={})
    with pytest.raises(cs.SpaceCheckFailed):
        cs.check_space(fake.get_json, fake.post, repo, version, timeout=1, interval=1)


def test_a_space_without_a_commit_fails(cs: ModuleType) -> None:
    with pytest.raises(cs.SpaceCheckFailed, match="did not report a commit"):
        _check(cs, Fake(info={"sha": None}))


@pytest.mark.parametrize(
    "url",
    [
        "http://huggingface.co/api/spaces/x",
        "https://huggingface.co.evil.example/x",
        "https://example.com/healthz",
        "https://a.b.hf.space/healthz",
        "file:///etc/passwd",
    ],
)
def test_only_https_to_the_hub_or_a_space_host_is_allowed(cs: ModuleType, url: str) -> None:
    with pytest.raises(ValueError, match="refusing to contact"):
        cs._allowed(url)


def test_the_hub_and_a_space_host_are_allowed(cs: ModuleType) -> None:
    assert cs._allowed("https://huggingface.co/api/spaces/x") == "https://huggingface.co/api/spaces/x"
    assert cs._allowed(f"https://{HOST}/mcp") == f"https://{HOST}/mcp"


def test_main_reports_success_and_failure_in_plain_words(
    cs: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake()
    argv = ["--repo", REPO, "--version", VERSION, "--timeout", "30", "--interval", "5"]
    assert cs.main(argv, fake.get_json, fake.post, fake.clock, fake.sleep) == 0
    assert (
        capsys.readouterr().out.strip()
        == f"https://{HOST} runs {VERSION}, is healthy, and answered list_sources."
    )
    fake = Fake(health=[{"status": "ok", "version": "0.0.1"}])
    assert cs.main(argv, fake.get_json, fake.post, fake.clock, fake.sleep) == 1
    out = capsys.readouterr().out
    assert out.startswith("::error::Space check failed: ") and "Roll back the Space" in out
    assert fake.sleeps == [5] * 6


def test_an_unexpected_error_is_a_failed_check_never_a_pass(
    cs: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(url: str) -> Any:
        raise RuntimeError("boom")

    assert cs.main(["--repo", REPO, "--version", VERSION], broken, Fake().post) == 1
    assert "the check itself broke (RuntimeError: boom)" in capsys.readouterr().out
