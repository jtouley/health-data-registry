"""ODPS data products (contracts/*.odps.yaml): the products, their output ports, and the checks they must pass.

Three products, one file each:

- contracts/health-data-registry.odps.yaml, the open data hub. Each output port is one catalogued source; its
  contractId and version name that source's ODCS contract. The server shows the product and port with every
  source, and `export-public` writes them into each release manifest (`load_ports`, hub only).
- contracts/id-conference-abstracts.odps.yaml and contracts/michigan-hiv.odps.yaml. Each output port has its own
  ODCS contract in contracts/ports/, and reads one or more catalogued sources (`inputContracts`).

`validate_products` checks every *.odps.yaml against Bitol's official ODPS v1.1.0 JSON schema and every
*.odcs.yaml against the official ODCS v3.2.0 schema, both vendored byte for byte in schemas/ (fetched at
development time by scripts/vendor_schemas.py, never at run time; each sha256 is checked before use). Each port
contract in contracts/ports/ also carries interface and docs fingerprints (a new major version for a change of
shape, a new minor for a change of meaning) and quality rules with a dbt-test name, a dimension and a severity, so
the silver stage can generate them as dbt tests; a port is served only when its error-severity rules pass
(`servable`). Then this project's own rules:

- every port names its tier (public or private), where it is hosted, its release tag, its SLA and its feedback
  channel (the contract-finding issue form);
- a port inherits its sources' licences: the licences it declares must be exactly its sources' licences, and its
  answers carry its sources' caveats (`inherited_context`);
- no public port exposes private-tier data: whatever its hosting, every source of a public port is licensed for a
  public copy (a restricted or unresolved licence, or a source read live only, fails), it never reads a
  private-tier port, and every column of its contract declares its tier, none of them private;
- a private-tier port is never on the public hub (hard rule 10), and is stored in a private Hugging Face dataset
  only when every source's licence allows a stored copy (a source read live only never does);
- a port refreshes no more often than its slowest source (`sla.refresh`; irregular always fits);
- a port whose sources aren't catalogued yet is a draft stub: not hosted, with open questions, never a guess.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from health_data_registry.registry import (
    REDISTRIBUTABLE_LICENCES,
    Contract,
    Registry,
    RegistryError,
    UnreliableSpec,
    parse_contract,
)

PRODUCT_FILE = Path("contracts") / "health-data-registry.odps.yaml"
PRODUCT_GLOB = "*.odps.yaml"
PORT_CONTRACTS = Path("contracts") / "ports"

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


@dataclass(frozen=True)
class VendoredSchema:
    """One of Bitol's official JSON schemas, vendored byte for byte from its release tag.

    Fetched only at development time by scripts/vendor_schemas.py, which checks `sha256`; never at run time.
    """

    standard: str
    api_version: str
    url: str
    sha256: str

    @property
    def path(self) -> Path:
        return SCHEMA_DIR / self.url.rsplit("/", 1)[1]


ODPS = VendoredSchema(
    "ODPS",
    "v1.1.0",
    "https://raw.githubusercontent.com/bitol-io/open-data-product-standard/v1.1.0/schema/odps-json-schema-v1.1.0.json",
    "b7eae1af15b3c6b18e5dffaa2119212acb38ec73286933c76d16c8712e0d90d1",
)
ODCS = VendoredSchema(
    "ODCS",
    "v3.2.0",
    "https://raw.githubusercontent.com/bitol-io/open-data-contract-standard/v3.2.0/schema/odcs-json-schema-v3.2.0.json",
    "edb41f33ec46e84780e99872ab2bd67f074959d2bf3e9c9fc54e61f8982b0d93",
)
VENDORED = (ODPS, ODCS)
ODPS_API_VERSION = ODPS.api_version
ODCS_API_VERSION = ODCS.api_version

# Every port's feedback channel: the contract-finding issue form (.github/ISSUE_TEMPLATE/contract-finding.yml).
FEEDBACK_URL = "https://github.com/jtouley/health-data-registry/issues/new?template=contract-finding.yml"

TIERS = ("public", "private")
# public-hub: data files in the open Hugging Face dataset. private-hf: data files only in a private Hugging Face
# dataset. live: read from the publisher per question, in memory only, never stored. none: not built yet.
HOSTINGS = ("public-hub", "private-hf", "live", "none")
RELEASE_TAG = "release-<run date>"
SLA_KEYS = ("refresh", "release", "support")
# From most to least frequent; a port promises no more than its slowest source.
REFRESH = ("daily", "weekly", "monthly", "quarterly", "annual", "irregular")
# Licence ids a maintainer has reviewed as allowing a stored copy in a private Hugging Face dataset. A source
# read live only (its licence forbids redistribution, live.is_live) is never stored, whatever its id.
PRIVATE_COPY_LICENCES = REDISTRIBUTABLE_LICENCES
DATA_KINDS = ("surveillance", "programme")
UNRESOLVED = "unresolved"
_PORT_NAME = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


@dataclass(frozen=True)
class Port:
    product_id: str
    product_version: str
    name: str
    contract_id: str
    version: str
    hosted: bool
    # Set when two hub ports serve this contract: the plain-words reason its product and port can't be named.
    conflict: str = ""


@dataclass(frozen=True)
class PendingInput:
    """A source a port will read once it is catalogued (e.g. on an unmerged pull request)."""

    id: str
    licence: str
    fills_from: str


@dataclass(frozen=True)
class OutputPort:
    """One output port with the properties this project requires of every port."""

    product_id: str
    name: str
    version: str
    contract_id: str
    tier: str
    hosting: str
    release_tag: str
    sla: Mapping[str, str]
    feedback: str
    licences: tuple[str, ...]
    inputs: tuple[str, ...]
    pending: tuple[PendingInput, ...]
    stub: bool
    open_questions: tuple[str, ...]
    constraints: tuple[str, ...]
    custom: Mapping[str, Any] = field(repr=False, compare=False)

    @property
    def is_public(self) -> bool:
        return self.tier == "public"


@dataclass(frozen=True)
class Product:
    id: str
    version: str
    path: Path
    ports: tuple[OutputPort, ...]
    raw: Mapping[str, Any] = field(repr=False, compare=False)


def _custom(items: Any) -> dict[str, Any]:
    return {
        str(i["property"]): i.get("value") for i in items or [] if isinstance(i, dict) and "property" in i
    }


def _read(path: Path) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise RegistryError(f"{path.name}: not valid YAML: {e}") from e
    if not isinstance(doc, dict):
        raise RegistryError(f"{path.name}: a data product must be a YAML mapping")
    return doc


def load_ports(root: Path) -> dict[str, Port]:
    """The hub's output ports by contract id; empty when the registry has no hub file (e.g. a test registry).

    Only the hub's ports: the other products' ports are never published by `export-public`. A contract served by
    two ports is kept with `conflict` set, so provenance and export refuse that one source in plain words and
    every other source still works; `validate` reports it. A `hosted` that isn't exactly true counts as false.
    """
    path = root / PRODUCT_FILE
    if not path.is_file():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if doc.get("kind") != "DataProduct":
        raise ValueError(f"{path}: kind must be DataProduct")
    ports: dict[str, Port] = {}
    for p in doc.get("outputPorts") or []:
        cp = _custom(p.get("customProperties"))
        hosted = cp["hosted"] if "hosted" in cp else cp.get("hosting") == "public-hub"
        port = Port(
            product_id=str(doc.get("id", "")),
            product_version=str(doc.get("version", "")),
            name=str(p.get("name", "")),
            contract_id=str(p.get("contractId", "")),
            version=str(p.get("version", "")),
            hosted=hosted is True,
        )
        first = ports.get(port.contract_id)
        if first is not None:
            port = replace(
                first,
                conflict=f"two output ports of the data product, {first.name} and {port.name}, both claim this "
                "source, so which one it belongs to can't be said",
            )
        ports[port.contract_id] = port
    return ports


# ----------------------------------------------------------------------------------- the official schemas


def load_schema(spec: VendoredSchema, path: Path | None = None) -> dict[str, Any]:
    """A vendored Bitol JSON schema, refused unless its bytes are exactly the ones recorded."""
    path = path or spec.path
    try:
        data = path.read_bytes()
    except OSError as e:
        raise RegistryError(
            f"the vendored {spec.standard} schema is missing ({path.name}); run scripts/vendor_schemas.py"
        ) from e
    digest = hashlib.sha256(data).hexdigest()
    if digest != spec.sha256:
        raise RegistryError(
            f"the vendored {spec.standard} schema {path.name} has sha256 {digest}, not the recorded "
            f"{spec.sha256}; restore it with scripts/vendor_schemas.py"
        )
    schema: dict[str, Any] = json.loads(data)
    return schema


def odps_schema() -> dict[str, Any]:
    return load_schema(ODPS)


def odcs_schema() -> dict[str, Any]:
    return load_schema(ODCS)


def schema_errors(doc: Mapping[str, Any], schema: Mapping[str, Any] | None = None) -> list[str]:
    """Every place `doc` breaks the JSON schema (ODPS by default), as `path: message` lines; empty when it conforms."""
    import jsonschema

    validator = jsonschema.Draft201909Validator(
        schema or odps_schema(), format_checker=jsonschema.Draft201909Validator.FORMAT_CHECKER
    )
    errors = sorted(validator.iter_errors(dict(doc)), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(top)'}: {e.message}" for e in errors]


# ------------------------------------------------------------------------------------------ loading ports


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise RegistryError(f"{where} must be a list of non-empty texts")
    return tuple(value)


def _pending(value: Any, where: str) -> tuple[PendingInput, ...]:
    out: list[PendingInput] = []
    for item in value or []:
        if not isinstance(item, Mapping) or not all(
            str(item.get(k) or "").strip() for k in ("id", "licence", "fillsFrom")
        ):
            raise RegistryError(f"{where}: each pendingInputs entry needs id, licence and fillsFrom")
        out.append(PendingInput(str(item["id"]), str(item["licence"]), str(item["fillsFrom"])))
    return tuple(out)


def _port(product_id: str, p: Mapping[str, Any]) -> OutputPort:
    name = str(p.get("name", ""))
    where = f"{product_id}/{name}"
    cp = _custom(p.get("customProperties"))
    sla = cp.get("sla")
    if not isinstance(sla, Mapping) or any(not str(sla.get(k) or "").strip() for k in SLA_KEYS):
        raise RegistryError(f"{where}: customProperties.sla needs {', '.join(SLA_KEYS)}")
    inputs = tuple(str(i.get("id", "")) for i in p.get("inputContracts") or [] if isinstance(i, Mapping))
    context = p.get("context") or {}
    if not isinstance(context, Mapping):
        raise RegistryError(f"{where}: context must be a mapping (instructions, constraints)")
    constraints = tuple(
        str(c.get("constraint", "")) for c in context.get("constraints") or [] if isinstance(c, Mapping)
    )
    return OutputPort(
        product_id=product_id,
        name=name,
        version=str(p.get("version", "")),
        contract_id=str(p.get("contractId", "")),
        tier=str(cp.get("tier", "")),
        hosting=str(cp.get("hosting", "")),
        release_tag=str(cp.get("releaseTag") or ""),
        sla={k: str(sla[k]) for k in SLA_KEYS},
        feedback=str(cp.get("feedback") or ""),
        licences=_strings(cp.get("licences"), f"{where}: licences"),
        inputs=inputs or (str(p.get("contractId", "")),),
        pending=_pending(cp.get("pendingInputs"), where),
        stub=cp.get("stub") is True,
        open_questions=_strings(cp.get("openQuestions"), f"{where}: openQuestions"),
        constraints=constraints,
        custom=cp,
    )


def load_product(path: Path) -> Product:
    doc = _read(path)
    if doc.get("kind") != "DataProduct":
        raise RegistryError(f"{path.name}: kind must be DataProduct")
    pid = str(doc.get("id", ""))
    ports = tuple(_port(pid, p) for p in doc.get("outputPorts") or [] if isinstance(p, Mapping))
    return Product(id=pid, version=str(doc.get("version", "")), path=path, ports=ports, raw=doc)


def product_files(root: Path) -> list[Path]:
    return sorted((root / "contracts").glob(PRODUCT_GLOB))


def load_products(root: Path) -> dict[str, Product]:
    products: dict[str, Product] = {}
    for path in product_files(root):
        prod = load_product(path)
        if prod.id in products:
            raise RegistryError(f"{path.name}: product id {prod.id!r} is also used by another product file")
        products[prod.id] = prod
    return products


# --------------------------------------------------------------------------------------- port contracts


def port_contract(root: Path, registry: Registry, contract_id: str) -> Contract:
    """The ODCS contract a port serves: a catalogued source's own contract, or its file in contracts/ports/."""
    if contract_id in registry.datasets:
        return registry.get(contract_id).contract
    path = root / PORT_CONTRACTS / f"{contract_id}.odcs.yaml"
    if not path.is_file():
        raise RegistryError(
            f"port contract {contract_id} is neither a catalogued source's contract nor "
            f"{PORT_CONTRACTS}/{contract_id}.odcs.yaml"
        )
    doc = _read(path)
    if doc.get("id") != contract_id:
        raise RegistryError(f"{path.name}: contract id must be {contract_id}")
    return _parse_port_contract(doc, contract_id)


