"""Structured open questions and the conformed dimensions every contract declares (conformed.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry import tools
from health_data_registry.conformed import (
    ConformedInvalid,
    OpenQuestion,
    parse_conformed,
    parse_open_questions,
    valid_place_key,
)
from health_data_registry.registry import RegistryError, load_registry
from tests.conftest import CATALOG, CONTRACT, write_registry

REPO = Path(__file__).resolve().parents[1]
ANSWERED = {
    "id": "q2",
    "question": "Is it?",
    "status": "answered",
    "changesNumbers": True,
    "answer": "Yes.",
    "source": "codebook p. 3",
    "reviewedBy": "a maintainer",
    "reviewedAt": "2026-11-25",
    "issue": "#45",
}


def q(**kw: Any) -> dict[str, Any]:
    return {"id": "q1", "question": "Why?", "status": "open", "changesNumbers": True, **kw}


def test_open_questions_parse_into_records() -> None:
    (a, b) = parse_open_questions([q(issue="#14"), ANSWERED], "c")
    assert a == OpenQuestion("q1", "Why?", "open", True, issue="#14")
    assert a.is_open and a.blocks_review
    assert not b.is_open and not b.blocks_review
    assert b.as_dict() == {
        "id": "q2",
        "question": "Is it?",
        "status": "answered",
        "changes_numbers": True,
        "answer": "Yes.",
        "source": "codebook p. 3",
        "reviewed_by": "a maintainer",
        "reviewed_at": "2026-11-25",
        "issue": "#45",
    }
    wording = parse_open_questions([q(changesNumbers=False)], "c")[0]
    assert wording.is_open and not wording.blocks_review
    assert parse_open_questions(None, "c") == ()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("text", "must be a list"),
        (["plain text"], "not plain text"),
        ([q(extra=1)], "unknown field extra"),
        ([q(id="Q 1")], "must be lower-case"),
        ([q(question=" ")], "has no question text"),
        ([q(status="closed")], "status must be open or answered"),
        ([q(changesNumbers="yes")], "changesNumbers: true or false"),
        ([{**ANSWERED, "answer": None}], "needs answer"),
        ([{**ANSWERED, "source": "", "reviewedBy": None}], "needs source, reviewedBy"),
        ([{**ANSWERED, "reviewedAt": None}], "needs reviewedAt"),
        ([{**ANSWERED, "reviewedAt": "25 Nov"}], "reviewedAt must be a date"),
        ([q(reviewedBy="me")], "can't name a reviewer until it is answered"),
        ([q(reviewedAt="2026-11-25")], "can't name a reviewer"),
        ([q(), q()], "two open questions share an id"),
    ],
)
def test_open_questions_refuse_unclear_records(value: Any, message: str) -> None:
    with pytest.raises(ConformedInvalid, match=message):
        parse_open_questions(value, "c")


def _with_questions(questions: str) -> str:
    old = "  - {property: openQuestions, value: [{id: q1, status: open, changesNumbers: true, question: \"Is 'N' a true zero or not notifiable?\"}]}\n"
    assert old in CONTRACT
    return CONTRACT.replace(old, f"  - {{property: openQuestions, value: {questions}}}\n")


def test_reviewed_only_with_no_number_changing_question_open(tmp_path: Path) -> None:
    reviewed = CATALOG.replace("status: draft", "status: reviewed")
    with pytest.raises(RegistryError, match=r"open questions could still change a number \(q1\)"):
        load_registry(write_registry(tmp_path / "a", catalog=reviewed))
    wording = _with_questions('[{id: q1, status: open, changesNumbers: false, question: "Wording only?"}]')
    ds = load_registry(write_registry(tmp_path / "b", catalog=reviewed, contract=wording)).get("toy_cases")
    assert ds.reviewed and ds.contract.blocking_questions == ()
    assert tools.review_notes(ds) == []
    draft = load_registry(write_registry(tmp_path / "c", contract=wording)).get("toy_cases")
    assert not draft.reviewed
    assert tools.review_notes(draft) == ["Contract is a draft; no maintainer has reviewed it yet."]
    two = _with_questions(
        '[{id: a, status: open, changesNumbers: true, question: "A?"}, {id: b, status: open, changesNumbers: true,'
        ' question: "B?"}]'
    )
    ds2 = load_registry(write_registry(tmp_path / "d", contract=two)).get("toy_cases")
    assert tools.review_notes(ds2)[-1] == "Not reviewed while 2 open questions could change a number: a, b."
    assert [q.id for q in ds2.contract.blocking_questions] == ["a", "b"]


def test_answered_questions_show_their_review(tmp_path: Path) -> None:
    answered = _with_questions(
        '[{id: q1, status: answered, changesNumbers: true, question: "Is N zero?", answer: "No", source: codebook,'
        " reviewedBy: a maintainer, reviewedAt: 2026-11-25}]"
    )
    reg = load_registry(write_registry(tmp_path / "r", contract=answered))
    text = str(tools.describe_dataset(reg, "toy_cases"))
    assert "- Is N zero? Answer: No (source: codebook; a maintainer, 2026-11-25)" in text.splitlines()
    assert "Not yet confirmed with the publisher:" not in text
    rows = tools.list_caveats(reg, source_id="toy_cases", fmt="json")["rows"]  # type: ignore[index]
    assert not [r for r in rows if r[1] == "not yet confirmed"]
    assert ["toy_cases", "review", "Contract is a draft; no maintainer has reviewed it yet."] in rows


def test_open_questions_are_strict_in_contracts(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="not plain text"):
        load_registry(write_registry(tmp_path / "r", contract=_with_questions('["A bare string?"]')))


COLS = frozenset({"area", "yr", "wk", "lbl", "status", "st", "co", "day"})


def conf(**kw: Any) -> dict[str, Any]:
    return {"countedUnit": "cases", "time": {"dateBasis": "report date"}, **kw}


def test_conformed_parses_each_place_match_and_time_grain() -> None:
    c = parse_conformed(
        conf(
            condition={
                "column": "lbl",
                "caseStatusColumn": "status",
                "caseSubtypes": {"A, clinical": "clinical"},
            },
            place={
                "match": "state_name",
                "column": "area",
                "places": {
                    "US": {"key": "USA", "level": "nation"},
                    "NYC": {"parent": "US-36", "note": "the city"},
                },
            },
            time={"mmwrYear": "yr", "mmwrWeek": "wk", "dateBasis": "MMWR week"},
        ),
        "c",
        COLS,
    )
    assert c.declared and c.counted_unit == "cases"
    assert c.condition.applies and c.condition.case_subtypes == (("A, clinical", "clinical"),)
    assert [(d.label, d.key, d.parent, d.level, d.note) for d in c.place.places] == [
        ("US", "USA", None, "nation", None),
        ("NYC", None, "US-36", "partial_area", "the city"),
    ]
    assert (c.time.grain, c.time.year_column, c.time.week_column, c.time.date_basis) == ("mmwr_week", "yr", "wk",
                                                                                          "MMWR week")  # fmt: skip
    assert sorted(c.columns()) == ["area", "lbl", "status", "wk", "yr"]
    fips = parse_conformed(
        conf(place={"match": "fips", "stateColumn": "st", "countyColumn": "co"}), "c", COLS
    )
    assert (fips.place.state_column, fips.place.county_column, fips.time.grain) == ("st", "co", "none")
    fixed = parse_conformed(
        conf(place={"match": "fixed", "fixed": {"name": "EIP", "parent": "USA", "note": "the sites"}},
             time={"mmwrDate": "day", "dateBasis": "onset"}, condition={"label": "CDI"}),
        "c",
        COLS,
    )  # fmt: skip
    assert fixed.place.fixed is not None and fixed.place.fixed.label == "EIP"
    assert (fixed.time.grain, fixed.time.date_column) == ("mmwr_date", "day")
    assert fixed.condition.label == "CDI" and fixed.condition.applies
    year = parse_conformed(conf(time={"year": "yr", "dateBasis": "July 1"}), "c", COLS)
    assert (year.time.grain, year.time.year_column, year.place.match) == ("year", "yr", "none")
    none = parse_conformed(None, "c", COLS)
    assert not none.declared and not none.condition.applies
    assert none.counted_unit.startswith("unresolved") and none.time.date_basis.startswith("unresolved")
    nomatch = parse_conformed(conf(place={"match": "none", "unresolved": "why not"}), "c", COLS)
    assert nomatch.place.unresolved == "why not"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("x", "conformedDimensions must be a mapping"),
        ({**conf(), "extra": 1}, "unknown field extra"),
        ({"time": {"dateBasis": "d"}}, "countedUnit must be a non-empty text"),
        ({"countedUnit": "c"}, "dateBasis must be a non-empty text"),
        (conf(condition={"column": "lbl", "label": "L"}), "a column or the one label"),
        (conf(condition={"caseSubtypes": {"A": "b"}}), "caseSubtypes needs the condition column"),
        (conf(condition={"column": "lbl", "caseSubtypes": {"A": " "}}), "caseSubtypes maps each label"),
        (conf(condition={"column": "lbl", "caseSubtypes": ["A"]}), "caseSubtypes maps each label"),
        (conf(place={"match": "nearest"}), "place.match must be one of"),
        (conf(place={"match": "state_name"}), "place.match state_name needs column"),
        (conf(place={"match": "iso3"}), "place.match iso3 needs column"),
        (conf(place={"match": "fips"}), "place.match fips needs stateColumn"),
        (conf(place={"match": "fixed"}), "place.match fixed needs fixed"),
        (conf(place={"match": "none"}), r"needs unresolved \(why no place key applies\)"),
        (conf(place={"match": "none", "unresolved": "u", "places": {"A": {"key": "USA"}}}), "needs the place column"),
        (conf(place={"match": "state_name", "column": "area", "places": ["A"]}), "maps each declared label"),
        (conf(place={"match": "state_name", "column": "area", "places": {"A": {}}}), "either the key"),
        (conf(place={"match": "state_name", "column": "area", "places": {"A": {"key": "USA", "parent": "USA"}}}),
         "either the key"),
        (conf(place={"match": "state_name", "column": "area", "places": {"A": {"key": "Michigan"}}}),
         "is not a place key"),
        (conf(place={"match": "state_name", "column": "area", "places": {"A": {"parent": "USA"}}}),
         "needs a note saying which part"),
        (conf(place={"match": "state_name", "column": "area", "places": {"A": {"key": "USA", "level": "planet"}}}),
         "level must be one of"),
        (conf(place={"match": "state_name", "column": "area", "places": {"A": {"key": "USA", "odd": 1}}}),
         "unknown field odd"),
        (conf(place={"match": "fixed", "fixed": {"parent": "USA", "note": "n"}}), "needs the name of the one place"),
        (conf(time={"mmwrYear": "yr", "dateBasis": "d"}), "mmwrYear and time.mmwrWeek go together"),
        (conf(time={"year": "yr", "mmwrDate": "day", "dateBasis": "d"}), "declares one grain"),
        (conf(place={"match": "state_name", "column": "nope"}), "names unknown column nope"),
    ],
)  # fmt: skip
def test_conformed_refuses_unclear_declarations(value: Any, message: str) -> None:
    with pytest.raises(ConformedInvalid, match=message):
        parse_conformed(value, "c", COLS)


@pytest.mark.parametrize(
    ("key", "ok"),
    [("USA", True), ("US-26", True), ("US-26163", True), ("US-36/cdc_nndss_weekly:new-york", True),
     ("us-26", False), ("US-2616", False), ("US-26/", False), ("Michigan", False), ("USA/Big Area", False)],
)  # fmt: skip
def test_place_keys(key: str, ok: bool) -> None:
    assert valid_place_key(key) is ok


def test_every_repo_contract_declares_its_conformed_dimensions() -> None:
    """Owner decision, 25 Nov: condition, place and time are declared in each ODCS contract."""
    for ds in load_registry(REPO):
        c = ds.contract.conformed
        assert c.declared, ds.id
        assert not c.counted_unit.startswith("unresolved: the contract declares"), ds.id
        place_cols = {col.name for col in ds.contract.columns if col.partial_places}
        assert place_cols <= {c.place.column}, f"{ds.id}: partialPlaces belong on the conformed place column"
        if ds.entry.conditions.dimension is not None:
            column = ds.semantic.dimension(ds.entry.conditions.dimension).column
            assert c.condition.column == column, f"{ds.id}: the condition key is the catalog's label column"
        for q in ds.contract.open_questions:
            assert q.status in ("open", "answered")
