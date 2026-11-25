"""Branch hygiene: archive-tag and delete inactive branches, purge listed ones, tag merged PR branches (issue #67).

Usage (from .github/workflows/branch-hygiene.yml; GH_TOKEN in the environment):
  python scripts/branch_hygiene.py run --repo OWNER/NAME [--dry-run] --report report.md --changes changes.md
  python scripts/branch_hygiene.py tag-merged --repo OWNER/NAME --branch B --sha SHA --pr N

`run` reads every branch from the GitHub API and decides, for each one, to keep, archive or purge it:

- keep: the default branch, protected branches, branches that are the head or base of an open pull request,
  dependabot/*, the `keep` list in config/branch_keep.yaml, branch names outside a plain character set, and
  branches with a commit in the last 7 days (unless a wip/* backup whose pull request has merged);
- archive: create the annotated tag archive/<branch>/<YYYY-MM-DD> at the branch head, check that the tag
  resolves to that same commit, check that the branch has not moved, and only then delete the branch;
- purge: delete with no tag (the `purge` list: content that must not stay in the repo).

A branch whose tag can't be created or verified is reported and never deleted. Every step is idempotent: a
second run on the same day finds the tag already in place, checks it, and goes on. `--dry-run` changes nothing
and reports what a live run would do. The report is Markdown for the tracking issue; branch names in it are
limited to [A-Za-z0-9._/-], so they can't format the issue or mention anyone.

`tag-merged` tags the head of a merged pull request's branch (archive/<branch>/<date>), so every branch the
repository auto-deletes on merge keeps an archive tag. It never deletes anything, and skips purge-listed branches.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "branch_keep.yaml"
INACTIVE_DAYS = 7
# Branch names this tool will act on: plain characters only, so a name is safe in a tag name, a URL path and
# the issue report. Any other name is kept and reported.
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MAX_REPORT = 60_000  # GitHub caps an issue body at 65,536 characters
MAX_TAG_DEPTH = 5


class ApiError(Exception):
    """A GitHub API call that did not succeed; `status` is the HTTP status (0 when no response came)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Api(Protocol):
    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Call the API at `path` (starting with /) and return the decoded JSON (None for an empty body)."""
        ...


class GitHubApi:
    """The GitHub REST API over HTTPS with a token. Tests use a fake with the same `request` method."""

    def __init__(self, token: str, base: str = "https://api.github.com") -> None:
        if not base.startswith("https://"):
            raise ValueError("the GitHub API URL must use HTTPS")
        if not token:
            raise ValueError("no token: set GH_TOKEN")
        self.base = base.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:  # pragma: no mutate
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 - the base URL is checked to be HTTPS above
            self.base + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "branch-hygiene",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - HTTPS only, see above
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, f"{method} {path}: HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise ApiError(0, f"{method} {path}: {e.reason}") from None
        return json.loads(raw) if raw else None


def q(name: str) -> str:
    """A ref name as a URL path segment (slashes kept, everything else quoted)."""
    return urllib.parse.quote(name, safe="/")


def get_all(api: Api, path: str) -> list[Any]:
    """Every item of a paged list endpoint, 100 per page."""
    items: list[Any] = []
    sep = "&" if "?" in path else "?"
    page = 1
    while True:
        batch = api.request("GET", f"{path}{sep}per_page=100&page={page}")
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def safe_name(name: str) -> bool:
    """True for a branch name this tool will act on (and write into a tag name and the report)."""
    return (
        SAFE_NAME.fullmatch(name) is not None
        and ".." not in name
        and "//" not in name
        and "/." not in name
        and not name.endswith((".", "/", ".lock"))
    )


def plain(text: str) -> str:
    """Free text (a reason or an error) on one line, with nothing that could format the report or mention."""
    return re.sub(r"[\r\n`@<>]|::", " ", text)[:300]


def shown(name: str) -> str:
    """A name as it may appear in the report: unsafe characters replaced, cut short."""
    return re.sub(r"[^A-Za-z0-9._/-]", "?", name)[:100]


@dataclass(frozen=True)
class Config:
    keep: dict[str, str]  # pattern -> reason
    purge: dict[str, str]  # exact name -> reason

    def kept_by(self, name: str) -> str | None:
        for pattern, reason in self.keep.items():
            if fnmatch.fnmatchcase(name, pattern):
                return reason
        return None


def load_config(path: Path) -> Config:
    """config/branch_keep.yaml. Strict: every entry has a name and a reason, purge names are exact."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or set(data) - {"keep", "purge"}:
        raise ValueError(f"{path}: only `keep` and `purge` lists are allowed")
    out: dict[str, dict[str, str]] = {"keep": {}, "purge": {}}
    for section in ("keep", "purge"):
        for entry in data.get(section) or []:
            if not isinstance(entry, dict) or not entry.get("name") or not entry.get("reason"):
                raise ValueError(f"{path}: each `{section}` entry needs a name and a reason")
            name = str(entry["name"])
            if section == "purge" and not safe_name(name):
                raise ValueError(f"{path}: purge entry {shown(name)!r} is not a plain branch name")
            out[section][name] = str(entry["reason"])
    return Config(keep=out["keep"], purge=out["purge"])


