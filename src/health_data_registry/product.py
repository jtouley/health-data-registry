"""The ODPS data product (contracts/health-data-registry.odps.yaml), and Bitol's schemas for it and every contract.

Each output port is one catalogued source; its contractId and version name the ODCS contract it serves. The
server shows the product and port with every source, and `export-public` writes them into each release manifest.

`schema_problems` checks every `*.odps.yaml` against Bitol's official ODPS v1.1.0 JSON schema and every
`*.odcs.yaml` (sources and models) against the official ODCS v3.2.0 schema. Both schemas are vendored byte for byte
in schemas/, fetched only at development time by scripts/vendor_schemas.py and never at run time; a copy whose
sha256 is not the recorded one is refused before use. `validate` reports every problem it finds.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PRODUCT_FILE = Path("contracts") / "health-data-registry.odps.yaml"
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


class SchemaError(ValueError):
    """A vendored schema is missing or is not the recorded copy."""


@dataclass(frozen=True)
class VendoredSchema:
    """One of Bitol's official JSON schemas, vendored byte for byte from its release tag."""

    standard: str
    api_version: str
    url: str
    sha256: str
    glob: str

    @property
    def path(self) -> Path:
        return SCHEMA_DIR / self.url.rsplit("/", 1)[1]


ODPS = VendoredSchema(
    "ODPS",
    "v1.1.0",
    "https://raw.githubusercontent.com/bitol-io/open-data-product-standard/v1.1.0/schema/odps-json-schema-v1.1.0.json",
    "b7eae1af15b3c6b18e5dffaa2119212acb38ec73286933c76d16c8712e0d90d1",
    "*.odps.yaml",
)
ODCS = VendoredSchema(
    "ODCS",
    "v3.2.0",
    "https://raw.githubusercontent.com/bitol-io/open-data-contract-standard/v3.2.0/schema/odcs-json-schema-v3.2.0.json",
    "edb41f33ec46e84780e99872ab2bd67f074959d2bf3e9c9fc54e61f8982b0d93",
    "*.odcs.yaml",
)
VENDORED = (ODPS, ODCS)


def load_schema(spec: VendoredSchema, path: Path | None = None) -> dict[str, Any]:
    """A vendored Bitol JSON schema, refused unless its bytes are exactly the recorded ones."""
    path = path or spec.path
    try:
        data = path.read_bytes()
    except OSError as e:
        raise SchemaError(
            f"the vendored {spec.standard} schema is missing ({path.name}); restore it with scripts/vendor_schemas.py"
        ) from e
    digest = hashlib.sha256(data).hexdigest()
    if digest != spec.sha256:
        raise SchemaError(
            f"the vendored {spec.standard} schema {path.name} has sha256 {digest}, not the recorded {spec.sha256}; "
            "restore it with scripts/vendor_schemas.py"
        )
    schema: dict[str, Any] = json.loads(data)
    return schema


def schema_errors(doc: Mapping[str, Any], schema: Mapping[str, Any]) -> list[str]:
    """Every place `doc` breaks `schema`, as `path: message` lines, in path order; empty when it conforms."""
    import jsonschema

    validator = jsonschema.Draft201909Validator(
        schema, format_checker=jsonschema.Draft201909Validator.FORMAT_CHECKER
    )
    errors = sorted(validator.iter_errors(dict(doc)), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(top)'}: {e.message}" for e in errors]


def schema_files(root: Path, spec: VendoredSchema) -> list[Path]:
    """Every file under contracts/ (models/ included) that the schema governs."""
    return sorted((root / "contracts").rglob(spec.glob))


def schema_problems(root: Path) -> list[str]:
    """Every product and contract under `root`/contracts checked against Bitol's vendored schemas, as lines naming
    the file; empty when all conform. Each file must also declare the vendored schema's apiVersion."""
    problems: list[str] = []
    for spec in VENDORED:
        try:
            schema = load_schema(spec)
        except SchemaError as e:
            problems.append(str(e))
            continue
        for path in schema_files(root, spec):
            where = path.relative_to(root).as_posix()
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as e:
                problems.append(f"{where}: not readable YAML ({e})")
                continue
            if not isinstance(doc, Mapping):
                problems.append(f"{where}: not a YAML mapping")
                continue
            if doc.get("apiVersion") != spec.api_version:
                problems.append(
                    f"{where}: apiVersion must be {spec.api_version}, the vendored {spec.standard} schema's version"
                )
            problems.extend(f"{where}: {m}" for m in schema_errors(doc, schema))
    return problems


@dataclass(frozen=True)
class Port:
    product_id: str
    product_version: str
    name: str
    contract_id: str
    version: str
    hosted: bool


def _custom(items: Any) -> dict[str, Any]:
    return {
        str(i["property"]): i.get("value") for i in items or [] if isinstance(i, dict) and "property" in i
    }


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
