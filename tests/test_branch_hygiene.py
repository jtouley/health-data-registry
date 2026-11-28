"""scripts/branch_hygiene.py on synthetic branch lists, against a fake GitHub API (nothing leaves the machine)."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import branch_hygiene as bh

REPO = "acme/registry"
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
DAY = "2026-11-25"
OLD = "2026-11-01T10:00:00Z"  # more than 7 days
MID = "2026-11-23T10:00:00Z"  # between 24 hours and 7 days
FRESH = "2026-11-25T01:00:00Z"  # under 24 hours
CFG = bh.Config(
    keep={"research/*": "kept for the ledger"},
    purge={"claude/mi-sitemap-seeds": "sitemap data"},
    probes={"ci/fast-ci-probe-*": "probe", "pre-pr/ci-fast-*probe*": "pre-pr probe"},
)


def sha(n: int) -> str:
    return f"{n:040x}"


def closed(n: int, merged: bool = True) -> dict[str, Any]:
    return {"number": n, "state": "closed", "merged_at": "2026-11-20T00:00:00Z" if merged else None}


@dataclass
class FakeGitHub:
    """A tiny in-memory GitHub: branches, commits, pushes, rulesets, pull requests, tags. Records every write."""

    branches: dict[str, tuple[str, bool]] = field(default_factory=dict)  # name -> (sha, protected)
    commits: dict[str, str] = field(default_factory=dict)  # sha -> committer date
    pushes: dict[str, str] = field(default_factory=dict)  # branch -> last push time
    rules: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # branch -> active rules
    # (head, base) from this repository, or (head, base, head repo) for a fork
    open_pulls: list[tuple[str, ...]] = field(default_factory=list)
    # Like GitHub's GET /commits/{sha}/pulls for a commit not on the default branch: pull requests closed without
    # merging are left out of the answer, even when listed here.
    commit_pulls: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    closed_pulls: list[dict[str, Any]] = field(default_factory=list)  # GET /pulls?state=closed
    ancestors: dict[str, set[str]] = field(default_factory=dict)  # sha -> commits it descends from
    refs: dict[str, dict[str, str]] = field(default_factory=dict)  # refs/tags/x -> {type, sha}
    tag_objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    writes: list[tuple[str, str]] = field(default_factory=list)
    fail: dict[tuple[str, str], int] = field(default_factory=dict)  # (method, path prefix) -> status
    tag_points_elsewhere: bool = False
    on_reread: dict[str, tuple[str, bool]] = field(default_factory=dict)  # branch -> state when read alone
    default_branch: str = "main"
    reads: dict[str, int] = field(default_factory=dict)
    rate_limited: bool = False

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        for (m, prefix), status in self.fail.items():
            if m == method and path.startswith(prefix):
                raise bh.ApiError(status, f"{method} {path}: HTTP {status}")
        path, _, query = path.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        page = int(params.get("page", "1"))
        path = urllib.parse.unquote(path)
        base = f"/repos/{REPO}"
        if path == base:
            return {"default_branch": self.default_branch}
        if path == f"{base}/pulls":
            if params.get("state") == "closed":
                pulls = list(self.closed_pulls)
            else:
                pulls = [
                    {
                        "number": 100 + i,
                        "state": "open",
                        "head": {"ref": x[0], "repo": {"full_name": x[2] if len(x) > 2 else REPO}},
                        "base": {"ref": x[1]},
                    }
                    for i, x in enumerate(self.open_pulls)
                ]
            if "head" in params:
                owner, _, ref = params["head"].partition(":")
                pulls = [
                    p
                    for p in pulls
                    if p["head"]["ref"] == ref
                    and (p["head"]["repo"] or {}).get("full_name") == f"{owner}/registry"
                ]
            if "base" in params:
                pulls = [p for p in pulls if p["base"]["ref"] == params["base"]]
            size = int(params.get("per_page", "100"))
            return pulls[(page - 1) * size : page * size]
        if m := re.fullmatch(rf"{base}/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)", path):
            a, b = m[1], m[2]
            return {
                "status": "identical"
                if a == b
                else "ahead"
                if a in self.ancestors.get(b, set())
                else "diverged"
            }
        if m := re.fullmatch(rf"{base}/git/matching-refs/(tags/.+)", path):
            return [{"ref": r} for r in sorted(self.refs) if r.startswith("refs/" + m[1])]
        if path == f"{base}/branches":
            items = [
                {"name": n, "commit": {"sha": s}, "protected": p}
                for n, (s, p) in sorted(self.branches.items())
            ]
            return items[(page - 1) * 100 : page * 100]
        if path == f"{base}/activity":
            name = params["ref"].removeprefix("refs/heads/")
            assert params["per_page"] == "1" and params["direction"] == "desc"
            return [{"timestamp": self.pushes[name]}] if name in self.pushes else []
        if m := re.fullmatch(rf"{base}/rules/branches/(.+)", path):
            return self.rules.get(m[1], [])
        if m := re.fullmatch(rf"{base}/branches/(.+)", path):
            name = m[1]
            self.reads[name] = self.reads.get(name, 0) + 1
            if name not in self.branches:
                raise bh.ApiError(404, "no branch")
            if name in self.on_reread:
                self.branches[name] = self.on_reread[name]
            s, p = self.branches[name]
            return {"commit": {"sha": s}, "protected": p}
        if m := re.fullmatch(rf"{base}/commits/([0-9a-f]+)/pulls", path):
            return [
                p for p in self.commit_pulls.get(m[1], []) if p.get("state") != "closed" or p.get("merged_at")
            ]
        if m := re.fullmatch(rf"{base}/commits/([0-9a-f]+)", path):
            return {
                "commit": {
                    "committer": {"date": self.commits[m[1]]},
                    "author": {"name": "A. Researcher"},
                    "message": "subject line\n\nbody",
                }
            }
        if m := re.fullmatch(rf"{base}/git/ref/(tags/.+)", path):
            ref = "refs/" + m[1]
            if ref not in self.refs:
                raise bh.ApiError(404, "no ref")
            return {"object": self.refs[ref]}
        if m := re.fullmatch(rf"{base}/git/tags/([0-9a-z]+)", path):
            return {"object": self.tag_objects[m[1]]}
        if method == "POST" and path == f"{base}/git/tags":
            assert body is not None
            self.writes.append(("tag-object", body["tag"]))
            obj_sha = f"tagobj{len(self.tag_objects)}"
            target = sha(999) if self.tag_points_elsewhere else body["object"]
            self.tag_objects[obj_sha] = {"type": "commit", "sha": target}
            return {"sha": obj_sha}
        if method == "POST" and path == f"{base}/git/refs":
            assert body is not None
            if body["ref"] in self.refs:
                raise bh.ApiError(422, "exists")
            self.writes.append(("create-ref", body["ref"]))
            self.refs[body["ref"]] = {"type": "tag", "sha": body["sha"]}
            return {}
        if method == "DELETE" and (m := re.fullmatch(rf"{base}/git/refs/heads/(.+)", path)):
            self.writes.append(("delete", m[1]))
            del self.branches[m[1]]
            return None
        raise AssertionError(f"unexpected call {method} {path}")

    def branch(
        self, name: str, n: int, date: str, protected: bool = False, pushed: str | None = None
    ) -> None:
        self.branches[name] = (sha(n), protected)
        self.commits[sha(n)] = date
        if pushed:
            self.pushes[name] = pushed

    def deleted(self) -> list[str]:
        return [x for k, x in self.writes if k == "delete"]

    def abandoned(self, head: str, n: int, number: int) -> None:
        """A pull request from `head` closed without merging, whose last head was commit `n`."""
        pr = {
            **closed(number, merged=False),
            "head": {"ref": head, "sha": sha(n), "repo": {"full_name": REPO}},
        }
        self.closed_pulls.append(pr)
        self.commit_pulls.setdefault(sha(n), []).append(pr)  # what a naive fake would return; GitHub doesn't


def run(gh: FakeGitHub, dry_run: bool = False) -> tuple[list[bh.Decision], list[bh.Outcome]]:
    decisions = bh.plan(gh, REPO, CFG, NOW)
    return decisions, bh.apply(gh, REPO, decisions, DAY, dry_run)


def actions(decisions: list[bh.Decision]) -> dict[str, str]:
    return {d.branch: d.action for d in decisions}


def outcome(outcomes: list[bh.Outcome], branch: str) -> bh.Outcome:
    (o,) = [o for o in outcomes if o.branch == branch]
    return o


@pytest.fixture
def gh() -> FakeGitHub:
    g = FakeGitHub()
    g.branch("main", 1, OLD)
    g.branch("release", 2, OLD, protected=True)
    g.branch("feat/stale", 3, OLD)
    g.branch("feat/mid", 4, MID)
    g.branch("feat/open-pr", 5, OLD)
    g.branch("feat/stack-base", 6, OLD)
    g.branch("pre-pr/feat/open-pr", 12, OLD)
    g.branch("dependabot/uv/x", 7, OLD)
    g.branch("research/notes", 8, OLD)
    g.branch("claude/mi-sitemap-seeds", 9, MID)
    g.branch("wip/feat-merged", 10, MID)
    g.branch("wip/feat-active", 11, MID)
    g.branch("pre-pr/feat-done", 13, MID)
    g.branch("pre-pr/feat-abandoned", 14, MID)
    g.branch("ci/fast-ci-probe-base", 15, OLD, pushed="2026-11-24T08:00:00Z")
    g.branch("pre-pr/ci-fast-mutation-probe-2", 16, MID)
    g.branch("pre-pr/ci-fast-ci-probe-new", 17, OLD, pushed="2026-11-25T09:00:00Z")
    g.branch("feat/old-commit-new-push", 18, OLD, pushed=MID)
    g.open_pulls = [("feat/open-pr", "main"), ("feat/on-stack", "feat/stack-base")]
    g.commit_pulls[sha(10)] = [closed(19)]
    g.commit_pulls[sha(11)] = [{"number": 20, "state": "open", "merged_at": None}, closed(9)]
    g.commit_pulls[sha(13)] = [closed(21), closed(3)]
    g.abandoned("feat-abandoned", 14, 22)
    return g


def test_plan_sorts_every_branch_into_keep_archive_or_purge(gh: FakeGitHub) -> None:
    decisions, _ = run(gh, dry_run=True)
    assert actions(decisions) == {
        "ci/fast-ci-probe-base": "archive",
        "claude/mi-sitemap-seeds": "purge",
        "dependabot/uv/x": "keep",
        "feat/mid": "keep",
        "feat/old-commit-new-push": "keep",
        "feat/open-pr": "keep",
        "feat/stack-base": "keep",
        "feat/stale": "archive",
        "main": "keep",
        "pre-pr/ci-fast-ci-probe-new": "keep",
        "pre-pr/ci-fast-mutation-probe-2": "archive",
        "pre-pr/feat-abandoned": "archive",
        "pre-pr/feat-done": "archive",
        "pre-pr/feat/open-pr": "keep",
        "release": "keep",
        "research/notes": "keep",
        "wip/feat-active": "keep",
        "wip/feat-merged": "archive",
    }
    reasons = {d.branch: d.reason for d in decisions}
    assert reasons["main"] == "default branch"
    assert reasons["release"] == "protected branch"
    assert reasons["feat/open-pr"] == "has an open pull request"
    assert reasons["feat/stack-base"] == "base of an open pull request"
    assert reasons["pre-pr/feat/open-pr"] == "pre-pr twin of an open pull request"
    assert reasons["dependabot/uv/x"] == "dependabot branch"
    assert reasons["research/notes"] == "keep list: kept for the ledger"
    assert reasons["feat/stale"] == "no push or commit since 2026-11-01"
    assert reasons["feat/mid"] == "active: last push or commit 2026-11-23"
    assert reasons["feat/old-commit-new-push"] == "active: last push or commit 2026-11-23"
    assert reasons["wip/feat-merged"] == "its pull request #19 was merged"
    assert reasons["pre-pr/feat-done"] == "its pull request #21 was merged"
    assert reasons["pre-pr/feat-abandoned"] == "its pull request #22 was closed without merging"
    assert reasons["ci/fast-ci-probe-base"] == "CI probe branch (probe)"
    assert reasons["pre-pr/ci-fast-ci-probe-new"] == "active in the last 24 hours"
    assert reasons["claude/mi-sitemap-seeds"] == "purge list: sitemap data"


OPEN = {"number": 30, "state": "open", "merged_at": None}

# One branch at a time through the selection logic. Columns: name, head commit date, last push (or None),
# protected, open pull requests as (head, base), the pull requests its head commit belongs to, then the
# expected action and reason. The real config's lists are used, so a change there shows up here.
# fmt: off
SELECTION = [
    ("main", OLD, None, False, [], [], "keep", "default branch"),
    ("release", OLD, None, True, [], [], "keep", "protected branch"),
    ("pre-pr/feat", OLD, None, True, [], [closed(1)], "keep", "protected branch"),
    ("feat/a", OLD, None, False, [("feat/a", "main")], [], "keep", "has an open pull request"),
    ("feat/base", OLD, None, False, [("feat/b", "feat/base")], [], "keep", "base of an open pull request"),
    ("pre-pr/feat/a", OLD, None, False, [("feat/a", "main")], [closed(1)], "keep",
     "pre-pr twin of an open pull request"),
    ("wip/a", OLD, None, False, [("wip/a", "main")], [closed(1)], "keep", "has an open pull request"),
    ("dependabot/pip/x", OLD, None, False, [], [], "keep", "dependabot branch"),
    ("pre-pr/feat/c", FRESH, None, False, [], [closed(2)], "keep", "active in the last 24 hours"),
    ("wip/c", OLD, FRESH, False, [], [closed(2)], "keep", "active in the last 24 hours"),
    ("ci/fast-ci-probe-9", FRESH, None, False, [], [], "keep", "active in the last 24 hours"),
    ("pre-pr/feat/d", MID, None, False, [], [closed(3)], "archive", "its pull request #3 was merged"),
    ("pre-pr/feat/e", MID, None, False, [], [closed(4, merged=False)], "archive",
     "its pull request #4 was closed without merging"),
    ("wip/f", MID, None, False, [], [closed(5), closed(6, merged=False)], "archive",
     "its pull request #6 was closed without merging"),
    ("wip/g", MID, None, False, [], [OPEN, closed(5)], "keep", "active: last push or commit 2026-11-23"),
    ("pre-pr/h", MID, None, False, [], [], "keep", "active: last push or commit 2026-11-23"),
    ("feat/i", MID, None, False, [], [closed(7)], "keep", "active: last push or commit 2026-11-23"),
    ("ci/fast-ci-probe-x", MID, None, False, [], [], "archive", "CI probe branch (fast-CI probe)"),
    ("pre-pr/ci-fast-ci-probe-y", MID, None, False, [], [], "archive",
     "CI probe branch (fast-CI probe on the pre-PR gate)"),
    ("pre-pr/ci/fast-mutation-rerun", MID, None, False, [], [], "archive",
     "CI probe branch (one-off mutation re-run probe on the pre-PR gate)"),
    ("ci/fast-mutation-rerun", MID, None, False, [], [], "keep", "active: last push or commit 2026-11-23"),
    ("claude/mi-sitemap-seeds", MID, None, False, [], [], "purge",
     "purge list: contains sitemap data that must not stay in the repo (issue #67)"),
    ("claude/mi-sitemap-seeds", FRESH, None, False, [], [], "keep", "active in the last 24 hours"),
    ("claude/mi-sitemap-seeds", OLD, None, False, [("claude/mi-sitemap-seeds", "main")], [], "keep",
     "has an open pull request"),
    ("feat/stale", OLD, None, False, [], [], "archive", "no push or commit since 2026-11-01"),
    ("feat/stale-pushed", OLD, MID, False, [], [], "keep", "active: last push or commit 2026-11-23"),
    ("bad name", OLD, None, False, [], [], "keep", "unusual branch name: left for a maintainer"),
    # A fork's branch of the same name says nothing about ours.
    ("claude/mi-sitemap-seeds", MID, None, False, [("claude/mi-sitemap-seeds", "main", "eve/registry")], [],
     "purge", "purge list: contains sitemap data that must not stay in the repo (issue #67)"),
    ("pre-pr/feat/z", MID, None, False, [("feat/z", "main", "eve/registry")], [closed(8)], "archive",
     "its pull request #8 was merged"),
    # A fork's pull request into one of our branches can't keep it silently: a maintainer decides.
    ("claude/mi-sitemap-seeds", MID, None, False, [("x", "claude/mi-sitemap-seeds", "eve/registry")], [],
     "hold", "base of open pull request #100 from a fork; a maintainer decides (purge list: contains sitemap"
     " data that must not stay in the repo (issue #67))"),
    ("feat/forked-into", MID, None, False, [("x", "feat/forked-into", "eve/registry")], [], "keep",
     "active: last push or commit 2026-11-23"),
]
# fmt: on


@pytest.mark.parametrize(
    ("name", "committed", "pushed", "protected", "pulls", "head_pulls", "action", "reason"),
    SELECTION,
    ids=[f"{row[0]}->{row[6]}" for row in SELECTION],
)
def test_selection_table(
    name: str,
    committed: str,
    pushed: str | None,
    protected: bool,
    pulls: list[tuple[str, ...]],
    head_pulls: list[dict[str, Any]],
    action: str,
    reason: str,
) -> None:
    g = FakeGitHub()
    g.branch("main", 1, OLD)
    g.branch(name, 2, committed, protected=protected, pushed=pushed)
    g.open_pulls = list(pulls)
    g.commit_pulls[sha(2)] = [p for p in head_pulls if p["state"] == "open" or p["merged_at"]]
    for p in head_pulls:
        if p["state"] == "closed" and not p["merged_at"]:
            g.abandoned(name.removeprefix("pre-pr/"), 2, p["number"])
    cfg = bh.load_config(bh.DEFAULT_CONFIG)
    (d,) = [d for d in bh.plan(g, REPO, cfg, NOW) if d.branch == name]
    assert (d.action, d.reason) == (action, reason)


def test_nothing_under_24_hours_old_is_archived() -> None:
    g = FakeGitHub()
    g.branch("main", 1, OLD)
    g.branch("pre-pr/x", 2, FRESH)
    g.branch("ci/fast-ci-probe-y", 3, FRESH)
    g.branch("wip/z", 4, OLD, pushed=FRESH)
    g.branch("edge", 5, "2026-11-24T12:00:01Z")
    g.commit_pulls[sha(2)] = [closed(1)]
    g.commit_pulls[sha(4)] = [closed(2)]
    assert set(actions(bh.plan(g, REPO, CFG, NOW)).values()) == {"keep"}


def test_exactly_seven_days_and_24_hours_are_boundaries() -> None:
    g = FakeGitHub()
    g.branch("main", 1, OLD)
    g.branch("edge7", 2, "2026-11-18T12:00:00Z")
    g.branch("over7", 3, "2026-11-18T11:59:59Z")
    g.branch("ci/fast-ci-probe-edge", 4, "2026-11-24T12:00:00Z")
    g.branch("ci/fast-ci-probe-fresh", 5, "2026-11-24T12:00:01Z")
    assert actions(bh.plan(g, REPO, CFG, NOW)) == {
        "ci/fast-ci-probe-edge": "archive",
        "ci/fast-ci-probe-fresh": "keep",
        "edge7": "keep",
        "main": "keep",
        "over7": "archive",
    }


def test_dry_run_changes_nothing(gh: FakeGitHub) -> None:
    _, outcomes = run(gh, dry_run=True)
    assert gh.writes == []
    assert {o.action for o in outcomes} == {"would-archive", "would-purge"}


def test_live_run_tags_verifies_then_deletes_and_purges_without_a_tag(gh: FakeGitHub) -> None:
    _, outcomes = run(gh)
    stale = [w for w in gh.writes if "feat/stale" in w[1]]
    assert stale == [
        ("tag-object", "archive/feat/stale/2026-11-25"),
        ("create-ref", "refs/tags/archive/feat/stale/2026-11-25"),
        ("delete", "feat/stale"),
    ]
    assert ("delete", "claude/mi-sitemap-seeds") in gh.writes
    assert not any("claude/mi-sitemap-seeds" in r for r in gh.refs)
    assert bh.peel(gh, REPO, "archive/feat/stale/2026-11-25") == sha(3)
    assert sorted(gh.deleted()) == sorted(
        [
            "ci/fast-ci-probe-base",
            "claude/mi-sitemap-seeds",
            "feat/stale",
            "pre-pr/ci-fast-mutation-probe-2",
            "pre-pr/feat-abandoned",
            "pre-pr/feat-done",
            "wip/feat-merged",
        ]
    )
    for d in gh.deleted():
        if d != "claude/mi-sitemap-seeds":
            assert f"refs/tags/archive/{d}/2026-11-25" in gh.refs
    assert {o.action for o in outcomes} == {"archived", "purged"}


def test_open_pull_request_branches_and_their_twins_are_never_touched(gh: FakeGitHub) -> None:
    gh.open_pulls += [("claude/mi-sitemap-seeds", "main"), ("ci/fast-ci-probe-base", "main")]
    decisions, _ = run(gh)
    for name in (
        "claude/mi-sitemap-seeds",
        "ci/fast-ci-probe-base",
        "feat/open-pr",
        "pre-pr/feat/open-pr",
        "feat/stack-base",
        "main",
        "release",
    ):
        assert actions(decisions)[name] == "keep"
        assert name not in gh.deleted()
        assert name in gh.branches


def test_an_unverified_tag_skips_the_branch_and_reports_it(gh: FakeGitHub) -> None:
    gh.tag_points_elsewhere = True
    decisions, outcomes = run(gh)
    failed = {o.branch: o.detail for o in outcomes if o.action == "failed"}
    assert "feat/stale" in failed and "wip/feat-merged" in failed
    assert "not the branch head" in failed["feat/stale"]
    assert gh.deleted() == ["claude/mi-sitemap-seeds"]
    text = bh.report(decisions, outcomes, dry_run=False, run_url="", day=DAY)
    assert "Not deleted: needs a maintainer" in text
    assert "- `feat/stale`: tag archive/feat/stale/2026-11-25 resolves to" in text


def test_a_tag_that_cannot_be_created_keeps_the_branch(gh: FakeGitHub) -> None:
    gh.fail[("POST", f"/repos/{REPO}/git/tags")] = 403
    _, outcomes = run(gh)
    assert gh.deleted() == ["claude/mi-sitemap-seeds"]
    assert outcome(outcomes, "feat/stale").detail.startswith("could not create or read")


def test_an_existing_tag_at_another_commit_is_never_moved(gh: FakeGitHub) -> None:
    gh.refs["refs/tags/archive/feat/stale/2026-11-25"] = {"type": "commit", "sha": sha(42)}
    _, outcomes = run(gh)
    assert gh.refs["refs/tags/archive/feat/stale/2026-11-25"]["sha"] == sha(42)
    assert ("tag-object", "archive/feat/stale/2026-11-25") not in gh.writes
    alt = f"archive/feat/stale/2026-11-25-{sha(3)[:12]}"
    assert bh.peel(gh, REPO, alt) == sha(3)
    o = outcome(outcomes, "feat/stale")
    assert (o.action, o.tag) == ("archived", alt)
    assert "feat/stale" not in gh.branches


def test_a_taken_alternative_tag_keeps_the_branch(gh: FakeGitHub) -> None:
    gh.refs["refs/tags/archive/feat/stale/2026-11-25"] = {"type": "commit", "sha": sha(42)}
    gh.refs[f"refs/tags/archive/feat/stale/2026-11-25-{sha(3)[:12]}"] = {"type": "commit", "sha": sha(43)}
    gh.fail[("GET", f"/repos/{REPO}/git/ref/tags/archive/wip")] = 500
    _, outcomes = run(gh)
    assert outcome(outcomes, "feat/stale").action == "failed"
    assert "feat/stale" in gh.branches
    assert outcome(outcomes, "wip/feat-merged").detail.startswith("could not create or read the tag")
    assert "wip/feat-merged" in gh.branches


def test_a_second_run_reuses_the_verified_tag(gh: FakeGitHub) -> None:
    gh.refs["refs/tags/archive/feat/stale/2026-11-25"] = {"type": "commit", "sha": sha(3)}
    _, outcomes = run(gh)
    assert ("tag-object", "archive/feat/stale/2026-11-25") not in gh.writes
    assert "feat/stale" in gh.deleted()
    assert outcome(outcomes, "feat/stale").action == "archived"


def test_a_tag_created_meanwhile_is_checked_not_overwritten(gh: FakeGitHub) -> None:
    real = gh.request

    def racing(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if (
            method == "POST"
            and path.endswith("/git/tags")
            and body
            and body["tag"].endswith("feat/stale/2026-11-25")
        ):
            gh.refs[f"refs/tags/{body['tag']}"] = {"type": "commit", "sha": body["object"]}
        return real(method, path, body)

    gh.request = racing  # type: ignore[method-assign]
    _, outcomes = run(gh)
    assert "feat/stale" in gh.deleted()
    assert outcome(outcomes, "feat/stale").action == "archived"


def test_a_branch_that_moved_or_became_protected_after_the_plan_is_kept(gh: FakeGitHub) -> None:
    gh.on_reread["feat/stale"] = (sha(77), False)
    gh.on_reread["claude/mi-sitemap-seeds"] = (sha(9), True)
    _, outcomes = run(gh)
    assert "feat/stale" in gh.branches and "claude/mi-sitemap-seeds" in gh.branches
    assert outcome(outcomes, "feat/stale").detail == f"the branch moved to {sha(77)}; kept"
    assert outcome(outcomes, "claude/mi-sitemap-seeds").detail == "the branch is protected"


def test_a_pull_request_opened_after_the_plan_keeps_the_branch(gh: FakeGitHub) -> None:
    decisions = bh.plan(gh, REPO, CFG, NOW)
    gh.open_pulls += [("feat/stale", "main"), ("feat/x", "wip/feat-merged"), ("feat-done", "main")]
    gh.open_pulls += [("ci/fast-ci-probe-base", "main", "eve/registry")]  # a fork's head says nothing
    outcomes = bh.apply(gh, REPO, decisions, DAY, False)
    assert outcome(outcomes, "feat/stale").detail == "an open pull request uses it now"
    assert outcome(outcomes, "wip/feat-merged").detail == "it is now the base of an open pull request"
    assert outcome(outcomes, "pre-pr/feat-done").detail == "its pull request is open again"
    assert outcome(outcomes, "ci/fast-ci-probe-base").action == "archived"
    assert {"feat/stale", "wip/feat-merged", "pre-pr/feat-done"} <= set(gh.branches)
    gh.open_pulls = [("feat/mid", "main")]
    assert bh.open_pull(gh, REPO, "feat/mid") == "an open pull request uses it now"
    assert bh.open_pull(gh, REPO, "pre-pr/feat/mid") == "its pull request is open again"
    assert bh.open_pull(gh, REPO, "main") == "it is now the base of an open pull request"
    assert bh.open_pull(gh, REPO, "feat/other") is None
    assert bh.open_pull(gh, REPO, "pre-pr/feat/other") is None
    gh.fail[("GET", f"/repos/{REPO}/pulls")] = 500
    d = bh.Decision("feat/mid", sha(4), "archive", "r")
    assert bh.check_deletable(gh, REPO, d.branch, d.sha).startswith("could not read its open pull requests")


def test_a_run_deletes_at_most_max_deletes_purges_first(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bh, "MAX_DELETES", 2)
    paused: list[float] = []
    decisions = bh.plan(gh, REPO, CFG, NOW)
    outcomes = bh.apply(gh, REPO, decisions, DAY, False, pause=paused.append)
    assert gh.deleted() == ["claude/mi-sitemap-seeds", "ci/fast-ci-probe-base"]
    assert paused == [bh.PAUSE]
    deferred = [o for o in outcomes if o.action == "deferred"]
    assert len(deferred) == 5
    assert deferred[0].detail.startswith("left for the next run (at most 2 a run): ")
    assert "5 left for the next run" in bh.counts(outcomes)
    preview = bh.apply(gh, REPO, bh.plan(gh, REPO, CFG, NOW), DAY, True)
    assert [o.action for o in preview].count("would-archive") == 2


def test_a_rate_limited_run_stops_deleting(gh: FakeGitHub) -> None:
    decisions = bh.plan(gh, REPO, CFG, NOW)
    gh.rate_limited = True
    outcomes = bh.apply(gh, REPO, decisions, DAY, False)
    assert gh.writes == []
    assert {o.action for o in outcomes} == {"deferred"}
    assert outcomes[0].detail.startswith("left for the next run (GitHub rate limit): ")


@pytest.mark.parametrize(
    ("status", "headers", "limited"),
    [
        (429, {}, True),
        (403, {"x-ratelimit-remaining": "0"}, True),
        (403, {"retry-after": "60"}, True),
        (403, {"x-ratelimit-remaining": "12"}, False),
        (404, {"x-ratelimit-remaining": "0"}, False),
    ],
)
def test_rate_limited(status: int, headers: dict[str, str], limited: bool) -> None:
    assert bh.rate_limited(status, headers) is limited


def test_a_purge_lists_older_archive_tags(gh: FakeGitHub) -> None:
    gh.refs["refs/tags/archive/claude/mi-sitemap-seeds/2026-11-01"] = {"type": "commit", "sha": sha(9)}
    _, outcomes = run(gh)
    o = outcome(outcomes, "claude/mi-sitemap-seeds")
    assert o.action == "purged"
    assert o.detail.endswith(
        "; older archive tags still hold it, for a maintainer to remove: archive/claude/mi-sitemap-seeds/2026-11-01"
    )
    assert bh.older_tags(gh, REPO, "feat/none") == ""
    gh.fail[("GET", f"/repos/{REPO}/git/matching-refs")] = 500
    assert bh.older_tags(gh, REPO, "x").startswith("; could not list its older archive tags")


def test_a_ruleset_that_forbids_deletion_is_checked_not_assumed(gh: FakeGitHub) -> None:
    gh.rules["feat/stale"] = [{"type": "deletion"}]
    gh.rules["wip/feat-merged"] = [{"type": "required_signatures"}]
    gh.fail[("GET", f"/repos/{REPO}/rules/branches/pre-pr/feat-done")] = 500
    _, outcomes = run(gh)
    assert outcome(outcomes, "feat/stale").detail == "a ruleset forbids deleting it"
    assert "wip/feat-merged" in gh.deleted()
    assert outcome(outcomes, "pre-pr/feat-done").detail.startswith("could not read the rulesets")
    assert "feat/stale" in gh.branches and "pre-pr/feat-done" in gh.branches


def test_a_branch_already_gone_counts_as_done(gh: FakeGitHub) -> None:
    decisions = bh.plan(gh, REPO, CFG, NOW)
    del gh.branches["feat/stale"]
    outcomes = bh.apply(gh, REPO, decisions, DAY, False)
    assert outcome(outcomes, "feat/stale").action == "archived"


def test_a_delete_or_read_error_keeps_the_branch(gh: FakeGitHub) -> None:
    gh.fail[("DELETE", f"/repos/{REPO}/git/refs/heads/feat/stale")] = 422
    gh.fail[("GET", f"/repos/{REPO}/branches/wip")] = 500
    _, outcomes = run(gh)
    assert outcome(outcomes, "feat/stale").detail.startswith("could not delete the branch")
    assert outcome(outcomes, "wip/feat-merged").detail.startswith("could not read the branch")
    assert "feat/stale" in gh.branches and "wip/feat-merged" in gh.branches


def test_unreadable_history_keeps_the_branch(gh: FakeGitHub) -> None:
    gh.fail[("GET", f"/repos/{REPO}/activity")] = 403
    decisions = bh.plan(gh, REPO, CFG, NOW)
    assert set(actions(decisions).values()) == {"keep"}  # the purge list too
    assert "could not read its last commit or push" in {d.branch: d.reason for d in decisions}["feat/stale"]
    g = FakeGitHub()
    g.branch("main", 1, OLD)
    g.branch("pre-pr/x", 2, MID)
    g.fail[("GET", f"/repos/{REPO}/commits/{sha(2)}/pulls")] = 500
    (d,) = [d for d in bh.plan(g, REPO, CFG, NOW) if d.branch == "pre-pr/x"]
    assert (d.action, d.reason.startswith("could not read its pull requests")) == ("keep", True)


def test_unusual_names_are_kept() -> None:
    g = FakeGitHub()
    g.branch("main", 1, OLD)
    g.branch("odd`@name", 2, OLD)
    g.branch("a..b", 3, OLD)
    assert actions(bh.plan(g, REPO, CFG, NOW)) == {"a..b": "keep", "main": "keep", "odd`@name": "keep"}


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("feat/x-1.2_y", True),
        ("wip/agent-a4ffa39d7be3d802a", True),
        ("-x", False),
        ("a..b", False),
        ("a//b", False),
        ("a/.b", False),
        ("a.", False),
        ("a/", False),
        ("a.lock", False),
        ("a b", False),
        ("a" * 200, True),
        ("a" * 201, False),
        ("", False),
    ],
)
def test_safe_name(name: str, ok: bool) -> None:
    assert bh.safe_name(name) is ok


def test_plain_and_shown_strip_formatting() -> None:
    assert bh.shown("a`b@c d") == "a?b?c?d"
    assert bh.shown("x" * 150) == "x" * 100
    assert bh.plain("a`b@c\nd::e<f>") == "a b c d e f "
    assert len(bh.plain("y" * 400)) == 300


def test_paging_reads_every_branch() -> None:
    g = FakeGitHub()
    for i in range(250):
        g.branch(f"b/{i:03d}", 100 + i, FRESH)
    g.branch("main", 1, OLD)
    assert len(bh.get_all(g, f"/repos/{REPO}/branches")) == 251
    assert len(bh.get_all(g, f"/repos/{REPO}/pulls?state=open")) == 0


def test_paging_stops_after_max_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    g = FakeGitHub()
    for i in range(250):
        g.branch(f"b/{i:03d}", 100 + i, FRESH)
    monkeypatch.setattr(bh, "MAX_PAGES", 2)
    with pytest.raises(bh.ApiError, match="more than 2 pages"):
        bh.get_all(g, f"/repos/{REPO}/branches")
    monkeypatch.setattr(bh, "MAX_PAGES", 3)
    assert len(bh.get_all(g, f"/repos/{REPO}/branches")) == 250


def test_apply_records_every_outcome_in_order_and_logs_each(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bh, "MAX_DELETES", 3)
    decisions = [
        bh.Decision("feat/stale", sha(3), "archive", "old"),
        bh.Decision("feat/mid", sha(4), "keep", "active"),
        bh.Decision("wip/feat-merged", sha(10), "archive", "merged"),
        bh.Decision("x/held", sha(5), "hold", "fork base"),
        bh.Decision("claude/mi-sitemap-seeds", sha(9), "purge", "purge list: sitemap data"),
        bh.Decision("pre-pr/feat-done", sha(13), "archive", "done"),
        bh.Decision("ci/fast-ci-probe-base", sha(15), "archive", "probe"),
    ]
    out: list[bh.Outcome] = []
    logged: list[bh.Outcome] = []
    paused: list[float] = []
    got = bh.apply(gh, REPO, decisions, DAY, False, out=out, log=logged.append, pause=paused.append)
    assert got is out and logged == out
    assert out == [
        bh.Outcome("x/held", "failed", "fork base"),
        bh.Outcome("claude/mi-sitemap-seeds", "purged", "purge list: sitemap data"),
        bh.Outcome("feat/stale", "archived", "old", "archive/feat/stale/2026-11-25"),
        bh.Outcome("wip/feat-merged", "archived", "merged", "archive/wip/feat-merged/2026-11-25"),
        bh.Outcome("pre-pr/feat-done", "deferred", "left for the next run (at most 3 a run): done"),
        bh.Outcome("ci/fast-ci-probe-base", "deferred", "left for the next run (at most 3 a run): probe"),
    ]
    assert paused == [bh.PAUSE, bh.PAUSE]
    assert gh.deleted() == ["claude/mi-sitemap-seeds", "feat/stale", "wip/feat-merged"]
    preview = bh.apply(gh, REPO, decisions[:5], DAY, True)
    assert preview == [
        bh.Outcome("x/held", "failed", "fork base"),
        bh.Outcome("claude/mi-sitemap-seeds", "would-purge", "purge list: sitemap data"),
        bh.Outcome("feat/stale", "would-archive", "old", "archive/feat/stale/2026-11-25"),
        bh.Outcome("wip/feat-merged", "would-archive", "merged", "archive/wip/feat-merged/2026-11-25"),
    ]


def test_a_fork_base_holds_with_the_commit_details(gh: FakeGitHub) -> None:
    gh.open_pulls += [("x", "feat/stale", "eve/registry"), ("y", "feat/mid", "eve/registry")]
    decisions = {d.branch: d for d in bh.plan(gh, REPO, CFG, NOW)}
    assert decisions["feat/stale"] == bh.Decision(
        "feat/stale",
        sha(3),
        "hold",
        "base of open pull request #102 from a fork; a maintainer decides (no push or commit since 2026-11-01)",
        "2026-11-01",
        "A. Researcher",
        "subject line",
    )
    assert decisions["feat/mid"].action == "keep"  # held only when it would otherwise be deleted


def test_log_outcome_prints_one_line_and_a_warning_for_a_failure(capsys: Any) -> None:
    bh.log_outcome(bh.Outcome("a/b", "archived", "old"))
    bh.log_outcome(bh.Outcome("c`d", "failed", "x@y"))
    assert capsys.readouterr().out == (
        "archived: a/b: old\nfailed: c?d: x y\n::warning::c?d was not deleted: x y\n"
    )


def test_closed_pr_needs_every_pull_request_closed() -> None:
    g = FakeGitHub()
    g.commit_pulls[sha(5)] = [closed(3), closed(7)]
    g.abandoned("wip/x", 5, 12)
    assert bh.closed_pr(g, REPO, "wip/x", sha(5)) == (12, False)
    assert bh.closed_pr(g, REPO, "wip/other", sha(5)) == (7, True)
    g.commit_pulls[sha(6)] = [closed(3), {"number": 4, "state": "open"}]
    assert bh.closed_pr(g, REPO, "wip/y", sha(6)) is None
    assert bh.closed_pr(g, REPO, "wip/z", sha(8)) is None


def test_closed_pr_finds_an_abandoned_pull_request_by_head_branch() -> None:
    """GitHub's commit-to-pull-requests answer leaves out pull requests closed without merging."""
    g = FakeGitHub()
    g.abandoned("feat/x", 5, 30)
    assert g.request("GET", f"/repos/{REPO}/commits/{sha(5)}/pulls") == []
    assert bh.closed_pr(g, REPO, "pre-pr/feat/x", sha(5)) == (30, False)
    assert bh.closed_pr(g, REPO, "feat/x", sha(5)) == (30, False)
    # A pull request whose last head is another commit says nothing about this one.
    assert bh.closed_pr(g, REPO, "pre-pr/feat/x", sha(6)) is None
    # Another owner's branch of the same name is not ours.
    g.closed_pulls[0]["head"]["repo"] = {"full_name": "eve/registry"}
    assert bh.closed_pr(g, REPO, "pre-pr/feat/x", sha(5)) is None


