"""A series with asked_with words (vaccination coverage, a percent of deaths) answers only a question that uses one.

Such a figure carries a disease's name but is not a measure of the disease itself, so a plain disease question
never gets it unasked. Synthetic registry only (tests/conftest.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry.registry import GenericSeries, Registry, Series, load_registry
from health_data_registry.tools import series_for
from tests.conftest import SEMANTIC, write_registry

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
