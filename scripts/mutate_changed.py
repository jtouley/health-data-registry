"""Mutation-test the modules a change touches (spec: mutation tests on changed functions, during development).

Usage:
  python scripts/mutate_changed.py [--base origin/main] [--floor 70] [--all]       # local: `make mutate`
  python scripts/mutate_changed.py --list [--base origin/main] [--all]             # CI: shard ids as JSON
  python scripts/mutate_changed.py --module SHARD --base SHA --report out.json     # CI: one shard
  python scripts/mutate_changed.py --aggregate DIR --expect '["m", ...]'           # CI: floor on the total

Finds the Python modules under src/ and eval/ changed since --base (merge-base), runs mutmut on the functions the
change touches (changed_functions: a function holding a changed line, or reading a module-level name a changed
line defines; every function of a new file; --all: every function), prints each mutant it did not kill, and fails
when the share of killed mutants among all of them is below --floor. Every status but `killed` and `caught by type check` counts against the score: `no tests`, `skipped`
and `timeout` are mutants nothing noticed.
A survivor means a test suite that would not notice that change to the code: add or sharpen a test, or say in
the PR why the mutant is equivalent.

CI shards the run, one job per shard, and the aggregate step applies the floor to the summed counts. The shards
of a diff-scoped module are named `<module>#<k>of<n>@<split>` (split_module, split_hash), `#1of1@<split>` included;
only a whole module (--all) that fits one shard is named after the module alone. Each shard is a set of mutmut
name globs, one per top-level function or method (`<module>.x_f__mutmut_*`, `<module>.xǁClassǁm__mutmut_*`), and a
function too big for one shard is split by the last digit of its mutant numbers (`<module>.x_f__mutmut_*[0123]`).
The split is computed from the module's source and the changed functions alone, with the standard library, so
--list and every shard (given the same --base) compute the same one, and <split> is a hash of it: a shard whose
module now splits differently (the source or the scope changed, or another Python's ast) is refused. A
shard of a split module checks that every mutant of the module matches exactly one shard of the split, and fails
if one does not. A shard none of whose names matches a generated mutant has nothing to mutate, and passes.

It fails closed: a shard whose baseline failed, whose run left mutants not checked, interrupted or crashed
(segfault), whose module has a mutant no shard covers, or that left no report fails the whole check, and so does
an --expect list that names only some shards of a module. A clean run that finds no mutable function (constants
only, or every function under `# pragma: no mutate`) has nothing to mutate, and passes, in CI and locally alike.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import fnmatch
import hashlib
import io
import json
import math
import re
import shutil
import signal
import subprocess
import sys
import threading
import tokenize
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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
# A shard id: a module name, alone (the whole module) or with `#<k>of<n>@<split>` (shard k of n of the split whose
# hash is <split>, split_hash). `#` and `@` are valid in a GitHub job name, an artifact name and a file name; the
# workflow and precheck quote the id wherever it reaches a shell.
SHARD_ID = re.compile(
    r"^(?P<module>[a-z_][a-z0-9_.]*)(?:#(?P<k>[1-9][0-9]*)of(?P<n>[1-9][0-9]*)(?:@(?P<split>[0-9a-f]{12}))?)?$"
)
# mutmut's message when no generated mutant matches the names it was given (mutmut/__main__.py). It comes after
# the stats run, so the baseline passed: the shard has nothing to mutate.
NOTHING_MATCHES = "Filtered for specific mutants, but nothing matches"
# What mutmut never mutates (mutmut/mutation/file_mutation.py): these functions, these calls with their arguments,
# and these methods of an enum class.
NEVER_MUTATE_FUNCTION_NAMES = frozenset({"__getattribute__", "__setattr__", "__new__"})
NEVER_MUTATE_FUNCTION_CALLS = frozenset({"len", "isinstance"})
NEVER_MUTATE_ENUM_FUNCTION_NAMES = frozenset({"__init__", "_generate_next_value_"})
ENUM_BASE_NAMES = frozenset({"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"})
# coverage.py's default exclusion; mutmut ignores the statements it excludes, as under `# pragma: no mutate block`.
NO_COVER = re.compile(r"#\s*(pragma|PRAGMA)[:\s]?\s*(no|NO)\s*(cover|COVER)")
# mutmut's separator in a method's mutant name, x<SEP>Class<SEP>method (mutmut CLASS_NAME_SEPARATOR, U+01C1).
SEP = "ǁ"

# Shard sizes, in estimated mutants (estimate_mutants comes within about 0.7 to 1.3 times mutmut's count), for
# about 3 minutes of mutants per shard. Measured 25 Nov 2026 on GitHub's runners (PR #49, CI run 45536523615):
# mutants ran at 54 to 145 a minute, except registry.py's at 13 a minute (most tests load the registry, so each of
# its mutants runs most of the suite). 150 is under 3 minutes at the slowest ordinary rate, and 40 is about that
# for registry.py. Setup comes on top (3.5 to 6.5 minutes then). With mutmut_scope (CI run 45643738512, 25 Nov
# 2026, scoped shards): sql_guard took 7.5 minutes in all, of which checkout and install 14 s, generating its
# mutants 3 s, mutmut's stats run 3.0 minutes and its clean test run 2.9 minutes over the 48 test files that reach
# it, then 99 mutants in 1.2 minutes; registry.py took 5.1 minutes. Setup is now most of a shard; caching the stats
# run is #115's work. The shard job's timeout-minutes (12) is the hard stop. Add a module here when its shards run
# long.
SHARD_TARGET = 150
SHARD_TARGET_BY_MODULE = {"health_data_registry.registry": 40}
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


def merge_base(base: str) -> str:
    """The commit HEAD branched from `base` (git merge-base). A ValueError when git finds none: diffing against
    the tip of `base` would count the base branch's newer changes as this branch's."""
    found = subprocess.run(
        ["git", "merge-base", base, "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    if not found:
        raise ValueError(
            f"No merge base of {base} and HEAD, so the changed functions are unknown. Fetch {base} and more"
            " history (git fetch --unshallow, or a deeper fetch) and run again."
        )
    return found


def changed_modules(base: str) -> list[str]:
    merge_base_ = merge_base(base)
    out = subprocess.run(
        ["git", "diff", "--name-only", merge_base_, "--", *PACKAGE_DIRS],
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


# Diff scope: only the functions a change touches -----------------------------------------------------------

# Module -> the mutmut keys of its changed functions, for the modules set_scope scoped. A module not in it is
# mutated whole (`--all`). module_shards, shard_ids, shard_globs and run_mutmut all read it, so --list, every
# shard and the local run split the same functions.
SCOPE: dict[str, frozenset[str]] = {}
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def changed_lines(path: str, base_commit: str) -> set[int] | None:
    """The lines of `path` (in the working tree) a change since `base_commit` added or edited, and, for a pure
    deletion, the lines on either side of it. None when the file is new there (untracked, or added since the
    base), so every function in it counts as changed. --no-renames makes a moved file a new one."""
    status = subprocess.run(
        ["git", "diff", "--name-status", "--no-renames", base_commit, "--", path],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    if status and status[0].startswith("A"):
        return None
    if not status:  # unchanged since the base, or untracked (new)
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", path], cwd=ROOT, capture_output=True, check=False
        ).returncode == 0  # fmt: skip
        return set() if tracked else None
    diff = subprocess.run(
        ["git", "diff", "-U0", "--no-renames", "--no-color", base_commit, "--", path],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    lines: set[int] = set()
    for line in diff.splitlines():
        m = HUNK.match(line)
        if not m:
            continue
        start, count = int(m["start"]), int(m["count"] if m["count"] is not None else 1)
        lines.update(range(start, start + count) if count else (start, start + 1))
    return lines


FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def mutmut_functions(tree: ast.Module) -> Iterator[tuple[str, FunctionNode, ast.ClassDef | None]]:
    """(mutmut key, function, its class or None) for the top-level functions (`x_f`) and the methods of top-level
    classes (`xǁClassǁm`), in source order. A nested function is part of its outer one, as in mutmut. Both the
    diff scope (function_spans) and the split (mutated_functions) take their keys from here."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield f"x_{node.name}", node, None
        elif isinstance(node, ast.ClassDef):
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f"x{SEP}{node.name}{SEP}{m.name}", m, node