@dataclass(frozen=True)
class RepoState:
    default_branch: str
    open_heads: frozenset[str]
    open_bases: frozenset[str]


@dataclass(frozen=True)
class Decision:
    branch: str
    sha: str
    action: str  # keep | archive | purge
    reason: str
    committed_at: str = ""
    author: str = ""
    subject: str = ""


@dataclass(frozen=True)
class Outcome:
    branch: str
    action: str  # archived | purged | failed | would-archive | would-purge
    detail: str
    tag: str = ""


def read_state(api: Api, repo: str) -> RepoState:
    default = api.request("GET", f"/repos/{repo}")["default_branch"]
    pulls = get_all(api, f"/repos/{repo}/pulls?state=open")
    return RepoState(
        default_branch=str(default),
        open_heads=frozenset(str(p["head"]["ref"]) for p in pulls),
        open_bases=frozenset(str(p["base"]["ref"]) for p in pulls),
    )


def exclusion(name: str, protected: bool, state: RepoState, cfg: Config) -> str | None:
    """Why a branch is never touched, or None. Checked before anything else, the purge list included."""
    if name == state.default_branch:
        return "default branch"
    if protected:
        return "protected branch"
    if name in state.open_heads:
        return "has an open pull request"
    if name in state.open_bases:
        return "base of an open pull request"
    if name.startswith("dependabot/"):
        return "dependabot branch"
    kept = cfg.kept_by(name)
    if kept is not None:
        return f"keep list: {kept}"
    if not safe_name(name):
        return "unusual branch name: left for a maintainer"
    return None


def merged_pr(api: Api, repo: str, sha: str) -> int | None:
    """The merged pull request this commit belongs to, if it belongs to no open one."""
    pulls = api.request("GET", f"/repos/{repo}/commits/{sha}/pulls")
    if any(p.get("state") == "open" for p in pulls):
        return None
    merged = sorted(int(p["number"]) for p in pulls if p.get("merged_at"))
    return merged[-1] if merged else None


def decide(
    api: Api, repo: str, branch: dict[str, Any], state: RepoState, cfg: Config, now: datetime
) -> Decision:
    name = str(branch["name"])
    sha = str(branch["commit"]["sha"])
    reason = exclusion(name, bool(branch.get("protected")), state, cfg)
    if reason is not None:
        return Decision(name, sha, "keep", reason)
    if name in cfg.purge:
        return Decision(name, sha, "purge", f"purge list: {cfg.purge[name]}")
    commit = api.request("GET", f"/repos/{repo}/commits/{sha}")["commit"]
    when = datetime.fromisoformat(commit["committer"]["date"].replace("Z", "+00:00"))
    info = {
        "committed_at": when.date().isoformat(),
        "author": str(commit["author"]["name"]),
        "subject": str(commit["message"]).split("\n", 1)[0][:200],
    }
    if when < now - timedelta(days=INACTIVE_DAYS):
        return Decision(name, sha, "archive", f"no commit since {info['committed_at']}", **info)
    if name.startswith("wip/"):
        pr = merged_pr(api, repo, sha)
        if pr is not None:
            return Decision(name, sha, "archive", f"wip backup of merged pull request #{pr}", **info)
    return Decision(name, sha, "keep", f"active: last commit {info['committed_at']}", **info)


def plan(api: Api, repo: str, cfg: Config, now: datetime) -> list[Decision]:
    state = read_state(api, repo)
    branches = get_all(api, f"/repos/{repo}/branches")
    return [decide(api, repo, b, state, cfg, now) for b in sorted(branches, key=lambda b: str(b["name"]))]


def tag_name(branch: str, day: str) -> str:
    return f"archive/{branch}/{day}"


