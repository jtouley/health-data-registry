"""Mutation-test the modules a change touches (spec: mutation tests on changed functions, during development).

Usage:
  python scripts/mutate_changed.py [--base origin/main] [--floor 70] [--all]   # local: `make mutate`
  python scripts/mutate_changed.py --list [--base origin/main] [--all]         # CI: shard ids as JSON
  python scripts/mutate_changed.py --module SHARD --report out.json            # CI: one shard
  python scripts/mutate_changed.py --aggregate DIR --expect '["m", ...]'       # CI: floor on the total

Finds the Python modules under src/ and eval/ changed since --base (merge-base), runs mutmut on just those modules,
prints each mutant it did not kill, and fails when the share of killed mutants among all of them is below
--floor. Every status but `killed` and `caught by type check` counts against the score: `no tests`, `skipped`
and `timeout` are mutants nothing noticed.
A survivor means a test suite that would not notice that change to the code: add or sharpen a test, or say in
the PR why the mutant is equivalent.

CI shards the run, one job per shard, and the aggregate step applies the floor to the summed counts. A module
whose estimated mutants fit one shard is one shard, named after the module. A bigger one is split into shards
named `<module>#<k>of<n>` (split_module): each shard is a set of mutmut name globs, one per top-level function or
method (`<module>.x_f__mutmut_*`, `<module>.xǁClassǁm__mutmut_*`), and a function too big for one shard is split
by the last digit of its mutant numbers (`<module>.x_f__mutmut_*[0123]`). The split is computed from the module's
source alone, with the standard library, so --list and every shard compute the same one. A shard of a split
module checks that every mutant of the module matches exactly one shard of the split, and fails if one does not.

It fails closed: a shard whose baseline failed, whose run left mutants not checked, interrupted or crashed
(segfault), whose module has a mutant no shard covers, or that left no report fails the whole check, and so does
an --expect list that names only some shards of a module. A clean run that finds no mutable function (constants
only, or every function under `# pragma: no mutate`) has nothing to mutate, and passes, in CI and locally alike.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import math
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
PACKAGE_DIRS = ("src/health_data_registry", "eval")
# mutmut statuses (mutmut/stats.py). Killed ones raise the score; every other status lowers it.
KILLED_STATUSES = frozenset({"killed", "caught by type check"})
# A run that leaves any of these did not finish checking its mutants: the shard fails, whatever its score.
INCOMPLETE_STATUSES = frozenset({"not checked", "check was interrupted by user", "segfault"})
# Module names reach CI job names, artifact names and the mutmut command line: allow nothing else.
MODULE_NAME = re.compile(r"^(?:health_data_registry|eval)(\.[a-z_][a-z0-9_]*)+$")
# A shard id: a module name, alone (the whole module) or with `#<k>of<n>` (shard k of n). `#` is valid in a GitHub
# job name, an artifact name and a file name; the workflow and precheck quote it wherever it reaches a shell.
SHARD_ID = re.compile(r"^(?P<module>[a-z_][a-z0-9_.]*)(?:#(?P<k>[1-9][0-9]*)of(?P<n>[1-9][0-9]*))?$")
# mutmut's separator in a method's mutant name, x<SEP>Class<SEP>method (mutmut CLASS_NAME_SEPARATOR, U+01C1).
SEP = "ǁ"

# Shard sizes, in estimated mutants (estimate_mutants comes within about 0.7 to 1.3 times mutmut's count).
# Measured 25 Nov 2026 on GitHub's runners (PR #49, CI run 45536523615): setup (mutant generation and the stats
# run) took 3.5 to 6.5 minutes, and mutants ran at 54 to 145 a minute, except registry.py's at 13 a minute (most
# tests load the registry, so each of its mutants runs most of the suite). 500 keeps a shard near 10 minutes of
# mutants at the slowest ordinary rate, and 150 does the same for registry.py. Add a module here when its shards
# run long.
SHARD_TARGET = 500
SHARD_TARGET_BY_MODULE = {"health_data_registry.registry": 150}
# GitHub runs at most 256 jobs from one matrix.
MAX_SHARDS = 256
DIGITS = "0123456789"


@dataclass
class ShardResult:
    """What one mutmut run checked. baseline_ok is False when the tests failed on the unmutated copy.

    survived counts every mutant not killed (survived, timeout, no tests, skipped, ...); incomplete counts the
    ones the run never finished (not checked, interrupted, segfault), which fail the shard. survivors holds
    one `name: status` line per mutant not killed. unassigned holds the module's mutants that match no shard of
    the split, or more than one; any fails the shard.
    """

    modules: list[str]
    killed: int = 0
    survived: int = 0
    baseline_ok: bool = True
    survivors: list[str] = field(default_factory=list)
    incomplete: int = 0
    unassigned: list[str] = field(default_factory=list)

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


def module_path(module: str) -> Path:
    """health_data_registry.x -> src/health_data_registry/x.py; eval.x -> eval/x.py."""
    parts = module.split(".")
    base = ROOT / "src" if parts[0] == "health_data_registry" else ROOT
    return base.joinpath(*parts[:-1], parts[-1] + ".py")


def select_modules(mods: list[str]) -> list[str]:
    """The modules to mutate: sorted, without package __init__ files. A strange file name is an error."""
    picked = sorted({m for m in mods if not m.endswith("__init__")})
    bad = [m for m in picked if not MODULE_NAME.fullmatch(m)]
    if bad:
        raise ValueError(f"not a plain module name: {', '.join(bad)}")
    return picked


# Splitting a module into shards ----------------------------------------------------------------------------


def _skipped_by_mutmut(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """mutmut leaves decorated functions alone, except a lone @staticmethod or @classmethod."""
    decs = fn.decorator_list
    if not decs:
        return False
    return not (
        len(decs) == 1 and isinstance(decs[0], ast.Name) and decs[0].id in ("staticmethod", "classmethod")
    )


def estimate_mutants(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """About how many mutants mutmut makes of one function: its constants, call arguments, operators,
    comparisons, assignments and conditional expressions, leaving out the docstring, annotations and decorators.
    Only the balance of the split depends on it, never which mutants are run or counted."""
    if _skipped_by_mutmut(fn):
        return 0
    ignored: set[int] = set()
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        ignored.add(id(fn.body[0].value))
    for node in ast.walk(fn):
        for sub in (getattr(node, "annotation", None), getattr(node, "returns", None)):
            if isinstance(sub, ast.AST):
                ignored.update(id(n) for n in ast.walk(sub))
    n = 0
    for node in ast.walk(fn):
        if id(node) in ignored:
            continue
        if isinstance(node, ast.Constant):
            n += 1
        elif isinstance(node, ast.Call):
            n += len(node.args) + len(node.keywords)
        elif isinstance(node, (ast.BinOp, ast.UnaryOp, ast.AugAssign, ast.Lambda, ast.Break, ast.Continue)):
            n += 1
        elif isinstance(node, ast.BoolOp):
            n += len(node.values) - 1
        elif isinstance(node, ast.Compare):
            n += len(node.ops)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            n += 1
        elif isinstance(node, ast.IfExp):
            n += 2
    return n


def mutated_functions(source: str) -> list[tuple[str, int]]:
    """(mutmut key, estimated mutants) for every function mutmut can mutate, in source order: the top-level
    functions (`x_f`) and the methods of top-level classes (`xǁClassǁm`). A nested function is part of its outer
    one, as in mutmut."""
    out: list[tuple[str, int]] = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((f"x_{node.name}", estimate_mutants(node)))
        elif isinstance(node, ast.ClassDef):
            out += [
                (f"x{SEP}{node.name}{SEP}{m.name}", estimate_mutants(m))
                for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
    return out


def digit_groups(parts: int) -> list[str]:
    """The ten last digits in `parts` contiguous groups, as even as possible: 3 -> 0123, 456, 789."""
    size, extra = divmod(len(DIGITS), parts)
    groups, start = [], 0
    for i in range(parts):
        end = start + size + (1 if i < extra else 0)
        groups.append(DIGITS[start:end])
        start = end
    return groups


@dataclass(frozen=True)
class Unit:
    """A function's mutants, or the part of them whose number ends in one of `digits` (empty: all of them)."""

    key: str
    weight: float
    digits: str = ""

    def glob(self, module: str) -> str:
        base = f"{module}.{self.key}__mutmut_*"
        return f"{base}[{self.digits}]" if self.digits else base


