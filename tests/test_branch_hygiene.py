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
    open_pulls: list[tuple[str, str]] = field(default_factory=list)  # (head, base)
    commit_pulls: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    refs: dict[str, dict[str, str]] = field(default_factory=dict)  # refs/tags/x -> {type, sha}
    tag_objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    writes: list[tuple[str, str]] = field(default_factory=list)
    fail: dict[tuple[str, str], int] = field(default_factory=dict)  # (method, path prefix) -> status
    tag_points_elsewhere: bool = False
    on_reread: dict[str, tuple[str, bool]] = field(default_factory=dict)  # branch -> state when read alone
    default_branch: str = "main"
    reads: dict[str, int] = field(default_factory=dict)

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
            pulls = [{"head": {"ref": h}, "base": {"ref": b}} for h, b in self.open_pulls]
            return pulls[(page - 1) * 100 : page * 100]
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
            return self.commit_pulls.get(m[1], [])
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
    g.branch("claude/mi-sitemap-seeds", 9, FRESH)
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
    g.commit_pulls[sha(14)] = [closed(22, merged=False)]
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
    assert "feat/stale" in gh.branches
    assert gh.refs["refs/tags/archive/feat/stale/2026-11-25"]["sha"] == sha(42)
    assert ("tag-object", "archive/feat/stale/2026-11-25") not in gh.writes
    assert outcome(outcomes, "feat/stale").action == "failed"


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
    assert {a for n, a in actions(decisions).items() if n != "claude/mi-sitemap-seeds"} == {"keep"}
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


def test_closed_pr_needs_every_pull_request_closed() -> None:
    g = FakeGitHub()
    g.commit_pulls[sha(5)] = [closed(3), closed(12, merged=False), closed(7)]
    assert bh.closed_pr(g, REPO, sha(5)) == (12, False)
    g.commit_pulls[sha(6)] = [closed(3), {"number": 4, "state": "open"}]
    assert bh.closed_pr(g, REPO, sha(6)) is None
    assert bh.closed_pr(g, REPO, sha(8)) is None


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
    assert bh.changes([]) == ""
    decisions = [bh.Decision(f"b/{i:05d}", sha(i), "keep", "r" * 200) for i in range(400)]
    text = bh.report(decisions, [], dry_run=False, run_url="", day=DAY)
    assert len(text) < bh.MAX_REPORT + 100
    assert text.endswith("(cut short; the full list is in the run log)\n")


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
    g.branch("pre-pr/feat/x", 3, FRESH)
    return g


def test_pr_merged_tags_the_head_and_archives_the_twin() -> None:
    g = pr_repo()
    out = bh.pr_closed(g, REPO, "feat/x", sha(3), 41, True, CFG, DAY)
    assert [(o.branch, o.action) for o in out] == [("feat/x", "tagged"), ("pre-pr/feat/x", "archived")]
    assert bh.peel(g, REPO, "archive/feat/x/2026-11-25") == sha(3)
    assert bh.peel(g, REPO, "archive/pre-pr/feat/x/2026-11-25") == sha(3)
    assert g.deleted() == ["pre-pr/feat/x"]
    again = bh.pr_closed(g, REPO, "feat/x", sha(3), 41, True, CFG, DAY)
    assert [o.action for o in again] == ["tagged"]
    assert [k for k, _ in g.writes].count("tag-object") == 2


def test_pr_closed_unmerged_archives_the_twin_only() -> None:
    g = pr_repo()
    out = bh.pr_closed(g, REPO, "feat/x", sha(3), 41, False, CFG, DAY)
    assert [(o.branch, o.action) for o in out] == [("pre-pr/feat/x", "archived")]
    assert "closed without merging" in out[0].detail


def test_pr_twin_with_other_work_or_still_in_use_is_kept() -> None:
    g = pr_repo()
    g.branches["pre-pr/feat/x"] = (sha(4), False)
    g.commits[sha(4)] = FRESH
    out = bh.pr_closed(g, REPO, "feat/x", sha(3), 41, False, CFG, DAY)
    assert out[-1].action == "kept" and "not a commit of pull request #41" in out[-1].detail
    g.commit_pulls[sha(4)] = [closed(41)]
    assert bh.pr_closed(g, REPO, "feat/x", sha(3), 41, False, CFG, DAY)[-1].action == "archived"
    g2 = pr_repo()
    g2.open_pulls = [("feat/x", "main")]  # reopened meanwhile
    assert bh.pr_closed(g2, REPO, "feat/x", sha(3), 41, False, CFG, DAY)[-1].action == "kept"
    g3 = pr_repo()
    g3.branches["pre-pr/feat/x"] = (sha(3), True)
    assert bh.pr_closed(g3, REPO, "feat/x", sha(3), 41, False, CFG, DAY)[-1].detail == "protected branch"
    assert g.deleted() == ["pre-pr/feat/x"] and g2.deleted() == [] and g3.deleted() == []