def test_peel_follows_nested_tags_and_refuses_non_commits() -> None:
    g = FakeGitHub()
    g.refs["refs/tags/t"] = {"type": "tag", "sha": "o1"}
    g.tag_objects["o1"] = {"type": "tag", "sha": "o2"}
    g.tag_objects["o2"] = {"type": "commit", "sha": sha(7)}
    assert bh.peel(g, REPO, "t") == sha(7)
    g.refs["refs/tags/tree"] = {"type": "tree", "sha": "x"}
    with pytest.raises(bh.ApiError, match="not a commit"):
        bh.peel(g, REPO, "tree")
    g.refs["refs/tags/deep"] = {"type": "tag", "sha": "loop"}
    g.tag_objects["loop"] = {"type": "tag", "sha": "loop"}
    with pytest.raises(bh.ApiError, match="too deeply"):
        bh.peel(g, REPO, "deep")
    g.fail[("GET", f"/repos/{REPO}/git/ref/tags/boom")] = 500
    with pytest.raises(bh.ApiError):
        bh.peel(g, REPO, "boom")


def test_report_lists_actions_restore_commands_and_kept_branches(gh: FakeGitHub) -> None:
    decisions, outcomes = run(gh)
    text = bh.report(decisions, outcomes, dry_run=False, run_url="https://example.test/run/1", day=DAY)
    assert text.startswith("Branch hygiene, 2026-11-25, live. Run: https://example.test/run/1\n")
    assert "18 branches: 6 archived, 1 purged, 0 not deleted (needs a maintainer), 11 kept." in text
    assert "### Archived (tagged, verified, then deleted)" in text
    assert "### Purged (deleted with no tag)" in text
    restore = (
        'git fetch origin "refs/tags/archive/feat/stale/2026-11-25:refs/tags/archive/feat/stale/2026-11-25"'
        ' && git push origin "refs/tags/archive/feat/stale/2026-11-25^{commit}:refs/heads/feat/stale"'
    )
    assert f"  - restore: `{restore}`" in text
    assert "<details><summary>Kept: 11 branch(es)</summary>" in text
    assert "- `main`: default branch" in text
    purged = text.split("### Purged")[1].split("###")[0]
    assert "restore:" not in purged


