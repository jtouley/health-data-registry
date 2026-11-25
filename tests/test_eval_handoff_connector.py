"""C1: HANDOFF_CONNECTOR in eval/scorer.py, with HANDOFF_GBD kept as an alias that scores exactly as before.

Synthetic records only: made-up ids, places, datasets and numbers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from eval.report import read_results, tables_md
from eval.scorer import (
    CLASSES,
    ExpectedRecord,
    ScorerError,
    ToolCall,
    load_expected,
    score_question,
)

DS = "toy_abstracts"
SHA = "cd" * 32
PROV = (
    f"Provenance for {DS}: https://example.org/toy; retrieved_at 2026-01-02T03:04:05; "
    f"raw file sha256 {SHA}; snapshot 20260102T030405Z-0a1b2c; contract version 0.1.0 (contract_status: draft)"
)
ROWS = f"title,year,doi\nToy fever in toys,2023,10.5555/toy.2023.77\n# {PROV}\n"


def rec(cls: str, nearest: object, datasets: Sequence[str] = ()) -> ExpectedRecord:
    return ExpectedRecord.model_validate(
        {
            "id": "syn-h",
            "expected_class": cls,
            "expected_datasets": list(datasets),
            "expected_nearest": nearest,
        }
    )


def score(record: ExpectedRecord, answer: str, response: str = "") -> tuple[int, int]:
    calls, responses = (
        ([ToolCall("query_sql", {"sql": f"SELECT * FROM {DS}"})], [response]) if response else ([], [])
    )
    got = score_question(record, calls, responses, answer)
    return got.a.class_score, got.a.target_score


def test_the_new_class_is_listed_after_its_alias() -> None:
    assert CLASSES.index("HANDOFF_CONNECTOR") == CLASSES.index("HANDOFF_GBD") + 1


@pytest.mark.parametrize(
    ("nearest", "targets", "places"),
    [
        ("openalex_connector|pubmed_connector", {"openalex", "pubmed"}, ()),
        (["pubmed"], {"pubmed"}, ()),
        ("GBD_connector|Toyland", {"gbd"}, ("Toyland",)),
        (["gbd", "Toyland", "Gameland"], {"gbd"}, ("Toyland", "Gameland")),
        (" openalex | ", {"openalex"}, ()),
    ],
)
def test_targets_and_places_come_from_expected_nearest(
    nearest: object, targets: set[str], places: tuple[str, ...]
) -> None:
    r = rec("HANDOFF_CONNECTOR", nearest)
    assert r.connector_targets == targets
    assert r.expected_places == places


def test_the_alias_targets_gbd_and_keeps_its_places_as_given() -> None:
    r = rec("HANDOFF_GBD", "Toyland|Gameland")
    assert r.connector_targets == {"gbd"}
    assert r.expected_places == ("Toyland|Gameland",)  # not split: the alias reads expected_nearest as before


@pytest.mark.parametrize("cls", ["ANSWER", "DECLINE_NEAREST", "OUT_OF_SCOPE"])
def test_other_classes_have_no_connector_and_keep_nearest(cls: str) -> None:
    r = rec(cls, "pubmed|Toyland", [DS])
    assert r.connector_targets == frozenset()
    assert r.expected_places == ("pubmed|Toyland",)


@pytest.mark.parametrize("nearest", [None, "Toyland", "openalexx", "connector", ["toy_connector"]])
def test_a_handoff_connector_record_must_name_a_connector(nearest: object) -> None:
    with pytest.raises(ValidationError, match="HANDOFF_CONNECTOR needs a connector"):
        rec("HANDOFF_CONNECTOR", nearest)


CASES = [
    # (nearest, datasets, response, answer, (class, target))
    # OpenAlex or PubMed: the reply may list what this server holds, DOI and year included, beside the hand-off.
    (
        "openalex_connector|pubmed_connector",
        [DS],
        ROWS,
        f"One {DS} abstract from 2023 (doi 10.5555/toy.2023.77). Whether it became a paper: ask the OpenAlex "
        f"connector.\n{PROV}",
        (2, 2),
    ),
    ("openalex|pubmed", [], "", "Ask the PubMed connector for later papers.", (2, 2)),
    ("openalex|pubmed", [], "", "Ask the IHME GBD connector.", (2, 0)),  # handed off, to the wrong connector
    ("openalex|pubmed", [], "", "I can't tell whether it was published.", (0, 0)),
    ("pubmed", [], ROWS, f"Ask PubMed, or use {DS}.", (2, 0)),  # a dataset claimed to fit (R6)
    # GBD target: the HANDOFF_GBD class rule, plus the connector and place parts.
    ("gbd|Toyland", [], "", "Deaths are modelled: ask the IHME GBD connector for Toyland.", (2, 2)),
    ("gbd|Toyland", [], "", "Ask the IHME GBD connector.", (2, 0)),
    ("gbd|Toyland", [], "", "GBD overestimates deaths in Toyland; ask IHME.", (1, 2)),
    (
        "gbd|Toyland",
        [],
        ROWS,
        f"Toyland: 77 deaths; see the GBD connector.\n{PROV}",
        (1, 0),
    ),  # cites a dataset
    ("gbd|Toyland", [], "", "Ask the PubMed connector about Toyland.", (2, 0)),
]


@pytest.mark.parametrize(("nearest", "datasets", "response", "answer", "want"), CASES)
def test_handoff_connector_scores_the_handoff_itself(
    nearest: str, datasets: list[str], response: str, answer: str, want: tuple[int, int]
) -> None:
    assert score(rec("HANDOFF_CONNECTOR", nearest, datasets), answer, response) == want


ALIAS_CASES = [
    # (answer, response): the alias's rows in the class and target tables of test_eval_scorer.py, and their twins.
    ("Deaths are modelled burden: ask the IHME GBD connector for Toyland.", ""),
    ("Ask the GBD connector; GBD overestimates deaths in Toyland.", ""),
    ("Local counts are higher than GBD for Toyland, so ask IHME.", ""),
    (f"Toyland: 77 deaths; see the GBD connector.\n{PROV}", ROWS),
    ("Ask the IHME GBD connector.", ""),
    (f"Ask the IHME GBD connector for Toyland, or use {DS}.", ""),
]


@pytest.mark.parametrize(("answer", "response"), ALIAS_CASES)
def test_the_alias_and_a_gbd_connector_record_agree_whenever_gbd_is_named(answer: str, response: str) -> None:
    alias = score(rec("HANDOFF_GBD", "Toyland"), answer, response)
    general = score(rec("HANDOFF_CONNECTOR", "gbd_connector|Toyland"), answer, response)
    assert alias == general


@pytest.mark.parametrize(
    ("answer", "want"),
    [
        (
            "Toyland had 77 deaths.",
            (0, 2),
        ),  # no hand-off: the alias's Target stays as before (no connector part)
        ("Ask the PubMed connector about Toyland.", (0, 2)),  # PubMed is not a hand-off for the alias
    ],
)
def test_the_alias_scores_a_missing_handoff_as_before(answer: str, want: tuple[int, int]) -> None:
    assert score(rec("HANDOFF_GBD", "Toyland"), answer) == want


def test_rows_keep_the_alias_class_name() -> None:
    got = score_question(rec("HANDOFF_GBD", "Toyland"), [], [], "Ask the IHME GBD connector for Toyland.")
    assert got.to_row("dev")["expected_class"] == "HANDOFF_GBD"
    general = score_question(rec("HANDOFF_CONNECTOR", "pubmed"), [], [], "Ask PubMed.")
    assert general.to_row("dev")["expected_class"] == "HANDOFF_CONNECTOR"


def test_the_report_tables_a_handoff_connector_row(tmp_path: Path) -> None:
    r = score_question(rec("HANDOFF_CONNECTOR", "pubmed"), [], [], "Ask PubMed.")
    path = tmp_path / "run.jsonl"
    path.write_text(json.dumps(r.to_row("test")) + "\n")
    table = tables_md(read_results(path), "test")
    assert "| HANDOFF_CONNECTOR | 1 | 1 |" in table


def test_load_expected_takes_a_reviewed_hash_in_place_of_the_frozen_one(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    path.write_text('{"id": "syn-1", "expected_class": "HANDOFF_CONNECTOR", "expected_nearest": "pubmed"}\n')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ScorerError, match="frozen test sha256"):
        load_expected(path, "test")
    got = load_expected(path, "test", sha256=digest)
    assert [r.connector_targets for r in got] == [{"pubmed"}]
    with pytest.raises(ScorerError, match="frozen dev sha256"):
        load_expected(path, "dev", sha256="0" * 64)
