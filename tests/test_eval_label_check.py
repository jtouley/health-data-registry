"""eval/label_check.py on a throwaway git repo with a synthetic catalog and synthetic labels."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from eval import label_check
from eval.label_check import (
    L1,
    L2,
    L3,
    Affected,
    CatalogChange,
    LabelCheckError,
    affected,
    catalog_changes,
    coverage_words,
    parse_name_status,
    report_md,
    resolve_commit,
    words,
)
from eval.scorer import ExpectedRecord

MARKER = "SYNTHETIC-QUESTION-TEXT"


def entry(dataset_id: str, title: str, *labels: str, caveat: str = "") -> str:
    lines = [f"id: {dataset_id}", f"title: {title}", "conditions:", "  labels:"]
    lines += [f'    - "{x}"' for x in labels] or ["    []"]
    if caveat:
        lines += ["caveats:", f'  - "{caveat}"']
    return "\n".join(lines) + "\n"


def git(repo: Path, *args: str) -> str:
    config = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    return subprocess.run(  # noqa: S603 - fixed arguments, test-only
        ["git", "-C", str(repo), *config, *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repo: Path, files: dict[str, str | None]) -> str:
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "c")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    return r


def rec(rid: str, cls: str, datasets: list[str], condition: str) -> ExpectedRecord:
    return ExpectedRecord.model_validate(
        {
            "id": rid,
            "question": MARKER,
            "expected_class": cls,
            "expected_datasets": datasets,
            "tags": {"condition": condition},
        }
    )


RECORDS = [
    rec("syn-1", "ANSWER", ["toy_weekly"], "toy fever"),
    rec("syn-2", "DECLINE_ELSEWHERE", ["toy_gone"], "gizmo pox"),
    rec("syn-3", "OUT_OF_SCOPE", [], "widget measles"),
    rec("syn-4", "ANSWER", ["toy_still"], "plain cough"),
    rec("syn-5", "HANDOFF_GBD", [], "infection"),  # only generic words: never matched
]


def test_catalog_changes_reads_added_modified_and_removed_entries(repo: Path) -> None:
    base = commit(
        repo,
        {
            "catalog/toy_weekly.yaml": entry("toy_weekly", "Toy weekly", "Toy fever"),
            "catalog/toy_gone.yaml": entry("toy_gone", "Gone", "Gizmo pox"),
            "catalog/toy_still.yaml": entry("toy_still", "Still", "Plain cough"),
            "README.md": "x",
        },
    )
    head = commit(
        repo,
        {
            "catalog/toy_weekly.yaml": entry("toy_weekly", "Toy weekly", "Toy fever", "Toy chill"),
            "catalog/toy_gone.yaml": None,
            "catalog/widget.yaml": entry("widget_counts", "Widget counts", "Widget measles"),
            "catalog/sub/ignored.yaml": "id: x\n",
            "contracts/toy_still.yaml": "changed: true\n",
            "README.md": "y",
        },
    )
    changes = catalog_changes(repo, base, head)
    assert [(c.dataset_id, c.status) for c in changes] == [
        ("widget_counts", "added"),  # the entry's own id, not its file name
        ("toy_weekly", "modified"),
        ("toy_gone", "removed"),
    ]
    assert "Gizmo pox" in changes[2].text  # a removed entry is read from the older commit
    found = affected(RECORDS, changes)
    assert found == [
        Affected("syn-1", (L1,)),
        Affected("syn-2", (L2,)),
        Affected("syn-3", (L3,)),
    ]


def test_a_rename_is_a_removal_and_an_addition(repo: Path) -> None:
    text = entry("toy_weekly", "Toy weekly", "Toy fever")
    base = commit(repo, {"catalog/toy_weekly.yaml": text})
    head = commit(
        repo,
        {
            "catalog/toy_weekly.yaml": None,
            "catalog/toy_weekly_v2.yaml": text.replace("id: toy_weekly", "id: toy_weekly_v2"),
        },
    )
    changes = catalog_changes(repo, base, head)
    assert [(c.dataset_id, c.status) for c in changes] == [
        ("toy_weekly_v2", "added"),
        ("toy_weekly", "removed"),
    ]
    # The expected entry was removed (L2); its condition is named in an entry it does not expect (L3).
    assert affected(RECORDS[:1], changes) == [Affected("syn-1", (L2, L3))]


def test_l3_skips_an_entry_the_item_expects_and_reads_coverage_fields_only() -> None:
    weekly = CatalogChange("toy_weekly", "added", entry("toy_weekly", "Toy weekly", "Toy fever"))
    assert affected(RECORDS[:1], [weekly]) == []  # an added entry the item already expects is not L3
    caveat_only = CatalogChange(
        "other", "added", entry("other", "Other", "Nothing", caveat="Toy fever is not here")
    )
    assert affected(RECORDS[:1], [caveat_only]) == []
    assert affected(RECORDS[:1], [CatalogChange("other", "modified", "toy fever: [")]) == [
        Affected("syn-1", (L3,))
    ]


def test_l3_needs_a_condition_tag() -> None:
    bare = ExpectedRecord.model_validate({"id": "syn-9", "expected_class": "OUT_OF_SCOPE"})
    odd = ExpectedRecord.model_validate(
        {"id": "syn-8", "expected_class": "OUT_OF_SCOPE", "tags": "toy fever"}
    )
    change = CatalogChange("x", "added", entry("x", "Toy fever"))
    assert affected([bare, odd], [change]) == []


def test_words_drop_short_and_generic_words() -> None:
    assert words("Hepatitis A virus infection, P&S syphilis and HIV") == {"hepatitis", "syphilis", "hiv"}
    assert coverage_words("id: x\ntitle: Toy\nconditions: {labels: [Fever, Chill]}\ncaveats: [Cough]\n") == {
        "toy",
        "fever",
        "chill",
    }
    assert coverage_words("- not a mapping") == {"not", "mapping"}


def test_parse_name_status_keeps_top_level_catalog_yaml_only() -> None:
    out = "M\0catalog/a.yaml\0A\0catalog/b.yml\0D\0catalog/c.yaml\0M\0catalog/x/d.yaml\0M\0docs/e.yaml\0T\0catalog/f.yaml\0A\0catalog/g.md\0"
    assert parse_name_status(out) == [
        ("modified", "catalog/a.yaml"),
        ("added", "catalog/b.yml"),
        ("removed", "catalog/c.yaml"),
    ]


def test_only_full_commit_shas_are_accepted(repo: Path) -> None:
    head = commit(repo, {"catalog/a.yaml": "id: a\n"})
    assert resolve_commit(repo, head) == head
    for bad in (head[:12], "HEAD", "main", f"--output={head}"[:40], head.upper()):
        with pytest.raises(LabelCheckError, match="full 40-character"):
            resolve_commit(repo, bad)
    with pytest.raises(LabelCheckError, match="git rev-parse failed"):
        resolve_commit(repo, "0" * 40)


def test_report_names_ids_and_fixed_reasons_only() -> None:
    changes = [CatalogChange("toy_weekly", "modified", ""), CatalogChange("widget_counts", "added", "")]
    found = [Affected("syn-1", (L1,)), Affected("syn-3", (L1, L3))]
    md = report_md(changes, found, 5, "a" * 40, "b" * 40)
    assert "catalog aaaaaaaaaaaa to bbbbbbbbbbbb" in md
    assert "- Catalog entries added: `widget_counts`" in md
    assert "- Catalog entries modified: `toy_weekly`" in md
    assert "removed" not in md.split("Items")[0]
    assert "Items to re-audit: 2 of 5." in md
    assert f"| syn-3 | {L1}; {L3} |" in md
    for text in (MARKER, "toy fever", "widget measles", "ANSWER", "OUT_OF_SCOPE"):
        assert text not in md
    assert "No catalog entry changed" in report_md([], [], 5, "a" * 40, "b" * 40)
    assert "| item id |" not in report_md(changes, [], 5, "a" * 40, "b" * 40)


def labels_file(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "labels.jsonl"
    path.write_text("".join(r.model_dump_json() + "\n" for r in RECORDS))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_main_prints_the_report_without_questions_or_labels(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = commit(repo, {"catalog/toy_weekly.yaml": entry("toy_weekly", "Toy weekly", "Toy fever")})
    head = commit(repo, {"catalog/toy_weekly.yaml": entry("toy_weekly", "Toy weekly", "Toy fever", "Chill")})
    labels, digest = labels_file(tmp_path)
    argv = [
        "--labels",
        str(labels),
        "--split",
        "test",
        "--expect-sha",
        digest,
        "--base",
        base,
        "--head",
        head,
    ]
    assert label_check.main([*argv, "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert f"| syn-1 | {L1} |" in out
    assert MARKER not in out and "toy fever" not in out.lower().split("items")[1]


def test_main_reports_errors_without_quoting_labels(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = commit(repo, {"catalog/a.yaml": "id: a\n"})
    labels, digest = labels_file(tmp_path)
    argv = [
        "--labels",
        str(labels),
        "--split",
        "test",
        "--expect-sha",
        digest,
        "--base",
        "main",
        "--head",
        head,
    ]
    assert label_check.main([*argv, "--repo", str(repo)]) == 1
    assert capsys.readouterr().out.startswith("::error::'main' is not a full 40-character commit sha.")
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"id": "x", "question": MARKER, "expected_class": "NOPE"}) + "\n")
    argv = ["--labels", str(bad), "--split", "test", "--base", head, "--head", head, "--repo", str(repo)]
    argv += ["--expect-sha", hashlib.sha256(bad.read_bytes()).hexdigest()]
    assert label_check.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == "::error::label check failed (ValidationError); details are withheld.\n"
    assert MARKER not in captured.out + captured.err