def _parse_port_contract(doc: Mapping[str, Any], contract_id: str) -> Contract:
    """A port contract as the registry's parser reads it, plus one kind only a port contract may use:
    unreliableRule kind unresolved (the publisher's rule isn't read yet). It keeps the port an unserved stub and
    is never read as "nothing is suppressed" (hard rules 1 and 4). A catalogued source's contract never may."""
    custom = doc.get("customProperties")
    rule = _custom(custom).get("unreliableRule")
    if not (isinstance(rule, Mapping) and rule.get("kind") == UNRESOLVED):
        return parse_contract(doc, expected_id=contract_id)
    desc = str(rule.get("description") or "").strip()
    if not desc or rule.get("threshold") is not None:
        raise RegistryError(
            f"{contract_id}: an unresolved unreliableRule needs a description and no threshold"
        )
    stand_in = {"kind": "none", "description": desc}
    parsed = parse_contract(
        {
            **doc,
            "customProperties": [
                {**c, "value": stand_in}
                if isinstance(c, Mapping) and c.get("property") == "unreliableRule"
                else c
                for c in custom or []
            ],
        },
        expected_id=contract_id,
    )
    return replace(parsed, raw=doc, unreliable=UnreliableSpec(UNRESOLVED, desc))


def _columns(doc: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        prop
        for obj in doc.get("schema") or []
        if isinstance(obj, Mapping)
        for prop in obj.get("properties") or []
        if isinstance(prop, Mapping)
    ]


