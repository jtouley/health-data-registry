"""The ODPS data product (contracts/health-data-registry.odps.yaml): the hub's products and their output ports.

Each output port is one catalogued source; its contractId and version name the ODCS contract it serves. The
server shows the product and port with every source, and `export-public` writes them into each release manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PRODUCT_FILE = Path("contracts") / "health-data-registry.odps.yaml"


@dataclass(frozen=True)
class Port:
    product_id: str
    product_version: str
    name: str
    contract_id: str
    version: str
    hosted: bool


def _custom(items: Any) -> dict[str, Any]:
    return {str(i["property"]): i.get("value") for i in items or [] if isinstance(i, dict) and "property" in i}


def load_ports(root: Path) -> dict[str, Port]:
    """Output ports by contract id; empty when the registry has no product file (e.g. a test registry)."""
    path = root / PRODUCT_FILE
    if not path.is_file():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if doc.get("kind") != "DataProduct":
        raise ValueError(f"{path}: kind must be DataProduct")
    ports: dict[str, Port] = {}
    for p in doc.get("outputPorts") or []:
        cp = _custom(p.get("customProperties"))
        port = Port(
            product_id=str(doc.get("id", "")),
            product_version=str(doc.get("version", "")),
            name=str(p.get("name", "")),
            contract_id=str(p.get("contractId", "")),
            version=str(p.get("version", "")),
            hosted=bool(cp.get("hosted", False)),
        )
        ports[port.contract_id] = port
    return ports
