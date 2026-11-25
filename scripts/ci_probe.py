"""CI timing probes (tests/ci_probes/probes.yml): make one, or predict the jobs a pull request's run selects for it.

Usage:
  python scripts/ci_probe.py list
  python scripts/ci_probe.py predict NAME     # JSON: the selection scripts/ci_impact.py and mutate_changed.py make
  python scripts/ci_probe.py apply NAME       # make the change in this checkout (for a live timing run)

A probe is a tiny synthetic change (see the probes file for the kinds). `predict` works in memory and writes
nothing; tests/test_ci_probes.py compares it with each probe's `expect`. Needs PyYAML (the project's environment).
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_impact  # noqa: E402 - sibling scripts, found through the line above
import mutate_changed as mc  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROBES = ROOT / "tests" / "ci_probes" / "probes.yml"


def load(path: Path = PROBES) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    probes: list[dict[str, Any]] = data["probes"]
    names = [p["name"] for p in probes]
    if len(names) != len(set(names)):
        raise ValueError("probe names must be unique")
    return probes


def find(name: str, probes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    for p in probes if probes is not None else load():
        if p["name"] == name:
            return p
    raise KeyError(f"no probe named {name!r}")


def edited(probe: dict[str, Any], root: Path = ROOT) -> tuple[str, str, set[int] | None]:
    """(path, new text, changed lines) of the probe's change; lines None for a new file."""
    path = probe["path"]
    if probe["edit"] == "new_file":
        if (root / path).exists():
            raise ValueError(f"{path} already exists; a new-file probe needs a path that does not")
        return path, probe["text"], None
    lines = (root / path).read_text(encoding="utf-8").splitlines(keepends=True)
    if probe["edit"] == "append":
        at = len(lines)
    elif probe["edit"] == "function":
        fn = next(
            (
                n
                for n in ast.parse("".join(lines)).body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == probe["function"]
            ),
            None,
        )
        if fn is None:
            raise ValueError(f"{path} has no top-level function {probe['function']}; update the probe")
        # Right before the body's first statement, after the def line and its continuation lines.
        at = fn.body[0].lineno - 1
        indent = len(lines[at]) - len(lines[at].lstrip())
        probe = {**probe, "text": " " * indent + "# ci probe"}
    elif probe["edit"] == "assignment":
        at = next((i + 1 for i, text in enumerate(lines) if text.startswith(probe["assigns"])), None)
        if at is None:
            raise ValueError(f"{path} has no line starting {probe['assigns']}; update the probe")
    else:
        raise ValueError(f"unknown edit kind {probe['edit']!r}")
    new = [*lines[:at], probe["text"].rstrip("\n") + "\n", *lines[at:]]
    if at == len(lines) and lines and not lines[-1].endswith("\n"):
        new[at - 1] += "\n"
    return path, "".join(new), {at + 1}


def predict(probe: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    """The selection a pull request's run makes for the probe: tests, dbt, and mutation shards (bool)."""
    path, text, changed = edited(probe, root)
    imp = ci_impact.impact([path], root).as_dict()
    mutation = False
    if path.endswith(".py") and path.startswith(("src/health_data_registry/", "eval/")):
        mutation = bool(mc.changed_functions(text, changed))
    return {"full": imp["full"], "tests": imp["tests"] != "none", "dbt": imp["dbt"], "mutation": mutation}


def apply(probe: dict[str, Any], root: Path = ROOT) -> str:
    path, text, _ = edited(probe, root)
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["list", "predict", "apply"])
    ap.add_argument("name", nargs="?")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        print("\n".join(p["name"] for p in load()))
        return 0
    if not a.name:
        ap.error(f"{a.cmd} needs a probe name")
    probe = find(a.name)
    if a.cmd == "predict":
        print(json.dumps(predict(probe)))
    else:
        print(f"Changed {apply(probe)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
