"""eval/drive.py with a scripted API client and the real server in memory: no network, no key, no real question.

Every question and answer here is synthetic. The scripted client returns the tool_use and text blocks a test lays
out, as the SDK's own Message objects, and records each request it was sent.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TextIO

import anthropic
import anyio
import httpx2
import pytest
from anthropic.types import Message
from mcp import ClientSession, types
from mcp.client._memory import InMemoryTransport
from mcp.shared.exceptions import MCPError

from eval import drive
from eval.drive import DriveError, Limits, Outcome, Question, Setup
from eval.scorer import (
    DRIVE_HEADER_KEY,
    ExpectedRecord,
    ScorerError,
    answers_by_question,
    score_question,
    transcript_by_question,
)
from health_data_registry.query import Engine
from health_data_registry.registry import Registry
from health_data_registry.server import build_server

ROOT = Path(__file__).resolve().parents[1]
# drive_prompt.md's sha256 for each PROMPT_VERSION. A change to the prompt bumps the version and adds a line here.
PROMPT_SHA256 = {"1": "dfdcb758ba1c70d4efe63defd671d71276e2e4d5704b1425bd2d945e17528cba"}
MODEL = "model-under-test"
ENV = {"EVAL_MODEL": MODEL}

# ---------------------------------------------------------------------------------------------------------
# Scripted API client, fake MCP session, the real server in memory


def block_text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def block_tool(name: str, args: dict[str, Any] | None = None, tid: str = "t1") -> dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": args or {}}


def message(*blocks: dict[str, Any], stop: str = "end_turn", usage: dict[str, int] | None = None) -> Message:
    return Message.model_validate(
        {
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": list(blocks),
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage or {"input_tokens": 10, "output_tokens": 5},
        }
    )


Step = Message | Exception | Callable[[dict[str, Any]], Message]


class ScriptedAPI:
    """Stands in for `AsyncAnthropic().messages.create`: returns (or raises) each step in turn; the last repeats."""

    def __init__(self, *steps: Step, hang: bool = False) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        self.hang = hang

    async def __call__(self, **request: Any) -> Message:
        self.requests.append({**request, "messages": copy.copy(request["messages"])})
        if self.hang:
            await anyio.sleep_forever()
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return step(request) if callable(step) else step


def mcp_tool(name: str, description: str | None = "does a thing") -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    )


class FakeSession:
    """An MCP session with fixed tools, paged `per_page` at a time; each call answers "rows for <name>"."""

    def __init__(
        self, names: Sequence[str] = ("get_series", "query_sql"), *, per_page: int = 100, hang: bool = False
    ) -> None:
        self.tools = [mcp_tool(n) for n in names]
        self.per_page = per_page
        self.hang = hang
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.error: Exception | None = None

    async def list_tools(self, *, params: Any = None) -> types.ListToolsResult:
        start = int(params.cursor) if params is not None else 0
        end = start + self.per_page
        nxt = str(end) if end < len(self.tools) else None
        return types.ListToolsResult(tools=self.tools[start:end], next_cursor=nxt)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> types.CallToolResult:
        self.calls.append((name, arguments))
        if self.hang:
            await anyio.sleep_forever()
        if self.error is not None:
            raise self.error
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"rows for {name}")])


@asynccontextmanager
async def memory_session(
    registry: Registry, lake: Path
) -> AsyncIterator[tuple[ClientSession, str, str | None]]:
    server = build_server(registry, Engine(registry=registry, lake=lake))
    async with InMemoryTransport(server) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        yield session, init.server_info.name, init.instructions


def setup(
    tools: Sequence[str] = ("get_series", "query_sql"), protocol: drive.Protocol_ = "A", **limits: Any
) -> Setup:
    api_tools = drive.to_api_tools([mcp_tool(n) for n in tools], protocol)
    return Setup(MODEL, "system", api_tools, protocol, Limits(**limits))


def ask(
    api: ScriptedAPI,
    session: FakeSession | None = None,
    s: Setup | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> tuple[Outcome, list[dict[str, Any]]]:
    recorded: list[dict[str, Any]] = []
    waits: list[float] = []

    async def no_sleep(seconds: float) -> None:
        waits.append(seconds)

    async def go() -> Outcome:
        return await drive.ask(
            Question("syn-1", "synthetic question"),
            s or setup(),
            session or FakeSession(),
            api,
            recorded.append,
            sleep=sleep or no_sleep,
        )

    return anyio.run(go), recorded


def api_error(cls: type[anthropic.APIStatusError], status: int, retry_after: str | None = None) -> Exception:
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return cls("synthetic", response=httpx2.Response(status, request=req, headers=headers), body=None)


def git_checkout(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q"],
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
    ):
        git(path, *args)
    return path


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)  # noqa: S603, S607
    return proc.stdout.strip()


@pytest.fixture
def connect(registry: Registry, lake: Path) -> drive.Connect:
    """A `connect` for drive.run/main that serves the real server in memory, and keeps the env it was given."""

    @asynccontextmanager
    async def _connect(
        server_dir: Path, env: Any, errlog: TextIO
    ) -> AsyncIterator[tuple[ClientSession, str, str | None]]:
        _connect.env = dict(env)  # type: ignore[attr-defined]
        async with memory_session(registry, lake) as got:
            yield got

    return _connect


@pytest.fixture
def run_env(tmp_path: Path, lake: Path) -> Iterator[dict[str, str]]:
    yield {
        **ENV,
        "HDR_LAKE": str(lake),
        "ANTHROPIC_API_KEY": "sk-test-not-a-key",
        "HTTPS_PROXY": "http://p:1",
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------------------------------------
# Tool conversion


@pytest.mark.parametrize(
    ("protocol", "expected"),
    [("A", ["get_series", "query_sql", "age_adjust"]), ("B", ["get_series", "age_adjust"])],
)
def test_to_api_tools_keeps_each_tool_in_order_and_b_drops_query_sql(
    protocol: drive.Protocol_, expected: list[str]
) -> None:
    # Arrange
    tools = [mcp_tool("get_series"), mcp_tool("query_sql"), mcp_tool("age_adjust", description=None)]
    # Act
    got = drive.to_api_tools(tools, protocol)
    # Assert
    assert [t["name"] for t in got] == expected
    assert got[0] == {
        "name": "get_series",
        "description": "does a thing",
        "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    }
    assert got[-1]["description"] == ""


def test_to_api_tools_converts_every_tool_the_real_server_lists(registry: Registry, lake: Path) -> None:
    # Arrange
    async def go() -> tuple[list[Any], list[dict[str, Any]], list[dict[str, Any]]]:
        async with memory_session(registry, lake) as (session, _, _):
            listed = await drive.list_all_tools(session)
            return listed, drive.to_api_tools(listed, "A"), drive.to_api_tools(listed, "B")

    # Act
    listed, a, b = anyio.run(go)
    # Assert
    assert [t["name"] for t in a] == [t.name for t in listed]
    assert "query_sql" in {t["name"] for t in a}
    assert {t["name"] for t in b} == {t.name for t in listed} - {"query_sql"}
    assert all(t["input_schema"]["type"] == "object" and t["description"] for t in a)


def test_list_all_tools_follows_every_page() -> None:
    # Arrange
    session = FakeSession(["a", "b", "c", "d", "e"], per_page=2)
    # Act
    got = anyio.run(drive.list_all_tools, session)
    # Assert
    assert [t.name for t in got] == ["a", "b", "c", "d", "e"]


def test_result_text_joins_text_and_names_other_blocks() -> None:
    # Arrange
    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="a,b"),
            types.ImageContent(type="image", data="AA==", mime_type="image/png"),
            types.TextContent(type="text", text="1,2"),
        ]
    )
    # Act / Assert
    assert drive.result_text(result) == "a,b\n[image content]\n1,2"


# ---------------------------------------------------------------------------------------------------------
# The loop


def test_ask_runs_tool_calls_and_records_the_final_answer() -> None:
    # Arrange
    api = ScriptedAPI(
        message(block_text("Let me look."), block_tool("get_series", {"x": "1"}), stop="tool_use"),
        message(block_text("Toyland had 3 cases."), usage={"input_tokens": 7, "output_tokens": 2}),
    )
    session = FakeSession()
    # Act
    out, recorded = ask(api, session)
    # Assert
    assert (out.answer, out.status, out.turns, out.tool_calls, out.stop_reason) == (
        "Toyland had 3 cases.",
        "answered",
        2,
        1,
        "end_turn",
    )
    assert out.usage == {"input_tokens": 17, "output_tokens": 7}
    assert session.calls == [("get_series", {"x": "1"})]
    assert [(r["id"], r["tool"], r["args"], r["result"], r["turn"], r["is_error"]) for r in recorded] == [
        ("syn-1", "get_series", {"x": "1"}, "rows for get_series", 1, False)
    ]
    first, second = api.requests
    assert first["model"] == MODEL and first["system"] == "system" and first["max_tokens"] == drive.MAX_TOKENS
    assert first["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in first
    assert first["messages"] == [{"role": "user", "content": "synthetic question"}]
    assert second["messages"][2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "rows for get_series", "is_error": False}
        ],
    }


def test_ask_sends_the_assistant_content_back_unchanged_with_its_thinking_block() -> None:
    # Arrange
    first = message(
        {"type": "thinking", "thinking": "", "signature": "sig"},
        block_tool("get_series", {"x": "1"}),
        stop="tool_use",
    )
    api = ScriptedAPI(first, message(block_text("done")))
    # Act
    ask(api)
    # Assert
    assert api.requests[1]["messages"][1] == {"role": "assistant", "content": first.content}


def test_ask_sends_tool_errors_back_marked_as_errors() -> None:
    # Arrange
    session = FakeSession()
    session.error = MCPError(-32602, "bad argument")
    api = ScriptedAPI(
        message(block_tool("get_series", {"x": "1"}), stop="tool_use"), message(block_text("ok"))
    )
    # Act
    out, recorded = ask(api, session)
    # Assert
    assert recorded[0]["is_error"] is True and recorded[0]["result"].startswith("The tool call failed:")
    assert api.requests[1]["messages"][2]["content"][0]["is_error"] is True
    assert out.status == "answered"


@pytest.mark.parametrize(
    ("stop", "status"),
    [
        ("end_turn", "answered"),
        ("stop_sequence", "answered"),
        ("refusal", "refusal"),
        ("max_tokens", "max_tokens"),
        ("model_context_window_exceeded", "stopped"),
    ],
)
def test_ask_ends_on_each_final_stop_reason(stop: str, status: str) -> None:
    # Arrange
    api = ScriptedAPI(message(block_text("partial"), stop=stop))
    # Act
    out, _ = ask(api)
    # Assert
    assert (out.status, out.stop_reason, out.answer, out.turns) == (status, stop, "partial", 1)


def test_ask_continues_after_pause_turn() -> None:
    # Arrange
    api = ScriptedAPI(
        message(block_text("thinking out loud"), stop="pause_turn"), message(block_text("final"))
    )
    # Act
    out, _ = ask(api)
    # Assert
    assert (out.answer, out.turns, out.status) == ("final", 2, "answered")


# Protocol B


def test_protocol_b_hides_query_sql_and_refuses_it_without_reaching_the_server() -> None:
    # Arrange
    session = FakeSession()
    s = setup(protocol="B")
    api = ScriptedAPI(
        message(block_tool("query_sql", {"sql": "SELECT 1"}), stop="tool_use"), message(block_text("no sql"))
    )
    # Act
    out, recorded = ask(api, session, s)
    # Assert
    assert [t["name"] for t in api.requests[0]["tools"]] == ["get_series"]
    assert session.calls == [] and recorded == []
    assert out.refused_calls == [{"tool": "query_sql", "args": {"sql": "SELECT 1"}, "turn": 1}]
    assert out.tool_calls == 0
    result = api.requests[1]["messages"][2]["content"][0]
    assert result["is_error"] is True and "query_sql is not available" in result["content"]


def test_protocol_a_runs_query_sql() -> None:
    # Arrange
    session = FakeSession()
    api = ScriptedAPI(
        message(block_tool("query_sql", {"sql": "SELECT 1"}), stop="tool_use"), message(block_text("x"))
    )
    # Act
    out, recorded = ask(api, session)
    # Assert
    assert session.calls == [("query_sql", {"sql": "SELECT 1"})] and out.refused_calls == []
    assert [r["tool"] for r in recorded] == ["query_sql"]


def test_a_tool_the_server_did_not_list_is_refused() -> None:
    # Arrange
    session = FakeSession()
    api = ScriptedAPI(message(block_tool("made_up", {"x": "1"}), stop="tool_use"), message(block_text("x")))
    # Act
    out, recorded = ask(api, session)
    # Assert
    assert session.calls == [] and recorded == []
    assert out.refused_calls == [{"tool": "made_up", "args": {"x": "1"}, "turn": 1}]
    assert "no tool named 'made_up'" in api.requests[1]["messages"][2]["content"][0]["content"]


@pytest.mark.parametrize(
    ("name", "protocol", "calls_made", "says"),
    [
        ("query_sql", "B", 0, "not available"),
        ("query_sql", "A", 0, ""),
        ("nope", "A", 0, "no tool named"),
        ("get_series", "A", 3, "limit of 3 tool calls"),
        ("get_series", "A", 2, ""),
        ("get_series", "B", 4, "limit of 3 tool calls"),
    ],
)
def test_refusal_reasons(name: str, protocol: drive.Protocol_, calls_made: int, says: str) -> None:
    # Act
    got = drive.refusal(name, protocol, frozenset({"get_series", "query_sql"}), calls_made, Limits(calls=3))
    # Assert
    assert (says in got) if says else got == ""


# Caps and timeouts


@pytest.mark.parametrize("turns", [1, 3, 12])
def test_turn_cap_stops_a_model_that_never_answers(turns: int) -> None:
    # Arrange
    api = ScriptedAPI(message(block_text("again"), block_tool("get_series", {"x": "1"}), stop="tool_use"))
    # Act
    out, recorded = ask(api, s=setup(turns=turns))
    # Assert
    assert (out.status, out.turns, len(api.requests), len(recorded)) == ("turn_cap", turns, turns, turns)
    assert out.answer == "again"


def test_call_cap_runs_the_first_calls_and_refuses_the_rest() -> None:
    # Arrange
    session = FakeSession()
    blocks = [block_tool("get_series", {"x": str(i)}, tid=f"t{i}") for i in range(4)]
    api = ScriptedAPI(message(*blocks, stop="tool_use"), message(block_text("done")))
    # Act
    out, recorded = ask(api, session, setup(calls=3))
    # Assert
    assert [a for _, a in session.calls] == [{"x": "0"}, {"x": "1"}, {"x": "2"}]
    assert out.tool_calls == 3 and len(recorded) == 3
    assert out.refused_calls == [{"tool": "get_series", "args": {"x": "3"}, "turn": 1}]
    results = api.requests[1]["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t0", "t1", "t2", "t3"]
    assert [r["is_error"] for r in results] == [False, False, False, True]


def test_default_limits_are_12_turns_and_25_calls() -> None:
    # Act
    d = Limits()
    # Assert
    assert (d.turns, d.calls) == (12, 25)
    assert d.tool_timeout_s > 0 and d.api_timeout_s > 0 and d.question_timeout_s >= d.api_timeout_s


def test_a_tool_that_hangs_times_out_and_the_question_goes_on() -> None:
    # Arrange
    session = FakeSession(hang=True)
    api = ScriptedAPI(
        message(block_tool("get_series", {"x": "1"}), stop="tool_use"), message(block_text("ok"))
    )
    # Act
    out, recorded = ask(api, session, setup(tool_timeout_s=0.05))
    # Assert
    assert recorded[0]["is_error"] is True
    assert recorded[0]["result"] == "The tool did not answer within 0.05 seconds."
    assert out.status == "answered" and out.answer == "ok"


def test_the_question_deadline_stops_an_api_call_that_hangs() -> None:
    # Arrange
    api = ScriptedAPI(message(block_text("never")), hang=True)
    # Act
    out, _ = ask(api, s=setup(question_timeout_s=0.1))
    # Assert
    assert (out.status, out.turns, out.answer) == ("deadline", 0, "")


def test_an_api_call_that_times_out_before_the_deadline_is_tried_again() -> None:
    # Arrange
    calls = 0

    class SlowOnce(ScriptedAPI):
        async def __call__(self, **request: Any) -> Message:
            nonlocal calls
            calls += 1
            if calls == 1:
                await anyio.sleep_forever()
            return await super().__call__(**request)

    api = SlowOnce(message(block_text("ok")))
    # Act
    out, _ = ask(api, s=setup(api_timeout_s=0.05, question_timeout_s=30))
    # Assert
    assert (calls, out.status, out.answer) == (2, "answered", "ok")


def test_a_tool_call_with_no_time_left_ends_the_question() -> None:
    # Arrange
    clock = iter([0.0, 0.0, 10.0])  # the deadline, the request, then the tool call starts after it

    async def go() -> Outcome:
        return await drive.ask(
            Question("syn-1", "q"),
            setup(question_timeout_s=5),
            FakeSession(),
            ScriptedAPI(message(block_tool("get_series", {"x": "1"}), stop="tool_use")),
            lambda _: None,
            clock=lambda: next(clock),
        )

    # Act
    out = anyio.run(go)
    # Assert
    assert out.status == "deadline" and out.turns == 1


# Retries


def test_overload_and_rate_limits_are_retried_with_backoff() -> None:
    # Arrange
    api = ScriptedAPI(
        api_error(anthropic.RateLimitError, 429),
        api_error(anthropic.OverloadedError, 529, retry_after="7"),
        anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")),
        message(block_text("ok")),
    )
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    # Act
    out, _ = ask(api, sleep=sleep)
    # Assert
    assert out.answer == "ok" and len(api.requests) == 4
    assert waits == [2.0, 7.0, 8.0]


def test_retries_stop_after_a_bounded_number_of_tries() -> None:
    # Arrange
    api = ScriptedAPI(api_error(anthropic.OverloadedError, 529))
    # Act / Assert
    with pytest.raises(DriveError, match=f"after {drive.API_ATTEMPTS} tries"):
        ask(api)
    assert len(api.requests) == drive.API_ATTEMPTS == 6


@pytest.mark.parametrize(
    "error",
    [
        api_error(anthropic.BadRequestError, 400),
        api_error(anthropic.AuthenticationError, 401),
        api_error(anthropic.NotFoundError, 404),
    ],
)
def test_errors_that_would_repeat_are_not_retried(error: Exception) -> None:
    # Arrange
    api = ScriptedAPI(error, message(block_text("never")))
    # Act / Assert
    with pytest.raises(type(error)):
        ask(api)
    assert len(api.requests) == 1


def test_a_wait_past_the_deadline_ends_the_question() -> None:
    # Arrange
    api = ScriptedAPI(api_error(anthropic.RateLimitError, 429, retry_after="50"), message(block_text("late")))
    # Act
    out, _ = ask(api, s=setup(question_timeout_s=10))
    # Assert
    assert out.status == "deadline" and len(api.requests) == 1


@pytest.mark.parametrize(
    ("attempt", "retry_after", "wait"),
    [
        (0, None, 2.0),
        (1, None, 4.0),
        (4, None, 32.0),
        (5, None, 60.0),
        (9, None, 60.0),
        (0, 0.0, 0.0),
        (3, 5.0, 5.0),
        (0, 600.0, 60.0),
        (1, -1.0, 4.0),
    ],
)
def test_backoff(attempt: int, retry_after: float | None, wait: float) -> None:
    assert drive.backoff(attempt, retry_after) == wait


@pytest.mark.parametrize(
    ("status", "retry"),
    [
        (408, True),
        (409, True),
        (429, True),
        (500, True),
        (529, True),
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (413, False),
    ],
)
def test_is_retryable_by_status(status: int, retry: bool) -> None:
    assert drive.is_retryable(api_error(anthropic.APIStatusError, status)) is retry


def test_is_retryable_other_errors() -> None:
    req = httpx2.Request("POST", "https://api.anthropic.com")
    assert drive.is_retryable(anthropic.APITimeoutError(request=req)) is True
    assert drive.is_retryable(ValueError("x")) is False


@pytest.mark.parametrize(("header", "got"), [("3", 3.0), ("1.5", 1.5), ("soon", None), (None, None)])
def test_retry_after_header(header: str | None, got: float | None) -> None:
    assert drive._retry_after(api_error(anthropic.RateLimitError, 429, retry_after=header)) == got
    assert drive._retry_after(ValueError("no response")) is None


# ---------------------------------------------------------------------------------------------------------
# Inputs


def test_read_questions_keeps_id_and_question_only(tmp_path: Path) -> None:
    # Arrange
    f = tmp_path / "q.jsonl"
    rows = [
        {"id": "syn-1", "question": "first", "expected_class": "ANSWER", "expected_datasets": ["x"]},
        {"id": "syn-2", "question": "second", "notes": "label"},
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n\n")
    # Act
    got = drive.read_questions(f)
    # Assert
    assert got == [Question("syn-1", "first"), Question("syn-2", "second")]


@pytest.mark.parametrize(
    ("lines", "says"),
    [
        (
            ['{"id": "a", "question": "secret words"}', "not json secret words"],
            "Line 2 of q.jsonl is not JSON.",
        ),
        (['{"question": "secret words"}'], "Line 1 of q.jsonl needs"),
        (['{"id": "", "question": "secret words"}'], "Line 1 of q.jsonl needs"),
        (['{"id": "a", "question": "  "}'], "Line 1 of q.jsonl needs"),
        (['{"id": 3, "question": "secret words"}'], "Line 1 of q.jsonl needs"),
        (['["secret words"]'], "Line 1 of q.jsonl needs"),
        (['{"id": "a", "question": "secret words"}', '{"id": "a", "question": "secret words"}'], "repeats"),
        ([""], "holds no questions"),
    ],
)
def test_read_questions_refuses_bad_files_without_quoting_them(
    tmp_path: Path, lines: list[str], says: str
) -> None:
    # Arrange
    f = tmp_path / "q.jsonl"
    f.write_text("\n".join(lines))
    # Act
    with pytest.raises(DriveError) as err:
        drive.read_questions(f)
    # Assert
    assert says in str(err.value) and "secret" not in str(err.value)


def test_read_questions_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DriveError, match=r"Could not read the questions file q\.jsonl \(FileNotFoundError\)"):
        drive.read_questions(tmp_path / "q.jsonl")


@pytest.mark.parametrize("env", [{}, {"EVAL_MODEL": ""}, {"EVAL_MODEL": "   "}])
def test_require_model_has_no_default(env: dict[str, str]) -> None:
    with pytest.raises(DriveError, match="EVAL_MODEL is not set"):
        drive.require_model(env)


def test_require_model_strips() -> None:
    assert drive.require_model({"EVAL_MODEL": " m-1 "}) == "m-1"


def test_require_lake(tmp_path: Path) -> None:
    assert drive.require_lake({"HDR_LAKE": str(tmp_path)}) == tmp_path.resolve()
    with pytest.raises(DriveError, match="HDR_LAKE is not set"):
        drive.require_lake({"HDR_LAKE": " "})
    with pytest.raises(DriveError, match="not a directory"):
        drive.require_lake({"HDR_LAKE": str(tmp_path / "missing")})


def test_lake_snapshots_reads_each_latest_pointer(tmp_path: Path, lake: Path) -> None:
    # Arrange
    (tmp_path / "empty").mkdir()
    (lake / "datasets" / "no_pointer_yet").mkdir()
    # Act
    got = drive.lake_snapshots(lake)
    # Assert
    assert list(got) == ["toy_cases"]
    assert got["toy_cases"] == (lake / "datasets" / "toy_cases" / "LATEST").read_text().strip() != ""
    assert drive.lake_snapshots(tmp_path / "empty") == {}


def test_server_commit_and_dirty_flag(tmp_path: Path) -> None:
    # Arrange
    repo = git_checkout(tmp_path / "server")
    head = git(repo, "rev-parse", "HEAD")
    # Act / Assert
    assert drive.server_commit(repo) == (head, False)
    (repo / "new.txt").write_text("x")
    assert drive.server_commit(repo) == (head, True)
    (tmp_path / "plain").mkdir()
    with pytest.raises(DriveError, match="not a git checkout"):
        drive.server_commit(tmp_path / "plain")


def test_server_env_passes_lake_proxy_and_settings_but_no_key() -> None:
    # Arrange
    env = {
        "HDR_LAKE": "/l",
        "HDR_HUB": "off",
        "HTTPS_PROXY": "http://p",
        "no_proxy": "x",
        "SSL_CERT_FILE": "/ca",
        "ANTHROPIC_API_KEY": "secret",
        "EVAL_MODEL": "m",
        "HDR_PRIVATE_HF_TOKEN": "secret",
        "HDR_SIGNING_KEY": "secret",
        "OTHER": "x",
    }
    # Act / Assert
    assert drive.server_env(env) == {
        "HDR_LAKE": "/l",
        "HDR_HUB": "off",
        "HTTPS_PROXY": "http://p",
        "no_proxy": "x",
        "SSL_CERT_FILE": "/ca",
    }


def test_prompt_is_versioned_and_names_no_model() -> None:
    # Act
    text, sha = drive.read_prompt()
    # Assert
    assert sha == hashlib.sha256((ROOT / "eval" / "drive_prompt.md").read_bytes()).hexdigest()
    assert PROMPT_SHA256[drive.PROMPT_VERSION] == sha, (
        "drive_prompt.md changed: bump PROMPT_VERSION and pin it"
    )
    assert "clinician-researcher" in text and "Never state a number that no tool returned" in text
    for name in ("claude", "opus", "sonnet", "haiku", "fable"):
        assert name not in text.lower()


def test_system_prompt_appends_the_server_instructions() -> None:
    assert drive.system_prompt("Prompt.\n", "srv", None) == "Prompt.\n"
    assert drive.system_prompt("Prompt.\n", "srv", "") == "Prompt.\n"
    assert drive.system_prompt("Prompt.\n", "srv", "  Use tools.\n") == (
        "Prompt.\n\n# MCP Server Instructions\n\n## srv\n\nUse tools.\n"
    )


def test_prepare_out_refuses_a_directory_that_holds_a_run(tmp_path: Path) -> None:
    # Arrange
    out = tmp_path / "a" / "b"
    # Act
    drive.prepare_out(out)
    # Assert
    assert out.is_dir()
    for name in ("transcript.jsonl", "answers.jsonl"):
        (out / name).write_text("")
        with pytest.raises(DriveError, match=f"already holds .*{name}"):
            drive.prepare_out(out)
        (out / name).unlink()


@pytest.mark.parametrize(
    ("error", "says"),
    [
        (DriveError("ours"), "ours"),
        (api_error(anthropic.AuthenticationError, 401), "did not accept the credentials"),
        (anthropic.CredentialsError("no key"), "did not accept the credentials"),
        (api_error(anthropic.NotFoundError, 404), "Check EVAL_MODEL"),
        (api_error(anthropic.BadRequestError, 400), "status 400, BadRequestError"),
        (ValueError("secret words"), "The run stopped (ValueError). Details are withheld"),
        (ExceptionGroup("g", [ExceptionGroup("h", [DriveError("ours, grouped")])]), "ours, grouped"),
        (ExceptionGroup("g", [ValueError("secret"), KeyError("secret")]), "The run stopped (ExceptionGroup)"),
    ],
)
def test_explain_never_quotes_a_library_message(error: Exception, says: str) -> None:
    got = drive.explain(error)
    assert says in got and "secret" not in got


# ---------------------------------------------------------------------------------------------------------
# A run: files, header, scorer round trip, privacy


def run_main(
    tmp_path: Path,
    env: dict[str, str],
    connect: drive.Connect,
    api: ScriptedAPI,
    questions: Sequence[dict[str, Any]],
    *extra: str,
) -> tuple[int, Path]:
    qfile = tmp_path / "questions.jsonl"
    qfile.write_text("".join(json.dumps(q) + "\n" for q in questions))
    out = tmp_path / "out"
    server = git_checkout(tmp_path / "server")
    argv = ["--questions", str(qfile), "--server-dir", str(server), "--out", str(out), *extra]
    return drive.main(argv, env=env, create=api, connect=connect), out


def answer_from_series(request: dict[str, Any]) -> Message:
    """Answer with a number only if the get_series result holds it, as a careful client would."""
    result = request["messages"][-1]["content"][0]["content"]
    assert "310" in result
    return message(block_text("Michigan reported 310 legionellosis cases in 2023 (SENTINEL-ANSWER)."))


def test_a_run_round_trips_through_the_scorer(
    tmp_path: Path, run_env: dict[str, str], connect: drive.Connect
) -> None:
    # Arrange
    api = ScriptedAPI(
        message(
            block_tool("get_series", {"condition": "Legionellosis", "locations": ["Michigan"]}),
            stop="tool_use",
        ),
        answer_from_series,
        message(block_text("Nothing to add.")),
    )
    questions = [
        {"id": "syn-1", "question": "SENTINEL-QUESTION one", "expected_class": "ANSWER"},
        {"id": "syn-2", "question": "two"},
    ]
    # Act
    code, out = run_main(tmp_path, run_env, connect, api, questions)
    # Assert
    assert code == 0
    transcript, answers = read_jsonl(out / "transcript.jsonl"), read_jsonl(out / "answers.jsonl")
    assert transcript[0] == answers[0] and DRIVE_HEADER_KEY in transcript[0]
    calls = transcript_by_question(transcript)
    given = answers_by_question(answers)
    assert list(calls) == ["syn-1"] and list(given) == ["syn-1", "syn-2"]
    assert [(c.tool, c.args) for c in calls["syn-1"][0]] == [
        ("get_series", {"condition": "Legionellosis", "locations": ["Michigan"]})
    ]
    assert calls["syn-1"][1] == [transcript[1]["result"]]
    expected = ExpectedRecord.model_validate(
        {"id": "syn-1", "expected_class": "ANSWER", "expected_datasets": ["toy_cases"]}
    )
    scored = score_question(expected, *calls["syn-1"], given["syn-1"])
    assert scored.a.fabricated is False and scored.unmatched_numbers == ()
    invented = score_question(expected, *calls["syn-1"], given["syn-1"].replace("310", "317"))
    assert invented.a.fabricated is True and invented.unmatched_numbers == ("317",)


def test_the_header_records_the_run(tmp_path: Path, run_env: dict[str, str], connect: drive.Connect) -> None:
    # Arrange
    api = ScriptedAPI(message(block_text("x")))
    # Act
    code, out = run_main(
        tmp_path,
        run_env,
        connect,
        api,
        [{"id": "syn-1", "question": "q"}],
        "--protocol",
        "B",
        "--max-turns",
        "4",
    )
    # Assert
    assert code == 0
    head = read_jsonl(out / "answers.jsonl")[0][DRIVE_HEADER_KEY]
    head_sha = git(tmp_path / "server", "rev-parse", "HEAD")
    _, prompt_sha = drive.read_prompt()
    assert head["model"] == MODEL and head["protocol"] == "B"
    assert head["prompt_version"] == drive.PROMPT_VERSION and head["prompt_sha256"] == prompt_sha
    assert head["server_commit"] == head_sha and head["server_dirty"] is False
    assert head["server_name"] == "health-data-registry"
    assert list(head["lake_snapshots"]) == ["toy_cases"]
    assert "query_sql" not in head["tools_offered"] and "get_series" in head["tools_offered"]
    assert head["limits"]["turns"] == 4 and head["questions"] == 1
    assert head["temperature"] == drive.TEMPERATURE_NOTE and head["driver_version"] == drive.DRIVER_VERSION
    from health_data_registry.server import INSTRUCTIONS

    assert head["server_instructions_sha256"] == hashlib.sha256(INSTRUCTIONS.encode()).hexdigest()
    system = api.requests[0]["system"]
    assert system.startswith(drive.read_prompt()[0].rstrip()) and INSTRUCTIONS.strip() in system
    assert connect.env == {"HDR_LAKE": run_env["HDR_LAKE"], "HTTPS_PROXY": "http://p:1"}  # type: ignore[attr-defined]


def test_no_question_or_answer_reaches_stdout_or_stderr(
    tmp_path: Path, run_env: dict[str, str], connect: drive.Connect, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    api = ScriptedAPI(
        message(
            block_tool("get_series", {"condition": "SENTINEL-ARG", "locations": ["Michigan"]}),
            stop="tool_use",
        ),
        message(block_text("SENTINEL-ANSWER")),
    )
    questions = [
        {"id": "syn-1", "question": "SENTINEL-QUESTION"},
        {"id": "syn-2", "question": "SENTINEL-QUESTION 2"},
    ]
    # Act
    code, out = run_main(tmp_path, run_env, connect, api, questions)
    # Assert
    captured = capsys.readouterr()
    assert code == 0
    assert "SENTINEL" not in captured.out + captured.err
    assert captured.out.splitlines() == ["1/2 done", "2/2 done", "drive: answered 2"]
    assert captured.err == ""
    assert "SENTINEL-ANSWER" in (out / "answers.jsonl").read_text()
    assert sorted(p.name for p in out.iterdir()) == [
        "answers.jsonl",
        "drive.log",
        "server.log",
        "transcript.jsonl",
    ]


def test_a_failure_mid_run_prints_a_plain_message_only(
    tmp_path: Path, run_env: dict[str, str], connect: drive.Connect, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    api = ScriptedAPI(ValueError("SENTINEL-QUESTION leaked"))
    # Act
    code, _ = run_main(tmp_path, run_env, connect, api, [{"id": "syn-1", "question": "SENTINEL-QUESTION"}])
    # Assert
    captured = capsys.readouterr()
    assert code == 1 and "SENTINEL" not in captured.out + captured.err
    assert (
        captured.err
        == "drive: The run stopped (ValueError). Details are withheld: they could quote a question.\n"
    )


@pytest.mark.parametrize("model", [None, "", " "])
def test_missing_eval_model_fails_with_a_plain_message(
    tmp_path: Path,
    run_env: dict[str, str],
    connect: drive.Connect,
    capsys: pytest.CaptureFixture[str],
    model: str | None,
) -> None:
    # Arrange
    env = {k: v for k, v in run_env.items() if k != "EVAL_MODEL"}
    if model is not None:
        env["EVAL_MODEL"] = model
    api = ScriptedAPI(message(block_text("never")))
    # Act
    code, out = run_main(tmp_path, env, connect, api, [{"id": "syn-1", "question": "SENTINEL-QUESTION"}])
    # Assert
    captured = capsys.readouterr()
    assert code == 2 and api.requests == [] and not out.exists()
    assert captured.err == (
        "drive: EVAL_MODEL is not set. Set it to the id of the model that should answer; the driver has no default.\n"
    )
    assert captured.out == ""


def test_main_refuses_bad_inputs_before_starting(
    tmp_path: Path, run_env: dict[str, str], connect: drive.Connect, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    env = {**run_env, "HDR_LAKE": str(tmp_path / "missing")}
    # Act
    code, _ = run_main(
        tmp_path, env, connect, ScriptedAPI(message(block_text("x"))), [{"id": "a", "question": "q"}]
    )
    # Assert
    assert code == 2 and "HDR_LAKE" in capsys.readouterr().err


def test_main_refuses_an_out_directory_with_a_run(
    tmp_path: Path, run_env: dict[str, str], connect: drive.Connect, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "answers.jsonl").write_text("old")
    # Act
    code, out = run_main(
        tmp_path, run_env, connect, ScriptedAPI(message(block_text("x"))), [{"id": "a", "question": "q"}]
    )
    # Assert
    assert code == 2 and (out / "answers.jsonl").read_text() == "old"
    assert "Pick an empty --out directory" in capsys.readouterr().err


def test_main_leaves_logging_as_it_found_it(
    tmp_path: Path, run_env: dict[str, str], connect: drive.Connect
) -> None:
    # Arrange
    import logging

    before = list(logging.getLogger().handlers)
    # Act
    run_main(
        tmp_path, run_env, connect, ScriptedAPI(message(block_text("x"))), [{"id": "a", "question": "q"}]
    )
    # Assert
    assert logging.getLogger().handlers == before


# ---------------------------------------------------------------------------------------------------------
# The scorer's readers


def test_scorer_readers_skip_the_drive_header() -> None:
    # Arrange
    head = {DRIVE_HEADER_KEY: {"model": "m"}}
    transcript = [head, {"id": "syn-1", "tool": "get_series", "args": {}, "result": "r"}]
    answers = [head, {"id": "syn-1", "answer": "a"}, {"id": "syn-2", "answer": None}, {"id": 3}]
    # Act / Assert
    assert list(transcript_by_question(transcript)) == ["syn-1"]
    assert answers_by_question(answers) == {"syn-1": "a", "syn-2": "", "3": ""}


def test_answers_by_question_refuses_a_question_answered_twice() -> None:
    with pytest.raises(ScorerError, match="more than once"):
        answers_by_question([{"id": "syn-1", "answer": "a"}, {"id": "syn-1", "answer": "b"}])
