"""The behaviour eval's question driver: ask each question through the MCP server with the Claude API.

It plays the clinician's Claude. For each question it starts from the fixed system prompt in `drive_prompt.md`
plus the server's own `instructions`, offers the model the server's tools, runs every tool call the model makes
against the server over stdio, and stops at a final text answer or a cap. It writes two files to `--out`, in the
format `eval/scorer.py` reads (`transcript_by_question` and `answers_by_question`):

- `transcript.jsonl`: one object per tool call that reached the server: `id`, `tool`, `args`, `result` (the text
  the tool returned), plus `turn`, `is_error` and `seconds`.
- `answers.jsonl`: one object per question: `id`, `answer`, and how the question ended (`status`, `turns`,
  `tool_calls`, `refused_calls`, `stop_reason`, `usage`).

Each file starts with one header record, `{"drive_header": {...}}`: the model (EVAL_MODEL), the prompt's version
and sha256, the server instructions' sha256, the server commit, the lake's snapshot ids, the protocol, the tools
offered and the limits. The scorer skips it.

Usage (EVAL_MODEL, ANTHROPIC_API_KEY and HDR_LAKE come from the environment; eval/README.md):

    uv run python -m eval.drive --questions eval/dev/dev.jsonl --server-dir . --out ../run-dev [--protocol B]
    uv run python -m eval.drive --check --server-dir . --out ../check

`--check` asks the first DEV question alone and exits 0 only if it was answered: the sealed workflow's readiness
check before it decrypts anything.

Protocol B takes `query_sql` out of the tools offered, and refuses it if the model calls it anyway; a refused
call never reaches the server, so it is listed in the answer's `refused_calls`, not in the transcript.

Privacy: the sealed workflow runs this on TEST, so nothing here prints a question, an answer, a tool argument
or a tool result. Progress lines give counts only, every error message is one this module wrote, and any other
failure is reported by its type alone. The server's stderr goes to `server.log` in `--out`; log records and
Python's warnings (`logging.captureWarnings`) go to `drive.log` there.

Why a hand-written tool loop rather than the SDK's tool runner: the driver needs a cap on turns and on tool
calls, a timeout on each call, a deadline per question, protocol B's refusal and a record of every call, and the
tests drive it with a scripted client in place of the API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, TextIO

import anyio

from .label_check import LabelCheckError, run_git
from .scorer import DRIVE_HEADER_KEY as HEADER_KEY

DRIVER_VERSION = "1"
PROMPT_PATH = Path(__file__).with_name("drive_prompt.md")
# --check asks this file's first question: DEV is public, so a check spends no use of TEST.
DEV_QUESTIONS = Path(__file__).with_name("dev") / "dev.jsonl"
# Bump with every change to drive_prompt.md; tests/test_eval_drive.py pins each version's sha256.
PROMPT_VERSION = "1"

SQL_TOOL = "query_sql"
MAX_TOKENS = 16000
# The current API takes no sampling parameters (temperature, top_p, top_k), so none is sent and repeated runs can
# differ. Recorded in every header so a reader doesn't assume temperature 0.
TEMPERATURE_NOTE = "not set: the API rejects sampling parameters, so runs are not deterministic"
API_ATTEMPTS = 6
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 60.0
# Statuses the API returns for overload and rate limits, and the server errors worth another try.
RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
# Passed to the server process besides mcp's safe defaults (HOME, PATH, ...): the lake and the server's own
# settings, and the proxy and CA settings it needs for live sources. Never an API key.
SERVER_ENV_PREFIX = "HDR_"
SERVER_ENV_PASS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)
SERVER_ENV_BLOCK = frozenset({"HDR_PRIVATE_HF_TOKEN", "HDR_SIGNING_KEY"})

Protocol_ = Literal["A", "B"]
Status = Literal["answered", "turn_cap", "deadline", "refusal", "max_tokens", "stopped"]
# How a final (non-tool_use) stop_reason ends a question; any other ends it as "stopped".
STOP_STATUS: Mapping[str, Status] = {
    "end_turn": "answered",
    "stop_sequence": "answered",
    "refusal": "refusal",
    "max_tokens": "max_tokens",
}


class DriveError(Exception):
    """A run that can't go on. Its message is written here, quotes no question or answer, and is safe to print."""