def private_columns(contract: Contract, *, untiered_is_private: bool = True) -> list[str]:
    """Columns not cleared for a public port: marked private tier (e.g. abstract text under a restrictive
    licence) or, in a port contract, declaring no tier at all: such a column is private until a reviewed change
    says public. A catalogued source's own contract is cleared by its licence, so only `tier: private` counts."""
    out = []
    for prop in _columns(contract.raw):
        tier = _custom(prop.get("customProperties")).get("tier")
        if tier == "private" or (untiered_is_private and tier != "public"):
            out.append(str(prop.get("name")))
    return out


def inherited_licences(port: OutputPort, registry: Registry) -> tuple[str, ...]:
    """The licence ids of every source the port reads, catalogued or still pending, sorted and distinct."""
    ids = {registry.get(i).entry.licence.id for i in port.inputs if i in registry.datasets}
    ids |= {p.licence for p in port.pending}
    return tuple(sorted(ids))


def inherited_context(port: OutputPort, registry: Registry) -> list[str]:
    """What travels with every answer from this port: its own constraints, then each catalogued source's
    licence terms and caveats, then the open questions of a draft stub."""
    lines = list(port.constraints)
    for i in port.inputs:
        if i in registry.datasets:
            e = registry.get(i).entry
            lines.append(f"{e.id}: licence {e.licence.id} ({e.licence.terms_url})")
            lines.extend(f"{e.id}: {c}" for c in e.caveats)
    lines.extend(f"pending {p.id} ({p.fills_from}): licence {p.licence}" for p in port.pending)
    lines.extend(f"open question: {q}" for q in port.open_questions)
    return lines


