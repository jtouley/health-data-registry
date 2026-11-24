"""Publication outlook: per-conference stages, base rate only, never a bare or borrowed number."""

from __future__ import annotations

import json
import math
import re
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from health_data_registry import cli, publication
from health_data_registry.publication import (
    BASIS,
    DESIGNS,
    FEATURES_NOT_USED,
    NO_BASE_RATE,
    UNKNOWN,
    get_publication_outlook,
    load_base_rates,
    natural_frequency,
    wilson_interval,
)

CONFERENCES = publication.conference_names()
# A percentage is allowed only as the upper end of an interval "lo–hi%"; any other is a bare point estimate.
PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
RANGE_PCT = re.compile(r"\d+–\d+%|95% (?:interval|CI)")  # an interval, or the interval's level
DECIMAL_RATE = re.compile(r"(?<![\d.])0\.\d+")  # a proportion such as 0.27


def _json(**kw: Any) -> dict[str, Any]:
    out = get_publication_outlook(fmt="json", **kw)
    assert isinstance(out, dict)
    return out


def _rows(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(out["columns"], r, strict=True)) for r in out["rows"]]


def _bare_percentages(text: str) -> list[str]:
    stripped = RANGE_PCT.sub("", text)
    return PCT.findall(stripped)


# ------------------------------------------------------------------------------------------ property tests

# Free-text inputs are echoed back as recorded features, so they are kept to letters: a "5%" typed by the
# user is theirs, not a number the tool presents.
WORDS = st.characters(whitelist_categories=("Lu", "Ll"), whitelist_characters=" _-")
features = st.fixed_dictionaries(
    {},
    optional={
        "conference": st.sampled_from([*CONFERENCES, *(c.lower() for c in CONFERENCES)]),
        "design": st.sampled_from(DESIGNS),
        "data_sources": st.lists(st.text(WORDS, min_size=1, max_size=20).filter(str.strip), max_size=3),
        "condition": st.text(WORDS, max_size=30),
        "geography": st.text(WORDS, max_size=30),
    },
)


@settings(max_examples=150, deadline=None)
@given(features)
def test_every_row_has_basis_and_known_rows_have_interval_and_source(kw: dict[str, Any]) -> None:
    out = _json(**kw)
    rows = _rows(out)
    assert rows
    for r in rows:
        assert r["basis"] and r["model_version"] == "none" and r["calibration"] == "not yet assessed"
        if r["status"] == BASIS:
            assert re.fullmatch(r"\d+–\d+%", r["interval_95"]), r
            assert r["source"] and r["n"] and r["interval_method"] and r["event"]
        else:
            # an unknown stage carries no number at all
            assert r["status"] in (UNKNOWN, NO_BASE_RATE)
            assert not r["interval_95"] and r["n"] is None and not r["source"]
            assert not re.search(r"\d", r["outlook"])


@settings(max_examples=150, deadline=None)
@given(features)
def test_never_a_bare_number_or_probability(kw: dict[str, Any]) -> None:
    out = _json(**kw)
    texts = [str(v) for row in out["rows"] for v in row if isinstance(v, str)] + out["notes"]
    for t in texts:
        assert not _bare_percentages(t), t
        assert not DECIMAL_RATE.search(t), t
    assert not any(isinstance(v, float) for row in out["rows"] for v in row)
    blob = " ".join(texts).lower()
    assert "probability that your idea will be published" not in blob.replace("never a probability", "")
    assert "publication outlook" in blob


@settings(max_examples=100, deadline=None)
@given(features)
def test_features_do_not_change_numbers_yet(kw: dict[str, Any]) -> None:
    conf = kw.get("conference")
    base = _json(conference=conf) if conf else _json()
    out = _json(**kw)
    assert out["rows"] == base["rows"]
    assert any(FEATURES_NOT_USED in n for n in out["notes"])


# ------------------------------------------------------------------------------------------ golden


