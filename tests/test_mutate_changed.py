"""scripts/mutate_changed.py: the --list output for the CI matrix and the aggregate that applies the floor."""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
A, B = "health_data_registry.a", "health_data_registry.b"

# Under mutmut (which sets MUTANT_UNDER_TEST before its stats run) the tests run in mutants/, whose package
# sources carry mutmut's trampolines, so splitting them gives a different split from the real one. This script is
# not mutated (scripts/ is outside [tool.mutmut] source_paths), so the tests that split real modules skip there
# and no mutant goes unnoticed.
reads_real_sources = pytest.mark.skipif(
    "MUTANT_UNDER_TEST" in os.environ,
    reason="mutants/ holds mutmut's trampolined copy of the package sources",
)


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


@reads_real_sources
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
    assert "\n" not in out
    ids = json.loads(out)
    # socrata fits one shard, named after the module; tools.py is big enough to split into shards k of n.
    assert ids[0] == "health_data_registry.adapters.socrata"
    tools = ids[1:]
    assert len(tools) >= 2
    h = mc.split_hash("health_data_registry.tools", mc.module_shards("health_data_registry.tools"))
    assert tools == [f"health_data_registry.tools#{k}of{len(tools)}@{h}" for k in range(1, len(tools) + 1)]


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


@reads_real_sources
def test_list_all_prints_every_module_as_json(mc: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert mc.main(["--list", "--all"]) == 0
    mods = json.loads(capsys.readouterr().out)
    modules = [mc.parse_shard_id(m)[0] for m in mods]
    assert "health_data_registry.tools" in modules
    assert "health_data_registry.adapters.socrata" in mods
    assert modules == sorted(modules)
    assert not [m for m in modules if m.endswith("__init__")]
    assert mc.split_gaps(mods) == []


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
        json.dumps(
            {
                "modules": [A],
                "killed": 10,
                "survived": 0,
                "baseline_ok": "yes",
                "incomplete": 0,
                "unassigned": [],
            }
        ),
        encoding="utf-8",
    )
    [shard] = mc.load_reports(tmp_path)
    assert shard.baseline_ok is False


