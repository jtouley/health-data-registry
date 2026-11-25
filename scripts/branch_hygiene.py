"""Branch hygiene: archive-tag, verify, then delete finished and inactive branches (issue #67).

Usage (from .github/workflows/branch-hygiene.yml; GH_TOKEN in the environment):
  python scripts/branch_hygiene.py run --repo OWNER/NAME [--dry-run] --report report.md --changes changes.md
  python scripts/branch_hygiene.py pr-closed --repo OWNER/NAME --branch B --sha SHA --pr N [--merged] --report r.md
  python scripts/branch_hygiene.py probe-done --repo OWNER/NAME --branch B --sha SHA --report r.md

`run` (the daily sweep, live unless --dry-run) reads every branch and decides to keep, archive or purge it.

Never touched: the default branch; a branch that is protected, by branch protection or by a ruleset that
restricts deletion (checked through the API for each branch, never assumed); the head or base of an open pull
request, and the pre-pr/<head> twin of an open pull request's head; dependabot/*; the `keep` list in
config/branch_keep.yaml; a name outside [A-Za-z0-9._/-]; and any branch pushed to or committed to in the last
24 hours.

Archived (in this order): a CI probe branch (the `probes` patterns); a pre-pr/* or wip/* branch whose head
commit belongs only to closed pull requests (merged or abandoned); any branch with no push and no commit for
7 days. Purged: the `purge` list, deleted with no tag because its content must not stay in the repo.

Archiving a branch: create the annotated tag archive/<branch>/<YYYY-MM-DD> at its head (or reuse that tag; a
tag is never moved), read the tag back and check that it resolves to the head, re-read the branch and its
protection, check it has not moved, and only then delete it. A branch whose tag can't be verified, or that
moved or became protected, is reported and never deleted. Every step is idempotent.

`pr-closed` runs when a pull request closes: when it merged, it tags the head (the repository auto-deletes the
branch); either way it archives the pull request's pre-pr/<branch> twin, if the twin holds only this pull
request's commits. `probe-done` runs after CI finishes on a probe branch and archives it, if it still points at
the commit CI ran on. The protections above apply to both, except the 24-hour rule: these two act on an event
that says the branch's work is done.

Reports are Markdown (the tracking issue, the job summary): branch names are limited to [A-Za-z0-9._/-] and
free text loses backticks, @ and angle brackets, so a report can't format the issue or mention anyone.
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
INACTIVE = timedelta(days=7)
FRESH = timedelta(hours=24)
TWIN = "pre-pr/"
# Branch names this tool will act on: plain characters only, so a name is safe in a tag name, a URL path and
# a report. Any other name is kept and reported.
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MAX_REPORT = 60_000  # GitHub caps an issue body at 65,536 characters
MAX_TAG_DEPTH = 5
GONE = "gone"  # check_deletable: the branch no longer exists


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
    """True for a branch name this tool will act on (and write into a tag name and a report)."""
    return (
        SAFE_NAME.fullmatch(name) is not None
        and ".." not in name
        and "//" not in name
        and "/." not in name
        and not name.endswith((".", "/", ".lock"))
    )


def plain(text: str) -> str:
    """Free text (a reason or an error) on one line, with nothing that could format a report or mention."""
    return re.sub(r"[\r\n`@<>]|::", " ", text)[:300]


def shown(name: str) -> str:
    """A name as it may appear in a report: unsafe characters replaced, cut short."""
    return re.sub(r"[^A-Za-z0-9._/-]", "?", name)[:100]


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Config:
    keep: dict[str, str]  # glob -> reason
    purge: dict[str, str]  # exact name -> reason
    probes: dict[str, str]  # glob -> reason

    def kept_by(self, name: str) -> str | None:
        return next((r for p, r in self.keep.items() if fnmatch.fnmatchcase(name, p)), None)

    def probe(self, name: str) -> str | None:
        return next((r for p, r in self.probes.items() if fnmatch.fnmatchcase(name, p)), None)


SECTIONS = ("keep", "purge", "probes")


def load_config(path: Path) -> Config:
    """config/branch_keep.yaml. Strict: known lists only, every entry has a name and a reason, purge names exact."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or set(data) - set(SECTIONS):
        raise ValueError(f"{path}: only the lists {', '.join(SECTIONS)} are allowed")
    out: dict[str, dict[str, str]] = {s: {} for s in SECTIONS}
    for section in SECTIONS:
        for entry in data.get(section) or []:
            if not isinstance(entry, dict) or not entry.get("name") or not entry.get("reason"):
                raise ValueError(f"{path}: each `{section}` entry needs a name and a reason")
            name = str(entry["name"])
            if section == "purge" and not safe_name(name):
                raise ValueError(f"{path}: purge entry {shown(name)!r} is not a plain branch name")
            if section == "probes" and name.strip("*") == "":
                raise ValueError(f"{path}: probe pattern {name!r} would match every branch")
            out[section][name] = str(entry["reason"])
    return Config(keep=out["keep"], purge=out["purge"], probes=out["probes"])


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
    action: str  # archived | purged | failed | kept | skipped | would-archive | would-purge
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
    if name.startswith(TWIN) and name[len(TWIN) :] in state.open_heads:
        return "pre-pr twin of an open pull request"
    if name.startswith("dependabot/"):
        return "dependabot branch"
    kept = cfg.kept_by(name)
    if kept is not None:
        return f"keep list: {kept}"
    if not safe_name(name):
        return "unusual branch name: left for a maintainer"
    return None