# ------------------------------------------------------------------- port contracts: fingerprints and rules

# The shape a consumer codes against. A change here is a major version.
_INTERFACE_KEYS = ("name", "physicalType", "logicalType", "required", "primaryKey", "primaryKeyPosition")
# Recorded history and review state: they don't change what a column means.
_NOT_MEANING = frozenset({"fingerprints", "openQuestions"})
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SEVERITIES = ("error", "warn")
# The conformed dimensions the silver stage builds (feat/contract-silver, stage c). Port contracts declare keys
# into them; they never build the gazetteer or the concept mappings themselves.
CONFORMED_DIMENSIONS = ("dim_concept", "dim_place", "dim_time")
TAG_STATES = ("proposed", "accepted", "rejected")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def interface_fingerprint(doc: Mapping[str, Any]) -> str:
    """sha256 of the contract's shape: each table and each column's name, types, required and key."""
    shape = [
        {
            "name": obj.get("name"),
            "properties": [{k: p.get(k) for k in _INTERFACE_KEYS} for p in obj.get("properties") or []],
        }
        for obj in doc.get("schema") or []
    ]
    return _digest(shape)


def docs_fingerprint(doc: Mapping[str, Any]) -> str:
    """sha256 of what the contract means: every description, rule and property, but not its version, status,
    open questions or fingerprint history."""
    meaning = {k: v for k, v in doc.items() if k not in ("version", "status", "customProperties")}
    meaning["customProperties"] = [
        c
        for c in doc.get("customProperties") or []
        if isinstance(c, Mapping) and c.get("property") not in _NOT_MEANING
    ]
    return _digest(meaning)