def peel(api: Api, repo: str, tag: str) -> str | None:
    """The commit a tag resolves to, or None when the tag does not exist."""
    try:
        obj = api.request("GET", f"/repos/{repo}/git/ref/tags/{q(tag)}")["object"]
    except ApiError as e:
        if e.status == 404:
            return None
        raise
    for _ in range(MAX_TAG_DEPTH):
        if obj["type"] == "commit":
            return str(obj["sha"])
        if obj["type"] != "tag":
            raise ApiError(0, f"tag {tag} points at a {obj['type']}, not a commit")
        obj = api.request("GET", f"/repos/{repo}/git/tags/{obj['sha']}")["object"]
    raise ApiError(0, f"tag {tag} is nested too deeply")


def ensure_tag(api: Api, repo: str, tag: str, sha: str, message: str) -> str | None:
    """Create the annotated tag at `sha` unless it exists, then verify it resolves to `sha`.

    Returns None when the tag is in place and verified, else why not (in plain words). Never moves a tag.
    """
    try:
        if peel(api, repo, tag) is None:
            obj = api.request(
                "POST",
                f"/repos/{repo}/git/tags",
                {"tag": tag, "message": message, "object": sha, "type": "commit"},
            )
            try:
                api.request("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/tags/{tag}", "sha": obj["sha"]})
            except ApiError as e:
                if e.status != 422:  # 422: the ref appeared meanwhile; the check below decides
                    raise
        got = peel(api, repo, tag)
    except ApiError as e:
        return f"could not create or read the tag {tag} ({e})"
    if got != sha:
        return f"tag {tag} resolves to {got or 'nothing'}, not the branch head {sha}"
    return None


def head_now(api: Api, repo: str, branch: str) -> str | None:
    """The branch's current head commit, or None when the branch is gone."""
    try:
        return str(api.request("GET", f"/repos/{repo}/branches/{q(branch)}")["commit"]["sha"])
    except ApiError as e:
        if e.status == 404:
            return None
        raise


def delete_branch(api: Api, repo: str, d: Decision) -> str | None:
    """Delete the branch if it still points at the planned commit. None when deleted (or already gone)."""
    try:
        now_sha = head_now(api, repo, d.branch)
        if now_sha is None:
            return None
        if now_sha != d.sha:
            return f"the branch moved to {now_sha} after the plan; kept"
        api.request("DELETE", f"/repos/{repo}/git/refs/heads/{q(d.branch)}")
    except ApiError as e:
        return f"could not delete the branch ({e})"
    return None


def tag_message(d: Decision) -> str:
    return (
        f"Archive of branch {d.branch}\n\n"
        f"Last commit: {d.sha} {d.subject}\n"
        f"Author: {d.author}, committed {d.committed_at}\n"
        f"Reason: {d.reason}\n"
    )


def apply(api: Api, repo: str, decisions: list[Decision], day: str, dry_run: bool) -> list[Outcome]:
    out: list[Outcome] = []
    for d in decisions:
        if d.action == "archive":
            tag = tag_name(d.branch, day)
            if dry_run:
                out.append(Outcome(d.branch, "would-archive", d.reason, tag))
                continue
            err = ensure_tag(api, repo, tag, d.sha, tag_message(d))
            if err is None:
                err = delete_branch(api, repo, d)
            out.append(Outcome(d.branch, "failed" if err else "archived", err or d.reason, tag))
        elif d.action == "purge":
            if dry_run:
                out.append(Outcome(d.branch, "would-purge", d.reason))
                continue
            err = delete_branch(api, repo, d)
            out.append(Outcome(d.branch, "failed" if err else "purged", err or d.reason))
    return out


def restore_command(branch: str, tag: str) -> str:
    return (
        f'git fetch origin "refs/tags/{tag}:refs/tags/{tag}" && '
        f'git push origin "refs/tags/{tag}^{{commit}}:refs/heads/{branch}"'
    )


HEADINGS = {
    "archived": "Archived (tagged, verified, then deleted)",
    "purged": "Purged (deleted with no tag)",
    "failed": "Not deleted: needs a maintainer",
    "would-archive": "Would archive (dry run: nothing changed)",
    "would-purge": "Would purge (dry run: nothing changed)",
}


def changes(outcomes: list[Outcome]) -> str:
    """Markdown lines for what this run did or would do; empty when nothing."""
    lines: list[str] = []
    for action, heading in HEADINGS.items():
        rows = [o for o in outcomes if o.action == action]
        if not rows:
            continue
        lines += ["", f"### {heading}", ""]
        for o in rows:
            line = f"- `{shown(o.branch)}`: {plain(o.detail)}"
            if o.tag:
                line += f"; tag `{shown(o.tag)}`"
            lines.append(line)
            if o.action == "archived":
                lines.append(f"  - restore: `{restore_command(o.branch, o.tag)}`")
    return "\n".join(lines).strip()


