"""Check the hosted Space after a deploy: it built the commit just uploaded, it is healthy, and it answers.

Usage (CI; the verify-live job of .github/workflows/deploy-space.yml):
  python scripts/check_space.py --repo OWNER/SPACE --version X.Y.Z [--timeout 1200] [--interval 20]

Three checks, in order, all within --timeout seconds:

1. Build: the Space's runtime reports the Space repo's latest commit (the one the deploy just uploaded) with stage
   RUNNING. A build or start error on that commit fails at once; anything else (still building, the old container
   still serving) waits.
2. Health: GET https://<space host>/healthz answers {"status": "ok", "version": X.Y.Z}.
3. One known-good question: an MCP `tools/call` of `list_sources` over the streamable HTTP endpoint (/mcp)
   returns a result that is not an error and whose CSV starts with the `source_id` column.

It reads only public Hugging Face endpoints, sends nothing secret, and talks only to huggingface.co and the
Space's own *.hf.space host over HTTPS. Any failure prints a plain `::error::` line with the next step (roll back:
docs/dev/automation.md, "Roll back the Space") and exits 1. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

HUB_API = "https://huggingface.co/api/spaces"
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
# The Space's own host: one DNS label under hf.space, nothing else.
SPACE_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.hf\.space$")
RUNNING = "RUNNING"
# Stages that mean the uploaded commit will not serve without a fix (huggingface_hub SpaceStage).
FAILED_STAGES = frozenset({"BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR", "NO_APP_FILE"})
KNOWN_GOOD_TOOL = "list_sources"
KNOWN_GOOD_PREFIX = "source_id,"
ROLLBACK = 'Roll back to the last good release (docs/dev/automation.md, "Roll back the Space").'

GetJson = Callable[[str], Any]
PostText = Callable[[str, bytes, dict[str, str]], str]
Clock = Callable[[], float]
Sleep = Callable[[float], None]


class SpaceCheckFailed(Exception):
    """The Space is not serving the deployed release. The message is plain words with the next step."""


def _allowed(url: str) -> str:
    """`url`, when it is https to huggingface.co or a Space host; else ValueError."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not (host == "huggingface.co" or SPACE_HOST.fullmatch(host)):
        raise ValueError(f"refusing to contact {url!r}: only https to huggingface.co or a *.hf.space host")
    return url


def space_commit(get_json: GetJson, repo: str) -> str:
    """The Space repo's latest commit: the one a deploy just uploaded."""
    sha = str(get_json(f"{HUB_API}/{repo}").get("sha") or "")
    if not SHA.fullmatch(sha):
        raise SpaceCheckFailed(f"Hugging Face did not report a commit for the Space {repo}. Re-run this job.")
    return sha


def space_host(runtime: dict[str, Any]) -> str | None:
    """The Space's ready *.hf.space host from its runtime, or None."""
    for d in runtime.get("domains") or []:
        name = str(d.get("domain") or "")
        if d.get("stage") == "READY" and SPACE_HOST.fullmatch(name):
            return name
    return None


def wait_until_running(
    get_json: GetJson, repo: str, commit: str, *, deadline: float, clock: Clock, sleep: Sleep, interval: float
) -> str:
    """The Space's host, once its runtime runs `commit`. Fails at once on a build or start error for `commit`."""
    last = "no answer yet"
    while True:
        try:
            runtime = get_json(f"{HUB_API}/{repo}/runtime")
        except (OSError, ValueError) as e:  # a network blip or a bad body: ask again until the deadline
            last = f"the runtime could not be read ({type(e).__name__})"
        else:
            stage = str(runtime.get("stage") or "")
            mine = runtime.get("sha") == commit
            if mine and stage in FAILED_STAGES:
                raise SpaceCheckFailed(
                    f"The Space stopped with {stage} on the deployed commit {commit}. Read the Space's build and "
                    f"container logs on Hugging Face. {ROLLBACK}"
                )
            if mine and stage == RUNNING:
                host = space_host(runtime)
                if host:
                    return host
                last = "running, but no ready *.hf.space address"
            else:
                last = f"stage {stage or 'unknown'} on commit {runtime.get('sha') or 'unknown'}"
        if clock() >= deadline:
            raise SpaceCheckFailed(
                f"The Space did not start the deployed commit {commit} in time (last seen: {last}). {ROLLBACK}"
            )
        sleep(interval)


