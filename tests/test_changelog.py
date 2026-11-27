"""scripts/changelog.py: changelog fragments, the CI check that a change adds one, and the release assembly."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from scripts import changelog as cl

ROOT = Path(__file__).resolve().parents[1]

LEGACY = """# Changelog

Intro.

## [Unreleased]

### Fixed

- An old fix.

### Added

- An old feature.

[Unreleased]: https://github.com/acme/registry/commits/main
"""


def git(root: Path, *args: str) -> str:
    """Fixed git arguments in a test repository under tmp_path, through the script's own runner."""
    return cl._git(
        root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repository on main with a CHANGELOG, the fragment README and one source file, branched to `work`;
    isolated from the caller's git config and from a hook's GIT_* variables."""
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "CHANGELOG.md").write_text(LEGACY, encoding="utf-8")
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "README.md").write_text("# How to\n\n## Not a fragment\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "work")
    return tmp_path


def commit(root: Path, files: dict[str, str | None]) -> None:
    for name, text in files.items():
        path = root / name
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "change")


GOOD = "### Added\n\n- A new source.\n  - with a nested note.\n"


# Parsing fragments


def test_parse_keeps_bullets_and_nesting() -> None:
    assert cl.parse_fragment(
        "x.md", "\n### Fixed\n\n- One.\n- Two,\n  wrapped.\n\n### Added\n- Three.\n"
    ) == {
        "Fixed": "- One.\n- Two,\n  wrapped.",
        "Added": "- Three.",
    }


@pytest.mark.parametrize(
    ("name", "text", "why"),
    [
        ("x.md", "", "no '### <section>'"),
        ("x.md", "Intro\n### Added\n- a\n", "text before"),
        ("x.md", "### Misc\n- a\n", "not one of"),
        ("x.md", "### Added\n- a\n### Added\n- b\n", "appears twice"),
        ("x.md", "## Added\n- a\n", "only '### <section>'"),
        ("x.md", "### Added\n\nprose, no bullet\n", "at least one bullet"),
        ("x.md", "### Added\n\n### Fixed\n- a\n", "at least one bullet"),
        ("bad name.md", GOOD, "letters, digits"),
        ("-x.md", GOOD, "letters, digits"),
    ],
)
def test_parse_refuses_malformed(name: str, text: str, why: str) -> None:
    with pytest.raises(cl.FragmentError, match=why):
        cl.parse_fragment(name, text)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("changelog.d/source-x.md", True),
        ("changelog.d/README.md", False),
        ("changelog.d/sub/x.md", False),
        ("changelog.d/x.txt", False),
        ("docs/x.md", False),
    ],
)
def test_is_fragment(path: str, expected: bool) -> None:
    assert cl.is_fragment(path) is expected


# Assembly


def test_assemble_folds_unreleased_and_fragments(repo: Path) -> None:
    (repo / "changelog.d" / "b.md").write_text("### Fixed\n\n- Fix B.\n", encoding="utf-8")
    (repo / "changelog.d" / "a.md").write_text(
        "### Security\n\n- Sec A.\n\n### Added\n\n- Add A.\n", encoding="utf-8"
    )
    assert cl.main(["assemble", "1.2.0", "--date", "2026-12-01"], root=repo) == 0
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == (
        "# Changelog\n\nIntro.\n\n## [Unreleased]\n\n## [1.2.0] - 2026-12-01\n\n"
        "### Fixed\n\n- An old fix.\n- Fix B.\n\n"
        "### Added\n\n- An old feature.\n- Add A.\n\n"
        "### Security\n\n- Sec A.\n\n"
        "[Unreleased]: https://github.com/acme/registry/compare/v1.2.0...HEAD\n"
        "[1.2.0]: https://github.com/acme/registry/releases/tag/v1.2.0\n"
    )
    assert sorted(p.name for p in (repo / "changelog.d").iterdir()) == ["README.md"]


def test_assembled_section_is_what_the_release_workflow_reads(repo: Path) -> None:
    """release.yml takes the lines between '## [X.Y.Z]' and the next '## [' as the release notes."""
    (repo / "changelog.d" / "a.md").write_text(GOOD, encoding="utf-8")
    cl.assemble(repo, "0.9.0", "2026-12-01")
    lines = (repo / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    start = lines.index("## [0.9.0] - 2026-12-01")
    notes = [ln for ln in lines[start + 1 :] if not ln.startswith("[")]
    assert "- A new source." in notes and "- An old fix." in notes
    # A second release starts from an empty Unreleased, so the first one's notes aren't repeated.
    (repo / "changelog.d" / "b.md").write_text("### Changed\n\n- Later.\n", encoding="utf-8")
    cl.assemble(repo, "0.10.0", "2026-12-02")
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    section = text.split("## [0.10.0] - 2026-12-02\n", 1)[1].split("## [0.9.0]", 1)[0]
    assert section.strip() == "### Changed\n\n- Later."
    assert "[0.9.0]: https://github.com/acme/registry/releases/tag/v0.9.0" in text
    assert "[Unreleased]: https://github.com/acme/registry/compare/v0.10.0...HEAD" in text


@pytest.mark.parametrize(
    ("version", "changelog", "why"),
    [
        ("v1.0", LEGACY, "not a version"),
        (
            "1.0.0",
            LEGACY.replace("## [Unreleased]", "## [Unreleased]\n\n## [1.0.0] - 2026-01-01"),
            "already has",
        ),
        ("1.0.0", "# Changelog\n", "no '## \\[Unreleased\\]'"),
        ("1.0.0", "# Changelog\n\n## [Unreleased]\n\n## [0.1.0]\n\n- old\n", "Nothing to release"),
    ],
)
def test_assemble_refuses(version: str, changelog: str, why: str) -> None:
    with pytest.raises(cl.FragmentError, match=why):
        cl.assemble_text(changelog, [], version, "2026-12-01")


def test_assemble_leaves_everything_on_a_bad_fragment(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "changelog.d" / "a.md").write_text(GOOD, encoding="utf-8")
    (repo / "changelog.d" / "b.md").write_text("no heading\n", encoding="utf-8")
    assert cl.main(["assemble", "1.0.0"], root=repo) == 1
    assert "b.md" in capsys.readouterr().err
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == LEGACY
    assert (repo / "changelog.d" / "a.md").exists()


# The CI check


def test_check_skips_without_a_merge_base(capsys: pytest.CaptureFixture[str]) -> None:
    assert cl.main(["check", "--base", ""]) == 0
    assert "skipped" in capsys.readouterr().out


@pytest.mark.parametrize("top", ["src/a.py", "catalog/x.yaml", "contracts/x.yaml", "semantic/x.yaml"])
def test_check_fails_a_source_change_without_a_fragment(
    repo: Path, top: str, capsys: pytest.CaptureFixture[str]
) -> None:
    commit(repo, {top: "y = 2\n"})
    assert cl.main(["check", "--base", "main"], root=repo) == 1
    err = capsys.readouterr().err
    assert top in err and "adds no changelog fragment" in err and "changelog.d/README.md" in err


def test_check_passes_a_source_change_with_a_fragment(repo: Path) -> None:
    commit(repo, {"src/a.py": "x = 2\n", "changelog.d/work.md": GOOD})
    assert cl.main(["check", "--base", "main"], root=repo) == 0


def test_check_counts_a_changed_fragment(repo: Path) -> None:
    commit(repo, {"changelog.d/work.md": GOOD})
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "work")
    git(repo, "checkout", "-q", "-b", "next")
    commit(repo, {"src/a.py": "x = 3\n", "changelog.d/work.md": GOOD + "- And more.\n"})
    assert cl.main(["check", "--base", "main"], root=repo) == 0


def test_check_does_not_count_the_readme_or_a_deleted_fragment(repo: Path) -> None:
    commit(repo, {"changelog.d/old.md": GOOD})
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "work")
    git(repo, "checkout", "-q", "-b", "next")
    commit(
        repo,
        {"src/a.py": "x = 4\n", "changelog.d/README.md": "# How to, edited\n", "changelog.d/old.md": None},
    )
    assert cl.main(["check", "--base", "main"], root=repo) == 1


def test_check_ignores_paths_that_need_no_fragment(repo: Path) -> None:
    commit(repo, {"docs/x.md": "doc\n", "tests/test_x.py": "\n", "scripts/x.py": "\n", "srcx/a.py": "\n"})
    assert cl.main(["check", "--base", "main"], root=repo) == 0


def test_check_compares_with_the_merge_base(repo: Path) -> None:
    """A source change that reached main after the branch started is main's, not the branch's."""
    git(repo, "checkout", "-q", "main")
    commit(repo, {"src/a.py": "x = 5\n"})
    git(repo, "checkout", "-q", "work")
    commit(repo, {"docs/x.md": "doc\n"})
    assert cl.main(["check", "--base", "main"], root=repo) == 0


def test_check_ignores_uncommitted_work(repo: Path) -> None:
    (repo / "src" / "a.py").write_text("x = 6\n", encoding="utf-8")
    assert cl.main(["check", "--base", "main"], root=repo) == 0


def test_check_fails_a_hand_edit_of_the_changelog(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit(repo, {"CHANGELOG.md": LEGACY + "\n- sneaked in\n", "changelog.d/work.md": GOOD})
    assert cl.main(["check", "--base", "main"], root=repo) == 1
    assert "edits CHANGELOG.md directly" in capsys.readouterr().err


def test_check_passes_a_release_assembly(repo: Path) -> None:
    commit(repo, {"changelog.d/work.md": GOOD})
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "work")
    git(repo, "checkout", "-q", "-b", "release")
    cl.assemble(repo, "1.0.0", "2026-12-01")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "release")
    assert cl.main(["check", "--base", "main"], root=repo) == 0


def test_check_fails_a_malformed_fragment_anywhere(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit(repo, {"changelog.d/work.md": "### Notes\n- x\n"})
    assert cl.main(["check", "--base", "main"], root=repo) == 1
    assert "work.md:1" in capsys.readouterr().err


def test_check_errors_are_annotations_in_github_actions(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    commit(repo, {"src/a.py": "x = 7\n"})
    assert cl.main(["check", "--base", "main"], root=repo) == 1
    assert capsys.readouterr().err.startswith("::error::")


# This repository


def test_this_repositorys_fragments_parse() -> None:
    """Every committed fragment can be assembled, and the README explains the format."""
    cl.read_fragments(ROOT)
    readme = (ROOT / "changelog.d" / "README.md").read_text(encoding="utf-8")
    assert "Never edit `CHANGELOG.md` directly" in readme


def test_ci_runs_the_check_in_the_checks_job_without_a_condition() -> None:
    """The step sits in the Checks job, has no `if:` (the required job gains no condition) and passes the merge
    base, which is empty on main, the merge queue and nightly."""
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = [s for s in ci["jobs"]["checks"]["steps"] if s.get("name") == "Changelog fragment"]
    assert len(steps) == 1
    step = steps[0]
    assert "if" not in step
    assert step["env"]["MERGE_BASE"] == "${{ needs.changes.outputs.merge_base }}"
    assert step["run"] == 'python3 scripts/changelog.py check --base "${MERGE_BASE}"'
    assert not any(
        "changelog" in str(s) for job, j in ci["jobs"].items() if job != "checks" for s in j["steps"]
    )
