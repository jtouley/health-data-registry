"""The command line (outside review (g)): every subcommand's exit code and plain-words output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry import cli
from health_data_registry.registry import load_registry
from tests.conftest import RECORDS, raw_fetch, write_registry
from tests.test_publish import make_lake, write_hub_registry


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = write_hub_registry(tmp_path / "reg")
    monkeypatch.setenv("HDR_REGISTRY", str(root))
    monkeypatch.setenv("HDR_LAKE", str(tmp_path / "lake"))
    monkeypatch.setenv("HDR_HUB", "off")
    monkeypatch.setattr(cli, "_fetcher", lambda reg, log=None: lambda url: raw_fetch(url, RECORDS))
    return root


def test_a_broken_registry_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    root = write_registry(tmp_path / "reg", contract="kind: Nope\n")
    monkeypatch.setenv("HDR_REGISTRY", str(root))
    assert cli.main(["validate"]) == 2
    assert capsys.readouterr().err.startswith("registry error: ")


def test_refresh_fetch_and_failures(env: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    assert cli.main(["fetch", "toy_cases", "--max-rows", "5"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ok   toy_cases: 6 rows, snapshot ")
    assert cli.main(["refresh", "--all"]) == 0
    assert "skip toy_listed: toy_listed is read live from" in capsys.readouterr().out

    def broken(reg: Any, log: Any = None) -> Any:
        def get(url: str) -> Any:
            raise RuntimeError("the publisher answered with status 503 instead of the data")

        return get

    monkeypatch.setattr(cli, "_fetcher", broken)
    assert cli.main(["refresh", "toy_cases"]) == 1
    assert "FAIL toy_cases: the publisher answered with status 503" in capsys.readouterr().err
    assert cli.main(["refresh", "no_such_source"]) == 1
    with pytest.raises(SystemExit):
        cli.main(["refresh"])  # names nothing


def test_export_public_to_a_directory(env: Path, tmp_path: Path, capsys: Any) -> None:
    make_lake(tmp_path / "lake", load_registry(env), tmp_path)
    out = tmp_path / "release"
    assert cli.main(["export-public", "--to", str(out), "--run-date", "2026-11-24"]) == 0
    assert (out / "manifest.json").is_file() and capsys.readouterr().out
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "notes.txt").write_text("keep")
    assert cli.main(["export-public", "--to", str(mine)]) == 1
    assert "export stopped: " in capsys.readouterr().err and (mine / "notes.txt").read_text() == "keep"
    assert cli.main(["export-public", "--to", str(out), "--run-date", "24-11-2026"]) == 1


def test_check_contracts_reports_ok_drift_and_failure(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from health_data_registry import adapters

    reg = load_registry(env)
    fields = {c.source_field for c in reg.get("toy_cases").contract.columns}
    monkeypatch.setattr(adapters, "publisher_fields", lambda entry, get: fields)
    assert cli.main(["check-contracts"]) == 0
    assert "ok   toy_cases: all " in capsys.readouterr().out
    monkeypatch.setattr(adapters, "publisher_fields", lambda entry, get: fields - {"states"})
    assert cli.main(["check-contracts"]) == 1
    assert "DRIFT toy_cases: publisher no longer serves states" in capsys.readouterr().err

    def fails(entry: Any, get: Any) -> set[str]:
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(adapters, "publisher_fields", fails)
    assert cli.main(["check-contracts"]) == 1
    assert "FAIL toy_cases: could not read publisher metadata" in capsys.readouterr().err


def test_check_contracts_scope_splits_published_from_unpublished_sources(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The refresh publish blocks only on the sources a release carries (hosting: public); the rest are checked
    in their own run, which the workflow reports as a warning."""
    from health_data_registry import adapters

    reg = load_registry(env)
    hosted = {ds.id for ds in reg if ds.entry.hosted}
    unhosted = {ds.id for ds in reg} - hosted
    assert hosted and unhosted  # the fixture registry has both kinds
    seen: list[str] = []

    def fails_unless_hosted(entry: Any, get: Any) -> set[str]:
        seen.append(entry.id)
        if entry.hosted:
            return {c.source_field for c in reg.get(entry.id).contract.columns}
        raise RuntimeError("This source needs a browser to read")

    monkeypatch.setattr(adapters, "publisher_fields", fails_unless_hosted)
    assert cli.main(["check-contracts", "--scope", "published"]) == 0
    assert set(seen) == hosted
    out = capsys.readouterr()
    assert all(f"ok   {i}: all " in out.out for i in hosted) and "FAIL" not in out.err
    seen.clear()
    assert cli.main(["check-contracts", "--scope", "unpublished"]) == 1
    assert set(seen) == unhosted
    err = capsys.readouterr().err
    assert all(f"FAIL {i}: could not read publisher metadata" in err for i in unhosted)
    seen.clear()
    assert cli.main(["check-contracts"]) == 1  # the default checks every source, as the drift check does
    assert set(seen) == hosted | unhosted
    with pytest.raises(SystemExit):
        cli.main(["check-contracts", "--scope", "hosted"])
    with pytest.raises(ValueError, match="unknown scope"):
        cli.check_contracts(reg, "hosted")


def test_eval_without_and_with_broken_seals(env: Path, tmp_path: Path, capsys: Any) -> None:
    empty = tmp_path / "sealed"
    empty.mkdir()
    assert cli.main(["eval", "--sealed", str(empty)]) == 0
    assert "no sealed sets" in capsys.readouterr().out
    (empty / "set.yaml").write_text("questions: []\n")
    assert cli.main(["eval", "--sealed", str(empty)]) == 1
    assert capsys.readouterr().err.startswith("seal check failed: ")


def test_serve_starts_the_server(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry import server

    called: list[bool] = []
    monkeypatch.setattr(server, "main", lambda: called.append(True))
    assert cli.main(["serve"]) == 0 and called == [True]
