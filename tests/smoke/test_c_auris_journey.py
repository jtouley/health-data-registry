"""Smoke test: the C. auris in Michigan reference journey, run against the built MCP server on a synthetic lake.

A clinician asks about C. auris in Michigan in 2024. The scripted client calls the real tools through
server.py, in the order a client would, and every step asserts what the server must do: the right source, the
clinical and screening series kept apart and never summed (hard rule 2), the publisher's flags and the Census
denominator carried, total rows left out, and provenance on every number (source URL, retrieved_at, sha256 of the
raw bytes, version; hard rule 8). The four DEV trial questions (eval/dev/dev.jsonl, tag source=trial) are then
scored with eval/scorer.py, the same rules the behaviour eval applies, so the two never drift apart.

Only the NNDSS weekly table is catalogued for C. auris on main. CDC's C. auris tracking page and the AR Lab
Network are named in the caveats but have no catalog entry, so their steps check that the server says so and
shows no number for them.

Behaviour the server doesn't have yet is a strict xfail naming its issue, with raises=AssertionError so only the
missing behaviour counts as the expected failure. Each turns into a failure when its issue lands, and whoever
merges that fix removes the mark. The module's conftest blocks every connection off this machine.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from eval.scorer import (
    PROVENANCE_RE,
    ExpectedRecord,
    check_fabrication,
    data_returned,
    load_expected,
    provenance_complete,
    provenance_ids,
    score_question,
    structured_fields,
)
from tests.smoke import client
from tests.smoke.conftest import NetworkBlocked
from tests.smoke.fixtures import (
    CENSUS,
    NNDSS,
    NNDSS_URL,
    ROOT,
    Journey,
    build_journey,
)

pytestmark = pytest.mark.smoke

QUESTION = "C. auris in Michigan in 2024"
CLINICAL = "C. auris (clinical cases)"
SCREENING = "C. auris (screening, colonisation)"
TRIALS = {
    "trial-1add4039": "C. auris in Michigan in 2024",
    "trial-29479c90": "measles growth in Michigan",
    "trial-d75afb70": "childhood vaccination in Michigan, Detroit and Wayne County",
    "trial-73d98ce6": "HIV care in Kent, Ingham and Wayne",
}
UNCATALOGUED = {"tracking page": "cdc_c_auris_tracking", "AR Lab Network": "cdc_ar_lab_network"}


@pytest.fixture
def journey(tmp_path: Path) -> Journey:
    return build_journey(tmp_path)


@pytest.fixture(scope="module")
def trials() -> dict[str, ExpectedRecord]:
    """The DEV trial records, read through the scorer's own loader (it checks DEV's frozen sha256)."""
    dev = load_expected(ROOT / "eval" / "dev" / "dev.jsonl", "dev")
    return {r.id: r for r in dev if (r.model_extra or {}).get("tags", {}).get("source") == "trial"}


# ------------------------------------------------------------------------------------------ shared checks


def assert_provenance_on_every_number(journey: Journey, response: str) -> None:
    """Hard rule 8: each source in a table has a complete provenance line, with the sha256 of its raw bytes."""
    sources = {r["source_id"] for r in client.table_rows(response) if r.get("source_id")}
    sources |= {
        r[k] for r in client.table_rows(response) for k in ("case_source", "population_source") if r.get(k)
    }
    assert sources, "a data table must name the source of its rows"
    lines = {m.group(1): m.group(0) for m in PROVENANCE_RE.finditer(response)}
    assert sources <= provenance_ids(response), f"no provenance line for {sources - provenance_ids(response)}"
    for src in sources:
        assert provenance_complete(lines[src]), lines[src]
        assert journey.raw[src].sha256 in lines[src], (
            f"{src}: provenance does not carry the raw bytes' sha256"
        )


def series(response: str, condition: str) -> list[dict[str, str]]:
    return [r for r in client.table_rows(response) if r.get("condition") == condition]


# ------------------------------------------------------------------------------------------ the DEV records


def test_the_trial_questions_are_the_four_dev_trials(trials: dict[str, ExpectedRecord]) -> None:
    assert {k: (r.model_extra or {})["question"] for k, r in trials.items()} == TRIALS
    assert trials["trial-1add4039"].expected_class == "ANSWER"
    assert trials["trial-1add4039"].expected_datasets == (NNDSS,)
    assert trials["trial-29479c90"].expected_datasets == (NNDSS,)
    assert {trials[k].expected_class for k in ("trial-d75afb70", "trial-73d98ce6")} == {"OUT_OF_SCOPE"}


# ------------------------------------------------------------------------------------------ the journey, step by step


def test_step1_suggest_routes_c_auris_to_nndss_only(journey: Journey) -> None:
    out = journey.call("suggest_datasets", question=QUESTION)
    assert client.fitting_sources(out) == [NNDSS]
    assert "places ['Michigan'], years [2024]" in out


def test_step2_describe_names_both_series_their_flags_and_caveats(journey: Journey) -> None:
    out = journey.call("describe_dataset", source_id=NNDSS)
    assert f"- {CLINICAL}: cases" in out and f"- {SCREENING}: cases" in out
    assert "'U' = unavailable" in out and "'-' = no reported cases" in out
    assert "Candida auris clinical cases and screening (colonization) cases are separate series" in out
    assert "no rates" in out  # NNDSS publishes counts only: a rate needs a denominator from elsewhere
    assert "Review status: draft" in out


def test_step3_series_come_back_separate_never_summed_with_totals_left_out(journey: Journey) -> None:
    out = journey.call(
        "get_series", condition="C. auris", locations=["Michigan"], year_start=2024, year_end=2024
    )
    rows = client.table_rows(out)
    assert {r["condition"] for r in rows} == {CLINICAL, SCREENING}
    assert {r["location"] for r in rows} == {"Michigan"}  # US RESIDENTS and TOTAL never appear as a place
    got = {(r["condition"], r["observation_type"]): r["value"] for r in rows}
    assert got == {
        (CLINICAL, "year_end_ytd"): "21",  # the running total at week 52, not weeks 51 + 52 added
        (CLINICAL, "subsequent_year_restatement"): "23",
        (SCREENING, "year_end_ytd"): "57",
        (SCREENING, "subsequent_year_restatement"): "60",
    }
    assert "78" not in {r["value"] for r in rows}  # clinical + screening is never a row
    for r in series(out, CLINICAL) + series(out, SCREENING):
        assert r["source_id"] == NNDSS and r["retrieved_at"].startswith("2026-11-24")
        if r["observation_type"] == "year_end_ytd":
            assert r["period_status"].startswith("provisional")
        else:
            assert (
                "unverified_restatement" in r["period_status"]
                and "not a final disease count" in r["period_status"]
            )
    assert "draft" in out and "'-' is CDC's 'no reported cases'" in out and "read as not reported" in out
    assert_provenance_on_every_number(journey, out)


def test_step4_a_flagged_place_stays_empty_with_its_flag_never_zero(journey: Journey) -> None:
    out = journey.call(
        "get_series", condition="C. auris, clinical", locations=["Ohio"], year_start=2024, year_end=2024
    )
    rows = [r for r in client.table_rows(out) if r["observation_type"] == "year_end_ytd"]
    assert rows, out
    assert all(r["value"] == "" and r["flag"] == "U" for r in rows), rows
    assert "An empty value is never a zero" in out


def test_step5_rates_use_the_census_denominator_and_are_never_summed(journey: Journey) -> None:
    out = journey.call("get_rate", condition="C. auris", locations=["Michigan"], year=2024)
    rows = {r["condition"]: r for r in client.table_rows(out)}
    assert set(rows) == {CLINICAL, SCREENING}
    # Hard rule 2: the 95% interval comes back with every rate. Pinned to the exact Poisson (Garwood) bounds for
    # 21 cases in 1,000,000, so a change of interval method fails here too.
    r = rows[CLINICAL]
    assert (r["cases"], r["population"], r["rate_per_100k"]) == ("21.0", "1000000.0", "2.1")
    assert (r["lower_95"], r["upper_95"]) == ("1.3", "3.21"), r
    assert float(r["lower_95"]) <= float(r["rate_per_100k"]) <= float(r["upper_95"])
    # Screening counts colonisation found by screening, not new cases of disease (the contract's notIncidence),
    # so its count comes back with no rate per 100,000 people (issue #173; it was 5.7).
    s = rows[SCREENING]
    assert (s["cases"], s["rate_per_100k"], s["lower_95"], s["upper_95"]) == ("57.0", "", "", ""), s
    assert "not new cases of disease" in s["flags"]
    for r in rows.values():
        assert (r["case_source"], r["population_source"]) == (NNDSS, CENSUS)
        assert "provisional" in r["flags"]
    assert "Garwood) 95% interval" in out
    assert "7.8" not in out  # the two rates are never added
    assert "crude rate" in out and "not age-standardised" in out
    assert_provenance_on_every_number(journey, out)


def test_step6_caveats_keep_the_sources_apart(journey: Journey) -> None:
    out = journey.call("list_caveats", condition="C. auris")
    assert "Candida auris clinical cases and screening (colonization) cases are separate series" in out
    assert (
        "C. auris clinical cases and screening (colonisation) cases are different series; never add them."
        in out
    )
    assert (
        "tracking page counts cases by specimen collection date" in out
        and "AR Lab Network counts isolates" in out
    )
    assert "Contract is a draft" in out


@pytest.mark.parametrize(
    ("source", "source_id"),
    [
        pytest.param("tracking page", UNCATALOGUED["tracking page"], id="tracking page"),
        pytest.param(
            "AR Lab Network",
            UNCATALOGUED["AR Lab Network"],
            id="AR Lab Network",
            marks=pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="#48: 'AR Lab Network' also offers cdc_nhsn_ar_hai and cdc_respnet by word overlap",
            ),
        ),
    ],
)
def test_step7_an_uncatalogued_c_auris_source_shows_no_number(
    journey: Journey, source: str, source_id: str
) -> None:
    out = journey.call("suggest_datasets", question=f"C. auris in Michigan in 2024 from the {source}")
    assert client.fitting_sources(out) == [NNDSS], out  # only NNDSS reports C. auris; nothing else fits
    assert source_id not in out  # no entry is invented for the uncatalogued source
    out = journey.call("describe_dataset", source_id=source_id)
    assert "Use search_datasets to see what is available" in out
    out = journey.call("get_series", condition="C. auris", locations=["Michigan"], source_id=source_id)
    assert not data_returned(journey.calls[-2:], journey.responses[-2:]), out


def test_step8_provenance_names_the_exact_fetch(journey: Journey) -> None:
    out = journey.call("get_provenance", source_id=NNDSS, year=2024)
    (row,) = client.table_rows(out)
    assert row["url"] == NNDSS_URL
    assert row["content_sha256"] == journey.raw[NNDSS].sha256
    assert row["retrieved_at"].startswith("2026-11-24") and row["contract_version"] == "0.1.0"
    assert row["snapshot_id"]


# ------------------------------------------------------------------------------------------ trial questions, scored


def run_c_auris(journey: Journey) -> list[str]:
    journey.call("suggest_datasets", question=QUESTION)
    journey.call("describe_dataset", source_id=NNDSS)
    a = journey.call(
        "get_series", condition="C. auris", locations=["Michigan"], year_start=2024, year_end=2024
    )
    b = journey.call("get_rate", condition="C. auris", locations=["Michigan"], year=2024)
    journey.call("list_caveats", condition="C. auris")
    return [a, b]


def test_trial_c_auris_michigan_passes_the_eval_rules(
    journey: Journey, trials: dict[str, ExpectedRecord]
) -> None:
    reply = client.answer(run_c_auris(journey))
    scored = score_question(trials["trial-1add4039"], journey.calls, journey.responses, reply)
    assert (scored.a.class_score, scored.a.target_score, scored.a.prov_score) == (2, 2, 2), reply
    assert not scored.a.fabricated and scored.unmatched_numbers == ()
    assert scored.target_flags == () and scored.forbidden_derivations == ()
    assert scored.a.passed and scored.b.passed  # no query_sql needed


@pytest.mark.parametrize(
    ("bad", "token", "why"),
    [
        ("78 C. auris cases in Michigan in 2024.", "78", "series_added"),  # clinical 21 + screening 57
        ("A rate of 7.8 per 100,000 in Michigan in 2024.", "7.8", None),  # rates 2.1 + 5.7: never summed
        # A number no tool returned. (Not 184, the real tracking-page figure: the NNDSS contract quotes it in an
        # open question that list_caveats returns, so the scorer rightly finds it in the transcript.)
        ("The tracking page shows 312 clinical cases in Michigan in 2024.", "312", None),
    ],
    ids=["clinical-plus-screening", "rates-added", "number-from-memory"],
)
def test_trial_c_auris_a_summed_or_invented_number_fails(
    journey: Journey, trials: dict[str, ExpectedRecord], bad: str, token: str, why: str | None
) -> None:
    good = client.answer(run_c_auris(journey))
    fab = check_fabrication(bad + "\n" + good, journey.calls, journey.responses)
    assert fab.unmatched == (token,), fab
    if why:
        assert why in {name for _, name in fab.forbidden}, fab.forbidden
    scored = score_question(trials["trial-1add4039"], journey.calls, journey.responses, bad + "\n" + good)
    assert not scored.a.passed and scored.a.total == 0


def run_measles(journey: Journey) -> str:
    journey.call("suggest_datasets", question="measles growth in Michigan")
    return journey.call("get_series", condition="measles", locations=["Michigan"])


def test_trial_measles_returns_indigenous_and_imported_apart(
    journey: Journey, trials: dict[str, ExpectedRecord]
) -> None:
    out = run_measles(journey)
    rows = [r for r in client.table_rows(out) if r["observation_type"] == "year_end_ytd"]
    got = {(r["condition"], r["year"]): (r["value"], r["flag"]) for r in rows}
    # Indigenous and imported come back as two series, never one summed series.
    assert got == {
        ("Measles, Indigenous", "2023"): ("1", ""),
        ("Measles, Imported", "2023"): ("3", ""),
        ("Measles, Indigenous", "2024"): ("6", ""),
        # Imported 2024 is empty with '-' in every week: it comes back empty with its flag and the codebook's
        # meaning (no reported cases), never as 0 (issue #173).
        ("Measles, Imported", "2024"): ("", "-"),
    }, out
    assert journey.registry.get(NNDSS).contract.flag_column("m3").no_cases_code == "-"  # type: ignore[union-attr]
    (imported_2024,) = [r for r in rows if r["condition"] == "Measles, Imported" and r["year"] == "2024"]
    assert "'-' = no reported cases" in imported_2024["period_status"], imported_2024
    assert "never counted as 0" in imported_2024["period_status"], imported_2024
    assert_provenance_on_every_number(journey, out)
    scored = score_question(trials["trial-29479c90"], journey.calls, journey.responses, client.answer([out]))
    assert scored.a.passed, scored


OUT_OF_SCOPE = [
    (
        "trial-d75afb70",
        "childhood vaccination coverage",
        ["Michigan", "Detroit", "Wayne County"],
        "vaccination",
    ),
    ("trial-73d98ce6", "HIV care", ["Kent", "Ingham", "Wayne"], "HIV"),
]


def run_out_of_scope(journey: Journey, question: str, condition: str, places: list[str]) -> str:
    suggest = journey.call("suggest_datasets", question=question)
    got = journey.call("get_series", condition=condition, locations=places)
    return client.out_of_scope_answer(condition, ", ".join(places), suggest, got)


def test_trial_vaccination_is_declined_with_no_number(
    journey: Journey, trials: dict[str, ExpectedRecord]
) -> None:
    qid, label, places, condition = OUT_OF_SCOPE[0]
    # Scored on the full catalog, adult COVID-19 and RSV coverage included (#256): a bare "vaccination" names no
    # vaccine, so no gated coverage series answers it. #175 re-audits the label itself.
    reply = run_out_of_scope(journey, TRIALS[qid], condition, places)
    assert not data_returned(journey.calls, journey.responses)
    scored = score_question(trials[qid], journey.calls, journey.responses, reply)
    assert scored.a.passed and scored.a.prov_na, (reply, scored)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#37 item 9: suggest_datasets still offers cdc_respnet for 'HIV care' by word overlap",
)
def test_trial_hiv_care_is_declined_with_no_number(
    journey: Journey, trials: dict[str, ExpectedRecord]
) -> None:
    qid, label, places, condition = OUT_OF_SCOPE[1]
    reply = run_out_of_scope(journey, TRIALS[qid], condition, places)
    assert not data_returned(journey.calls, journey.responses)
    scored = score_question(trials[qid], journey.calls, journey.responses, reply)
    assert scored.a.passed and scored.a.prov_na, (reply, scored)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#37 item 3: in csv a decline gives its next step as prose, not as machine-readable fields",
)
@pytest.mark.parametrize(
    ("qid", "label", "places", "condition"), OUT_OF_SCOPE, ids=[q for q, *_ in OUT_OF_SCOPE]
)
def test_trial_out_of_scope_declines_are_structured(
    journey: Journey,
    trials: dict[str, ExpectedRecord],
    qid: str,
    label: str,
    places: list[str],
    condition: str,
) -> None:
    reply = run_out_of_scope(journey, TRIALS[qid], condition, places)
    assert "next_step" in structured_fields(journey.responses[-1])
    assert score_question(trials[qid], journey.calls, journey.responses, reply).class_source == "fields"


def test_discovery_carries_provenance_for_the_fitting_source(journey: Journey) -> None:
    out = journey.call("suggest_datasets", question=QUESTION)
    (row,) = [r for r in client.table_rows(out) if r["source_id"] == NNDSS]
    assert row["raw_sha256"] == journey.raw[NNDSS].sha256, row
    assert row["retrieved_at"].startswith("2026-11-24"), row
    assert row["contract_status"] == "draft"


@pytest.mark.parametrize("source_id", UNCATALOGUED.values())
def test_an_uncatalogued_source_refusal_is_structured(journey: Journey, source_id: str) -> None:
    out = journey.call("describe_dataset", source_id=source_id, fmt="json")
    assert structured_fields(out).get("next_step"), out


def test_the_smoke_journey_cannot_reach_the_network() -> None:
    """The conftest guard: a connection or DNS lookup off this machine fails the test instead of fetching."""
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("192.0.2.1", 443), timeout=1)  # TEST-NET-1: never routed
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("data.cdc.gov", 443)
    with socket.socket() as server:  # loopback stays open, so a local stub server would still work
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=1):
            pass
