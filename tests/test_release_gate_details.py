"""The release gate's records to the letter: ids, log rows, kinds, times and the words of each hold.

test_release_gate.py shows the gate's decisions; these pin the details the nightly mutation run (hard rule 13;
issue #213) showed nothing else would notice: a build id computed another way, a log row missing a key, a hold
that forgets what is served, a check that skips the contracts' expected tests.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from health_data_registry import release_gate as rg
from tests.test_release_gate import GOOD, T0, T1, TOY, target

ROOT = Path(__file__).resolve().parents[1]


def test_the_build_id_is_the_sha256_of_the_sorted_compact_inputs() -> None:
    inputs = {"datasets": {"b": [2, 1]}, "commit": "c1"}
    canonical = '{"commit":"c1","datasets":{"b":[2,1]}}'
    assert rg.build_id_of(inputs) == "build-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]


def test_build_inputs_name_every_model_contract_by_its_file_hash(tmp_path: Path) -> None:
    from health_data_registry.registry import load_registry
    from health_data_registry.transform_gen import model_contracts

    reg = load_registry(ROOT)
    got = rg.build_inputs(reg, tmp_path / "empty-lake", "c1")
    assert set(got) == {"commit", "datasets", "model_contracts", "dbt_vars"} and got["commit"] == "c1"
    want = {
        c.id: hashlib.sha256((reg.root / p).read_bytes()).hexdigest() for p, c in model_contracts(reg.root)
    }
    assert want and got["model_contracts"] == want


def test_the_kind_of_a_node_is_its_resource_type_else_the_first_part_of_its_id() -> None:
    assert rg._kind("model.p.stg_a", {"resource_type": "seed"}) == "seed"
    assert rg._kind("unit_test.p.queue_a.u", {}) == "unit_test"
    assert rg._kind("test.p.codes_a.1", {"name": "codes_a"}) == "test"


def test_times_are_recorded_in_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        noon_in_berlin = datetime(2026, 11, 25, 13, 0, tzinfo=timezone(timedelta(hours=1)))
        assert rg._now(noon_in_berlin) == "2026-11-25T12:00:00+00:00"
        assert rg._now(None).endswith("+00:00")
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_log_rows_are_appended_with_sorted_keys_under_new_folders(tmp_path: Path) -> None:
    log = tmp_path / "ops" / "deep" / "er" / "log.jsonl"
    rg._append(log, {"b": 1, "a": 2})
    rg._append(log, {"c": 3})
    assert log.read_text() == '{"a": 2, "b": 1}\n{"c": 3}\n'


def test_the_expected_models_are_only_the_generated_sql_models() -> None:
    from health_data_registry.registry import load_registry

    models = rg.expected_from(load_registry(ROOT)).models
    assert models
    sql = {p.stem for p in (ROOT / "transform" / "models").rglob("*.sql")}
    assert models <= sql


def test_unreadable_state_names_the_kind_of_failure(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / rg.SERVING).write_text("{not json")
    with pytest.raises(rg.GateStateUnreadable) as e:
        rg.serving(ops)
    assert "(JSONDecodeError)" in str(e.value)
    (ops / rg.SERVING_LOG).write_text('{"act')
    with pytest.raises(rg.GateStateUnreadable) as e:
        rg.read_log(ops, rg.SERVING_LOG)
    assert "(JSONDecodeError)" in str(e.value)


# ------------------------------------------------------------------------------------------ decide and confirm


def test_decide_records_when_and_holds_name_their_build(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    good = rg.read_build(target(tmp_path, GOOD))
    rg.decide(ops, good, now=T0)
    (built,) = rg.read_log(ops, rg.BUILDS)
    assert built["recorded_at"] == "2026-11-25T12:00:00+00:00" and built["servable"] is True
    bad = rg.read_build(target(tmp_path, [("model.p.m", "error", None)], invocation="inv-2"))
    row = rg.decide(ops, bad, now=T1)
    assert row == {"at": "2026-11-25T13:00:00+00:00", "action": "hold", "build_id": bad.build_id, "kept": None,
                   "reason": bad.reason()}  # fmt: skip
    assert rg.read_log(ops, rg.SERVING_LOG)[-1] == row


def test_confirm_finds_the_pass_of_this_build_only(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    mine = rg.read_build(target(tmp_path, GOOD), inputs={"commit": "mine"})
    other = rg.read_build(target(tmp_path, GOOD, invocation="inv-2"), inputs={"commit": "other"})
    rg.decide(ops, other, now=T0, run_date="2026-11-25")  # another build's pass
    with pytest.raises(ValueError, match="has no recorded pass"):
        rg.confirm(ops, mine, "release-2026-11-25")


def test_confirm_names_both_dbt_runs_and_the_flag_to_add(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    build = rg.read_build(target(tmp_path, GOOD), inputs={"commit": "c1"})
    rg.decide(ops, build, now=T0)
    with pytest.raises(ValueError) as e:
        rg.confirm(ops, build, "release-2026-11-25")
    assert str(e.value) == (
        f"build {build.build_id} passed without a run date, so no release tag can be matched to it; run the gate "
        "check again with --run-date"
    )
    rg.decide(ops, build, now=T0, run_date="2026-11-25")
    rerun = rg.read_build(target(tmp_path, GOOD, invocation="inv-9"), inputs={"commit": "c1"})
    with pytest.raises(ValueError) as e:
        rg.confirm(ops, rerun, "release-2026-11-25")
    assert str(e.value) == (
        f"build {build.build_id} passed in dbt run inv-1, but this run record is from inv-9; only the run that was "
        "judged can be served"
    )


def test_each_serve_records_the_release_it_replaced(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    build = rg.read_build(target(tmp_path, GOOD))
    rg.decide(ops, build, now=T0, run_date="2026-11-25")
    assert rg.confirm(ops, build, "release-2026-11-25", now=T0)["previous"] is None
    assert rg.confirm(ops, build, "release-2026-11-25.2", now=T1)["previous"] == "release-2026-11-25"


# ------------------------------------------------------------------------------------------ the CLI's check


def test_a_hold_without_a_build_keeps_the_served_release_and_says_why(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    build = rg.read_build(target(tmp_path, GOOD))
    rg.decide(ops, build, now=T0, run_date="2026-11-25")
    rg.confirm(ops, build, "release-2026-11-25", now=T0)
    passed, message = rg.run(tmp_path / "no-target", ops)
    row = rg.read_log(ops, rg.SERVING_LOG)[-1]
    assert not passed
    assert set(row) == {"at", "action", "build_id", "kept", "reason"}
    assert (row["action"], row["build_id"], row["kept"], row["reason"]) == (
        "hold", None, "release-2026-11-25", message,
    )  # fmt: skip
    assert row["at"].endswith("+00:00")


def test_the_check_uses_the_contracts_expected_tests(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    partial = target(tmp_path, [r for r in GOOD if r[0] != "test.p.codes_a.1"])
    passed, message = rg.run(partial, ops, expected=TOY, inputs={"commit": "c1"})
    assert not passed and message.startswith("Build ") and "is held:" in message
    assert message.endswith("Still serving no release (none was served before).")


def test_a_clean_pass_without_warnings_says_so_plainly(tmp_path: Path) -> None:
    clean = [r for r in GOOD if r[1] != "warn"]
    passed, message = rg.run(target(tmp_path, clean), tmp_path / "ops", inputs={"commit": "c1"})
    build_id = rg.build_id_of({"commit": "c1"})
    assert (
        passed and message == f"Build {build_id} passed the gate; it is served once its release is uploaded."
    )


def test_confirm_checks_the_contracts_expected_tests_again(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    inputs = {"commit": "c1"}
    assert rg.run(target(tmp_path, GOOD), ops, expected=TOY, inputs=inputs, run_date="2026-11-25")[0]
    partial = target(tmp_path, [r for r in GOOD if r[0] != "test.p.codes_a.1"], manifest_invocation="inv-1")
    ok, message = rg.run_confirm(partial, ops, "release-2026-11-25", expected=TOY, inputs=inputs)
    assert not ok and "did not pass the gate" in message
    assert rg.serving(ops) is None


def test_log_lines_are_plain_json(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    rg.decide(ops, rg.read_build(target(tmp_path, GOOD)), now=T0)
    for line in (ops / rg.SERVING_LOG).read_text().splitlines():
        assert isinstance(json.loads(line), dict)
    assert datetime.fromisoformat(rg.read_log(ops, rg.SERVING_LOG)[0]["at"]).tzinfo == UTC
