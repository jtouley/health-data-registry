"""Mutation-test the modules a change touches (spec: mutation tests on changed functions, during development).

Usage:
  python scripts/mutate_changed.py [--base origin/main] [--floor 70] [--all]   # local: `make mutate`
  python scripts/mutate_changed.py --list [--base origin/main] [--all]         # CI: changed modules as JSON
  python scripts/mutate_changed.py --module M --report out.json                # CI: one shard
  python scripts/mutate_changed.py --aggregate DIR --expect '["m", ...]'       # CI: floor on the total

Finds the Python modules under src/ changed since --base (merge-base), runs mutmut on just those modules,
prints each surviving mutant, and fails when the share of killed mutants among checked ones is below --floor.
A survivor means a test suite that would not notice that change to the code: add or sharpen a test, or say in
the PR why the mutant is equivalent.

CI shards the run, one job per module, and the aggregate step applies the floor to the summed counts. It fails
closed: a shard whose baseline failed, that checked no mutant, or that left no report fails the whole check.
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
# Module names reach CI job names, artifact names and the mutmut command line: allow nothing else.
MODULE_NAME = re.compile(r"^health_data_registry(\.[a-z_][a-z0-9_]*)+$")


@dataclass
class ShardResult:
    """What one mutmut run checked. baseline_ok is False when the tests failed on the unmutated copy."""

    modules: list[str]
    killed: int = 0
    survived: int = 0
    baseline_ok: bool = True
    survivors: list[str] = field(default_factory=list)

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
        ["git", "diff", "--name-only", merge_base, "--", "src/health_data_registry"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "src/health_data_registry"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return sorted(
        {_module(p) for p in (out + untracked).split() if p.endswith(".py") and (ROOT / p).exists()}
    )


def _module(path: str) -> str:
    rel = Path(path).relative_to("src").with_suffix("")
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


def parse_results(text: str, mods: list[str]) -> tuple[int, int, list[str]]:
    """Count killed and surviving mutants of `mods` in `mutmut results` output (`name: status` lines)."""
    killed = survived = 0
    survivors = []
    for line in text.splitlines():
        name, _, status = line.strip().rpartition(": ")
        if not any(name.startswith(m + ".") for m in mods):
            continue
        if status == "killed":
            killed += 1
        elif status in ("survived", "suspicious", "timeout"):
            survived += 1
            survivors.append(name)
    return killed, survived, survivors


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
    if res.returncode != 0:  # older mutmut has no --all flag
        res = subprocess.run(["mutmut", "results"], cwd=ROOT, capture_output=True, text=True, check=False)
    killed, survived, survivors = parse_results(res.stdout, mods)
    for s in survivors:
        print("survived:", s)
    return ShardResult(modules=mods, killed=killed, survived=survived, survivors=survivors)


def shard_failure(r: ShardResult) -> str | None:
    """Why a shard counts as failed (fail closed), or None. The floor is applied to the total, not here."""
    if not r.baseline_ok:
        return f"{', '.join(r.modules)}: mutmut failed its baseline run, so no mutant was checked"
    if not r.checked:
        return f"{', '.join(r.modules)}: no mutant was checked, so there is no mutation score"
    return None


def aggregate(results: list[ShardResult], floor: float, expected: list[str]) -> tuple[bool, list[str]]:
    """Sum every shard's counts and apply the floor to the total.

    Fails when an expected module has no report, any shard failed its baseline or checked nothing, or the
    total score is under the floor. No expected module and no report passes: nothing changed.
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
    lines += [f"FAILED {f}" for f in failures]
    if not checked:
        lines.append("No mutant was checked in any shard, so there is no mutation score. Failing.")
        return False, lines
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
    ap.add_argument("--expect", default="[]", help="with --aggregate: the --list output, as JSON")
    a = ap.parse_args(argv)

    if a.aggregate:
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
        print(
            failure or f"{a.module}: {result.killed} killed of {result.checked} (floor applied to the total)"
        )
        return 1 if failure else 0

    mods = select_modules(
        [_module(str(p.relative_to(ROOT))) for p in PKG.rglob("*.py")] if a.all else changed_modules(a.base)
    )
    if a.list:
        print(list_json(mods))
        return 0
    if not mods:
        print("No changed modules to mutate.")
        return 0
    result = run_mutmut(mods)
    if not result.baseline_ok:
        return 1
    ok, lines = aggregate([result], a.floor, mods)
    print("\n".join(lines))
    print("Inspect one with: uv run mutmut show <name>")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
