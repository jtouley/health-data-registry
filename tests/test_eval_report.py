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


def test_breakdown_is_dev_only_and_names_its_protocol() -> None:
    # Arrange: syn-1 passes A but not B.
    rows = [row("syn-1", strict=False), row("syn-2", passed=False), row("syn-3", cls="OUT_OF_SCOPE")]
    tags = {"syn-1": {"phrasing": "lay"}, "syn-2": {"phrasing": "lay"}, "syn-3": {"phrasing": "vague"}}
    # Act
    a = breakdown_md(rows, tags, "dev")
    b = breakdown_md(rows, tags, "dev", "B")
    # Assert
    assert a == (
        "Passes by tag, protocol A:\n- place_level: None 2/3\n- phrasing: lay 1/2, vague 1/1\n"
        "- intent: None 2/3\n- condition_group: None 2/3\n"
    )
    assert b.startswith("Passes by tag, protocol B:\n")
    assert "- phrasing: lay 0/2, vague 1/1\n" in b
    with pytest.raises(SealedSplitError):
        breakdown_md(rows, tags, "test")


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


def test_cli_refuses_test_questions_before_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    results = write_rows(tmp_path / "run.jsonl", TEST_ROWS)
    out = tmp_path / "report.md"
    # Act
    code = main(["--split", "test", "--results", str(results), "--questions", "--out", str(out)])
    # Assert
    captured = capsys.readouterr()
    assert code == 2
    assert not out.exists()
    assert captured.out == ""
    assert captured.err == (
        "Per-question output is shown for the DEV split only, not 'test'. "
        "TEST is sealed: report its aggregate tables instead.\n"
    )


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


# ---------------------------------------------------------------------------------------------------------
# Fix round of PR 25: exact messages and output, so a changed word or rule is noticed


def test_require_dev_messages_name_the_next_step() -> None:
    # Act / Assert
    with pytest.raises(SealedSplitError) as name:
        require_dev("test")
    with pytest.raises(SealedSplitError) as mixed:
        require_dev("dev", [TEST_ROWS[0]])
    assert str(name.value) == (
        "Per-question output is shown for the DEV split only, not 'test'. "
        "TEST is sealed: report its aggregate tables instead."
    )
    assert str(mixed.value) == (
        "These results include questions outside DEV, and per-question output is shown for DEV only. "
        "Pass a DEV-only results file, or drop --questions to get the aggregate tables."
    )


def test_check_split_messages() -> None:
    # Act / Assert
    with pytest.raises(ValueError, match=r"^unknown split 'held_out'; use dev or test$"):
        tables_md(DEV_ROWS, "held_out")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"^every row must be from TEST$"):
        tables_md(DEV_ROWS, "test")


def test_tables_exact_layout_with_protocol_b() -> None:
    # Arrange: syn-1 passes A only.
    rows = [row("syn-1", strict=False), row("syn-2", cls="OUT_OF_SCOPE")]
    # Act
    md = tables_md(rows, "dev")
    # Assert
    assert md == (
        "#### DEV\n"
        "\n"
        "| class | n | passes | pass rate | 95% Wilson CI | mean total | fabrication fails |\n"
        "|---|---|---|---|---|---|---|\n"
        "| ANSWER | 1 | 1 | 100.0% | [20.7%, 100.0%] | 6.00 | 0 |\n"
        "| OUT_OF_SCOPE | 1 | 1 | 100.0% | [20.7%, 100.0%] | 6.00 | 0 |\n"
        "| **Overall** | 2 | 2 | 100.0% | [34.2%, 100.0%] | 6.00 | 0 |\n"
        "\n"
        "Protocol B (no query_sql), DEV:\n"
        "\n"
        "| class | n | passes | pass rate | 95% Wilson CI |\n"
        "|---|---|---|---|---|\n"
        "| ANSWER | 1 | 0 | 0.0% | [0.0%, 79.3%] |\n"
        "| OUT_OF_SCOPE | 1 | 1 | 100.0% | [20.7%, 100.0%] |\n"
        "| **Overall** | 2 | 1 | 50.0% | [9.5%, 90.5%] |\n"
        "\n"
        "Fabrication fails: 0 of 2.\n"
    )


def test_release_blocked_only_on_test() -> None:
    # Arrange
    fab_dev = row("syn-1").model_copy(update={"fabricated": True, "total": 0, "passed": False})
    # Act
    md = tables_md([fab_dev], "dev")
    # Assert
    assert md.endswith("Fabrication fails: 1 of 1.\n")