@dataclass(frozen=True)
class Limits:
    turns: int = 12  # API requests per question
    calls: int = 25  # tool calls that reach the server, per question
    tool_timeout_s: float = 120.0  # one tool call
    api_timeout_s: float = 300.0  # one API request
    question_timeout_s: float = 900.0  # the whole question, retries and waits included

    def __post_init__(self) -> None:
        # A zero, negative or NaN limit would end every question at once and still give a run to score.
        for name in ("turns", "calls"):
            value = getattr(self, name)
            if not (isinstance(value, int) and value >= 1):
                raise DriveError(f"The limit {name} is {value}; it must be a whole number, 1 or more.")
        for name in ("tool_timeout_s", "api_timeout_s", "question_timeout_s"):
            value = getattr(self, name)
            if not (math.isfinite(value) and value > 0):
                raise DriveError(f"The limit {name} is {value}; it must be a number of seconds above 0.")


@dataclass(frozen=True)
class Question:
    id: str
    question: str


@dataclass
class Outcome:
    """How one question ended; `to_row` is its line in answers.jsonl."""

    id: str
    answer: str = ""
    status: Status = "answered"
    turns: int = 0
    tool_calls: int = 0
    refused_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


class ToolSession(Protocol):
    """What the driver needs from an MCP client session (mcp.ClientSession has both)."""

    async def list_tools(self, *, params: Any = None) -> Any: ...

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any: ...


CreateMessage = Callable[..., Awaitable[Any]]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
Record = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------------------------------------
# Inputs


def read_prompt(path: Path = PROMPT_PATH) -> tuple[str, str]:
    """The fixed system prompt and the sha256 of its bytes."""
    data = path.read_bytes()
    return data.decode("utf-8"), hashlib.sha256(data).hexdigest()


def system_prompt(prompt: str, server_name: str, instructions: str | None) -> str:
    """The prompt, then the server's instructions under its name, as an MCP client hands them to the model."""
    if not instructions:
        return prompt
    return f"{prompt.rstrip()}\n\n# MCP Server Instructions\n\n## {server_name}\n\n{instructions.strip()}\n"


def read_questions(path: Path) -> list[Question]:
    """Read id and question from each line; every other field (the expected answer) is left unread."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DriveError(f"Could not read the questions file {path.name} ({type(exc).__name__}).") from exc
    out: list[Question] = []
    seen: set[str] = set()
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            raise DriveError(f"Line {n} of {path.name} is not JSON.") from None
        qid = rec.get("id") if isinstance(rec, dict) else None
        text = rec.get("question") if isinstance(rec, dict) else None
        if not (isinstance(qid, str) and qid and isinstance(text, str) and text.strip()):
            raise DriveError(f"Line {n} of {path.name} needs a non-empty string id and question.")
        if qid in seen:
            raise DriveError(f"Line {n} of {path.name} repeats a question id. Give each question its own id.")
        seen.add(qid)
        out.append(Question(qid, text))
    if not out:
        raise DriveError(f"{path.name} holds no questions.")
    return out


def require_model(env: Mapping[str, str]) -> str:
    model = env.get("EVAL_MODEL", "").strip()
    if not model:
        raise DriveError(
            "EVAL_MODEL is not set. Set it to the id of the model that should answer; the driver has no default."
        )
    return model


def require_lake(env: Mapping[str, str]) -> Path:
    lake = env.get("HDR_LAKE", "").strip()
    if not lake:
        raise DriveError("HDR_LAKE is not set. Point it at the lake the server should read.")
    path = Path(lake).resolve()
    if not path.is_dir():
        raise DriveError(
            f"HDR_LAKE is {path}, which is not a directory. Point it at a lake (`make refresh` builds one)."
        )
    return path


def lake_snapshots(lake: Path) -> dict[str, str]:
    """The snapshot id each dataset's LATEST pointer names: which data the run was asked against."""
    base = lake / "datasets"
    if not base.is_dir():
        return {}
    return {
        p.parent.name: p.read_text(encoding="utf-8").strip()
        for p in sorted(base.glob("*/LATEST"))
        if p.is_file()
    }


def server_commit(server_dir: Path) -> tuple[str, bool]:
    """The server checkout's HEAD, and whether its tree has uncommitted changes."""

    try:
        head = run_git(server_dir, "rev-parse", "HEAD").strip()
        dirty = bool(run_git(server_dir, "status", "--porcelain").strip())
    except LabelCheckError:
        raise DriveError(
            f"{server_dir} is not a git checkout, so the server commit can't be recorded."
        ) from None
    return head, dirty