def _units(functions: list[tuple[str, int]], target: int) -> list[Unit]:
    out: list[Unit] = []
    for key, est in functions:
        parts = min(len(DIGITS), max(1, math.ceil(est / target)))
        if parts == 1:
            out.append(Unit(key, float(est)))
        else:
            out += [Unit(key, est / parts, d) for d in digit_groups(parts)]
    return out


def _pack(units: list[Unit], k: int) -> list[list[Unit]]:
    """Largest unit first into the lightest shard (ties go to the lower index): the same source gives the same
    shards, whatever order the functions are in."""
    bins: list[list[Unit]] = [[] for _ in range(k)]
    loads = [0.0] * k
    for u in sorted(units, key=lambda u: (-u.weight, u.key, u.digits)):
        i = min(range(k), key=lambda j: (loads[j], j))
        bins[i].append(u)
        loads[i] += u.weight
    return bins


def split_module(source: str, target: int) -> list[list[Unit]]:
    """The module's shards, each a list of units; a single shard when the estimated total fits `target`.

    Uses the fewest shards, from total / target up, whose heaviest stays within `target` (or one shard per unit).
    Each function is in exactly one shard, or each of its last-digit parts is, and every mutant name ends in one
    digit, so every mutant of the module matches exactly one shard. Functions with no estimated mutant go in the
    first shard, so no shard is made of them alone.
    """
    functions = mutated_functions(source)
    total = sum(est for _, est in functions)
    if total <= target:
        return [[Unit(key, float(est)) for key, est in functions]]
    units = _units(functions, target)
    weighted = [u for u in units if u.weight > 0]
    k = math.ceil(total / target)
    while True:
        bins = _pack(weighted, k)
        if k >= len(weighted) or max(sum(u.weight for u in b) for b in bins) <= target:
            break
        k += 1
    bins[0] += [u for u in units if u.weight <= 0]
    return bins


