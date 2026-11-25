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
    check_fabrication,
    guidance,
    load_expected,
    note_candidates,
    observe,
    provenance_complete,
    says_provisional,
    score_question,
    sql_target_flags,
    structured_fields,
    substantive_numbers,
    transcript_by_question,
    unmatched_numbers,
    unmatched_numbers_legacy,
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
    # A question or caveat beside returned data is still an answer (S2); with no data returned it lowers Class.
    ("ANSWER", [DS], None, SERIES, f"Toyland reported 310 cases. Did you mean 2023?\n{PROV}", 2),
    ("ANSWER", [DS], None, SERIES, f"Toyland reported 310 cases, but {DS} does not cover towns.\n{PROV}", 2),
    ("ANSWER", [DS], None, "", f"Toyland reported 310 cases. Did you mean 2023?\n{PROV}", 1),
    ("ANSWER", [DS], None, "", f"Toyland reported 310 cases, but {DS} does not cover towns.\n{PROV}", 1),
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
# R4 Fabrication: the baseline's substring check, kept behind legacy=True


LEGACY_FAB_CASES = [
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


@pytest.mark.parametrize(("response", "args", "answer", "want"), LEGACY_FAB_CASES)
def test_legacy_fabrication_check_matches_baseline_number_rules(
    response: str, args: dict[str, object], answer: str, want: tuple[str, ...]
) -> None:
    # Arrange
    calls = [ToolCall("get_series", args)]
    # Act
    got = unmatched_numbers(answer, calls, [response], legacy=True)
    # Assert
    assert got == want
    assert unmatched_numbers_legacy(answer, calls, [response]) == want


# ---------------------------------------------------------------------------------------------------------
# R4 Fabrication: token-level matching (N1-N6)


FAB_CASES = [
    # (response, args, answer, unmatched)
    ("cases\n1234\n", {}, "There were 1,234 cases.", ()),
    ("cases\n1,234\n", {}, "There were 1234 cases.", ()),
    ("share\n12.5\n", {}, "A share of 12.5% of cases.", ()),
    ("share\n0.125\n", {}, "A share of 12.5% of cases.", ()),  # fraction x 100
    ("share\n0.125\n", {}, "A share of 0.125 of cases.", ()),
    ("share\n12.5\n", {}, "A share of 0.125 of cases.", ("0.125",)),
    ("rate\n2.337\n", {}, "The rate was 2.34 per 100000.", ()),  # rounded to the decimals shown
    ("rate\n2.337\n", {}, "The rate was 2.3 per 100,000.", ()),
    ("rate\n2.337\n", {}, "The rate was 2.33 per 100,000.", ("2.33",)),
    ("rate\n116\n", {}, "The rate was 116.0 per 100,000.", ()),
    ("cases\n310\n", {"years": [2024]}, "In 2024 there were 310 cases.", ()),
    ("cases\n310\n", {"years": [2024]}, "There were 2024 cases.", ("2024",)),  # a year in args is no count
    ("location,year,cases\nToyland,2024,310\n", {}, "There were 2024 cases.", ("2024",)),  # nor a year cell
    ("cases\n310\n", {}, "In 2025 there were 310 cases.", ("2025",)),  # a year must still come from a tool
    ("Years: 2022-2023.\ncases\n310\n", {}, "From 2023 there were 310 cases.", ()),  # second year of a range
    ("cases\n2012\n", {}, "There were 12 cases.", ("12",)),  # no substring matches
    ("cases\n12\n", {}, "There were 12 cases.", ()),
    ("cases\n310\n", {}, "Toy H5N1 cases: 310.", ()),
    ("cases\n310\n", {}, "Cases fell from 310 to 300.", ("300",)),
    ("cases\n310\n", {}, "Cases: 310. Top 5 places, 2 of 10.", ()),
    ("cases\n310\n", {}, "Cases: 310, and 2 cases more.", ("2",)),  # allowlist skips only non-counts
    ("cases\n310\n", {}, "Cases: 310 {P:cdc_toy_weekly 999}.", ()),
    ("cases\n310 through week 52\n", {}, "310 cases through week 52.", ()),
    ("cases\n310\n", {}, "310 cases through week 52.", ("52",)),
    ("label,last_week\nToy,49\n", {}, "last week with data 49.", ()),  # a week a response returned
    (SERIES, {}, f"Toyland had 310 cases.\n{PROV}", ()),  # the provenance line is not checked here (R3)
]


@pytest.mark.parametrize(("response", "args", "answer", "want"), FAB_CASES)
def test_fabrication_tokens_match_on_value_not_digits(
    response: str, args: dict[str, object], answer: str, want: tuple[str, ...]
) -> None:
    # Arrange
    calls = [ToolCall("get_series", args)]
    # Act
    got = unmatched_numbers(answer, calls, [response])
    # Assert
    assert got == want


WEEKLY = "location,year,week,label,m1\nToyland,2024,1,Toy fever,10\nToyland,2024,2,Toy fever,15\nToyland,2024,3,Toy fever,7\n"
PLACES = (
    "location,year,label,ytd_cases\nToyland,2024,Toy fever,100\nGameland,2024,Toy fever,40\n"
    "US RESIDENTS,2024,Toy fever,140\nNew York,2024,Toy fever,30\nNew York City,2024,Toy fever,20\n"
)
YTD = "location,year,week,label,ytd_cases\nToyland,2024,1,Toy fever,10\nToyland,2024,2,Toy fever,25\n"
TWO_SERIES = (
    "location,year,label,cases\nToyland,2024,Toy auris clinical,30\nToyland,2024,Toy auris screening,70\n"
)
RATES = "location,year,label,rate\nToyland,2024,Toy fever,1.5\nGameland,2024,Toy fever,2.5\n"

DERIVATION_CASES = [
    # (response, answer number, derived, forbidden)
    (WEEKLY, "25", (("25", "sum_across_weeks"),), ()),
    (WEEKLY, "32", (("32", "sum_across_weeks"),), ()),
    (WEEKLY, "17", (), ()),  # weeks 1 and 3 without 2: not a run of weeks, not allowed
    (PLACES, "140", (), ()),  # returned as the US RESIDENTS row itself
    (PLACES, "50", (("50", "sum_across_places"),), ()),
    (PLACES, "280", (), (("280", "total_row_added"),)),
    (YTD, "35", (), (("35", "running_total_summed_across_weeks"),)),
    (TWO_SERIES, "100", (), (("100", "series_added"),)),
    (RATES, "4.0", (), (("4.0", "rate_or_maximum_summed"),)),
]


@pytest.mark.parametrize(("response", "number", "derived", "forbidden"), DERIVATION_CASES)
def test_fabrication_derivations_allowed_only_from_rubric(
    response: str, number: str, derived: tuple[tuple[str, str], ...], forbidden: tuple[tuple[str, str], ...]
) -> None:
    # Arrange
    answer = f"Toy fever: {number} cases."
    # Act
    got = check_fabrication(answer, [ToolCall("query_sql", {"sql": "SELECT 1"})], [response])
    # Assert
    assert got.derived == derived
    assert got.forbidden == forbidden
    assert got.fabricated is (not derived and number not in ("140",))


def test_fabrication_derived_numbers_reported_apart_and_pass() -> None:
    # Arrange
    response = WEEKLY + f"# {PROV}\n"
    answer = f"Toyland had 25 cases in weeks 1-2 of 2024.\n{PROV}"
    # Act
    got = score_question(rec("ANSWER"), [series_call()], [response], answer)
    # Assert
    assert not got.a.fabricated
    assert got.derived_numbers == (("25", "sum_across_weeks"),)
    assert got.unmatched_numbers == ()
    assert got.a.prov_score == 2


def test_fabrication_legacy_flag_restores_substring_gate() -> None:
    # Arrange
    answer = f"Toyland had 12 cases.\n{PROV}"
    response = f"cases\n2012\n# {PROV}\n"
    # Act
    new = score_question(rec("ANSWER"), [series_call()], [response], answer)
    old = score_question(rec("ANSWER"), [series_call()], [response], answer, legacy=True)
    # Assert
    assert new.a.fabricated
    assert not old.a.fabricated


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
        "pass_strict", "total", "sql_needed", "tools_called", "sums_tried",
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


# ---------------------------------------------------------------------------------------------------------
# S2 phrases and S6 provisional


@pytest.mark.parametrize(
    ("answer", "declined"),
    [
        ("Toytown isn't a reporting area; Toyland is.", True),
        ("The toy survey isn't catalogued here.", True),
        ("No toy hospital rate is catalogued.", True),
        ("I can't give a number for Toytown.", True),
        ("Toyland is a reporting area.", False),
    ],
)
def test_decline_phrases_cover_common_wordings(answer: str, declined: bool) -> None:
    # Act
    got = observe(rec("DECLINE_NEAREST", nearest="Toyland"), [], [], answer)
    # Assert
    assert got.declined is declined


@pytest.mark.parametrize(
    ("answer", "asked"),
    [
        ("Tell me which toy fever you mean.", True),
        ("Pick one or more.", True),
        ("Toy fever has two series.", False),
    ],
)
def test_ask_phrases_count_without_question_mark(answer: str, asked: bool) -> None:
    # Act / Assert
    assert observe(rec("DISAMBIGUATE"), [], [], answer).asked_question is asked


@pytest.mark.parametrize(
    ("text", "provisional"),
    [
        ("Counts are provisional.", True),
        ("the publisher marks no provisional or final status for this row", False),
        ("This figure is not provisional.", False),
        ("No final count yet; this one is provisional.", True),
        ("", False),
    ],
)
def test_says_provisional_ignores_negations(text: str, provisional: bool) -> None:
    # Act / Assert
    assert says_provisional(text) is provisional


# ---------------------------------------------------------------------------------------------------------
# S7 structured decline fields, with the phrases as fallback


@pytest.mark.parametrize(
    ("response", "want"),
    [
        ('{"nearest_served": ["Toyland"], "rows": []}', {"nearest_served": ("Toyland",)}),
        ('{"result": {"structuredContent": {"outside_registry": true}}}', {"outside_registry": ("true",)}),
        ('{"outside_registry": false, "did_you_mean": []}', {}),
        ('{"served_elsewhere": {"dataset": "cdc_toy_hai"}}', {"served_elsewhere": ("cdc_toy_hai",)}),
        ("# next_step: ask_which_series\nlocation,value\n", {"next_step": ("ask_which_series",)}),
        (
            'did_you_mean: ["toy fever, early", "toy fever, late"]',
            {"did_you_mean": ("toy fever, early", "toy fever, late")},
        ),
        (
            "did_you_mean: toy fever, early; toy fever, late",
            {"did_you_mean": ("toy fever, early", "toy fever, late")},
        ),
        ("filter_matched_nothing: true", {"filter_matched_nothing": ("true",)}),
        ("{not json", {}),
        ("location,year,value\nToyland,2024,310\n", {}),
        ("unknown_field: x", {}),
    ],
)
def test_structured_fields_read_json_and_lines(response: str, want: dict[str, tuple[str, ...]]) -> None:
    # Act / Assert
    assert structured_fields(response) == want


FIELD_CASES = [
    # (expected class, nearest, response, answer, class score, class source)
    (
        "DECLINE_NEAREST",
        "Toyland",
        '{"nearest_served": ["Toyland"]}',
        "Toytown: Toyland has the figures.",
        2,
        "fields",
    ),
    ("DECLINE_NEAREST", "Toyland", "", "Toytown: Toyland has the figures.", 0, "phrases"),
    (
        "DECLINE_ELSEWHERE",
        None,
        f'{{"served_elsewhere": ["{DS}"]}}',
        f"Use {DS} for case counts.",
        2,
        "fields",
    ),
    ("OUT_OF_SCOPE", None, '{"outside_registry": true}', "Toy coverage: nothing to show.", 2, "fields"),
    ("OUT_OF_SCOPE", None, '{"filter_matched_nothing": true}', "Toy coverage: nothing to show.", 2, "fields"),
    (
        "OUT_OF_SCOPE",
        None,
        '{"next_step": "outside_registry"}',
        "Toy coverage: nothing to show.",
        2,
        "fields",
    ),
    ("OUT_OF_SCOPE", None, "", "Toy coverage: nothing to show.", 0, "phrases"),
    (
        "DISAMBIGUATE",
        None,
        '{"did_you_mean": ["toy fever, early", "toy fever, late"]}',
        "Toy fever, early or late.",
        2,
        "fields",
    ),
    ("DISAMBIGUATE", None, '{"next_step": "ask_which"}', "Toy fever comes in two kinds.", 2, "fields"),
    ("DISAMBIGUATE", None, '{"did_you_mean": ["toy fever, early"]}', "Toy fever, late it is.", 0, "fields"),
]


@pytest.mark.parametrize(("cls", "nearest", "response", "answer", "want", "source"), FIELD_CASES)
def test_class_reads_structured_fields_before_phrases(
    cls: str, nearest: object, response: str, answer: str, want: int, source: str
) -> None:
    # Arrange
    calls, responses = ([series_call()], [response]) if response else ([], [])
    datasets = [] if cls == "OUT_OF_SCOPE" else [DS]
    # Act
    got = score_question(rec(cls, datasets, nearest), calls, responses, answer)
    # Assert
    assert (got.a.class_score, got.class_source) == (want, source)


def test_guidance_maps_next_step_and_fields_to_classes() -> None:
    # Arrange
    responses = ['{"next_step": "handoff_gbd"}', '{"nearest_served": ["Toyland"]}', '{"next_step": "zzz"}']
    # Act
    got = guidance(responses).classes()
    # Assert
    assert got == {"HANDOFF_GBD", "DECLINE_NEAREST"}


# ---------------------------------------------------------------------------------------------------------
# S8 and T1-T3: the dataset and filters in the tool calls


def test_target_counts_called_dataset_when_no_number_is_shown() -> None:
    # Arrange
    calls = [ToolCall("query_sql", {"sql": f"SELECT DISTINCT label FROM {DS}"})]
    answer = "Which did you mean: toy fever, early or toy fever, late?"
    # Act
    asked = score_question(rec("DISAMBIGUATE"), calls, ["label\ntoy fever, early\n"], answer)
    failed = score_question(
        rec("DISAMBIGUATE"), calls, ["That query can't run: no tagged release yet"], answer
    )
    # Assert
    assert asked.a.target_score == 2
    assert failed.a.target_score == 0


SQL_FLAG_CASES = [
    # (sql, flags)
    (f"SELECT sum(m1) FROM {DS} WHERE upper(states) IN ('TOYLAND', 'TOTAL')", {"T1 total row added"}),
    (f"SELECT sum(m1) FROM {DS} WHERE label = 'Toy fever'", {"T1 total row added"}),
    (f"SELECT sum(m1) FROM {DS} WHERE label = 'Toy fever' AND location2 IS NULL", set()),
    (f"SELECT states, sum(m1) FROM {DS} WHERE states IN ('TOYLAND', 'TOTAL') GROUP BY states", set()),
    (
        f"SELECT sum(m1) FROM {DS} WHERE states = 'TOYLAND' AND label IN ('Toy a', 'Toy b')",
        {"T2 two series added"},
    ),
    (
        f"SELECT sum(m1) FROM {DS} WHERE states = 'TOYLAND' AND (label = 'Toy a' OR label = 'Toy b')",
        {"T2 two series added"},
    ),
    (
        f"SELECT label, sum(m1) FROM {DS} WHERE states = 'TOYLAND' AND label IN ('Toy a', 'Toy b') GROUP BY label",
        set(),
    ),
    (f"SELECT sum(m1) FROM {DS} WHERE states = 'TOYLAND' AND label ILIKE 'Toy%'", {"T2 two series added"}),
    (f"SELECT sum(m1) FROM {DS} WHERE states = 'TOYLAND' AND label ILIKE 'Toy a%'", set()),
    (
        f"SELECT sum(m1) FROM {DS} WHERE upper(states) IN ('NEW YORK', 'NEW YORK CITY')",
        {"T3 sub-area added to its parent"},
    ),
    (f"SELECT m1 FROM {DS} WHERE states IN ('TOYLAND', 'TOTAL')", set()),
]


@pytest.mark.parametrize(("sql", "want"), SQL_FLAG_CASES)
def test_sql_target_flags_find_rows_that_must_stay_apart(sql: str, want: set[str]) -> None:
    # Act / Assert
    assert sql_target_flags(sql, ["Toy a", "Toy b"]) == want


def test_target_flag_from_sql_caps_target_at_one() -> None:
    # Arrange
    sql = f"SELECT sum(m1) AS cases FROM {DS} WHERE upper(states) IN ('TOYLAND', 'TOTAL')"
    response = f"cases\n620\n# {PROV}\n"
    answer = f"Toyland had 620 cases.\n{PROV}"
    # Act
    got = score_question(rec("ANSWER"), [ToolCall("query_sql", {"sql": sql})], [response], answer)
    # Assert
    assert got.target_flags == ("T1 total row added",)
    assert got.a.target_score == 1
    assert not got.a.passed


def test_target_flag_ignores_sql_whose_numbers_are_not_shown() -> None:
    # Arrange
    sql = f"SELECT sum(m1) AS cases FROM {DS} WHERE upper(states) IN ('TOYLAND', 'TOTAL')"
    calls = [ToolCall("query_sql", {"sql": sql}), series_call()]
    # Act
    got = score_question(rec("ANSWER"), calls, ["cases\n620\n", SERIES], f"Toyland had 310 cases.\n{PROV}")
    # Assert
    assert got.target_flags == ()
    assert got.a.target_score == 2


@pytest.mark.parametrize(
    ("response", "number", "flag"),
    [
        (TWO_SERIES + f"# {PROV}\n", "100", "T2 two series added"),
        (PLACES + f"# {PROV}\n", "280", "T1 total row added"),
        (PLACES + f"# {PROV}\n", "50", "T3 sub-area added to its parent"),
    ],
)
def test_target_flag_from_derivation(response: str, number: str, flag: str) -> None:
    # Act
    got = score_question(rec("ANSWER"), [series_call()], [response], f"Toy fever: {number} cases.\n{PROV}")
    # Assert
    assert flag in got.target_flags
    assert got.a.target_score == 1


# ---------------------------------------------------------------------------------------------------------
# H1-H5: the hosted run with no data release

NO_RELEASE = (
    "location,year,value,source_id\n\n"
    f"# {DS}: The open data hub (hf://datasets/toy/toy) has no tagged release yet.\n"
)
NO_SOURCE = (
    "No loaded source reports 'toy pox'. Conditions available now: Toy auris (clinical cases), "
    "Toy auris (screening, colonisation), Toy fever. Try one of those, or suggest_datasets."
)
SQL_DOWN = "That query can't run: The open data hub (hf://datasets/toy/toy) has no tagged release yet."
NODATA = "The hosted server returned no data for this, so I can't give the number."


DESCRIBE = f"Toy weekly ({DS})\nConditions you can ask for with get_series:\n- Toy fever: cases\n"


def hosted_case(series: bool) -> tuple[list[ToolCall], list[str]]:
    first = NO_RELEASE if series else NO_SOURCE
    calls = [
        series_call(),
        ToolCall("describe_dataset", {"source_id": DS}),
        ToolCall("query_sql", {"sql": "x"}),
    ]
    return calls, [first, DESCRIBE, SQL_DOWN]


HOSTED_CASES = [
    # (class, nearest, notes, series served, answer, (class, target), rule)
    ("ANSWER", None, "", True, "The source is catalogued." + NODATA, (0, 2), "H1"),
    ("ANSWER", None, "", False, "No loaded source reports this." + NODATA, (0, 0), "H1"),
    (
        "DECLINE_NEAREST",
        "Toyland",
        "",
        True,
        "Toytown isn't a reporting area; Toyland is." + NODATA,
        (2, 2),
        "H2",
    ),
    ("DECLINE_ELSEWHERE", None, "", True, "No toy hospital measure is catalogued." + NODATA, (2, 2), "H2"),
    ("DECLINE_ELSEWHERE", None, "", False, "No loaded source reports this." + NODATA, (1, 0), "H3"),
    ("DECLINE_NEAREST", "Toyland", "", False, "Toytown isn't served; try Toyland." + NODATA, (1, 0), "H3"),
    ("OUT_OF_SCOPE", None, "", False, "No catalogued source covers toy pox.", (2, 2), "H4"),
    ("HANDOFF_GBD", "Toyland", "", False, "Ask the IHME GBD connector for Toyland.", (2, 2), "H4"),
    (
        "DISAMBIGUATE",
        None,
        "Ask: Toy auris clinical, Toy auris screening?",
        False,
        "Which: clinical or screening?",
        (2, 2),
        "H5",
    ),
    (
        "DISAMBIGUATE",
        None,
        "Ask: Toy auris clinical, toy pox, ...?",
        False,
        "Which: clinical or screening?",
        (2, 1),
        "H5",
    ),
    ("DISAMBIGUATE", None, "Ask: toy pox, toy flu?", False, "Which: pox or flu?", (0, 2), "H5"),
    ("DISAMBIGUATE", None, "Ask which toys.", False, "Which toys?", (2, 2), "H5"),
]


@pytest.mark.parametrize(("cls", "nearest", "notes", "series", "answer", "want", "rule"), HOSTED_CASES)
def test_hosted_rules_score_a_run_without_data(
    cls: str, nearest: object, notes: str, series: bool, answer: str, want: tuple[int, int], rule: str
) -> None:
    # Arrange
    datasets = [] if cls in ("OUT_OF_SCOPE", "HANDOFF_GBD") else [DS]
    expected = ExpectedRecord.model_validate(
        {
            "id": "syn-h",
            "expected_class": cls,
            "expected_datasets": datasets,
            "expected_nearest": nearest,
            "notes": notes,
        }
    )
    calls, responses = hosted_case(series)
    # Act
    got = score_question(expected, calls, responses, answer, hosted=True)
    # Assert
    assert (got.a.class_score, got.a.target_score) == want
    assert got.hosted_rule == rule
    assert not got.a.fabricated


def test_hosted_rules_off_for_local_runs_and_for_hosted_runs_with_data() -> None:
    # Arrange
    calls, responses = hosted_case(True)
    answer = "The source is catalogued." + NODATA
    # Act
    local = score_question(rec("ANSWER"), calls, responses, answer)
    served = score_question(
        rec("ANSWER"), [series_call()], [SERIES], f"Toyland had 310 cases.\n{PROV}", hosted=True
    )
    # Assert
    assert local.hosted_rule is None
    assert served.hosted_rule is None
    assert served.a.passed


@pytest.mark.parametrize(
    ("notes", "want"),
    [
        ("Ask: C. auris clinical, coccidioidomycosis, ...?", ({"auris", "clinical"}, {"coccidioidomycosis"})),
        ("Ask: A, B or C; acute or chronic?", ({"acute"}, {"chronic"})),
        ("Ask which diseases; don't build a composite.", ()),
        ("", ()),
    ],
)
def test_note_candidates_parse_the_ask_list(notes: str, want: tuple[set[str], ...]) -> None:
    # Act / Assert
    assert note_candidates(notes) == tuple(frozenset(w) for w in want)


# ---------------------------------------------------------------------------------------------------------
# Fix round of PR 25: one test per finding


STATES = [f"State {chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(55)]
# 55 made-up state counts from 0 to 60: no single count, run of rows or the whole table gives 87, but the
# pair 46 + 41 does, so the old every-pair search would have passed an invented 87.
STATE_COUNTS = [
    50, 48, 54, 12, 51, 15, 52, 25, 47, 51, 14, 12, 33, 31, 22, 46, 1, 1, 50, 17, 30, 16, 12, 44, 38, 22, 28,
    51, 59, 46, 22, 23, 5, 14, 6, 14, 30, 12, 21, 13, 30, 39, 57, 39, 53, 0, 30, 58, 41, 22, 51, 41, 5, 53, 42,
]  # fmt: skip
STATE_TABLE = "location,year,label,cases\n" + "".join(
    f"{p},2024,Toy fever,{v}\n" for p, v in zip(STATES, STATE_COUNTS, strict=True)
)


def test_invented_regional_sum_over_a_55_state_table_fails() -> None:
    # Arrange
    answer = "There were 87 cases in the region in 2024."
    # Act
    got = check_fabrication(answer, [ToolCall("get_series", {"condition": "toy fever"})], [STATE_TABLE])
    # Assert
    assert got.unmatched == ("87",)
    assert got.derived == ()
    assert 0 < got.sums_tried < 2000  # whole table and runs of rows only: 1 + 55 * 54 / 2 candidates


def test_place_sum_allowed_over_places_the_answer_or_filter_names() -> None:
    # Arrange: rows 15 (46) and 48 (41) are far apart in the table.
    a, b = STATES[15], STATES[48]
    named_in_answer = f"{a} and {b} had 87 cases together in 2024."
    named_in_filter = ToolCall("get_series", {"condition": "toy fever", "locations": [a, b]})
    # Act
    by_answer = check_fabrication(named_in_answer, [ToolCall("get_series", {})], [STATE_TABLE])
    by_filter = check_fabrication("87 cases in 2024.", [named_in_filter], [STATE_TABLE])
    # Assert
    assert by_answer.derived == (("87", "sum_across_places"),)
    assert by_filter.derived == (("87", "sum_across_places"),)


def test_sums_tried_reach_the_row() -> None:
    # Act
    got = score_question(rec("ANSWER"), [series_call()], [STATE_TABLE], "87 cases in 2024.")
    # Assert
    assert got.sums_tried > 0
    assert got.to_row("dev")["sums_tried"] == got.sums_tried


ELEVEN_WEEKS = "location,year,week,label,m1\n" + "".join(
    f"Toyland,2024,{w},Toy fever,{v}\n"
    for w, v in zip(range(1, 12), [3, 4, 6, 7, 9, 11, 13, 17, 19, 23, 29], strict=True)
)


@pytest.mark.parametrize(
    ("number", "derived"),
    [
        ("71", True),  # weeks 9 + 10 + 11 (19 + 23 + 29): neighbours in numeric order
        ("55", False),  # weeks 1 + 10 + 11 (3 + 23 + 29): neighbours only when "10" sorts before "2"
    ],
)
def test_weeks_are_summed_in_numeric_order(number: str, derived: bool) -> None:
    # Act
    got = check_fabrication(f"Toy fever: {number} cases.", [ToolCall("get_series", {})], [ELEVEN_WEEKS])
    # Assert
    assert bool(got.derived) is derived
    assert got.fabricated is not derived


def test_hub_unavailable_only_on_the_hubs_own_wording() -> None:
    # Arrange: a guard refusal from query_sql, then a correct get_series answer, in a hosted run.
    calls = [ToolCall("query_sql", {"sql": "SELECT median(x) FROM t"}), series_call()]
    refusal = "That query can't run: the function median is not allowed. The other tools can usually answer."
    answer = f"Toyland reported 310 cases in 2024.\n{PROV}"
    # Act
    got = score_question(rec("ANSWER"), calls, [refusal, SERIES], answer, hosted=True)
    published = score_question(
        rec("ANSWER"),
        [series_call()],
        ["# The open data hub (hf://datasets/toy/toy) has no published release yet, so there is no data."],
        "No data.",
        hosted=True,
    )
    # Assert
    assert got.hosted_rule is None
    assert got.a.class_score == 2
    assert published.hosted_rule == "H1"


@pytest.mark.parametrize(
    ("answer", "declined"),
    [
        ("'-' means not reported.", False),
        ("Week 53 is not available.", False),
        ("There is no data for 2025 yet.", False),
        ("Toy fever is not served here.", True),
        ("No source covers toy pox.", True),
    ],
)
def test_caveats_are_not_declines(answer: str, declined: bool) -> None:
    # Act / Assert
    assert observe(rec("ANSWER"), [], [], answer).declined is declined


@pytest.mark.parametrize(
    ("answer", "asked"),
    [
        ("Would you like the weekly breakdown?", False),
        ("Do you want 2023 as well?", False),
        ("Shall I add the rate?", False),
        ("If you'd like, I can add rates?", False),
        ("Which toy fever did you mean?", True),
    ],
)
def test_follow_up_offers_are_not_clarifying_questions(answer: str, asked: bool) -> None:
    # Act / Assert
    assert observe(rec("DISAMBIGUATE"), [], [], answer).asked_question is asked


def test_answer_with_caveat_and_offer_keeps_class_two() -> None:
    # Arrange
    answer = (
        "Toyland reported 310 cases in 2024. '-' means not reported. "
        f"Would you like the weekly breakdown?\n{PROV}"
    )
    # Act
    got = score_question(rec("ANSWER"), [series_call()], [SERIES], answer)
    # Assert
    assert got.a.class_score == 2
    assert got.b.passed


def test_structured_fields_decide_before_phrases() -> None:
    # Arrange: the phrase says "not served", but the fields name a nearest place the reply never names.
    response = '{"nearest_served": ["Toyland"]}'
    answer = "Toytown is not served."
    # Act
    got = observe(rec("DECLINE_NEAREST", nearest="Toyland"), [series_call()], [response], answer)
    # Assert
    assert got.class_source == "fields"
    assert not got.declined


@pytest.mark.parametrize(
    ("lines", "want"),
    [
        ((PROV, PROV), 2),
        ((PROV, f"Provenance for {DS}: snapshot only"), 1),
        ((f"Provenance for {DS}: snapshot only", PROV), 1),
    ],
)
def test_every_provenance_line_per_dataset_must_be_complete(lines: tuple[str, str], want: int) -> None:
    # Arrange
    answer = "Toyland reported 310 cases in 2024.\n" + "\n".join(lines)
    # Act
    got = score_question(rec("ANSWER"), [series_call()], [SERIES], answer)
    # Assert
    assert got.a.prov_score == want


def test_negation_counts_mentions_case_insensitively() -> None:
    # Arrange: an upper-case negated mention must not hide a lower-case plain one.
    answer = f"Use {DS} for counts. This is not {DS.upper()} rates."
    # Act
    got = observe(rec("DECLINE_ELSEWHERE"), [], [], answer)
    # Assert
    assert DS in got.fitting_datasets


@pytest.mark.parametrize(
    ("response", "answer", "want"),
    [
        ("label,change_pct\nToy,-35\n", "a -35% change", ()),
        ("label,change_pct\nToy,-35\n", "cases fell 35%", ()),
        ("label,change_pct\nToy,35\n", "a -35% change", ("-35",)),
        ("cases\n120\n", "between 120-150 cases", ("150",)),
        ("cases\n120\n150\n", "between 120-150 cases", ()),
        ("cases\n3\n", "3/17 places", ("17",)),
    ],
)
def test_fabrication_reads_signs_and_both_ends_of_a_range(
    response: str, answer: str, want: tuple[str, ...]
) -> None:
    # Act / Assert
    assert unmatched_numbers(answer, [ToolCall("get_series", {})], [response]) == want


# Rule 14 disclosure: the year-range and year-before-"reports" rules, each proved on synthetic text alone.


def test_second_year_of_a_range_is_checked_as_a_year() -> None:
    # Arrange
    response = "location,year,cases\nToyland,2022,310\nToyland,2023,320\n"
    call = [ToolCall("get_series", {})]
    # Act
    seen = unmatched_numbers("Toyland, 2022-2023: 310 and 320 cases.", call, [response])
    invented = unmatched_numbers("Toyland, 2022-2031: 310 cases.", call, [response])
    # Assert
    assert seen == ()
    assert invented == ("2031",)


def test_year_followed_by_reports_is_a_year_not_a_count() -> None:
    # Arrange: "reports" is not a count word, so 2023 is checked as a year and backed by the year column.
    response = "location,year,cases\nToyland,2023,310\n"
    call = [ToolCall("get_series", {})]
    # Act
    got = unmatched_numbers("In Toyland, 2023 the publisher reports 310 cases.", call, [response])
    as_count = unmatched_numbers("Toyland had 2023 reported cases.", call, [response])
    # Assert
    assert got == ()
    assert as_count == ("2023",)
