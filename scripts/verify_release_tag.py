"""Refuse a release unless its tag names a commit on main that passed CI there.

Usage (CI; see .github/workflows/verify-release-tag.yml, called by release.yml, npm-publish.yml and deploy-space.yml):
  python scripts/verify_release_tag.py --repo OWNER/NAME --tag vX.Y.Z [--sha COMMIT] [--github-output FILE]

One rule for every way a release leaves the repository (PyPI, npm, the hosted Space). The release goes ahead only
when all of these hold:

- the tag is a release tag `vX.Y.Z`;
- the tag exists in this repository and names a commit (an annotated tag is followed to its commit); when --sha is
  given (the commit the workflow run was started for), the tag must name exactly that commit, so a tag moved after
  the run started is refused;
- that commit is main's head or one of its ancestors (GitHub's compare: `identical` or `behind`);
- CI (.github/workflows/ci.yml) has a successful run for that commit, triggered by a push to main of this
  repository.

Anything else, including any API or network error, is a refusal: the script prints a plain `::error::` line saying
what is wrong and what to do, and exits 1. On success it prints the commit and, with --github-output, writes
`sha=<commit>` there for later jobs. The token is the run's own GITHUB_TOKEN with `contents: read` and
`actions: read`. Standard library only, so the job needs no project install.

The workflow that runs this comes from the tagged commit itself when a tag push starts it, so a tag on an
unreviewed commit could carry an edited copy. This check stops mistakes; the tag ruleset and the `pypi`, `npm` and
`space` environments' deployment rules (docs/dev/automation.md) are what stop a deliberate bypass.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

CI_WORKFLOW_FILE = "ci.yml"
CI_WORKFLOW_PATH = f".github/workflows/{CI_WORKFLOW_FILE}"
MAIN = "main"
TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ON_MAIN = frozenset({"identical", "behind"})
# An annotated tag may point at another tag; a few hops is plenty, and the loop must end.
MAX_TAG_HOPS = 5
PER_PAGE = 100
MAX_PAGES = 5

GetJson = Callable[[str], Any]


class Refused(Exception):
    """The release must not go ahead. The message is plain words with the next step."""


def tag_commit(get_json: GetJson, repo: str, tag: str) -> str:
    """The commit `tag` names in `repo`, following annotated tags."""
    try:
        obj = get_json(f"/repos/{repo}/git/ref/tags/{tag}")["object"]
    except LookupError as e:
        raise Refused(f"There is no tag {tag} in {repo}. Push the tag first, then run this again.") from e
    for _ in range(MAX_TAG_HOPS):
        if obj.get("type") == "commit":
            sha = str(obj.get("sha") or "")
            if not SHA.fullmatch(sha):
                break
            return sha
        if obj.get("type") != "tag":
            break
        obj = get_json(f"/repos/{repo}/git/tags/{obj['sha']}")["object"]
    raise Refused(f"Tag {tag} does not name a commit. Delete it and tag a commit on main.")


def is_on_main(get_json: GetJson, repo: str, sha: str) -> bool:
    """True when `sha` is main's head or one of its ancestors."""
    status = get_json(f"/repos/{repo}/compare/{MAIN}...{sha}").get("status")
    return status in ON_MAIN


def ci_passed_on_main(get_json: GetJson, repo: str, sha: str) -> bool:
    """True when CI has a successful push run on main of this repository for exactly `sha`."""
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode(
            {"head_sha": sha, "branch": MAIN, "event": "push", "per_page": PER_PAGE, "page": page}
        )
        body = get_json(f"/repos/{repo}/actions/workflows/{CI_WORKFLOW_FILE}/runs?{query}")
        runs = body.get("workflow_runs") or []
        for run in runs:
            if (
                run.get("head_sha") == sha
                and run.get("head_branch") == MAIN
                and run.get("event") == "push"
                and run.get("path") == CI_WORKFLOW_PATH
                and run.get("status") == "completed"
                and run.get("conclusion") == "success"
                and (run.get("repository") or {}).get("full_name") == repo
                and (run.get("head_repository") or {}).get("full_name") == repo
            ):
                return True
        if len(runs) < PER_PAGE:
            break
    return False


def verify(get_json: GetJson, repo: str, tag: str, sha: str = "") -> str:
    """The commit `tag` names, when it may be released; raises Refused otherwise."""
    if not REPO.fullmatch(repo):
        raise Refused(f"{repo!r} is not a repository name (OWNER/NAME).")
    if not TAG.fullmatch(tag):
        raise Refused(f"{tag!r} is not a release tag. Release tags look like v1.2.3.")
    if sha and not SHA.fullmatch(sha):
        raise Refused(f"{sha!r} is not a full commit SHA.")
    commit = tag_commit(get_json, repo, tag)
    if sha and commit != sha:
        raise Refused(
            f"Tag {tag} now names {commit}, but this run was started for {sha}. A release tag must never move: "
            "find out who moved it before releasing anything."
        )
    if not is_on_main(get_json, repo, commit):
        raise Refused(
            f"Tag {tag} names {commit}, which is not on main. Merge the change through a pull request, then tag "
            "the merged commit on main."
        )
    if not ci_passed_on_main(get_json, repo, commit):
        raise Refused(
            f"CI has not passed on main for {commit} (tag {tag}). Wait for CI on main to finish green, or fix "
            "main and tag the fixed commit."
        )
    return commit


def github_get_json(api_url: str, token: str) -> GetJson:
    """A GetJson for the GitHub REST API at api_url (https only), authorised with token. A 404 is a KeyError."""
    if urllib.parse.urlsplit(api_url).scheme != "https":
        raise ValueError(f"the API URL must be https: {api_url!r}")
    base = api_url.rstrip("/")

    def get(path: str) -> Any:
        request = urllib.request.Request(  # noqa: S310 (https only, checked above)
            base + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https only)
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise KeyError(path) from e
            raise

    return get


def main(argv: list[str] | None = None, get_json: GetJson | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--repo", required=True, help="OWNER/NAME of this repository (github.repository)")
    ap.add_argument("--tag", required=True, help="the release tag, vX.Y.Z")
    ap.add_argument("--sha", default="", help="the commit this run was started for; the tag must name it")
    ap.add_argument("--github-output", default="", help="append sha=<commit> to this file ($GITHUB_OUTPUT)")
    a = ap.parse_args(argv)
    try:
        if get_json is None:
            token = os.environ.get("GITHUB_TOKEN", "")
            if not token:
                raise Refused("No GITHUB_TOKEN, so the tag could not be checked. Nothing was released.")
            get_json = github_get_json(os.environ.get("GITHUB_API_URL", "https://api.github.com"), token)
        commit = verify(get_json, a.repo, a.tag, a.sha)
    except Refused as e:
        print(f"::error::Not released: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 (any other failure is a refusal too: fail closed)
        print(
            f"::error::Not released: GitHub could not be asked whether {a.tag} is on main and passed CI "
            f"({type(e).__name__}). Re-run the workflow; if it fails again, check GitHub's status page."
        )
        return 1
    if a.github_output:
        with open(a.github_output, "a", encoding="utf-8") as out:
            out.write(f"sha={commit}\n")
    print(f"{a.tag} ({commit}) is on main and passed CI there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
