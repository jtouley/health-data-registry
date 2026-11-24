"""eval/scorer.py on synthetic transcripts: every class rule, target, provenance, the fabrication port, protocol B
and the judgement hook. No real question, answer or data: ids, datasets and numbers are made up."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from eval.scorer import (
    DEV_SHA256,
    Case,
    Criterion,
    ExpectedRecord,
    LLMGrader,
    ScorerError,
    ToolCall,
    load_expected,
    observe,
    provenance_complete,
    score_question,
    substantive_numbers,
    transcript_by_question,
    unmatched_numbers,
)

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_toy_weekly"
ALT = "cdc_toy_hai"
SHA = "ab" * 32
PROV = (
    f"Provenance for {DS}: https://data.cdc.gov/d/toy0-toy0; retrieved_at 2026-01-02T03:04:05; "
    f"raw file sha256 {SHA}; snapshot 20260102T030405Z-0a1b2c; contract version 0.1.0 (contract_status: draft)"
)
ALT_PROV = PROV.replace(DS, ALT)
SERIES = f"location,year,cases\nToyland,2024,310\nToyland,2023,1234\n# {PROV}\n"
PROVISIONAL = SERIES + f"# {DS}: Counts for the latest year are provisional.\n"


def rec(cls: str, datasets: Sequence[str] = (DS,), nearest: object = None) -> ExpectedRecord:
    return ExpectedRecord.model_validate(
        {
            "id": "syn-1",
            "expected_class": cls,
            "expected_datasets": list(datasets),
            "expected_nearest": nearest,
        }
    )


def series_call(**args: object) -> ToolCall:
    return ToolCall(
        "get_series", {"condition": "toy fever", "locations": ["Toyland"], "years": [2024], **args}
    )


# ---------------------------------------------------------------------------------------------------------
# R1 Class: one case per rule branch


CLASS_CASES = [
    # (expected class, datasets, nearest, response, answer, class score)
    ("ANSWER", [DS], None, SERIES, f"Toyland reported 310 cases in 2024.\n{PROV}", 2),
    ("ANSWER", [DS], None, SERIES, f"Toyland reported 310 cases. Did you mean 2023?\n{PROV}", 1),
    ("ANSWER", [DS], None, SERIES, f"Toyland reported 310 cases, but {DS} does not cover towns.\n{PROV}", 1),
    ("ANSWER", [DS], None, SERIES, "I could not find that.", 0),
    ("DECLINE_NEAREST", [DS], "Toyland", SERIES, f"Toytown is not served; Toyland had 310 cases.\n{PROV}", 2),
    ("DECLINE_NEAREST", [DS], "Toyland", SERIES, "Toytown is not served. I can give Toyland's numbers.", 2),
    ("DECLINE_NEAREST", [DS], "Toyland", SERIES, "Toytown is not served by any source.", 1),
    ("DECLINE_NEAREST", [DS], "Toyland", SERIES, f"Toytown had 310 cases.\n{PROV}", 0),
    ("DECLINE_ELSEWHERE", [DS], None, SERIES, f"Rates are not published; {DS} has counts, not rates.", 2),
    ("DECLINE_ELSEWHERE", [DS], None, SERIES, f"Rates are not published. Toyland had 310 cases.\n{PROV}", 2),
    ("DECLINE_ELSEWHERE", [DS], None, SERIES, "Rates are not published anywhere here.", 1),
    ("DECLINE_ELSEWHERE", [DS], None, SERIES, f"The rate is 310.\n{PROV}", 0),
    (
        "HANDOFF_GBD",
        [],
        "Toyland",
        "",
        "Deaths are modelled burden: ask the IHME GBD connector for Toyland.",
        2,
    ),
    ("HANDOFF_GBD", [], "Toyland", "", "Ask the GBD connector; GBD overestimates deaths in Toyland.", 1),
    ("HANDOFF_GBD", [], "Toyland", "", "Local counts are higher than GBD for Toyland, so ask IHME.", 1),
    ("HANDOFF_GBD", [], "Toyland", SERIES, f"Toyland had 310 deaths; see the GBD connector.\n{PROV}", 1),
    ("HANDOFF_GBD", [], "Toyland", SERIES, f"Toyland had 310 deaths.\n{PROV}", 0),
    ("DISAMBIGUATE", [DS], None, "", f"Which did you mean in {DS}: toy fever, early or toy fever, late?", 2),
    ("DISAMBIGUATE", [DS], None, SERIES, f"Toy fever: 310 and 1234.\n{PROV}\nWhich one did you mean?", 1),
    ("DISAMBIGUATE", [DS], None, "", f"{DS} has toy fever, early and toy fever, late.", 0),
    ("OUT_OF_SCOPE", [], None, "", "No catalogued source covers toy vaccination coverage.", 2),
    ("OUT_OF_SCOPE", [], None, "", "Did you mean toy fever cases instead?", 1),
    ("OUT_OF_SCOPE", [], None, "", "This is modelled; the IHME connector may help.", 1),
    ("OUT_OF_SCOPE", [], None, SERIES, f"Toyland had 310 cases.\n{PROV}", 0),
    ("OUT_OF_SCOPE", [], None, "", "Let me think about toy vaccination.", 0),
]


@pytest.mark.parametrize(("cls", "datasets", "nearest", "response", "answer", "want"), CLASS_CASES)
def test_class_rule_scores_each_behaviour(
    cls: str, datasets: list[str], nearest: object, response: str, answer: str, want: int
) -> None:
    # Arrange
    calls, responses = ([series_call()], [response]) if response else ([], [])
    # Act
    got = score_question(rec(cls, datasets, nearest), calls, responses, answer)
    # Assert
    assert got.a.class_score == want


# ---------------------------------------------------------------------------------------------------------
# R2 Target


TARGET_CASES = [
    # (expected class, datasets, nearest, answer, target score)
    ("ANSWER", [DS], None, f"Toyland had 310 cases.\n{PROV}", 2),
    ("ANSWER", [DS], None, f"Toyland had 310 cases.\n{ALT_PROV}", 0),
    ("ANSWER", [DS, ALT], None, f"Toyland had 310 cases.\n{PROV}", 1),
    (
        "ANSWER",
        [DS],
        None,
        f"Toyland had 310 cases.\n{PROV}\n{ALT} is a separate source for hospital data.",
        2,
    ),
    ("OUT_OF_SCOPE", [], None, f"Nothing catalogued covers this; {DS} fits best.", 0),
    ("OUT_OF_SCOPE", [], None, f"No catalogued source covers this; {DS} is not a substitute.", 2),
    ("OUT_OF_SCOPE", [], None, f"Not {DS}: nothing catalogued covers this.", 2),
    ("OUT_OF_SCOPE", [], None, f"{DS} does not cover it, but {DS} fits best.", 0),
    ("DECLINE_NEAREST", [DS], "Toyland", f"Toytown is not served; Toyland had 310.\n{PROV}", 2),
    ("DECLINE_NEAREST", [DS], ["Toyland", "Gameland"], f"Not served; Toyland had 310.\n{PROV}", 1),
    ("DECLINE_NEAREST", [DS], ["Toyland", "Gameland"], f"Not served; ask {DS} for Toytown.", 0),
    ("HANDOFF_GBD", [], "Toyland", "Ask the IHME GBD connector for TOYLAND.", 2),
    ("HANDOFF_GBD", [], "Toyland", "Ask the IHME GBD connector.", 0),
    ("HANDOFF_GBD", [], "Toyland", f"Ask the IHME GBD connector for Toyland, or use {DS}.", 0),
]


@pytest.mark.parametrize(("cls", "datasets", "nearest", "answer", "want"), TARGET_CASES)
def test_target_compares_datasets_and_places(
    cls: str, datasets: list[str], nearest: object, answer: str, want: int
) -> None:
    # Arrange
    responses = [SERIES + f"# {ALT_PROV}\n"]
    # Act
    got = score_question(rec(cls, datasets, nearest), [series_call()], responses, answer)
    # Assert
    assert got.a.target_score == want


def test_target_treats_census_denominator_as_neutral_for_get_rate() -> None:
    # Arrange
    census = PROV.replace(DS, "census_county_pop")
    response = f"rate\n12.5\n# {PROV}\n# {census}\n"
    answer = f"The rate was 12.5 per 100000.\n{PROV}\n{census}"
    # Act
    with_rate = score_question(rec("ANSWER"), [ToolCall("get_rate", {"year": 2024})], [response], answer)
    without = score_question(rec("ANSWER"), [series_call()], [response], answer)
    # Assert
    assert with_rate.a.target_score == 2
    assert (
        "census_county_pop"
        not in observe(rec("ANSWER"), [ToolCall("get_rate")], [response], answer).cited_datasets
    )
    assert without.a.target_score == 2  # named beside the expected dataset: R5 leaves it at 2


# ---------------------------------------------------------------------------------------------------------
# R3 Provenance


PROV_CASES = [
    # (response, answer, (score, not applicable))
    (SERIES, "No catalogued source covers this.", (2, True)),
    (SERIES, f"Toyland had 310 cases.\n{PROV}", (2, False)),
    (SERIES, "Toyland had 310 cases.", (0, False)),
    (SERIES, f"Toyland had 310 cases.\n{PROV.replace(SHA, 'ab12')}", (1, False)),
    (SERIES, f"Toyland had 310 cases.\n{PROV.replace('retrieved_at 2026-01-02', 'fetched')}", (1, False)),
    (SERIES, f"Toyland had 310 cases.\n{PROV.replace('snapshot ', 'snap-')}", (1, False)),
    (SERIES, f"Toyland had 310 cases.\n{PROV.replace('contract version', 'contract')}", (1, False)),
    (
        SERIES,
        f"Toyland had 310 cases.\n{PROV.replace('https://data.cdc.gov/d/toy0-toy0', 'CDC')}",
        (1, False),
    ),
    (PROVISIONAL, f"Toyland had 310 cases.\n{PROV}", (1, False)),
    (PROVISIONAL, f"Toyland had 310 cases (provisional).\n{PROV}", (2, False)),
    ("location,cases\nToyland,310\n", f"Toyland had 310 cases.\n{PROV}", (0, False)),
]


@pytest.mark.parametrize(("response", "answer", "want"), PROV_CASES)
def test_provenance_requires_a_complete_line_per_number(
    response: str, answer: str, want: tuple[int, bool]
) -> None:
    # Arrange
    expected = rec("ANSWER")
    # Act
    got = score_question(expected, [series_call()], [response], answer)
    # Assert
    assert (got.a.prov_score, got.a.prov_na) == want


@pytest.mark.parametrize(
    ("line", "complete"),
    [
        (PROV, True),
        (PROV.replace(f"raw file sha256 {SHA}", f"raw file sha256 {SHA}, {SHA} and 3 more"), True),
        (PROV.replace(f"raw file sha256 {SHA}", "read live, not stored (no stored raw file)"), True),
        (PROV.replace(SHA, SHA.upper()), False),
        ("", False),
    ],
)
def test_provenance_line_needs_url_time_sha_and_version(line: str, complete: bool) -> None:
    # Act / Assert
    assert provenance_complete(line) is complete


def test_provenance_uses_the_dataset_whose_response_held_the_number() -> None:
    # Arrange: 310 comes from ALT's response, but the answer carries only DS's provenance line.
    calls = [series_call(), ToolCall("get_series", {"condition": "toy hai"})]
    responses = [f"cases\n999\n# {PROV}\n", f"cases\n310\n# {ALT_PROV}\n"]
    # Act
    got = score_question(rec("ANSWER", [DS, ALT]), calls, responses, f"310 infections.\n{PROV}")
    # Assert
    assert got.a.prov_score == 0


# ---------------------------------------------------------------------------------------------------------
# R4 Fabrication, ported from fabcheck.py


FAB_CASES = [
    # (response, args, answer, unmatched)
    ("cases\n1234\n", {}, "There were 1,234 cases.", ()),
    ("cases\n1,234\n", {}, "There were 1234 cases.", ()),
    ("cases\n1234567\n", {}, "There were 1,234,567 cases.", ()),
    ("share\n12.5\n", {}, "A share of 12.5% of cases.", ()),
    ("share\n0.125\n", {}, "A share of 12.5% of cases.", ("12.5",)),
    ("rate\n2.337\n", {}, "The rate was 2.34 per 100000.", ("2.34",)),
    ("rate\n2.337\n", {}, "The rate was 2.337 per 100000.", ()),
    ("rate\n2.337\n", {}, "The rate was 2.337 per 100,000.", ("100,000",)),
    ("cases\n310\n", {"years": [2024]}, "In 2024 there were 310 cases.", ()),
    ("cases\n310\n", {"years": [2024]}, "There were 2024 cases.", ()),  # year in args matches: kept quirk
    ("cases\n310\n", {}, "There were 2025 cases.", ("2025",)),
    ("cases\n2012\n", {}, "There were 12 cases.", ()),  # substring match: kept quirk
    ("cases\n310\n", {}, "Toy H5N1 cases: 310.", ()),
    ("cases\n310\n", {}, "Cases fell from 310 to 300.", ("300",)),
    ("cases\n310\n", {}, "Cases: 310. Top 5 places, 2 of 10.", ()),
    ("cases\n310\n", {}, "Cases: 310 {P:cdc_toy_weekly 999}.", ()),
    ("cases\n100\n200\n", {}, "Together 300 cases.", ("300",)),  # derived sums are unmatched
]


@pytest.mark.parametrize(("response", "args", "answer", "want"), FAB_CASES)
def test_fabrication_check_matches_baseline_number_rules(
    response: str, args: dict[str, object], answer: str, want: tuple[str, ...]
) -> None:
    # Arrange
    calls = [ToolCall("get_series", args)]
    # Act
    got = unmatched_numbers(answer, calls, [response])
    # Assert
    assert got == want


def test_fabrication_gate_zeroes_total_and_fails() -> None:
    # Arrange
    answer = f"Toyland had 311 cases.\n{PROV}"
    # Act
    got = score_question(rec("ANSWER"), [series_call()], [SERIES], answer)
    # Assert
    assert got.a.fabricated
    assert got.a.total == 0
    assert not got.a.passed
    assert not got.b.passed


@pytest.mark.parametrize(
    ("answer", "want"),
    [
        ("In 2024, during week 45, people aged 65 had 310 cases.", ("310",)),
        (f"310 cases.\n{PROV}", ("310",)),
        ("See https://data.cdc.gov/d/1234-5678 for 1,234 cases and 1234 more.", ("1234",)),
        ("Top 5 of 10.", ()),
        ("", ()),
    ],
)
def test_substantive_numbers_skip_years_times_urls_and_provenance(answer: str, want: tuple[str, ...]) -> None:
    # Act / Assert
    assert substantive_numbers(answer) == want


# ---------------------------------------------------------------------------------------------------------
# Protocol B and the full PASS


def test_pass_needs_every_criterion_at_two() -> None:
    # Act
    got = score_question(rec("ANSWER"), [series_call()], [SERIES], f"Toyland had 310 cases.\n{PROV}")
    # Assert
    assert got.a.passed and got.b.passed
    assert got.a.total == 6
    assert not got.sql_needed


def test_protocol_b_fails_an_answer_that_needed_query_sql() -> None:
    # Arrange
    calls = [series_call(), ToolCall("query_sql", {"sql": "SELECT 1"})]
    responses = ["No loaded source reports 'toy fever'.", SERIES]
    # Act
    got = score_question(rec("ANSWER"), calls, responses, f"Toyland had 310 cases.\n{PROV}")
    # Assert
    assert got.a.passed
    assert got.sql_needed
    assert not got.b.passed
    assert (got.b.class_score, got.b.total) == (got.a.class_score, got.a.total)


def test_protocol_b_keeps_a_pass_when_another_tool_already_returned_the_number() -> None:
    # Arrange
    calls = [series_call(), ToolCall("query_sql", {"sql": "SELECT 1"})]
    # Act
    got = score_question(rec("ANSWER"), calls, [SERIES, SERIES], f"Toyland had 310 cases.\n{PROV}")
    # Assert
    assert not got.sql_needed
    assert got.b.passed


@pytest.mark.parametrize(
    ("cls", "answer", "b_passes"),
    [
        ("OUT_OF_SCOPE", "No catalogued source covers toy vaccination.", True),
        ("DISAMBIGUATE", f"Which did you mean in {DS}: toy fever, early or late?", False),
    ],
)
def test_protocol_b_with_query_sql_and_no_numbers(cls: str, answer: str, b_passes: bool) -> None:
    # Arrange
    datasets = [] if cls == "OUT_OF_SCOPE" else [DS]
    calls = [ToolCall("query_sql", {"sql": "SELECT DISTINCT label"})]
    # Act
    got = score_question(rec(cls, datasets), calls, ["label\ntoy fever, early\n"], answer)
    # Assert
    assert got.a.passed
    assert got.b.passed is b_passes


def test_calls_and_responses_must_pair_up() -> None:
    # Act / Assert
    with pytest.raises(ScorerError, match="1 calls but 0 responses"):
        score_question(rec("ANSWER"), [series_call()], [], "310")


def test_row_has_aggregate_fields_only() -> None:
    # Act
    row = score_question(rec("ANSWER"), [series_call()], [SERIES], f"310 cases.\n{PROV}").to_row("dev")
    # Assert
    assert set(row) == {
        "id", "split", "expected_class", "class_score", "target_score", "prov_score", "fabricated", "pass",
        "pass_strict", "total", "sql_needed", "tools_called",
    }  # fmt: skip
    assert (row["split"], row["pass"], row["tools_called"]) == ("DEV", True, ["get_series"])


# ---------------------------------------------------------------------------------------------------------
# The judgement hook


class FixedGrader:
    def __init__(self, scores: dict[Criterion, int]) -> None:
        self.scores = scores
        self.asked: list[Criterion] = []

    def grade(self, criterion: Criterion, case: Case) -> int:
        self.asked.append(criterion)
        return self.scores.get(criterion, 2)


def test_grader_can_only_lower_a_score() -> None:
    # Arrange
    low = FixedGrader({Criterion.TARGET: 1, Criterion.PROVENANCE: 1})
    answer = "No catalogued source covers this."
    # Act
    lowered = score_question(rec("ANSWER"), [series_call()], [SERIES], f"310 cases.\n{PROV}", grader=low)
    raised = score_question(rec("ANSWER"), [], [], answer, grader=FixedGrader({Criterion.CLASS: 2}))
    # Assert
    assert (lowered.a.target_score, lowered.a.prov_score) == (1, 1)
    assert not lowered.a.passed
    assert raised.a.class_score == 0


def test_grader_fabrication_zero_marks_fabricated_and_na_provenance_is_not_asked() -> None:
    # Arrange
    grader = FixedGrader({Criterion.FABRICATION: 0})
    # Act
    got = score_question(rec("OUT_OF_SCOPE", []), [], [], "No catalogued source covers this.", grader=grader)
    # Assert
    assert got.a.fabricated
    assert Criterion.PROVENANCE not in grader.asked


def test_grader_off_by_default_never_called() -> None:
    # Act
    got = score_question(rec("OUT_OF_SCOPE", []), [], [], "No catalogued source covers this.")
    # Assert
    assert got.a.passed


@pytest.mark.parametrize("bad", [3, -1, True])
def test_grader_out_of_range_is_refused(bad: int) -> None:
    # Act / Assert
    with pytest.raises(ScorerError):
        score_question(
            rec("OUT_OF_SCOPE", []), [], [], "No source.", grader=FixedGrader({Criterion.CLASS: bad})
        )


def test_llm_grader_uses_fixed_prompt_and_parses_json() -> None:
    # Arrange
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return '{"score": 1}'

    case = Case(rec("ANSWER"), (series_call(),), (SERIES,), "310 cases.")
    # Act
    got = LLMGrader(complete).grade(Criterion.TARGET, case)
    # Assert
    assert got == 1
    assert "Criterion: target" in prompts[0]
    assert "get_series" in prompts[0] and "310 cases." in prompts[0]


@pytest.mark.parametrize(
    ("criterion", "reply"),
    [
        (Criterion.CLASS, "two"),
        (Criterion.CLASS, '{"score": 3}'),
        (Criterion.CLASS, '{"score": true}'),
        (Criterion.CLASS, "[2]"),
        (Criterion.FABRICATION, '{"score": 1}'),
    ],
)
def test_llm_grader_refuses_unreadable_replies(criterion: Criterion, reply: str) -> None:
    # Arrange
    case = Case(rec("ANSWER"), (), (), "")
    # Act / Assert
    with pytest.raises(ScorerError):
        LLMGrader(lambda _: reply).grade(criterion, case)


# ---------------------------------------------------------------------------------------------------------
# Files


def test_committed_dev_split_matches_its_frozen_hash() -> None:
    # Arrange
    path = ROOT / "eval" / "dev" / "dev.jsonl"
    # Act
    records = load_expected(path, "dev")
    # Assert
    assert hashlib.sha256(path.read_bytes()).hexdigest() == DEV_SHA256
    assert len(records) == 29


def test_load_expected_refuses_a_changed_file(tmp_path: Path) -> None:
    # Arrange
    path = tmp_path / "dev.jsonl"
    path.write_text(json.dumps({"id": "syn-1", "expected_class": "ANSWER"}) + "\n")
    # Act / Assert
    with pytest.raises(ScorerError, match="run is void"):
        load_expected(path, "dev")


def test_load_expected_refuses_repeated_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    line = json.dumps({"id": "syn-1", "expected_class": "ANSWER"}) + "\n"
    path = tmp_path / "dev.jsonl"
    path.write_text(line * 2)
    monkeypatch.setattr("eval.scorer.DEV_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    # Act / Assert
    with pytest.raises(ScorerError, match="repeats"):
        load_expected(path, "dev")


def test_expected_nearest_accepts_null_string_or_list() -> None:
    # Act / Assert
    assert rec("ANSWER").expected_nearest == ()
    assert rec("DECLINE_NEAREST", nearest="Toyland").expected_nearest == ("Toyland",)
    assert rec("DECLINE_NEAREST", nearest=["A", "B"]).expected_nearest == ("A", "B")


def test_transcript_groups_calls_by_question() -> None:
    # Arrange
    records = [
        {"id": "syn-1", "tool": "suggest_datasets", "args": {"question": "q"}, "result": "r1"},
        {"id": "syn-2", "tool": "get_series", "args": None, "result": "r2"},
        {"id": "syn-1", "tool": "get_series", "args": {}, "result": "r3"},
    ]
    # Act
    got = transcript_by_question(records)
    # Assert
    assert [c.tool for c in got["syn-1"][0]] == ["suggest_datasets", "get_series"]
    assert got["syn-1"][1] == ["r1", "r3"]
    assert got["syn-2"][0][0].args == {}
