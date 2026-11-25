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
OLD = "2026-11-01T10:00:00Z"
RECENT = "2026-11-24T10:00:00Z"
CFG = bh.Config(keep={"research/*": "kept for the ledger"}, purge={"claude/mi-sitemap-seeds": "sitemap data"})


def sha(n: int) -> str:
    return f"{n:040x}"


@dataclass
class FakeGitHub:
    """A tiny in-memory GitHub: branches, commits, pull requests, tags. Records every write."""

    branches: dict[str, tuple[str, bool]] = field(default_factory=dict)  # name -> (sha, protected)
    commits: dict[str, str] = field(default_factory=dict)  # sha -> committer date
    open_pulls: list[tuple[str, str]] = field(default_factory=list)  # (head, base)
    commit_pulls: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    refs: dict[str, dict[str, str]] = field(default_factory=dict)  # refs/tags/x -> {type, sha}
    tag_objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    writes: list[tuple[str, str]] = field(default_factory=list)
    fail: dict[tuple[str, str], int] = field(default_factory=dict)  # (method, path prefix) -> status
    tag_points_elsewhere: bool = False
    move_on_read: dict[str, str] = field(default_factory=dict)  # branch -> sha it moves to before delete
    default_branch: str = "main"

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        for (m, prefix), status in self.fail.items():
            if m == method and path.startswith(prefix):
                raise bh.ApiError(status, f"{method} {path}: HTTP {status}")
        path, _, query = path.partition("?")
        page = int(dict(urllib.parse.parse_qsl(query)).get("page", "1"))
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
        if m := re.fullmatch(rf"{base}/branches/(.+)", path):
            name = m[1]
            if name not in self.branches:
                raise bh.ApiError(404, "no branch")
            if name in self.move_on_read:
                self.branches[name] = (self.move_on_read[name], False)
            return {"commit": {"sha": self.branches[name][0]}}
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

    def branch(self, name: str, n: int, date: str, protected: bool = False) -> None:
        self.branches[name] = (sha(n), protected)
        self.commits[sha(n)] = date

    def deleted(self) -> list[str]:
        return [x for k, x in self.writes if k == "delete"]


def run(gh: FakeGitHub, dry_run: bool = False) -> tuple[list[bh.Decision], list[bh.Outcome]]:
    decisions = bh.plan(gh, REPO, CFG, NOW)
    return decisions, bh.apply(gh, REPO, decisions, DAY, dry_run)


def actions(decisions: list[bh.Decision]) -> dict[str, str]:
    return {d.branch: d.action for d in decisions}


@pytest.fixture
def gh() -> FakeGitHub:
    g = FakeGitHub()
    g.branch("main", 1, OLD)
    g.branch("release", 2, OLD, protected=True)
    g.branch("feat/stale", 3, OLD)
    g.branch("feat/fresh", 4, RECENT)
    g.branch("feat/open-pr", 5, OLD)
    g.branch("feat/stack-base", 6, OLD)
    g.branch("dependabot/uv/x", 7, OLD)
    g.branch("research/notes", 8, OLD)
    g.branch("claude/mi-sitemap-seeds", 9, RECENT)
    g.branch("wip/feat-merged", 10, RECENT)
    g.branch("wip/feat-active", 11, RECENT)
    g.open_pulls = [("feat/open-pr", "main"), ("feat/on-stack", "feat/stack-base")]
    g.commit_pulls[sha(10)] = [{"number": 19, "state": "closed", "merged_at": "2026-11-24T00:00:00Z"}]
    g.commit_pulls[sha(11)] = [{"number": 20, "state": "open", "merged_at": None}]
    return g


