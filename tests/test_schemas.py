"""Bitol's vendored ODPS and ODCS schemas: the hash pin, and every product and contract in the repo against them."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from health_data_registry import cli, product
from health_data_registry.product import (
    ODCS,
    ODPS,
    VENDORED,
    SchemaError,
    load_schema,
    schema_errors,
    schema_files,
    schema_problems,
)
from tests.conftest import CONTRACT

REPO = Path(__file__).resolve().parents[1]


def _vendor_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vendor_schemas", REPO / "scripts" / "vendor_schemas.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_vendored_copies_are_the_pinned_bytes() -> None:
    for spec in VENDORED:
        assert hashlib.sha256(spec.path.read_bytes()).hexdigest() == spec.sha256
        assert spec.url.startswith("https://raw.githubusercontent.com/bitol-io/")
        assert spec.api_version in spec.url
        assert load_schema(spec)["$schema"].startswith("https://json-schema.org/")


def test_every_repo_product_and_contract_conforms() -> None:
    assert schema_problems(REPO) == []
    # Source contracts, model contracts and the product are all covered.
    odcs = {p.relative_to(REPO).as_posix() for p in schema_files(REPO, ODCS)}
    assert "contracts/census_county_pop.odcs.yaml" in odcs
    assert "contracts/models/mmwr_calendar.odcs.yaml" in odcs
    assert [p.name for p in schema_files(REPO, ODPS)] == ["health-data-registry.odps.yaml"]


def test_the_toy_contract_conforms() -> None:
    assert schema_errors(yaml.safe_load(CONTRACT), load_schema(ODCS)) == []


def test_an_altered_schema_is_refused(tmp_path: Path) -> None:
    copy = tmp_path / ODCS.path.name
    copy.write_bytes(ODCS.path.read_bytes() + b"\n")
    with pytest.raises(SchemaError, match="not the recorded"):
        load_schema(ODCS, copy)
    with pytest.raises(SchemaError, match="missing"):
        load_schema(ODCS, tmp_path / "absent.json")


def test_a_missing_schema_is_one_problem_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(product, "VENDORED", (replace(ODPS, sha256="0" * 64), ODCS))
    (tmp_path / "contracts").mkdir()
    problems = schema_problems(tmp_path)
    assert len(problems) == 1
    assert "ODPS" in problems[0] and "vendor_schemas.py" in problems[0]


def _contract_root(tmp_path: Path, text: str) -> Path:
    (tmp_path / "contracts" / "models").mkdir(parents=True)
    (tmp_path / "contracts" / "models" / "toy.odcs.yaml").write_text(text, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("apiVersion: v3.2.0", "apiVersion: v3.0.2", "apiVersion must be v3.2.0"),
        ("kind: DataContract", "kind: Contract", "kind"),
        (
            "status: draft\n",
            "status: draft\nservers:\n  - {id: x, server: data.cdc.gov, type: https, location: https://data.cdc.gov}\n",
            "'https' is not one of",
        ),
        (
            "    logicalType: object\n",
            "    logicalType: object\n    quality:\n      - {type: library, metric: rowCount, mustBeBetween: [83, 83]}\n",
            "quality/0",  # a rowCount equal to one number is mustBe; the schema refuses equal bounds
        ),
    ],
)
def test_schema_problems_name_the_file_and_place(tmp_path: Path, old: str, new: str, message: str) -> None:
    assert old in CONTRACT
    root = _contract_root(tmp_path, CONTRACT.replace(old, new, 1))
    problems = schema_problems(root)
    assert problems, "the broken contract was accepted"
    assert all(p.startswith("contracts/models/toy.odcs.yaml: ") for p in problems)
    assert any(message in p for p in problems), problems


def test_the_custom_server_and_mustbe_forms_conform(tmp_path: Path) -> None:
    text = CONTRACT.replace(
        "status: draft\n",
        "status: draft\nservers:\n  - {id: x, server: www2.census.gov, type: custom, format: csv, "
        "location: https://www2.census.gov/a.csv}\n",
        1,
    ).replace(
        "    logicalType: object\n",
        "    logicalType: object\n    quality:\n      - {type: library, metric: rowCount, mustBe: 83}\n",
        1,
    )
    assert schema_problems(_contract_root(tmp_path, text)) == []


def test_unreadable_files_are_problems(tmp_path: Path) -> None:
    root = _contract_root(tmp_path, "- a list\n")
    (root / "contracts" / "bad.odps.yaml").write_text("a: [unclosed\n", encoding="utf-8")
    problems = schema_problems(root)
    assert "contracts/models/toy.odcs.yaml: not a YAML mapping" in problems
    assert any(p.startswith("contracts/bad.odps.yaml: not readable YAML") for p in problems)


def test_validate_reports_each_problem(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.copytree(REPO / "contracts", tmp_path / "contracts")
    assert cli.validate_schemas(tmp_path) == 0
    out = capsys.readouterr().out
    assert "conform to Bitol's ODCS v3.2.0 schema" in out
    assert "1 *.odps.yaml files conform to Bitol's ODPS v1.1.0 schema" in out
    census = tmp_path / "contracts" / "census_county_pop.odcs.yaml"
    census.write_text(
        census.read_text(encoding="utf-8").replace(
            "    type: custom\n    format: csv", "    type: https\n    format: csv"
        ),
        encoding="utf-8",
    )
    assert cli.validate_schemas(tmp_path) == 2  # the server's type, and the fields only a custom server has
    err = capsys.readouterr().err.splitlines()
    assert all(line.startswith("FAIL contracts/census_county_pop.odcs.yaml: servers/0") for line in err)
    assert any("servers/0/type: 'https' is not one of" in line for line in err)


def test_vendor_script_checks_without_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _vendor_script()
    monkeypatch.setattr(mod, "_get", lambda url: pytest.fail(f"--check fetched {url}"))
    assert mod.main(["--check"]) == 0
    monkeypatch.setattr(mod, "VENDORED", (replace(ODCS, sha256="0" * 64),))
    assert mod.main(["--check"]) == 1


def test_vendor_script_fetches_only_https(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = _vendor_script()
    with pytest.raises(SystemExit, match="non-HTTPS"):
        mod._get("http://raw.githubusercontent.com/x.json")
    with pytest.raises(SystemExit, match="non-HTTPS"):
        mod._get("file:///etc/passwd")
    # A fetch whose bytes don't match the pin is never written.
    target = replace(ODCS, sha256="0" * 64)
    monkeypatch.setattr(mod, "VENDORED", (target,))
    monkeypatch.setattr(mod, "_get", lambda url: b"{}")
    before = ODCS.path.read_bytes()
    assert mod.main([]) == 1
    assert ODCS.path.read_bytes() == before
