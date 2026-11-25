"""Map the files a change touches to the CI work it can affect (the `changes` job in .github/workflows/ci.yml).

Usage:
  python scripts/ci_impact.py --base MERGE_BASE [--github-output FILE]   # a pull request or a pre-pr push
  python scripts/ci_impact.py --full [--github-output FILE]              # main, the merge queue, nightly

It prints one JSON object and, with --github-output, writes the same keys as step outputs:

  full    true when everything runs (--full, or a changed path that runs everything)
  tests   "all", "none", or a JSON list of the test files to run
  dbt     true when the dbt build on fixtures must run
  kit     true when the Claude Projects kit must be built
  reason  one line per rule that fired, for the log

The rules, in order, for each path changed since the merge base (`git diff --name-only`, renames as delete + add,
plus untracked files when run locally):

1. Run everything: workflows and other CI config, scripts/, the Makefile, pyproject.toml, uv.lock, the
   pre-commit config, .python-version, tests/fixtures/ and tests/golden/, and any file this map does not know.
   An unknown path fails closed: it runs everything.
2. Python code (src/health_data_registry, eval, tools, tests): the module and every module that imports it,
   however indirectly (the static import graph of scripts/mutate_changed.py, which also counts a project module
   named in a string, such as a monkeypatch target). Test files in that set, or under a conftest.py in it, run.
   dbt runs when the module is one the dbt build imports (transform_gen, dbt_raw, and what they import).
3. Data and generated files (contracts/, semantic/, catalog/, transform/, template/, plugin/, fuzz/, assets/,
   ...): every project module whose source names the changed file, its folder, or the file's name is treated as
   changed (rule 2 then follows its importers). contracts/, semantic/, catalog/ and transform/ also run dbt.
4. Documentation (README and the other root *.md files, docs/, LICENSE, NOTICE, CITATION.cff): only the docs
   checks, which always run in the Checks job (pre-commit, docs drift from `make docs`, the kit), plus
   tests/test_workflows.py when docs/helpers.md changes. No dbt, no other test.

What a pull request skips here, the full suite runs on main, in the merge queue and nightly (--full), so a test
this map misses can never reach main untested. Standard library only (with scripts/mutate_changed.py), so the
job needs no project install.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutate_changed as mc  # noqa: E402 - a sibling script, found through the line above

ROOT = Path(__file__).resolve().parents[1]

RUN_ALL_PREFIXES = (
    ".github/",
    "scripts/",
    "tests/fixtures/",
    "tests/golden/",
    ".clusterfuzzlite/",
)
RUN_ALL_FILES = frozenset(
    {"Makefile", "pyproject.toml", "uv.lock", ".pre-commit-config.yaml", ".python-version", ".gitignore"}
)
PYTHON_TOPS = ("src/health_data_registry/", "eval/", "tools/", "tests/")
# Folders of data, generated output and packaging: whatever names them is affected (rule 3).
DATA_TOPS = (
    "contracts/",
    "semantic/",
    "catalog/",
    "transform/",
    "template/",
    "plugin/",
    ".claude-plugin/",
    "fuzz/",
    "assets/",
    "licenses/",
    "packaging/",
    "npm/",
    "space/",
    "eval/",
)
DBT_TOPS = ("contracts/", "semantic/", "catalog/", "transform/")
# The modules `make transform` runs: transform_gen --check, and dbt's duckdb plugin (transform/profiles.yml).
DBT_MODULES = frozenset({"health_data_registry.transform_gen", "health_data_registry.dbt_raw"})
DOC_FILES = frozenset(
    {
        "README.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        "CLAUDE.md",
        "LICENSE",
        "NOTICE",
        "CITATION.cff",
        ".dockerignore",
    }
)
DOC_TOPS = ("docs/",)
# The docs file a test checks (docs/helpers.md is generated from helpers.py; the test fails on drift).
DOC_TESTS = {"docs/helpers.md": "tests/test_workflows.py"}


@dataclass
class Impact:
    full: bool = False
    tests: set[str] = field(default_factory=set)
    dbt: bool = False
    kit: bool = False
    reasons: list[str] = field(default_factory=list)

    def everything(self, why: str) -> None:
        self.full = True
        self.reasons.append(f"everything: {why}")

    def as_dict(self) -> dict[str, object]:
        if self.full:
            return {"full": True, "tests": "all", "dbt": True, "kit": True, "reason": self.reasons}
        tests: object = sorted(self.tests) if self.tests else "none"
        return {"full": False, "tests": tests, "dbt": self.dbt, "kit": self.kit, "reason": self.reasons}


def changed_paths(base: str, root: Path = ROOT) -> list[str]:
    """Paths changed since `base` (a commit), in the working tree, plus untracked files. No renames: a moved file
    is a deletion and an addition, so both paths count."""
    diff = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", base, "--"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return sorted({p for p in (diff + untracked).splitlines() if p})


def _module_of(path: str, files: dict[str, Path]) -> str | None:
    by_path = {rel.as_posix(): n for n, rel in files.items()}
    return by_path.get(path)


def _mentions(source: str, path: str) -> bool:
    """Whether a module's source names a changed file: its path, its file name, or one of its folders (quoted,
    as `ROOT / "contracts"` does)."""
    p = Path(path)
    if path in source or (p.suffix and f'"{p.name}"' in source):
        return True
    return any(f'"{part}"' in source for part in p.parts[:-1])


def impact(paths: list[str], root: Path = ROOT, full: bool = False) -> Impact:
    """What a change to `paths` can affect (the rules in the module docstring)."""
    out = Impact()
    if full:
        out.everything("a full run (main, the merge queue or nightly)")
        return out
    files, deps = mc.import_graph(root)
    sources = {n: (root / rel).read_text(encoding="utf-8") for n, rel in files.items()}
    changed_modules: set[str] = set()
    for path in paths:
        name = Path(path).name
        if path in RUN_ALL_FILES or path.startswith(RUN_ALL_PREFIXES):
            out.everything(f"{path} runs everything")
            return out
        if path.endswith(".py") and path.startswith(PYTHON_TOPS) and not path.startswith("eval/sealed/"):
            module = _module_of(path, files)
            if module is None and (root / path).exists():
                out.everything(f"{path} is Python the import graph does not know")
                return out
            if module is not None:
                changed_modules.add(module)
                out.reasons.append(f"{path}: module {module} and its importers")
            else:  # deleted: whatever names it is affected
                hits = {n for n, src in sources.items() if path in src or Path(path).stem in src}
                changed_modules |= hits
                out.reasons.append(f"{path} (deleted): the {len(hits)} modules that named it")
            out.kit = True
            continue
        if (path in DOC_FILES and "/" not in path) or path.startswith(DOC_TOPS):
            out.kit = True
            if path in DOC_TESTS:
                out.tests.add(DOC_TESTS[path])
            out.reasons.append(f"{path}: docs checks only")
            continue
        if path.startswith(DATA_TOPS):
            hits = {n for n, src in sources.items() if _mentions(src, path)}
            changed_modules |= hits
            out.kit = True
            if path.startswith(DBT_TOPS):
                out.dbt = True
            out.reasons.append(
                f"{path}: data; the {len(hits)} modules that name it"
                + (", and dbt" if path.startswith(DBT_TOPS) else "")
            )
            continue
        if name.endswith(".md") and "/" not in path:
            out.reasons.append(f"{path}: a root Markdown file, docs checks only")
            continue
        out.everything(f"{path} is a path this map does not know")
        return out
    reached = mc.importers_closure(changed_modules, deps)
    out.tests |= set(mc.test_files_reached(files, reached, ("tests/integration/",)))
    # dbt imports DBT_MODULES and what they import: a change to any of those runs dbt.
    dbt_needs = _imports_closure(DBT_MODULES, deps)
    if changed_modules & dbt_needs:
        out.dbt = True
        out.reasons.append("a module the dbt build imports changed: dbt")
    return out


def _imports_closure(modules: frozenset[str], deps: dict[str, set[str]]) -> set[str]:
    """`modules` and everything they import, however indirectly."""
    seen = set(modules)
    todo = list(modules)
    while todo:
        for d in deps.get(todo.pop(), ()):
            if d not in seen:
                seen.add(d)
                todo.append(d)
    return seen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="the merge base to compare with (a commit)")
    ap.add_argument("--full", action="store_true", help="run everything (main, the merge queue, nightly)")
    ap.add_argument("--github-output", type=Path, help="append the results as step outputs to this file")
    a = ap.parse_args(argv)
    if not a.full and not a.base:
        ap.error("--base is required unless --full")
    result = impact(changed_paths(a.base) if not a.full else [], full=a.full).as_dict()
    for line in result["reason"]:  # type: ignore[attr-defined]
        print(line, file=sys.stderr)
    print(json.dumps(result))
    if a.github_output:
        with a.github_output.open("a", encoding="utf-8") as f:
            for key in ("full", "tests", "dbt", "kit"):
                value = result[key]
                f.write(f"{key}={value if isinstance(value, str) else json.dumps(value)}\n")
            # The test files as pytest arguments: project paths only (letters, digits, _ . / -), never shell text.
            tests = result["tests"]
            f.write(f"test_args={' '.join(tests) if isinstance(tests, list) else ''}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
