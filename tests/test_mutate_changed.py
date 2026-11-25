"""scripts/mutate_changed.py: the --list output for the CI matrix and the aggregate that applies the floor."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
A, B = "health_data_registry.a", "health_data_registry.b"


@pytest.fixture
def mc(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The script, loaded fresh; its sys.modules entry (dataclasses look the module up by name) is undone."""
    spec = importlib.util.spec_from_file_location("mutate_changed", ROOT / "scripts" / "mutate_changed.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "mutate_changed", mod)
    spec.loader.exec_module(mod)
    return mod


def Shard(*args: Any, **kwargs: Any) -> Any:  # noqa: N802 - reads like the class it builds
    return sys.modules["mutate_changed"].ShardResult(*args, **kwargs)


# --list ----------------------------------------------------------------------------------------------------


def test_list_json_is_one_sorted_line_without_init_files(mc: ModuleType) -> None:
    out = mc.list_json(
        [
            "health_data_registry.tools",
            "health_data_registry.__init__",
            "health_data_registry.adapters.socrata",
            "health_data_registry.adapters.__init__",
            "health_data_registry.tools",
        ]
    )
    assert out == '["health_data_registry.adapters.socrata","health_data_registry.tools"]'
    assert "\n" not in out


def test_list_json_of_nothing_is_an_empty_array(mc: ModuleType) -> None:
    assert mc.list_json([]) == "[]"
    assert mc.list_json(["health_data_registry.__init__"]) == "[]"


@pytest.mark.parametrize(
    "name",
    [
        "health_data_registry.tools;rm -rf",
        "health_data_registry.$(id)",
        "health_data_registry.Tools",
        "other_pkg.tools",
        "health_data_registry",
        "health_data_registry..tools",
        "health_data_registry.tools ",
    ],
)
def test_a_name_that_is_not_a_plain_module_is_refused(mc: ModuleType, name: str) -> None:
    with pytest.raises(ValueError, match="not a plain module name"):
        mc.list_json([name])


def test_list_all_prints_every_module_as_json(mc: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert mc.main(["--list", "--all"]) == 0
    mods = json.loads(capsys.readouterr().out)
    assert "health_data_registry.tools" in mods
    assert "health_data_registry.adapters.socrata" in mods
    assert mods == sorted(mods)
    assert not [m for m in mods if m.endswith("__init__")]


def test_module_mode_needs_a_report_path(mc: ModuleType) -> None:
    with pytest.raises(SystemExit):
        mc.main(["--module", "health_data_registry.tools"])


def test_module_mode_refuses_an_init_file(mc: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        mc.main(["--module", "health_data_registry.__init__", "--report", str(tmp_path / "r.json")])


# mutmut results parsing ------------------------------------------------------------------------------------


def test_parse_results_counts_only_the_named_modules(mc: ModuleType) -> None:
    text = "\n".join(
        [
            "health_data_registry.tools.x__mutmut_1: killed",
            "health_data_registry.tools.x__mutmut_2: survived",
            "health_data_registry.tools.x__mutmut_3: timeout",
            "health_data_registry.tools.x__mutmut_4: suspicious",
            "health_data_registry.tools.x__mutmut_5: no tests",
            "health_data_registry.toolsx.y__mutmut_1: killed",
            "health_data_registry.query.z__mutmut_1: killed",
        ]
    )
    r = mc.parse_results(text, ["health_data_registry.tools"])
    assert (r.killed, r.survived, r.incomplete) == (1, 4, 0)
    assert r.survivors[0] == "health_data_registry.tools.x__mutmut_2: survived"


def test_mutants_no_test_reaches_lower_the_score(mc: ModuleType) -> None:
    # 10 killed and 90 that no test runs: 10%, never 100%.
    text = "\n".join(
        [f"health_data_registry.a.f__mutmut_{i}: killed" for i in range(10)]
        + [f"health_data_registry.a.f__mutmut_{i}: no tests" for i in range(10, 100)]
    )
    r = mc.parse_results(text, [A])
    assert (r.killed, r.checked) == (10, 100)
    ok, lines = mc.aggregate([r], 70.0, [A])
    assert not ok
    assert "10.0% (10 killed of 100)" in lines[-1]


@pytest.mark.parametrize("status", ["skipped", "timeout", "suspicious", "a status mutmut adds later"])
def test_every_status_but_a_kill_counts_against_the_score(mc: ModuleType, status: str) -> None:
    r = mc.parse_results(f"health_data_registry.a.f__mutmut_1: {status}", [A])
    assert (r.killed, r.survived, r.incomplete) == (0, 1, 0)


def test_a_mutant_the_type_checker_caught_counts_as_killed(mc: ModuleType) -> None:
    r = mc.parse_results("health_data_registry.a.f__mutmut_1: caught by type check", [A])
    assert (r.killed, r.survived) == (1, 0)


@pytest.mark.parametrize("status", ["not checked", "check was interrupted by user", "segfault"])
def test_a_run_that_did_not_finish_fails_its_shard(mc: ModuleType, status: str) -> None:
    text = "\n".join(
        [f"health_data_registry.a.f__mutmut_{i}: killed" for i in range(99)]
        + [f"health_data_registry.a.f__mutmut_99: {status}"]
    )
    r = mc.parse_results(text, [A])
    assert r.incomplete == 1
    assert mc.shard_failure(r)
    ok, lines = mc.aggregate([r], 70.0, [A])
    assert not ok
    assert any(line.startswith("FAILED") and "did not finish" in line for line in lines)


# aggregation -----------------------------------------------------------------------------------------------


def test_the_floor_applies_to_the_total_not_each_shard(mc: ModuleType) -> None:
    # a: 10 of 20 (50%), b: 60 of 80 (75%); total 70 of 100 = 70%, which meets a 70% floor.
    shards = [Shard([A], killed=10, survived=10), Shard([B], killed=60, survived=20)]
    ok, lines = mc.aggregate(shards, 70.0, [A, B])
    assert ok
    assert "70.0% (70 killed of 100)" in lines[-1]


def test_just_under_the_floor_fails(mc: ModuleType) -> None:
    shards = [Shard([A], killed=69, survived=31)]
    ok, lines = mc.aggregate(shards, 70.0, [A])
    assert not ok
    assert "69.0% (69 killed of 100)" in lines[-1]


def test_the_score_is_not_an_average_of_shard_scores(mc: ModuleType) -> None:
    # Average of shard scores would be (100% + 50%) / 2 = 75%; the weighted total is 11 of 20 = 55%.
    shards = [Shard([A], killed=1, survived=0), Shard([B], killed=10, survived=9)]
    ok, lines = mc.aggregate(shards, 70.0, [A, B])
    assert not ok
    assert "55.0% (11 killed of 20)" in lines[-1]


def test_a_failed_baseline_fails_even_above_the_floor(mc: ModuleType) -> None:
    shards = [Shard([A], killed=100, survived=0), Shard([B], baseline_ok=False)]
    ok, lines = mc.aggregate(shards, 70.0, [A, B])
    assert not ok
    assert any(B in line and "baseline" in line for line in lines)


def test_a_clean_shard_with_nothing_to_mutate_passes(mc: ModuleType) -> None:
    # B is constants only: its baseline ran clean and it has no mutant. The score is A's alone.
    shards = [Shard([A], killed=80, survived=20), Shard([B])]
    ok, lines = mc.aggregate(shards, 70.0, [A, B])
    assert ok
    assert not any(line.startswith("FAILED") for line in lines)
    assert "80.0% (80 killed of 100)" in lines[-1]


def test_only_modules_with_nothing_to_mutate_pass(mc: ModuleType) -> None:
    ok, lines = mc.aggregate([Shard([A]), Shard([B])], 70.0, [A, B])
    assert ok
    assert "Nothing to mutate" in lines[-1]


def test_aggregate_prints_one_line_per_mutant_not_killed(mc: ModuleType) -> None:
    survivors = ["a.f__mutmut_2: survived", "a.f__mutmut_3: no tests"]
    _, lines = mc.aggregate([Shard([A], killed=1, survived=2, survivors=survivors)], 70.0, [A])
    assert "not killed: a.f__mutmut_2: survived" in lines
    assert "not killed: a.f__mutmut_3: no tests" in lines


def test_a_missing_shard_report_fails(mc: ModuleType) -> None:
    ok, lines = mc.aggregate([Shard([A], killed=100, survived=0)], 70.0, [A, B])
    assert not ok
    assert any(B in line and "no shard report" in line for line in lines)


def test_no_mutant_checked_anywhere_fails(mc: ModuleType) -> None:
    ok, lines = mc.aggregate([Shard([A], baseline_ok=False)], 70.0, [A])
    assert not ok
    assert "a shard failed" in lines[-1]


def test_no_changed_module_passes(mc: ModuleType) -> None:
    ok, lines = mc.aggregate([], 70.0, [])
    assert ok
    assert lines == ["No changed modules to mutate."]


def test_expected_modules_with_no_reports_fail(mc: ModuleType) -> None:
    ok, _ = mc.aggregate([], 70.0, [A])
    assert not ok


# reports on disk -------------------------------------------------------------------------------------------


def _write(d: Path, shard: Any) -> None:
    (d / f"{shard.modules[0]}.json").write_text(json.dumps(asdict(shard)), encoding="utf-8")


def test_aggregate_mode_reads_the_reports(
    mc: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, Shard([A], killed=8, survived=2, survivors=["x"]))
    _write(tmp_path, Shard([B], killed=6, survived=4))
    expect = json.dumps([A, B])
    assert mc.main(["--aggregate", str(tmp_path), "--expect", expect, "--floor", "70"]) == 0
    assert "70.0% (14 killed of 20)" in capsys.readouterr().out
    assert mc.main(["--aggregate", str(tmp_path), "--expect", expect, "--floor", "71"]) == 1


def test_aggregate_mode_fails_on_a_missing_report(mc: ModuleType, tmp_path: Path) -> None:
    _write(tmp_path, Shard([A], killed=10, survived=0))
    assert mc.main(["--aggregate", str(tmp_path), "--expect", json.dumps([A, B])]) == 1


def test_aggregate_mode_with_nothing_expected_passes(mc: ModuleType, tmp_path: Path) -> None:
    assert mc.main(["--aggregate", str(tmp_path), "--expect", "[]"]) == 0


def test_aggregate_mode_needs_expect(mc: ModuleType, tmp_path: Path) -> None:
    _write(tmp_path, Shard([A], killed=10, survived=0))
    with pytest.raises(SystemExit) as exc:
        mc.main(["--aggregate", str(tmp_path)])
    assert exc.value.code == 2


def test_module_mode_with_nothing_to_mutate_passes(
    mc: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mc, "run_mutmut", lambda mods: mc.ShardResult(modules=mods))
    report = tmp_path / "r.json"
    assert mc.main(["--module", "health_data_registry.tools", "--report", str(report)]) == 0
    assert "nothing to mutate" in capsys.readouterr().out
    assert json.loads(report.read_text(encoding="utf-8"))["killed"] == 0


def test_module_mode_fails_a_failed_baseline(
    mc: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mc, "run_mutmut", lambda mods: mc.ShardResult(modules=mods, baseline_ok=False))
    assert mc.main(["--module", "health_data_registry.tools", "--report", str(tmp_path / "r.json")]) == 1


def test_local_mode_applies_the_same_rule(mc: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # A changed module with nothing to mutate passes locally, as its shard does in CI.
    monkeypatch.setattr(mc, "changed_modules", lambda base: ["health_data_registry.tools"])
    monkeypatch.setattr(mc, "run_mutmut", lambda mods: mc.ShardResult(modules=mods))
    assert mc.main([]) == 0
    monkeypatch.setattr(mc, "run_mutmut", lambda mods: mc.ShardResult(modules=mods, baseline_ok=False))
    assert mc.main([]) == 1


def test_a_report_must_say_its_baseline_passed(mc: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text(
        json.dumps({"modules": [A], "killed": 10, "survived": 0, "baseline_ok": "yes", "incomplete": 0}),
        encoding="utf-8",
    )
    [shard] = mc.load_reports(tmp_path)
    assert shard.baseline_ok is False


def test_an_unreadable_report_is_an_error_not_a_zero(mc: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('{"modules": ["x"]}', encoding="utf-8")
    with pytest.raises(KeyError):
        mc.load_reports(tmp_path)


# The eval scorer is mutated too ----------------------------------------------------------------------------


def test_an_eval_module_is_listed_and_accepted(mc: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert mc.list_json(["eval.scorer", "eval.__init__", "health_data_registry.tools"]) == (
        '["eval.scorer","health_data_registry.tools"]'
    )
    assert mc._module("eval/numbers.py") == "eval.numbers"
    assert mc._module("src/health_data_registry/tools.py") == "health_data_registry.tools"
    assert mc.main(["--list", "--all"]) == 0
    mods = json.loads(capsys.readouterr().out)
    assert {"eval.scorer", "eval.numbers", "eval.report", "eval.stats"} <= set(mods)
    r = mc.parse_results(
        "eval.scorer.x_f__mutmut_1: killed\neval.scorerx.y__mutmut_1: killed", ["eval.scorer"]
    )
    assert (r.killed, r.survived) == (1, 0)


@pytest.mark.parametrize("name", ["eval", "eval.Scorer", "eval.scorer;id", "evaluation.scorer"])
def test_an_eval_name_that_is_not_a_plain_module_is_refused(mc: ModuleType, name: str) -> None:
    with pytest.raises(ValueError, match="not a plain module name"):
        mc.list_json([name])