def server_env(env: Mapping[str, str]) -> dict[str, str]:
    """The variables passed to the server process beyond mcp's defaults (see SERVER_ENV_PASS)."""
    return {
        k: v
        for k, v in env.items()
        if (k.startswith(SERVER_ENV_PREFIX) and k not in SERVER_ENV_BLOCK) or k in SERVER_ENV_PASS
    }


# ---------------------------------------------------------------------------------------------------------
# Tools


async def list_all_tools(session: ToolSession) -> list[Any]:
    """Every tool the server lists, following its pages, in the server's order."""
    from mcp import types

    tools: list[Any] = []
    cursor: str | None = None
    while True:
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await session.list_tools(params=params)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return tools


def to_api_tools(tools: Sequence[Any], protocol: Protocol_) -> list[dict[str, Any]]:
    """MCP tools as Claude API tool definitions: name, description and input schema, unchanged and in order.
    Protocol B leaves out query_sql."""
    out = []
    for t in tools:
        if protocol == "B" and t.name == SQL_TOOL:
            continue
        out.append({"name": t.name, "description": t.description or "", "input_schema": dict(t.input_schema)})
    return out


def result_text(result: Any) -> str:
    """The text a tool returned: its text blocks joined by newlines; other blocks named by their type."""
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        parts.append(text if isinstance(text, str) else f"[{block.type} content]")
    return "\n".join(parts)


def refusal(name: str, protocol: Protocol_, offered: frozenset[str], calls_made: int, limits: Limits) -> str:
    """Why a tool call is not run, or "" when it may run. The text goes back to the model as an error result."""
    if protocol == "B" and name == SQL_TOOL:
        return f"{SQL_TOOL} is not available in this session. Answer with the other tools."
    if name not in offered:
        return f"There is no tool named {name!r}. Use one of the tools listed."
    if calls_made >= limits.calls:
        return (
            f"The limit of {limits.calls} tool calls for this question is reached. Answer with what you have."
        )
    return ""


# ---------------------------------------------------------------------------------------------------------
# The API


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def is_retryable(exc: Exception) -> bool:
    """Overload, rate limits, server errors and dropped connections; never a bad request or a bad key."""
    import anthropic

    if isinstance(exc, anthropic.APIConnectionError):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code in RETRY_STATUSES


def backoff(attempt: int, retry_after: float | None) -> float:
    """Seconds to wait before attempt `attempt + 1`: the server's retry-after, else 2, 4, 8 ... up to 60."""
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, BACKOFF_MAX_S)
    return min(BACKOFF_BASE_S * float(2**attempt), BACKOFF_MAX_S)


class DeadlineReached(Exception):
    """The question's deadline passed; the question ends with status "deadline"."""


async def create_with_retry(
    create: CreateMessage,
    request: dict[str, Any],
    *,
    deadline: float,
    limits: Limits,
    clock: Clock,
    sleep: Sleep,
) -> Any:
    """One API request, retried on overload and rate limits up to API_ATTEMPTS times, within the deadline."""
    attempt = 0
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise DeadlineReached
        try:
            with anyio.fail_after(min(limits.api_timeout_s, remaining)):
                return await create(**request)
        except TimeoutError:  # past the deadline, the wait below ends the question
            error: Exception = TimeoutError()
        except Exception as exc:  # the SDK's errors; anything not retryable stops the run
            if not is_retryable(exc):
                raise
            error = exc
        # A request that failed at or after the deadline ends the question, even on the last try: running out of
        # the question's time is not the API being down, so it must not stop the run.
        if clock() >= deadline:
            raise DeadlineReached
        if attempt == API_ATTEMPTS - 1:
            raise DriveError(
                f"The Claude API was still overloaded, rate limited or unreachable after {API_ATTEMPTS} tries "
                f"({type(error).__name__}). Run again later."
            ) from error
        wait = backoff(attempt, _retry_after(error))
        if clock() + wait >= deadline:
            raise DeadlineReached
        await sleep(wait)
        attempt += 1


def _usage(total: dict[str, int], usage: Any) -> None:
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


def _text(content: Sequence[Any]) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


# ---------------------------------------------------------------------------------------------------------
# One question


