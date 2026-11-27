"""The build manifest (ADR 0017 section 2): one versioned, immutable file naming everything a build read, with the
quality gate's verdict, that repeats byte for byte when the same inputs are built again."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import build_manifest as bm
from health_data_registry import dbt_raw
from health_data_registry import release_gate as rg
from health_data_registry.publish import PublishCheckFailed, export_public
from health_data_registry.ratchet import _git
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import Manifest, build_snapshot, latest_snapshot, write_ndjson
from tests.conftest import (
    RECORDS,
    TEST_CODE,
    TEST_ENGINES,
    pass_toy_build,
    pin_toy_build,
    raw_fetch,
    write_registry,
)

FIELDS = ["label", "m1", "m2", "states", "year"]


def _snapshot(reg: Registry, lake: Path, tmp_path: Path, *, days: int = 0) -> Manifest:
    page = tmp_path / "page.ndjson"
    write_ndjson(page, RECORDS)
    fetch = raw_fetch("https://data.cdc.gov/resource/toy0-toy0.json", RECORDS)
    fetch = replace(fetch, retrieved_at=fetch.retrieved_at + timedelta(days=days))
    return build_snapshot(lake, reg.get("toy_cases"), [fetch], [page])


@pytest.fixture
def toy(tmp_path: Path) -> tuple[Registry, Path]:
    reg = load_registry(write_registry(tmp_path / "reg"))
    lake = tmp_path / "lake"
    lake.mkdir()
    _snapshot(reg, lake, tmp_path)
    return reg, lake


def _build(pinned: dict[str, Any], **kw: Any) -> rg.Build:
    fields: dict[str, Any] = {
        "build_id": bm.build_id_of(pinned),
        "generated_at": "2026-11-25T12:00:00Z",
        "dbt_version": "1.12.7",
        "invocation_id": "inv-1",
        "models_built": 2,
        "tests_passed": 1,
        "passed_tests": ["toy_cases_has_rows"],
        "warnings": [{"test": "toy_cases_fill_rate_m1", "failures": 3, "type": "test"}],
        "threads": 4,
    }
    return rg.Build(**{**fields, **kw})


def test_the_pin_names_every_input_and_its_id_covers_each(toy: tuple[Registry, Path], tmp_path: Path) -> None:
    reg, lake = toy
    pinned = pin_toy_build(reg, lake)
    assert list(pinned) == list(bm.PINNED_KEYS) and pinned["manifest_version"] == bm.MANIFEST_VERSION
    found = latest_snapshot(lake, "toy_cases")
    assert found is not None
    snap = found[1]
    sid = snap.snapshot_id
    read = pinned["inputs"]["toy_cases"]
    records = lake / f"raw_records/toy_cases/{sid}.ndjson"
    assert read == {
        "snapshot_id": sid,
        "raw_sha256": snap.raw_sha256,
        "partial": False,
        "records": {"path": f"raw_records/toy_cases/{sid}.ndjson", "sha256": bm.sha256_file(records)},
    }
    contract = pinned["contracts"]["sources"]["toy_cases"]
    assert contract["version"] == "0.1.0" and contract["path"] == "contracts/toy_cases.odcs.yaml"
    assert pinned["semantic"]["toy_cases"]["path"] == "semantic/toy_cases.yaml"
    assert pinned["transform"] == {"transform/dbt_project.yml": bm.sha256_file(reg.root / bm.DBT_PROJECT)}
    assert pinned["vars"] == {"near_zero_ytd_max": 1} and pinned["control_plane"] == {}
    first = bm.build_id_of(pinned)
    assert bm.BUILD_ID.fullmatch(first) and pin_toy_build(reg, lake) == pinned  # the same inputs, the same id

    def changed() -> str:
        return bm.build_id_of(pin_toy_build(reg, lake))

    # dbt's own output is never an input.
    (reg.root / "transform" / "target").mkdir()
    (reg.root / "transform" / "target" / "run_results.json").write_text("{}")
    assert changed() == first
    (reg.root / "transform" / "macros").mkdir()
    (reg.root / "transform" / "macros" / "m.sql").write_text("select 1")
    assert changed() != first
    project = reg.root / bm.DBT_PROJECT
    before = changed()
    project.write_text("name: toy\nvars:\n  near_zero_ytd_max: 2\n")
    assert changed() != before
    before = changed()
    path = reg.root / "contracts" / "toy_cases.odcs.yaml"
    path.write_text(path.read_text() + "# edited\n")
    assert changed() != before
    before = changed()
    other = bm.pin(reg, lake, code={**TEST_CODE, "commit": "f" * 40}, engine_versions=TEST_ENGINES)
    assert bm.build_id_of(other) != before
    other = bm.pin(reg, lake, code=TEST_CODE, engine_versions={**TEST_ENGINES, "duckdb": "1.5.0"})
    assert bm.build_id_of(other) != before
    # The same bytes fetched a day later: a new snapshot id (retrieval time is provenance), so a new build id
    # (ADR 0017 section 2); the raw hashes stay equal.
    _snapshot(reg, lake, tmp_path, days=1)
    later = pin_toy_build(reg, lake)
    assert bm.build_id_of(later) != before
    assert later["inputs"]["toy_cases"]["raw_sha256"] == read["raw_sha256"]


def test_a_source_with_nothing_to_read_is_pinned_as_empty(tmp_path: Path) -> None:
    reg = load_registry(write_registry(tmp_path / "reg"))
    empty = pin_toy_build(reg, tmp_path / "no-lake")
    assert empty["inputs"]["toy_cases"] == {
        "snapshot_id": None,
        "raw_sha256": [],
        "partial": False,
        "records": None,
    }
    lake = tmp_path / "lake"
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    fetch = raw_fetch("https://data.cdc.gov/resource/toy0-toy0.json", RECORDS)
    sample = build_snapshot(lake, reg.get("toy_cases"), [fetch], [page], partial=True)
    entry = pin_toy_build(reg, lake)["inputs"]["toy_cases"]
    assert entry["snapshot_id"] == sample.snapshot_id and entry["partial"] and entry["records"] is None
    (lake / "datasets" / "toy_cases" / "LATEST").write_text("not a snapshot\n")
    with pytest.raises(bm.BuildManifestError, match="LATEST pointer or its snapshot manifest can't be read"):
        pin_toy_build(reg, lake)


def test_a_rebuild_gives_a_byte_identical_manifest(toy: tuple[Registry, Path], tmp_path: Path) -> None:
    """No clock, host, thread count or dbt run id: two runs of the same build write the same bytes, once."""
    reg, lake = toy
    pinned = pin_toy_build(reg, lake)
    one = bm.render(bm.manifest_of(pinned, _build(pinned)))
    other_run = _build(
        pin_toy_build(reg, lake),
        generated_at="2026-12-01T00:00:00Z",
        invocation_id="inv-2",
        threads=1,
        passed_tests=["toy_cases_has_rows"],
    )
    assert bm.render(bm.manifest_of(pinned, other_run)) == one
    assert b"inv-1" not in one and b"2026-11-25T12:00:00Z" not in one and b"threads" not in one
    path = bm.write_manifest(lake, bm.manifest_of(pinned, _build(pinned)))
    assert path == lake / "builds" / bm.build_id_of(pinned) / "build.json" and path.read_bytes() == one
    assert not os.access(path, os.W_OK) or os.geteuid() == 0
    assert path.stat().st_mode & 0o777 == 0o444
    stamp = path.stat().st_mtime_ns
    assert bm.write_manifest(lake, bm.manifest_of(pinned, other_run)) == path  # a no-op
    assert path.stat().st_mtime_ns == stamp and not list(path.parent.glob(".*.tmp"))
    doc = bm.read_manifest(path)
    assert doc["verdict"]["status"] == "pass" and doc["verdict"]["tests"] == {
        "toy_cases_fill_rate_m1": {"failures": 3, "outcome": "warn"},
        "toy_cases_has_rows": {"failures": 0, "outcome": "pass"},
    }


def test_a_build_manifest_is_never_rewritten_and_tampering_is_seen(toy: tuple[Registry, Path]) -> None:
    reg, lake = toy
    pinned = pin_toy_build(reg, lake)
    path = bm.write_manifest(lake, bm.manifest_of(pinned, _build(pinned)))
    held = _build(pinned, data_failures=[{"test": "toy_cases_codes", "failures": 2}])
    with pytest.raises(bm.BuildManifestConflict, match="the same inputs gave a different result"):
        bm.write_manifest(lake, bm.manifest_of(pinned, held))
    assert bm.read_manifest(path)["verdict"]["status"] == "pass"
    forged = bm.manifest_of(pinned, held)
    with pytest.raises(bm.BuildManifestError, match="is not the hash of its pinned inputs"):
        bm.write_manifest(lake, {**forged, "build_id": "build-" + "0" * 64})
    path.chmod(0o644)
    doc = json.loads(path.read_text())
    doc["pinned"]["inputs"]["toy_cases"]["raw_sha256"] = ["0" * 64]
    path.write_text(json.dumps(doc))
    with pytest.raises(bm.BuildManifestError, match="changed after it was written"):
        bm.read_manifest(path)
    with pytest.raises(bm.BuildManifestError, match="not a build id"):
        bm.build_dir(lake, "../escape")


def test_the_verdict_names_each_check_once_by_its_strongest_outcome(toy: tuple[Registry, Path]) -> None:
    reg, lake = toy
    pinned = pin_toy_build(reg, lake)
    build = _build(
        pinned,
        passed_tests=["b_rule"],
        warnings=[{"test": "a_rule", "failures": 1}],
        data_failures=[{"test": "a_rule", "failures": None}],  # an error rule downgraded to warn
        not_evaluated=[{"test": "c_rule"}],
        rule_errors=[{"test": "d_rule"}],
        model_errors=[{"model": "stg_x", "error": "/srv/runner/somewhere: boom"}],
        skipped=[{"node": "int_x"}],
        input_problems=["the pin differs"],
    )
    verdict = bm.verdict_of(build)
    assert verdict["status"] == "hold" and verdict["reason"].startswith("the pin differs")
    assert list(verdict["tests"]) == ["a_rule", "b_rule", "c_rule", "d_rule"]
    assert verdict["tests"]["a_rule"] == {"outcome": "fail", "failures": None}
    assert verdict["tests"]["c_rule"]["outcome"] == "not evaluated"
    assert verdict["model_errors"] == ["stg_x"] and verdict["skipped"] == ["int_x"]
    assert "/srv/runner/somewhere" not in json.dumps(verdict)  # dbt's messages can name paths; never recorded


def test_the_code_state_is_the_commit_lock_and_any_uncommitted_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        _git(repo, *args)

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (repo / ".gitignore").write_text("lake/\n")
    (repo / "uv.lock").write_text("lock\n")
    git("add", ".")
    git("commit", "-qm", "c")
    clean = bm.code_state(repo, git=_git)
    assert clean["uncommitted_sha256"] is None and len(clean["commit"]) == 40
    assert clean["uv_lock_sha256"] == bm.sha256_file(repo / "uv.lock")
    assert bm.code_state(repo, git=_git, commit=clean["commit"]) == clean
    with pytest.raises(bm.BuildManifestError, match="not .*the commit this build names"):
        bm.code_state(repo, git=_git, commit="e" * 40)
    (repo / "lake").mkdir()
    (repo / "lake" / "x").write_text("ignored")
    assert bm.code_state(repo, git=_git) == clean  # ignored files are not code
    (repo / "new.py").write_text("x = 1\n")
    untracked = bm.code_state(repo, git=_git)["uncommitted_sha256"]
    (repo / "new.py").write_text("x = 2\n")
    assert untracked and bm.code_state(repo, git=_git)["uncommitted_sha256"] not in (None, untracked)
    (repo / "new.py").unlink()
    (repo / "uv.lock").write_text("lock 2\n")
    edited = bm.code_state(repo, git=_git)
    assert edited["uncommitted_sha256"] and edited["commit"] == clean["commit"]
    with pytest.raises(bm.BuildManifestError, match="not a git checkout"):
        bm.code_state(tmp_path / "nowhere", git=_git)


def test_differences_name_what_the_build_read_otherwise(toy: tuple[Registry, Path], tmp_path: Path) -> None:
    reg, lake = toy
    pinned = pin_toy_build(reg, lake)
    assert bm.differences(pinned, {**pinned, "engines": {"python": "3.13"}}) == []
    _snapshot(reg, lake, tmp_path, days=1)
    assert bm.differences(pinned, pin_toy_build(reg, lake)) == ["inputs.toy_cases"]
    code = {**pinned, "code": {**TEST_CODE, "uncommitted_sha256": "a" * 64}}
    assert bm.differences(pinned, code) == ["code.uncommitted_sha256"]


def test_a_pin_is_read_back_or_refused(toy: tuple[Registry, Path], tmp_path: Path) -> None:
    reg, lake = toy
    pinned = pin_toy_build(reg, lake)
    path = tmp_path / "target" / bm.PIN_FILE
    assert bm.write_pin(path, pinned) == bm.build_id_of(pinned) and bm.read_pin(path) == pinned
    for bad, match in (
        ("{", "can't be read"),
        ("{}", "not a build pin"),
        (json.dumps({**pinned, "manifest_version": 99}), "version 99"),
        (json.dumps({**pinned, "inputs": {"toy_cases": {"snapshot_id": "../../x"}}}), "not a snapshot id"),
    ):
        path.write_text(bad)
        with pytest.raises(bm.BuildManifestError, match=match):
            bm.read_pin(path)


def test_dbt_reads_only_the_pinned_load_and_refuses_changed_bytes(
    toy: tuple[Registry, Path], tmp_path: Path
) -> None:
    reg, lake = toy
    pinned = pin_toy_build(reg, lake)
    inputs = pinned["inputs"]
    sid = inputs["toy_cases"]["snapshot_id"]
    newer = _snapshot(reg, lake, tmp_path, days=1).snapshot_id  # LATEST moves after the pin
    sql, warning = dbt_raw.pinned_sql(lake, "toy_cases", FIELDS, inputs)
    import duckdb

    rows = duckdb.connect().execute(sql).fetchall()
    assert warning is None and {r[-2] for r in rows} == {sid} != {newer} and len(rows) == len(RECORDS)
    with pytest.raises(dbt_raw.PinnedLoadRefused, match="not in the build's pin"):
        dbt_raw.pinned_sql(lake, "other", FIELDS, inputs)
    empty = {"toy_cases": {**inputs["toy_cases"], "records": None}}
    sql, warning = dbt_raw.pinned_sql(lake, "toy_cases", FIELDS, empty)
    assert sql == dbt_raw.empty_sql(FIELDS) and warning and "has no full load kept for dbt" in warning
    records = lake / f"raw_records/toy_cases/{sid}.ndjson"
    records.write_text(records.read_text() + "{}\n")
    with pytest.raises(dbt_raw.PinnedLoadRefused, match="no longer holds the bytes the build pinned"):
        dbt_raw.pinned_sql(lake, "toy_cases", FIELDS, inputs)
    plugin = dbt_raw.Plugin.__new__(dbt_raw.Plugin)
    with pytest.raises(dbt_raw.PinnedLoadRefused, match="run `health-data-registry pin-build` before dbt"):
        plugin.initialize({"lake": str(lake), "pin": str(tmp_path / "missing.json")})
    path = tmp_path / bm.PIN_FILE
    bm.write_pin(path, pinned)
    plugin.initialize({"lake": str(lake), "pin": str(path)})
    assert plugin.inputs == inputs
    plugin.initialize({"lake": str(lake), "pin": ""})
    assert plugin.inputs is None  # no pin: LATEST, as before


def _hub_registry(tmp_path: Path) -> tuple[Registry, Path]:
    from tests.test_publish import make_lake, write_hub_registry

    reg = load_registry(write_hub_registry(tmp_path / "reg"))
    return reg, make_lake(tmp_path / "lake", reg, tmp_path)


def test_the_release_carries_the_build_that_read_its_snapshots(tmp_path: Path) -> None:
    """export-public writes build.json into the release, names it in every manifest, and provenance reads it."""
    from health_data_registry.hub import Hub
    from health_data_registry.query import Engine
    from health_data_registry.tools import get_provenance

    reg, lake = _hub_registry(tmp_path)
    found = bm.latest_build(lake)
    assert found is not None
    build_path, manifest = found
    out = tmp_path / "release"
    export_public(reg, lake, str(out), run_date="2026-11-25", build=manifest["build_id"])
    assert (out / "build.json").read_bytes() == build_path.read_bytes()
    release = json.loads((out / "manifest.json").read_text())
    ref = {"build_id": manifest["build_id"], "manifest": "build.json", "sha256": bm.sha256_file(build_path)}
    assert release["build"] == ref and release["files"]["build.json"]["sha256"] == ref["sha256"]
    assert all(sm["build"] == ref for sm in release["sources"].values())
    assert f"Built by `{manifest['build_id']}`" in (out / "README.md").read_text()
    # A local export not given a build carries none.
    plain = tmp_path / "plain"
    export_public(reg, lake, str(plain), run_date="2026-11-25")
    assert (
        not (plain / "build.json").exists()
        and json.loads((plain / "manifest.json").read_text())["build"] is None
    )

    engine = Engine(
        registry=reg, lake=tmp_path / "server-lake", hub=Hub(url=str(out), cache=tmp_path / "cache")
    )
    result = get_provenance(reg, engine, source_id="toy_cases", fmt="json")
    m = engine.manifest("toy_cases")
    assert (m.origin, m.build_id, m.build_manifest_sha256) == ("hub", ref["build_id"], ref["sha256"])
    assert engine.provenance("toy_cases")["build_id"] == ref["build_id"]
    assert f"Built by {ref['build_id']}." in json.dumps(result)


def test_a_release_never_names_a_build_that_did_not_read_it(tmp_path: Path) -> None:
    reg, lake = _hub_registry(tmp_path)
    passed = bm.latest_build(lake)
    assert passed is not None
    first = passed[1]["build_id"]
    # The lake's LATEST moves on after the build: publishing it under that build is refused.
    page = tmp_path / "p2.ndjson"
    write_ndjson(page, RECORDS)
    fetch = raw_fetch("https://data.cdc.gov/resource/toy0-toy0.json", RECORDS)
    build_snapshot(
        lake, reg.get("toy_cases"), [replace(fetch, retrieved_at=fetch.retrieved_at + timedelta(1))], [page]
    )
    with pytest.raises(PublishCheckFailed, match=f"build {first} read snapshot .* but the lake's LATEST is"):
        export_public(reg, lake, str(tmp_path / "o"), run_date="2026-11-25", build=first)
    held = pass_toy_build(reg, lake, servable=False)
    with pytest.raises(PublishCheckFailed, match=f"build {held} did not pass the release gate"):
        export_public(reg, lake, str(tmp_path / "o2"), run_date="2026-11-25", build=held)
    with pytest.raises(PublishCheckFailed, match="the build manifest can't be used"):
        export_public(reg, lake, str(tmp_path / "o3"), run_date="2026-11-25", build="build-" + "a" * 64)
    # A hub upload with no build that passed in the lake is refused before anything is staged.
    (lake / "builds" / "LATEST").unlink()
    with pytest.raises(PublishCheckFailed, match="no build has passed the release gate in this lake"):
        export_public(reg, lake, "hf://datasets/org/hub", run_date="2026-11-25", api=object())
