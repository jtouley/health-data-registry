"""The release gate passes a build only when every node built and every error-severity test passed, and records a
release as served only after its upload is confirmed with the exact tag export-public created."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import cli
from health_data_registry import release_gate as rg

T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)


def target(
    tmp_path: Path,
    results: list[tuple[str, str, Any]],
    *,
    invocation: str = "inv-1",
    declared: tuple[str, ...] = (),
    manifest_invocation: str | None = None,
) -> Path:
    """A dbt target directory: (unique_id, status, severity or None) per result, as dbt writes them. `declared`
    adds manifest nodes the run has no result for; the manifest is from `manifest_invocation` (default: the same
    run)."""
    t = tmp_path / f"target-{invocation}-{manifest_invocation}"
    t.mkdir()
    run = {
        "metadata": {
            "dbt_version": "1.12.7",
            "generated_at": "2026-11-25T11:59:00Z",
            "invocation_id": invocation,
        },
        "results": [
            {
                "unique_id": uid,
                "status": status,
                "failures": 3 if status in ("fail", "warn") else 0,
                "message": "boom" if status == "error" else None,
            }
            for uid, status, _ in results
        ],  # fmt: skip
    }
    nodes: dict[str, Any] = {}
    unit: dict[str, Any] = {}
    for uid, _, sev in results:
        kind, name = uid.split(".")[0], uid.split(".")[2]
        node = {"name": name, "resource_type": kind}
        if kind == "test":
            node["config"] = {"severity": sev, "meta": {"dimension": "conformity"}}
        (unit if kind == "unit_test" else nodes)[uid] = node
    for uid in declared:
        nodes[uid] = {"name": uid.split(".")[2], "resource_type": uid.split(".")[0]}
    (t / "run_results.json").write_text(json.dumps(run))
    meta = {"invocation_id": manifest_invocation or invocation}
    (t / "manifest.json").write_text(json.dumps({"metadata": meta, "nodes": nodes, "unit_tests": unit}))

    return t


GOOD = [
    ("model.p.stg_a", "success", None),
    ("test.p.codes_a.1", "pass", "ERROR"),
    ("test.p.fill_a.2", "warn", "warn"),
    ("unit_test.p.queue_a.u", "pass", None),
]
# What the toy contracts expect of a build: GOOD's model and its two contract tests (one warn-severity).
TOY = rg.Expected(frozenset({"stg_a"}), {"codes_a": "error", "fill_a": "warn"})


def test_an_empty_run_with_an_empty_manifest_is_not_servable(tmp_path: Path) -> None:
    """Outside review #8: zero failures in a run that ran nothing must never be servable."""
    t = tmp_path / "empty"
    t.mkdir()
    (t / "run_results.json").write_text(json.dumps({"results": []}))
    (t / "manifest.json").write_text(json.dumps({}))
    build = rg.read_build(t)
    assert not build.servable
    assert "dbt's run record lists nothing built or tested" in build.reason()
    assert not rg.read_build(t, expected=TOY).servable


def test_every_test_the_contracts_expect_must_run(tmp_path: Path) -> None:
    assert rg.read_build(target(tmp_path, GOOD), expected=TOY).servable
    missing = rg.read_build(target(tmp_path, GOOD[:2] + GOOD[3:], invocation="no-fill"), expected=TOY)
    # A test with no result was never evaluated (#222): it holds the release, apart from a rule error.
    assert (
        not missing.servable
        and missing.rule_errors == []
        and missing.not_evaluated
        == [
            {
                "test": "fill_a",
                "type": "test",
                "error": "expected by the contracts, no result",
                "reason": "no_result",
            }
        ]
    )
    # The label comes from the structured reason, never the error text, so rewording the text moves nothing.
    reworded = replace(missing, not_evaluated=[{**missing.not_evaluated[0], "error": "x"}])
    assert reworded.reason() == missing.reason()
    assert missing.reason() == ("1 check(s) the contracts expect have no result (fill_a)")
    no_model = rg.read_build(target(tmp_path, GOOD[1:], invocation="no-model"), expected=TOY)
    assert no_model.model_errors == [
        {"model": "stg_a", "type": "model", "error": "expected by the contracts, not built"}
    ]
    # An error-severity rule that came back as a warning (its test's severity was changed) is a failure.
    downgraded = rg.read_build(
        target(tmp_path, [GOOD[0], ("test.p.codes_a.1", "warn", "warn"), *GOOD[2:]], invocation="down"),
        expected=TOY,
    )
    assert [f["test"] for f in downgraded.data_failures] == ["codes_a"] and not downgraded.servable