@dataclass(frozen=True)
class Setup:
    """What every question shares: the request's fixed parts, the protocol and the limits."""

    model: str
    system: str
    tools: list[dict[str, Any]]
    protocol: Protocol_
    limits: Limits

    @property
    def offered(self) -> frozenset[str]:
        return frozenset(t["name"] for t in self.tools)


async def ask(
    q: Question,
    setup: Setup,
    session: ToolSession,
    create: CreateMessage,
    record: Record,
    *,
    clock: Clock = time.monotonic,
    sleep: Sleep = anyio.sleep,
) -> Outcome:
    """Ask one question; `record` gets each tool call that reached the server. Returns how it ended."""
    limits = setup.limits
    out = Outcome(q.id)
    deadline = clock() + limits.question_timeout_s
    messages: list[dict[str, Any]] = [{"role": "user", "content": q.question}]
    try:
        while True:
            if out.turns >= limits.turns:
                out.status = "turn_cap"
                return out
            response = await create_with_retry(
                create,
                {
                    "model": setup.model,
                    "max_tokens": MAX_TOKENS,
                    "system": setup.system,
                    "tools": setup.tools,
                    "messages": messages,
                    "cache_control": {"type": "ephemeral"},
                },
                deadline=deadline,
                limits=limits,
                clock=clock,
                sleep=sleep,
            )
            out.turns += 1
            out.stop_reason = response.stop_reason
            _usage(out.usage, response.usage)
            # The whole content goes back unchanged, thinking blocks included.
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason != "tool_use":
                # Only a final turn's text is the answer. A question cut off by turn_cap or deadline keeps "",
                # so interim text ("Let me look up ...") is never scored as an answer.
                out.answer = _text(response.content)
                out.status = STOP_STATUS.get(response.stop_reason, "stopped")
                return out
            results = []
            for block in (b for b in response.content if b.type == "tool_use"):
                args = dict(block.input)  # the SDK parses tool input as a JSON object
                why = refusal(block.name, setup.protocol, setup.offered, out.tool_calls, limits)
                if why:
                    out.refused_calls.append({"tool": block.name, "args": args, "turn": out.turns})
                    text, is_error = why, True
                else:
                    out.tool_calls += 1
                    text, is_error, seconds = await run_tool(
                        session, block.name, args, deadline, limits, clock
                    )
                    record(
                        {
                            "id": q.id,
                            "tool": block.name,
                            "args": args,
                            "result": text,
                            "turn": out.turns,
                            "is_error": is_error,
                            "seconds": seconds,
                        }
                    )
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error}
                )
            messages.append({"role": "user", "content": results})
    except DeadlineReached:
        out.status = "deadline"
        return out


async def run_tool(
    session: ToolSession, name: str, args: dict[str, Any], deadline: float, limits: Limits, clock: Clock
) -> tuple[str, bool, float]:
    """Run one tool call within its timeout and the question's deadline: (text, is_error, seconds)."""
    from mcp.shared.exceptions import MCPError

    start = clock()
    budget = min(limits.tool_timeout_s, deadline - start)
    if budget <= 0:
        raise DeadlineReached
    try:
        with anyio.fail_after(budget):
            result = await session.call_tool(name, args)
        text, is_error = result_text(result), bool(result.is_error)
    except TimeoutError:  # recorded like any result; a passed deadline ends the question at the next request
        text, is_error = f"The tool did not answer within {budget:g} seconds.", True
    except MCPError as exc:  # the server refused the call itself (unknown arguments, say); the model sees why
        text, is_error = f"The tool call failed: {exc}", True
    return text, is_error, round(clock() - start, 3)


# ---------------------------------------------------------------------------------------------------------
# A run


def header(setup: Setup, **extra: Any) -> dict[str, Any]:
    return {
        HEADER_KEY: {
            "driver_version": DRIVER_VERSION,
            "model": setup.model,
            "protocol": setup.protocol,
            "tools_offered": [t["name"] for t in setup.tools],
            "limits": asdict(setup.limits),
            "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE_NOTE,
            **extra,
        }
    }


def _write(fh: TextIO, obj: Mapping[str, Any]) -> None:
    fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    fh.flush()