def module_shards(module: str) -> list[list[Unit]]:
    target = SHARD_TARGET_BY_MODULE.get(module, SHARD_TARGET)
    return split_module(module_path(module).read_text(encoding="utf-8"), target)


def shard_ids(module: str, count: int) -> list[str]:
    return [module] if count == 1 else [f"{module}#{k}of{count}" for k in range(1, count + 1)]


def parse_shard_id(shard: str) -> tuple[str, int, int]:
    """(module, k, n) of a shard id; a bare module name is shard 1 of 1. A malformed id is a ValueError."""
    m = SHARD_ID.fullmatch(shard)
    if not m or not MODULE_NAME.fullmatch(m["module"]):
        raise ValueError(f"not a plain module name or shard id: {shard}")
    if m["k"] is None:
        return m["module"], 1, 1
    k, n = int(m["k"]), int(m["n"])
    if n < 2 or k > n:
        raise ValueError(f"not a shard id: {shard} (shard k of n needs 1 <= k <= n and n >= 2)")
    return m["module"], k, n


def shard_globs(shard: str) -> list[str]:
    """The mutmut name globs one shard runs. A ValueError when the module now splits into a different number of
    shards than the id says (the id was listed from another version of the source)."""
    module, k, n = parse_shard_id(shard)
    if n == 1:
        return [f"{module}.*"]
    bins = module_shards(module)
    if len(bins) != n:
        raise ValueError(
            f"{shard}: {module} now splits into {len(bins)} shards, not {n}; list the shards again"
        )
    return [u.glob(module) for u in bins[k - 1]]


def unassigned_mutants(names: list[str], module: str, bins: list[list[Unit]]) -> list[str]:
    """The mutants of `module` among `names` that match no shard of the split, or more than one."""
    globs = [[u.glob(module) for u in b] for b in bins]
    out = []
    for name in names:
        if not name.startswith(module + "."):
            continue
        hits = sum(any(fnmatch.fnmatchcase(name, g) for g in shard) for shard in globs)
        if hits != 1:
            out.append(f"{name}: matches {hits} shards")
    return out


def list_shards(mods: list[str]) -> list[str]:
    """Every shard of the modules to mutate, module by module, shards in order."""
    out = [s for m in select_modules(mods) for s in shard_ids(m, len(module_shards(m)))]
    if len(out) > MAX_SHARDS:
        raise ValueError(
            f"{len(out)} shards is more than one CI matrix runs ({MAX_SHARDS}); raise SHARD_TARGET"
        )
    return out


def list_json(mods: list[str]) -> str:
    """The `--list` output: one JSON array of shard ids on one line, ready for a CI matrix."""
    return json.dumps(list_shards(mods), separators=(",", ":"))


def select_shards(ids: list[str]) -> list[str]:
    """The shard ids of an --expect list, validated, without duplicates, in their order."""
    for s in ids:
        parse_shard_id(s)
    return list(dict.fromkeys(ids))


def split_gaps(expected: list[str]) -> list[str]:
    """Modules the expected ids do not cover as one whole split: some shards of n only, or mixed counts."""
    by_module: dict[str, list[tuple[int, int]]] = {}
    for s in expected:
        module, k, n = parse_shard_id(s)
        by_module.setdefault(module, []).append((k, n))
    gaps = []
    for module, kn in sorted(by_module.items()):
        counts = {n for _, n in kn}
        ks = sorted(k for k, _ in kn)
        if len(counts) != 1 or ks != list(range(1, counts.pop() + 1)):
            gaps.append(f"{module}: the expected shards {ks} are not one complete split of it")
    return gaps