def test_report_dry_run_nothing_to_do_and_cap() -> None:
    text = bh.report([], [], dry_run=True, run_url="", day=DAY)
    assert "dry run (nothing changed). Run: (local)" in text
    assert "Nothing to archive or purge today." in text
    assert "stopped early" not in text and "matched no branch" not in text
    assert bh.changes([]) == ""
    decisions = [bh.Decision(f"b/{i:05d}", sha(i), "keep", "r" * 200) for i in range(400)]
    outcomes = [bh.Outcome(f"c/{i:05d}", "failed", "f" * 200) for i in range(400)]
    text = bh.report(decisions, outcomes, dry_run=False, run_url="", day=DAY)
    assert len(text) <= bh.MAX_REPORT
    # Whole lines are cut, inside each list, so the details block and every code span stay closed.
    assert text.endswith("</details>\n")
    assert text.count("`") % 2 == 0
    assert "more lines, cut short; the full list is in the run log" in text
    assert bh.fit(["ab", "cd", "ef"], 6) == [
        "ab",
        "cd",
        "- ... and 1 more lines, cut short; the full list is in the run log",
    ]
    assert bh.fit(["ab", "cd"], 6) == ["ab", "cd"]


def test_report_shows_a_stopped_run_and_unused_keep_entries(gh: FakeGitHub) -> None:
    decisions = bh.plan(gh, REPO, CFG, NOW)
    cfg = bh.Config(keep={"research/*": "r", "reserch/*": "typo"}, purge={}, probes={})
    unused = bh.unused_keep(cfg, decisions)
    assert unused == ["reserch/*"]
    text = bh.report(
        decisions, [], dry_run=False, run_url="", day=DAY, unused=unused, stopped="KeyError: 'x'"
    )
    assert "**The run stopped early (KeyError: 'x').**" in text
    assert "Keep entries that matched no branch (a typo?): `reserch/*`" in text