def _semver(v: str) -> tuple[int, int, int] | None:
    m = _SEMVER.fullmatch(str(v))
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def fingerprint_errors(doc: Mapping[str, Any], where: str) -> list[str]:
    """The version rule: a new major for a change of shape, a new minor for a change of meaning.

    customProperties.fingerprints is the history, one {version, interface, docs} per released version; its last
    entry must be this version and match the contract as it stands.
    """
    history = _custom(doc.get("customProperties")).get("fingerprints")
    if not isinstance(history, list) or not history or not all(isinstance(h, Mapping) for h in history):
        return [f"{where}: customProperties.fingerprints needs at least one {{version, interface, docs}}"]
    errs: list[str] = []
    last = history[-1]
    if str(last.get("version")) != str(doc.get("version")):
        errs.append(
            f"{where}: the last fingerprints entry is for {last.get('version')}, not version {doc.get('version')}"
        )
    if last.get("interface") != interface_fingerprint(doc):
        errs.append(f"{where}: its shape changed; bump the major version and add a fingerprints entry")
    elif last.get("docs") != docs_fingerprint(doc):
        errs.append(f"{where}: its meaning changed; bump the minor version and add a fingerprints entry")
    for a, b in zip(history, history[1:], strict=False):
        va, vb = _semver(str(a.get("version"))), _semver(str(b.get("version")))
        if va is None or vb is None:
            errs.append(f"{where}: fingerprint versions must be MAJOR.MINOR.PATCH")
            continue
        if a.get("interface") != b.get("interface") and vb[0] <= va[0]:
            errs.append(f"{where}: {b.get('version')} changed the shape, so it must be a new major version")
        elif a.get("docs") != b.get("docs") and vb[:2] <= va[:2]:
            errs.append(
                f"{where}: {b.get('version')} changed the meaning, so it must be at least a new minor"
            )
        elif vb <= va:
            errs.append(f"{where}: fingerprint versions must increase")
    return errs


@dataclass(frozen=True)
class QualityRule:
    name: str
    dbt_test: str
    dimension: str
    severity: str
    column: str | None


def quality_rules(doc: Mapping[str, Any]) -> list[QualityRule]:
    """Every quality rule in the contract, table-level and column-level, as the silver builder generates them."""
    rules: list[QualityRule] = []
    for obj in doc.get("schema") or []:
        if not isinstance(obj, Mapping):
            continue
        holders = [(None, obj)] + [(str(p.get("name")), p) for p in _columns({"schema": [obj]})]
        for column, holder in holders:
            for q in holder.get("quality") or []:
                if not isinstance(q, Mapping):
                    raise RegistryError(f"quality rules on {column or 'the table'} must each be a mapping")
                rules.append(
                    QualityRule(
                        name=str(q.get("name") or ""),
                        dbt_test=str(_custom(q.get("customProperties")).get("dbtTest") or ""),
                        dimension=str(q.get("dimension") or ""),
                        severity=str(q.get("severity") or ""),
                        column=column,
                    )
                )
    return rules


def quality_errors(doc: Mapping[str, Any], where: str) -> list[str]:
    rules = quality_rules(doc)
    errs = [] if rules else [f"{where}: a port contract declares its quality rules"]
    if rules and not any(r.severity == "error" for r in rules):
        errs.append(
            f"{where}: a port contract needs at least one error-severity rule; it is served only when they pass"
        )
    names = [r.name for r in rules]
    for r in rules:
        if not r.name or not r.dbt_test or not r.dimension or r.severity not in SEVERITIES:
            errs.append(
                f"{where}: quality rule {r.name or '(unnamed)'} needs a name, a dbtTest, a dimension and a "
                f"severity ({' or '.join(SEVERITIES)})"
            )
    errs.extend(
        f"{where}: quality rule name {n} is used twice"
        for n in sorted({n for n in names if names.count(n) > 1})
    )
    return errs


# One quality rule's outcome. failed: the data broke the rule (a contract finding about the publisher's data).
# error: the rule itself couldn't run, e.g. a renamed column (a code fix). not_run: no result was recorded.
OUTCOMES = ("passed", "failed", "error", "not_run")


def rule_report(doc: Mapping[str, Any], results: Mapping[str, str]) -> dict[str, list[str]]:
    """Each rule's name under its outcome, so a data failure is never confused with a rule that couldn't run.

    A rule with no result, or an outcome outside OUTCOMES, is reported as not_run.
    """
    report: dict[str, list[str]] = {o: [] for o in OUTCOMES}
    for r in quality_rules(doc):
        outcome = results.get(r.name)
        report[outcome if outcome in OUTCOMES else "not_run"].append(r.name)
    return report


def servable(doc: Mapping[str, Any], results: Mapping[str, str]) -> bool:
    """A port is served only when every error-severity rule ran and passed; failed, error and not_run all fail
    closed. A contract with no error-severity rule is never served, and `quality_errors` refuses it."""
    errors = [r.name for r in quality_rules(doc) if r.severity == "error"]
    return bool(errors) and all(results.get(n) == "passed" for n in errors)