def test_results_are_matched_by_name_never_by_position(tmp_path: Path) -> None:
    """dbt may list results in any order; the verdict and the build are the same whatever the order."""
    inputs = {"commit": "c1"}
    forward = rg.read_build(target(tmp_path, GOOD, invocation="fw"), expected=TOY, inputs=inputs)
    backward = rg.read_build(target(tmp_path, GOOD[::-1], invocation="bw"), expected=TOY, inputs=inputs)
    assert forward.servable and backward.servable and forward.build_id == backward.build_id
    held = [GOOD[0], ("test.p.codes_a.1", "fail", "error"), *GOOD[2:]]
    a = rg.read_build(target(tmp_path, held, invocation="h1"), expected=TOY)
    b = rg.read_build(target(tmp_path, held[::-1], invocation="h2"), expected=TOY)
    assert a.reason() == b.reason() == "1 error-severity check(s) failed (codes_a)"


def test_not_evaluated_and_metric_errors_block_under_their_own_labels(tmp_path: Path) -> None:
    """A value rule over an all-empty column is not evaluated: never a pass, never a data failure, and it blocks;
    a rule that could not run is a metric error, labelled apart."""
    t = target(tmp_path, [GOOD[0], ("test.p.codes_a.1", "error", "error"), *GOOD[2:]])
    run = json.loads((t / "run_results.json").read_text())
    run["results"][1]["message"] = (
        "Database Error in test codes_a\n  Invalid Input Error: Not evaluated: no row has a value in this column, "
        "so there was nothing to check."
    )
    (t / "run_results.json").write_text(json.dumps(run))
    unjudged = rg.read_build(t, expected=TOY)
    assert not unjudged.servable and not unjudged.data_failures and not unjudged.rule_errors
    assert [n["test"] for n in unjudged.not_evaluated] == ["codes_a"]
    assert unjudged.reason() == "1 check(s) could not be judged, having no values to check (codes_a)"
    broken = rg.read_build(
        target(tmp_path, [GOOD[0], ("test.p.codes_a.1", "error", "error"), *GOOD[2:]], invocation="broken"),
        expected=TOY,
    )
    assert not broken.servable and not broken.not_evaluated and not broken.data_failures
    assert broken.reason() == "1 check(s) could not run"


NOTHING_TO_JUDGE = (
    "Database Error in test {}\n  Invalid Input Error: Not evaluated: no row has a value in this column, so there "
    "was nothing to check."
)


def _not_evaluated(tmp_path: Path, results: list[tuple[str, str, Any]], index: int, invocation: str) -> Path:
    t = target(tmp_path, results, invocation=invocation)
    run = json.loads((t / "run_results.json").read_text())
    run["results"][index]["message"] = NOTHING_TO_JUDGE.format(results[index][0].split(".")[2])
    (t / "run_results.json").write_text(json.dumps(run))
    return t