def test_tag_message_names_the_last_commit() -> None:
    d = bh.Decision("feat/x", sha(3), "archive", "no push", "2026-11-01", "A. B", "fix it")
    assert bh.tag_message(d) == (
        f"Archive of branch feat/x\n\nLast commit: {sha(3)} fix it\n"
        "Author: A. B, committed 2026-11-01\nReason: no push\n"
    )


def pr_repo() -> FakeGitHub:
    g = FakeGitHub()
    g.commits[sha(3)] = OLD
    g.branch("main", 1, OLD)
    g.branch("pre-pr/feat/x", 3, MID)
    return g


def test_pr_merged_tags_the_head_and_archives_the_twin() -> None:
    g = pr_repo()
    out = bh.pr_closed(g, REPO, "feat/x", sha(3), 41, True, CFG, NOW)
    assert [(o.branch, o.action) for o in out] == [("feat/x", "tagged"), ("pre-pr/feat/x", "archived")]
    assert bh.peel(g, REPO, "archive/feat/x/2026-11-25") == sha(3)
    assert bh.peel(g, REPO, "archive/pre-pr/feat/x/2026-11-25") == sha(3)
    assert g.deleted() == ["pre-pr/feat/x"]
    again = bh.pr_closed(g, REPO, "feat/x", sha(3), 41, True, CFG, NOW)
    assert [o.action for o in again] == ["tagged"]
    assert [k for k, _ in g.writes].count("tag-object") == 2


