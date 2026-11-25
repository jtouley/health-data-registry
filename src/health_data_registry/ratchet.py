"""The Trusted gate's ratchet: fail a pull request that weakens the checks guarding main.

Usage (the Trusted gate workflow, .github/workflows/trusted-gate.yml; locally: `make ratchet`):
  python -m health_data_registry.ratchet --base MAIN_SHA --head PR_SHA [--required-checks FILE] [--github]

The workflow runs main's own copy of this module (pull_request_target), never the pull request's. The pull
request's commit is fetched as git objects and read as text: nothing in it is imported, executed or installed.
Python is parsed with ast, workflows with yaml.safe_load, TOML with tomllib. Each rule compares the pull
request's head with its merge base (git merge-base BASE HEAD) and fails when the change:

1. thresholds: lowers a coverage or mutation floor, or any other floor, fail-under or threshold in pyproject.toml,
   the Makefile, scripts/, the workflows or another tool config, or removes one; or sets the coverage floor below
   85 or the mutation floor below 70 anywhere in those files;
2. escape hatches: adds a skip, skipif or xfail mark, a test deselection or a pytest -m/-k filter, a no-cover or
   no-mutate pragma, a mutmut exclusion, a type-ignore or noqa comment, an inline mypy setting, or a ruff, mypy,
   coverage or pytest relaxation, unless the exact line is in the allowlist (ALLOWLIST, read from main); or adds a
   tool settings file beside pyproject.toml (mypy.ini, setup.cfg, a nested ruff.toml, ...), or a file the gate
   cannot read as text (a symbolic link, a submodule, a config file holding a NUL byte), which no entry allows;
3. tests: leaves fewer tests in the test modules it touches than the merge base had (deleted or renamed tests
   with no replacement), unless each removed test is in the allowlist;
4. required checks: removes or renames a check the ruleset requires, makes a required job (or a job it needs, old
   or new) conditional, stops a required job waiting for a job it needed, adds continue-on-error to one, or adds a
   job whose check name is an expression the gate cannot resolve (it could report any required check);
5. sealed sets: touches eval/sealed/;
6. hooks: adds a way around the git hooks (no-verify, the n flag of git commit, a hooks-path override, pre-commit's
   skip and allow-no-config variables), or removes a hook from .pre-commit-config.yaml;
7. allowlist and gate: changes the allowlist together with anything else, or changes the gate's own files
   together with anything but files under docs/ and CHANGELOG.md;
8. CI budgets: loosens ci/budgets.yml: raises a time budget, adds a key (a new budget is a new allowance), removes
   one, or deletes the file;
9. mutation: weakens a job that runs mutation tests, in any workflow (the PR mutation shards, the nightly run, the
   release gate's mutation check): removes it, makes it conditional, adds continue-on-error, drops a mutation
   command from its steps, takes a schedule away from its workflow, or stops a job that waited for it (a release
   job, an aggregate) waiting for it. Each may be allowlisted except continue-on-error.

It prints one line per failure, naming the file and line, and exits 1; 0 when nothing weakens; 2 when it could not
check (a git error), so the gate fails closed.
"""

from __future__ import annotations

import argparse
import ast
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

ALLOWLIST = ".github/ratchet-allowlist.txt"
# The files main's gate runs (python -m health_data_registry.ratchet imports the package's __init__ first).
GATE_FILES = frozenset(
    {
        ".github/workflows/trusted-gate.yml",
        "src/health_data_registry/__init__.py",
        "src/health_data_registry/ratchet.py",
        "tests/test_ratchet.py",
    }
)
SEALED = "eval/sealed/"
WORKFLOWS = ".github/workflows/"
# The coverage and mutation floors in CLAUDE.md; no file may set them lower, whatever the merge base said.
MINIMUM_COVERAGE = 85.0
MINIMUM_MUTATION = 70.0
# Larger files are not parsed (ast, YAML, TOML), and a scoped config that large is a finding of its own.
MAX_PARSE_BYTES = 2_000_000
LINE_TEXT_LIMIT = 200

Reader = Callable[[str], str | None]

# What to do about a finding no allowlist entry can clear, by rule.
NEXT_STEPS = {
    "threshold": "Restore it. Only the maintainer can lower a floor, by changing the gate in a pull request of its"
    " own.",
    "sealed sets": "Revert the change under eval/sealed/.",
    "budgets": "Restore it; budgets only tighten. Only the maintainer can loosen one, by changing the gate.",
    "allowlist": "Move the allowlist change to a pull request of its own, merge that first, then this one.",
    "gate files": "Move the gate change to a pull request of its own; only files under docs/ and CHANGELOG.md may"
    " come with it.",
    "required check": "Restore it. If the required checks really change, the maintainer changes the ruleset first.",
    "hooks": "Remove it: every commit goes through the hooks.",
    "mutation": "Remove it: a mutation job never ignores its own failure.",
    "links": "Commit the file itself, not a link to it.",
    "escape hatch": "Keep tool settings in pyproject.toml, as text the gate can read.",
    "tests": "Fix the test module so it parses, or split it.",
}


@dataclass(frozen=True)
class Finding:
    """One way the change weakens a check. text is the stripped line an allowlist entry must repeat."""

    rule: str
    path: str
    line: int
    message: str
    text: str = ""
    allowlistable: bool = False
    next_step: str = ""

    def render(self) -> str:
        """The failure line, then the way out. Every line starts with fixed text, never with a path or line from
        the pull request, so no printed line can read as a workflow command."""
        where = f"{clean(self.path)}:{self.line}" if self.line else clean(self.path)
        out = f"FAIL [{self.rule}] {where}: {clean(self.message)}"
        if self.allowlistable:
            out += (
                f"\n    If it is needed, add the entry below to {ALLOWLIST} in a pull request that changes nothing"
                " else, merge that first, then run this check again (push, or edit the pull request's title):"
                f"\n    Allowlist entry: {clean(self.path)} :: {clean(self.text)}"
            )
        else:
            out += f"\n    Next step: {self.next_step or NEXT_STEPS.get(self.rule, 'Restore it.')}"
        return out

    def annotation(self) -> str:
        line = f",line={self.line}" if self.line else ""
        return f"::error file={_prop(self.path)}{line},title=Trusted gate::{_data(self.message)}"


@dataclass(frozen=True)
class Change:
    """One changed path between the merge base and the head; old is None when added, new is None when deleted."""

    old: str | None
    new: str | None


@dataclass
class Snapshot:
    """The files of one commit, read on demand. paths lists every file (needed for the workflows); modes holds
    git's mode for the paths that are not plain files (120000 a symbolic link, 160000 a submodule)."""

    read: Reader
    paths: list[str] = field(default_factory=list)
    modes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, files: Mapping[str, str], modes: Mapping[str, str] | None = None) -> Snapshot:
        return cls(read=files.get, paths=sorted(files), modes=dict(modes or {}))


def clean(text: str) -> str:
    """Text from the pull request, safe to print: no control characters (so no forged workflow command)."""
    out = "".join(ch if ch >= " " and ch != "\x7f" else "?" for ch in text)
    return out if len(out) <= LINE_TEXT_LIMIT else out[: LINE_TEXT_LIMIT - 3] + "..."


def _data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _prop(text: str) -> str:
    return _data(text).replace(":", "%3A").replace(",", "%2C")


def changes_between(base: Mapping[str, str], head: Mapping[str, str]) -> list[Change]:
    """The changes between two file maps, without rename detection (git's diff supplies renames)."""
    paths = sorted(set(base) | set(head))
    return [
        Change(p if p in base else None, p if p in head else None)
        for p in paths
        if base.get(p) != head.get(p)
    ]


def _lines(text: str | None) -> list[str]:
    return [] if text is None else text.splitlines()


def _locate(text: str | None, needle: str) -> tuple[int, str]:
    """The first line containing needle, as (1-based number, stripped text); (0, '') if none. A needle of
    NUL-separated parts means the first line holding every part, else the first holding the first part."""
    parts = needle.split("\0")
    lines = _lines(text)
    for wanted in (parts, parts[:1]):
        for i, line in enumerate(lines, 1):
            if all(part in line for part in wanted):
                return i, line.strip()
    return 0, ""