def test_pr_closed_edge_cases() -> None:
    g = pr_repo()
    assert bh.pr_closed(g, REPO, "a b", sha(3), 1, True, CFG, DAY)[0].action == "failed"
    assert bh.pr_closed(g, REPO, "feat/x", "HEAD", 1, True, CFG, DAY)[0].action == "failed"
    assert bh.pr_closed(g, REPO, "claude/mi-sitemap-seeds", sha(3), 1, True, CFG, DAY) == []
    assert bh.pr_closed(g, REPO, "feat/none", sha(3), 1, False, CFG, DAY) == []
    assert [o.action for o in bh.pr_closed(g, REPO, "pre-pr/feat/x", sha(3), 1, True, CFG, DAY)] == ["tagged"]
    assert g.deleted() == []
    g.fail[("GET", f"/repos/{REPO}/branches/pre-pr")] = 500
    assert bh.pr_closed(g, REPO, "feat/x", sha(3), 1, False, CFG, DAY)[-1].action == "failed"
    g4 = pr_repo()
    g4.branches["pre-pr/feat/x"] = (sha(4), False)
    g4.fail[("GET", f"/repos/{REPO}/commits/{sha(4)}/pulls")] = 500
    assert bh.pr_closed(g4, REPO, "feat/x", sha(3), 1, False, CFG, DAY)[-1].action == "failed"
    g5 = pr_repo()
    g5.tag_points_elsewhere = True
    assert [o.action for o in bh.pr_closed(g5, REPO, "feat/x", sha(3), 1, True, CFG, DAY)] == [
        "failed",
        "failed",
    ]
    assert g5.deleted() == []


def test_probe_done_archives_the_probe_it_ran_on() -> None:
    g = FakeGitHub()
    g.branch("main", 1, OLD)
    g.branch("pre-pr/ci-fast-ci-probe-code", 2, FRESH)
    o = bh.probe_done(g, REPO, "pre-pr/ci-fast-ci-probe-code", sha(2), CFG, DAY)
    assert (o.action, o.detail) == ("archived", "CI probe branch, run finished (pre-pr probe)")
    assert g.deleted() == ["pre-pr/ci-fast-ci-probe-code"]
    assert bh.probe_done(g, REPO, "pre-pr/ci-fast-ci-probe-code", sha(2), CFG, DAY).action == "skipped"


def test_probe_done_keeps_what_it_must() -> None:
    g = FakeGitHub()
    g.branch("main", 1, OLD)
    g.branch("ci/fast-ci-probe-a", 2, FRESH)
    g.branch("feat/not-a-probe", 3, OLD)
    assert bh.probe_done(g, REPO, "feat/not-a-probe", sha(3), CFG, DAY).action == "skipped"
    assert bh.probe_done(g, REPO, "ci/fast-ci-probe-a", sha(9), CFG, DAY).action == "kept"
    g.open_pulls = [("ci/fast-ci-probe-a", "main")]
    assert bh.probe_done(g, REPO, "ci/fast-ci-probe-a", sha(2), CFG, DAY).detail == "has an open pull request"
    assert bh.probe_done(g, REPO, "a b", sha(2), CFG, DAY).action == "failed"
    g.fail[("GET", f"/repos/{REPO}/branches/ci")] = 500
    assert bh.probe_done(g, REPO, "ci/fast-ci-probe-a", sha(2), CFG, DAY).action == "failed"
    assert g.deleted() == []


def test_load_config(tmp_path: Path) -> None:
    real = bh.load_config(bh.DEFAULT_CONFIG)
    assert "claude/mi-sitemap-seeds" in real.purge
    assert real.probe("ci/fast-ci-probe-base") and real.probe("pre-pr/ci-fast-mutation-probe-2")
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
        "keep:\n  - just-a-string\n",
    ):
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            bh.load_config(p)


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


def test_main_dry_run(gh: FakeGitHub, tmp_path: Path) -> None:
    report, changes = tmp_path / "r.md", tmp_path / "c.md"
    argv = ["run", "--dry-run", "--repo", REPO, "--report", str(report), "--changes", str(changes)]
    assert bh.main(argv, api_factory=lambda: gh) == 0
    assert gh.writes == []
    assert "Would purge" in changes.read_text(encoding="utf-8")


def test_main_pr_closed_and_probe_done(tmp_path: Path, capsys: Any) -> None:
    g = pr_repo()
    report = tmp_path / "r.md"
    base = ["--repo", REPO, "--sha", sha(3), "--report", str(report)]
    assert bh.main(["pr-closed", *base, "--branch", "feat/x", "--pr", "5", "--merged"], lambda: g) == 0
    text = report.read_text(encoding="utf-8")
    assert text.startswith("Pull request #5 closed: 1 archived, 0 purged, 0 not deleted")
    assert "### Tagged" in text and "restore:" in text
    assert bh.main(["pr-closed", *base, "--branch", "bad name", "--pr", "5"], lambda: g) == 1
    g.branch("ci/fast-ci-probe-z", 3, FRESH)
    assert bh.main(["probe-done", *base, "--branch", "ci/fast-ci-probe-z"], lambda: g) == 0
    assert report.read_text(encoding="utf-8").startswith("Probe branch cleanup: 1 archived")
    assert bh.main(["probe-done", *base, "--branch", "x y"], lambda: g) == 1
    assert "::warning::" in capsys.readouterr().out


def test_main_refuses_a_bad_repo(tmp_path: Path) -> None:
    argv = ["probe-done", "--repo", "x", "--branch", "b", "--sha", sha(1), "--report", str(tmp_path / "r")]
    with pytest.raises(SystemExit):
        bh.main(argv, lambda: FakeGitHub())


def test_main_builds_the_real_client_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token"):
        bh.main(
            ["probe-done", "--repo", REPO, "--branch", "b", "--sha", sha(1), "--report", str(tmp_path / "r")]
        )