def test_pr_closed_unmerged_archives_the_twin_only() -> None:
    g = pr_repo()
    out = bh.pr_closed(g, REPO, "feat/x", sha(3), 41, False, CFG, NOW)
    assert [(o.branch, o.action) for o in out] == [("pre-pr/feat/x", "archived")]
    assert "closed without merging" in out[0].detail


def test_pr_twin_with_other_work_or_still_in_use_is_kept() -> None:
    g = pr_repo()
    g.branches["pre-pr/feat/x"] = (sha(4), False)
    g.commits[sha(4)] = MID
    out = bh.pr_closed(g, REPO, "feat/x", sha(3), 41, False, CFG, NOW)
    assert out[-1].action == "kept" and "not a commit of pull request #41" in out[-1].detail
    g.ancestors[sha(3)] = {sha(4)}  # the twin's head is an earlier commit of the pull request
    assert bh.pr_closed(g, REPO, "feat/x", sha(3), 41, False, CFG, NOW)[-1].action == "archived"
    g2 = pr_repo()
    g2.open_pulls = [("feat/x", "main")]  # reopened meanwhile
    assert bh.pr_closed(g2, REPO, "feat/x", sha(3), 41, False, CFG, NOW)[-1].action == "kept"
    g3 = pr_repo()
    g3.branches["pre-pr/feat/x"] = (sha(3), True)
    assert bh.pr_closed(g3, REPO, "feat/x", sha(3), 41, False, CFG, NOW)[-1].detail == "protected branch"
    assert g.deleted() == ["pre-pr/feat/x"] and g2.deleted() == [] and g3.deleted() == []