def conformed_key_errors(doc: Mapping[str, Any], where: str) -> list[str]:
    """Conformed keys name a silver dimension; a dictionary-proposed tag carries its own review state."""
    errs: list[str] = []
    for obj in doc.get("schema") or []:
        props = {str(p.get("name")): p for p in _columns({"schema": [obj]})}
        for name, p in props.items():
            key = _custom(p.get("customProperties")).get("conformedKey")
            if key is None:
                continue
            if not isinstance(key, Mapping) or key.get("dimension") not in CONFORMED_DIMENSIONS:
                errs.append(
                    f"{where}.{name}: conformedKey.dimension must be one of {', '.join(CONFORMED_DIMENSIONS)}"
                )
                continue
            if key.get("assignment") == "dictionary_proposed":
                state = str(key.get("reviewStateColumn") or "")
                allowed = [
                    (q.get("arguments") or {}).get("validValues")
                    for q in (props.get(state) or {}).get("quality") or []
                    if isinstance(q, Mapping) and q.get("metric") == "invalidValues"
                ]
                if state not in props or list(TAG_STATES) not in allowed:
                    errs.append(
                        f"{where}.{name}: a dictionary-proposed tag needs a reviewStateColumn whose values are "
                        f"checked to be {', '.join(TAG_STATES)} (rules 1 and 9: proposed is never a fact)"
                    )
    return errs


def port_contract_errors(path: Path) -> list[str]:
    """The rules every file in contracts/ports/ follows beyond the ODCS schema."""
    doc = _read(path)
    where = path.name
    try:
        errs = fingerprint_errors(doc, where) + quality_errors(doc, where) + conformed_key_errors(doc, where)
    except RegistryError as e:
        return [f"{where}: {e}"]
    except (AttributeError, TypeError, ValueError) as e:
        return [f"{where}: a field has the wrong type or is empty where a value is needed ({e})"]
    custom = _custom(doc.get("customProperties"))
    if custom.get("tier") not in TIERS:
        errs.append(f"{where}: customProperties.tier must be one of {', '.join(TIERS)}")
    untiered = [
        str(c.get("name"))
        for c in _columns(doc)
        if _custom(c.get("customProperties")).get("tier") not in TIERS
    ]
    if untiered:
        errs.append(
            f"{where}: every column declares its tier (public or private); missing on {', '.join(untiered)}"
        )
    rule = custom.get("unreliableRule")
    if isinstance(rule, Mapping) and rule.get("kind") == UNRESOLVED:
        if doc.get("status") != "draft":
            errs.append(f"{where}: an unresolved unreliableRule keeps the contract draft")
        if not any("suppress" in str(q).lower() for q in custom.get("openQuestions") or []):
            errs.append(
                f"{where}: an unresolved unreliableRule is listed in openQuestions (the suppression rule)"
            )
    return errs


def contract_files(root: Path) -> list[Path]:
    return sorted((root / "contracts").glob("*.odcs.yaml")) + sorted(
        (root / PORT_CONTRACTS).glob("*.odcs.yaml")
    )


# ---------------------------------------------------------------------------------------------- checks


