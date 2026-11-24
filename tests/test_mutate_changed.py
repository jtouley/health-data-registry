"""scripts/mutate_changed.py: the --list output for the CI matrix and the aggregate that applies the floor."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mutate_changed", ROOT / "scripts" / "mutate_changed.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mutate_changed"] = mod  # dataclasses look the module up by name
    spec.loader.exec_module(mod)
    return mod


mc = _load()
Shard = mc.ShardResult


# --list ----------------------------------------------------------------------------------------------------


def test_list_json_is_one_sorted_line_without_init_files() -> None:
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


def test_list_json_of_nothing_is_an_empty_array() -> None:
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
def test_a_name_that_is_not_a_plain_module_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="not a plain module name"):
        mc.list_json([name])


def test_list_all_prints_every_module_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert mc.main(["--list", "--all"]) == 0
    mods = json.loads(capsys.readouterr().out)
    assert "health_data_registry.tools" in mods
    assert "health_data_registry.adapters.socrata" in mods
    assert mods == sorted(mods)
    assert not [m for m in mods if m.endswith("__init__")]


def test_module_mode_needs_a_report_path() -> None:
    with pytest.raises(SystemExit):
        mc.main(["--module", "health_data_registry.tools"])


def test_module_mode_refuses_an_init_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        mc.main(["--module", "health_data_registry.__init__", "--report", str(tmp_path / "r.json")])


# mutmut results parsing ------------------------------------------------------------------------------------


def test_parse_results_counts_only_the_named_modules() -> None:
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
    killed, survived, survivors = mc.parse_results(text, ["health_data_registry.tools"])
    assert (killed, survived) == (1, 3)
    assert survivors[0] == "health_data_registry.tools.x__mutmut_2"


# aggregation -----------------------------------------------------------------------------------------------

A, B = "health_data_registry.a", "health_data_registry.b"


def test_the_floor_applies_to_the_total_not_each_shard() -> None:
    # a: 10 of 20 (50%), b: 60 of 80 (75%); total 70 of 100 = 70%, which meets a 70% floor.
    shards = [Shard([A], killed=10, survived=10), Shard([B], killed=60, survived=20)]
    ok, lines = mc.aggregate(shards, 70.0, [A, B])
    assert ok
    assert "70.0% (70 killed of 100)" in lines[-1]


def test_just_under_the_floor_fails() -> None:
    shards = [Shard([A], killed=69, survived=31)]
    ok, lines = mc.aggregate(shards, 70.0, [A])
    assert not ok
    assert "69.0% (69 killed of 100)" in lines[-1]


def test_the_score_is_not_an_average_of_shard_scores() -> None:
    # Average of shard scores would be (100% + 50%) / 2 = 75%; the weighted total is 11 of 20 = 55%.
    shards = [Shard([A], killed=1, survived=0), Shard([B], killed=10, survived=9)]
    ok, lines = mc.aggregate(shards, 70.0, [A, B])
    assert not ok
    assert "55.0% (11 killed of 20)" in lines[-1]


def test_a_failed_baseline_fails_even_above_the_floor() -> None:
    shards = [Shard([A], killed=100, survived=0), Shard([B], baseline_ok=False)]
    ok, lines = mc.aggregate(shards, 70.0, [A, B])
    assert not ok
    assert any(B in line and "baseline" in line for line in lines)


def test_a_shard_that_checked_nothing_fails_even_above_the_floor() -> None:
    shards = [Shard([A], killed=100, survived=0), Shard([B])]
    ok, lines = mc.aggregate(shards, 70.0, [A, B])
    assert not ok
    assert any(B in line and "no mutant was checked" in line for line in lines)


def test_a_missing_shard_report_fails() -> None:
    ok, lines = mc.aggregate([Shard([A], killed=100, survived=0)], 70.0, [A, B])
    assert not ok
    assert any(B in line and "no shard report" in line for line in lines)


def test_no_mutant_checked_anywhere_fails() -> None:
    ok, lines = mc.aggregate([Shard([A], baseline_ok=False)], 70.0, [A])
    assert not ok
    assert "no mutation score" in lines[-1]


def test_no_changed_module_passes() -> None:
    ok, lines = mc.aggregate([], 70.0, [])
    assert ok
    assert lines == ["No changed modules to mutate."]


def test_expected_modules_with_no_reports_fail() -> None:
    ok, _ = mc.aggregate([], 70.0, [A])
    assert not ok


# reports on disk -------------------------------------------------------------------------------------------


def _write(d: Path, shard: Shard) -> None:
    (d / f"{shard.modules[0]}.json").write_text(json.dumps(asdict(shard)), encoding="utf-8")


def test_aggregate_mode_reads_the_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, Shard([A], killed=8, survived=2, survivors=["x"]))
    _write(tmp_path, Shard([B], killed=6, survived=4))
    expect = json.dumps([A, B])
    assert mc.main(["--aggregate", str(tmp_path), "--expect", expect, "--floor", "70"]) == 0
    assert "70.0% (14 killed of 20)" in capsys.readouterr().out
    assert mc.main(["--aggregate", str(tmp_path), "--expect", expect, "--floor", "71"]) == 1


def test_aggregate_mode_fails_on_a_missing_report(tmp_path: Path) -> None:
    _write(tmp_path, Shard([A], killed=10, survived=0))
    assert mc.main(["--aggregate", str(tmp_path), "--expect", json.dumps([A, B])]) == 1


def test_aggregate_mode_with_nothing_expected_passes(tmp_path: Path) -> None:
    assert mc.main(["--aggregate", str(tmp_path), "--expect", "[]"]) == 0


def test_a_report_must_say_its_baseline_passed(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text(
        json.dumps({"modules": [A], "killed": 10, "survived": 0, "baseline_ok": "yes"}), encoding="utf-8"
    )
    [shard] = mc.load_reports(tmp_path)
    assert shard.baseline_ok is False


def test_an_unreadable_report_is_an_error_not_a_zero(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('{"modules": ["x"]}', encoding="utf-8")
    with pytest.raises(KeyError):
        mc.load_reports(tmp_path)
