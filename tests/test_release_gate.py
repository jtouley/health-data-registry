"""The release gate serves a release only when its build passed every error-severity test, and logs every call."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import cli
from health_data_registry import release_gate as rg

T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def target(tmp_path: Path, results: list[tuple[str, str, Any]], *, invocation: str = "inv-1") -> Path:
    """A dbt target directory: (unique_id, status, severity or None) per node, as dbt writes them."""
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
    nodes = {
        uid: {"name": uid.split(".")[2], "config": {"severity": sev, "meta": {"dimension": "conformity"}}}
        for uid, _, sev in results
        if uid.startswith("test.")
    }
    nodes |= {uid: {"name": uid.split(".")[2]} for uid, _, _ in results if uid.startswith("model.")}
    (t / "run_results.json").write_text(json.dumps(run))
    (t / "manifest.json").write_text(json.dumps({"nodes": nodes}))
    return t


GOOD = [
    ("model.p.stg_a", "success", None),
    ("test.p.codes_a.1", "pass", "ERROR"),
    ("test.p.fill_a.2", "warn", "warn"),
]


def test_a_clean_build_is_served_with_its_warnings_and_logged(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    build = rg.read_build(target(tmp_path, GOOD), "release-2026-11-25")
    assert build.servable and build.reason() == ""
    assert (build.models_built, build.tests_passed) == (1, 1)
    assert build.warnings == [
        {"test": "fill_a", "severity": "warn", "dimension": "conformity", "failures": 3}
    ]
    assert build.build_id.startswith("build-") and len(build.build_id) == 18
    row = rg.decide(ops, build, now=T0)
    assert row == {"at": "2026-11-25T12:00:00+00:00", "action": "serve", "release_ref": "release-2026-11-25",
                   "build_id": build.build_id, "previous": None, "warnings": 1}  # fmt: skip
    assert rg.serving(ops) == {"release_ref": "release-2026-11-25", "build_id": build.build_id,
                               "since": "2026-11-25T12:00:00+00:00"}  # fmt: skip
    (logged,) = rg.read_log(ops, rg.BUILDS)
    assert (
        logged["servable"] is True
        and logged["invocation_id"] == "inv-1"
        and logged["dbt_version"] == "1.12.7"
    )
    assert not list(ops.glob(".*.tmp"))


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (("test.p.rows_b.3", "fail", "error"), "1 error-severity check(s) failed (rows_b)"),
        (("test.p.rule_c.4", "error", "error"), "1 check(s) could not run"),
        (("test.p.rule_d.5", "skipped", "error"), "1 check(s) could not run"),
        (("model.p.stg_e", "error", None), "1 model(s) failed to build"),
    ],
)
def test_a_blocking_build_is_held_and_the_previous_release_stays_served(
    tmp_path: Path, bad: tuple[str, str, Any], reason: str
) -> None:
    ops = tmp_path / "ops"
    first = rg.read_build(target(tmp_path, GOOD), "release-2026-11-18")
    rg.decide(ops, first, now=T0)
    held = rg.read_build(target(tmp_path, [*GOOD, bad], invocation="inv-2"), "release-2026-11-25")
    assert not held.servable and held.reason() == reason
    row = rg.decide(ops, held, now=T0)
    assert row["action"] == "hold" and row["kept"] == "release-2026-11-18" and row["reason"] == reason
    assert rg.serving(ops)["release_ref"] == "release-2026-11-18"  # type: ignore[index]
    assert [r["action"] for r in rg.read_log(ops, rg.SERVING_LOG)] == ["serve", "hold"]
    assert [b["servable"] for b in rg.read_log(ops, rg.BUILDS)] == [True, False]


def test_reasons_join_and_a_hold_before_any_serve_keeps_nothing(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    t = target(
        tmp_path,
        [("model.p.m", "error", None), ("test.p.x.1", "error", "error"), ("test.p.y.2", "fail", "error")],
    )
    build = rg.read_build(t, "r1")
    assert (
        build.reason()
        == "1 model(s) failed to build; 1 check(s) could not run; 1 error-severity check(s) failed (y)"
    )
    assert build.model_errors == [{"model": "m", "error": "boom"}] and build.rule_errors == [
        {"test": "x", "error": "boom"}
    ]
    assert rg.decide(ops, build, now=T0)["kept"] is None
    assert rg.serving(ops) is None and rg.read_log(tmp_path / "nowhere", rg.BUILDS) == []


def test_missing_run_record_holds_and_the_cli_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ops = tmp_path / "ops"
    served, message = rg.run(tmp_path / "no-target", ops, "r1")
    assert not served and "dbt's run record could not be read" in message and "the release is held" in message
    (row,) = rg.read_log(ops, rg.SERVING_LOG)
    assert (row["action"], row["build_id"], row["kept"]) == ("hold", None, None)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "run_results.json").write_text("[]")
    with pytest.raises(rg.GateInputMissing, match="not a dbt run record"):
        rg.read_build(bad, "r1")
    good = target(tmp_path, GOOD)
    assert cli.main(["gate", "r2", "--target", str(good), "--ops", str(ops)]) == 0
    assert "r2 is served (build build-" in capsys.readouterr().out
    held = target(tmp_path, [("test.p.z.1", "fail", "error")], invocation="inv-3")
    assert cli.main(["gate", "r3", "--target", str(held), "--ops", str(ops)]) == 1
    assert "r3 is held: 1 error-severity check(s) failed (z). Still serving r2." in capsys.readouterr().err
    assert rg.run(good, ops, "r4") == (
        True,
        f"r4 is served (build {rg.read_build(good, 'r4').build_id}, with 1 warning(s)).",
    )
