"""Mutation-test the modules a change touches (spec: mutation tests on changed functions, during development).

Usage: python scripts/mutate_changed.py [--base origin/main] [--floor 70] [--all]

Finds the Python modules under src/ changed since --base (merge-base), runs mutmut on just those modules,
prints each surviving mutant, and fails when the share of killed mutants among checked ones is below --floor.
A survivor means a test suite that would not notice that change to the code: add or sharpen a test, or say in
the PR why the mutant is equivalent.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "health_data_registry"


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--floor", type=float, default=70.0)
    ap.add_argument("--all", action="store_true", help="every module, not only changed ones (slow)")
    a = ap.parse_args()
    mods = (
        sorted(_module(str(p.relative_to(ROOT))) for p in PKG.rglob("*.py"))
        if a.all
        else changed_modules(a.base)
    )
    mods = [m for m in mods if not m.endswith("__init__")]
    if not mods:
        print("No changed modules to mutate.")
        return 0
    print("Mutating:", ", ".join(mods))
    shutil.rmtree(ROOT / "mutants", ignore_errors=True)
    subprocess.run(["mutmut", "run", *[f"{m}.*" for m in mods]], cwd=ROOT, check=False)
    res = subprocess.run(
        ["mutmut", "results", "--all", "true"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if res.returncode != 0:  # older mutmut has no --all flag
        res = subprocess.run(["mutmut", "results"], cwd=ROOT, capture_output=True, text=True, check=False)
    killed = survived = 0
    survivors = []
    for line in res.stdout.splitlines():
        name, _, status = line.strip().rpartition(": ")
        if not any(name.startswith(m + ".") for m in mods):
            continue
        if status == "killed":
            killed += 1
        elif status in ("survived", "suspicious", "timeout"):
            survived += 1
            survivors.append(name)
    for s in survivors:
        print("survived:", s)
    checked = killed + survived
    score = 100.0 * killed / checked if checked else 100.0
    print(
        f"Mutation score on changed modules: {score:.1f}% ({killed} killed of {checked}); floor {a.floor:.0f}%"
    )
    print("Inspect one with: uv run mutmut show <name>")
    return 0 if score >= a.floor else 1


if __name__ == "__main__":
    sys.exit(main())
