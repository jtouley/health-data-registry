"""Record each port contract's interface and docs fingerprints (contracts/ports/*.odcs.yaml).

Run after changing a port contract and bumping its version: a new major version for a change of shape (columns,
types, required, keys), a new minor for a change of meaning (descriptions, rules, properties). It adds one
{version, interface, docs} entry to customProperties.fingerprints; `validate` then checks the version rule.

    uv run python scripts/fingerprint_ports.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

from health_data_registry.product import PORT_CONTRACTS, _custom, docs_fingerprint, interface_fingerprint

ROOT = Path(__file__).resolve().parents[1]
# The customProperties item that holds the history, at any indent, on a line of its own.
MARK = re.compile(r"^(?P<indent>[ \t]*)- property: fingerprints[ \t]*\n", re.MULTILINE)
OPEN_QUESTIONS = re.compile(r"^(?P<indent>[ \t]*)- property: openQuestions[ \t]*\n", re.MULTILINE)


def _block(history: list[dict[str, str]], indent: str = "") -> str:
    rows = "".join(
        f"{indent}  - {{version: {h['version']}, interface: {h['interface']}, docs: {h['docs']}}}\n"
        for h in history
    )
    return f"{indent}- property: fingerprints\n{indent}  value:\n" + rows


def _block_end(text: str, start: int, indent: str) -> int:
    """Where the fingerprints item ends: at the first non-blank line indented no deeper than its `- property`."""
    pos = start
    for line in text[start:].splitlines(keepends=True):
        body = line.rstrip("\n")
        if body.strip() and len(body) - len(body.lstrip(" \t")) <= len(indent):
            return pos
        pos += len(line)
    return pos


def stamp(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    history = list(_custom(doc.get("customProperties")).get("fingerprints") or [])
    entry = {
        "version": str(doc["version"]),
        "interface": interface_fingerprint(doc),
        "docs": docs_fingerprint(doc),
    }
    if history and {k: str(v) for k, v in history[-1].items()} == entry:
        return "unchanged"
    if history and str(history[-1]["version"]) == entry["version"]:
        raise SystemExit(f"{path.name}: it changed since {entry['version']}; bump its version first")
    history.append(entry)
    found = MARK.search(text)
    if found:
        indent = found["indent"]
        end = _block_end(text, found.end(), indent)
        text = text[: found.start()] + _block(history, indent) + text[end:]
    else:
        oq = OPEN_QUESTIONS.search(text)
        if not oq:
            raise SystemExit(f"{path.name}: no top-level openQuestions to place the fingerprints before")
        text = text[: oq.start()] + _block(history, oq["indent"]) + text[oq.start() :]
    # The edit is checked, never trusted: everything but the history reads back exactly as before.
    after = yaml.safe_load(text)
    if _without_history(after) != _without_history(doc) or (
        _custom(after.get("customProperties")).get("fingerprints") != history
    ):
        raise SystemExit(
            f"{path.name}: recording the fingerprints would change other content; nothing written"
        )
    path.write_text(text, encoding="utf-8")
    return f"recorded {entry['version']}"


def _without_history(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    out["customProperties"] = [
        c
        for c in doc.get("customProperties") or []
        if not (isinstance(c, dict) and c.get("property") == "fingerprints")
    ]
    return out


def main() -> int:
    for path in sorted((ROOT / PORT_CONTRACTS).glob("*.odcs.yaml")):
        print(f"{path.name}: {stamp(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