def deletion_rules(api: Api, repo: str, branch: str) -> str | None:
    """Why a ruleset forbids deleting the branch, or None. An unreadable answer counts as forbidden."""
    try:
        rules = api.request("GET", f"/repos/{repo}/rules/branches/{q(branch)}")
    except ApiError as e:
        return f"could not read the rulesets for the branch ({e})"
    if any(r.get("type") == "deletion" for r in rules):
        return "a ruleset forbids deleting it"
    return None


def check_deletable(api: Api, repo: str, branch: str, sha: str) -> str | None:
    """Right before a delete: None when the branch still points at `sha` and nothing protects it; GONE when it
    no longer exists; otherwise why it must be kept. Every error keeps the branch."""
    try:
        b = api.request("GET", f"/repos/{repo}/branches/{q(branch)}")
    except ApiError as e:
        return GONE if e.status == 404 else f"could not read the branch ({e})"
    if b.get("protected"):
        return "the branch is protected"
    head = str(b["commit"]["sha"])
    if head != sha:
        return f"the branch moved to {head}; kept"
    return deletion_rules(api, repo, branch)


def commit_info(api: Api, repo: str, sha: str) -> dict[str, str]:
    commit = api.request("GET", f"/repos/{repo}/commits/{sha}")["commit"]
    return {
        "committed_at": str(commit["committer"]["date"]),
        "author": str(commit["author"]["name"]),
        "subject": str(commit["message"]).split("\n", 1)[0][:200],
    }


def last_push(api: Api, repo: str, branch: str) -> datetime | None:
    """When the branch was last pushed to, created or moved (repository activity), or None if never recorded."""
    ref = urllib.parse.quote(f"refs/heads/{branch}", safe="")
    items = api.request("GET", f"/repos/{repo}/activity?ref={ref}&per_page=1&direction=desc")
    return parse_time(str(items[0]["timestamp"])) if items else None


def closed_pr(api: Api, repo: str, sha: str) -> tuple[int, bool] | None:
    """(number, merged) of the latest pull request this commit belongs to, if it belongs to no open one."""
    pulls = api.request("GET", f"/repos/{repo}/commits/{sha}/pulls")
    if not pulls or any(p.get("state") != "closed" for p in pulls):
        return None
    last = max(pulls, key=lambda p: int(p["number"]))
    return int(last["number"]), bool(last.get("merged_at"))


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
    try:
        info = commit_info(api, repo, sha)
        pushed = last_push(api, repo, name)
    except ApiError as e:
        return Decision(name, sha, "keep", f"could not read its last commit or push, so kept ({e})")
    active = max(t for t in (parse_time(info["committed_at"]), pushed) if t is not None)
    info["committed_at"] = info["committed_at"][:10]
    if active > now - FRESH:
        return Decision(name, sha, "keep", "active in the last 24 hours", **info)
    probe = cfg.probe(name)
    if probe is not None:
        return Decision(name, sha, "archive", f"CI probe branch ({probe})", **info)
    if name.startswith((TWIN, "wip/")):
        try:
            pr = closed_pr(api, repo, sha)
        except ApiError as e:
            return Decision(name, sha, "keep", f"could not read its pull requests, so kept ({e})", **info)
        if pr is not None:
            how = "merged" if pr[1] else "closed without merging"
            return Decision(name, sha, "archive", f"its pull request #{pr[0]} was {how}", **info)
    if active < now - INACTIVE:
        return Decision(name, sha, "archive", f"no push or commit since {active.date().isoformat()}", **info)
    return Decision(name, sha, "keep", f"active: last push or commit {active.date().isoformat()}", **info)


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