_TOML_HEADER = re.compile(r"\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")
_BARE_KEY = re.compile(r"[\w-]+")


def _locate_toml(text: str | None, section: str, token: str) -> tuple[int, str]:
    """Like _locate, but only within the TOML tables under section ('' for the whole file). A bare key name as
    token matches only the line that sets that key, so a removed key is not found (0, '') rather than matched on
    some other line that happens to contain the word."""
    kept: list[str] = []
    numbers: list[int] = []
    current = ""
    for i, line in enumerate(_lines(text), 1):
        m = _TOML_HEADER.match(line)
        if m:
            current = m[1].replace('"', "").replace("'", "")
            continue
        if not section or current == section or current.startswith(section + "."):
            kept.append(line)
            numbers.append(i)
    if _BARE_KEY.fullmatch(token):
        key = re.compile(rf"\s*(?:[\w.\"'-]+\.)?[\"']?{re.escape(token)}[\"']?\s*=")
        for number, line in zip(numbers, kept, strict=True):
            if key.match(line):
                return number, line.strip()
        return 0, ""
    found, text_found = _locate("\n".join(kept), token)
    return (numbers[found - 1], text_found) if found else (0, "")


def _suffix_or_name(path: str) -> tuple[str, str]:
    p = PurePosixPath(path)
    return p.suffix, p.name


# Allowlist ---------------------------------------------------------------------------------------------------


def parse_allowlist(text: str | None) -> frozenset[tuple[str, str]]:
    """`path :: exact stripped line` entries; blank lines and lines starting with # are ignored."""
    entries = set()
    for raw in _lines(text):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path, sep, rest = line.partition(" :: ")
        if sep:
            entries.add((path.strip(), rest.strip()))
    return frozenset(entries)


# Rule 5 and 7: sealed sets, allowlist and gate isolation -----------------------------------------------------


def plural(n: int, word: str) -> str:
    """'1 file', '2 files'."""
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _paths(changes: Iterable[Change]) -> set[str]:
    return {p for c in changes for p in (c.old, c.new) if p}


def check_sealed(changes: list[Change]) -> list[Finding]:
    return [
        Finding(
            "sealed sets", p, 0, "changes the sealed blind sets in eval/sealed/; no pull request changes them"
        )
        for p in sorted(_paths(changes))
        if p.startswith(SEALED)
    ]


def check_isolation(changes: list[Change]) -> list[Finding]:
    paths = _paths(changes)
    out = []
    others = sorted(paths - {ALLOWLIST})
    if ALLOWLIST in paths and others:
        out.append(
            Finding(
                "allowlist", ALLOWLIST, 0,
                f"the allowlist changes only in a pull request that changes nothing else; this one also changes "
                f"{plural(len(others), 'other file')}: {', '.join(others[:5])}",
            )
        )  # fmt: skip
    gate = sorted(paths & GATE_FILES)
    rest = sorted(
        p for p in paths - GATE_FILES - {ALLOWLIST} if not (p.startswith("docs/") or p == "CHANGELOG.md")
    )
    if gate and rest:
        out.append(
            Finding(
                "gate files", gate[0], 0,
                f"the gate's own files ({', '.join(gate)}) change in a pull request of their own, with nothing but "
                f"files under docs/ and CHANGELOG.md; this one also changes {plural(len(rest), 'other file')}: "
                f"{', '.join(rest[:5])}",
            )
        )  # fmt: skip
    return out


# Rule 2: links the gate would read as their target's name -------------------------------------------------------

LINK_MODES = {"120000": "a symbolic link", "160000": "a submodule"}


def check_links(changes: list[Change], base: Snapshot, head: Snapshot) -> list[Finding]:
    """A new symbolic link or submodule. git hands the gate a link's target name, not the file Python, make or
    pytest would read through it, so every rule would read nothing there; a submodule is not read at all."""
    out = []
    for change in changes:
        if change.new is None:
            continue
        mode = head.modes.get(change.new)
        if mode in LINK_MODES and (change.old is None or base.modes.get(change.old) != mode):
            out.append(
                Finding(
                    "links", change.new, 0,
                    f"adds {LINK_MODES[mode]}, which the gate cannot read through, so no rule would check what it"
                    " points to",
                )
            )  # fmt: skip
    return out


# Rules 2 and 6: escape hatches added line by line ------------------------------------------------------------


@dataclass(frozen=True)
class Marker:
    """A line pattern that turns a check off. scope holds file suffixes or exact file names; empty means any."""

    rule: str
    what: str
    pattern: re.Pattern[str]
    scope: frozenset[str] = frozenset()
    allowlistable: bool = True

    def applies(self, path: str) -> bool:
        suffix, name = _suffix_or_name(path)
        return not self.scope or suffix in self.scope or name in self.scope


PY = frozenset({".py", ".pyi"})
CONFIG = frozenset(
    {
        ".py",
        ".toml",
        ".cfg",
        ".ini",
        ".yml",
        ".yaml",
        ".sh",
        "Makefile",
        ".coveragerc",
        ".pre-commit-config.yaml",
    }
)
SHELL = frozenset({".yml", ".yaml", ".sh", "Makefile", ".toml", ".cfg", ".ini"})
DOCS = frozenset({".md", ".rst", ".txt"})

MARKERS: tuple[Marker, ...] = (
    Marker(
        "escape hatch", "a skip, skipif or xfail mark",
        re.compile(
            r"\b(?:pytest\.)?mark\.(?:skip|skipif|xfail)\b|\bpytest\.(?:skip|xfail|importorskip)\s*\("
            r"|\bunittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b|\.skipTest\s*\("
        ),
        PY,
    ),
    Marker(
        "escape hatch", "a test deselection",
        # Spelled so this module's own source does not match it.
        re.compile(r"--de(?:select)\b|--ig(?:nore)(?:-glob)?\b|\bcollect_ig(?:nore)(?:_glob)?\b"),
        CONFIG,
    ),
    Marker(
        "escape hatch", "a no-cover pragma",
        re.compile(r"#\s*(?:pragma|PRAGMA)[:\s]?\s*(?:no|NO)\s*(?:cover|COVER)"),
        PY,
    ),
    Marker("escape hatch", "a no-mutate pragma", re.compile(r"#\s*pragma\s*:.*\bno\s*mutate"), PY),
    Marker("escape hatch", "a type-ignore comment", re.compile(r"#\s*type\s*:\s*ignore\b"), PY),
    Marker(
        "escape hatch", "a noqa comment", re.compile(r"#\s*(?:(?:ruff|flake8)\s*:\s*)?noqa\b", re.IGNORECASE), PY
    ),
    Marker("escape hatch", "an inline mypy setting", re.compile(r"#\s*mypy\s*:"), PY),
    Marker(
        "escape hatch", "a ruff, mypy or test-runner relaxation on a command line",
        re.compile(
            r"--(?:extend-ignore|disable-error-code|no-strict[\w-]*|allow-untyped-[\w-]+|ignore-errors|exit-zero"
            r"|follow-imports[= ](?:skip|silent))\b"
        ),
        SHELL,
    ),
    Marker("escape hatch", "a command whose failure is ignored", re.compile(r"\|\|\s*(?:true|:)(?:\s|$|;)"), SHELL),
    # A changed -m or -k expression on a pytest command line changes which tests run.
    Marker(
        "escape hatch", "a pytest -m or -k filter",
        re.compile(r"\bpytest\b[^\n|;&]*\s-[mk](?=\s|=|[\"']|$)"),
        SHELL,
    ),
    Marker(
        "hooks",
        "a way around the git hooks (no-verify, the n flag of git commit, a hooks-path override, or pre-commit's"
        " skip and allow-no-config variables)",
        # git commit's own flags only: the pattern stops at a pipe, ; or &, so `git log ... | head -n 1` is not it.
        re.compile(
            r"--no-ver(?:ify)\b|\bgit\b(?:\s+[^\s|;&]+)*?\s+commit(?=\s|$)[^|;&\n]*?\s-[a-zA-Z]*n[a-zA-Z]*\b"
            r"|\bcore\.hooksPath\b|\bSK(?:IP)=|\bPRE_COMMIT_ALLOW_NO_CONF(?:IG)\b"
        ),
        allowlistable=False,
    ),
)  # fmt: skip
# The same hook bypass mentioned in prose (a doc saying never to use it) may be allowlisted.
DOC_HOOKS = Marker("hooks", MARKERS[-1].what, MARKERS[-1].pattern, DOCS, allowlistable=True)