def test_an_unreadable_report_is_an_error_not_a_zero(mc: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('{"modules": ["x"]}', encoding="utf-8")
    with pytest.raises(KeyError):
        mc.load_reports(tmp_path)


# The eval scorer is mutated too ----------------------------------------------------------------------------


@reads_real_sources
def test_an_eval_module_is_listed_and_accepted(mc: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    ids = json.loads(mc.list_json(["eval.stats", "eval.__init__", "health_data_registry.geo"]))
    assert ids == ["eval.stats", "health_data_registry.geo"]
    assert mc._module("eval/numbers.py") == "eval.numbers"
    assert mc._module("src/health_data_registry/tools.py") == "health_data_registry.tools"
    assert mc.main(["--list", "--all"]) == 0
    mods = {mc.parse_shard_id(s)[0] for s in json.loads(capsys.readouterr().out)}
    assert {"eval.scorer", "eval.numbers", "eval.report", "eval.stats"} <= mods
    r = mc.parse_results(
        "eval.scorer.x_f__mutmut_1: killed\neval.scorerx.y__mutmut_1: killed", ["eval.scorer"]
    )
    assert (r.killed, r.survived) == (1, 0)


@pytest.mark.parametrize("name", ["eval", "eval.Scorer", "eval.scorer;id", "evaluation.scorer"])
def test_an_eval_name_that_is_not_a_plain_module_is_refused(mc: ModuleType, name: str) -> None:
    with pytest.raises(ValueError, match="not a plain module name"):
        mc.list_json([name])


# Splitting a big module into shards ------------------------------------------------------------------------

SEP = "\u01c1"


def _source(n_funcs: int, size: int) -> str:
    """A synthetic module: n_funcs top-level functions and one class, each with about `size` mutants, and one
    decorated function mutmut leaves alone."""
    body = "\n".join(f"    x = {i}" for i in range(size))
    funcs = [f"def f{i}():\n{body}\n    return x\n" for i in range(n_funcs)]
    method_body = "\n".join(f"        x = {i}" for i in range(size))
    cls = f"class C:\n    def m(self):\n{method_body}\n        return x\n"
    deco = "@decorator\ndef skipped():\n    return 1\n"
    return "\n\n".join([*funcs, cls, deco])


def _names(module: str, source: str, mc: ModuleType, per_function: int) -> list[str]:
    """Mutant names as mutmut makes them: <module>.<key>__mutmut_<n>, n from 1."""
    return [
        f"{module}.{key}__mutmut_{n}"
        for key, _ in mc.mutated_functions(source)
        for n in range(1, per_function + 1)
    ]


def _shard_of(name: str, module: str, bins: list[Any]) -> list[int]:
    return [i for i, b in enumerate(bins) if any(fnmatch.fnmatchcase(name, u.glob(module)) for u in b)]


def test_the_split_covers_every_mutant_exactly_once(mc: ModuleType) -> None:
    src = _source(12, 40)
    bins = mc.split_module(src, 100)
    assert len(bins) > 1
    names = _names(A, src, mc, 137)  # 137: numbers ending in every digit, and past 100
    assert all(len(_shard_of(n, A, bins)) == 1 for n in names)
    assert mc.unassigned_mutants(names, A, bins) == []


def test_the_split_is_deterministic_and_ignores_function_order(mc: ModuleType) -> None:
    src = _source(9, 30)
    first = mc.split_module(src, 80)
    assert mc.split_module(src, 80) == first
    reordered = "\n\n".join(reversed(src.split("\n\n")))
    assert mc.split_module(reordered, 80) == first


def test_each_shard_stays_within_the_target(mc: ModuleType) -> None:
    bins = mc.split_module(_source(20, 25), 120)
    loads = [sum(u.weight for u in b) for b in bins]
    assert max(loads) <= 120
    # the fewest shards that fit: packed into one fewer, some shard would be over the target
    weighted = [u for b in bins for u in b if u.weight > 0]
    assert max(sum(u.weight for u in b) for b in mc._pack(weighted, len(bins) - 1)) > 120


def test_a_module_that_fits_is_one_shard_named_after_it(mc: ModuleType) -> None:
    src = _source(3, 10)
    assert len(mc.split_module(src, 500)) == 1
    assert mc.shard_ids(A, mc.split_module(src, 500)) == [A]
    bins = mc.split_module(_source(3, 40), 50)
    h = mc.split_hash(A, bins)
    assert len(h) == 12
    assert mc.shard_ids(A, bins) == [f"{A}#{k}of{len(bins)}@{h}" for k in range(1, len(bins) + 1)]


def test_a_function_bigger_than_a_shard_is_split_by_last_digit(mc: ModuleType) -> None:
    src = _source(1, 150)  # about 300 mutants: three parts of a 100-mutant shard
    bins = mc.split_module(src, 100)
    parts = [u for b in bins for u in b if u.key == "x_f0"]
    assert sorted(u.digits for u in parts) == ["0123", "456", "789"]
    assert len({i for i, b in enumerate(bins) for u in b if u.key == "x_f0"}) == 3
    assert mc.unassigned_mutants(_names(A, src, mc, 250), A, bins) == []


@pytest.mark.parametrize("parts", range(1, 11))
def test_digit_groups_partition_the_ten_digits(mc: ModuleType, parts: int) -> None:
    groups = mc.digit_groups(parts)
    assert len(groups) == parts
    assert "".join(groups) == "0123456789"
    assert max(map(len, groups)) - min(map(len, groups)) <= 1


def test_methods_use_mutmut_keys_and_decorated_functions_carry_no_weight(mc: ModuleType) -> None:
    funcs = dict(mc.mutated_functions(_source(1, 5)))
    assert f"x{SEP}C{SEP}m" in funcs
    assert funcs["x_skipped"] == 0
    assert funcs["x_f0"] >= 5


def test_a_mutant_outside_the_split_is_reported(mc: ModuleType) -> None:
    bins = mc.split_module(_source(6, 40), 100)
    stray = [f"{A}.x_new_function__mutmut_1", f"{B}.x_f__mutmut_1"]
    assert mc.unassigned_mutants(stray, A, bins) == [f"{A}.x_new_function__mutmut_1: matches 0 shards"]


@reads_real_sources
@pytest.mark.parametrize("module", ["health_data_registry.registry", "health_data_registry.query"])
def test_the_real_split_covers_mutmuts_own_mutants_once(mc: ModuleType, module: str) -> None:
    # mutmut's own generator, no test run: every mutant it makes of the module is in exactly one shard.
    from mutmut.mutation.file_mutation import mutate_file_contents

    path = mc.module_path(module)
    made = mutate_file_contents(str(path), path.read_text(encoding="utf-8")).mutant_names
    names = [f"{module}.{n}" for n in made]
    bins = mc.module_shards(module)
    assert len(bins) > 1
    assert names
    assert mc.unassigned_mutants(names, module, bins) == []


# Shard ids -------------------------------------------------------------------------------------------------


def test_shard_ids_parse(mc: ModuleType) -> None:
    assert mc.parse_shard_id("health_data_registry.registry#3of4") == ("health_data_registry.registry", 3, 4)
    assert mc.parse_shard_id("eval.scorer") == ("eval.scorer", 1, 1)


@pytest.mark.parametrize(
    "shard",
    [
        "health_data_registry.tools#0of2",
        "health_data_registry.tools#3of2",
        "health_data_registry.tools#1of1",
        "health_data_registry.tools#01of2",
        "health_data_registry.tools#1of2;id",
        "health_data_registry.tools#1of2 ",
        "health_data_registry.tools#1",
        "health_data_registry.$(id)#1of2",
        "other_pkg.tools#1of2",
    ],
)
def test_a_malformed_shard_id_is_refused(mc: ModuleType, shard: str) -> None:
    with pytest.raises(ValueError):
        mc.parse_shard_id(shard)


@reads_real_sources
def test_a_shard_id_from_another_version_of_the_source_is_refused(mc: ModuleType) -> None:
    bins = mc.module_shards("health_data_registry.tools")
    n, h = len(bins), mc.split_hash("health_data_registry.tools", bins)
    assert mc.shard_globs(f"health_data_registry.tools#1of{n}@{h}")
    with pytest.raises(ValueError, match="now splits into"):
        mc.shard_globs(f"health_data_registry.tools#1of{n + 1}@{h}")
    with pytest.raises(ValueError, match="needs its split hash"):
        mc.shard_globs(f"health_data_registry.tools#1of{n}")


@reads_real_sources
def test_too_many_shards_for_one_matrix_is_an_error(mc: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mc, "MAX_SHARDS", 3)
    with pytest.raises(ValueError, match="more than one CI matrix"):
        mc.list_shards(["health_data_registry.tools"])


@reads_real_sources
def test_module_mode_fails_closed_on_a_stale_shard_id(mc: ModuleType, tmp_path: Path) -> None:
    report = tmp_path / "r.json"
    assert mc.main(["--module", "health_data_registry.tools#1of99", "--report", str(report)]) == 1
    assert json.loads(report.read_text(encoding="utf-8"))["baseline_ok"] is False


def test_module_mode_refuses_a_malformed_shard_id(mc: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        mc.main(["--module", "health_data_registry.tools#2of1", "--report", str(tmp_path / "r.json")])


# One shard of a split module, end to end (mutmut replaced) -------------------------------------------------


def test_a_shard_runs_and_counts_only_its_own_mutants(
    mc: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _source(6, 40)
    bins = mc.split_module(src, 100)
    monkeypatch.setattr(mc, "module_shards", lambda module: mc.split_module(src, 100))
    first = mc.shard_ids(A, bins)[0]
    names = _names(A, src, mc, 23)
    mine = [x for x in names if _shard_of(x, A, bins) == [0]]
    # Other shards' mutants are `not checked` in this run: they are not this shard's, so they never count.
    lines = [f"    {x}: {'killed' if x in mine else 'not checked'}" for x in names]
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0, stdout="\n".join(lines) if cmd[1] == "results" else "", stderr=""
        )

    monkeypatch.setattr(mc.subprocess, "run", fake_run)
    r = mc.run_mutmut([first])
    assert calls[0][:2] == ["mutmut", "run"]
    assert calls[0][2:] == [u.glob(A) for u in bins[0]]
    assert (r.killed, r.survived, r.incomplete, r.unassigned) == (len(mine), 0, 0, [])
    assert mc.shard_failure(r) is None

    # A mutant of the module that no shard covers fails the shard, whatever its score.
    lines.append(f"    {A}.x_unknown__mutmut_1: not checked")
    r = mc.run_mutmut([first])
    assert r.unassigned == [f"{A}.x_unknown__mutmut_1: matches 0 shards"]
    assert "in no shard" in mc.shard_failure(r)


# Aggregating split modules ---------------------------------------------------------------------------------


def test_every_shard_of_a_split_module_must_report(mc: ModuleType) -> None:
    ids = [f"{A}#1of3", f"{A}#2of3", f"{A}#3of3", B]
    shards = [Shard([s], killed=10) for s in ids]
    assert mc.aggregate(shards, 70.0, ids)[0]
    ok, lines = mc.aggregate([s for s in shards if s.modules != [f"{A}#2of3"]], 70.0, ids)
    assert not ok
    assert f"FAILED {A}#2of3: no shard report (the shard was cancelled, timed out or never ran)" in lines


@pytest.mark.parametrize(
    "expected",
    [
        [f"{A}#1of3", f"{A}#3of3"],
        [f"{A}#1of2", f"{A}#2of3", f"{A}#3of3"],
        [A, f"{A}#1of2", f"{A}#2of2"],
    ],
)
def test_an_expect_list_with_part_of_a_split_fails(mc: ModuleType, expected: list[str]) -> None:
    shards = [Shard([s], killed=10) for s in expected]
    ok, lines = mc.aggregate(shards, 70.0, expected)
    assert not ok
    assert any("not one complete split" in line for line in lines)


def test_a_shard_with_an_unassigned_mutant_fails_the_aggregate(mc: ModuleType) -> None:
    ids = [f"{A}#1of2", f"{A}#2of2"]
    shards = [Shard([ids[0]], killed=10), Shard([ids[1]], killed=10, unassigned=["x: matches 0 shards"])]
    ok, lines = mc.aggregate(shards, 70.0, ids)
    assert not ok
    assert any("in no shard, or in several" in line for line in lines)


def test_aggregate_mode_reads_shard_reports_with_hash_names(mc: ModuleType, tmp_path: Path) -> None:
    ids = [f"{A}#1of2", f"{A}#2of2"]
    for s in ids:
        _write(tmp_path, Shard([s], killed=9, survived=1))
    assert (tmp_path / f"{A}#1of2.json").exists()
    assert mc.main(["--aggregate", str(tmp_path), "--expect", json.dumps(ids)]) == 0
    assert mc.main(["--aggregate", str(tmp_path), "--expect", json.dumps([*ids, f"{A}#3of3"])]) == 1


@pytest.mark.parametrize("expect", [json.dumps([f"{A}#1of2;id"]), "[1]", "not json"])
def test_aggregate_mode_refuses_a_malformed_expect(
    mc: ModuleType, tmp_path: Path, expect: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # A one-line usage error, as --module gives, never a traceback.
    with pytest.raises(SystemExit) as exc:
        mc.main(["--aggregate", str(tmp_path), "--expect", expect])
    assert exc.value.code == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert err[-1].startswith("pytest: error: --expect: ") or "error: --expect: " in err[-1]
    assert "Traceback" not in "\n".join(err)


def test_a_report_without_the_unassigned_field_is_an_error(mc: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text(
        json.dumps({"modules": [A], "killed": 1, "survived": 0, "baseline_ok": True, "incomplete": 0}),
        encoding="utf-8",
    )
    with pytest.raises(KeyError):
        mc.load_reports(tmp_path)


# Findings of the review round on PR #83 ---------------------------------------------------------------------


def _one_function(size: int, name: str = "big") -> str:
    body = "\n".join(f"    x = {i}" for i in range(size))
    return f"def {name}():\n{body}\n    return x\n"


def test_one_very_big_function_never_makes_an_empty_shard(mc: ModuleType) -> None:
    # About 1,200 estimated mutants with a target of 100: twelve shards' worth, but only ten digit parts.
    src = _one_function(600)
    bins = mc.split_module(src, 100)
    assert 1 < len(bins) <= 10
    assert all(b for b in bins)
    assert all([u.glob(A) for u in b] for b in bins)
    assert mc.unassigned_mutants([f"{A}.x_big__mutmut_{n}" for n in range(1, 1300)], A, bins) == []


def test_mutmut_is_never_run_with_no_names(
    mc: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(mc, "shard_globs", lambda shard: [])
    monkeypatch.setattr(mc.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    with pytest.raises(ValueError, match="no mutant names"):
        mc.run_mutmut([f"{A}#1of2@{'0' * 12}"])
    report = tmp_path / "r.json"
    assert mc.main(["--module", f"{A}#1of2@{'0' * 12}", "--report", str(report)]) == 1
    assert json.loads(report.read_text(encoding="utf-8"))["baseline_ok"] is False
    assert calls == []


@pytest.mark.parametrize(
    "src",
    [
        "def f():  # pragma: no mutate block\n    x = 1\n    return x + 2\n",
        "def f():\n    # pragma: no mutate block\n    x = 1\n    return x + 2\n",
        "def f():\n    # pragma: no mutate start\n    x = 1\n    return x + 2\n    # pragma: no mutate end\n",
        "def f():\n    x = 1  # pragma: no mutate\n    return x + 2  # pragma: no mutate\n",
        "def f():  # pragma: no cover\n    x = 1\n    return x + 2\n",
        "class C:\n    def __new__(cls):\n        return 1 + 2\n",
        "class C(Enum):\n    def __init__(self):\n        self.x = 1 + 2\n",
        "class C(enum.Enum):\n    def _generate_next_value_(name):\n        return 1 + 2\n",
        "def f(xs):\n    return len(xs[1:2])\n",
        "def f(xs):\n    return isinstance(xs, (1, 2))\n",
    ],
)
def test_a_function_mutmut_never_mutates_weighs_nothing(mc: ModuleType, src: str) -> None:
    assert [w for _, w in mc.mutated_functions(src)] == [0]


def test_a_pragma_on_one_line_leaves_the_rest_of_the_function_weighed(mc: ModuleType) -> None:
    funcs = dict(mc.mutated_functions("def f():\n    x = 1  # pragma: no mutate\n    return x + 2\n"))
    assert funcs["x_f"] == 2  # x + 2: the operator and the constant, not the assignment


def test_functions_never_mutated_do_not_make_a_shard_of_their_own(mc: ModuleType) -> None:
    skipped = "\n\n".join(
        f"def s{i}():  # pragma: no mutate block\n{_one_function(60)[10:]}" for i in range(6)
    )
    src = "\n\n".join([_one_function(40, "a"), _one_function(40, "b"), skipped])
    bins = mc.split_module(src, 100)
    assert len(bins) == 2
    assert all(any(u.weight > 0 for u in b) for b in bins)


def test_a_shard_whose_names_match_no_mutant_has_nothing_to_mutate(
    mc: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _source(6, 40)
    bins = mc.split_module(src, 100)
    monkeypatch.setattr(mc, "module_shards", lambda module: mc.split_module(src, 100))
    ids = mc.shard_ids(A, bins)
    others = [x for x in _names(A, src, mc, 23) if _shard_of(x, A, bins) != [0]]
    results = "\n".join(f"    {x}: not checked" for x in others)

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        if cmd[1] == "results":
            return SimpleNamespace(returncode=0, stdout=results, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr=f"AssertionError: {mc.NOTHING_MATCHES}\n")

    monkeypatch.setattr(mc.subprocess, "run", fake_run)
    report = tmp_path / "r.json"
    assert mc.main(["--module", ids[0], "--report", str(report)]) == 0
    out = capsys.readouterr().out
    assert "nothing to mutate" in out
    assert "baseline" not in out
    assert json.loads(report.read_text(encoding="utf-8"))["baseline_ok"] is True

    # Any other failure of the run is still a failed baseline.
    monkeypatch.setattr(
        mc.subprocess,
        "run",
        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="tests failed"),
    )
    assert mc.main(["--module", ids[0], "--report", str(report)]) == 1
    assert json.loads(report.read_text(encoding="utf-8"))["baseline_ok"] is False


def test_overload_stubs_and_their_function_are_one_unit(mc: ModuleType) -> None:
    stubs = "@overload\ndef big(x: int) -> int: ...\n\n\n@overload\ndef big(x: str) -> str: ...\n"
    src = "\n\n".join(
        [stubs, _one_function(150), _one_function(40, "small"), "@overload\ndef small(): ...\n"]
    )
    keys = [k for k, _ in mc.mutated_functions(src)]
    assert keys == ["x_big", "x_small"]
    bins = mc.split_module(src, 100)
    assert len(bins) > 1
    names = [f"{A}.x_{f}__mutmut_{n}" for f in ("big", "small") for n in range(1, 400)]
    assert all(len(_shard_of(n, A, bins)) == 1 for n in names)
    assert mc.unassigned_mutants(names, A, bins) == []


def test_a_split_that_moved_functions_is_refused_even_with_the_same_count(
    mc: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = "\n\n".join(_one_function(n, f"f{n}") for n in (30, 25, 20, 15))
    after = "\n\n".join(_one_function(n, f"f{n}") for n in (30, 25, 20, 16, 2))
    old, new = mc.split_module(before, 60), mc.split_module(after, 60)
    assert len(old) == len(new)
    assert mc.split_hash(A, old) != mc.split_hash(A, new)
    listed = mc.shard_ids(A, old)
    monkeypatch.setattr(mc, "module_shards", lambda module: new)
    with pytest.raises(ValueError, match="splits differently"):
        mc.shard_globs(listed[0])
    monkeypatch.setattr(mc, "module_shards", lambda module: old)
    assert mc.shard_globs(listed[0]) == [u.glob(A) for u in old[0]]


def test_an_expect_list_mixing_two_splits_of_a_module_fails(mc: ModuleType) -> None:
    ids = [f"{A}#1of2@{'a' * 12}", f"{A}#2of2@{'b' * 12}"]
    assert mc.split_gaps(ids) == [f"{A}: the expected shards [1, 2] are not one complete split of it"]
    assert mc.split_gaps([f"{A}#1of2@{'a' * 12}", f"{A}#2of2@{'a' * 12}"]) == []


def test_the_list_job_splits_with_the_shards_python() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    list_job = ci.split("  mutation-list:", 1)[1].split("\n  mutation:", 1)[0]
    shard_job = ci.split("\n  mutation:", 1)[1].split("\n  mutation-aggregate:", 1)[0]
    pin = 'python-version: "3.12"'
    assert pin in list_job and pin in shard_job
    assert "python3 scripts/mutate_changed.py" not in list_job
    assert "uv run --no-project python scripts/mutate_changed.py --list" in list_job