def wait_until_healthy(
    get_json: GetJson,
    host: str,
    version: str,
    *,
    deadline: float,
    clock: Clock,
    sleep: Sleep,
    interval: float,
) -> dict[str, Any]:
    """The /healthz body, once it says ok for `version`."""
    last = "no answer yet"
    while True:
        try:
            body = get_json(f"https://{host}/healthz")
        except (OSError, ValueError) as e:
            last = f"no healthy answer ({type(e).__name__})"
        else:
            if body.get("status") == "ok" and body.get("version") == version:
                return dict(body)
            last = f"status {body.get('status')!r}, version {body.get('version')!r}"
        if clock() >= deadline:
            raise SpaceCheckFailed(
                f"https://{host}/healthz did not report version {version} as ok in time (last seen: {last}). "
                f"{ROLLBACK}"
            )
        sleep(interval)


def mcp_result(text: str) -> dict[str, Any]:
    """The JSON-RPC message in a streamable HTTP response: plain JSON, or the last `data:` line of an SSE stream."""
    stripped = text.strip()
    if stripped.startswith("{"):
        message = json.loads(stripped)
    else:
        data = [line[len("data:") :].strip() for line in stripped.splitlines() if line.startswith("data:")]
        if not data:
            raise ValueError("no JSON-RPC message in the response")
        message = json.loads(data[-1])
    if not isinstance(message, dict):
        raise ValueError("the JSON-RPC message is not an object")
    return message


def ask_known_good_question(post: PostText, host: str) -> None:
    """One MCP tools/call of list_sources; fails unless it answers with the sources table."""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": KNOWN_GOOD_TOOL, "arguments": {}},
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    try:
        message = mcp_result(post(f"https://{host}/mcp", json.dumps(request).encode(), headers))
    except (OSError, ValueError) as e:
        raise SpaceCheckFailed(
            f"The Space did not answer the MCP question {KNOWN_GOOD_TOOL} ({type(e).__name__}). {ROLLBACK}"
        ) from e
    result = message.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise SpaceCheckFailed(f"The Space answered {KNOWN_GOOD_TOOL} with an error: {message}. {ROLLBACK}")
    texts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict)]
    if not texts or not str(texts[0]).startswith(KNOWN_GOOD_PREFIX):
        raise SpaceCheckFailed(
            f"The Space's answer to {KNOWN_GOOD_TOOL} is not the sources table it should be. {ROLLBACK}"
        )


def check_space(
    get_json: GetJson,
    post: PostText,
    repo: str,
    version: str,
    *,
    timeout: float,
    interval: float,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> str:
    """Run the three checks; returns the Space host. Raises SpaceCheckFailed with the reason."""
    if not REPO.fullmatch(repo):
        raise SpaceCheckFailed(f"{repo!r} is not a Space name (OWNER/NAME).")
    if not VERSION.fullmatch(version):
        raise SpaceCheckFailed(f"{version!r} is not a release version (X.Y.Z).")
    deadline = clock() + timeout
    commit = space_commit(get_json, repo)
    host = wait_until_running(
        get_json, repo, commit, deadline=deadline, clock=clock, sleep=sleep, interval=interval
    )
    wait_until_healthy(
        get_json, host, version, deadline=deadline, clock=clock, sleep=sleep, interval=interval
    )
    ask_known_good_question(post, host)
    return host


def _get_json(url: str) -> Any:
    request = urllib.request.Request(_allowed(url), headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https to allowed hosts only)
        return json.load(response)


def _post(url: str, body: bytes, headers: dict[str, str]) -> str:
    request = urllib.request.Request(_allowed(url), data=body, headers=headers, method="POST")  # noqa: S310
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (https to allowed hosts only)
        return str(response.read().decode("utf-8"))


def main(
    argv: list[str] | None = None,
    get_json: GetJson = _get_json,
    post: PostText = _post,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--repo", required=True, help="the Space, OWNER/NAME")
    ap.add_argument("--version", required=True, help="the release version the Space should report, X.Y.Z")
    ap.add_argument("--timeout", type=float, default=1200, help="seconds for all three checks (default 1200)")
    ap.add_argument("--interval", type=float, default=20, help="seconds between polls (default 20)")
    a = ap.parse_args(argv)
    try:
        host = check_space(
            get_json,
            post,
            a.repo,
            a.version,
            timeout=a.timeout,
            interval=a.interval,
            clock=clock,
            sleep=sleep,
        )
    except SpaceCheckFailed as e:
        print(f"::error::Space check failed: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 (anything unexpected is a failed check, never a pass)
        print(f"::error::Space check failed: the check itself broke ({type(e).__name__}: {e}). {ROLLBACK}")
        return 1
    print(f"https://{host} runs {a.version}, is healthy, and answered {KNOWN_GOOD_TOOL}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
