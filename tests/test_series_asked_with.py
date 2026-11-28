"""A series with asked_with words (vaccination coverage, a percent of deaths) answers only a question that uses one.

Such a figure carries a disease's name but is not a measure of the disease itself, so a plain disease question
never gets it unasked. Synthetic registry only (tests/conftest.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from health_data_registry.registry import GenericSeries, Registry, Series, load_registry
from health_data_registry.tools import series_for
from tests.conftest import CATALOG, SEMANTIC, write_registry

GATED = "Legionellosis vaccination coverage (toy)"
COVERAGE = f"""\
  - condition: legionellosis
    label: {GATED}
    metric: case_rate
    measure: percent
    filters: {{condition: Legionellosis}}
    location_dimension: state
    time_dimension: year
    asked_with: ["vaccination", "coverage", "up to date"]
"""


@pytest.fixture
def reg(tmp_path: Path) -> Registry:
    sem = SEMANTIC.replace("series:\n", "series:\n" + COVERAGE, 1)
    return load_registry(write_registry(tmp_path, semantic=sem))


def _labels(found: list[tuple[Any, Series]]) -> set[str]:
    return {s.label for _, s in found}


def test_a_plain_disease_question_never_gets_the_gated_series(reg: Registry) -> None:
    assert _labels(series_for(reg, "legionellosis")) == {"Legionellosis"}
    assert _labels(series_for(reg, "Legionellosis", measure="percent")) == set()


@pytest.mark.parametrize("question", ["legionellosis vaccination", "Legionellosis vaccination coverage"])
def test_a_question_using_one_of_its_words_gets_it(reg: Registry, question: str) -> None:
    assert _labels(series_for(reg, question)) == {GATED}


def test_generic_series_carry_the_words_when_bound() -> None:
    g = GenericSeries(metric="m", measure="percent", time_dimension="year", asked_with=["coverage"])
    bound = g.bind("MMR", "vaccine")
    assert isinstance(bound, Series)
    assert bound.asked_with == ["coverage"]


# Adult vaccination coverage as the wave-2 coverage sources declare it: each series gated by the words a coverage
# question uses, beside a plain disease series that must keep answering plain questions.
VAX_WORDS = '["vaccination", "vaccine", "vaccinated", "coverage", "immunization", "up to date"]'
VAX = "".join(
    f"""\
  - condition: {condition}
    label: {label}
    metric: case_rate
    measure: percent
    filters: {{condition: Legionellosis}}
    location_dimension: state
    time_dimension: year
    asked_with: {VAX_WORDS}
"""
    for condition, label in (
        ("Shingles vaccination coverage", "Shingles vaccination coverage, adults 50 and older (toy)"),
        ("COVID-19 vaccination coverage", "COVID-19 vaccination coverage, adults (toy)"),
        ("RSV vaccination coverage", "RSV vaccination coverage, adults 60 and older (toy)"),
    )
)


@pytest.fixture
def vax(tmp_path: Path) -> Registry:
    sem = SEMANTIC.replace("series:\n", "series:\n" + VAX, 1)
    return load_registry(write_registry(tmp_path, semantic=sem))


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Shingles vaccination coverage", {"Shingles vaccination coverage, adults 50 and older (toy)"}),
        ("COVID-19 vaccine", {"COVID-19 vaccination coverage, adults (toy)"}),
        ("RSV immunization", {"RSV vaccination coverage, adults 60 and older (toy)"}),
        ("vaccination coverage for RSV", {"RSV vaccination coverage, adults 60 and older (toy)"}),
        ("COVID-19 up to date", {"COVID-19 vaccination coverage, adults (toy)"}),
    ],
)
def test_a_coverage_question_gets_its_own_vaccine_only(
    vax: Registry, question: str, expected: set[str]
) -> None:
    """The disease words and the gate words may come in any order and any of the declared words; the whole
    question still has to name the series, so "COVID-19 vaccine" never also gets RSV or shingles coverage."""
    assert _labels(series_for(vax, question)) == expected


@pytest.mark.parametrize("question", ["vaccination", "vaccine coverage", "immunization", "up to date"])
def test_a_bare_vaccination_question_gets_no_coverage_series(vax: Registry, question: str) -> None:
    """Only gate words name no vaccine, so no adult coverage series answers them."""
    assert _labels(series_for(vax, question)) == set()


def test_a_plain_disease_question_beside_coverage_still_gets_the_disease(vax: Registry) -> None:
    assert _labels(series_for(vax, "legionellosis")) == {"Legionellosis"}
    assert _labels(series_for(vax, "COVID-19")) == set()


def test_a_gated_match_that_isnt_asked_for_leaves_the_catalog_labels_to_answer(tmp_path: Path) -> None:
    """A plain "Legionellosis" question whose only named match is a gated series falls through to the catalog's
    labels (the generic series), instead of being refused because a gated series was named."""
    sem = SEMANTIC[: SEMANTIC.index("series:\n")] + "series:\n" + COVERAGE
    sem += """\
generic_series:
  - metric: case_count
    measure: cases
    location_dimension: state
    time_dimension: year
"""
    vocab = 'conditions: {dimension: condition, source: "toy codebook (synthetic)", labels: [Legionellosis, Syphilis]}\n'
    catalog = CATALOG.replace("contract: contracts/", vocab + "contract: contracts/")
    reg = load_registry(write_registry(tmp_path, catalog=catalog, semantic=sem))
    found = series_for(reg, "Legionellosis")
    assert {s.condition for _, s in found} == {"Legionellosis"}


@pytest.mark.parametrize("words", [["deaths", ""], ["-"], ["  "]])
def test_an_asked_with_entry_with_no_word_is_refused(words: list[str]) -> None:
    """An entry that normalises to nothing would be part of every question and so switch the gate off."""
    with pytest.raises(ValidationError, match="hold no word"):
        GenericSeries(metric="m", measure="percent", time_dimension="year", asked_with=words)
    with pytest.raises(ValidationError, match="hold no word"):
        Series(
            condition="c", label="c", metric="m", measure="percent", time_dimension="year", asked_with=words
        )