def _marker_for(marker: Marker, path: str) -> Marker | None:
    if marker is MARKERS[-1] and DOC_HOOKS.applies(path):
        return DOC_HOOKS
    return marker if marker.applies(path) else None


def _is_text(text: str | None) -> bool:
    return text is not None and "\0" not in text


# The files some marker reads (the hooks marker reads every file, binaries included, so it is left out here).
TEXT_SCOPE = PY | CONFIG | SHELL | DOCS


def _in_text_scope(path: str) -> bool:
    suffix, name = _suffix_or_name(path)
    return suffix in TEXT_SCOPE or name in TEXT_SCOPE


def check_markers(
    changes: list[Change], base: Snapshot, head: Snapshot, allow: frozenset[tuple[str, str]]
) -> list[Finding]:
    """Each matching line the head has more often than the merge base (the same file, or its old name)."""
    out = []
    for change in changes:
        if change.new is None or change.new == ALLOWLIST:
            continue
        new_text = head.read(change.new)
        if new_text is None:
            continue
        old_text = base.read(change.old) if change.old else None
        if not _is_text(new_text):
            # make, sh and configparser read past a NUL byte; the markers would not. A config or script that
            # newly holds one fails rather than going unread. Other binaries (images) are not read.
            if _in_text_scope(change.new) and (old_text is None or _is_text(old_text)):
                out.append(
                    Finding(
                        "escape hatch", change.new, 0,
                        "holds a NUL byte, so the gate cannot read it as text, and make, the shell or a config"
                        " reader may still run it",
                        next_step="Remove the NUL byte; scripts and settings are plain text.",
                    )
                )  # fmt: skip
            continue
        for marker in MARKERS:
            applied = _marker_for(marker, change.new)
            if applied is None:
                continue
            before = Counter(s.strip() for s in _lines(old_text) if applied.pattern.search(s))
            for number, line in enumerate(_lines(new_text), 1):
                if not applied.pattern.search(line):
                    continue
                text = line.strip()
                if before[text] > 0:
                    before[text] -= 1
                    continue
                if applied.allowlistable and (change.new, text) in allow:
                    continue
                out.append(
                    Finding(
                        applied.rule, change.new, number, f"adds {applied.what}: {text}", text,
                        applied.allowlistable,
                    )
                )  # fmt: skip
    return out


# Rule 1: thresholds ------------------------------------------------------------------------------------------

THRESHOLD_FILES = frozenset(
    {
        "Makefile", "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", ".coveragerc", "mypy.ini",
        "ruff.toml", ".ruff.toml", ".pre-commit-config.yaml",
    }
)  # fmt: skip
THRESHOLD_DIRS = ("scripts/", WORKFLOWS, ".github/actions/")
_NUMBER = r"(?P<value>\d+(?:\.\d+)?)"
THRESHOLD_PATTERNS = (
    # COV_FLOOR ?= 85, fail_under = 85, MUTATION_FLOOR: "70"
    re.compile(
        r"(?P<name>\b(?:[A-Za-z_][\w-]*?)?(?:floor|fail[_-]?under|threshold)\w*)\b\s*(?:\?=|:=|\+=|=|:)\s*[\"']?"
        + _NUMBER,
        re.IGNORECASE,
    ),
    # --cov-fail-under=85, --floor 70
    re.compile(r"--(?P<name>cov-fail-under|fail-under|floor|[\w-]*threshold[\w-]*)(?:=|\s+)[\"']?" + _NUMBER),
    # ${MUTATION_FLOOR:-70}
    re.compile(r"\$\{(?P<name>\w*(?:floor|fail_under|threshold)\w*):?-" + _NUMBER + r"\}", re.IGNORECASE),
    # add_argument("--floor", type=float, default=70.0)
    re.compile(
        r"add_argument\(\s*[\"']--(?P<name>[\w-]*(?:floor|fail-under|threshold)[\w-]*)[\"'][^)]*?\bdefault\s*=\s*"
        + _NUMBER
    ),
)


def in_threshold_scope(path: str) -> bool:
    return path in THRESHOLD_FILES or path.startswith(THRESHOLD_DIRS)


def _norm(name: str) -> str:
    return name.lower().replace("-", "_")


def thresholds(text: str | None) -> dict[str, list[tuple[float, int, str]]]:
    """Every threshold the text sets: normalised name -> [(value, line number, stripped line)]."""
    found: dict[str, list[tuple[float, int, str]]] = {}
    for number, line in enumerate(_lines(text), 1):
        for pattern in THRESHOLD_PATTERNS:
            for m in pattern.finditer(line):
                found.setdefault(_norm(m["name"]), []).append((float(m["value"]), number, line.strip()))
    return found


def minimum_for(name: str) -> float | None:
    """The absolute floor a threshold's name stands for: coverage 85, mutation 70, others none. Whole words of the
    name decide, so discovery_threshold or recovery_floor are not coverage floors."""
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]
    if any(w.startswith(("mutation", "mutmut")) for w in words) or words == ["floor"]:
        return MINIMUM_MUTATION
    if any(w in ("cov", "coverage") for w in words) or "fail_under" in name.lower():
        return MINIMUM_COVERAGE
    return None


def _fmt(value: float) -> str:
    return f"{value:g}"


def check_thresholds(changes: list[Change], base: Snapshot, head: Snapshot) -> list[Finding]:
    out = []
    for change in changes:
        old_scoped = change.old is not None and in_threshold_scope(change.old)
        new_scoped = change.new is not None and in_threshold_scope(change.new)
        if not (old_scoped or new_scoped):
            continue
        before = thresholds(base.read(change.old)) if change.old else {}
        after = thresholds(head.read(change.new)) if change.new else {}
        for name, entries in after.items():
            floor = minimum_for(name)
            for value, number, text in entries:
                if floor is not None and value < floor and change.new:
                    out.append(
                        Finding(
                            "threshold", change.new, number,
                            f"sets {name} to {_fmt(value)}, below the project's floor of {_fmt(floor)}: {text}",
                        )
                    )  # fmt: skip
        for name, entries in before.items():
            was = min(v for v, _, _ in entries)
            where = change.new or change.old or ""
            if name not in after:
                _, number, text = entries[0]
                out.append(
                    Finding(
                        "threshold", where, 0 if change.new else number,
                        f"removes the threshold {name} (was {_fmt(was)} at line {number}: {text})",
                    )
                )  # fmt: skip
                continue
            now, number, text = min(after[name])
            if now < was:
                out.append(
                    Finding(
                        "threshold", where, number, f"lowers {name} from {_fmt(was)} to {_fmt(now)}: {text}"
                    )
                )
    return out


# Rule 2: ruff, mypy, coverage and mutmut relaxations in TOML -------------------------------------------------

