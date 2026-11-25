"""The release gate passes a build only when every node built and every error-severity test passed, and records a
release as served only after its upload is confirmed with the exact tag export-public created."""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
) -> Path:
    """A dbt target directory: (unique_id, status, severity or None) per result, as dbt writes them. `declared`
    adds manifest nodes the run has no result for."""
    t = tmp_path / f"target-{invocation}"
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
    (t / "manifest.json").write_text(json.dumps({"nodes": nodes, "unit_tests": unit}))
    return t


GOOD = [
    ("model.p.stg_a", "success", None),
    ("test.p.codes_a.1", "pass", "ERROR"),
    ("test.p.fill_a.2", "warn", "warn"),
    ("unit_test.p.queue_a.u", "pass", None),
]


def test_a_clean_build_passes_and_is_served_only_once_confirmed(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    build = rg.read_build(target(tmp_path, GOOD))
    assert build.servable and build.reason() == ""
    assert (build.models_built, build.tests_passed) == (1, 2)
    assert build.warnings == [
        {"test": "fill_a", "type": "test", "severity": "warn", "dimension": "conformity", "failures": 3}
    ]
    assert build.build_id.startswith("build-") and len(build.build_id) == 18
    row = rg.decide(ops, build, now=T0)
    assert row == {"at": "2026-11-25T12:00:00+00:00", "action": "pass", "build_id": build.build_id,
                   "serving": None, "warnings": 1}  # fmt: skip
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
    rg.decide(ops, first, now=T0)
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
    rg.decide(ops, build, now=T0)
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
    passed, message = rg.run(good, ops)
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
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
    build_id = rg.read_build(good).build_id
    assert cli.main(["gate", "--target", str(good), "--ops", str(ops)]) == 0
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