def test_a_warn_rule_not_evaluated_warns_as_on_the_read_path(tmp_path: Path) -> None:
    """QualityReport.blocking holds a not-evaluated rule only when it is error-severity; the gate agrees, and
    takes the severity from the contracts, so a run that downgraded an error rule's config still holds."""
    warn = rg.read_build(
        _not_evaluated(tmp_path, [*GOOD[:2], ("test.p.fill_a.2", "error", "warn"), GOOD[3]], 2, "w"),
        expected=TOY,
    )
    assert warn.servable and not warn.not_evaluated and [w["test"] for w in warn.warnings] == ["fill_a"]
    downgraded = rg.read_build(
        _not_evaluated(tmp_path, [GOOD[0], ("test.p.codes_a.1", "error", "warn"), *GOOD[2:]], 1, "d"),
        expected=TOY,
    )
    assert not downgraded.servable and [n["test"] for n in downgraded.not_evaluated] == ["codes_a"]


def test_the_run_record_and_manifest_must_be_from_one_run(tmp_path: Path) -> None:

    mixed = rg.read_build(target(tmp_path, GOOD, manifest_invocation="inv-other"))
    assert not mixed.servable
    assert "dbt's run record and its manifest come from different runs" in mixed.reason()


def test_confirm_promotes_only_the_judged_run_and_its_release(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    inputs = {"commit": "c1", "datasets": {"a": {"snapshot_id": "s1"}}}
    build = rg.read_build(target(tmp_path, GOOD), inputs=inputs)
    rg.decide(ops, build, now=T0, run_date="2026-11-25")
    with pytest.raises(ValueError, match="is not a release of run date 2026-11-25"):
        rg.confirm(ops, build, "release-2026-11-24")
    rerun = rg.read_build(target(tmp_path, GOOD, invocation="inv-9"), inputs=inputs)
    assert rerun.build_id == build.build_id  # same inputs, same id; but a different dbt run
    with pytest.raises(ValueError, match="only the run that was judged can be served"):
        rg.confirm(ops, rerun, "release-2026-11-25")
    assert rg.confirm(ops, build, "release-2026-11-25.2", now=T1)["release_ref"] == "release-2026-11-25.2"


def test_the_build_id_is_a_content_hash_of_its_inputs(tmp_path: Path) -> None:
    """Two builds from the same inputs get the same id, whatever dbt's run id or the clock; any input changes it."""
    from health_data_registry.registry import load_registry
    from health_data_registry.store import build_snapshot, write_ndjson
    from tests.conftest import RECORDS, raw_fetch, write_registry

    reg = load_registry(write_registry(tmp_path / "reg"))
    project = reg.root / rg.DBT_PROJECT
    with pytest.raises(rg.GateInputMissing, match="could not be read"):  # no dbt project: nothing was built
        rg.build_inputs(reg, tmp_path, "c1")
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text("name: toy\nvars:\n  near_zero_ytd_max: 0\n")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    lake.mkdir()
    fetch = raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)
    first_snapshot = build_snapshot(lake, reg.get("toy_cases"), [fetch], [page]).snapshot_id
    first = rg.build_inputs(reg, lake, "c1")
    assert first == rg.build_inputs(reg, lake, "c1")
    (ds,) = first["datasets"].values()
    assert ds["raw_sha256"] and ds["contract_version"] == "0.1.0" and len(ds["contract_sha256"]) == 64
    assert "snapshot_id" not in ds and first["dbt_vars"] == {"near_zero_ytd_max": 0}
    # The same bytes fetched again a day later: a new snapshot id (it carries the retrieval time), the same id.
    later = replace(fetch, retrieved_at=fetch.retrieved_at + timedelta(days=1))
    assert build_snapshot(lake, reg.get("toy_cases"), [later], [page]).snapshot_id != first_snapshot
    assert rg.build_inputs(reg, lake, "c1") == first

    one = rg.read_build(target(tmp_path, GOOD, invocation="run-a"), inputs=first)
    two = rg.read_build(target(tmp_path, GOOD, invocation="run-b"), inputs=rg.build_inputs(reg, lake, "c1"))
    assert one.build_id == two.build_id == rg.build_id_of(first)
    assert rg.build_id_of(rg.build_inputs(reg, lake, "c2")) != one.build_id  # another commit
    assert rg.build_id_of(rg.build_inputs(reg, tmp_path / "empty-lake", "c1")) != one.build_id  # no snapshot
    (reg.root / reg.get("toy_cases").entry.contract).write_text(
        (reg.root / reg.get("toy_cases").entry.contract).read_text() + "# edited\n"
    )
    assert rg.build_id_of(rg.build_inputs(reg, lake, "c1")) != one.build_id  # the contract's bytes changed
    project.write_text("name: toy\nvars:\n  near_zero_ytd_max: 1\n")
    assert rg.build_id_of(rg.build_inputs(reg, lake, "c1")) != one.build_id  # a dbt var changed