def test_pr_twin_also_held_by_another_open_pull_request_is_kept() -> None:
    g = pr_repo()
    g.commit_pulls[sha(3)] = [
        {"number": 41, "state": "closed", "merged_at": None},
        {"number": 50, "state": "open"},
    ]
    out = bh.pr_closed(g, REPO, "feat/x", sha(3), 41, False, CFG, NOW)
    assert (out[-1].action, out[-1].detail) == ("kept", "its head is also in open pull request #50")
    assert g.deleted() == []


def test_pr_closed_reports_unreadable_open_pull_requests() -> None:
    g = pr_repo()
    g.fail[("GET", f"/repos/{REPO}/pulls")] = 404
    out = bh.pr_closed(g, REPO, "feat/x", sha(3), 41, False, CFG, NOW)
    assert (out[-1].branch, out[-1].action) == ("pre-pr/feat/x", "failed")
    assert out[-1].detail.startswith("could not read the open pull requests")


def test_pr_closed_edge_cases() -> None:
    g = pr_repo()
    assert bh.pr_closed(g, REPO, "a b", sha(3), 1, True, CFG, NOW)[0].action == "failed"
    assert bh.pr_closed(g, REPO, "feat/x", "HEAD", 1, True, CFG, NOW)[0].action == "failed"
    assert bh.pr_closed(g, REPO, "claude/mi-sitemap-seeds", sha(3), 1, True, CFG, NOW) == []
    assert bh.pr_closed(g, REPO, "feat/none", sha(3), 1, False, CFG, NOW) == []
    assert [o.action for o in bh.pr_closed(g, REPO, "pre-pr/feat/x", sha(3), 1, True, CFG, NOW)] == ["tagged"]
    assert g.deleted() == []
    g.fail[("GET", f"/repos/{REPO}/branches/pre-pr")] = 500
    assert bh.pr_closed(g, REPO, "feat/x", sha(3), 1, False, CFG, NOW)[-1].action == "failed"
    g4 = pr_repo()
    g4.branches["pre-pr/feat/x"] = (sha(4), False)
    g4.fail[("GET", f"/repos/{REPO}/commits/{sha(4)}/pulls")] = 500
    assert bh.pr_closed(g4, REPO, "feat/x", sha(3), 1, False, CFG, NOW)[-1].action == "failed"
    g5 = pr_repo()
    g5.tag_points_elsewhere = True
    assert [o.action for o in bh.pr_closed(g5, REPO, "feat/x", sha(3), 1, True, CFG, NOW)] == [
        "failed",
        "failed",
    ]
    assert g5.deleted() == []