TOML_CONFIGS = frozenset({"pyproject.toml", "ruff.toml", ".ruff.toml"})
MYPY_STRICTNESS_PREFIXES = (
    "disallow_",
    "warn_",
    "strict",
    "check_untyped_defs",
    "no_implicit_",
    "extra_checks",
)
MYPY_RELAXING = frozenset(
    {
        "ignore_errors", "allow_untyped_globals", "allow_redefinition", "ignore_missing_imports",
        "implicit_reexport", "implicit_optional", "allow_untyped_calls", "allow_untyped_defs",
        "allow_incomplete_defs", "allow_any_generics", "allow_subclassing_any", "allow_untyped_decorators",
    }
)  # fmt: skip


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _table(data: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    node: Any = data
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [json.dumps(v, sort_keys=True) if not isinstance(v, str) else v for v in value]
    return []


def _pairs(value: Any) -> set[str]:
    """per-file-ignores {glob: [codes]} as 'glob: code' strings."""
    if not isinstance(value, dict):
        return set()
    return {f"{k}: {code}" for k, codes in value.items() for code in _items(codes)}


def _parse_toml(text: str | None) -> dict[str, Any] | str:
    """The parsed TOML, or the reason it could not be read."""
    if text is None:
        return {}
    if len(text) > MAX_PARSE_BYTES:
        return "too large to check"
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return str(exc)


def _ruff(data: Mapping[str, Any], path: str) -> dict[str, Any]:
    top = _table(data, "tool", "ruff") if path == "pyproject.toml" else dict(data)
    return {**top, **_table(top, "lint")}


def _gained(before: Iterable[str], after: Iterable[str]) -> list[str]:
    seen = set(before)
    return sorted({a for a in after if a not in seen})


def _ruff_relaxations(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(description, a token to find the line by) for each ruff relaxation."""
    out = []
    for key in ("select", "extend-select"):
        for code in _gained(_items(after.get(key)), _items(before.get(key))):
            out.append((f"ruff {key} no longer has {code}", key))
    for key in ("ignore", "extend-ignore", "exclude", "extend-exclude"):
        for code in _gained(_items(before.get(key)), _items(after.get(key))):
            out.append((f"ruff {key} gains {code}", f'"{code}"'))
    for key in ("per-file-ignores", "extend-per-file-ignores"):
        for pair in _gained(_pairs(before.get(key)), _pairs(after.get(key))):
            glob, code = pair.rsplit(": ", 1)
            out.append((f"ruff {key} gains {pair}", f'{glob}\0"{code}"'))
    return out


def _strictness(key: str) -> bool:
    return key.startswith(MYPY_STRICTNESS_PREFIXES)


def _mypy_section(before: Mapping[str, Any], after: Mapping[str, Any], where: str) -> list[tuple[str, str]]:
    out = []
    for key in sorted(set(before) | set(after)):
        was, now = before.get(key), after.get(key)
        if _strictness(key) and now is False and was is not False:
            out.append((f"mypy {where}{key} turned off", key))
        elif _strictness(key) and was is True and now is None:
            out.append((f"mypy {where}{key} removed", key))
        elif key in MYPY_RELAXING and now is True and was is not True:
            out.append((f"mypy {where}{key} turned on", key))
        elif key == "follow_imports" and now in ("skip", "silent") and now != was:
            out.append((f"mypy {where}follow_imports = {now}", key))
    for code in _gained(_items(before.get("disable_error_code")), _items(after.get("disable_error_code"))):
        out.append((f"mypy {where}disable_error_code gains {code}", f'"{code}"'))
    for code in _gained(_items(after.get("enable_error_code")), _items(before.get("enable_error_code"))):
        out.append((f"mypy {where}enable_error_code no longer has {code}", "enable_error_code"))
    for path in _gained(_items(after.get("files")), _items(before.get("files"))):
        out.append((f"mypy {where}files no longer has {path}", "files"))
    for path in _gained(_items(before.get("exclude")), _items(after.get("exclude"))):
        out.append((f"mypy {where}exclude gains {path}", f'"{path}"'))
    return out


def _mypy_relaxations(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[tuple[str, str]]:
    out = _mypy_section(
        {k: v for k, v in before.items() if k != "overrides"},
        {k: v for k, v in after.items() if k != "overrides"},
        "",
    )
    old = {json.dumps(o, sort_keys=True) for o in _list(before.get("overrides")) if isinstance(o, dict)}
    for override in _list(after.get("overrides")):
        if isinstance(override, dict) and json.dumps(override, sort_keys=True) not in old:
            modules = ", ".join(_items(override.get("module"))) or "?"
            body = {k: v for k, v in override.items() if k != "module"}
            out += _mypy_section({}, body, f"override for {modules}: ")
    return out


def _coverage_relaxations(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[tuple[str, str]]:
    out = []
    for section, key in (
        ("run", "omit"),
        ("report", "omit"),
        ("report", "exclude_lines"),
        ("report", "exclude_also"),
    ):
        for item in _gained(
            _items(_table(before, section).get(key)), _items(_table(after, section).get(key))
        ):
            out.append((f"coverage {section}.{key} gains {item}", f'"{item}"'))
    for section, key in (("run", "include"), ("report", "include"), ("report", "partial_branches")):
        for item in _gained(
            _items(_table(before, section).get(key)), _items(_table(after, section).get(key))
        ):
            out.append((f"coverage {section}.{key} gains {item}", f'"{item}"'))
    for item in _gained(
        _items(_table(after, "run").get("source")), _items(_table(before, "run").get("source"))
    ):
        out.append((f"coverage run.source no longer has {item}", "source"))
    if _table(before, "run").get("branch") is True and _table(after, "run").get("branch") is not True:
        out.append(("coverage run.branch turned off", "branch"))
    return out


# Flags in pytest's addopts that narrow what is collected or run, or let collection errors pass (spelled in
# pieces, so this module trips none of its own markers).
PYTEST_NARROWING = (
    "-k", "-m", "--ig" + "nore", "--de" + "select", "-p", "--continue-on-collection-errors", "--lf", "--last-failed",
    "--sw", "--stepwise", "--co", "--collect-only",
)  # fmt: skip
PYTEST_TAKES_VALUE = frozenset({"-k", "-m", "--ig" + "nore", "--ig" + "nore-glob", "--de" + "select", "-p"})


def _pytest_filters(addopts: Any) -> list[str]:
    """The narrowing options in addopts, each with its value ('-m not slow')."""
    if isinstance(addopts, str):
        try:
            tokens = shlex.split(addopts)
        except ValueError:
            tokens = addopts.split()
    else:
        tokens = _items(addopts)
    out = []
    for i, token in enumerate(tokens):
        if not token.startswith(PYTEST_NARROWING):
            continue
        if token in PYTEST_TAKES_VALUE and i + 1 < len(tokens):
            out.append(f"{token} {tokens[i + 1]}")
        else:
            out.append(token)
    return out


def _pytest_relaxations(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[tuple[str, str]]:
    """[tool.pytest.ini_options]: fewer test paths, other file or function patterns, more skipped directories,
    or a narrowing option in addopts."""
    b, a = _table(before, "ini_options"), _table(after, "ini_options")
    out = []
    for item in _gained(_items(a.get("testpaths")), _items(b.get("testpaths"))):
        out.append((f"pytest testpaths no longer has {item}", "testpaths"))
    for key in ("python_files", "python_classes", "python_functions"):
        if a.get(key) != b.get(key):
            out.append((f"pytest {key} changes, which changes what is collected", key))
    for item in _gained(_items(b.get("norecursedirs")), _items(a.get("norecursedirs"))):
        out.append((f"pytest norecursedirs gains {item}", f'"{item}"'))
    for item in _gained(_pytest_filters(b.get("addopts")), _pytest_filters(a.get("addopts"))):
        out.append((f"pytest addopts gains {item}", "addopts"))
    return out


def _mutmut_relaxations(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[tuple[str, str]]:
    out = []
    for item in _gained(_items(after.get("source_paths")), _items(before.get("source_paths"))):
        out.append((f"mutmut source_paths no longer has {item}", "source_paths"))
    for key in ("do_not_mutate", "pytest_add_cli_args", "pytest_add_cli_args_test_selection"):
        for item in _gained(_items(before.get(key)), _items(after.get(key))):
            out.append((f"mutmut {key} gains {item} (a mutmut exclusion)", f'"{item}"'))
    return out


def check_toml(
    changes: list[Change], base: Snapshot, head: Snapshot, allow: frozenset[tuple[str, str]]
) -> list[Finding]:
    out = []
    for change in changes:
        if change.new not in TOML_CONFIGS:
            continue
        head_text = head.read(change.new)
        before = _parse_toml(base.read(change.old) if change.old else None)
        after = _parse_toml(head_text)
        if isinstance(after, str):
            out.append(Finding("escape hatch", change.new, 0, f"could not read it to check it ({after})"))
            continue
        if isinstance(before, str):
            before = {}
        pyproject = change.new == "pyproject.toml"
        found = [
            ("tool.ruff" if pyproject else "", what, token)
            for what, token in _ruff_relaxations(_ruff(before, change.new), _ruff(after, change.new))
        ]
        if pyproject:
            tool_b, tool_a = _table(before, "tool"), _table(after, "tool")
            for tool, relaxations in (
                ("mypy", _mypy_relaxations), ("coverage", _coverage_relaxations),
                ("mutmut", _mutmut_relaxations), ("pytest", _pytest_relaxations),
            ):  # fmt: skip
                found += [
                    (f"tool.{tool}", what, token)
                    for what, token in relaxations(_table(tool_b, tool), _table(tool_a, tool))
                ]
        for section, what, token in found:
            number, text = _locate_toml(head_text, section, token)
            if text and (change.new, text) in allow:
                continue
            out.append(
                Finding(
                    "escape hatch", change.new, number, f"relaxes a check: {what}", text, bool(text),
                    "" if text else "Restore the setting: a setting that is gone has no line to allowlist.",
                )
            )  # fmt: skip
    return out


# Settings files that ruff, mypy, pytest or coverage read before pyproject.toml, or instead of it for the files
# under them (pytest looks upwards from the tests it is given). The gate checks settings only in pyproject.toml.
SIDE_CONFIGS = frozenset(
    {"mypy.ini", ".mypy.ini", "setup.cfg", "pytest.ini", ".pytest.ini", "tox.ini", ".coveragerc", "ruff.toml",
     ".ruff.toml", "pyproject.toml"}
)  # fmt: skip
ROOT_TOML = frozenset({"pyproject.toml", "ruff.toml", ".ruff.toml"})


def check_side_configs(changes: list[Change]) -> list[Finding]:
    """A new or changed settings file beside pyproject.toml. The root pyproject.toml, and a root ruff.toml that is
    already there, are compared key by key (check_toml); any other one could switch a check off unseen."""
    out = []
    for change in changes:
        path = change.new
        if path is None or PurePosixPath(path).name not in SIDE_CONFIGS:
            continue
        if path in ROOT_TOML and (path == "pyproject.toml" or change.old == path):
            continue
        out.append(
            Finding(
                "escape hatch", path, 0,
                "adds or changes a settings file that ruff, mypy, pytest or coverage reads before pyproject.toml"
                " (or instead of it, below this folder); the gate checks settings only in pyproject.toml",
            )
        )  # fmt: skip
    return out


# Rule 3: test count ------------------------------------------------------------------------------------------


def is_test_module(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        path.startswith("tests/")
        and name.endswith(".py")
        and (name.startswith("test_") or name.endswith("_test.py"))
    )


def _parametrize_count(decorators: list[ast.expr], consts: Mapping[str, int] | None = None) -> int:
    """The cases a stack of parametrize marks makes: literal lists, or module-level names bound to one."""
    count = 1
    for dec in decorators:
        if not (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "parametrize"
        ):
            continue
        values = (
            dec.args[1]
            if len(dec.args) > 1
            else next((k.value for k in dec.keywords if k.arg == "argvalues"), None)
        )
        if isinstance(values, (ast.List, ast.Tuple, ast.Set)):
            count *= max(len(values.elts), 1)
        elif isinstance(values, ast.Name) and consts and values.id in consts:
            count *= max(consts[values.id], 1)
    return count


def _literal_lengths(tree: ast.Module) -> dict[str, int]:
    """Module-level NAME = [literal list, tuple or set] bindings, by name; the last binding wins."""
    out: dict[str, int] = {}
    for node in tree.body:
        target, value = None, None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name):
            if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                out[target.id] = len(value.elts)
            else:
                out.pop(target.id, None)
    return out


def _is_test_class(node: ast.ClassDef) -> bool:
    bases = {getattr(b, "id", None) or getattr(b, "attr", None) for b in node.bases}
    return node.name.startswith("Test") or "TestCase" in bases


def collect_tests(source: str) -> dict[str, tuple[int, int]]:
    """Test id -> (number of collected cases, line): test functions, test methods of Test classes, parametrized.

    Raises SyntaxError (or ValueError) when the source does not parse."""
    tree = ast.parse(source)
    consts = _literal_lengths(tree)
    found: dict[str, tuple[int, int]] = {}

    def visit(body: list[ast.stmt], prefix: str, multiplier: int) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                found[prefix + node.name] = (
                    multiplier * _parametrize_count(node.decorator_list, consts),
                    node.lineno,
                )
            elif isinstance(node, ast.ClassDef) and _is_test_class(node):
                visit(
                    node.body,
                    f"{prefix}{node.name}::",
                    multiplier * _parametrize_count(node.decorator_list, consts),
                )

    visit(tree.body, "", 1)
    return found


def _tests_in(text: str | None, path: str, side: str) -> dict[str, tuple[int, int]] | Finding:
    if text is None:
        return {}
    if len(text) > MAX_PARSE_BYTES:
        return Finding("tests", path, 0, f"too large to count its tests ({side})")
    try:
        return collect_tests(text)
    except (SyntaxError, ValueError) as exc:
        return Finding(
            "tests", path, getattr(exc, "lineno", 0) or 0, f"could not count its tests ({side}): {exc}"
        )


def check_tests(
    changes: list[Change], base: Snapshot, head: Snapshot, allow: frozenset[tuple[str, str]]
) -> list[Finding]:
    """A net drop in collected tests over the touched test modules, net of moves between them."""
    out: list[Finding] = []
    before_total = after_total = allowed = 0
    removed: list[tuple[str, str, int]] = []
    for change in changes:
        old = change.old if change.old and is_test_module(change.old) else None
        new = change.new if change.new and is_test_module(change.new) else None
        if not (old or new):
            continue
        before = _tests_in(base.read(old), old, "merge base") if old else {}
        after = _tests_in(head.read(new), new, "head") if new else {}
        if isinstance(after, Finding):
            out.append(after)
            continue
        if isinstance(before, Finding):
            before = {}
        before_total += sum(n for n, _ in before.values())
        after_total += sum(n for n, _ in after.values())
        for test_id, (count, line) in sorted(before.items(), key=lambda item: item[1][1]):
            path = old or ""
            dropped = count - after[test_id][0] if test_id in after else count
            if dropped <= 0:
                continue
            if (path, f"def {test_id.rsplit('::', 1)[-1]}") in allow:
                allowed += dropped
            else:
                removed.append(
                    (
                        path,
                        test_id if test_id not in after else f"{test_id} ({plural(dropped, 'fewer case')})",
                        line,
                    )
                )
    if after_total + allowed < before_total:
        names = ", ".join(f"{t} ({p} line {n} at the merge base)" for p, t, n in removed[:10])
        first = removed[0] if removed else ("tests/", "", 0)
        entries = "; ".join(
            f"{p} :: def {t.split(' ', 1)[0].rsplit('::', 1)[-1]}" for p, t, _ in removed[:10]
        )
        out.append(
            Finding(
                "tests", first[0], first[2],
                f"the test modules it touches had {before_total} tests at the merge base and have {after_total} now;"
                f" removed or renamed without a replacement: {names}",
                next_step=(
                    "Keep the tests, or, if they are meant to go, add an entry for each to the allowlist in a pull"
                    f" request of its own, merge that first, then this one. Entries: {entries}"
                ),
            )
        )  # fmt: skip
    return out


# Rule 4: required checks -------------------------------------------------------------------------------------

_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}")


@dataclass(frozen=True)
class Workflow:
    path: str
    text: str
    jobs: dict[str, dict[str, Any]]
    triggers: Any = None


def load_workflow(path: str, text: str | None) -> Workflow | None:
    if text is None or len(text) > MAX_PARSE_BYTES:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        return None
    # YAML 1.1 reads a bare `on:` key as True.
    triggers = data.get("on", data.get(True))
    return Workflow(path, text, {str(k): v for k, v in jobs.items() if isinstance(v, dict)}, triggers)


def workflows(snapshot: Snapshot) -> list[Workflow]:
    out = []
    for path in snapshot.paths:
        if (
            path.startswith(WORKFLOWS)
            and path.endswith((".yml", ".yaml"))
            and "/" not in path[len(WORKFLOWS) :]
        ):
            wf = load_workflow(path, snapshot.read(path))
            if wf is not None:
                out.append(wf)
    return out


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def matrix_combos(job: Mapping[str, Any]) -> list[dict[str, str]]:
    """The matrix's combinations, as GitHub builds them: the product of the axes, less each exclude; an include
    is added to every combination whose axis values it matches, or else becomes a combination of its own."""
    matrix = _table(job, "strategy", "matrix")
    axes = {k: v for k, v in matrix.items() if k not in ("include", "exclude") and isinstance(v, list)}
    names = list(axes)
    combos = (
        [{names[i]: _scalar(v) for i, v in enumerate(values)} for values in itertools.product(*axes.values())]
        if axes
        else []
    )
    excludes = [e for e in _list(matrix.get("exclude")) if isinstance(e, dict)]
    combos = [
        c for c in combos if not any(all(c.get(k) == _scalar(v) for k, v in e.items()) for e in excludes)
    ]
    includes = _list(matrix.get("include"))
    for inc in includes:
        if not isinstance(inc, dict):
            continue
        entry = {k: _scalar(v) for k, v in inc.items()}
        matched = [c for c in combos if all(c.get(k) == v for k, v in entry.items() if k in axes)]
        if matched:
            for c in matched:
                c.update({k: v for k, v in entry.items() if k not in axes})
        else:
            combos.append(entry)
    return combos


def check_names(job_id: str, job: Mapping[str, Any]) -> list[str]:
    """The status-check names a job reports, with matrix values filled in where they are literal."""
    name = _scalar(job.get("name", job_id))
    if not _MATRIX_REF.search(name):
        return [name]
    names: list[str] = []
    for combo in matrix_combos(job) or [{}]:

        def fill(m: re.Match[str], values: dict[str, str] = combo) -> str:
            return values.get(m[1], m[0])

        filled = _MATRIX_REF.sub(fill, name)
        if filled not in names:
            names.append(filled)
    return names


def producers(flows: list[Workflow]) -> dict[str, list[tuple[Workflow, str]]]:
    out: dict[str, list[tuple[Workflow, str]]] = {}
    for wf in flows:
        for job_id, job in wf.jobs.items():
            for name in check_names(job_id, job):
                out.setdefault(name, []).append((wf, job_id))
    return out


def _needs(job: Mapping[str, Any]) -> list[str]:
    needs = job.get("needs")
    return [_scalar(n) for n in ([needs] if isinstance(needs, str) else _list(needs))]


def closure(wf: Workflow, job_id: str) -> list[str]:
    """The job and every job it needs, directly or not, in the order found."""
    seen: list[str] = []
    todo = [job_id]
    while todo:
        current = todo.pop(0)
        if current in seen or current not in wf.jobs:
            continue
        seen.append(current)
        todo.extend(_needs(wf.jobs[current]))
    return seen


def _cond(value: Any) -> str:
    return " ".join(_scalar(value).split()) if value is not None else ""


def _truthy(value: Any) -> bool:
    return value is not None and value is not False and _scalar(value).strip().lower() != "false"


def _job_line(text: str, job_id: str, key: str | None = None, value: str = "", *, step: bool = False) -> int:
    """The line of `job_id:` under jobs, or of `key:` in that job: at the job's own level, or (step=True) in a
    step, preferring the line that holds value. Falls back to the job's line; 0 when the job is not found."""
    lines = _lines(text)
    head = re.compile(rf"\s+{re.escape(job_id)}:\s*(#.*)?")
    start = next((i for i, line in enumerate(lines) if head.fullmatch(line)), None)
    if start is None:
        return 0
    if key is None:
        return start + 1
    indent = _indent(lines[start])
    block = []
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.lstrip().startswith("#") and _indent(line) <= indent:
            break
        if line.strip():
            block.append(i)
    if not block:
        return start + 1
    child = _indent(lines[block[0]])
    pattern = re.compile(rf"\s*(?:-\s+)?{re.escape(key)}\s*:")
    hits = [i for i in block if pattern.match(lines[i]) and (_indent(lines[i]) > child) == step]
    exact = [i for i in hits if value and value[:40] in " ".join(lines[i].split())]
    return (exact or hits or [start])[0] + 1


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _step_values(job: Mapping[str, Any], key: str) -> list[str]:
    steps = _list(job.get("steps"))
    return [_cond(s[key]) for s in steps if isinstance(s, dict) and key in s and _truthy(s[key])]


def _compare_job(
    base_job: Mapping[str, Any],
    head_job: Mapping[str, Any],
    wf: Workflow,
    job_id: str,
    allow: frozenset[tuple[str, str]],
    rule: str = "required check",
    why: str = "which a required check runs or waits for",
) -> list[Finding]:
    """Conditions and continue-on-error a job gained. A new or changed `if:` may be allowlisted as `if: <cond>`
    (the condition with its whitespace collapsed); continue-on-error may not."""
    out = []
    was, now = _cond(base_job.get("if")), _cond(head_job.get("if"))
    allow_text = f"if: {now}".strip()
    if now != was and (wf.path, allow_text) not in allow:
        out.append(
            Finding(
                rule, wf.path, _job_line(wf.text, job_id, "if"),
                f"makes job '{job_id}', {why}, conditional: if: was "
                f"'{was or '(none)'}' at the merge base and is '{now or '(none)'}' now",
                allow_text, True,
            )
        )  # fmt: skip
    if _truthy(head_job.get("continue-on-error")) and not _truthy(base_job.get("continue-on-error")):
        out.append(
            Finding(
                rule, wf.path, _job_line(wf.text, job_id, "continue-on-error"),
                f"adds continue-on-error to job '{job_id}', {why}",
            )
        )  # fmt: skip
    for key in ("if", "continue-on-error"):
        old = Counter(_step_values(base_job, key))
        for value in _step_values(head_job, key):
            if old[value] > 0:
                old[value] -= 1
                continue
            text = f"{key}: {value}"
            if key == "if" and (wf.path, text) in allow:
                continue
            out.append(
                Finding(
                    rule, wf.path, _job_line(wf.text, job_id, key, value, step=True),
                    f"adds {key} on a step of job '{job_id}', {why}: {text}",
                    text, key == "if",
                )
            )  # fmt: skip
    return out


def check_required(
    base: Snapshot, head: Snapshot, required: list[str], allow: frozenset[tuple[str, str]]
) -> list[Finding]:
    out: list[Finding] = []
    before, after = producers(workflows(base)), producers(workflows(head))
    for check in required:
        # A required check is matched by its name alone, so any second job reporting that name could stand in for
        # the real one. Only the jobs that reported it at the merge base may report it.
        known = {(w.path, j) for w, j in before.get(check, [])}
        for wf_n, job_n in after.get(check, []) if known else []:
            if (wf_n.path, job_n) not in known:
                out.append(
                    Finding(
                        "required check", wf_n.path, _job_line(wf_n.text, job_n),
                        f"adds job '{job_n}', which reports the required check '{check}' beside the job that "
                        f"already does; a check is matched by name only, so it could stand in for the real one",
                    )
                )  # fmt: skip
        for wf_b, job_b in before.get(check, []):
            now = after.get(check)
            if not now:
                out.append(
                    Finding(
                        "required check", wf_b.path, _job_line(wf_b.text, job_b),
                        f"removes or renames the required check '{check}' (job '{job_b}' at the merge base). The "
                        f"ruleset still requires it; the maintainer changes the ruleset first",
                    )
                )  # fmt: skip
                continue
            wf_h, job_h = next(((w, j) for w, j in now if (w.path, j) == (wf_b.path, job_b)), now[0])
            jobs_b, jobs_h = closure(wf_b, job_b), closure(wf_h, job_h)
            for lost in (j for j in jobs_b if j not in jobs_h and j != job_b):
                out.append(
                    Finding(
                        "required check", wf_h.path, _job_line(wf_h.text, job_h, "needs"),
                        f"the required check '{check}' no longer runs or waits for job '{lost}'",
                    )
                )  # fmt: skip
            out += _compare_job(wf_b.jobs[job_b], wf_h.jobs[job_h], wf_h, job_h, allow)
            for j in jobs_h:
                if j == job_h:
                    continue
                # A job new to what the required job waits for is compared with nothing: a skipped need skips the
                # required job, and GitHub counts a skipped required check as passing.
                was = wf_b.jobs[j] if j in jobs_b else {}
                out += _compare_job(
                    was, wf_h.jobs[j], wf_h, j, allow,
                    why="which a required check runs or waits for" + ("" if j in jobs_b else " (newly)"),
                )  # fmt: skip
    if required:
        out += _unresolved_names(base, head)
    return out


def _unresolved_names(base: Snapshot, head: Snapshot) -> list[Finding]:
    """A new or renamed job whose check name is still an expression after the literal matrix values are filled in
    (`${{ 'CI ' + 'result' }}`, a fromJSON matrix). GitHub resolves it before it reports the check, so the gate
    cannot tell which check it reports: it could stand in for any required one."""
    old = {(wf.path, j): _scalar(job.get("name", j)) for wf in workflows(base) for j, job in wf.jobs.items()}
    out = []
    for wf in workflows(head):
        for job_id, job in wf.jobs.items():
            raw = _scalar(job.get("name", job_id))
            if old.get((wf.path, job_id)) == raw or not any("${{" in n for n in check_names(job_id, job)):
                continue
            out.append(
                Finding(
                    "required check", wf.path, _job_line(wf.text, job_id),
                    f"adds or renames job '{job_id}', whose check name '{raw}' is an expression the gate cannot"
                    " resolve, so it could report a required check",
                    next_step="Give the job a literal name, or a matrix of literal values.",
                )
            )  # fmt: skip
    return out


# Rule 6: hooks removed from the pre-commit config ---------------------------------------------------------------

PRE_COMMIT = ".pre-commit-config.yaml"


def hook_ids(text: str | None) -> Counter[str] | None:
    """The hook ids in a pre-commit config, or None when it does not parse."""
    if text is None:
        return Counter()
    if len(text) > MAX_PARSE_BYTES:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    repos = data.get("repos") if isinstance(data, dict) else None
    return Counter(
        _scalar(hook["id"])
        for repo in _list(repos)
        if isinstance(repo, dict)
        for hook in _list(repo.get("hooks"))
        if isinstance(hook, dict) and "id" in hook
    )


def check_hooks(
    changes: list[Change], base: Snapshot, head: Snapshot, allow: frozenset[tuple[str, str]]
) -> list[Finding]:
    """A hook removed from .pre-commit-config.yaml (or the file deleted) no longer runs on any commit."""
    out: list[Finding] = []
    for change in changes:
        if change.old != PRE_COMMIT:
            continue
        before = hook_ids(base.read(PRE_COMMIT))
        if before is None:
            continue  # main's own file does not parse; a pull request can only repair it
        head_text = head.read(PRE_COMMIT) if change.new == PRE_COMMIT else None
        after = hook_ids(head_text)
        if after is None:
            out.append(
                Finding(
                    "hooks", PRE_COMMIT, 0, "could not read the pre-commit config to check its hooks",
                    next_step="Fix the YAML so the gate can read it.",
                )
            )  # fmt: skip
            continue
        for hook in sorted((before - after).elements()):
            text = f"hook {hook}"
            if (PRE_COMMIT, text) in allow:
                continue
            out.append(Finding("hooks", PRE_COMMIT, 0, f"removes the pre-commit hook '{hook}'", text, True))
    return out


# Rule 8: CI time budgets only tighten ------------------------------------------------------------------------

BUDGETS = "ci/budgets.yml"


def _budget_leaves(data: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Every value in the budgets file by its key path; an empty table counts as a value of its own."""
    if isinstance(data, dict) and data:
        out: dict[tuple[str, ...], Any] = {}
        for key, value in data.items():
            out.update(_budget_leaves(value, (*prefix, _scalar(key))))
        return out
    return {prefix: data}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _parse_budgets(text: str | None) -> Any:
    """The parsed budgets, or a ValueError saying why they could not be read."""
    if text is None or len(text) > MAX_PARSE_BYTES:
        raise ValueError("missing or too large to check")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"not valid YAML ({str(exc).splitlines()[0]})") from exc


def check_budgets(changes: list[Change], base: Snapshot, head: Snapshot) -> list[Finding]:
    """A budget is a ceiling: every value at the merge base stays, numbers may only go down, nothing is added."""
    out: list[Finding] = []
    for change in changes:
        if change.old != BUDGETS:
            continue  # another file, or the budgets are new: there was nothing to loosen
        if change.new != BUDGETS:
            out.append(
                Finding("budgets", BUDGETS, 0, "deletes or moves the CI time budgets; they only tighten")
            )
            continue
        try:
            before = _budget_leaves(_parse_budgets(base.read(BUDGETS)))
        except ValueError:
            continue  # main's own file does not parse; a pull request can only repair it
        head_text = head.read(BUDGETS)
        try:
            after = _budget_leaves(_parse_budgets(head_text))
        except ValueError as exc:
            out.append(
                Finding("budgets", BUDGETS, 0, f"could not read the CI time budgets to check them: {exc}")
            )
            continue
        for key, was in before.items():
            name = " > ".join(key) or "(the whole file)"
            if key not in after:
                out.append(Finding("budgets", BUDGETS, 0, f"removes the budget {name} (was {was!r})"))
                continue
            now = after[key]
            old_n, new_n = _number(was), _number(now)
            if old_n is not None and (new_n is None or new_n > old_n):
                number, _ = _locate(head_text, f"{json.dumps(key[-1]) if key else ''}\0{now}")
                out.append(
                    Finding("budgets", BUDGETS, number, f"loosens the budget {name} from {was!r} to {now!r}")
                )
            elif old_n is None and now != was:
                out.append(
                    Finding("budgets", BUDGETS, 0, f"changes the budget {name} from {was!r} to {now!r}")
                )
        for key in after:
            if key not in before:
                name = " > ".join(key)
                number, _ = _locate(head_text, json.dumps(key[-1]) if key else "")
                out.append(
                    Finding(
                        "budgets", BUDGETS, number,
                        f"adds the budget {name} ({after[key]!r}); a new budget is a new allowance, so it loosens",
                    )
                )  # fmt: skip
    return out


# Rule 9: mutation jobs, wherever they run ---------------------------------------------------------------------

MUTATION = re.compile(r"mutmut|mutat(?:e|ion)", re.IGNORECASE)


def is_mutation_job(job_id: str, job: Mapping[str, Any]) -> bool:
    """A job that runs, gathers or checks mutation tests: its id, name, steps, env or reusable workflow say so."""
    return bool(MUTATION.search(job_id) or MUTATION.search(json.dumps(job, sort_keys=True, default=str)))


def _mutation_commands(job: Mapping[str, Any]) -> Counter[str]:
    """The lines of the job's steps (run and uses) and its own uses that mention mutation."""
    lines: list[str] = []
    if isinstance(job.get("uses"), str):
        lines.append(f"uses: {job['uses'].strip()}")
    for step in _list(job.get("steps")):
        if not isinstance(step, dict):
            continue
        if isinstance(step.get("uses"), str):
            lines.append(f"uses: {step['uses'].strip()}")
        if isinstance(step.get("run"), str):
            lines += [line.strip() for line in step["run"].splitlines()]
    return Counter(line for line in lines if line and MUTATION.search(line))


def _schedules(triggers: Any) -> list[str]:
    schedule = triggers.get("schedule") if isinstance(triggers, dict) else None
    return [
        _cond(s.get("cron")) for s in _list(schedule) if isinstance(s, dict) and s.get("cron") is not None
    ]


def _allowed_or(
    out: list[Finding], allow: frozenset[tuple[str, str]], path: str, line: int, message: str, text: str
) -> None:
    if (path, text) not in allow:
        out.append(Finding("mutation", path, line, message, text, True))


def check_mutation(base: Snapshot, head: Snapshot, allow: frozenset[tuple[str, str]]) -> list[Finding]:
    out: list[Finding] = []
    after = {wf.path: wf for wf in workflows(head)}
    for wf_b in workflows(base):
        mutation_jobs = [j for j, job in wf_b.jobs.items() if is_mutation_job(j, job)]
        if not mutation_jobs:
            continue
        wf_h = after.get(wf_b.path)
        if wf_h is None:
            for job_id in mutation_jobs:
                _allowed_or(
                    out, allow, wf_b.path, 0,
                    f"deletes or moves the workflow holding job '{job_id}', which runs mutation tests",
                    f"job {job_id}",
                )  # fmt: skip
            continue
        for cron in _schedules(wf_b.triggers):
            if cron not in _schedules(wf_h.triggers):
                _allowed_or(
                    out, allow, wf_h.path, 0,
                    f"takes the schedule '{cron}' away from a workflow that runs mutation tests",
                    f"schedule {cron}",
                )  # fmt: skip
        for job_id in mutation_jobs:
            head_job = wf_h.jobs.get(job_id)
            if head_job is None:
                _allowed_or(
                    out,
                    allow,
                    wf_h.path,
                    0,
                    f"removes job '{job_id}', which runs mutation tests",
                    f"job {job_id}",
                )
                continue
            out += _compare_job(
                wf_b.jobs[job_id], head_job, wf_h, job_id, allow, "mutation", "which runs mutation tests"
            )
            lost = _mutation_commands(wf_b.jobs[job_id]) - _mutation_commands(head_job)
            for command in sorted(lost):
                _allowed_or(
                    out, allow, wf_h.path, _job_line(wf_h.text, job_id),
                    f"job '{job_id}' no longer runs: {command}", f"{job_id}: {command}",
                )  # fmt: skip
        for waiter in wf_b.jobs:
            if waiter not in wf_h.jobs:
                continue
            still = closure(wf_h, waiter)
            for job_id in closure(wf_b, waiter)[1:]:
                if job_id in mutation_jobs and job_id not in still:
                    _allowed_or(
                        out, allow, wf_h.path, _job_line(wf_h.text, waiter, "needs"),
                        f"job '{waiter}' no longer waits for job '{job_id}', which runs mutation tests",
                        f"needs {waiter} -> {job_id}",
                    )  # fmt: skip
    return out


# Running every rule ------------------------------------------------------------------------------------------


def check(
    changes: list[Change], base: Snapshot, head: Snapshot, trusted: Snapshot, required: list[str] | None
) -> list[Finding]:
    """Every rule over one change. base is the merge base, head the pull request, trusted main (the allowlist)."""
    allow = parse_allowlist(trusted.read(ALLOWLIST))
    found = (
        check_sealed(changes)
        + check_isolation(changes)
        + check_links(changes, base, head)
        + check_thresholds(changes, base, head)
        + check_markers(changes, base, head, allow)
        + check_toml(changes, base, head, allow)
        + check_side_configs(changes)
        + check_tests(changes, base, head, allow)
        + (check_required(base, head, required, allow) if required is not None else [])
        + check_budgets(changes, base, head)
        + check_hooks(changes, base, head, allow)
        + check_mutation(base, head, allow)
    )
    unique: dict[tuple[str, str, int, str], Finding] = {}
    for f in found:
        unique.setdefault((f.rule, f.path, f.line, f.message), f)
    return list(unique.values())


# Reading git -------------------------------------------------------------------------------------------------


class GitError(RuntimeError):
    """git failed; the gate fails closed."""


def git_env() -> dict[str, str]:
    """The environment without git's own variables: inside a git hook GIT_DIR, GIT_INDEX_FILE and the like point at
    the repository being pushed, and would override `git -C <repo>`."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=git_env())
    if proc.returncode != 0:
        raise GitError(f"git {args[0]} failed: {proc.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return proc.stdout


def _commit(repo: Path, ref: str) -> str:
    # The ^{commit} suffix means no ref, whatever it starts with, reads as an option.
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()


def parse_ls_tree(raw: str) -> dict[str, str]:
    """`git ls-tree -r -z` output as path -> mode."""
    out = {}
    for entry in raw.split("\0"):
        meta, tab, path = entry.partition("\t")
        if tab:
            out[path] = meta.split(" ", 1)[0]
    return out


def git_snapshot(repo: Path, commit: str) -> Snapshot:
    modes = parse_ls_tree(_git(repo, "ls-tree", "-r", "-z", commit).decode("utf-8", "replace"))

    def read(path: str) -> str | None:
        """The file's text; None when the commit has no such file (or it is a submodule). Any other failure is a
        git error, so the gate fails closed rather than reading it as absent."""
        if modes.get(path, "160000") == "160000":
            return None
        return _git(repo, "cat-file", "blob", f"{commit}:{path}").decode("utf-8", "replace")

    return Snapshot(read=read, paths=sorted(modes), modes={p: m for p, m in modes.items() if m in LINK_MODES})


def parse_name_status(raw: str) -> list[Change]:
    """`git diff --name-status -z` output as changes; renames and copies carry both paths."""
    fields = raw.split("\0")
    out = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i][0]
        if status in ("R", "C"):
            old, new = fields[i + 1], fields[i + 2]
            out.append(Change(old if status == "R" else None, new))
            i += 3
        else:
            path = fields[i + 1]
            out.append(Change(None if status == "A" else path, None if status == "D" else path))
            i += 2
    return out


def git_changes(repo: Path, base: str, head: str) -> list[Change]:
    raw = _git(
        repo, "diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z", "-M",
        base, head, "--",
    )  # fmt: skip
    return parse_name_status(raw.decode("utf-8", "replace"))


def run(repo: Path, base_ref: str, head_ref: str, required: list[str] | None) -> list[Finding]:
    trusted, head = _commit(repo, base_ref), _commit(repo, head_ref)
    merge_base = _git(repo, "merge-base", trusted, head).decode().strip()
    return check(
        git_changes(repo, merge_base, head),
        git_snapshot(repo, merge_base),
        git_snapshot(repo, head),
        git_snapshot(repo, trusted),
        required,
    )


def read_required(path: Path | None) -> list[str] | None:
    """The required check names, one per line; None when no file is given. An empty file is an error: the ruleset
    always requires checks, so an empty answer means the lookup went wrong, and the rule must not go quiet."""
    if path is None:
        return None
    names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not names:
        raise ValueError(f"{path} lists no required checks; main's ruleset should require some")
    return names


def main(argv: list[str] | None = None, out: Any = None) -> int:
    stream = out or sys.stdout
    ap = argparse.ArgumentParser(
        prog="python -m health_data_registry.ratchet", description=__doc__.split("\n")[0]
    )
    ap.add_argument("--repo", type=Path, default=Path("."), help="the git checkout holding both commits")
    ap.add_argument(
        "--base", required=True, help="main's commit (trusted); compared at its merge base with --head"
    )
    ap.add_argument("--head", required=True, help="the pull request's commit, read as data")
    ap.add_argument("--required-checks", type=Path, help="one required check name per line, from the ruleset")
    ap.add_argument("--github", action="store_true", help="also print GitHub error annotations")
    a = ap.parse_args(argv)
    try:
        required = read_required(a.required_checks)
        findings = run(a.repo, a.base, a.head, required)
    except (GitError, OSError, ValueError) as exc:
        print(
            f"The Trusted gate could not check this change, so it fails: {clean(str(exc)).rstrip('.')}. Run the check again;"
            " if it fails the same way, merge origin/main into the branch and push again, and if it still fails,"
            " the gate itself needs the maintainer (docs/dev/README.md#when-the-gate-itself-is-wrong).",
            file=stream,
        )
        return 2
    if required is None:
        print("Required checks: not checked (no --required-checks file given).", file=stream)
    for f in findings:
        print(f.render(), file=stream)
        if a.github:
            print(f.annotation(), file=stream)
    if findings:
        print(
            f"\nThe Trusted gate found {plural(len(findings), 'problem')} with this change: each weakens main's"
            " checks.",
            file=stream,
        )
        return 1
    print("The Trusted gate found nothing that weakens main's checks.", file=stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
