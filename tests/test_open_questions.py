"""Structured open questions in a contract: {id, question, columns, answeredBy}; only the question is shown."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from health_data_registry.registry import Contract, RegistryError, parse_contract
from tests.test_split_part import _contract


def _with(questions: Any) -> Contract:
    doc = copy.deepcopy(dict(_contract().raw))
    doc["customProperties"] = [*doc["customProperties"], {"property": "openQuestions", "value": questions}]
    return parse_contract(doc, expected_id="toy_cov")


def test_plain_and_structured_questions_read_as_their_text() -> None:
    c = _with(
        [
            "Is NA suppression?",
            {
                "id": "ci_level",
                "question": "  Is the interval 95%?  ",
                "columns": ["ci_lower", "ci_upper"],
                "answeredBy": "publisher_codebook",
            },
            {"id": "who_answers", "question": "Which place is overall?", "answeredBy": "maintainer_review"},
            {"id": "bare", "question": "Anything else?"},
        ]
    )
    assert c.open_questions == (
        "Is NA suppression?",
        "Is the interval 95%?",
        "Which place is overall?",
        "Anything else?",
    )


def test_no_open_questions_is_an_empty_tuple() -> None:
    assert _contract().open_questions == ()


@pytest.mark.parametrize(
    ("questions", "message"),
    [
        ("one question", "openQuestions must be a list"),
        ([{"question": "No id?"}], "needs an id"),
        ([{"id": "Bad Id", "question": "Bad id?"}], "needs an id"),
        ([{"id": "no_text", "question": "  "}], "needs an id"),
        ([{"id": "extra", "question": "Extra key?", "blocks": "x"}], "needs an id"),
        ([{"id": "twice", "question": "A?"}, {"id": "twice", "question": "B?"}], "twice is used twice"),
        ([{"id": "cols", "question": "A?", "columns": "ci_lower"}], "cols needs columns as a list"),
        ([{"id": "cols", "question": "A?", "columns": ["nope"]}], "it doesn't have nope$"),
        ([{"id": "who", "question": "A?", "answeredBy": "a model"}], "answeredBy must be one of"),
    ],
)
def test_a_malformed_structured_question_is_refused(questions: Any, message: str) -> None:
    with pytest.raises(RegistryError, match=f"^toy_cov: .*{message}"):
        _with(questions)


def test_columns_message_lists_only_unknown_columns() -> None:
    with pytest.raises(RegistryError) as e:
        _with([{"id": "cols", "question": "A?", "columns": ["ci_lower", "zz", "aa"]}])
    assert str(e.value).endswith("it doesn't have aa, zz")
