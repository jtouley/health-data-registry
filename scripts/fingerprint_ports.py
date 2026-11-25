"""Record each port contract's interface and docs fingerprints (contracts/ports/*.odcs.yaml).

Run after changing a port contract and bumping its version: a new major version for a change of shape (columns,
types, required, keys), a new minor for a change of meaning (descriptions, rules, properties). It adds one
{version, interface, docs} entry to customProperties.fingerprints; `validate` then checks the version rule.

    uv run python scripts/fingerprint_ports.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from health_data_registry.product import PORT_CONTRACTS, _custom, docs_fingerprint, interface_fingerprint

ROOT = Path(__file__).resolve().parents[1]
MARK = "- property: fingerprints\n"


def _block(history: list[dict[str, str]]) -> str:
    rows = "".join(
        f"  - {{version: {h['version']}, interface: {h['interface']}, docs: {h['docs']}}}\n" for h in history
    )
    return MARK + "  value:\n" + rows


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
    if MARK in text:
        head, rest = text.split(MARK, 1)
        tail = rest.split("\n- property:", 1)
        text = head + _block(history) + ("- property:" + tail[1] if len(tail) == 2 else "")
    else:
        head, sep, rest = text.partition("\n- property: openQuestions\n")
        if not sep:
            raise SystemExit(f"{path.name}: no top-level openQuestions to place the fingerprints before")
        text = head + "\n" + _block(history) + sep.lstrip("\n") + rest
    path.write_text(text, encoding="utf-8")
    return f"recorded {entry['version']}"


def main() -> int:
    for path in sorted((ROOT / PORT_CONTRACTS).glob("*.odcs.yaml")):
        print(f"{path.name}: {stamp(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