def _span(fn: FunctionNode) -> tuple[int, int]:
    first = min([fn.lineno, *(d.lineno for d in fn.decorator_list)])
    return first, fn.end_lineno or fn.lineno


def function_spans(source: str) -> dict[str, list[tuple[int, int]]]:
    """The lines each mutmut key spans (decorators included), with the keys mutated_functions uses: top-level
    functions and the methods of top-level classes. An `@overload`ed name has one span per definition."""
    out: dict[str, list[tuple[int, int]]] = {}
    for key, fn, _ in mutmut_functions(ast.parse(source)):
        out.setdefault(key, []).append(_span(fn))
    return out


def _loads(nodes: list[ast.AST]) -> set[str]:
    """The names these nodes read (ast.Name in a Load context), nested functions included."""
    return {
        n.id
        for node in nodes
        for n in ast.walk(node)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def _binds(stmt: ast.stmt) -> set[str]:
    """The module-level names a top-level statement (not a class) defines: assignment targets, imports, and the
    functions and classes it defines (under `if`, `try`, ...)."""
    out: set[str] = set()
    for n in ast.walk(stmt):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
        elif isinstance(n, ast.alias):
            out.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
    return out


def _class_level(cls: ast.ClassDef) -> list[ast.AST]:
    """A class's own code outside its methods: bases, keywords, decorators and the class-body statements."""
    body = [s for s in cls.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return [*cls.bases, *cls.keywords, *cls.decorator_list, *body]


def changed_functions(source: str, lines: set[int] | None) -> frozenset[str]:
    """The keys of the functions a change touches; every function when `lines` is None (a new file).

    A function holding a changed line is in. mutmut mutates function bodies only, so a change outside every
    function (a constant such as an allowlist, an import, a class attribute) makes no mutant of its own; instead
    the functions it can change the behaviour of are in: every function that reads a module-level name a changed
    statement defines, through other module-level definitions built from it (`_ALL = _A + _B`), and every method of
    a class whose own code (outside its methods) changed. A changed top-level statement that defines nothing (a
    bare call) puts every function in. A changed docstring, comment or blank line puts none in."""
    tree = ast.parse(source)
    functions = list(mutmut_functions(tree))
    if lines is None:
        return frozenset(k for k, _, _ in functions)
    spans = [_span(fn) for _, fn, _ in functions]
    inside = {n for n in lines if any(a <= n <= b for a, b in spans)}
    scope = {
        k for (k, _, _), (a, b) in zip(functions, spans, strict=True) if any(a <= n <= b for n in inside)
    }
    outside = lines - inside
    if not outside:
        return frozenset(scope)

    def touched(stmt: ast.stmt) -> bool:
        return any(stmt.lineno <= n <= (stmt.end_lineno or stmt.lineno) for n in outside)

    names: set[str] = set()
    classes: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) or not touched(stmt):
            continue  # a function's own lines are in its span, decorators included
        if isinstance(stmt, ast.ClassDef):
            classes.add(stmt.name)
            names.add(stmt.name)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # a docstring
        elif bound := _binds(stmt):
            names |= bound
        else:  # a top-level statement that defines nothing, such as a registration call: anything may change
            return frozenset(k for k, _, _ in functions)
    grew = True
    while grew:  # module-level definitions built from a changed name change too
        grew = False
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(stmt, ast.ClassDef):
                if stmt.name not in classes and _loads(_class_level(stmt)) & names:
                    classes.add(stmt.name)
                    names.add(stmt.name)
                    grew = True
            elif _loads([stmt]) & names and not _binds(stmt) <= names:
                names |= _binds(stmt)
                grew = True
    for key, fn, cls in functions:
        if (cls is not None and cls.name in classes) or _loads([fn]) & names:
            scope.add(key)
    return frozenset(scope)


def set_scope(mods: list[str], base: str) -> None:
    """Scope each module to the functions changed since the merge base of `base` and HEAD. A changed module that
    scopes to no function is named on stderr (stdout carries --list's JSON), so a run with no shard says why."""
    commit = merge_base(base)
    for module in mods:
        path = module_path(module)
        rel = str(path.relative_to(ROOT))
        SCOPE[module] = changed_functions(path.read_text(encoding="utf-8"), changed_lines(rel, commit))
        if not SCOPE[module]:
            print(
                f"{module} changed, but no function holds or reads the change (only comments, docstrings or"
                " unused definitions), so it has no mutant to test.",
                file=sys.stderr,
            )


def mutant_key(name: str, module: str) -> str:
    """`<module>.<key>__mutmut_<n>` -> <key>; '' for a name of another module."""
    if not name.startswith(module + "."):
        return ""
    return name[len(module) + 1 :].rpartition("__mutmut_")[0]


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


def _pragma_kind(comment: str) -> str | None:
    """'block', 'start', 'end' or 'bare' for a `# pragma: no mutate` comment, else None (as mutmut reads it)."""
    if "# pragma:" not in comment or "no mutate" not in comment:
        return None
    tokens = comment.partition("no mutate")[-1].strip().lstrip(": ").split(",", 1)[0].split()
    return tokens[0] if tokens and tokens[0] in ("block", "start", "end") else "bare"


def _spans(stmts: list[ast.stmt]) -> set[int]:
    return {n for s in stmts for n in range(s.lineno, (s.end_lineno or s.lineno) + 1)}


def _end(node: ast.AST) -> int:
    return getattr(node, "end_lineno", None) or 0


def _bodies(node: ast.AST) -> list[list[ast.stmt]]:
    out = [
        getattr(node, f) for f in ("body", "orelse", "finalbody") if isinstance(getattr(node, f, None), list)
    ]
    out += [h.body for h in getattr(node, "handlers", [])] + [c.body for c in getattr(node, "cases", [])]
    return [b for b in out if b and isinstance(b[0], ast.stmt)]


def ignored_lines(source: str, tree: ast.Module) -> set[int]:
    """The lines mutmut does not mutate: under `# pragma: no mutate` (bare, block, start ... end) or excluded
    from coverage (`# pragma: no cover`). An estimate for the split only; mutmut itself decides what it mutates."""
    stmts = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
    compound = [(n, b[0][0].lineno) for n in [tree, *stmts] if (b := _bodies(n))]
    lines = source.splitlines()
    out: set[int] = set()
    start: int | None = None
    try:
        comments = [
            t for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type == tokenize.COMMENT
        ]
    except (tokenize.TokenError, SyntaxError):
        return out
    for tok in comments:
        line = tok.start[0]
        standalone = not lines[line - 1][: tok.start[1]].strip()
        kind = _pragma_kind(tok.string)
        if NO_COVER.search(tok.string) and not standalone:
            out |= _spans([s for s in stmts if s.lineno == line])
            continue
        if kind == "bare":
            out.add(line)
        elif kind == "start":
            start = line
        elif kind == "end" and start is not None:
            out.update(range(start, line + 1))
            start = None
        elif kind == "block" and not standalone:
            # on a compound statement's header: the whole statement
            out |= _spans(
                [n for n, first in compound if isinstance(n, ast.stmt) and n.lineno <= line < first]
            )
            out.add(line)
        elif kind == "block":
            # on a line of its own: the rest of the innermost block that holds it
            holders = [n for n, _ in compound if n is tree or getattr(n, "lineno", 0) < line <= _end(n)]
            inner = max(holders, key=lambda n: getattr(n, "lineno", 0))
            out |= _spans([s for b in _bodies(inner) for s in b if s.lineno > line])
            out.add(line)
    return out


def _never_mutated_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in NEVER_MUTATE_FUNCTION_CALLS
    )