def _wilson_by_inversion(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Independent route: the p where the score test |p_hat - p| / sqrt(p(1-p)/n) equals z, by bisection."""

    def excess(p: float) -> float:
        return abs(k / n - p) / math.sqrt(p * (1 - p) / n) - z

    def root(lo: float, hi: float) -> float:
        for _ in range(200):
            mid = (lo + hi) / 2
            if (excess(lo) > 0) == (excess(mid) > 0):
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    p_hat = k / n
    return root(1e-12, p_hat), root(p_hat, 1 - 1e-12)


def test_golden_johnson_rate_and_wilson_interval() -> None:
    r = load_base_rates().rates["johnson_2022_idweek"]
    assert (r.k, r.n, r.pmid, r.doi) == (236, 887, "36147600", "10.1093/ofid/ofac415")
    assert round(100 * r.k / r.n, 1) == 26.6  # as reported by the source
    a = wilson_interval(236, 887)
    b = _wilson_by_inversion(236, 887)
    assert a == pytest.approx(b, abs=1e-9)
    assert a == pytest.approx((0.23804, 0.29611), abs=1e-5)
    assert (r.low, r.high) == pytest.approx(a, abs=5e-5)  # the YAML matches the computation
    row = _rows(_json(conference="IDWeek"))[2]
    assert row["interval_95"] == "24–30%" and row["n"] == 887
    assert "Of 887 sampled IDWeek 2017–2018 abstracts, 236 were full papers" in row["outlook"]
    assert "about 1 in 4 (95% interval 24–30%)" in row["outlook"]


def test_subgroup_counts_match_the_paper() -> None:
    r = load_base_rates().rates["johnson_2022_idweek"]
    groups = {label: (n, k) for label, n, k in r.subgroups}
    assert groups["oral/platform presentation"] == (382, 132)
    assert groups["poster presentation"] == (505, 104)
    assert sum(n for n, _ in (groups[g] for g in groups if g.startswith("IDWeek"))) == 887
    assert sum(k for _, k in (groups[g] for g in groups if g.startswith("IDWeek"))) == 236


def test_natural_frequency_stays_inside_its_interval() -> None:
    assert natural_frequency(236 / 887, 0.238, 0.296) == "about 1 in 4"
    # 37.3% must not read "1 in 3" (33%), which is outside 35.3-39.3%
    assert natural_frequency(0.373, 0.353, 0.393) == "about 3 in 8"
    assert natural_frequency(0.5, 0.4999, 0.5001) == "about 1 in 2"
    assert natural_frequency(0.123, 0.1231, 0.1232) == "a share"


# ------------------------------------------------------------------------------------------ unknown stages


def test_unknown_stages_are_never_multiplied_in() -> None:
    rows = _rows(_json(conference="IDWeek"))
    assert [r["stage"] for r in rows] == [1, 2, 3]
    assert [r["status"] for r in rows[:2]] == [UNKNOWN, UNKNOWN]
    # stage 3 is exactly the Johnson interval: nothing from stages 1-2 multiplied into it
    lo, hi = wilson_interval(236, 887)
    assert rows[2]["interval_95"] == f"{round(lo * 100)}–{round(hi * 100)}%"
    assert not any(r["stage"] not in (1, 2, 3) for r in rows)  # no overall row
    assert not hasattr(publication, "overall") and not hasattr(publication, "combine")


@pytest.mark.parametrize("conf", [c for c in CONFERENCES if c != "IDWeek"])
def test_other_conferences_never_borrow_a_rate(conf: str) -> None:
    out = _json(conference=conf)
    rows = _rows(out)
    assert rows[2]["status"] == NO_BASE_RATE
    blob = json.dumps(out)
    for borrowed in ("887", "236", "24–30", "Johnson", "Scherer", "35–39", "3 in 8", "1 in 4"):
        assert borrowed not in blob, (conf, borrowed)


def test_croi_comes_first_and_has_an_open_slot() -> None:
    croi = load_base_rates().conferences[0]
    assert croi.name == "CROI" and croi.abstract_to_paper == UNKNOWN


def test_no_conference_shows_the_table_not_a_pooled_number() -> None:
    out = _json()
    rows = _rows(out)
    assert [r["conference"] for r in rows] == CONFERENCES
    assert all(r["stage"] == 3 for r in rows)
    assert [r["conference"] for r in rows if r["status"] == BASIS] == ["IDWeek"]
    assert any("no pooled number" in n for n in out["notes"])
    assert "Scherer" not in json.dumps(out)


def test_a_conference_may_not_point_at_another_meetings_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    real = publication.resources.files("health_data_registry").joinpath("publication_base_rates.yaml")
    text = real.read_text("utf-8").replace(
        "  - name: CROI\n", "  - name: CROI\n    abstract_to_paper: johnson_2022_idweek\n", 1
    )
    # drop CROI's own "unknown" line so the borrowed id is the one that counts
    head, _, tail = text.partition("  - name: IDWeek")
    head = head.replace("    abstract_to_paper: unknown\n", "", 1)
    text = head + "  - name: IDWeek" + tail

    class Fake:
        def joinpath(self, _: str) -> Fake:
            return self

        def read_text(self, _: str) -> str:
            return text

    publication.load_base_rates.cache_clear()
    monkeypatch.setattr(publication.resources, "files", lambda _: Fake())
    try:
        with pytest.raises(ValueError, match="borrows"):
            publication.load_base_rates()
    finally:
        monkeypatch.undo()
        publication.load_base_rates.cache_clear()


# ------------------------------------------------------------------------------------------ refusals


@pytest.mark.parametrize(
    ("kw", "expect"),
    [
        ({"conference": "Not A Meeting"}, "isn't a conference the outlook covers"),
        ({"design": "banana"}, "isn't a study design the outlook records"),
        ({"data_sources": ["  "]}, "One of the data sources is blank"),
    ],
)
def test_unknown_inputs_are_refused_in_plain_words(kw: dict[str, Any], expect: str) -> None:
    text = get_publication_outlook(**kw)
    assert isinstance(text, str) and expect in text
    assert "Traceback" not in text and "Error" not in text and not re.search(r"\d", text)
    out = get_publication_outlook(fmt="json", **kw)
    assert isinstance(out, dict) and out["refused"] == "unknown_input" and expect in out["message"]


def test_aliases_and_csv_default() -> None:
    text = get_publication_outlook(conference="  idweek ", design="Retrospective_Cohort", data_sources="x")
    assert isinstance(text, str) and text.startswith("conference,stage,step,status,outlook")
    assert "design: retrospective cohort; data sources: x" in text


# ------------------------------------------------------------------------------------------ CLI parity


def test_cli_csv_json_and_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["publication-outlook", "--conference", "IDWeek", "--design", "ecological"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("conference,stage") and "24–30%" in out
    assert cli.main(["publication-outlook", "--json", "--data-source", "a", "--data-source", "b"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["rows"]) == len(CONFERENCES)
    assert any("data sources: a, b" in n for n in data["notes"])
    assert cli.main(["publication-outlook", "--design", "banana"]) == 1
    assert "isn't a study design" in capsys.readouterr().out
    assert cli.main(["publication-outlook", "--json", "--conference", "nope"]) == 1
    assert json.loads(capsys.readouterr().out)["refused"] == "unknown_input"