def delete_branch(api: Api, repo: str, d: Decision) -> str | None:
    """Delete the branch if it still points at the planned commit and nothing protects it. None when deleted
    (or already gone), else why it was kept."""
    why = check_deletable(api, repo, d.branch, d.sha)
    if why == GONE:
        return None
    if why is not None:
        return why
    try:
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


def archive(api: Api, repo: str, d: Decision, day: str) -> Outcome:
    """Tag, verify the tag, then delete. Any failure keeps the branch and says why."""
    tag = tag_name(d.branch, day)
    err = ensure_tag(api, repo, tag, d.sha, tag_message(d))
    if err is None:
        err = delete_branch(api, repo, d)
    return Outcome(d.branch, "failed" if err else "archived", err or d.reason, tag)


def apply(api: Api, repo: str, decisions: list[Decision], day: str, dry_run: bool) -> list[Outcome]:
    out: list[Outcome] = []
    for d in decisions:
        if d.action == "archive":
            if dry_run:
                out.append(Outcome(d.branch, "would-archive", d.reason, tag_name(d.branch, day)))
            else:
                out.append(archive(api, repo, d, day))
        elif d.action == "purge":
            if dry_run:
                out.append(Outcome(d.branch, "would-purge", d.reason))
                continue
            err = delete_branch(api, repo, d)
            out.append(Outcome(d.branch, "failed" if err else "purged", err or d.reason))
    return out


def with_commit(api: Api, repo: str, branch: str, sha: str, reason: str) -> Decision:
    """A Decision to archive `sha`, with the commit details for the tag message when they can be read."""
    try:
        info = commit_info(api, repo, sha)
        info["committed_at"] = info["committed_at"][:10]
    except ApiError:
        info = {}  # the tag message then lacks the commit details; the tag itself is what matters
    return Decision(branch, sha, "archive", reason, **info)


def pr_closed(
    api: Api, repo: str, branch: str, sha: str, pr: int, merged: bool, cfg: Config, day: str
) -> list[Outcome]:
    """A pull request closed: tag its head if it merged, and archive its pre-pr/<branch> twin."""
    if not safe_name(branch) or SHA.fullmatch(sha) is None:
        return [Outcome(shown(branch), "failed", "unusual branch name or commit id: nothing done")]
    out: list[Outcome] = []
    if merged and branch not in cfg.purge:
        d = with_commit(api, repo, branch, sha, f"merged in pull request #{pr}")
        tag = tag_name(branch, day)
        err = ensure_tag(api, repo, tag, sha, tag_message(d))
        out.append(Outcome(branch, "failed" if err else "tagged", err or d.reason, tag))
    if branch.startswith(TWIN):
        return out
    twin = TWIN + branch
    try:
        head = api.request("GET", f"/repos/{repo}/branches/{q(twin)}")
        state = read_state(api, repo)
    except ApiError as e:
        if e.status == 404:
            return out
        return [*out, Outcome(twin, "failed", f"could not read the twin branch ({e})")]
    twin_sha = str(head["commit"]["sha"])
    reason = exclusion(twin, bool(head.get("protected")), state, cfg)
    if reason is not None:
        return [*out, Outcome(twin, "kept", reason)]
    if twin_sha != sha:
        try:
            ours = any(
                int(p["number"]) == pr for p in api.request("GET", f"/repos/{repo}/commits/{twin_sha}/pulls")
            )
        except ApiError as e:
            return [*out, Outcome(twin, "failed", f"could not read the twin's pull requests ({e})")]
        if not ours:
            return [*out, Outcome(twin, "kept", f"its head is not a commit of pull request #{pr}")]
    how = "merged" if merged else "closed without merging"
    d = with_commit(api, repo, twin, twin_sha, f"pre-pr twin of pull request #{pr}, {how}")
    return [*out, archive(api, repo, d, day)]