def test_the_expected_set_comes_from_the_real_contracts() -> None:
    from health_data_registry.registry import load_registry

    expected = rg.expected_from(load_registry(Path(__file__).resolve().parents[1]))
    assert {
        "stg_cdc_nndss_weekly",
        "int_cdc_nndss_weekly",
        "flag_all_dash_areas",
        "mmwr_calendar",
    } <= expected.models
    assert expected.tests["cdc_nndss_weekly_has_rows"] == "error"
    assert expected.tests["cdc_nndss_weekly_cauris_labels_present"] == "warn"


def test_a_clean_build_passes_and_is_served_only_once_confirmed(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    build = rg.read_build(target(tmp_path, GOOD))
    assert build.servable and build.reason() == ""
    assert (build.models_built, build.tests_passed) == (1, 2)
    assert build.warnings == [
        {"test": "fill_a", "type": "test", "severity": "warn", "dimension": "conformity", "failures": 3}
    ]
    assert build.build_id.startswith("build-") and len(build.build_id) == 18
    row = rg.decide(ops, build, now=T0, run_date="2026-11-25")
    assert row == {"at": "2026-11-25T12:00:00+00:00", "action": "pass", "build_id": build.build_id,
                   "serving": None, "warnings": 1, "invocation_id": "inv-1", "run_date": "2026-11-25"}  # fmt: skip
    assert rg.serving(ops) is None  # a pass is not served: nothing has been uploaded yet
    served = rg.confirm(ops, build, "release-2026-11-25.2", now=T1)
    assert served == {"at": "2026-11-25T13:00:00+00:00", "action": "serve", "release_ref": "release-2026-11-25.2",
                      "build_id": build.build_id, "previous": None, "warnings": 1}  # fmt: skip
    assert rg.serving(ops) == {"release_ref": "release-2026-11-25.2", "build_id": build.build_id,
                               "since": "2026-11-25T13:00:00+00:00"}  # fmt: skip
    (logged,) = rg.read_log(ops, rg.BUILDS)
    assert (
        logged["servable"] is True
        and logged["invocation_id"] == "inv-1"
        and logged["dbt_version"] == "1.12.7"
    )
    assert [r["action"] for r in rg.read_log(ops, rg.SERVING_LOG)] == ["pass", "serve"]
    assert not list(ops.glob(".*.tmp"))


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (("test.p.rows_b.3", "fail", "error"), "1 error-severity check(s) failed (rows_b)"),
        (("unit_test.p.queue_b.u", "fail", None), "1 error-severity check(s) failed (queue_b)"),
        (("test.p.rule_c.4", "error", "error"), "1 check(s) could not run"),
        (("unit_test.p.queue_c.u", "runtime error", None), "1 check(s) could not run"),
        (("test.p.rule_d.5", "skipped", "error"), "1 model(s) or check(s) were skipped because something before them failed"),
        (("model.p.flag_d", "skipped", None), "1 model(s) or check(s) were skipped because something before them failed"),
        (("model.p.stg_e", "error", None), "1 model(s) failed to build"),
        (("seed.p.codes_e", "error", None), "1 model(s) failed to build"),
        (("snapshot.p.hist_e", "error", None), "1 model(s) failed to build"),
        (("operation.p.hook_e", "error", None), "1 model(s) failed to build"),
    ],
)  # fmt: skip
def test_a_blocking_build_is_held_and_the_previous_release_stays_served(
    tmp_path: Path, bad: tuple[str, str, Any], reason: str
) -> None:
    ops = tmp_path / "ops"
    first = rg.read_build(target(tmp_path, GOOD))
    rg.decide(ops, first, now=T0, run_date="2026-11-18")
    rg.confirm(ops, first, "release-2026-11-18", now=T0)
    held = rg.read_build(target(tmp_path, [*GOOD, bad], invocation="inv-2"))
    assert not held.servable and held.reason() == reason
    row = rg.decide(ops, held, now=T1)
    assert row["action"] == "hold" and row["kept"] == "release-2026-11-18" and row["reason"] == reason
    assert rg.serving(ops)["release_ref"] == "release-2026-11-18"  # type: ignore[index]
    assert [r["action"] for r in rg.read_log(ops, rg.SERVING_LOG)] == ["pass", "serve", "hold"]
    assert [b["servable"] for b in rg.read_log(ops, rg.BUILDS)] == [True, False]
    with pytest.raises(ValueError, match="did not pass the gate"):
        rg.confirm(ops, held, "release-2026-11-25")