def test_pr_closed_leaves_a_twin_active_in_the_last_24_hours_to_the_sweep() -> None:
    g = pr_repo()
    g.commits[sha(3)] = FRESH
    out = bh.pr_closed(g, REPO, "feat/x", sha(3), 41, False, CFG, NOW)
    assert [(o.branch, o.action) for o in out] == [("pre-pr/feat/x", "kept")]
    assert out[0].detail == "active in the last 24 hours; the daily sweep archives it later"
    g2 = pr_repo()
    g2.pushes["pre-pr/feat/x"] = FRESH  # an old commit pushed again just now
    assert bh.pr_closed(g2, REPO, "feat/x", sha(3), 41, False, CFG, NOW)[-1].action == "kept"
    g3 = pr_repo()
    g3.fail[("GET", f"/repos/{REPO}/activity")] = 500
    assert bh.pr_closed(g3, REPO, "feat/x", sha(3), 41, False, CFG, NOW)[-1].action == "failed"
    assert g.deleted() == [] and g2.deleted() == [] and g3.deleted() == []


def test_load_config(tmp_path: Path) -> None:
    real = bh.load_config(bh.DEFAULT_CONFIG)
    assert "claude/mi-sitemap-seeds" in real.purge
    assert real.probe("ci/fast-ci-probe-base") and real.probe("pre-pr/ci-fast-mutation-probe-2")
    assert real.probe("pre-pr/ci/fast-mutation-rerun")
    assert real.probe("pre-pr/ci/fast-mutation-rerun-2") is None
    assert real.probe("pre-pr/ci-fast-ci") is None and real.probe("ci/fast-mutation") is None
    p = tmp_path / "c.yaml"
    p.write_text("keep:\n  - name: 'x/*'\n    reason: r\npurge: []\n", encoding="utf-8")
    cfg = bh.load_config(p)
    assert cfg.kept_by("x/y/z") == "r"
    assert cfg.kept_by("y") is None
    p.write_text("", encoding="utf-8")
    assert bh.load_config(p) == bh.Config(keep={}, purge={}, probes={})
    for bad in (
        "other: []\n",
        "- a\n",
        "keep:\n  - name: x\n",
        "keep:\n  - reason: r\n",
        "purge:\n  - name: 'x/*'\n    reason: r\n",
        "probes:\n  - name: '**'\n    reason: r\n",
        "probes:\n  - name: '?*'\n    reason: r\n",
        "probes:\n  - name: '*/*'\n    reason: r\n",
        "probes:\n  - name: 'pre-pr/*'\n    reason: r\n",
        "keep:\n  - just-a-string\n",
    ):
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            bh.load_config(p)