def test_plan_sorts_every_branch_into_keep_archive_or_purge(gh: FakeGitHub) -> None:
    decisions, _ = run(gh, dry_run=True)
    assert actions(decisions) == {
        "claude/mi-sitemap-seeds": "purge",
        "dependabot/uv/x": "keep",
        "feat/fresh": "keep",
        "feat/open-pr": "keep",
        "feat/stack-base": "keep",
        "feat/stale": "archive",
        "main": "keep",
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
    assert reasons["dependabot/uv/x"] == "dependabot branch"
    assert reasons["research/notes"] == "keep list: kept for the ledger"
    assert reasons["feat/stale"] == "no commit since 2026-11-01"
    assert reasons["wip/feat-merged"] == "wip backup of merged pull request #19"
    assert reasons["feat/fresh"] == "active: last commit 2026-11-24"


def test_dry_run_changes_nothing(gh: FakeGitHub) -> None:
    _, outcomes = run(gh, dry_run=True)
    assert gh.writes == []
    assert {(o.branch, o.action) for o in outcomes} == {
        ("feat/stale", "would-archive"),
        ("wip/feat-merged", "would-archive"),
        ("claude/mi-sitemap-seeds", "would-purge"),
    }


def test_live_run_tags_verifies_then_deletes_and_purges_without_a_tag(gh: FakeGitHub) -> None:
    _, outcomes = run(gh)
    assert gh.writes == [
        ("delete", "claude/mi-sitemap-seeds"),
        ("tag-object", "archive/feat/stale/2026-11-25"),
        ("create-ref", "refs/tags/archive/feat/stale/2026-11-25"),
        ("delete", "feat/stale"),
        ("tag-object", "archive/wip/feat-merged/2026-11-25"),
        ("create-ref", "refs/tags/archive/wip/feat-merged/2026-11-25"),
        ("delete", "wip/feat-merged"),
    ]
    assert bh.peel(gh, REPO, "archive/feat/stale/2026-11-25") == sha(3)
    assert {o.action for o in outcomes} == {"archived", "purged"}
    assert not any(t.startswith("refs/tags/archive/claude/mi-sitemap-seeds") for t in gh.refs)


def test_open_pull_request_branches_are_never_touched_even_on_the_purge_list(gh: FakeGitHub) -> None:
    gh.open_pulls.append(("claude/mi-sitemap-seeds", "main"))
    decisions, _ = run(gh)
    assert actions(decisions)["claude/mi-sitemap-seeds"] == "keep"
    assert actions(decisions)["feat/open-pr"] == "keep"
    for name in ("claude/mi-sitemap-seeds", "feat/open-pr", "feat/stack-base", "main", "release"):
        assert name not in gh.deleted()
        assert name in gh.branches


def test_an_unverified_tag_skips_the_branch_and_reports_it(gh: FakeGitHub) -> None:
    gh.tag_points_elsewhere = True
    _, outcomes = run(gh)
    failed = {o.branch: o.detail for o in outcomes if o.action == "failed"}
    assert set(failed) == {"feat/stale", "wip/feat-merged"}
    assert "not the branch head" in failed["feat/stale"]
    assert gh.deleted() == ["claude/mi-sitemap-seeds"]
    text = bh.report(bh.plan(gh, REPO, CFG, NOW), outcomes, dry_run=False, run_url="", day=DAY)
    assert "Not deleted: needs a maintainer" in text
    assert "`feat/stale`" in text


def test_a_tag_that_cannot_be_created_keeps_the_branch(gh: FakeGitHub) -> None:
    gh.fail[("POST", f"/repos/{REPO}/git/tags")] = 403
    _, outcomes = run(gh)
    assert gh.deleted() == ["claude/mi-sitemap-seeds"]
    assert [o.detail for o in outcomes if o.branch == "feat/stale"][0].startswith("could not create or read")


def test_an_existing_tag_at_another_commit_is_never_moved(gh: FakeGitHub) -> None:
    gh.refs["refs/tags/archive/feat/stale/2026-11-25"] = {"type": "commit", "sha": sha(42)}
    _, outcomes = run(gh)
    assert "feat/stale" in gh.branches
    assert gh.refs["refs/tags/archive/feat/stale/2026-11-25"]["sha"] == sha(42)
    assert ("tag-object", "archive/feat/stale/2026-11-25") not in gh.writes
    assert [o.action for o in outcomes if o.branch == "feat/stale"] == ["failed"]


def test_a_second_run_reuses_the_verified_tag(gh: FakeGitHub) -> None:
    gh.refs["refs/tags/archive/feat/stale/2026-11-25"] = {"type": "commit", "sha": sha(3)}
    _, outcomes = run(gh)
    assert ("tag-object", "archive/feat/stale/2026-11-25") not in gh.writes
    assert "feat/stale" in gh.deleted()
    assert [o.action for o in outcomes if o.branch == "feat/stale"] == ["archived"]


def test_a_tag_created_meanwhile_is_checked_not_overwritten(gh: FakeGitHub) -> None:
    real = gh.request
    seen: list[str] = []

    def racing(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if (
            method == "POST"
            and path.endswith("/git/tags")
            and body
            and body["tag"].startswith("archive/feat/")
        ):
            seen.append(body["tag"])
            gh.refs[f"refs/tags/{body['tag']}"] = {"type": "commit", "sha": body["object"]}
        return real(method, path, body)

    gh.request = racing  # type: ignore[method-assign]
    _, outcomes = run(gh)
    assert seen == ["archive/feat/stale/2026-11-25"]
    assert "feat/stale" in gh.deleted()
    assert [o.action for o in outcomes if o.branch == "feat/stale"] == ["archived"]


def test_a_branch_that_moved_after_the_plan_is_kept(gh: FakeGitHub) -> None:
    gh.move_on_read["feat/stale"] = sha(77)
    gh.move_on_read["claude/mi-sitemap-seeds"] = sha(78)
    _, outcomes = run(gh)
    assert "feat/stale" in gh.branches
    assert "claude/mi-sitemap-seeds" in gh.branches
    assert all(
        o.action == "failed" and "moved" in o.detail for o in outcomes if o.branch != "wip/feat-merged"
    )


def test_a_branch_already_gone_counts_as_done(gh: FakeGitHub) -> None:
    decisions = bh.plan(gh, REPO, CFG, NOW)
    del gh.branches["feat/stale"]
    outcomes = bh.apply(gh, REPO, decisions, DAY, False)
    assert [o.action for o in outcomes if o.branch == "feat/stale"] == ["archived"]


def test_a_delete_error_is_reported(gh: FakeGitHub) -> None:
    gh.fail[("DELETE", f"/repos/{REPO}/git/refs/heads/feat")] = 422
    _, outcomes = run(gh)
    assert [o.action for o in outcomes if o.branch == "feat/stale"] == ["failed"]
    assert "could not delete" in [o.detail for o in outcomes if o.branch == "feat/stale"][0]


def test_a_branch_read_error_is_reported(gh: FakeGitHub) -> None:
    gh.fail[("GET", f"/repos/{REPO}/branches/feat")] = 500
    _, outcomes = run(gh)
    assert "feat/stale" in gh.branches


def test_unusual_names_are_kept() -> None:
    g = FakeGitHub()
    g.branch("main", 1, OLD)
    g.branch("odd`@name", 2, OLD)
    g.branch("a..b", 3, OLD)
    decisions = bh.plan(g, REPO, CFG, NOW)
    assert actions(decisions) == {"a..b": "keep", "main": "keep", "odd`@name": "keep"}


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
        g.branch(f"b/{i:03d}", 100 + i, RECENT)
    g.branch("main", 1, OLD)
    assert len(bh.get_all(g, f"/repos/{REPO}/branches")) == 251
    assert len(bh.get_all(g, f"/repos/{REPO}/pulls?state=open")) == 0


def test_exactly_seven_days_is_still_active() -> None:
    g = FakeGitHub()
    g.branch("main", 1, OLD)
    g.branch("edge", 2, "2026-11-18T12:00:00Z")
    g.branch("over", 3, "2026-11-18T11:59:59Z")
    assert actions(bh.plan(g, REPO, CFG, NOW)) == {"edge": "keep", "main": "keep", "over": "archive"}


def test_wip_backup_with_no_merged_pr_is_kept() -> None:
    g = FakeGitHub()
    g.branch("main", 1, OLD)
    g.branch("wip/a", 2, RECENT)
    g.branch("wip/b", 3, RECENT)
    g.commit_pulls[sha(3)] = [{"number": 4, "state": "closed", "merged_at": None}]
    assert actions(bh.plan(g, REPO, CFG, NOW))["wip/a"] == "keep"
    assert actions(bh.plan(g, REPO, CFG, NOW))["wip/b"] == "keep"


def test_merged_pr_picks_the_latest_merged() -> None:
    g = FakeGitHub()
    g.commit_pulls[sha(5)] = [
        {"number": 3, "state": "closed", "merged_at": "x"},
        {"number": 12, "state": "closed", "merged_at": "y"},
        {"number": 30, "state": "closed", "merged_at": None},
    ]
    assert bh.merged_pr(g, REPO, sha(5)) == 12


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
    assert "### Archived (tagged, verified, then deleted)" in text
    assert "### Purged (deleted with no tag)" in text
    restore = (
        'git fetch origin "refs/tags/archive/feat/stale/2026-11-25:refs/tags/archive/feat/stale/2026-11-25"'
        ' && git push origin "refs/tags/archive/feat/stale/2026-11-25^{commit}:refs/heads/feat/stale"'
    )
    assert f"  - restore: `{restore}`" in text
    assert "<details><summary>Kept: 8 branch(es)</summary>" in text
    assert "- `main`: default branch" in text
    purged = text.split("### Purged")[1]
    assert "restore:" not in purged.split("###")[0]


def test_report_dry_run_and_nothing_to_do() -> None:
    text = bh.report([], [], dry_run=True, run_url="", day=DAY)
    assert "dry run (nothing changed). Run: (local)" in text
    assert "Nothing to archive or purge today." in text
    assert bh.changes([]) == ""


def test_report_is_capped() -> None:
    decisions = [bh.Decision(f"b/{i:05d}", sha(i), "keep", "r" * 200) for i in range(400)]
    text = bh.report(decisions, [], dry_run=False, run_url="", day=DAY)
    assert len(text) < bh.MAX_REPORT + 100
    assert text.endswith("(cut short; the full list is in the run log)\n")


def test_tag_message_names_the_last_commit() -> None:
    d = bh.Decision("feat/x", sha(3), "archive", "no commit since 2026-11-01", "2026-11-01", "A. B", "fix it")
    assert bh.tag_message(d) == (
        f"Archive of branch feat/x\n\nLast commit: {sha(3)} fix it\n"
        "Author: A. B, committed 2026-11-01\nReason: no commit since 2026-11-01\n"
    )


def test_tag_merged_tags_and_never_deletes() -> None:
    g = FakeGitHub()
    g.commits[sha(3)] = OLD
    o = bh.tag_merged(g, REPO, "feat/x", sha(3), 41, CFG, DAY)
    assert (o.action, o.tag) == ("archived", "archive/feat/x/2026-11-25")
    assert bh.peel(g, REPO, "archive/feat/x/2026-11-25") == sha(3)
    assert g.deleted() == []
    obj = g.tag_objects[g.refs["refs/tags/archive/feat/x/2026-11-25"]["sha"]]
    assert obj["sha"] == sha(3)
    again = bh.tag_merged(g, REPO, "feat/x", sha(3), 41, CFG, DAY)
    assert again.action == "archived"
    assert [k for k, _ in g.writes] == ["tag-object", "create-ref"]


def test_tag_merged_skips_purge_and_refuses_odd_input() -> None:
    g = FakeGitHub()
    assert bh.tag_merged(g, REPO, "claude/mi-sitemap-seeds", sha(1), 1, CFG, DAY).action == "skipped"
    assert bh.tag_merged(g, REPO, "a b", sha(1), 1, CFG, DAY).action == "failed"
    assert bh.tag_merged(g, REPO, "feat/x", "HEAD", 1, CFG, DAY).action == "failed"
    assert g.writes == []


def test_tag_merged_survives_a_missing_commit_lookup() -> None:
    g = FakeGitHub()
    g.fail[("GET", f"/repos/{REPO}/commits/")] = 404
    o = bh.tag_merged(g, REPO, "feat/x", sha(3), 2, CFG, DAY)
    assert o.action == "archived"
    g2 = FakeGitHub(tag_points_elsewhere=True)
    g2.commits[sha(3)] = OLD
    assert bh.tag_merged(g2, REPO, "feat/y", sha(3), 2, CFG, DAY).action == "failed"


def test_load_config(tmp_path: Path) -> None:
    real = bh.load_config(bh.DEFAULT_CONFIG)
    assert "claude/mi-sitemap-seeds" in real.purge
    p = tmp_path / "c.yaml"
    p.write_text("keep:\n  - name: 'x/*'\n    reason: r\npurge: []\n", encoding="utf-8")
    cfg = bh.load_config(p)
    assert cfg.kept_by("x/y/z") == "r"
    assert cfg.kept_by("y") is None
    p.write_text("", encoding="utf-8")
    assert bh.load_config(p) == bh.Config(keep={}, purge={})
    for bad in (
        "other: []\n",
        "- a\n",
        "keep:\n  - name: x\n",
        "keep:\n  - reason: r\n",
        "purge:\n  - name: 'x/*'\n    reason: r\n",
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


def test_main_dry_run_and_tag_merged(gh: FakeGitHub, tmp_path: Path, capsys: Any) -> None:
    report, changes = tmp_path / "r.md", tmp_path / "c.md"
    argv = ["run", "--dry-run", "--repo", REPO, "--report", str(report), "--changes", str(changes)]
    assert bh.main(argv, api_factory=lambda: gh) == 0
    assert gh.writes == []
    assert "Would purge" in changes.read_text(encoding="utf-8")
    base = ["tag-merged", "--repo", REPO, "--sha", sha(3), "--pr", "5"]
    assert bh.main([*base, "--branch", "feat/stale"], api_factory=lambda: gh) == 0
    assert "feat/stale: archived: merged in pull request #5 archive/feat/stale/" in capsys.readouterr().out
    assert bh.main([*base, "--branch", "bad name"], api_factory=lambda: gh) == 1


def test_main_refuses_a_bad_repo() -> None:
    with pytest.raises(SystemExit):
        bh.main(
            ["tag-merged", "--repo", "x", "--branch", "b", "--sha", sha(1), "--pr", "1"], lambda: FakeGitHub()
        )


def test_main_builds_the_real_client_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token"):
        bh.main(["tag-merged", "--repo", REPO, "--branch", "b", "--sha", sha(1), "--pr", "1"])
