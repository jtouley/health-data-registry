"""Whether main has a merge queue, so a pull request may run only the tests its change can reach.

Usage (CI, the changes job): python scripts/merge_queue.py --repo OWNER/NAME [--branch main]

Prints `true` only when the rules in effect on the branch (GET /repos/{repo}/rules/branches/{branch}, read with the
run's own GITHUB_TOKEN) include a `merge_queue` rule. Anything else (no such rule, an API error, an unexpected
answer) prints `false`: then a pull request runs everything (every test on Python 3.12 and 3.13, the coverage
floor, dbt and the kit) and reuses no pre-pr verdict. Narrowing a pull request's run is only safe while a queue runs
everything on the merge before it lands.

This answers whether a queue rule is in effect, not whether every merge goes through it: a ruleset bypass actor can
still merge from the pull request's page. Keep the ruleset that holds `merge_queue` free of bypass actors
(docs/dev/README.md#ci). Should a merge skip the queue anyway, main's own full run tests it right after.
Standard library only.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _github_api  # noqa: E402 - a sibling script, found through the line above

REPO = re.compile(r"^(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+$")
BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")


def has_merge_queue(rules: Any) -> bool:
    """True only for a list of rules holding one of type `merge_queue`."""
    return isinstance(rules, list) and any(
        isinstance(r, dict) and r.get("type") == "merge_queue" for r in rules
    )


def fetch_rules(repo: str, branch: str, token: str) -> Any:
    get = _github_api.get_json(token)
    return get(f"/repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}?per_page=100")


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
    except Exception as e:  # noqa: BLE001 (any failure means: no queue, so everything runs)
        print(
            f"::warning::Could not read {a.branch}'s rules ({type(e).__name__}: {e}); everything runs.",
            file=sys.stderr,
        )
    print("true" if answer else "false")
    print(
        f"{a.branch} {'has' if answer else 'has no'} merge queue: a pull request "
        + (
            "runs what its change can reach, and the queue runs everything before the merge."
            if answer
            else "runs everything."
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
