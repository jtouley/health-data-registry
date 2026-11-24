"""eval/report.py: aggregate tables, McNemar and gap output, and the refusal to show TEST question by question.

Synthetic rows only (ids syn-*); nothing here reads a real run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.report import (
    ResultRow,
    SealedSplitError,
    breakdown_md,
    gap_md,
    main,
    mcnemar_md,
    question_rows_md,
    read_results,
    require_dev,
    tables_md,
)


def row(
    qid: str, split: str = "DEV", cls: str = "ANSWER", passed: bool = True, strict: bool | None = None
) -> ResultRow:
    return ResultRow.model_validate(
        {
            "id": qid,
            "split": split,
            "expected_class": cls,
            "class_score": 2 if passed else 1,
            "target_score": 2,
            "prov_score": 2,
            "fabricated": False,
            "pass": passed,
            "pass_strict": passed if strict is None else strict,
            "total": 6 if passed else 5,
        }
    )


DEV_ROWS = [row("syn-1"), row("syn-2", passed=False), row("syn-3", cls="OUT_OF_SCOPE", strict=True)]
TEST_ROWS = [row("syn-9", "TEST"), row("syn-8", "TEST", passed=False)]


def write_rows(path: Path, rows: list[ResultRow]) -> Path:
    path.write_text("".join(json.dumps(r.as_mapping()) + "\n" for r in rows))
    return path


# ---------------------------------------------------------------------------------------------------------
# The sealed-split guard


@pytest.mark.parametrize(
    "split", ["test", "TEST", " test ", "Test", "held_out", "sealed", "", "dev2", "de v"]
)
def test_question_rows_refused_for_any_split_but_dev(split: str) -> None:
    # Act / Assert
    with pytest.raises(SealedSplitError, match="DEV split only"):
        question_rows_md(DEV_ROWS, split)


@pytest.mark.parametrize("split", [None, 0, b"dev", ["dev"]])
def test_question_rows_refused_for_a_split_that_is_not_text(split: object) -> None:
    # Act / Assert
    with pytest.raises(SealedSplitError):
        question_rows_md(DEV_ROWS, split)  # type: ignore[arg-type]


def test_question_rows_refused_when_a_test_row_is_passed_as_dev() -> None:
    # Arrange
    mixed = [*DEV_ROWS, TEST_ROWS[0]]
    # Act / Assert
    with pytest.raises(SealedSplitError, match="outside DEV"):
        question_rows_md(mixed, "dev")
    with pytest.raises(SealedSplitError):
        breakdown_md(TEST_ROWS, {}, "dev")


@pytest.mark.parametrize("split", ["dev", "DEV", " Dev "])
def test_question_rows_shown_for_dev(split: str) -> None:
    # Act
    md = question_rows_md(DEV_ROWS, split)
    # Assert
    assert md.count("| syn-") == 3
    assert "| syn-2 | ANSWER | 1 | 2 | 2 | no | FAIL | FAIL |" in md


def test_require_dev_with_no_rows_still_checks_the_name() -> None:
    # Act / Assert
    require_dev("dev")
    with pytest.raises(SealedSplitError):
        require_dev("test")


def test_breakdown_is_dev_only_and_counts_protocol_b() -> None:
    # Arrange
    tags = {"syn-1": {"phrasing": "lay"}, "syn-2": {"phrasing": "lay"}, "syn-3": {"phrasing": "vague"}}
    # Act
    md = breakdown_md(DEV_ROWS, tags, "dev")
    # Assert
    assert "- phrasing: lay 1/2, vague 1/1" in md
    with pytest.raises(SealedSplitError):
        breakdown_md(DEV_ROWS, tags, "test")


# ---------------------------------------------------------------------------------------------------------
# Aggregates


def test_tables_use_the_baseline_layout() -> None:
    # Act
    md = tables_md(DEV_ROWS, "dev", tag="syn")
    # Assert
    assert md.startswith("#### DEV (syn)\n")
    assert "| ANSWER | 2 | 1 | 50.0% | [9.5%, 90.5%] | 5.50 | 0 |" in md
    assert "| **Overall** | 3 | 2 | 66.7% |" in md
    assert "Protocol B" not in md
    assert "Fabrication fails: 0 of 3." in md


def test_tables_add_protocol_b_and_block_release_on_fabrication() -> None:
    # Arrange
    fab = row("syn-9", "TEST").model_copy(update={"fabricated": True, "total": 0, "passed": False})
    rows = [row("syn-8", "TEST", strict=False), fab]
    # Act
    md = tables_md(rows, "test")
    # Assert
    assert "Protocol B (no query_sql), TEST:" in md
    assert "Fabrication fails: 1 of 2. Release is blocked." in md
    assert "syn-" not in md


@pytest.mark.parametrize(("split", "rows"), [("dev", TEST_ROWS), ("test", DEV_ROWS), ("held_out", DEV_ROWS)])
def test_tables_refuse_rows_from_another_split(split: str, rows: list[ResultRow]) -> None:
    # Act / Assert
    with pytest.raises(ValueError):
        tables_md(rows, split)  # type: ignore[arg-type]


def test_mcnemar_lists_regressed_ids_for_dev() -> None:
    # Arrange
    after = [row("syn-1", passed=False), row("syn-2"), row("syn-3", cls="OUT_OF_SCOPE")]
    # Act
    md = mcnemar_md(DEV_ROWS, after, "dev")
    # Assert
    assert "| **Overall** | 1 | 1 | 1 | 0 | +0 | 1.000 | exact |" in md
    assert "| ANSWER | 0 | 1 | 1 | 0 | +0 | 1.000 | exact |" in md
    assert "Regressed: syn-1." in md


def test_mcnemar_withholds_test_ids() -> None:
    # Arrange
    after = [row("syn-9", "TEST", passed=False), row("syn-8", "TEST")]
    # Act
    md = mcnemar_md(TEST_ROWS, after, "test", "B")
    # Assert
    assert "Regressed questions: 1 (ids withheld: TEST is sealed)." in md
    assert "syn-" not in md


def test_gap_reports_difference_with_interval() -> None:
    # Act
    md = gap_md(DEV_ROWS, TEST_ROWS, "A")
    # Assert
    assert md.startswith("DEV - TEST gap, protocol A: +16.7% (DEV 66.7%, TEST 50.0%), 95% CI [")


# ---------------------------------------------------------------------------------------------------------
# Files and the command line


def test_read_results_refuses_malformed_and_repeated_rows(tmp_path: Path) -> None:
    # Arrange
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "syn-1", "split": "DEV"}\n')
    twice = write_rows(tmp_path / "twice.jsonl", [DEV_ROWS[0], DEV_ROWS[0]])
    # Act / Assert
    with pytest.raises(ValueError, match="line 1"):
        read_results(bad)
    with pytest.raises(ValueError, match="twice"):
        read_results(twice)


def test_cli_refuses_test_questions_before_writing(tmp_path: Path) -> None:
    # Arrange
    results = write_rows(tmp_path / "run.jsonl", TEST_ROWS)
    out = tmp_path / "report.md"
    # Act / Assert
    with pytest.raises(SealedSplitError):
        main(["--split", "test", "--results", str(results), "--questions", "--out", str(out)])
    assert not out.exists()


def test_cli_writes_test_aggregates_without_ids(tmp_path: Path) -> None:
    # Arrange
    results = write_rows(tmp_path / "run.jsonl", TEST_ROWS)
    before = write_rows(tmp_path / "before.jsonl", [row("syn-9", "TEST", passed=False), row("syn-8", "TEST")])
    out = tmp_path / "report.md"
    # Act
    code = main(["--split", "test", "--results", str(results), "--before", str(before), "--out", str(out)])
    # Assert
    text = out.read_text()
    assert code == 0
    assert "#### TEST" in text and "McNemar, TEST, protocol A" in text and "McNemar, TEST, protocol B" in text
    assert "syn-" not in text


def test_cli_prints_dev_questions_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Arrange
    results = write_rows(tmp_path / "run.jsonl", DEV_ROWS)
    # Act
    code = main(["--split", "dev", "--results", str(results), "--questions"])
    # Assert
    assert code == 0
    assert capsys.readouterr().out.count("| syn-") == 3