@pytest.mark.parametrize(
    ("pattern", "ok"),
    [
        ("pre-pr/ci/fast-mutation-rerun", True),  # exact
        ("ci/fast-ci-probe-*", True),
        ("pre-pr/ci-fast-*probe*", True),
        ("?*", False),
        ("*-*", False),
        ("[!~]*", False),
        ("pre-pr/*probe*", False),  # nothing fixed past the first /
        ("ci-probe-*", False),  # no fixed first path segment
        ("ci/fast-*", False),  # not named a probe
        ("ci/x*probe*", True),
        ("ci/*", False),
    ],
)
def test_probe_pattern_ok(pattern: str, ok: bool) -> None:
    assert bh.probe_pattern_ok(pattern) is ok


def test_github_api_refuses_plain_http_and_no_token() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        bh.GitHubApi("t", "http://api.github.com")
    with pytest.raises(ValueError, match="token"):
        bh.GitHubApi("", "https://api.github.com")
    assert bh.GitHubApi("t", "https://h.test/").base == "https://h.test"


def test_q_quotes_everything_but_slashes() -> None:
    assert bh.q("a/b c#d") == "a/b%20c%23d"


def test_main_run_writes_the_report_and_changes(gh: FakeGitHub, tmp_path: Path, capsys: Any) -> None:
    gh.tag_points_elsewhere = True
    report, changes = tmp_path / "r.md", tmp_path / "c.md"
    argv = ["run", "--repo", REPO, "--report", str(report), "--changes", str(changes), "--run-url", "u"]
    assert bh.main(argv, api_factory=lambda: gh) == 0
    assert "Not deleted: needs a maintainer" in report.read_text(encoding="utf-8")
    assert "### Purged" in changes.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "::warning::feat/stale was not deleted:" in out
    assert "purged: claude/mi-sitemap-seeds" in out


def test_main_run_that_stops_early_still_reports_what_it_deleted(
    gh: FakeGitHub, tmp_path: Path, capsys: Any
) -> None:
    real = gh.request

    def broken(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if method == "POST" and body and "feat/stale" in str(body.get("tag", "")):
            raise KeyError("sha")  # an unexpected answer partway through
        return real(method, path, body)

    gh.request = broken  # type: ignore[method-assign]
    report, changes = tmp_path / "r.md", tmp_path / "c.md"
    argv = ["run", "--repo", REPO, "--report", str(report), "--changes", str(changes)]
    assert bh.main(argv, api_factory=lambda: gh) == 1
    text = report.read_text(encoding="utf-8")
    assert "**The run stopped early (KeyError: 'sha').**" in text
    assert "- `claude/mi-sitemap-seeds`:" in text and "- `ci/fast-ci-probe-base`:" in text
    assert "### Purged" in changes.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "purged: claude/mi-sitemap-seeds" in out and "::error::the run stopped early" in out
    gh.request = real  # type: ignore[method-assign]
    gh.fail[("GET", f"/repos/{REPO}/branches")] = (
        500  # the plan itself fails: nothing deleted, still reported
    )
    assert bh.main(argv, api_factory=lambda: gh) == 1
    assert "0 branches: 0 archived" in report.read_text(encoding="utf-8")


def test_main_dry_run(gh: FakeGitHub, tmp_path: Path) -> None:
    report, changes = tmp_path / "r.md", tmp_path / "c.md"
    argv = ["run", "--dry-run", "--repo", REPO, "--report", str(report), "--changes", str(changes)]
    assert bh.main(argv, api_factory=lambda: gh) == 0
    assert gh.writes == []
    assert "Would purge" in changes.read_text(encoding="utf-8")


def test_main_pr_closed(tmp_path: Path, capsys: Any) -> None:
    g = pr_repo()
    report = tmp_path / "r.md"
    base = ["--repo", REPO, "--sha", sha(3), "--report", str(report)]
    assert bh.main(["pr-closed", *base, "--branch", "feat/x", "--pr", "5", "--merged"], lambda: g) == 0
    text = report.read_text(encoding="utf-8")
    assert text.startswith("Pull request #5 closed: 1 archived, 0 purged, 0 not deleted")
    assert "### Tagged" in text and "restore:" in text
    assert bh.main(["pr-closed", *base, "--branch", "bad name", "--pr", "5"], lambda: g) == 1
    assert "::warning::" in capsys.readouterr().out


def test_main_refuses_a_bad_repo(tmp_path: Path) -> None:
    argv = ["pr-closed", "--repo=x", "--branch=b", f"--sha={sha(1)}", "--pr=1", f"--report={tmp_path / 'r'}"]
    with pytest.raises(SystemExit):
        bh.main(argv, lambda: FakeGitHub())


def test_main_builds_the_real_client_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token"):
        bh.main(
            [
                "pr-closed",
                f"--repo={REPO}",
                "--branch=b",
                f"--sha={sha(1)}",
                "--pr=1",
                f"--report={tmp_path / 'r'}",
            ]
        )