def estimate_mutants(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, ignored_at: frozenset[int] = frozenset()
) -> int:
    """About how many mutants mutmut makes of one function: its constants, call arguments, operators,
    comparisons, assignments and conditional expressions, leaving out the docstring, annotations, decorators,
    `len` and `isinstance` calls, and lines in `ignored_at` (ignored_lines). 0 for a function mutmut never mutates.
    Only the balance of the split depends on it, never which mutants are run or counted."""
    if _skipped_by_mutmut(fn) or fn.name in NEVER_MUTATE_FUNCTION_NAMES:
        return 0
    ignored: set[int] = set()
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        ignored.add(id(fn.body[0].value))
    for node in ast.walk(fn):
        for sub in (getattr(node, "annotation", None), getattr(node, "returns", None)):
            if isinstance(sub, ast.AST):
                ignored.update(id(n) for n in ast.walk(sub))
        if _never_mutated_call(node) or (node is not fn and getattr(node, "lineno", None) in ignored_at):
            ignored.update(id(n) for n in ast.walk(node))
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
    one, as in mutmut. A name defined more than once (`@overload` stubs and the function itself) is one key, with
    the estimates summed, so each mutant name falls in one shard. A function mutmut never mutates (under
    `# pragma: no mutate`, `__new__`, an enum's `__init__`, ...) weighs 0."""
    tree = ast.parse(source)
    ignored = frozenset(ignored_lines(source, tree))
    out: dict[str, int] = {}

    def add(key: str, fn: ast.FunctionDef | ast.AsyncFunctionDef, never: bool = False) -> None:
        out[key] = out.get(key, 0) + (0 if never else estimate_mutants(fn, ignored))

    for key, fn, cls in mutmut_functions(tree):
        enum = cls is not None and any(
            (b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", None)) in ENUM_BASE_NAMES
            for b in cls.bases
        )
        add(key, fn, enum and fn.name in NEVER_MUTATE_ENUM_FUNCTION_NAMES)
    return list(out.items())


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


def split_module(source: str, target: int, keys: frozenset[str] | None = None) -> list[list[Unit]]:
    """The module's shards, each a list of units; a single shard when the estimated total fits `target`.
    With `keys` (diff scope, see changed_functions), only those functions are split; with none of them in the
    module, there is no shard at all.

    Uses the fewest shards, from total / target up, whose heaviest stays within `target`, and never more shards
    than units, so no shard is empty (a function too big for one shard is at most ten units). Each function is in exactly one shard, or each of its last-digit parts is, and every mutant name ends in one
    digit, so every mutant of the module matches exactly one shard. Functions with no estimated mutant go in the
    first shard, so no shard is made of them alone.
    """
    functions = mutated_functions(source)
    if keys is not None:
        functions = [(key, est) for key, est in functions if key in keys]
        if not functions:
            return []
    total = sum(est for _, est in functions)
    if total <= target:
        return [[Unit(key, float(est)) for key, est in functions]]
    units = _units(functions, target)
    weighted = [u for u in units if u.weight > 0]
    k = min(math.ceil(total / target), len(weighted))
    while True:
        bins = _pack(weighted, k)
        if k >= len(weighted) or max(sum(u.weight for u in b) for b in bins) <= target:
            break
        k += 1
    bins = [b for b in bins if b]  # none is empty while k <= len(weighted); kept so an empty one never runs
    bins[0] += [u for u in units if u.weight <= 0]
    return bins


def module_shards(module: str) -> list[list[Unit]]:
    """The module's shards: of its changed functions only when set_scope has scoped it, else of all of them."""
    target = SHARD_TARGET_BY_MODULE.get(module, SHARD_TARGET)
    return split_module(module_path(module).read_text(encoding="utf-8"), target, SCOPE.get(module))


def split_hash(module: str, bins: list[list[Unit]]) -> str:
    """12 hex digits of the sha256 of the split's globs, shard by shard: any change to which mutants a shard runs
    changes it, so a shard listed from another version of the source (or another Python's ast) is refused."""
    globs = [[u.glob(module) for u in b] for b in bins]
    return hashlib.sha256(json.dumps(globs, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def shard_ids(module: str, bins: list[list[Unit]]) -> list[str]:
    """`module` for a whole module that is one shard; `module#<k>of<n>@<split hash>` for each shard of a split
    one, and for every shard of a diff-scoped module (`#1of1@<hash>` when one shard holds its changed functions)."""
    if len(bins) == 1 and module not in SCOPE:
        return [module]
    h = split_hash(module, bins)
    return [f"{module}#{k}of{len(bins)}@{h}" for k in range(1, len(bins) + 1)]


def _match_shard_id(shard: str) -> re.Match[str]:
    m = SHARD_ID.fullmatch(shard)
    if not m or not MODULE_NAME.fullmatch(m["module"]):
        raise ValueError(f"not a plain module name or shard id: {shard}")
    return m


def shard_split(shard: str) -> str | None:
    """The split hash an id carries (`@<hash>`), or None."""
    return _match_shard_id(shard)["split"]


def parse_shard_id(shard: str) -> tuple[str, int, int]:
    """(module, k, n) of a shard id; a bare module name is shard 1 of 1. A malformed id is a ValueError."""
    m = _match_shard_id(shard)
    if m["k"] is None:
        return m["module"], 1, 1
    k, n = int(m["k"]), int(m["n"])
    if (n < 2 and m["split"] is None) or k > n:
        raise ValueError(
            f"not a shard id: {shard} (shard k of n needs 1 <= k <= n, and n >= 2 or a split hash)"
        )
    return m["module"], k, n


def shard_globs(shard: str) -> list[str]:
    """The mutmut name globs one shard runs, never none. A ValueError when the id carries no split hash, or the
    module now splits into a different number of shards or a different split than the id says (the id was listed
    from another version of the source, or by another Python)."""
    module, k, n = parse_shard_id(shard)
    listed = shard_split(shard)
    if n == 1 and listed is None:
        if module in SCOPE:
            raise ValueError(
                f"{shard}: the module is diff-scoped, so its shard needs a split hash; list the shards again"
            )
        return [f"{module}.*"]
    if listed is None:
        raise ValueError(
            f"{shard}: a shard of a split module needs its split hash (@...); list the shards again"
        )
    bins = module_shards(module)
    if len(bins) != n:
        raise ValueError(
            f"{shard}: {module} now splits into {len(bins)} shards, not {n}; list the shards again"
        )
    now = split_hash(module, bins)
    if now != listed:
        raise ValueError(
            f"{shard}: {module} now splits differently (split {now}, listed {listed}): the source or the Python"
            " changed since --list; list the shards again with the same source and Python"
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
    out = [s for m in select_modules(mods) for s in shard_ids(m, module_shards(m))]
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
    by_module: dict[str, list[tuple[int, int, str | None]]] = {}
    for s in expected:
        module, k, n = parse_shard_id(s)
        by_module.setdefault(module, []).append((k, n, shard_split(s)))
    gaps = []
    for module, kn in sorted(by_module.items()):
        counts = {(n, h) for _, n, h in kn}
        ks = sorted(k for k, _, _ in kn)
        if len(counts) != 1 or ks != list(range(1, counts.pop()[0] + 1)):
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


# Which tests can reach a module: the static import graph ---------------------------------------------------

# The test files mutmut never runs: the --ignore entries of pytest_add_cli_args_test_selection in pyproject.toml,
# which mutmut_scope replaces with the selected files (a test checks the two match).
MUTMUT_SKIPPED_TESTS = ("tests/integration/", "tests/test_metamorphic.py", "tests/test_fuzz_harnesses.py")
# The project's top-level code directories tests can reach, by import or by loading a file by path.
LOCAL_DIRS = ("src/health_data_registry", "eval", "tools", "scripts", "fuzz", "transform", "tests")
DOTTED_NAME = re.compile(
    r"\b(?:health_data_registry|eval|tools|scripts|fuzz|transform|tests)(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
)
# A Python file named in a string (`ROOT / "scripts" / "crawl_doi.py"`, "tools/readme_svg/gen.py"), as a test
# loading it by path names it (importlib's spec_from_file_location, runpy.run_path, a subprocess).
PY_FILE_NAME = re.compile(r"(?:^|/)(?P<name>[A-Za-z_][A-Za-z0-9_]*\.py)$")


def _dotted(rel: Path) -> str:
    """src/health_data_registry/x.py -> health_data_registry.x; tests/conftest.py -> tests.conftest; a package's
    __init__.py is the package."""
    parts = list(rel.with_suffix("").parts)
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def local_modules(root: Path) -> dict[str, Path]:
    """Every Python module of the project that tests can import or load by path (LOCAL_DIRS)."""
    out: dict[str, Path] = {}
    for top in LOCAL_DIRS:
        for f in sorted((root / top).rglob("*.py")):
            rel = f.relative_to(root)
            if "sealed" not in rel.parts:
                out[_dotted(rel)] = rel
    return out


def imports_of(
    name: str, rel: Path, source: str, known: set[str], by_file_name: dict[str, set[str]] | None = None
) -> set[str]:
    """The project modules one file imports, statically: `import a.b`, `from a import b` (b a module or a name in
    a), relative imports, any dotted project module named in a string (monkeypatch targets, `-m` runs), and any
    project file named in a string (loaded by path; `by_file_name` maps a file name such as `gen.py` to the
    modules of that name, all of which count). Importing a.b.c imports the packages a and a.b too."""
    package = name if rel.name == "__init__.py" else name.rpartition(".")[0]
    found: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(known)  # unreadable: assume it reaches everything
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - node.level + 1]
                mod = ".".join([*base, node.module] if node.module else base)
            else:
                mod = node.module or ""
            found.add(mod)
            found.update(f"{mod}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and by_file_name:
            m = PY_FILE_NAME.search(node.value)
            if m and m["name"] != "__init__.py":
                found.update(by_file_name.get(m["name"], ()))
    found.update(DOTTED_NAME.findall(source))
    out: set[str] = set()
    for f in found:
        parts = f.split(".")
        out.update(".".join(parts[:i]) for i in range(1, len(parts) + 1) if ".".join(parts[:i]) in known)
    return out


def tests_reaching(modules: list[str], root: Path) -> list[str]:
    """The test files whose imports reach any of `modules`, directly or through other project modules (scripts/ and
    tools/ included, and files loaded by path) or a conftest.py above them, minus the files mutmut never runs. Leaving a test out can only let a mutant survive
    (a lower score), never count one as killed, so a narrow selection fails safe."""
    files = local_modules(root)
    known = set(files)
    by_file_name: dict[str, set[str]] = {}
    for n, rel in files.items():
        by_file_name.setdefault(rel.name, set()).add(n)
    deps = {
        n: imports_of(n, rel, (root / rel).read_text(encoding="utf-8"), known, by_file_name)
        for n, rel in files.items()
    }
    importers: dict[str, set[str]] = {}
    for n, ds in deps.items():
        for d in ds:
            importers.setdefault(d, set()).add(n)
    reached = set(modules)  # every module that imports one of `modules`, however indirectly
    todo = list(reached)
    while todo:
        for n in importers.get(todo.pop(), ()):
            if n not in reached:
                reached.add(n)
                todo.append(n)
    out = []
    for n, rel in files.items():
        posix = rel.as_posix()
        if not (rel.name.startswith("test_") and posix.startswith("tests/")):
            continue
        if any(posix.startswith(s) for s in MUTMUT_SKIPPED_TESTS):
            continue
        conftests = {c for c in known if c.endswith(".conftest") and n.startswith(c.rpartition(".")[0] + ".")}
        if reached & {n, *conftests}:
            out.append(posix)
    return sorted(out)


def _exit_on_signal(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


TEST_SELECTION_LINE = re.compile(r"^pytest_add_cli_args_test_selection = .*$", re.MULTILINE)


@contextlib.contextmanager
def mutmut_scope(paths: list[str], tests: list[str]) -> Iterator[None]:
    """While the block runs, mutmut generates mutants for `paths` only (its `only_mutate`), and its stats run (the
    test pass before any mutant) runs only `tests` (tests_reaching) instead of every test file; with no tests, the
    configured selection stays. Both go into pyproject.toml's [tool.mutmut], the only place mutmut reads them, and
    come out again afterwards, whatever happens. Generating every module's mutants and running every test took
    most of a shard's setup. The other files are copied unmutated; the mutant names run and the scoring do not
    depend on either setting, and a test left out can only lower the score."""
    pyproject = ROOT / "pyproject.toml"
    original = pyproject.read_text(encoding="utf-8")
    if "\nonly_mutate" in original:
        raise ValueError(
            "pyproject.toml already sets only_mutate, most likely left by an interrupted run. Restore the file"
            " (git checkout -- pyproject.toml) and run again; this script sets only_mutate for each run."
        )
    header = "[tool.mutmut]\n"
    if original.count(header) != 1 or len(TEST_SELECTION_LINE.findall(original)) != 1:
        raise ValueError(
            "pyproject.toml needs exactly one [tool.mutmut] table with exactly one"
            " pytest_add_cli_args_test_selection line; check for a duplicate or a missing one."
        )
    text = original.replace(header, header + f"only_mutate = {json.dumps(paths)}\n")
    if tests:
        line = f"pytest_add_cli_args_test_selection = {json.dumps(tests)}"
        text = TEST_SELECTION_LINE.sub(lambda _: line, text)
    # SIGTERM and SIGHUP (a stopped job, a closed terminal) end the run through SystemExit, so `finally` restores
    # the file; only SIGKILL skips it, and the only_mutate check above then refuses the next run.
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (getattr(signal, n) for n in ("SIGTERM", "SIGHUP") if hasattr(signal, n)):
            previous[sig] = signal.signal(sig, _exit_on_signal)
    pyproject.write_text(text, encoding="utf-8")
    try:
        yield
    finally:
        pyproject.write_text(original, encoding="utf-8")
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_mutmut(shards: list[str]) -> ShardResult:
    print("Mutating:", ", ".join(shards))
    globs = [g for s in shards for g in shard_globs(s)]
    if not globs:  # mutmut run with no names runs every mutant of the package: never do that
        raise ValueError(f"{', '.join(shards)}: no mutant names to run; refusing to run mutmut on everything")
    shutil.rmtree(ROOT / "mutants", ignore_errors=True)
    modules = sorted({parse_shard_id(s)[0] for s in shards})
    paths = [module_path(m).relative_to(ROOT).as_posix() for m in modules]
    tests = tests_reaching(modules, ROOT)
    names = ", ".join(modules)
    if tests:
        files = "1 test file" if len(tests) == 1 else f"{len(tests)} test files"
        print(f"Stats run on the {files} that import {names}, directly or through other modules.")
    else:
        print(f"No test file imports {names}; the stats run uses the configured test selection.")
    with mutmut_scope(paths, tests):
        run = subprocess.run(
            ["mutmut", "run", *globs], cwd=ROOT, check=False, stderr=subprocess.PIPE, text=True
        )
    sys.stderr.write(run.stderr or "")
    if run.returncode != 0 and NOTHING_MATCHES in (run.stderr or ""):
        # The stats run passed and no generated mutant matches the shard's names: nothing to mutate. The results
        # below still check that no mutant of the module is in no shard, or in several.
        print("No generated mutant matches this shard's names: nothing to mutate here.")
    elif run.returncode != 0:
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
    for module in sorted({parse_shard_id(s)[0] for s in shards if shard_split(s) is not None}):
        scoped = SCOPE.get(module)
        in_scope = [n for n in names if scoped is None or mutant_key(n, module) in scoped]
        result.unassigned += unassigned_mutants(in_scope, module, module_shards(module))
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
        return True, ["No changed functions to mutate."]
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
    ap.add_argument(
        "--base",
        help="compare with the merge base of this and HEAD (default origin/main for --list and the local run)."
        " With --module, scope the shard to the functions changed since then, as --list did; without --base,"
        " --module mutates the whole module.",
    )
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
        try:
            expected = select_shards(json.loads(a.expect))
        except (ValueError, TypeError) as e:  # json.JSONDecodeError is a ValueError
            ap.error(f"--expect: {e}")
        ok, lines = aggregate(load_reports(a.aggregate), a.floor, expected)
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
            if a.base:
                set_scope([module], a.base)
            result = run_mutmut([a.module])
        except ValueError as e:  # the split changed since --list, or no names: fail, never run other mutants
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

    base = a.base or "origin/main"
    try:
        mods = select_modules(
            [_module(str(p.relative_to(ROOT))) for pkg in PACKAGES for p in pkg.rglob("*.py")]
            if a.all
            else changed_modules(base)
        )
        if not a.all:
            set_scope(mods, base)  # only the functions the change touches (--all: every function)
        if a.list:
            print(list_json(mods))
            return 0
        shards = list_shards(mods)
    except ValueError as e:  # no merge base, a strange file name, too many shards: say so, never a traceback
        print(e, file=sys.stderr)
        return 1
    if not shards:
        print("No changed functions to mutate.")
        return 0
    result = run_mutmut(shards)
    ok, lines = aggregate([result], a.floor, shards)
    print("\n".join(lines))
    print("Inspect one with: uv run mutmut show <name>")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