def _port_errors(root: Path, registry: Registry, port: OutputPort, private_ports: set[str]) -> list[str]:
    where = f"{port.product_id}/{port.name}"
    errs: list[str] = []
    if not _PORT_NAME.fullmatch(port.name):
        errs.append(f"{where}: port name must be snake_case")
    if port.tier not in TIERS:
        errs.append(f"{where}: tier must be one of {', '.join(TIERS)}")
    if port.hosting not in HOSTINGS:
        errs.append(f"{where}: hosting must be one of {', '.join(HOSTINGS)}")
    if port.feedback != FEEDBACK_URL:
        errs.append(f"{where}: feedback must be the contract-finding issue form ({FEEDBACK_URL})")
    if port.hosting in ("public-hub", "private-hf") and port.release_tag != RELEASE_TAG:
        errs.append(f"{where}: a hosted port's releaseTag is {RELEASE_TAG}")
    if not port.release_tag:
        errs.append(f"{where}: every port needs a releaseTag")
    if port.sla["refresh"] not in REFRESH:
        errs.append(f"{where}: sla.refresh must be one of {', '.join(REFRESH)}")
    if "hosted" in port.custom and not isinstance(port.custom["hosted"], bool):
        errs.append(f"{where}: hosted must be true or false, not {port.custom['hosted']!r}")
    elif "hosted" in port.custom and port.custom["hosted"] != (port.hosting == "public-hub"):
        errs.append(f"{where}: hosted must be true exactly when hosting is public-hub")

    # Sources: catalogued now, or pending with the pull request that fills them; never anything else.
    pending_ids = {p.id for p in port.pending}
    for i in port.inputs:
        if i not in registry.datasets and i not in pending_ids:
            errs.append(f"{where}: input {i} is not catalogued and not listed in pendingInputs")
    for p in port.pending:
        if p.id not in port.inputs:
            errs.append(f"{where}: pendingInputs {p.id} is not one of the port's inputContracts")
    if port.pending and not port.stub:
        errs.append(f"{where}: a port whose sources aren't all catalogued must be marked stub: true")
    if port.stub:
        if port.hosting != "none":
            errs.append(f"{where}: a stub port is not hosted (hosting: none)")
        if not port.open_questions:
            errs.append(f"{where}: a stub port lists its openQuestions")
    if port.sla["refresh"] in REFRESH:
        for i in port.inputs:
            if i in registry.datasets:
                refresh = registry.get(i).entry.refresh
                if REFRESH.index(port.sla["refresh"]) < REFRESH.index(refresh):
                    errs.append(
                        f"{where}: sla.refresh {port.sla['refresh']} promises more often than {i} refreshes "
                        f"({refresh}); a port refreshes no more often than its slowest source"
                    )

    # Licences are inherited, never declared freely.
    inherited = inherited_licences(port, registry)
    if tuple(sorted(set(port.licences))) != inherited:
        errs.append(
            f"{where}: licences {list(port.licences)} must be its sources' licences {list(inherited)}"
        )

    # The contract the port serves.
    try:
        contract = port_contract(root, registry, port.contract_id)
    except RegistryError as e:
        errs.append(f"{where}: {e}")
        contract = None
    if contract is not None:
        if contract.version != port.version:
            errs.append(f"{where}: version {port.version} differs from contract version {contract.version}")
        if port.stub and contract.status != "draft":
            errs.append(f"{where}: a stub port's contract stays draft")
        if contract.unreliable.kind == UNRESOLVED and not port.stub:
            errs.append(f"{where}: its suppression rule is unresolved, so it stays a stub and is not served")
        contract_tier = _custom(contract.raw.get("customProperties")).get("tier")
        if port.contract_id not in registry.datasets and contract_tier != port.tier:
            errs.append(f"{where}: tier {port.tier} differs from its contract's tier {contract_tier}")

    # Public versus private (hard rule 10).
    if port.tier == "private" and port.hosting == "public-hub":
        errs.append(f"{where}: a private-tier port is never on the public hub")
    # A catalogued source read live only (registry tier live: its licence forbids redistribution and it has no
    # reviewed private store) is never stored anywhere (live.is_live).
    live_only = [i for i in port.inputs if i in registry.datasets and registry.get(i).entry.tier == "live"]
    if port.is_public:
        read_private = [i for i in port.inputs if i in private_ports]
        if read_private:
            errs.append(f"{where}: a public port may not read private-tier ports ({', '.join(read_private)})")
        hidden = (
            private_columns(contract, untiered_is_private=port.contract_id not in registry.datasets)
            if contract is not None
            else []
        )
        if hidden:
            errs.append(
                f"{where}: a public port's contract has columns not cleared as public tier ({', '.join(hidden)})"
            )
        # Whatever its hosting (none and live included): a public port reads only sources licensed for a public
        # copy, so restricted terms (CROI, ESCMID, AIDSVu, WHO, no terms stated) never sit behind a public tier.
        closed = [x for x in inherited if x not in REDISTRIBUTABLE_LICENCES or x.startswith(UNRESOLVED)]
        if closed or live_only:
            errs.append(
                f"{where}: a public port reads only sources licensed for a public copy; make it private tier "
                f"(restricted or unresolved: {', '.join(sorted(set(closed) | set(live_only)))})"
            )
        if port.hosting == "public-hub":
            not_public = [
                i for i in port.inputs if i in registry.datasets and not registry.get(i).entry.hosted
            ]
            if not_public or port.pending:
                errs.append(
                    f"{where}: on the public hub only when every source is catalogued with hosting: public "
                    f"(its catalog entry's hosted is true) (not: {', '.join(not_public) or 'pending sources'})"
                )
    if port.hosting == "private-hf":
        # A catalogued source may be stored privately when its own catalog entry says so after review (a private
        # store, registry tier private) or its licence is reviewed for a stored copy; never when it is live only.
        # A pending source only by its licence, since no reviewed catalog entry says anything yet.
        no_copy = [
            i
            for i in port.inputs
            if i in registry.datasets
            and (
                registry.get(i).entry.tier == "live"
                or (
                    registry.get(i).entry.tier != "private"
                    and registry.get(i).entry.licence.id not in PRIVATE_COPY_LICENCES
                )
            )
        ]
        no_copy += [p.licence for p in port.pending if p.licence not in PRIVATE_COPY_LICENCES]
        if no_copy:
            errs.append(
                f"{where}: stored in a private Hugging Face dataset only when every source may be stored (a "
                f"reviewed private store or a licence that allows a stored copy); use hosting live or none "
                f"(not: {', '.join(sorted(set(no_copy)))})"
            )
    return errs