async def drive(
    questions: Sequence[Question],
    session: ToolSession,
    create: CreateMessage,
    *,
    model: str,
    prompt: str,
    prompt_sha256: str,
    server_name: str,
    instructions: str | None,
    protocol: Protocol_,
    limits: Limits,
    out_dir: Path,
    run_info: Mapping[str, Any],
    progress: TextIO,
    clock: Clock = time.monotonic,
    sleep: Sleep = anyio.sleep,
) -> list[Outcome]:
    """Ask every question in file order, one at a time, writing transcript.jsonl and answers.jsonl as it goes."""
    tools = to_api_tools(await list_all_tools(session), protocol)
    setup = Setup(model, system_prompt(prompt, server_name, instructions), tools, protocol, limits)
    head = header(
        setup,
        prompt_version=PROMPT_VERSION,
        prompt_sha256=prompt_sha256,
        server_name=server_name,
        server_instructions_sha256=hashlib.sha256((instructions or "").encode("utf-8")).hexdigest(),
        questions=len(questions),
        **run_info,
    )
    outcomes = []
    with (
        (out_dir / "transcript.jsonl").open("x", encoding="utf-8") as tr,
        (out_dir / "answers.jsonl").open("x", encoding="utf-8") as an,
    ):
        _write(tr, head)
        _write(an, head)
        for n, q in enumerate(questions, 1):
            outcome = await ask(
                q, setup, session, create, lambda rec: _write(tr, rec), clock=clock, sleep=sleep
            )
            _write(an, outcome.to_row())
            outcomes.append(outcome)
            progress.write(f"{n}/{len(questions)} done\n")
            progress.flush()
    return outcomes


Connect = Callable[
    [Path, Mapping[str, str], TextIO], AbstractAsyncContextManager[tuple[ToolSession, str, str | None]]
]


# Opens the read and write streams to a server process from its launch parameters (mcp's `stdio_client`).
Transport = Callable[..., AbstractAsyncContextManager[tuple[Any, Any]]]


