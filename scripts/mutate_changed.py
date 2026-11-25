"""Mutation-test the modules a change touches (spec: mutation tests on changed functions, during development).

Usage:
  python scripts/mutate_changed.py [--base origin/main] [--floor 70] [--all]   # local: `make mutate`
  python scripts/mutate_changed.py --list [--base origin/main] [--all]         # CI: changed modules as JSON
  python scripts/mutate_changed.py --module M --report out.json                # CI: one shard
  python scripts/mutate_changed.py --aggregate DIR --expect '["m", ...]'       # CI: floor on the total

Finds the Python modules under src/ and eval/ (and the scripts in MUTATED_SCRIPTS) changed since --base (merge-base), runs mutmut on just those modules,
prints each mutant it did not kill, and fails when the share of killed mutants among all of them is below
--floor. Every status but `killed` and `caught by type check` counts against the score: `no tests`, `skipped`
and `timeout` are mutants nothing noticed.
A survivor means a test suite that would not notice that change to the code: add or sharpen a test, or say in
the PR why the mutant is equivalent.

CI shards the run, one job per module, and the aggregate step applies the floor to the summed counts. It fails
closed: a shard whose baseline failed, whose run left mutants not checked, interrupted or crashed (segfault), or
that left no report fails the whole check. A clean run that finds no mutable function (constants only, or every
function under `# pragma: no mutate`) has nothing to mutate, and passes, in CI and locally alike.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "health_data_registry"
# Mutated packages: the server, and the eval scorer that decides release gates. Keep in step with
# [tool.mutmut] source_paths in pyproject.toml.
PACKAGES = (PKG, ROOT / "eval")
# Scripts that decide something on their own (which branches get deleted) are mutated too, one file each.
MUTATED_SCRIPTS = ("scripts/branch_hygiene.py",)
PACKAGE_DIRS = ("src/health_data_registry", "eval", *MUTATED_SCRIPTS)
# Module names reach CI job names, artifact names and the mutmut command line: allow nothing else.
# mutmut statuses (mutmut/stats.py). Killed ones raise the score; every other status lowers it.
KILLED_STATUSES = frozenset({"killed", "caught by type check"})
# A run that leaves any of these did not finish checking its mutants: the shard fails, whatever its score.
INCOMPLETE_STATUSES = frozenset({"not checked", "check was interrupted by user", "segfault"})
MODULE_NAME = re.compile(r"^(?:(?:health_data_registry|eval)(\.[a-z_][a-z0-9_]*)+|scripts\.branch_hygiene)$")


@dataclass
class ShardResult:
    """What one mutmut run checked. baseline_ok is False when the tests failed on the unmutated copy.

    survived counts every mutant not killed (survived, timeout, no tests, skipped, ...); incomplete counts the
    ones the run never finished (not checked, interrupted, segfault), which fail the shard. survivors holds
    one `name: status` line per mutant not killed.
    """

    modules: list[str]
    killed: int = 0
    survived: int = 0
    baseline_ok: bool = True
    survivors: list[str] = field(default_factory=list)
    incomplete: int = 0

    @property
    def checked(self) -> int:
        return self.killed + self.survived


def changed_modules(base: str) -> list[str]:
    merge_base = (
        subprocess.run(
            ["git", "merge-base", base, "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()
        or base
    )
    out = subprocess.run(
        ["git", "diff", "--name-only", merge_base, "--", *PACKAGE_DIRS],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", *PACKAGE_DIRS],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return sorted(
        {_module(p) for p in (out + untracked).split() if p.endswith(".py") and (ROOT / p).exists()}
    )


def _module(path: str) -> str:
    """src/health_data_registry/x.py -> health_data_registry.x; eval/x.py -> eval.x."""
    rel = Path(path).with_suffix("")
    if rel.parts[0] == "src":
        rel = rel.relative_to("src")
    return ".".join(rel.parts)


def select_modules(mods: list[str]) -> list[str]:
    """The modules to mutate: sorted, without package __init__ files. A strange file name is an error."""
    picked = sorted({m for m in mods if not m.endswith("__init__")})
    bad = [m for m in picked if not MODULE_NAME.fullmatch(m)]
    if bad:
        raise ValueError(f"not a plain module name: {', '.join(bad)}")
    return picked


def list_json(mods: list[str]) -> str:
    """The `--list` output: one JSON array on one line, ready for a CI matrix."""
    return json.dumps(select_modules(mods), separators=(",", ":"))


def parse_results(text: str, mods: list[str]) -> ShardResult:
    """Count the mutants of `mods` in `mutmut results` output (`name: status` lines). Fails closed: any
    status but a kill, including one this script has never seen, counts as not killed."""
    r = ShardResult(modules=mods)
    for line in text.splitlines():
        name, _, status = line.strip().rpartition(": ")
        if not any(name.startswith(m + ".") for m in mods):
            continue
        if status in KILLED_STATUSES:
            r.killed += 1
            continue
        r.survived += 1
        r.survivors.append(f"{name}: {status}")
        if status in INCOMPLETE_STATUSES:
            r.incomplete += 1
    return r


def run_mutmut(mods: list[str]) -> ShardResult:
    print("Mutating:", ", ".join(mods))
    shutil.rmtree(ROOT / "mutants", ignore_errors=True)
    run = subprocess.run(["mutmut", "run", *[f"{m}.*" for m in mods]], cwd=ROOT, check=False)
    if run.returncode != 0:
        # A failed baseline (the tests fail on the unmutated copy) checks nothing: never report it as a score.
        print(
            f"mutmut run failed (exit code {run.returncode}), so no mutant was checked. Fix the run above first."
        )
        return ShardResult(modules=mods, baseline_ok=False)
    res = subprocess.run(
        ["mutmut", "results", "--all", "true"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        print(f"mutmut results failed (exit code {res.returncode}), so the run cannot be scored.")
        return ShardResult(modules=mods, baseline_ok=False)
    result = parse_results(res.stdout, mods)
    for s in result.survivors:
        print("not killed:", s)
    return result


def shard_failure(r: ShardResult) -> str | None:
    """Why a shard counts as failed (fail closed), or None. The floor is applied to the total, not here."""
    if not r.baseline_ok:
        return f"{', '.join(r.modules)}: mutmut failed its baseline run, so no mutant was checked"
    if r.incomplete:
        return (
            f"{', '.join(r.modules)}: {r.incomplete} mutant(s) not checked, interrupted or crashed, so the run"
            " did not finish"
        )
    return None


def aggregate(results: list[ShardResult], floor: float, expected: list[str]) -> tuple[bool, list[str]]:
    """Sum every shard's counts and apply the floor to the total.

    Fails when an expected module has no report, any shard failed its baseline or left mutants unchecked, or
    the total score is under the floor. Clean shards that found no mutant pass: there was nothing to mutate.
    """
    lines: list[str] = []
    covered = {m for r in results for m in r.modules}
    missing = sorted(set(expected) - covered)
    failures = [f"{m}: no shard report (the shard was cancelled, timed out or never ran)" for m in missing]
    failures += [f for f in map(shard_failure, results) if f]
    if not expected and not results:
        return True, ["No changed modules to mutate."]
    killed = sum(r.killed for r in results)
    checked = sum(r.checked for r in results)
    for r in sorted(results, key=lambda r: r.modules):
        lines.append(f"{', '.join(r.modules)}: {r.killed} killed of {r.checked}")
    lines += [f"not killed: {s}" for r in sorted(results, key=lambda r: r.modules) for s in r.survivors]
    lines += [f"FAILED {f}" for f in failures]
    if not checked:
        if failures:
            lines.append("No mutant was scored, and a shard failed. Failing.")
            return False, lines
        lines.append("Nothing to mutate: the changed modules have no mutable function.")
        return True, lines
    score = 100.0 * killed / checked
    lines.append(
        f"Mutation score on changed modules: {score:.1f}% ({killed} killed of {checked}); floor {floor:.0f}%"
    )
    return (not failures and score >= floor), lines


def load_reports(directory: Path) -> list[ShardResult]:
    """Every shard report (*.json) under `directory`. An unreadable report is an error, never a zero."""
    out = []
    for p in sorted(directory.rglob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out.append(
            ShardResult(
                modules=[str(m) for m in d["modules"]],
                killed=int(d["killed"]),
                survived=int(d["survived"]),
                baseline_ok=d["baseline_ok"] is True,
                survivors=[str(s) for s in d.get("survivors", [])],
                incomplete=int(d["incomplete"]),
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--floor", type=float, default=70.0)
    ap.add_argument("--all", action="store_true", help="every module, not only changed ones (slow)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="print the modules to mutate as a JSON array")
    mode.add_argument("--module", help="CI shard: mutate this one module (needs --report)")
    mode.add_argument("--aggregate", type=Path, metavar="DIR", help="CI: combine the shard reports in DIR")
    ap.add_argument("--report", type=Path, help="with --module: write the shard's counts here as JSON")
    ap.add_argument("--expect", help="with --aggregate (required): the --list output, as JSON")
    a = ap.parse_args(argv)

    if a.aggregate:
        if a.expect is None:  # without it a missing shard could never be noticed
            ap.error("--aggregate needs --expect (the --list output; pass '[]' explicitly for none)")
        ok, lines = aggregate(load_reports(a.aggregate), a.floor, select_modules(json.loads(a.expect)))
        print("\n".join(lines))
        return 0 if ok else 1

    if a.module:
        if not a.report:
            ap.error("--module needs --report")
        shard = select_modules([a.module])
        if not shard:  # an empty list would make mutmut mutate every module
            ap.error(f"--module {a.module} names no module to mutate")
        result = run_mutmut(shard)
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps(asdict(result), indent=1) + "\n", encoding="utf-8")
        failure = shard_failure(result)
        summary = (
            f"{a.module}: {result.killed} killed of {result.checked} (floor applied to the total)"
            if result.checked
            else f"{a.module}: nothing to mutate (no mutable function)"
        )
        print(failure or summary)
        return 1 if failure else 0

    mods = select_modules(
        [_module(str(p.relative_to(ROOT))) for pkg in PACKAGES for p in pkg.rglob("*.py")]
        + [_module(s) for s in MUTATED_SCRIPTS]
        if a.all
        else changed_modules(a.base)
    )
    if a.list:
        print(list_json(mods))
        return 0
    if not mods:
        print("No changed modules to mutate.")
        return 0
    result = run_mutmut(mods)
    ok, lines = aggregate([result], a.floor, mods)
    print("\n".join(lines))
    print("Inspect one with: uv run mutmut show <name>")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