# Running and scoring ---------------------------------------------------------------------------------------


def parse_results(text: str, shards: list[str], globs: list[str] | None = None) -> ShardResult:
    """Count the mutants of `shards` in `mutmut results` output (`name: status` lines). Fails closed: any
    status but a kill, including one this script has never seen, counts as not killed."""
    patterns = globs if globs is not None else [g for s in shards for g in shard_globs(s)]
    r = ShardResult(modules=shards)
    for line in text.splitlines():
        name, _, status = line.strip().rpartition(": ")
        if not any(fnmatch.fnmatchcase(name, p) for p in patterns):
            continue
        if status in KILLED_STATUSES:
            r.killed += 1
            continue
        r.survived += 1
        r.survivors.append(f"{name}: {status}")
        if status in INCOMPLETE_STATUSES:
            r.incomplete += 1
    return r


def run_mutmut(shards: list[str]) -> ShardResult:
    print("Mutating:", ", ".join(shards))
    globs = [g for s in shards for g in shard_globs(s)]
    shutil.rmtree(ROOT / "mutants", ignore_errors=True)
    run = subprocess.run(["mutmut", "run", *globs], cwd=ROOT, check=False)
    if run.returncode != 0:
        # A failed baseline (the tests fail on the unmutated copy) checks nothing: never report it as a score.
        print(
            f"mutmut run failed (exit code {run.returncode}), so no mutant was checked. Fix the run above first."
        )
        return ShardResult(modules=shards, baseline_ok=False)
    res = subprocess.run(
        ["mutmut", "results", "--all", "true"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        print(f"mutmut results failed (exit code {res.returncode}), so the run cannot be scored.")
        return ShardResult(modules=shards, baseline_ok=False)
    result = parse_results(res.stdout, shards, globs)
    names = [line.strip().rpartition(": ")[0] for line in res.stdout.splitlines()]
    for module in sorted({parse_shard_id(s)[0] for s in shards if parse_shard_id(s)[2] > 1}):
        result.unassigned += unassigned_mutants(names, module, module_shards(module))
    for s in result.survivors:
        print("not killed:", s)
    for s in result.unassigned:
        print("in no shard, or in several:", s)
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
    if r.unassigned:
        return (
            f"{', '.join(r.modules)}: {len(r.unassigned)} mutant(s) of the module are in no shard, or in several,"
            " so the split does not cover the module once"
        )
    return None


def aggregate(results: list[ShardResult], floor: float, expected: list[str]) -> tuple[bool, list[str]]:
    """Sum every shard's counts and apply the floor to the total.

    Fails when an expected shard has no report, the expected shards of a module are not one complete split, any
    shard failed its baseline, left mutants unchecked or found a mutant no shard covers, or the total score is
    under the floor. Clean shards that found no mutant pass: there was nothing to mutate.
    """
    lines: list[str] = []
    covered = {m for r in results for m in r.modules}
    missing = [m for m in expected if m not in covered]
    failures = split_gaps(expected)
    failures += [f"{m}: no shard report (the shard was cancelled, timed out or never ran)" for m in missing]
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
                unassigned=[str(s) for s in d["unassigned"]],
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--floor", type=float, default=70.0)
    ap.add_argument("--all", action="store_true", help="every module, not only changed ones (slow)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="print the shards to run as a JSON array")
    mode.add_argument("--module", help="CI shard: a module, or one shard of it (module#KofN); needs --report")
    mode.add_argument("--aggregate", type=Path, metavar="DIR", help="CI: combine the shard reports in DIR")
    ap.add_argument("--report", type=Path, help="with --module: write the shard's counts here as JSON")
    ap.add_argument("--expect", help="with --aggregate (required): the --list output, as JSON")
    a = ap.parse_args(argv)

    if a.aggregate:
        if a.expect is None:  # without it a missing shard could never be noticed
            ap.error("--aggregate needs --expect (the --list output; pass '[]' explicitly for none)")
        ok, lines = aggregate(load_reports(a.aggregate), a.floor, select_shards(json.loads(a.expect)))
        print("\n".join(lines))
        return 0 if ok else 1

    if a.module:
        if not a.report:
            ap.error("--module needs --report")
        try:
            module, _, _ = parse_shard_id(a.module)
        except ValueError as e:
            ap.error(str(e))
        if module.endswith("__init__"):  # mutmut would find nothing named after it
            ap.error(f"--module {a.module} names no module to mutate")
        try:
            result = run_mutmut([a.module])
        except ValueError as e:  # the split changed since --list: fail, never run a different set of mutants
            print(e)
            result = ShardResult(modules=[a.module], baseline_ok=False)
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
