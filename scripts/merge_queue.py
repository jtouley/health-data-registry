"""Whether main has a merge queue, so a pull request may defer mutation shards to it.

Usage (CI, the changes job): python scripts/merge_queue.py --repo OWNER/NAME [--branch main]

Prints `true` only when the rules in effect on the branch (GET /repos/{repo}/rules/branches/{branch}, read with the
run's own GITHUB_TOKEN) include a `merge_queue` rule. Anything else (no such rule, an API error, an unexpected
answer) prints `false`: then a pull request defers nothing, and every shard runs on it, slower but tested. Deferral
is only safe while a queue runs the deferred shards before the merge. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

REPO = re.compile(r"^(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+$")
BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")


def has_merge_queue(rules: Any) -> bool:
    """True only for a list of rules holding one of type `merge_queue`."""
    return isinstance(rules, list) and any(
        isinstance(r, dict) and r.get("type") == "merge_queue" for r in rules
    )


def fetch_rules(repo: str, branch: str, token: str) -> Any:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if urllib.parse.urlsplit(api).scheme != "https":
        raise ValueError(f"the API URL must be https: {api!r}")
    request = urllib.request.Request(  # noqa: S310 (https only, checked above)
        f"{api}/repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}?per_page=100",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https only)
        return json.load(response)


def main(argv: list[str] | None = None, fetch: Callable[[str, str, str], Any] = fetch_rules) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="main")
    a = ap.parse_args(argv)
    answer = False
    try:
        if not REPO.fullmatch(a.repo) or not BRANCH.fullmatch(a.branch):
            raise ValueError(f"not a repository and branch: {a.repo!r} {a.branch!r}")
        answer = has_merge_queue(fetch(a.repo, a.branch, os.environ.get("GITHUB_TOKEN", "")))
    except Exception as e:  # noqa: BLE001 (any failure means: no queue, so nothing defers)
        print(
            f"::warning::Could not read {a.branch}'s rules ({type(e).__name__}: {e}); nothing defers.",
            file=sys.stderr,
        )
    print("true" if answer else "false")
    print(
        f"{a.branch} {'has' if answer else 'has no'} merge queue: a pull request's mutation shards "
        + ("may defer to it." if answer else "all run on the pull request."),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