def test_mcnemar_exact_output_for_dev_protocol_b() -> None:
    # Arrange: under B, syn-1 regresses and syn-3 is fixed; under A nothing changes.
    before = [row("syn-1"), row("syn-2", passed=False), row("syn-3", cls="OUT_OF_SCOPE", strict=False)]
    after = [row("syn-1", strict=False), row("syn-2", passed=False), row("syn-3", cls="OUT_OF_SCOPE")]
    # Act
    b = mcnemar_md(before, after, "dev", "B")
    a = mcnemar_md(before, after, "dev")
    # Assert
    assert b == (
        "#### McNemar, DEV, protocol B\n"
        "\n"
        "| class | a | b (regressions) | c (fixes) | d | net c-b | p | test |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| ANSWER | 0 | 1 | 0 | 1 | -1 | 1.000 | exact |\n"
        "| OUT_OF_SCOPE | 0 | 0 | 1 | 0 | +1 | 1.000 | exact |\n"
        "| **Overall** | 0 | 1 | 1 | 1 | +0 | 1.000 | exact |\n"
        "\n"
        "Regressed: syn-1.\n"
    )
    assert a.startswith("#### McNemar, DEV, protocol A\n")
    assert "| ANSWER |" not in a
    assert a.endswith("Regressed: none.\n")


def test_mcnemar_on_dev_refuses_a_test_row_before_listing_ids() -> None:
    # Arrange: _check_split already refuses a TEST row on a DEV call; require_dev is the second lock.
    # Act / Assert
    with pytest.raises(ValueError):
        mcnemar_md(DEV_ROWS, [*DEV_ROWS[:2], row("syn-3", "TEST")], "dev")


def test_gap_protocol_b_and_exact_line() -> None:
    # Arrange
    dev = [row("syn-1", strict=False), row("syn-2")]
    test = [row("syn-9", "TEST"), row("syn-8", "TEST")]
    # Act
    a = gap_md(dev, test, "A")
    b = gap_md(dev, test, "B")
    # Assert
    assert a.startswith("DEV - TEST gap, protocol A: +0.0% (DEV 100.0%, TEST 100.0%), 95% CI [")
    assert b.startswith("DEV - TEST gap, protocol B: -50.0% (DEV 50.0%, TEST 100.0%), 95% CI [")
    assert b.endswith("%].\n")
    with pytest.raises(ValueError):
        gap_md(test, dev, "A")


def test_read_results_skips_blank_lines_and_names_the_file(tmp_path: Path) -> None:
    # Arrange
    path = tmp_path / "run.jsonl"
    path.write_text("\n" + json.dumps(DEV_ROWS[0].as_mapping()) + "\n   \n")
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n" + json.dumps(DEV_ROWS[0].as_mapping()) + "\n{}\n")
    # Act
    got = read_results(path)
    # Assert
    assert [r.id for r in got] == ["syn-1"]
    with pytest.raises(ValueError, match=r"^bad.jsonl line 3 is not a valid result row$"):
        read_results(bad)


@pytest.mark.parametrize(
    ("field", "value"),
    [("class_score", 3), ("class_score", -1), ("total", 7), ("total", -1), ("split", "HELD")],
)
def test_result_row_bounds(field: str, value: object) -> None:
    # Arrange
    data = DEV_ROWS[0].as_mapping() | {field: value}
    # Act / Assert
    with pytest.raises(ValueError):
        ResultRow.model_validate(data)


def test_result_row_reads_pass_alias_and_defaults_sql_needed() -> None:
    # Arrange
    data = DEV_ROWS[0].as_mapping()
    del data["sql_needed"]
    # Act
    got = ResultRow.model_validate(data | {"extra": 1})
    # Assert
    assert got.passed is True
    assert got.sql_needed is False
    assert "pass" in got.as_mapping() and "passed" not in got.as_mapping()


def test_cli_adds_the_dev_test_gap(tmp_path: Path) -> None:
    # Arrange
    results = write_rows(tmp_path / "run.jsonl", TEST_ROWS)
    dev = write_rows(tmp_path / "dev.jsonl", DEV_ROWS)
    out = tmp_path / "report.md"
    # Act
    code = main(["--split", "test", "--results", str(results), "--dev-results", str(dev), "--out", str(out)])
    # Assert
    text = out.read_text()
    assert code == 0
    assert "DEV - TEST gap, protocol A:" in text and "DEV - TEST gap, protocol B:" in text
    assert "syn-" not in text


def test_cli_refuses_gap_on_a_dev_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Arrange
    results = write_rows(tmp_path / "run.jsonl", DEV_ROWS)
    # Act
    code = main(["--split", "dev", "--results", str(results), "--dev-results", str(results)])
    # Assert
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == "--dev-results compares a DEV run with a TEST run: use it with --split test.\n"


def test_cli_stdout_has_tag_and_both_mcnemar_tables(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    results = write_rows(tmp_path / "run.jsonl", DEV_ROWS)
    # Act
    code = main(["--split", "dev", "--results", str(results), "--before", str(results), "--tag", "abc"])
    # Assert
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("#### DEV (abc)\n")
    assert "\n\n#### McNemar, DEV, protocol A\n" in out
    assert "#### McNemar, DEV, protocol B\n" in out
    assert "| syn-1 |" not in out