def report(
    decisions: list[Decision], outcomes: list[Outcome], *, dry_run: bool, run_url: str, day: str
) -> str:
    mode = "dry run (nothing changed)" if dry_run else "live"
    kept = [d for d in decisions if d.action == "keep"]
    body = [
        f"Branch hygiene, {day}, {mode}. Run: {run_url or '(local)'}",
        "",
        "How it works and how to restore a branch: `docs/dev/branch-hygiene.md`. A restore is:",
        "",
        "```bash",
        restore_command("<branch>", "archive/<branch>/<date>"),
        "```",
        "",
        changes(outcomes) or "Nothing to archive or purge today.",
        "",
        f"<details><summary>Kept: {len(kept)} branch(es)</summary>",
        "",
        *[f"- `{shown(d.branch)}`: {plain(d.reason)}" for d in kept],
        "",
        "</details>",
    ]
    text = "\n".join(body) + "\n"
    if len(text) > MAX_REPORT:
        text = text[:MAX_REPORT] + "\n\n(cut short; the full list is in the run log)\n"
    return text


def tag_merged(api: Api, repo: str, branch: str, sha: str, pr: int, cfg: Config, day: str) -> Outcome:
    """Tag a merged pull request's head so the auto-deleted branch keeps an archive tag. Never deletes."""
    if branch in cfg.purge:
        return Outcome(branch, "skipped", "purge list: never tagged")
    if not safe_name(branch) or SHA.fullmatch(sha) is None:
        return Outcome(branch, "failed", "unusual branch name or commit id: not tagged")
    tag = tag_name(branch, day)
    d = Decision(branch, sha, "archive", f"merged in pull request #{pr}")
    try:
        commit = api.request("GET", f"/repos/{repo}/commits/{sha}")["commit"]
        d = Decision(
            branch,
            sha,
            "archive",
            d.reason,
            committed_at=str(commit["committer"]["date"])[:10],
            author=str(commit["author"]["name"]),
            subject=str(commit["message"]).split("\n", 1)[0][:200],
        )
    except ApiError:
        pass  # the tag message then lacks the commit details; the tag itself is what matters
    err = ensure_tag(api, repo, tag, sha, tag_message(d))
    return Outcome(branch, "failed" if err else "archived", err or d.reason, tag)


def main(argv: list[str] | None = None, api_factory: Callable[[], Api] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="archive or purge branches (live unless --dry-run)")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--report", type=Path, required=True, help="write the tracking-issue body here")
    run.add_argument("--changes", type=Path, required=True, help="write what changed here (empty if nothing)")
    run.add_argument("--run-url", default="")
    tm = sub.add_parser("tag-merged", help="tag a merged pull request's branch head")
    tm.add_argument("--branch", required=True)
    tm.add_argument("--sha", required=True)
    tm.add_argument("--pr", type=int, required=True)
    for p in (run, tm):
        p.add_argument("--repo", required=True)
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = ap.parse_args(argv)
    if REPO.fullmatch(a.repo) is None:
        ap.error("--repo must be OWNER/NAME")
    cfg = load_config(a.config)
    api = (
        api_factory()
        if api_factory
        else GitHubApi(
            os.environ.get("GH_TOKEN", ""), os.environ.get("GITHUB_API_URL", "https://api.github.com")
        )
    )
    now = datetime.now(UTC)
    day = now.date().isoformat()

    if a.cmd == "tag-merged":
        o = tag_merged(api, a.repo, a.branch, a.sha, a.pr, cfg, day)
        print(f"{shown(o.branch)}: {o.action}: {plain(o.detail)} {shown(o.tag)}".rstrip())
        return 1 if o.action == "failed" else 0

    decisions = plan(api, a.repo, cfg, now)
    outcomes = apply(api, a.repo, decisions, day, a.dry_run)
    a.report.write_text(report(decisions, outcomes, dry_run=a.dry_run, run_url=a.run_url, day=day), "utf-8")
    a.changes.write_text(changes(outcomes), encoding="utf-8")
    for o in outcomes:
        print(f"{o.action}: {shown(o.branch)}: {plain(o.detail)}")
        if o.action == "failed":
            print(f"::warning::{shown(o.branch)} was not deleted: {plain(o.detail)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
