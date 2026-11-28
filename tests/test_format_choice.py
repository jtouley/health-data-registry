"""choose_output_format: a repeatable Choice from a sourced rubric; unknowns named, never imputed."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import pytest
import yaml

from health_data_registry import cli, format_choice, stats
from health_data_registry.format_choice import (
    CONFIDENCE_MEANS,
    NOT_A_PREDICTION,
    REVIEW_ACT,
    REVIEW_CLOSE,
    REVIEW_DISCUSS,
    RubricError,
    choose_output_format,
    declared_weights,
    load_rubric,
    parse_rubric,
    rubric_text,
)

PAPER = {
    "contribution": "methods_standard",
    "data_maturity": "frozen_dataset",
    "worked_examples": 7,
    "needs_methods_section": True,
}
IDWEEK_PRELIM = {
    "contribution": "empirical",
    "data_maturity": "preliminary_counts",
    "worked_examples": 1,
    "needs_methods_section": False,
    "weeks_to_deadline": 4,
    "conference": "IDWeek",
}
ARGUMENT = {
    "contribution": "argument",
    "data_maturity": "idea",
    "worked_examples": 0,
    "needs_methods_section": False,
}
CASES = [PAPER, IDWEEK_PRELIM, ARGUMENT, {**PAPER, "priorities": ["speed", "effort"]}]


# Most tests check the answer's shape, not its numbers, so they use the shipped rubric with 1,000 draws instead of
# 10,000: the simulation is pure Python and slow under coverage. A test that takes the `real_rubric` fixture runs
# on the shipped rubric itself (the golden numbers, the draws and the shipped file's sha256).
FAST_RUBRIC = parse_rubric(rubric_text().replace("draws: 10000", "draws: 1000", 1))


@pytest.fixture
def real_rubric() -> None:
    """Marks a test that needs the shipped rubric unchanged."""


@pytest.fixture(autouse=True)
def _fast_draws(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if "real_rubric" not in request.fixturenames:
        monkeypatch.setattr(format_choice, "load_rubric", lambda: FAST_RUBRIC)


def test_the_fast_copy_differs_from_the_shipped_rubric_only_in_draws() -> None:
    assert FAST_RUBRIC.draws == 1000 and load_rubric().draws == 10_000
    assert FAST_RUBRIC.seed == load_rubric().seed and FAST_RUBRIC.criteria == load_rubric().criteria


def _json(**kw: Any) -> dict[str, Any]:
    out = choose_output_format(fmt="json", **kw)
    assert isinstance(out, dict)
    return out


def _fresh(**kw: Any) -> dict[str, Any]:
    format_choice._decide.cache_clear()
    return _json(**kw)


# ------------------------------------------------------------------------------------------ the answer


def test_golden_paper_case_picks_the_manuscript_with_p_below_one(real_rubric: None) -> None:
    out = _fresh(**PAPER)
    assert out["type"] == "choice" and out["choice"] == "original_manuscript"
    p = out["probabilities"]
    assert 0.5 < p["original_manuscript"] < 1.0
    # Fixed seed: the golden numbers (to within a few draws, so a last-bit difference in a platform's libm
    # can't fail it; the same platform reproduces them exactly, see the determinism test).
    golden = {
        "abstract_poster": 0.0557,
        "research_letter": 0.0018,
        "original_manuscript": 0.9148,
        "viewpoint": 0.0277,
    }
    for f, g in golden.items():
        assert p[f] == pytest.approx(g, abs=0.005), f
    assert out["draws"] == 10_000 and out["seed"] == 0
    assert out["review"] == REVIEW_ACT
    assert out["excluded"] == []


@pytest.mark.parametrize("kw", CASES)
def test_same_inputs_give_the_same_answer_every_time(kw: dict[str, Any]) -> None:
    first = _fresh(**kw)
    again = _fresh(**kw)
    assert first == again
    assert _fresh(**kw)["probabilities"] == first["probabilities"]


@pytest.mark.parametrize("kw", CASES)
def test_probabilities_sum_to_one_across_every_format(kw: dict[str, Any]) -> None:
    out = _json(**kw)
    p = out["probabilities"]
    assert set(p) == set(load_rubric().format_ids)
    assert math.isclose(sum(p.values()), 1.0, abs_tol=1e-9)
    assert all(0.0 <= v <= 1.0 for v in p.values())
    wins = sum(r["wins"] for r in out["formats"])
    assert math.isclose(wins, out["draws"], abs_tol=1e-6)


@pytest.mark.parametrize("kw", CASES)
def test_confidence_follows_the_choice_formula(kw: dict[str, Any]) -> None:
    out = _json(**kw)
    n = sum(1 for r in out["formats"] if r["status"] == "compared")
    pmax = max(out["probabilities"].values())
    expect = 1.0 if n == 1 else (pmax - 1 / n) / (1 - 1 / n)
    assert out["confidence"] == pytest.approx(expect, abs=1e-4)
    assert out["confidence_means"] == CONFIDENCE_MEANS
    assert out["probabilities"][out["choice"]] == pmax


@pytest.mark.parametrize(
    ("p_max", "n", "expect"),
    [
        (0.25, 4, 0.0),
        (1.0, 4, 1.0),
        (0.625, 4, 0.5),
        (0.5, 2, 0.0),
        (0.9, 2, 0.8),
        (1.0, 1, 1.0),
        (0.1, 4, 0.0),
    ],
)
def test_choice_confidence(p_max: float, n: int, expect: float) -> None:
    assert stats.choice_confidence(p_max, n) == pytest.approx(expect)


def test_choice_confidence_refuses_nonsense() -> None:
    with pytest.raises(stats.StatsError):
        stats.choice_confidence(1.2, 4)
    with pytest.raises(stats.StatsError):
        stats.choice_confidence(0.5, 0)


@pytest.mark.parametrize(("p", "band"), [(0.95, REVIEW_ACT), (0.9, REVIEW_ACT), (0.8999, REVIEW_DISCUSS),
                                         (0.67, REVIEW_DISCUSS), (0.6699, REVIEW_CLOSE), (0.3, REVIEW_CLOSE)])  # fmt: skip
def test_review_bands(p: float, band: str) -> None:
    assert format_choice.review_band(load_rubric(), p) == band


@pytest.mark.parametrize("kw", CASES)
def test_rubric_sha256_version_and_fixed_wording_in_every_answer(kw: dict[str, Any]) -> None:
    sha = format_choice.load_rubric().sha256
    out = _json(**kw)
    assert out["rubric_sha256"] == sha and out["rubric_version"] == format_choice.load_rubric().version
    assert out["rubric_status"] == "draft"
    assert NOT_A_PREDICTION in out["notes"]
    text = choose_output_format(**kw)
    assert isinstance(text, str) and sha in text and NOT_A_PREDICTION in text
    assert "never ranks ideas, fellows or projects" in text


def test_unknowns_are_named_and_never_imputed() -> None:
    out = _json(**PAPER)
    named = {u["criterion"]: u["reason"] for u in out["not_used"]}
    assert named["reach"].startswith("unknown:")
    assert named["acceptance_chance"].startswith("unknown:") and "for any" in named["acceptance_chance"]
    assert "no venue was named" in named["deadline_fit"]
    assert "never an imputed rate" in named["citable_record (abstract_poster upside)"]
    scored = {s["criterion"] for s in out["scores"]}
    assert not scored & {"reach", "acceptance_chance", "deadline_fit"}
    abstract = next(
        s for s in out["scores"] if s["criterion"] == "citable_record" and s["format"] == "abstract_poster"
    )
    assert abstract["probabilities"] == {"2": 1.0}  # only the record it certainly gives


def test_a_conference_without_a_verified_rate_is_named_not_borrowed() -> None:
    out = _json(**{**IDWEEK_PRELIM, "conference": "CROI"})
    reason = next(u["reason"] for u in out["not_used"] if u["criterion"].startswith("citable_record"))
    assert "CROI" in reason and "no verified base rate" in reason
    abstract = next(
        s for s in out["scores"] if s["criterion"] == "citable_record" and s["format"] == "abstract_poster"
    )
    assert abstract["probabilities"] == {"2": 1.0}


def test_the_idweek_rate_enters_as_a_beta_posterior_with_its_source() -> None:
    out = _json(**IDWEEK_PRELIM)
    s = next(
        s for s in out["scores"] if s["criterion"] == "citable_record" and s["format"] == "abstract_poster"
    )
    p5 = s["probabilities"]["5"]
    assert p5 == pytest.approx(237 / 889, abs=0.003)  # the posterior mean of Beta(237, 652)
    assert s["probabilities"]["2"] == pytest.approx(1 - p5, abs=1e-3)
    assert s["score"] == pytest.approx(2 + 3 * p5, abs=1e-3)
    assert "236 of 887" in s["source"] and "Beta(237, 652)" in s["source"] and "Johnson 2022" in s["source"]
    journal = next(
        s
        for s in out["scores"]
        if s["criterion"] == "citable_record" and s["format"] == "original_manuscript"
    )
    assert journal["probabilities"] == {"5": 1.0}


def test_every_score_has_a_legend_a_weight_and_a_source() -> None:
    out = _json(**IDWEEK_PRELIM)
    for s in out["scores"]:
        assert s["type"] == "score" and s["source"] and s["legend"]
        assert all(lv in s["legend"] for lv in s["probabilities"]), s
        assert math.isclose(sum(s["probabilities"].values()), 1.0, abs_tol=1e-3)
        assert 1.0 <= s["score"] <= 5.0
    weights = {s["criterion"]: s["weight"] for s in out["scores"]}
    assert math.isclose(sum(weights.values()), 1.0, abs_tol=1e-3)


def test_gate_leaves_out_a_format_that_does_not_fit_before_scoring() -> None:
    out = _json(**{**PAPER, "contribution": "empirical"})
    assert [x["format"] for x in out["excluded"]] == ["viewpoint"]
    assert out["probabilities"]["viewpoint"] == 0.0
    vp = next(r for r in out["formats"] if r["format"] == "viewpoint")
    # ADR 0018: shown with its scores and the reason, never hidden.
    assert vp["status"].startswith("left out of the ranking") and "contribution = empirical" in vp["reason"]
    assert vp["score_interval_95"] is not None and vp["wins"] == 0.0
    assert any(n.startswith("Left out: viewpoint") for n in out["notes"])


def test_argument_leaves_one_format_and_says_no_weight_can_flip_it() -> None:
    out = _json(**ARGUMENT)
    assert out["choice"] == "viewpoint" and out["probabilities"]["viewpoint"] == 1.0
    assert out["confidence"] == 1.0 and out["flip_condition"]["found"] is False


def test_too_close_to_call_shows_the_flip_condition_first() -> None:
    out = _json(**IDWEEK_PRELIM)
    assert out["review"] == REVIEW_CLOSE
    assert (
        out["notes"][0].startswith("Too close to call.") and out["flip_condition"]["text"] in out["notes"][0]
    )


# ------------------------------------------------------------------------------------------ weights and flips


def test_unit_weights_without_priorities() -> None:
    w = declared_weights(load_rubric(), ["a", "b", "c", "d"], ())
    assert w == {k: 0.25 for k in "abcd"}


def test_rank_order_centroid_weights() -> None:
    rubric = load_rubric()
    crit = ["contribution_fit", "speed", "effort", "citable_record"]
    w = declared_weights(rubric, crit, ("speed", "effort", "citable_record"))
    # Barron and Barrett: K = 4 gives 25/48, 13/48, 7/48, 3/48.
    assert w["speed"] == pytest.approx(25 / 48)
    assert w["effort"] == pytest.approx(13 / 48)
    assert w["citable_record"] == pytest.approx(7 / 48)
    assert w["contribution_fit"] == pytest.approx(3 / 48)
    tied = declared_weights(rubric, crit, ("speed",))
    assert tied["speed"] == pytest.approx(25 / 48)
    assert tied["effort"] == tied["citable_record"] == tied["contribution_fit"] == pytest.approx(23 / 144)
    assert sum(tied.values()) == pytest.approx(1.0)


def test_a_priority_that_cannot_be_scored_is_named() -> None:
    out = _json(**{**PAPER, "priorities": ["reach", "speed"]})
    assert any("reach as a priority" in n for n in out["notes"])
    assert "reach" not in out["weights"]


def test_priorities_change_the_weights_and_say_so() -> None:
    out = _json(**{**PAPER, "priorities": ["speed"]})
    assert out["weights"]["speed"] > out["weights"]["effort"]
    assert any(n.startswith("Weights: your priorities (speed)") for n in out["notes"])
    plain = _json(**PAPER)
    assert any("no priorities were given" in n for n in plain["notes"])


@pytest.mark.parametrize("kw", [PAPER, IDWEEK_PRELIM, {**PAPER, "priorities": ["effort"]}])
def test_flip_condition_is_exact_on_the_declared_weights(kw: dict[str, Any]) -> None:
    rubric = load_rubric()
    out = _json(**kw)
    flip = out["flip_condition"]
    assert flip["found"]
    inp = format_choice._check_inputs(rubric, **{**dict.fromkeys(
        ("weeks_to_deadline", "conference", "priorities")), **kw})  # fmt: skip
    assert isinstance(inp, format_choice.Inputs)
    used, _ = format_choice.evaluate(rubric, inp)
    formats, _ = format_choice.gate(rubric, inp)
    w = declared_weights(rubric, [e.criterion.id for e in used], inp.priorities)

    def top(m: float) -> str:
        ww = {**w, flip["criterion"]: w[flip["criterion"]] * m}
        tot = {f: sum(ww[e.criterion.id] * format_choice._mean_level(e, f) for e in used) for f in formats}
        return max(formats, key=lambda f: tot[f])

    m = flip["multiplier"]
    assert rubric.flip_low <= m <= rubric.flip_high
    step = 1.02 if flip["direction"] == "more" else 0.98
    assert top(1.0) == flip["declared_top"]
    assert top(m * step) == flip["format"]
    assert top(m / step) == flip["declared_top"]


# ------------------------------------------------------------------------------------------ inputs


@pytest.mark.parametrize(
    ("change", "expect"),
    [
        ({"contribution": None}, "contribution must be one of"),
        ({"contribution": ""}, "contribution must be one of"),
        ({"contribution": "opinion"}, "Got 'opinion'"),
        ({"data_maturity": None}, "data_maturity must be one of"),
        ({"data_maturity": "  "}, "data_maturity must be one of"),
        ({"worked_examples": None}, "worked_examples must be a whole number"),
        ({"worked_examples": -1}, "worked_examples must be a whole number"),
        ({"worked_examples": 2.5}, "worked_examples must be a whole number"),
        ({"worked_examples": True}, "worked_examples must be a whole number"),
        ({"needs_methods_section": None}, "needs_methods_section must be true or false"),
        ({"needs_methods_section": "yes"}, "needs_methods_section must be true or false"),
        ({"weeks_to_deadline": -1}, "If the deadline has passed, leave it out"),
        ({"weeks_to_deadline": "soon"}, "weeks_to_deadline must be a whole number"),
        ({"conference": "Not A Meeting"}, "isn't a conference this tool knows"),
        ({"conference": 3}, "conference must be a conference's name"),
        ({"priorities": ["fame"]}, "isn't on the priorities menu"),
        ({"priorities": ["speed", "speed"]}, "listed twice"),
        ({"priorities": 5}, "priorities must be a list"),
    ],
)
def test_bad_inputs_are_refused_in_plain_words(change: dict[str, Any], expect: str) -> None:
    out = _json(**{**PAPER, **change})
    assert out["refused"] == "unknown_input" and expect in out["message"] and out["next_step"]
    text = choose_output_format(**{**PAPER, **change})
    assert isinstance(text, str) and expect in text and "Traceback" not in text


@pytest.mark.parametrize(
    ("change", "same_as"),
    [
        ({"contribution": "Methods/Standard"}, {}),
        ({"contribution": "methods"}, {}),
        ({"contribution": "methods or standard"}, {}),
        ({"data_maturity": "frozen dataset"}, {}),
        ({"data_maturity": "Frozen dataset"}, {}),
        ({"worked_examples": 7.0}, {}),
        ({"priorities": []}, {}),
        ({"priorities": None}, {}),
        ({"conference": ""}, {}),
        ({"weeks_to_deadline": 3}, {}),  # no venue: the deadline is not used, so the answer is the same
    ],
)
def test_equivalent_inputs_give_the_same_answer(change: dict[str, Any], same_as: dict[str, Any]) -> None:
    a, b = _json(**{**PAPER, **change}), _json(**{**PAPER, **same_as})
    assert a["probabilities"] == b["probabilities"] and a["choice"] == b["choice"]


@pytest.mark.parametrize(
    ("examples", "band"), [(0, "0"), (1, "1-2"), (2, "1-2"), (3, "3-5"), (5, "3-5"), (6, "6+")]
)
def test_worked_example_band_boundaries(examples: int, band: str) -> None:
    out = _json(**{**PAPER, "worked_examples": examples})
    rule = next(s["rule"] for s in out["scores"] if s["criterion"] == "room_for_examples")
    assert rule == f"band: worked_examples {band}"


@pytest.mark.parametrize(("weeks", "level"), [(0, 1), (1, 1), (2, 2), (3, 2), (4, 3), (52, 3)])
def test_deadline_counts_only_against_the_abstract_and_only_with_a_venue(weeks: int, level: int) -> None:
    out = _json(**{**IDWEEK_PRELIM, "weeks_to_deadline": weeks})
    rows = {s["format"]: s["score"] for s in out["scores"] if s["criterion"] == "deadline_fit"}
    assert rows["abstract_poster"] == level
    assert {rows[f] for f in ("research_letter", "original_manuscript", "viewpoint")} == {3.0}


# ------------------------------------------------------------------------------------------ the rubric file


def _rubric_with(edit: Any) -> str:
    raw = yaml.safe_load(rubric_text())
    edit(raw)
    return yaml.safe_dump(raw, sort_keys=False)


def test_shipped_rubric_loads_and_is_a_draft() -> None:
    r = load_rubric()
    assert r.status == "draft" and r.draws == 10_000 and r.seed == 0
    assert r.format_ids == ("abstract_poster", "research_letter", "original_manuscript", "viewpoint")
    assert r.open_questions


def _crit(raw: dict[str, Any], cid: str) -> dict[str, Any]:
    return next(c for c in raw["criteria"] if c["id"] == cid)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda r: _crit(r, "speed").pop("basis"), "has no basis"),
        (lambda r: _crit(r, "effort")["basis"].update(cites=[]), "must cite its source"),
        (lambda r: _crit(r, "speed")["basis"].update(cites=["nowhere_2020"]), "cites nowhere_2020"),
        (lambda r: _crit(r, "speed")["basis"].pop("status"), "proposed or reviewed"),
        (lambda r: _crit(r, "speed")["basis"].update(text=""), "say in words why"),
        (lambda r: _crit(r, "speed")["basis"].update(kind="hunch"), "basis kind 'hunch'"),
        (lambda r: r["weights"]["default"].pop("basis"), "weights.default has no basis"),
        (lambda r: r["weights"]["dirichlet"].pop("basis"), "weights.dirichlet has no basis"),
        (lambda r: r["review"].pop("basis"), "review has no basis"),
        (lambda r: r["sources"]["dawes_1979"].pop("doi"), "needs a citation and a PMID, DOI or URL"),
        (lambda r: r["formats"][1]["venues"][0].update(source="blog"), "must cite a listed source"),
        (lambda r: r["formats"][0]["venues"][0].update(reason=""), "unknown limit must say why"),
        (lambda r: _crit(r, "speed")["rule"]["levels"].update(viewpoint=6), "from 1 to 5"),
        (lambda r: _crit(r, "speed")["rule"]["levels"].pop("viewpoint"), "exactly these formats"),
        (lambda r: _crit(r, "data_readiness")["rule"]["levels"].pop("idea"), "exactly these values"),
        (lambda r: _crit(r, "room_for_examples")["rule"]["bands"][1].update(min=2), "leave no gap"),
        (lambda r: _crit(r, "speed")["legend"].pop(5), "legend must describe levels [5]"),
        (lambda r: _crit(r, "reach")["rule"].update(reason=""), "must say why it can't be scored"),
        (
            lambda r: _crit(r, "contribution_fit")["rule"]["levels"]["argument"].update(viewpoint=1),
            "leaves no format",
        ),
        (lambda r: _crit(r, "reach").pop("priority"), "must name exactly one criterion"),
        (lambda r: r["review"].update(act=0.5), "0 < discuss < act"),
        (lambda r: r.update(schema_version=2), "schema_version"),
    ],
)
def test_a_rubric_without_sources_or_with_contradictions_is_refused(edit: Any, message: str) -> None:
    with pytest.raises(
        RubricError, match=message.replace("[", r"\[").replace("]", r"\]").replace("(", r"\(")
    ):
        parse_rubric(_rubric_with(edit))


def test_the_answer_carries_the_shipped_files_sha256(real_rubric: None) -> None:
    out = _fresh(**PAPER)
    assert out["rubric_sha256"] == hashlib.sha256(rubric_text().encode("utf-8")).hexdigest()
    assert out["draws"] == 10_000


def test_the_sha256_changes_with_the_rubric() -> None:
    a = parse_rubric(rubric_text()).sha256
    b = parse_rubric(rubric_text() + "\n# a comment\n").sha256
    assert a != b and len(a) == 64


# ------------------------------------------------------------------------------------------ samplers


def test_beta_draws_match_the_posterior_moments() -> None:
    rng = stats.SplitMix64(1)
    xs = [stats.beta_draw(rng, 237, 652) for _ in range(20_000)]
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    a, b = 237, 652
    assert mean == pytest.approx(a / (a + b), abs=0.001)
    assert var == pytest.approx(a * b / ((a + b) ** 2 * (a + b + 1)), rel=0.05)


@pytest.mark.parametrize("shape", [0.3, 1.0, 2.5, 40.0])
def test_gamma_draws_match_their_mean_and_variance(shape: float) -> None:
    rng = stats.SplitMix64(7)
    xs = [stats.gamma_draw(rng, shape) for _ in range(20_000)]
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    assert mean == pytest.approx(shape, rel=0.04)
    assert var == pytest.approx(shape, rel=0.08)


def test_dirichlet_draws_sum_to_one_and_centre_on_their_weights() -> None:
    rng = stats.SplitMix64(3)
    alphas = [20 * w for w in (0.5, 0.3, 0.2)]
    draws = [stats.dirichlet_draw(rng, alphas) for _ in range(10_000)]
    assert all(math.isclose(sum(d), 1.0) for d in draws)
    for i, w in enumerate((0.5, 0.3, 0.2)):
        assert sum(d[i] for d in draws) / len(draws) == pytest.approx(w, abs=0.01)


def test_samplers_refuse_bad_shapes() -> None:
    with pytest.raises(stats.StatsError):
        stats.gamma_draw(stats.SplitMix64(0), 0.0)
    with pytest.raises(stats.StatsError):
        stats.dirichlet_draw(stats.SplitMix64(0), [])


def test_samplers_repeat_for_a_seed() -> None:
    a, b = stats.SplitMix64(5), stats.SplitMix64(5)
    assert [stats.beta_draw(a, 2, 3) for _ in range(3)] == [stats.beta_draw(b, 2, 3) for _ in range(3)]


def test_splitmix64_matches_the_reference_output() -> None:
    # The reference implementation's first five outputs for seed 1234567 (Vigna, splitmix64.c).
    rng = stats.SplitMix64(1234567)
    assert [rng.next64() for _ in range(5)] == [
        6457827717110365317, 3203168211198807973, 9817491932198370423, 4593380528125082431, 16408922859458223821,
    ]  # fmt: skip
    u = stats.SplitMix64(0)
    assert all(0.0 <= u.random() < 1.0 for _ in range(1000))


# ------------------------------------------------------------------------------------------ CLI and CSV


def test_csv_default_has_format_and_score_rows() -> None:
    text = choose_output_format(**PAPER)
    assert isinstance(text, str)
    head, *rows = text.split("\n")
    assert head.startswith("row,format,criterion,p_first")
    assert sum(r.startswith("format,") for r in rows) == 4
    assert any(r.startswith("score,original_manuscript,contribution_fit") for r in rows)
    assert "# Choice: full original manuscript ranks first" in text


def test_cli_csv_json_and_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    base = ["choose-output-format", "--contribution", "methods_standard", "--data-maturity", "frozen_dataset",
            "--worked-examples", "7", "--needs-methods-section", "yes"]  # fmt: skip
    assert cli.main(base) == 0
    assert capsys.readouterr().out.startswith("row,format")
    assert cli.main([*base, "--json", "--priority", "speed", "--conference", "IDWeek"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["type"] == "choice" and out["inputs"]["priorities"] == ["speed"]
    assert cli.main([*base[:2], "opinion", *base[3:]]) == 1
    assert "contribution must be one of" in capsys.readouterr().out
    assert cli.main([*base[:2], "opinion", *base[3:], "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["refused"] == "unknown_input"
