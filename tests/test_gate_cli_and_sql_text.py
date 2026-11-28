"""The gate's command line, export-public's tag file, and the SQL text the generators write (issue #213).

Generated SQL is committed and checked for drift, so its exact text matters: a literal spelled another way is a
new file in every pull request. The gate command must take its expected tests from the contracts and its build id
from the lake, and say why it held in plain words on stderr.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import duckdb
import pytest

from health_data_registry import cli, quality, query, transform_gen
from health_data_registry import release_gate as rg
from health_data_registry.registry import Registry
from tests.test_release_gate import GOOD, TOY, target

# ------------------------------------------------------------------------------------------ the gate command


def _args(t: Path, ops: Path | None, **kw: Any) -> argparse.Namespace:
    return argparse.Namespace(target=t, ops=ops, confirm=kw.get("confirm"), commit=kw.get("commit", "abc123"),
                              run_date=kw.get("run_date"))  # fmt: skip


@pytest.fixture
def gate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    seen: dict[str, list[Any]] = {"expected": [], "inputs": []}
    lake = tmp_path / "lake"
    monkeypatch.setenv("HDR_LAKE", str(lake))

    def expected_from(registry: Any) -> rg.Expected:
        seen["expected"].append(registry)
        return TOY

    def build_inputs(registry: Any, lake_dir: Path, commit: str) -> dict[str, Any]:
        seen["inputs"].append((registry, lake_dir, commit))
        return {"commit": commit}

    monkeypatch.setattr(rg, "expected_from", expected_from)
    monkeypatch.setattr(rg, "build_inputs", build_inputs)
    return seen


def test_the_gate_reads_the_contracts_and_the_lake_and_logs_under_the_lake(
    tmp_path: Path, gate_env: dict[str, list[Any]]
) -> None:
    assert cli.gate(_args(target(tmp_path, GOOD), None, run_date="2026-11-25")) == 0
    (registry,) = gate_env["expected"]
    assert isinstance(registry, Registry)
    ((reg2, lake_dir, commit),) = gate_env["inputs"]
    assert reg2 is registry and lake_dir == tmp_path / "lake" and commit == "abc123"
    assert (tmp_path / "lake" / "ops" / rg.SERVING_LOG).is_file()


def test_the_gate_holds_a_build_missing_a_test_the_contracts_expect(
    tmp_path: Path, gate_env: dict[str, list[Any]]
) -> None:
    ops = tmp_path / "ops"
    assert cli.gate(_args(target(tmp_path, GOOD), ops, run_date="2026-11-25")) == 0
    partial = target(tmp_path, [r for r in GOOD if r[0] != "test.p.codes_a.1"], manifest_invocation="inv-1")
    assert cli.gate(_args(partial, ops, run_date="2026-11-25")) == 1
    assert cli.gate(_args(partial, ops, confirm="release-2026-11-25")) == 1
    assert rg.serving(ops) is None


def test_without_a_commit_the_gate_holds_in_plain_words_on_stderr(
    tmp_path: Path,
    gate_env: dict[str, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    assert cli.gate(_args(target(tmp_path, GOOD), tmp_path / "ops", commit=None)) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == (
        "The release is held: the contracts or the lake could not be read (no commit was given (--commit, or "
        "GITHUB_SHA in CI)).\n"
    )


# ------------------------------------------------------------------------------------------ export-public


class _Result:
    def __init__(self, tag: str | None) -> None:
        self.tag = tag

    def summary(self) -> list[str]:
        return ["exported"]


def test_the_tag_file_holds_exactly_the_tag_and_only_when_there_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from health_data_registry import publish

    reg = cast(Registry, SimpleNamespace())
    tag_file = tmp_path / "tag.txt"
    monkeypatch.setattr(publish, "export_public", lambda *a, **kw: _Result("release-2026-11-25"))
    assert cli.export_public(reg, "out", lake=tmp_path, run_date=None, tag_file=tag_file) == 0
    assert tag_file.read_bytes() == b"release-2026-11-25\n"
    assert cli.export_public(reg, "out", lake=tmp_path, run_date=None) == 0
    monkeypatch.setattr(publish, "export_public", lambda *a, **kw: _Result(None))
    untagged = tmp_path / "none.txt"
    assert cli.export_public(reg, "out", lake=tmp_path, run_date=None, tag_file=untagged) == 0
    assert not untagged.exists()


# ------------------------------------------------------------------------------------------ SQL text


def test_quality_literals_are_lowercase_sql() -> None:
    assert (quality._lit(True), quality._lit(False)) == ("true", "false")
    assert quality._lit(3) == "3" and quality._lit(2.5) == "2.5"
    assert quality._lit("it's") == "'it''s'"


def test_a_between_rule_keeps_its_low_and_high_bounds() -> None:
    rule = quality.Rule(id="r", kind="library", description="d", dimension="accuracy", severity="error",
                        comparator="mustBeBetween", bound=[1, 5])  # fmt: skip
    assert quality.condition_sql(rule, "v") == "v BETWEEN 1 AND 5"
    con = duckdb.connect(":memory:")
    assert con.execute(f"SELECT {quality.condition_sql(rule, '3')}").fetchone() == (True,)


def test_renamed_labels_become_one_case_with_a_when_per_rename() -> None:
    sql = query.label_key_sql("x", [("Old A", "New A"), ("Old B", "New B")])
    con = duckdb.connect(":memory:")
    for raw, want in (("Old A", "NEW A"), ("old b", "NEW B"), ("Other", "OTHER")):
        assert con.execute(f"SELECT {sql} FROM (SELECT ? AS x)", [raw]).fetchone() == (want,)


def test_generated_identifiers_double_their_quotes() -> None:
    assert transform_gen._q('a"b') == '"a""b"'


def test_the_primary_key_puts_a_key_without_a_position_first() -> None:
    contract = SimpleNamespace(raw={"schema": [{"properties": [
        {"name": "b", "primaryKey": True, "primaryKeyPosition": 1},
        {"name": "a", "primaryKey": True},
        {"name": "c", "primaryKey": False},
    ]}]})  # fmt: skip
    assert transform_gen.primary_key(cast(Any, contract)) == ["a", "b"]


def test_an_empty_review_queue_is_lowercase_sql_that_returns_nothing() -> None:
    sql = transform_gen.unmapped_labels_sql(cast(Any, []))
    assert (
        "select cast(null as varchar) as dataset, cast(null as varchar) as label, cast(0 as bigint) as row_count "
        "where false"
    ) in sql


def test_make_contracts_check_names_each_stale_file_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(transform_gen, "load_registry", lambda root: SimpleNamespace(root=root))
    monkeypatch.setattr(transform_gen, "generate", lambda registry: {Path("models/a.sql"): "x"})
    monkeypatch.setattr(transform_gen, "stale", lambda root, files: ["models/a.sql"])
    monkeypatch.chdir(tmp_path)
    assert transform_gen.main(["--check"]) == 1
    assert capsys.readouterr().err == "out of date: models/a.sql (run make contracts)\n"
    written: list[Path] = []
    monkeypatch.setattr(
        transform_gen, "write", lambda root, files: (written.append(root), ["models/a.sql"])[1]
    )
    assert transform_gen.main([]) == 0
    assert written == [tmp_path] and capsys.readouterr().out == "wrote models/a.sql\n"