def probe_done(api: Api, repo: str, branch: str, sha: str, cfg: Config, day: str) -> Outcome:
    """CI finished on a probe branch: archive it if it still points at the commit CI ran on."""
    if not safe_name(branch) or SHA.fullmatch(sha) is None:
        return Outcome(shown(branch), "failed", "unusual branch name or commit id: nothing done")
    probe = cfg.probe(branch)
    if probe is None:
        return Outcome(branch, "skipped", "not a probe branch")
    try:
        head = api.request("GET", f"/repos/{repo}/branches/{q(branch)}")
        state = read_state(api, repo)
    except ApiError as e:
        if e.status == 404:
            return Outcome(branch, "skipped", "already deleted")
        return Outcome(branch, "failed", f"could not read the branch ({e})")
    reason = exclusion(branch, bool(head.get("protected")), state, cfg)
    if reason is not None:
        return Outcome(branch, "kept", reason)
    if str(head["commit"]["sha"]) != sha:
        return Outcome(branch, "kept", "pushed again since this CI run; the next run's cleanup handles it")
    return archive(
        api, repo, with_commit(api, repo, branch, sha, f"CI probe branch, run finished ({probe})"), day
    )


def restore_command(branch: str, tag: str) -> str:
    return (
        f'git fetch origin "refs/tags/{tag}:refs/tags/{tag}" && '
        f'git push origin "refs/tags/{tag}^{{commit}}:refs/heads/{branch}"'
    )


HEADINGS = {
    "archived": "Archived (tagged, verified, then deleted)",
    "purged": "Purged (deleted with no tag)",
    "tagged": "Tagged (merged pull request; GitHub deletes the branch)",
    "failed": "Not deleted: needs a maintainer",
    "kept": "Kept",
    "skipped": "Nothing to do",
    "would-archive": "Would archive (dry run: nothing changed)",
    "would-purge": "Would purge (dry run: nothing changed)",
}


def changes(outcomes: list[Outcome]) -> str:
    """Markdown for what a run did or would do; empty when nothing."""
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


def counts(outcomes: list[Outcome]) -> str:
    n = {a: sum(o.action == a for o in outcomes) for a in ("archived", "purged", "failed")}
    return f"{n['archived']} archived, {n['purged']} purged, {n['failed']} not deleted (needs a maintainer)"


def report(
    decisions: list[Decision], outcomes: list[Outcome], *, dry_run: bool, run_url: str, day: str
) -> str:
    mode = "dry run (nothing changed)" if dry_run else "live"
    kept = [d for d in decisions if d.action == "keep"]
    body = [
        f"Branch hygiene, {day}, {mode}. Run: {run_url or '(local)'}",
        "",
        f"{len(decisions)} branches: {counts(outcomes)}, {len(kept)} kept.",
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


def main(argv: list[str] | None = None, api_factory: Callable[[], Api] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="the daily sweep (live unless --dry-run)")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--changes", type=Path, required=True, help="write what changed here (empty if nothing)")
    run.add_argument("--run-url", default="")
    pc = sub.add_parser("pr-closed", help="a pull request closed: tag a merged head, archive its pre-pr twin")
    pc.add_argument("--pr", type=int, required=True)
    pc.add_argument("--merged", action="store_true")
    pd = sub.add_parser("probe-done", help="CI finished on a probe branch: archive it")
    for p in (pc, pd):
        p.add_argument("--branch", required=True)
        p.add_argument("--sha", required=True)
    for p in (run, pc, pd):
        p.add_argument("--repo", required=True)
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        p.add_argument("--report", type=Path, required=True, help="write the Markdown report here")
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

    if a.cmd == "run":
        decisions = plan(api, a.repo, cfg, now)
        outcomes = apply(api, a.repo, decisions, day, a.dry_run)
        text = report(decisions, outcomes, dry_run=a.dry_run, run_url=a.run_url, day=day)
        a.changes.write_text(changes(outcomes), encoding="utf-8")
    elif a.cmd == "pr-closed":
        outcomes = pr_closed(api, a.repo, a.branch, a.sha, a.pr, a.merged, cfg, day)
        text = (
            f"Pull request #{a.pr} closed: {counts(outcomes)}.\n\n{changes(outcomes) or 'Nothing to do.'}\n"
        )
    else:
        outcomes = [probe_done(api, a.repo, a.branch, a.sha, cfg, day)]
        text = f"Probe branch cleanup: {counts(outcomes)}.\n\n{changes(outcomes)}\n"
    a.report.write_text(text, encoding="utf-8")
    print(counts(outcomes))
    for o in outcomes:
        print(f"{o.action}: {shown(o.branch)}: {plain(o.detail)}")
        if o.action == "failed":
            print(f"::warning::{shown(o.branch)} was not deleted: {plain(o.detail)}")
    # The sweep reports a branch it could not delete and goes on; an event job that failed shows red.
    return 1 if a.cmd != "run" and any(o.action == "failed" for o in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