@asynccontextmanager
async def stdio_connect(
    server_dir: Path, env: Mapping[str, str], errlog: TextIO, *, transport: Transport | None = None
) -> AsyncIterator[tuple[ToolSession, str, str | None]]:
    """Start `uv run --no-sync health-data-registry serve` in `server_dir`; yield the session, the server's name
    and its instructions. The server's stderr goes to `errlog`. `transport` is mcp's `stdio_client` unless a test
    passes its own."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command="uv",
        args=["run", "--no-sync", "health-data-registry", "serve"],
        cwd=server_dir,
        env=dict(env),
    )
    open_streams = stdio_client if transport is None else transport
    async with open_streams(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        yield session, init.server_info.name, init.instructions


async def run(
    questions: Sequence[Question],
    *,
    server_dir: Path,
    out_dir: Path,
    protocol: Protocol_,
    limits: Limits,
    env: Mapping[str, str],
    create: CreateMessage,
    progress: TextIO,
    connect: Connect = stdio_connect,
) -> list[Outcome]:
    """Record the run's inputs, start the server in `server_dir` on the lake HDR_LAKE names, and drive it."""
    model = require_model(env)
    lake = require_lake(env)
    commit, dirty = server_commit(server_dir)
    prompt, prompt_sha = read_prompt()
    with (out_dir / "server.log").open("w", encoding="utf-8") as errlog:
        async with connect(server_dir, server_env(env) | {"HDR_LAKE": str(lake)}, errlog) as (
            session,
            server_name,
            instructions,
        ):
            return await drive(
                questions,
                session,
                create,
                model=model,
                prompt=prompt,
                prompt_sha256=prompt_sha,
                server_name=server_name,
                instructions=instructions,
                protocol=protocol,
                limits=limits,
                out_dir=out_dir,
                run_info={
                    "server_commit": commit,
                    "server_dirty": dirty,
                    "lake_snapshots": lake_snapshots(lake),
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                progress=progress,
            )


def prepare_out(out_dir: Path) -> None:
    """Create `--out`; refuse one that already holds a run, so two runs never mix."""
    out_dir.mkdir(parents=True, exist_ok=True)
    taken = [n for n in ("transcript.jsonl", "answers.jsonl") if (out_dir / n).exists()]
    if taken:
        raise DriveError(f"{out_dir} already holds {', '.join(taken)}. Pick an empty --out directory.")


def explain(exc: BaseException) -> str:
    """A plain message for a failure, naming no question or answer: SDK errors by status, others by type. A
    failure inside the MCP client's task groups arrives wrapped in an exception group, so a group of one is
    unwrapped first."""
    import anthropic

    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]

    if isinstance(exc, DriveError):
        return str(exc)
    if isinstance(exc, anthropic.AuthenticationError | anthropic.CredentialsError):
        return "The Claude API did not accept the credentials. Check ANTHROPIC_API_KEY."
    if isinstance(exc, anthropic.NotFoundError):
        return "The Claude API does not serve the model EVAL_MODEL names. Check EVAL_MODEL."
    if isinstance(exc, anthropic.APIStatusError):
        return (
            f"The Claude API refused a request (status {exc.status_code}, {type(exc).__name__}). "
            "Check EVAL_MODEL and the request limits, then run the same command on DEV to see the cause."
        )
    return (
        f"The run stopped ({type(exc).__name__}). Details are withheld because they could quote a question. "
        "Run the same command on DEV to see the cause, or read drive.log in --out."
    )


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m eval.drive",
        description="Ask each question through the MCP server with the Claude API (eval/README.md).",
    )
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--questions", type=Path, help="jsonl with id and question on each line")
    which.add_argument(
        "--check",
        action="store_true",
        help="ask the first DEV question alone; exit 0 only if it was answered",
    )
    ap.add_argument(
        "--server-dir", required=True, type=Path, help="the server checkout to run (after uv sync)"
    )
    ap.add_argument("--out", required=True, type=Path, help="an empty directory for the run's files")
    ap.add_argument("--protocol", choices=("A", "B"), default="A", help="B hides and refuses query_sql")
    d = Limits()
    ap.add_argument(
        "--max-turns", type=int, default=d.turns, help="API requests per question (default %(default)s)"
    )
    ap.add_argument(
        "--max-calls", type=int, default=d.calls, help="tool calls per question (default %(default)s)"
    )
    ap.add_argument(
        "--tool-timeout",
        type=float,
        default=d.tool_timeout_s,
        help="seconds per tool call (default %(default)s)",
    )
    ap.add_argument(
        "--api-timeout",
        type=float,
        default=d.api_timeout_s,
        help="seconds per API request (default %(default)s)",
    )
    ap.add_argument(
        "--question-timeout",
        type=float,
        default=d.question_timeout_s,
        help="seconds per question, retries included (default %(default)s)",
    )
    return ap


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    create: CreateMessage | None = None,
    connect: Connect = stdio_connect,
) -> int:
    a = parser().parse_args(argv)
    env = os.environ if env is None else env
    try:
        require_model(env)
        require_lake(env)
        limits = Limits(a.max_turns, a.max_calls, a.tool_timeout, a.api_timeout, a.question_timeout)
        questions = read_questions(DEV_QUESTIONS)[:1] if a.check else read_questions(a.questions)
        prepare_out(a.out)
    except DriveError as exc:
        sys.stderr.write(f"drive: {exc}\n")
        return 2
    # Library log records and warnings (mcp, the SDK, pydantic, asyncio) can quote a request, so they go to a
    # file in --out, never stderr: a handler on the root logger also stops logging's last-resort handler from
    # writing to stderr, and captureWarnings sends warnings.warn through logging instead of to stderr.
    log = logging.FileHandler(a.out / "drive.log", encoding="utf-8")
    root = logging.getLogger()
    root.addHandler(log)
    logging.captureWarnings(True)
    try:
        if create is None:  # the real API
            import anthropic

            # Retries are the driver's own (create_with_retry), so the SDK's are off.
            create = anthropic.AsyncAnthropic(max_retries=0, timeout=limits.api_timeout_s).messages.create
        outcomes = anyio.run(
            lambda: run(
                questions,
                server_dir=a.server_dir.resolve(),
                out_dir=a.out,
                protocol=a.protocol,
                limits=limits,
                env=env,
                create=create,
                progress=sys.stdout,
                connect=connect,
            )
        )
    except Exception as exc:  # every message is ours or a type name: a library's could quote a question
        sys.stderr.write(f"drive: {explain(exc)}\n")
        return 1
    finally:
        logging.captureWarnings(False)
        root.removeHandler(log)
        log.close()
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    sys.stdout.write("drive: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + "\n")
    if a.check and outcomes[0].status != "answered":
        # DEV is public, so the status may be named; drive.log and server.log in --out say more.
        sys.stderr.write(
            f"drive: The DEV check question ended as {outcomes[0].status}, not answered. "
            "Read answers.jsonl, server.log and drive.log in --out.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
