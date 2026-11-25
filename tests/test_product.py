"""ODPS data products: the vendored schemas, the port rules, licence inheritance and public versus private.

Synthetic registries only (tests/conftest.py's toy source); the real product files are checked as they stand.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from health_data_registry import cli, product
from health_data_registry.product import (
    FEEDBACK_URL,
    ODCS,
    ODPS,
    docs_fingerprint,
    fingerprint_errors,
    interface_fingerprint,
    load_ports,
    load_schema,
    quality_rules,
    schema_errors,
    servable,
    validate_products,
)
from health_data_registry.registry import RegistryError, load_registry
from tests.test_publish import write_hub_registry

REPO = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------------------------- helpers


def port(name: str, contract: str, **custom: Any) -> dict[str, Any]:
    inputs = custom.pop("inputs", None)
    props: dict[str, Any] = {
        "tier": "public",
        "hosting": "public-hub",
        "releaseTag": "release-<run date>",
        "sla": {"refresh": "weekly", "release": "next weekly release", "support": "best effort"},
        "feedback": FEEDBACK_URL,
        "licences": ["public-domain-us-federal"],
    }
    props.update(custom)
    out: dict[str, Any] = {
        "name": name,
        "type": "tables",
        "version": "0.1.0",
        "contractId": contract,
        "customProperties": [{"property": k, "value": v} for k, v in props.items()],
    }
    if inputs:
        out["inputContracts"] = [{"id": i, "version": "0.1.0"} for i in inputs]
    return out


def product_doc(pid: str, *ports: dict[str, Any], combine: str | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "apiVersion": "v1.1.0",
        "kind": "DataProduct",
        "id": pid,
        "name": pid,
        "version": "0.1.0",
        "outputPorts": list(ports),
    }
    if combine:
        doc["customProperties"] = [{"property": "combine", "value": combine}]
    return doc


def port_contract(cid: str, *, tier: str = "public", private_col: bool = False) -> dict[str, Any]:
    def col(name: str, **extra: Any) -> dict[str, Any]:
        extra.setdefault("tier", "public")
        c: dict[str, Any] = {
            "name": name,
            "physicalType": "VARCHAR",
            "logicalType": "string",
            "required": True,
            "description": f"The {name} as published.",
            "customProperties": [{"property": "role", "value": "attribute"}],
            "quality": [
                {
                    "name": f"{name}_not_null",
                    "type": "library",
                    "metric": "nullValues",
                    "mustBe": 0,
                    "dimension": "completeness",
                    "severity": "error",
                    "customProperties": [{"property": "dbtTest", "value": "not_null"}],
                }
            ],
        }
        c["customProperties"] += [{"property": k, "value": v} for k, v in extra.items()]
        return c

    cols = [col("title")]
    if private_col:
        cols.append(col("abstract_text", tier="private"))
    doc: dict[str, Any] = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": cid,
        "version": "0.1.0",
        "status": "draft",
        "schema": [
            {
                "name": cid,
                "physicalType": "table",
                "customProperties": [{"property": "totalRows", "value": "none"}],
                "properties": cols,
            }
        ],
        "customProperties": [
            {"property": "tier", "value": tier},
            {"property": "unreliableRule", "value": {"kind": "none", "description": "None stated."}},
        ],
    }
    return stamped(doc)


def stamped(doc: dict[str, Any], history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    doc = copy.deepcopy(doc)
    entry = {
        "version": doc["version"],
        "interface": interface_fingerprint(doc),
        "docs": docs_fingerprint(doc),
    }
    props = [c for c in doc["customProperties"] if c["property"] != "fingerprints"]
    doc["customProperties"] = props + [{"property": "fingerprints", "value": [*(history or []), entry]}]
    return doc


def registry_root(tmp_path: Path) -> Path:
    root = write_hub_registry(tmp_path / "reg")
    # The toy hub file predates the port rules; replace it with a product that follows them.
    (root / "contracts" / "health-data-registry.odps.yaml").write_text(
        yaml.safe_dump(product_doc("toy-hub", port("toy_cases", "toy_cases", hosted=True)))
    )
    (root / "contracts" / "ports").mkdir()
    return root


def write(root: Path, doc: dict[str, Any], name: str) -> None:
    (root / "contracts" / name).write_text(yaml.safe_dump(doc, sort_keys=False))


def errors(root: Path) -> list[str]:
    return validate_products(root, load_registry(root))


# ------------------------------------------------------------------------------------- vendored schemas


def test_vendored_schemas_match_their_recorded_hashes() -> None:
    for spec in (ODPS, ODCS):
        assert hashlib.sha256(spec.path.read_bytes()).hexdigest() == spec.sha256
        assert spec.url.startswith("https://raw.githubusercontent.com/bitol-io/")
        assert spec.api_version in spec.url
    assert load_schema(ODPS)["title"].startswith("Open Data Product Standard")
    assert load_schema(ODCS)["title"].startswith("Open Data Contract Standard")


def test_a_changed_schema_file_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "odps.json"
    bad.write_bytes(ODPS.path.read_bytes() + b" ")
    with pytest.raises(RegistryError, match="sha256"):
        load_schema(ODPS, bad)
    with pytest.raises(RegistryError, match="missing"):
        load_schema(ODPS, tmp_path / "nope.json")


def test_the_repo_products_and_contracts_pass() -> None:
    reg = load_registry()
    assert validate_products(reg.root, reg) == []
    products = product.load_products(reg.root)
    assert set(products) == {"health-data-registry-hub", "id-conference-abstracts", "michigan-hiv"}


def test_schema_errors_name_the_place() -> None:
    doc = product_doc("x", port("a", "a"))
    assert schema_errors(doc) == []
    doc["owner"] = "someone"  # not an ODPS field
    doc["outputPorts"][0]["customProperties"].append({"property": "no-value"})
    msgs = schema_errors(doc)
    assert any(m.startswith("(top)") and "owner" in m for m in msgs)
    assert any(m.startswith("outputPorts/0/customProperties") for m in msgs)


def test_odcs_schema_rejects_an_unknown_server_type(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    p = root / "contracts" / "toy_cases.odcs.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["servers"] = [{"server": "x.gov", "type": "https"}]
    write(root, doc, "toy_cases.odcs.yaml")
    assert any("toy_cases.odcs.yaml: servers/0/type" in e for e in errors(root))


def test_api_versions_are_the_vendored_ones(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    p = root / "contracts" / "toy_cases.odcs.yaml"
    p.write_text(p.read_text().replace("apiVersion: v3.2.0", "apiVersion: v3.0.2"))
    doc = product_doc("toy-hub", port("toy_cases", "toy_cases"))
    doc["apiVersion"] = "v1.0.0"
    write(root, doc, "health-data-registry.odps.yaml")
    errs = errors(root)
    assert any("apiVersion must be v3.2.0" in e for e in errs)
    assert any("apiVersion must be v1.1.0" in e for e in errs)


# -------------------------------------------------------------------------------------- the port rules


def test_a_good_toy_product_passes(tmp_path: Path) -> None:
    assert errors(registry_root(tmp_path)) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"tier": "secret"}, "tier must be one of"),
        ({"hosting": "s3"}, "hosting must be one of"),
        ({"feedback": "https://example.org"}, "contract-finding issue form"),
        ({"releaseTag": "v1"}, "releaseTag is release-<run date>"),
        ({"releaseTag": ""}, "needs a releaseTag"),
        ({"sla": {"refresh": "hourly", "release": "x", "support": "y"}}, "sla.refresh must be one of"),
        ({"sla": {"refresh": "daily", "release": "x", "support": "y"}}, "promises more often than toy_cases"),
        ({"hosted": "true"}, "hosted must be true or false, not 'true'"),
        ({"hosted": False}, "hosted must be true exactly when"),
    ],
)
def test_every_port_names_tier_hosting_release_sla_and_feedback(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    root = registry_root(tmp_path)
    write(
        root,
        product_doc("toy-hub", port("toy_cases", "toy_cases", **change)),
        "health-data-registry.odps.yaml",
    )
    assert any(message in e for e in errors(root)), errors(root)


def test_a_missing_sla_is_refused(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(
        root,
        product_doc("toy-hub", port("toy_cases", "toy_cases", sla={"refresh": "weekly"})),
        "health-data-registry.odps.yaml",
    )
    assert any("customProperties.sla needs refresh, release, support" in e for e in errors(root))


def test_ports_inherit_their_sources_licences(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(root, port_contract("toy_index"), "ports/toy_index.odcs.yaml")
    good = port("toy_index", "toy_index", inputs=["toy_cases"])
    write(root, product_doc("toys", good), "toys.odps.yaml")
    assert errors(root) == []
    wrong = port("toy_index", "toy_index", inputs=["toy_cases"], licences=["CC-BY-4.0"])
    write(root, product_doc("toys", wrong), "toys.odps.yaml")
    assert any("must be its sources' licences ['public-domain-us-federal']" in e for e in errors(root))
    # The port's answers carry each source's licence and caveats, after the port's own constraints.
    reg = load_registry(root)
    p = product.load_products(root)["toys"].ports[0]
    ctx = product.inherited_context(p, reg)
    assert (
        "toy_cases: licence public-domain-us-federal (https://www.cdc.gov/other/agencymaterials.html)" in ctx
    )
    assert "toy_cases: Counts for the latest year are provisional." in ctx


def test_restricted_sources_never_reach_the_public_hub(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(root, port_contract("toy_index"), "ports/toy_index.odcs.yaml")
    p = port("toy_index", "toy_index", inputs=["toy_listed"], licences=["CC-BY-NC-SA-3.0-IGO"])
    write(root, product_doc("toys", p), "toys.odps.yaml")
    assert any("public copy" in e and "CC-BY-NC-SA-3.0-IGO" in e for e in errors(root))
    # Listed, not hosted: allowed only as a private-tier port.
    p = port(
        "toy_index",
        "toy_index",
        inputs=["toy_listed"],
        licences=["CC-BY-NC-SA-3.0-IGO"],
        hosting="live",
        tier="private",
    )
    write(root, port_contract("toy_index", tier="private"), "ports/toy_index.odcs.yaml")
    write(root, product_doc("toys", p), "toys.odps.yaml")
    assert errors(root) == []


@pytest.mark.parametrize("hosting", ["none", "live", "public-hub", "private-hf"])
@pytest.mark.parametrize(
    ("source", "licence"),
    [
        ("croi_abstracts_text", "croi-abstract-text"),
        ("escmid_abstracts_text", "escmid-abstract-terms"),
        ("aidsvu_michigan", "unresolved: AIDSVu terms not yet read"),
        ("croi_abstracts_index", "croi-metadata-index"),
    ],
)
def test_a_public_port_with_a_restricted_input_fails_whatever_its_hosting(
    tmp_path: Path, hosting: str, source: str, licence: str
) -> None:
    root = registry_root(tmp_path)
    write(root, port_contract("toy_index"), "ports/toy_index.odcs.yaml")
    p = port(
        "toy_index",
        "toy_index",
        inputs=["toy_cases", source],
        licences=[licence, "public-domain-us-federal"],
        hosting=hosting,
        stub=True,
        openQuestions=["q"],
        pendingInputs=[{"id": source, "licence": licence, "fillsFrom": "PR #30"}],
    )
    write(root, product_doc("toys", p), "toys.odps.yaml")
    assert (
        f"toys/toy_index: a public port reads only sources licensed for a public copy; make it private tier "
        f"(restricted or unresolved: {licence})"
    ) in errors(root)


def test_a_public_port_reading_a_catalogued_live_only_source_fails(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(root, port_contract("toy_index"), "ports/toy_index.odcs.yaml")
    p = port(
        "toy_index", "toy_index", inputs=["toy_listed"], licences=["CC-BY-NC-SA-3.0-IGO"], hosting="none"
    )
    write(root, product_doc("toys", p), "toys.odps.yaml")
    assert errors(root) == [
        "toys/toy_index: a public port reads only sources licensed for a public copy; make it private tier "
        "(restricted or unresolved: CC-BY-NC-SA-3.0-IGO, toy_listed)"
    ]


def test_a_never_stored_source_is_never_given_private_hf_hosting(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(root, port_contract("toy_text", tier="private"), "ports/toy_text.odcs.yaml")
    # toy_listed is read live only (its licence forbids redistribution), like WHO and the Detroit dashboards.
    live = port(
        "toy_text",
        "toy_text",
        inputs=["toy_listed"],
        licences=["CC-BY-NC-SA-3.0-IGO"],
        tier="private",
        hosting="private-hf",
    )
    write(root, product_doc("toys", live), "toys.odps.yaml")
    assert errors(root) == [
        "toys/toy_text: stored in a private Hugging Face dataset only when every source may be stored (a reviewed "
        "private store or a licence that allows a stored copy); use hosting live or none (not: toy_listed)"
    ]
    # The same port read live, or not built yet, is fine.
    for hosting in ("live", "none"):
        ok = port(
            "toy_text",
            "toy_text",
            inputs=["toy_listed"],
            licences=["CC-BY-NC-SA-3.0-IGO"],
            tier="private",
            hosting=hosting,
        )
        write(root, product_doc("toys", ok), "toys.odps.yaml")
        assert errors(root) == [], hosting
    # A pending source whose licence isn't reviewed for a stored copy (CROI text) can't be planned there either.
    assert "croi-abstract-text" not in product.PRIVATE_COPY_LICENCES
    croi = port(
        "toy_text",
        "toy_text",
        inputs=["croi_abstracts_text"],
        licences=["croi-abstract-text"],
        tier="private",
        hosting="private-hf",
        pendingInputs=[{"id": "croi_abstracts_text", "licence": "croi-abstract-text", "fillsFrom": "PR #30"}],
        stub=True,
        openQuestions=["q"],
    )
    write(root, product_doc("toys", croi), "toys.odps.yaml")
    assert any("(not: croi-abstract-text)" in e for e in errors(root))
    # A public-domain source may be stored privately.
    pd = port("toy_text", "toy_text", inputs=["toy_cases"], tier="private", hosting="private-hf")
    write(root, product_doc("toys", pd), "toys.odps.yaml")
    assert errors(root) == []
    # So may a restricted source whose reviewed catalog entry keeps it in its own private store.
    cat = root / "catalog" / "toy_listed.yaml"
    cat.write_text(cat.read_text() + "private_store: true\nhosting: private\n")
    contract = root / "contracts" / "toy_listed.odcs.yaml"
    contract.write_text(
        contract.read_text().replace(
            "customProperties:\n", "customProperties:\n  - {property: tier, value: private}\n", 1
        )
    )
    assert load_registry(root).get("toy_listed").entry.tier == "private"
    write(root, product_doc("toys", live), "toys.odps.yaml")
    assert errors(root) == []


def test_no_public_port_exposes_private_tier_data(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(root, port_contract("toy_text", tier="private", private_col=True), "ports/toy_text.odcs.yaml")
    write(root, port_contract("toy_index"), "ports/toy_index.odcs.yaml")
    private = port("toy_text", "toy_text", inputs=["toy_cases"], tier="private", hosting="private-hf")
    public = port("toy_index", "toy_index", inputs=["toy_cases"])
    write(root, product_doc("toys", private, public), "toys.odps.yaml")
    assert errors(root) == []

    # A public port reading the private port.
    public_reads_private = port(
        "toy_index",
        "toy_index",
        inputs=["toy_text"],
        hosting="none",
        stub=True,
        openQuestions=["q"],
        pendingInputs=[{"id": "toy_text", "licence": "public-domain-us-federal", "fillsFrom": "test"}],
    )
    write(root, product_doc("toys", private, public_reads_private), "toys.odps.yaml")
    assert any("may not read private-tier ports (toy_text)" in e for e in errors(root))

    # A public port whose contract carries a private-tier column, even unhosted.
    write(root, port_contract("toy_index", private_col=True), "ports/toy_index.odcs.yaml")
    write(
        root,
        product_doc("toys", private, port("toy_index", "toy_index", inputs=["toy_cases"], hosting="none")),
        "toys.odps.yaml",
    )
    assert any("columns not cleared as public tier (abstract_text)" in e for e in errors(root))

    # A column that declares no tier fails open nowhere: it counts as private, and validate names it.
    untiered = port_contract("toy_index")
    untiered["schema"][0]["properties"][0]["customProperties"] = [{"property": "role", "value": "attribute"}]
    write(root, stamped(untiered), "ports/toy_index.odcs.yaml")
    errs = errors(root)
    assert "toy_index.odcs.yaml: every column declares its tier (public or private); missing on title" in errs
    assert any("columns not cleared as public tier (title)" in e for e in errs)

    # A private port is never on the public hub.
    write(
        root,
        product_doc("toys", port("toy_text", "toy_text", inputs=["toy_cases"], tier="private")),
        "toys.odps.yaml",
    )
    assert any("private-tier port is never on the public hub" in e for e in errors(root))


def test_the_real_private_ports_stay_private() -> None:
    reg = load_registry()
    products = product.load_products(reg.root)
    for prod in products.values():
        for p in prod.ports:
            contract = product.port_contract(reg.root, reg, p.contract_id)
            if p.is_public:
                catalogued = p.contract_id in reg.datasets
                assert product.private_columns(contract, untiered_is_private=not catalogued) == [], p.name
            else:
                assert p.hosting != "public-hub", p.name
    tiers = {p.name: p.tier for prod in products.values() for p in prod.ports}
    assert tiers["id_abstracts_text"] == "private" and tiers["mi_aidsvu"] == "private"
    # Private until their sources' terms are read and allow a public copy (hard rule 10).
    for name in ("id_abstracts_index", "mi_mdhhs_hiv_sti", "mi_detroit_ryan_white", "mi_detroit_ehe"):
        assert tiers[name] == "private", name
    text = product.port_contract(reg.root, reg, "id_abstracts_text")
    assert product.private_columns(text) == ["abstract_text"]
    # The hub (what export-public publishes) never lists a port of the other products.
    # Private-store sources are not part of the public hub product (publish.public_sources).
    from health_data_registry.publish import public_sources

    assert set(load_ports(reg.root)) == {ds.id for ds in public_sources(reg)}


def test_stubs_are_draft_unhosted_and_name_what_fills_them(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(root, port_contract("toy_stub", tier="private"), "ports/toy_stub.odcs.yaml")
    pending = [{"id": "future_source", "licence": "unresolved: not read", "fillsFrom": "PR #99"}]
    good = port(
        "toy_stub",
        "toy_stub",
        inputs=["future_source"],
        hosting="none",
        stub=True,
        openQuestions=["Which file?"],
        pendingInputs=pending,
        licences=["unresolved: not read"],
        tier="private",
    )
    write(root, product_doc("toys", good), "toys.odps.yaml")
    assert errors(root) == []
    for change, message in [
        ({"stub": False}, "must be marked stub: true"),
        ({"hosting": "private-hf"}, "a stub port is not hosted"),
        ({"openQuestions": []}, "lists its openQuestions"),
        ({"pendingInputs": []}, "not catalogued and not listed in pendingInputs"),
    ]:
        bad = port(
            "toy_stub",
            "toy_stub",
            **{
                **{
                    "inputs": ["future_source"],
                    "hosting": "none",
                    "stub": True,
                    "openQuestions": ["Which file?"],
                    "pendingInputs": pending,
                    "licences": ["unresolved: not read"],
                    "tier": "private",
                },
                **change,
            },
        )
        write(root, product_doc("toys", bad), "toys.odps.yaml")
        assert any(message in e for e in errors(root)), (change, errors(root))


def test_grouped_products_keep_each_source_its_own_series(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    contract = port_contract("toy_index")
    contract["customProperties"].insert(0, {"property": "dataKind", "value": "surveillance"})
    write(root, stamped(contract), "ports/toy_index.odcs.yaml")
    one = port("toy_index", "toy_index", inputs=["toy_cases"], countedUnit="case", dataKind="surveillance")
    write(root, product_doc("toys", one, combine="grouped-never-merged"), "toys.odps.yaml")
    assert errors(root) == []
    two = port(
        "toy_index",
        "toy_index",
        inputs=["toy_cases", "toy_listed"],
        licences=["CC-BY-NC-SA-3.0-IGO", "public-domain-us-federal"],
        hosting="none",
    )
    write(root, product_doc("toys", two, combine="grouped-never-merged"), "toys.odps.yaml")
    errs = errors(root)
    assert any("each port reads exactly one source" in e for e in errs)
    assert any("each port declares its countedUnit" in e for e in errs)
    assert (
        "toys.odps.yaml/toy_index: in a grouped product each port declares its dataKind (surveillance, programme, "
        "or unresolved: ...)"
    ) in errs


@pytest.mark.parametrize(
    ("port_kind", "contract_kind", "ok"),
    [
        ("programme", "programme", True),
        ("unresolved", "unresolved: which indicators are programme data", True),
        ("surveillance", "programme", False),
        ("programme", None, False),
    ],
)
def test_a_port_and_its_contract_name_the_same_data_kind(
    tmp_path: Path, port_kind: str, contract_kind: str | None, ok: bool
) -> None:
    root = registry_root(tmp_path)
    doc = port_contract("toy_index")
    if contract_kind is not None:
        doc["customProperties"].insert(0, {"property": "dataKind", "value": contract_kind})
    write(root, stamped(doc), "ports/toy_index.odcs.yaml")
    p = port("toy_index", "toy_index", inputs=["toy_cases"], countedUnit="case", dataKind=port_kind)
    write(root, product_doc("toys", p, combine="grouped-never-merged"), "toys.odps.yaml")
    want = (
        []
        if ok
        else [
            f"toys/toy_index: dataKind {port_kind!r} differs from its contract's dataKind {contract_kind or ''!r}"
        ]
    )
    assert errors(root) == want


def test_michigan_ports_are_grouped_and_programme_data_is_labelled() -> None:
    prod = product.load_products(REPO)["michigan-hiv"]
    assert product._custom(prod.raw["customProperties"])["combine"] == "grouped-never-merged"
    kinds = {p.name: p.custom["dataKind"] for p in prod.ports}
    assert kinds["mi_detroit_ryan_white"] == kinds["mi_detroit_ehe"] == "programme"
    for p in prod.ports:
        assert any("Out-Wayne" in c for c in p.constraints), p.name
        assert len(p.inputs) == 1 and p.stub
        # The suppression rule isn't read yet: an open question, never "nothing is suppressed" (rules 1 and 4).
        contract = product.port_contract(REPO, load_registry(), p.contract_id)
        assert contract.unreliable.kind == "unresolved", p.name
        assert any("suppress" in q.lower() for q in contract.open_questions), p.name
    assert kinds["mi_detroit_dhd_sti_annual"].startswith("unresolved")


def test_an_unresolved_suppression_rule_keeps_a_port_an_unserved_stub(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    doc = port_contract("toy_stub", tier="private")
    rule = {"kind": "unresolved", "description": "Not read yet."}
    doc["customProperties"] = [
        {"property": "tier", "value": "private"},
        {"property": "unreliableRule", "value": rule},
        {"property": "openQuestions", "value": ["Which values does the publisher suppress?"]},
    ]
    write(root, stamped(doc), "ports/toy_stub.odcs.yaml")
    stub = {
        "inputs": ["toy_cases"],
        "tier": "private",
        "hosting": "none",
        "stub": True,
        "openQuestions": ["q"],
    }
    write(root, product_doc("toys", port("toy_stub", "toy_stub", **stub)), "toys.odps.yaml")
    assert errors(root) == []
    served = {**stub, "stub": False, "hosting": "private-hf"}
    write(root, product_doc("toys", port("toy_stub", "toy_stub", **served)), "toys.odps.yaml")
    assert errors(root) == [
        "toys/toy_stub: its suppression rule is unresolved, so it stays a stub and is not served"
    ]
    # It is listed as an open question and keeps the contract draft.
    doc["customProperties"][2]["value"] = ["Something else?"]
    doc["status"] = "active"
    write(root, stamped(doc), "ports/toy_stub.odcs.yaml")
    errs = product.port_contract_errors(root / "contracts" / "ports" / "toy_stub.odcs.yaml")
    assert errs == [
        "toy_stub.odcs.yaml: an unresolved unreliableRule keeps the contract draft",
        "toy_stub.odcs.yaml: an unresolved unreliableRule is listed in openQuestions (the suppression rule)",
    ]
    # A catalogued source's own contract may never say unresolved.
    from health_data_registry.registry import parse_contract

    with pytest.raises(RegistryError, match="unreliableRule needs a kind"):
        parse_contract(doc, expected_id="toy_stub")
    parsed = product._parse_port_contract(doc, "toy_stub")
    assert parsed.unreliable.kind == "unresolved" and parsed.unreliable.description == "Not read yet."
    assert parsed.raw == doc  # the stand-in used for parsing never leaks into the contract
    for bad in ({"kind": "unresolved", "description": ""}, {**rule, "threshold": 5}):
        doc["customProperties"][1]["value"] = bad
        with pytest.raises(RegistryError, match="needs a description and no threshold"):
            product._parse_port_contract(doc, "toy_stub")


def test_duplicate_ids_and_names_are_refused(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(root, product_doc("toy-hub", port("toy_cases", "toy_cases")), "again.odps.yaml")
    assert any("used by more than one file" in e for e in errors(root))
    with pytest.raises(RegistryError, match="also used"):
        product.load_products(root)
    write(
        root,
        product_doc("x", port("toy_cases", "toy_cases"), port("toy_cases", "toy_cases")),
        "again.odps.yaml",
    )
    assert any("names must be unique" in e for e in errors(root))


def test_an_unknown_port_contract_is_refused(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(root, product_doc("toys", port("toy_x", "toy_x", inputs=["toy_cases"])), "toys.odps.yaml")
    assert any("neither a catalogued source's contract" in e for e in errors(root))


# --------------------------------------------------------------- fingerprints, quality rules and serving


def test_fingerprints_enforce_major_for_shape_and_minor_for_meaning() -> None:
    v1 = port_contract("toy_index")
    assert fingerprint_errors(v1, "t") == []
    history = product._custom(v1["customProperties"])["fingerprints"]

    meaning = copy.deepcopy(v1)
    meaning["schema"][0]["properties"][0]["description"] = "Changed meaning."
    assert any("meaning changed" in e for e in fingerprint_errors(meaning, "t"))
    meaning["version"] = "0.2.0"
    assert fingerprint_errors(stamped(meaning, history), "t") == []
    meaning["version"] = "0.1.1"
    assert any("at least a new minor" in e for e in fingerprint_errors(stamped(meaning, history), "t"))

    shape = copy.deepcopy(v1)
    shape["schema"][0]["properties"][0]["physicalType"] = "INTEGER"
    assert any("shape changed" in e for e in fingerprint_errors(shape, "t"))
    shape["version"] = "0.2.0"
    assert any("new major version" in e for e in fingerprint_errors(stamped(shape, history), "t"))
    shape["version"] = "1.0.0"
    assert fingerprint_errors(stamped(shape, history), "t") == []

    # Open questions and the history itself are not meaning.
    asked = copy.deepcopy(v1)
    asked["customProperties"].append({"property": "openQuestions", "value": ["Why?"]})
    assert fingerprint_errors(asked, "t") == []


def test_fingerprint_history_must_be_well_formed() -> None:
    v1 = port_contract("toy_index")
    bare = copy.deepcopy(v1)
    bare["customProperties"] = [c for c in bare["customProperties"] if c["property"] != "fingerprints"]
    assert "needs at least one" in fingerprint_errors(bare, "t")[0]
    other = copy.deepcopy(v1)
    other["version"] = "0.3.0"
    assert any("is for 0.1.0, not version 0.3.0" in e for e in fingerprint_errors(other, "t"))
    entry = product._custom(v1["customProperties"])["fingerprints"][0]
    for history in ([{**entry, "version": "x"}], [entry]):
        doc = copy.deepcopy(v1)
        doc["customProperties"][-1]["value"] = [*history, entry]
        assert fingerprint_errors(doc, "t"), history


def test_every_quality_rule_can_become_a_dbt_test(tmp_path: Path) -> None:
    doc = port_contract("toy_index")
    rules = quality_rules(doc)
    assert [(r.name, r.dbt_test, r.dimension, r.severity, r.column) for r in rules] == [
        ("title_not_null", "not_null", "completeness", "error", "title")
    ]
    assert product.quality_errors(doc, "t") == []
    bad = copy.deepcopy(doc)
    q = bad["schema"][0]["properties"][0]["quality"][0]
    q["severity"] = "fatal"
    q["customProperties"] = []
    assert any("needs a name, a dbtTest" in e for e in product.quality_errors(bad, "t"))
    bad["schema"][0]["properties"][0]["quality"] = []
    assert product.quality_errors(bad, "t") == ["t: a port contract declares its quality rules"]
    twice = copy.deepcopy(doc)
    twice["schema"][0]["quality"] = copy.deepcopy(doc["schema"][0]["properties"][0]["quality"])
    assert any("used twice" in e for e in product.quality_errors(twice, "t"))
    for f in sorted((REPO / "contracts" / "ports").glob("*.odcs.yaml")):
        assert all(
            r.dbt_test and r.severity in ("error", "warn")
            for r in quality_rules(yaml.safe_load(f.read_text()))
        )


def test_a_port_is_served_only_when_its_error_rules_pass() -> None:
    doc = port_contract("toy_index", private_col=True)
    doc["schema"][0]["properties"][1]["quality"][0]["severity"] = "warn"
    assert servable(doc, {"title_not_null": "passed", "abstract_text_not_null": "failed"})
    for outcome in ("failed", "error", "not_run", True, "PASSED"):
        assert not servable(doc, {"title_not_null": outcome}), outcome  # type: ignore[dict-item]
    assert not servable(doc, {})  # a rule that didn't run fails closed
    none = copy.deepcopy(doc)
    none["schema"][0]["properties"][0]["quality"] = []
    assert not servable(none, {"abstract_text_not_null": "passed"})  # no error rule at all: never served
    # ...and validate says so, in the code and the docs alike.
    assert product.quality_errors(none, "t") == [
        "t: a port contract needs at least one error-severity rule; it is served only when they pass"
    ]


def test_a_rule_that_could_not_run_is_told_apart_from_a_data_failure() -> None:
    doc = port_contract("toy_index", private_col=True)
    report = product.rule_report(doc, {"title_not_null": "error", "abstract_text_not_null": "failed"})
    assert report == {
        "passed": [],
        "failed": ["abstract_text_not_null"],
        "error": ["title_not_null"],
        "not_run": [],
    }
    report = product.rule_report(doc, {"title_not_null": "passed", "abstract_text_not_null": "odd"})
    assert report == {
        "passed": ["title_not_null"],
        "failed": [],
        "error": [],
        "not_run": ["abstract_text_not_null"],
    }
    assert product.rule_report(doc, {})["not_run"] == ["title_not_null", "abstract_text_not_null"]


def test_conformed_keys_name_a_dimension_and_proposed_tags_carry_review_state() -> None:
    doc = yaml.safe_load((REPO / "contracts" / "ports" / "id_abstracts_index.odcs.yaml").read_text())
    assert product.conformed_key_errors(doc, "t") == []
    keys = {
        p["name"]: product._custom(p["customProperties"]).get("conformedKey")
        for p in doc["schema"][0]["properties"]
    }
    assert keys["condition_concept_ids"]["vocabulary"] == "MeSH"
    assert keys["organism_taxon_ids"]["vocabulary"] == "NCBI Taxonomy"
    assert keys["place_keys"]["dimension"] == "dim_place"

    bad = copy.deepcopy(doc)
    props = {p["name"]: p for p in bad["schema"][0]["properties"]}
    props["concept_tag_state"]["quality"] = []
    errs = product.conformed_key_errors(bad, "t")
    assert any("condition_concept_ids: a dictionary-proposed tag" in e for e in errs)
    for c in props["year"]["customProperties"]:
        if c["property"] == "conformedKey":
            c["value"] = {"dimension": "dim_gazetteer"}
    assert any("t.year: conformedKey.dimension" in e for e in product.conformed_key_errors(bad, "t"))

    for f in sorted((REPO / "contracts" / "ports").glob("mi_*.odcs.yaml")):
        mi = yaml.safe_load(f.read_text())
        dims = {
            product._custom(p["customProperties"]).get("conformedKey", {}).get("dimension")
            for p in mi["schema"][0]["properties"]
        }
        assert {"dim_concept", "dim_place", "dim_time"} <= dims, f.name


@pytest.mark.xfail(
    reason="the conformed concept dimension lands with the silver stage c (feat/contract-silver)"
)
def test_one_concept_id_finds_both_the_surveillance_series_and_the_abstracts() -> None:
    from health_data_registry import silver  # type: ignore[attr-defined]

    hiv = "D015658"  # MeSH: HIV Infections
    assert silver.datasets_for_concept(hiv)
    assert silver.abstracts_for_concept(hiv)


def test_load_ports_derives_hosted_from_hosting(tmp_path: Path) -> None:
    (tmp_path / "contracts").mkdir()
    doc = product_doc("h", port("a", "a"), port("b", "b", hosting="live"))
    (tmp_path / "contracts" / "health-data-registry.odps.yaml").write_text(yaml.safe_dump(doc))
    ports = load_ports(tmp_path)
    assert ports["a"].hosted and not ports["b"].hosted


def test_bad_yaml_and_kinds_are_reported(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    (root / "contracts" / "broken.odps.yaml").write_text("a: [\n")
    (root / "contracts" / "ports" / "broken.odcs.yaml").write_text("- just a list\n")
    errs = errors(root)
    assert any("broken.odps.yaml: not valid YAML" in e for e in errs)
    assert any("broken.odcs.yaml: a data product must be a YAML mapping" in e for e in errs)
    (root / "contracts" / "broken.odps.yaml").write_text("kind: DataContract\nid: x\napiVersion: v1.1.0\n")
    assert any("kind must be DataProduct" in e for e in errors(root))


def test_cli_validate_reports_product_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    root = registry_root(tmp_path)
    monkeypatch.setenv("HDR_REGISTRY", str(root))
    monkeypatch.setenv("HDR_LAKE", str(tmp_path / "lake"))
    assert cli.main(["validate"]) == 0
    assert "ok   toy-hub/toy_cases: public tier, hosting public-hub" in capsys.readouterr().out
    write(
        root,
        product_doc("toy-hub", port("toy_cases", "toy_cases", tier="x")),
        "health-data-registry.odps.yaml",
    )
    assert cli.main(["validate"]) == 1
    assert "FAIL toy-hub/toy_cases: tier must be one of" in capsys.readouterr().err


# ------------------------------------------------------------------------- exact messages (mutation tests)


def test_port_errors_say_exactly_what_is_wrong(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(root, port_contract("toy_index"), "ports/toy_index.odcs.yaml")
    bad = port(
        "Toy-Index",
        "toy_index",
        inputs=["toy_cases"],
        tier="x",
        hosting="private-hf",
        releaseTag="v1",
        sla={"refresh": "hourly", "release": "r", "support": "s"},
        pendingInputs=[{"id": "elsewhere", "licence": "l", "fillsFrom": "PR"}],
        stub=True,
        openQuestions=["q"],
    )
    bad["version"] = "0.2.0"
    write(root, product_doc("toys", bad), "toys.odps.yaml")
    assert errors(root) == [
        "toys/Toy-Index: port name must be snake_case",
        "toys/Toy-Index: tier must be one of public, private",
        "toys/Toy-Index: a hosted port's releaseTag is release-<run date>",
        "toys/Toy-Index: sla.refresh must be one of daily, weekly, monthly, quarterly, annual, irregular",
        "toys/Toy-Index: pendingInputs elsewhere is not one of the port's inputContracts",
        "toys/Toy-Index: a stub port is not hosted (hosting: none)",
        "toys/Toy-Index: licences ['public-domain-us-federal'] must be its sources' licences "
        "['l', 'public-domain-us-federal']",
        "toys/Toy-Index: version 0.2.0 differs from contract version 0.1.0",
        "toys/Toy-Index: tier x differs from its contract's tier public",
        "toys/Toy-Index: stored in a private Hugging Face dataset only when every source may be stored (a "
        "reviewed private store or a licence that allows a stored copy); use hosting live or none (not: l)",
    ]
    ftp = port("toy_index", "toy_index", inputs=["toy_cases"], hosting="ftp")
    write(root, product_doc("toys", ftp), "toys.odps.yaml")
    assert errors(root) == ["toys/toy_index: hosting must be one of public-hub, private-hf, live, none"]


def test_a_stub_contract_stays_draft(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    doc = port_contract("toy_stub", tier="private")
    doc["status"] = "active"
    write(root, stamped(doc), "ports/toy_stub.odcs.yaml")
    pending = [{"id": "later", "licence": "l", "fillsFrom": "PR"}]
    stub = port(
        "toy_stub",
        "toy_stub",
        inputs=["later"],
        hosting="none",
        stub=True,
        openQuestions=["q"],
        pendingInputs=pending,
        licences=["l"],
        tier="private",
    )
    write(root, product_doc("toys", stub), "toys.odps.yaml")
    assert errors(root) == ["toys/toy_stub: a stub port's contract stays draft"]


def test_public_hub_needs_hosted_catalogued_sources(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    cat = root / "catalog" / "toy_cases.yaml"
    cat.write_text(cat.read_text().replace("hosting: public\n", ""))
    write(root, port_contract("toy_index"), "ports/toy_index.odcs.yaml")
    hub = product_doc("toy-hub", port("toy_cases", "toy_cases", hosting="live", hosted=False))
    write(root, hub, "health-data-registry.odps.yaml")
    write(root, product_doc("toys", port("toy_index", "toy_index", inputs=["toy_cases"])), "toys.odps.yaml")
    assert errors(root) == [
        "toys/toy_index: on the public hub only when every source is catalogued with hosting: public (its catalog "
        "entry's hosted is true) (not: toy_cases)"
    ]
    cat.write_text(cat.read_text() + "hosting: public\n")
    write(
        root,
        product_doc("toy-hub", port("toy_cases", "toy_cases", hosted=True)),
        "health-data-registry.odps.yaml",
    )
    pending = [{"id": "later", "licence": "public-domain-us-federal", "fillsFrom": "PR"}]
    p = port(
        "toy_index",
        "toy_index",
        inputs=["toy_cases", "later"],
        pendingInputs=pending,
        stub=True,
        openQuestions=["q"],
    )
    write(root, product_doc("toys", p), "toys.odps.yaml")
    assert any("(not: pending sources)" in e for e in errors(root)), errors(root)


def test_public_ports_name_the_private_ports_and_columns(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(root, port_contract("toy_a", tier="private"), "ports/toy_a.odcs.yaml")
    write(root, port_contract("toy_b", tier="private"), "ports/toy_b.odcs.yaml")
    doc = port_contract("toy_index", private_col=True)
    extra = copy.deepcopy(doc["schema"][0]["properties"][1])
    extra["name"] = "full_text"
    extra["quality"][0]["name"] = "full_text_not_null"
    doc["schema"][0]["properties"].append(extra)
    write(root, stamped(doc), "ports/toy_index.odcs.yaml")
    pend = [{"id": i, "licence": "public-domain-us-federal", "fillsFrom": "PR"} for i in ("toy_a", "toy_b")]
    reader = port(
        "toy_index",
        "toy_index",
        inputs=["toy_a", "toy_b"],
        hosting="none",
        stub=True,
        openQuestions=["q"],
        pendingInputs=pend,
    )
    private = [
        port(n, n, inputs=["toy_cases"], tier="private", hosting="private-hf") for n in ("toy_a", "toy_b")
    ]
    write(root, product_doc("toys", reader, *private), "toys.odps.yaml")
    assert errors(root) == [
        "toys/toy_index: a public port may not read private-tier ports (toy_a, toy_b)",
        "toys/toy_index: a public port's contract has columns not cleared as public tier (abstract_text, full_text)",
    ]


def test_a_product_needs_ports(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(root, product_doc("toys"), "toys.odps.yaml")
    assert errors(root) == ["toys.odps.yaml: a product needs at least one output port"]


def test_port_contract_errors_name_the_file(tmp_path: Path) -> None:
    doc = port_contract("toy_index", tier="secret")
    doc["schema"][0]["properties"][0]["quality"][0].pop("name")
    path = tmp_path / "toy_index.odcs.yaml"
    path.write_text(yaml.safe_dump(doc))
    assert product.port_contract_errors(path) == [
        "toy_index.odcs.yaml: its meaning changed; bump the minor version and add a fingerprints entry",
        "toy_index.odcs.yaml: quality rule (unnamed) needs a name, a dbtTest, a dimension and a severity "
        "(error or warn)",
        "toy_index.odcs.yaml: customProperties.tier must be one of public, private",
    ]
    shape = port_contract("toy_index")
    shape["schema"][0]["properties"][0]["quality"][0]["name"] = "fine"
    shape["schema"][0]["properties"][0]["physicalType"] = "INTEGER"
    shape["schema"][0]["properties"].append(
        {"name": "x", "customProperties": [{"property": "conformedKey", "value": {"dimension": "nope"}}]}
    )
    path.write_text(yaml.safe_dump(shape))
    assert product.port_contract_errors(path) == [
        "toy_index.odcs.yaml: its shape changed; bump the major version and add a fingerprints entry",
        "toy_index.odcs.yaml.x: conformedKey.dimension must be one of dim_concept, dim_place, dim_time",
        "toy_index.odcs.yaml: every column declares its tier (public or private); missing on x",
    ]


@pytest.mark.parametrize("missing", ["name", "dimension"])
def test_each_quality_field_is_required(missing: str) -> None:
    doc = port_contract("toy_index")
    doc["schema"][0]["properties"][0]["quality"][0].pop(missing)
    assert len(product.quality_errors(doc, "t")) == 1


def test_a_missing_dbt_test_is_refused() -> None:
    doc = port_contract("toy_index")
    doc["schema"][0]["properties"][0]["quality"][0]["customProperties"] = []
    assert product.quality_errors(doc, "t") == [
        "t: quality rule title_not_null needs a name, a dbtTest, a dimension and a severity (error or warn)"
    ]


def test_conformed_key_errors_cover_every_column() -> None:
    doc = port_contract("toy_index")
    props = doc["schema"][0]["properties"]
    props.append(copy.deepcopy(props[0]))
    props[1]["name"] = "tags"
    props[0]["customProperties"].append({"property": "conformedKey", "value": {"dimension": "dim_x"}})
    key = {"dimension": "dim_concept", "assignment": "dictionary_proposed", "reviewStateColumn": "title"}
    props[1]["customProperties"].append({"property": "conformedKey", "value": key})
    props[0]["quality"].append({"name": "odd", "metric": "invalidValues"})  # no arguments: not a state check
    assert product.conformed_key_errors(doc, "t") == [
        "t.title: conformedKey.dimension must be one of dim_concept, dim_place, dim_time",
        "t.tags: a dictionary-proposed tag needs a reviewStateColumn whose values are checked to be proposed, "
        "accepted, rejected (rules 1 and 9: proposed is never a fact)",
    ]


def test_fingerprint_messages_name_the_version() -> None:
    v1 = port_contract("toy_index")
    e1 = product._custom(v1["customProperties"])["fingerprints"][0]
    shape = {**e1, "version": "0.2.0", "interface": "other"}
    doc = copy.deepcopy(v1)
    doc["customProperties"][-1]["value"] = [
        {**e1, "version": "one"},
        e1,
        shape,
        {**shape, "version": "0.2.1", "docs": "x"},
        {**e1, "version": "0.0.9"},
    ]
    doc["version"] = "0.0.9"
    assert fingerprint_errors(doc, "t") == [
        "t: fingerprint versions must be MAJOR.MINOR.PATCH",
        "t: 0.2.0 changed the shape, so it must be a new major version",
        "t: 0.2.1 changed the meaning, so it must be at least a new minor",
        "t: 0.0.9 changed the shape, so it must be a new major version",
    ]
    doc["customProperties"][-1]["value"] = [
        e1,
        {**e1, "version": "0.1.1", "docs": "x"},
        {**e1, "version": "0.0.1"},
    ]
    doc["version"] = "0.0.1"
    assert fingerprint_errors(doc, "t") == [
        "t: 0.1.1 changed the meaning, so it must be at least a new minor",
        "t: 0.0.1 changed the meaning, so it must be at least a new minor",
    ]
    doc["customProperties"][-1]["value"] = [e1, {**e1, "version": "0.0.1"}]
    assert fingerprint_errors(doc, "t") == ["t: fingerprint versions must increase"]
    doc["customProperties"][-1]["value"] = []
    assert fingerprint_errors(doc, "t") == [
        "t: customProperties.fingerprints needs at least one {version, interface, docs}"
    ]
    doc["customProperties"][-1]["value"] = [e1, "text"]
    assert len(fingerprint_errors(doc, "t")) == 1


def test_digests_keep_non_ascii_text() -> None:
    assert product._digest({"b": "é", "a": 1}) == hashlib.sha256('{"a": 1, "b": "é"}'.encode()).hexdigest()


def test_summary_lines() -> None:
    prods = product.load_products(REPO)
    lines = product.summary([prods["michigan-hiv"], prods["health-data-registry-hub"]])
    assert lines[0] == "ok   michigan-hiv/mi_mdhhs_hiv_sti: private tier, hosting none, draft stub"
    assert "ok   health-data-registry-hub/cdc_respnet: public tier, hosting public-hub" in lines


def test_load_ports_keeps_a_contract_served_twice_as_a_conflict(tmp_path: Path) -> None:
    (tmp_path / "contracts").mkdir()
    doc = product_doc("h", port("first", "same"), port("second", "same"), port("other", "other"))
    (tmp_path / "contracts" / "health-data-registry.odps.yaml").write_text(yaml.safe_dump(doc))
    ports = load_ports(tmp_path)
    assert ports["same"].name == "first"
    assert ports["same"].conflict == (
        "two output ports of the data product, first and second, both claim this source, so which one it "
        "belongs to can't be said"
    )
    assert ports["other"].conflict == ""


def test_a_duplicated_hub_port_refuses_only_that_source(tmp_path: Path) -> None:
    from health_data_registry.publish import export_public
    from health_data_registry.query import Engine, QueryRefused
    from tests.test_publish import RUN, make_lake

    root = write_hub_registry(tmp_path / "reg")
    hub = (root / "contracts" / "health-data-registry.odps.yaml").read_text()
    hub += (
        "  - {name: toy_cases_again, type: tables, version: 0.1.0, contractId: toy_cases,\n"
        "     customProperties: [{property: hosted, value: true}]}\n"
    )
    (root / "contracts" / "health-data-registry.odps.yaml").write_text(hub)
    reg = load_registry(root)
    eng = Engine(registry=reg, lake=tmp_path / "no-lake", hub=None, cache=tmp_path / "cache")
    with pytest.raises(QueryRefused) as e:
        eng.provenance("toy_cases")
    assert e.value.code == "port_conflict"
    assert "both claim this source" in str(e.value) and "Traceback" not in str(e.value)
    assert "Something's wrong helper" in e.value.next_step
    assert eng.provenance("toy_listed")["output_port"] == "toy_listed"  # every other source still works

    lake = make_lake(tmp_path / "lake", reg, tmp_path)
    out = tmp_path / "hub"
    export_public(reg, lake, str(out), run_date=RUN)
    m = yaml.safe_load((out / "toy_cases" / "manifest.json").read_text())
    assert m["data_product"] is None
    assert m["data_product_problem"].startswith("no data product named for toy_cases: two output ports")
    listed = yaml.safe_load((out / "toy_listed" / "manifest.json").read_text())
    assert listed["data_product"]["port"] == "toy_listed" and "data_product_problem" not in listed


def test_a_port_refreshes_no_more_often_than_its_slowest_source(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    cat = root / "catalog" / "toy_listed.yaml"
    cat.write_text(cat.read_text().replace("refresh: weekly", "refresh: annual"))
    write(root, port_contract("toy_both", tier="private"), "ports/toy_both.odcs.yaml")

    def both(refresh: str) -> dict[str, Any]:
        return port(
            "toy_both",
            "toy_both",
            inputs=["toy_cases", "toy_listed"],  # weekly and annual
            licences=["CC-BY-NC-SA-3.0-IGO", "public-domain-us-federal"],
            tier="private",
            hosting="none",
            sla={"refresh": refresh, "release": "r", "support": "s"},
        )

    for ok in ("annual", "irregular"):
        write(root, product_doc("toys", both(ok)), "toys.odps.yaml")
        assert errors(root) == [], ok
    write(root, product_doc("toys", both("weekly")), "toys.odps.yaml")
    assert errors(root) == [
        "toys/toy_both: sla.refresh weekly promises more often than toy_listed refreshes (annual); a port "
        "refreshes no more often than its slowest source"
    ]


def test_hosted_must_be_a_boolean_and_only_true_counts(tmp_path: Path) -> None:
    (tmp_path / "contracts").mkdir()
    doc = product_doc(
        "h", port("a", "a", hosted="true"), port("b", "b", hosted=1), port("c", "c", hosted=True)
    )
    (tmp_path / "contracts" / "health-data-registry.odps.yaml").write_text(yaml.safe_dump(doc))
    ports = load_ports(tmp_path)
    assert not ports["a"].hosted and not ports["b"].hosted and ports["c"].hosted


# ------------------------------------------------------------------------------- validate never crashes


def _crash_root(tmp_path: Path) -> Path:
    root = registry_root(tmp_path)
    write(root, port_contract("toy_index", tier="private"), "ports/toy_index.odcs.yaml")
    p = port("toy_index", "toy_index", inputs=["toy_cases"], tier="private", hosting="none")
    write(root, product_doc("toys", p), "toys.odps.yaml")
    assert errors(root) == []
    return root


def test_a_port_contract_with_broken_yaml_is_a_fail_line(tmp_path: Path) -> None:
    root = _crash_root(tmp_path)
    (root / "contracts" / "ports" / "toy_index.odcs.yaml").write_text("version: [\n")
    errs = errors(root)
    assert any(e.startswith("toy_index.odcs.yaml: not valid YAML") for e in errs)
    assert any(e.startswith("toys/toy_index: toy_index.odcs.yaml: not valid YAML") for e in errs)


@pytest.mark.parametrize(
    "breakage",
    ["arguments_null", "quality_text", "properties_text", "context_list", "custom_text", "sla_text"],
)
def test_empty_or_wrong_typed_fields_are_fail_lines(tmp_path: Path, breakage: str) -> None:
    root = _crash_root(tmp_path)
    doc = port_contract("toy_index", tier="private")
    p = port("toy_index", "toy_index", inputs=["toy_cases"], tier="private", hosting="none")
    if breakage == "arguments_null":
        doc["schema"][0]["properties"][0]["customProperties"].append(
            {
                "property": "conformedKey",
                "value": {
                    "dimension": "dim_concept",
                    "assignment": "dictionary_proposed",
                    "reviewStateColumn": "title",
                },
            }
        )
        doc["schema"][0]["properties"][0]["quality"].append(
            {"name": "x", "metric": "invalidValues", "arguments": None}
        )
    elif breakage == "quality_text":
        doc["schema"][0]["properties"][0]["quality"] = "not a list"
    elif breakage == "properties_text":
        doc["schema"][0]["properties"] = "not a list"
    elif breakage == "context_list":
        p["context"] = ["not", "a", "mapping"]
    elif breakage == "custom_text":
        p["customProperties"] = "tier: private"
    elif breakage == "sla_text":
        p["customProperties"] = [
            c if c["property"] != "sla" else {"property": "sla", "value": "weekly"}
            for c in p["customProperties"]
        ]
    write(root, doc, "ports/toy_index.odcs.yaml")
    write(root, product_doc("toys", p), "toys.odps.yaml")
    errs = errors(root)  # never raises
    assert errs, breakage


def test_a_missing_or_altered_schema_is_a_fail_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    root = _crash_root(tmp_path)
    altered = tmp_path / "odcs.json"
    altered.write_bytes(product.ODCS.path.read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(product, "SCHEMA_DIR", tmp_path)  # the ODPS file is missing here
    (tmp_path / product.ODCS.path.name).write_bytes(altered.read_bytes())
    errs = errors(root)
    assert any(e.startswith("the vendored ODCS schema odcs-json-schema-v3.2.0.json has sha256") for e in errs)
    assert any(
        e.startswith("the vendored ODPS schema is missing (odps-json-schema-v1.1.0.json)") for e in errs
    )
    monkeypatch.setenv("HDR_REGISTRY", str(root))
    monkeypatch.setenv("HDR_LAKE", str(tmp_path / "lake"))
    assert cli.main(["validate"]) == 1
    err = capsys.readouterr().err
    assert "FAIL the vendored ODPS schema is missing" in err and "Traceback" not in err


def test_cli_validate_turns_a_registry_error_into_one_fail_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    root = _crash_root(tmp_path)

    def boom(*_: Any, **__: Any) -> list[str]:
        raise RegistryError("the vendored ODPS schema is missing (x.json); run scripts/vendor_schemas.py")

    monkeypatch.setattr(product, "validate_products", boom)
    monkeypatch.setenv("HDR_REGISTRY", str(root))
    monkeypatch.setenv("HDR_LAKE", str(tmp_path / "lake"))
    assert cli.main(["validate"]) == 1
    assert "FAIL the vendored ODPS schema is missing (x.json)" in capsys.readouterr().err


def test_vendored_schemas_are_kept_byte_exact_by_git() -> None:
    attrs = (REPO / ".gitattributes").read_text()
    assert "src/health_data_registry/schemas/** -text" in attrs.splitlines()


# ---------------------------------------------------------------------------------- fingerprint script


def _stamp_script() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fingerprint_ports", REPO / "scripts" / "fingerprint_ports.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stamp_keeps_everything_after_the_fingerprints(tmp_path: Path) -> None:
    stamp = _stamp_script().stamp
    doc = port_contract("toy_index")
    doc["version"] = "0.2.0"
    doc["description"] = {"purpose": "changed"}
    text = yaml.safe_dump(doc, sort_keys=False) + "slaProperties:\n- property: latency\n  value: 1d\n"
    path = tmp_path / "toy_index.odcs.yaml"
    path.write_text(text)
    assert stamp(path) == "recorded 0.2.0"
    after = yaml.safe_load(path.read_text())
    assert after["slaProperties"] == [{"property": "latency", "value": "1d"}]
    assert [h["version"] for h in product._custom(after["customProperties"])["fingerprints"]] == [
        "0.1.0",
        "0.2.0",
    ]
    assert fingerprint_errors(after, "t") == []
    assert stamp(path) == "unchanged"


def test_stamp_places_a_first_entry_before_the_open_questions(tmp_path: Path) -> None:
    stamp = _stamp_script().stamp
    path = tmp_path / "toy.odcs.yaml"
    path.write_text(
        "id: toy\nversion: 0.1.0\ncustomProperties:\n- property: tier\n  value: public\n"
        "- property: openQuestions\n  value:\n  - '- property: fingerprints is only text here'\n"
    )
    assert stamp(path) == "recorded 0.1.0"
    after = yaml.safe_load(path.read_text())
    assert [c["property"] for c in after["customProperties"]] == ["tier", "fingerprints", "openQuestions"]
    assert after["customProperties"][2]["value"] == ["- property: fingerprints is only text here"]


def test_a_contract_served_by_two_product_files_names_both(tmp_path: Path) -> None:
    root = registry_root(tmp_path)
    write(root, product_doc("other", port("again", "toy_cases")), "other.odps.yaml")
    assert (
        "contract toy_cases is served by two ports: health-data-registry.odps.yaml (toy_cases) and "
        "other.odps.yaml (again)"
    ) in errors(root)