def _product_errors(product: Product) -> list[str]:
    errs: list[str] = []
    where = product.path.name
    if product.raw.get("apiVersion") != ODPS_API_VERSION:
        errs.append(f"{where}: apiVersion must be {ODPS_API_VERSION}, the vendored schema's version")
    names = [p.name for p in product.ports]
    if len(set(names)) != len(names):
        errs.append(f"{where}: output port names must be unique")
    if not product.ports:
        errs.append(f"{where}: a product needs at least one output port")
    custom = _custom(product.raw.get("customProperties"))
    if custom.get("combine") == "grouped-never-merged":
        # Each source stays its own series with its own counted unit.
        for p in product.ports:
            if len(p.inputs) != 1:
                errs.append(f"{where}/{p.name}: in a grouped product each port reads exactly one source")
            if not str(p.custom.get("countedUnit") or "").strip():
                errs.append(f"{where}/{p.name}: in a grouped product each port declares its countedUnit")
            kind = str(p.custom.get("dataKind") or "")
            if kind not in DATA_KINDS and not kind.startswith(UNRESOLVED):
                errs.append(
                    f"{where}/{p.name}: in a grouped product each port declares its dataKind "
                    f"({', '.join(DATA_KINDS)}, or unresolved: ...)"
                )
    return errs


def _data_kind_errors(root: Path, registry: Registry, port: OutputPort) -> list[str]:
    """A port's dataKind and its contract's dataKind say the same (both unresolved counts as the same)."""
    if "dataKind" not in port.custom or port.contract_id in registry.datasets:
        return []
    try:
        contract = port_contract(root, registry, port.contract_id)
    except RegistryError:
        return []  # _port_errors reports it
    ours = str(port.custom.get("dataKind") or "")
    theirs = str(_custom(contract.raw.get("customProperties")).get("dataKind") or "")
    if ours == theirs or (ours.startswith(UNRESOLVED) and theirs.startswith(UNRESOLVED)):
        return []
    return [
        f"{port.product_id}/{port.name}: dataKind {ours!r} differs from its contract's dataKind {theirs!r}"
    ]


def validate_contracts(root: Path, schema: Mapping[str, Any] | None = None) -> list[str]:
    """Every ODCS contract against the official ODCS schema, and every port contract against this project's
    port rules (fingerprints and version rule, quality rules for dbt, conformed keys, tier)."""
    errs: list[str] = []
    if schema is None:
        try:
            schema = odcs_schema()
        except RegistryError as e:
            errs.append(str(e))
    for path in contract_files(root):
        try:
            doc = _read(path)
        except RegistryError as e:
            errs.append(str(e))
            continue
        if doc.get("apiVersion") != ODCS_API_VERSION:
            errs.append(f"{path.name}: apiVersion must be {ODCS_API_VERSION}, the vendored schema's version")
        if schema is not None:
            errs.extend(f"{path.name}: {m}" for m in schema_errors(doc, schema))
        if path.parent.name == PORT_CONTRACTS.name:
            errs.extend(port_contract_errors(path))
    return errs


def validate_products(root: Path, registry: Registry, schema: Mapping[str, Any] | None = None) -> list[str]:
    """Every problem with every product file and contract, as lines; empty when all pass."""
    errs = validate_contracts(root)
    if schema is None:
        try:
            schema = odps_schema()
        except RegistryError as e:
            errs.append(str(e))
    products: list[Product] = []
    for path in product_files(root):
        try:
            doc = _read(path)
        except RegistryError as e:
            errs.append(str(e))
            continue
        if schema is not None:
            errs.extend(f"{path.name}: {m}" for m in schema_errors(doc, schema))
        try:
            products.append(load_product(path))
        except RegistryError as e:
            errs.append(str(e))
        except (AttributeError, TypeError, ValueError) as e:
            errs.append(f"{path.name}: a field has the wrong type or is empty where a value is needed ({e})")
    ids = [p.id for p in products]
    errs.extend(
        f"product id {i} is used by more than one file" for i in sorted({i for i in ids if ids.count(i) > 1})
    )
    served: dict[str, str] = {}
    for prod in products:
        for p in prod.ports:
            here = f"{prod.path.name} ({p.name})"
            if p.contract_id in served:
                errs.append(
                    f"contract {p.contract_id} is served by two ports: {served[p.contract_id]} and {here}"
                )
            else:
                served[p.contract_id] = here
    private_ports = {p.contract_id for prod in products for p in prod.ports if p.tier == "private"}
    private_ports |= {p.name for prod in products for p in prod.ports if p.tier == "private"}
    for prod in products:
        errs.extend(_product_errors(prod))
        for port in prod.ports:
            try:
                errs.extend(_port_errors(root, registry, port, private_ports))
                errs.extend(_data_kind_errors(root, registry, port))
            except (AttributeError, TypeError, ValueError) as e:
                errs.append(
                    f"{prod.path.name} ({port.name}): a field has the wrong type or is empty where a value is "
                    f"needed ({e})"
                )
    return errs


def summary(products: Sequence[Product]) -> list[str]:
    """One line per port, for `validate`'s output."""
    return [
        f"ok   {prod.id}/{p.name}: {p.tier} tier, hosting {p.hosting}{', draft stub' if p.stub else ''}"
        for prod in products
        for p in prod.ports
    ]