def test_an_empty_or_partial_run_is_held(tmp_path: Path) -> None:
    empty = rg.read_build(target(tmp_path, [], invocation="empty"))
    assert not empty.servable and empty.reason() == "dbt's run record lists nothing built or tested"
    untested = rg.read_build(target(tmp_path, [("model.p.stg_a", "success", None)], invocation="untested"))
    assert not untested.servable and untested.reason() == "no test ran"
    missing = rg.read_build(
        target(tmp_path, GOOD, invocation="missing", declared=("model.p.int_b", "seed.p.s"))
    )
    assert missing.reason() == "2 model(s) failed to build"
    assert missing.model_errors == [
        {"model": "int_b", "type": "model", "error": "not built"},
        {"model": "s", "type": "seed", "error": "not built"},
    ]


def test_reasons_join_and_a_hold_before_any_serve_keeps_nothing(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    t = target(
        tmp_path,
        [("model.p.m", "error", None), ("test.p.x.1", "error", "error"), ("test.p.y.2", "fail", "error")],
    )
    build = rg.read_build(t)
    assert (
        build.reason()
        == "1 model(s) failed to build; 1 check(s) could not run; 1 error-severity check(s) failed (y)"
    )
    assert build.model_errors == [{"model": "m", "type": "model", "error": "boom"}]
    assert build.rule_errors == [{"test": "x", "type": "test", "error": "boom"}]
    assert rg.decide(ops, build, now=T0)["kept"] is None
    assert rg.serving(ops) is None and rg.read_log(tmp_path / "nowhere", rg.BUILDS) == []


def test_confirm_takes_only_a_release_tag_of_a_build_that_passed(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    build = rg.read_build(target(tmp_path, GOOD))
    with pytest.raises(ValueError, match="has no recorded pass"):
        rg.confirm(ops, build, "release-2026-11-25")
    rg.decide(ops, build, now=T0)  # a pass with no run date matches no tag, so it is never served
    with pytest.raises(ValueError, match="passed without a run date"):
        rg.confirm(ops, build, "release-2026-01-01")
    rg.decide(ops, build, now=T0, run_date="2026-11-25")
    for bad in ("release-2026-11-25.1", "release-2026-11-25.x", "v1", "release-26-11-25"):
        with pytest.raises(ValueError, match="is not a release tag"):
            rg.confirm(ops, build, bad)
    assert rg.serving(ops) is None
    rg.confirm(ops, build, "release-2026-11-25.12", now=T1)
    assert rg.serving(ops)["release_ref"] == "release-2026-11-25.12"  # type: ignore[index]


def test_unreadable_ops_state_holds_in_plain_words(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    ops.mkdir()
    good = target(tmp_path, GOOD)
    (ops / rg.SERVING).write_text("{not json")
    with pytest.raises(rg.GateStateUnreadable, match="the release served now is unknown"):
        rg.serving(ops)
    passed, message = rg.run(good, ops, run_date="2026-11-25")
    assert not passed and message.startswith("The release is held:") and "Restore the file" in message
    (ops / rg.SERVING).write_text('["x"]')
    with pytest.raises(rg.GateStateUnreadable, match="does not name a release"):
        rg.serving(ops)
    (ops / rg.SERVING).unlink()
    (ops / rg.SERVING_LOG).write_text('{"action": "pass"}\n{"act')
    with pytest.raises(rg.GateStateUnreadable, match="a line may be cut short"):
        rg.read_log(ops, rg.SERVING_LOG)
    assert rg.run(good, ops)[0] is False
    (ops / rg.SERVING_LOG).write_text("[1]\n")
    with pytest.raises(rg.GateStateUnreadable, match="not a record"):
        rg.read_log(ops, rg.SERVING_LOG)
    assert not (ops / rg.BUILDS).exists()  # nothing is appended to a log the gate couldn't read
    ok, msg = rg.run_confirm(good, ops, "release-2026-11-25")
    assert not ok and msg.startswith("release-2026-11-25 was not recorded as served:")
    # A missing run record still logs its hold when SERVING is unreadable.
    (ops / rg.SERVING_LOG).unlink()
    (ops / rg.SERVING).write_text("{")
    assert rg.run(tmp_path / "none", ops)[0] is False
    assert rg.read_log(ops, rg.SERVING_LOG)[0]["kept"] is None


def test_missing_run_record_holds_and_the_cli_checks_then_confirms(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CLI takes the expected set from the contracts and the build id from the lake; here, the toy run's.
    monkeypatch.setattr(rg, "expected_from", lambda registry: TOY)
    monkeypatch.setattr(rg, "build_inputs", lambda registry, lake, commit: {"commit": commit})
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    ops = tmp_path / "ops"
    passed, message = rg.run(tmp_path / "no-target", ops)
    assert not passed and "dbt's run record could not be read" in message and "the release is held" in message
    (row,) = rg.read_log(ops, rg.SERVING_LOG)
    assert (row["action"], row["build_id"], row["kept"]) == ("hold", None, None)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "run_results.json").write_text("[]")
    with pytest.raises(rg.GateInputMissing, match="not a dbt run record"):
        rg.read_build(bad)
    good = target(tmp_path, GOOD)
    build_id = rg.build_id_of({"commit": "abc123"})
    assert cli.main(["gate", "--target", str(good), "--ops", str(ops), "--run-date", "2026-11-25"]) == 0

    assert (
        f"Build {build_id} passed the gate, with 1 warning(s); it is served once" in capsys.readouterr().out
    )
    assert (
        cli.main(["gate", "--target", str(good), "--ops", str(ops), "--confirm", "release-2026-11-25"]) == 0
    )
    assert f"release-2026-11-25 is served (build {build_id}; before it: none)." in capsys.readouterr().out
    held = target(tmp_path, [*GOOD, ("test.p.z.1", "fail", "error")], invocation="inv-3")
    assert cli.main(["gate", "--target", str(held), "--ops", str(ops)]) == 1
    err = capsys.readouterr().err
    assert "is held: 1 error-severity check(s) failed (z). Still serving release-2026-11-25." in err
    assert (
        cli.main(["gate", "--target", str(held), "--ops", str(ops), "--confirm", "release-2026-11-26"]) == 1
    )
    assert "release-2026-11-26 was not recorded as served: build" in capsys.readouterr().err
    assert rg.serving(ops)["release_ref"] == "release-2026-11-25"  # type: ignore[index]
