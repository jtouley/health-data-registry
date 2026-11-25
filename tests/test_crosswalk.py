"""The crosswalk proposer writes proposed records only, from the catalog and the DEV eval set, never from TEST."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from health_data_registry import crosswalk as cw
from health_data_registry.registry import load_registry
from tests.conftest import CATALOG, write_registry

REPO = Path(__file__).resolve().parents[1]
LABELLED = (
    CATALOG.replace("{condition: legionellosis,", "{condition: Legionellosis,")
    .replace("{condition: syphilis,", "{condition: Syphilis,")
    .replace(
        "status: draft\n",
        "conditions:\n  dimension: condition\n  source: toy list\n  labels: [Legionellosis, Syphilis]\nstatus: draft\n",
    )
)


def toy_root(path: Path) -> Path:
    return write_registry(path, catalog=LABELLED)


def dev_file(tmp_path: Path, *records: dict[str, object]) -> Path:
    path = tmp_path / "eval" / "dev" / "dev.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n\n")
    return path


def question(condition: str, datasets: list[str], split: str = "dev") -> dict[str, object]:
    return {"id": "q", "tags": {"condition": condition}, "expected_datasets": datasets, "split": split}


def test_catalog_gbd_causes_become_proposals_only_for_exactly_spelled_labels() -> None:
    reg = load_registry(REPO)
    got = cw.from_catalog(reg)
    nndss = {(p.label, p.gbd_cause) for p in got if p.dataset == "cdc_nndss_weekly"}
    assert ("Legionellosis", "Lower respiratory infections") in nndss
    assert all(p.proposed_by == "string_match" and p.synonym is None for p in got)
    for p in got:
        assert p.label in reg.get(p.dataset).entry.conditions.labels
    # WHO's catalog cause names its indicator code, not its label: no exact spelling, so nothing is proposed
    assert not [p for p in got if p.dataset == "who_gho_tb_incidence"]
    rec = got[0].record()
    assert rec["state"] == "proposed" and rec["reviewer"] is None and rec["reviewed_at"] is None
    assert rec["source"] is None and rec["mesh_id"] is None and rec["ncbi_taxon_id"] is None
    assert (
        rec["record_id"] == got[0].record_id
        and rec["record_id"].startswith("p-")
        and len(rec["record_id"]) == 18
    )
    assert set(rec) == set(cw.FIELDS)
    assert rec["note"].startswith(
        "Proposed by exact spelling from the catalog's gbd.causes; a maintainer checks"
    )


def test_dev_synonyms_become_proposals_and_test_or_sealed_never(tmp_path: Path) -> None:
    reg = load_registry(toy_root(tmp_path / "reg"))
    path = dev_file(
        tmp_path,
        question("LEGIONELLOSIS", ["toy_cases", "not_catalogued"]),
        question("Legionellosis", ["toy_cases"]),  # the label itself: no synonym to propose
        question("mumps", ["toy_cases"]),  # spells no label
        {"id": "x", "tags": {}, "expected_datasets": ["toy_cases"], "split": "dev"},
    )
    (p,) = cw.from_dev_set(path, reg)
    assert (p.dataset, p.label, p.synonym, p.proposed_by) == ("toy_cases", "Legionellosis", "LEGIONELLOSIS",
                                                              "dev_eval_synonym")  # fmt: skip
    assert p.record()["state"] == "proposed"
    assert p.note.startswith(
        "Proposed from a DEV eval question's wording (spelling match only); a maintainer"
    )
    with pytest.raises(cw.NotAllowed, match="line 2 is not a DEV question"):
        cw.from_dev_set(dev_file(tmp_path / "t", question("a", []), question("b", [], split="test")), reg)
    sealed = tmp_path / "eval" / "sealed" / "dev" / "dev.jsonl"
    sealed.parent.mkdir(parents=True)
    sealed.write_text("")
    for bad in (sealed, tmp_path / "eval" / "dev" / "test.jsonl", tmp_path / "other" / "dev.jsonl"):
        with pytest.raises(cw.NotAllowed, match="only the DEV eval file"):
            cw.from_dev_set(bad, reg)


def test_the_repo_dev_set_yields_proposed_records_only() -> None:
    reg = load_registry(REPO)
    got = cw.from_dev_set(REPO / "eval" / "dev" / "dev.jsonl", reg)
    assert all(p.record()["state"] == "proposed" and p.proposed_by == "dev_eval_synonym" for p in got)


def test_merge_keeps_reviewed_records_and_adds_only_new_proposals() -> None:
    accepted = {"record_id": "r1", "dataset": "d", "label": "L", "state": "accepted", "reviewer": "m"}
    p = cw.Proposal("d", "L", "string_match", gbd_cause="Syphilis")
    out = cw.merge([accepted], [p, p])
    assert out[0] == accepted and [r["record_id"] for r in out] == ["r1", p.record_id]
    assert cw.merge(out, [p]) == out

    class Reviewed(cw.Proposal):
        def record(self) -> dict[str, object]:
            return {**super().record(), "state": "accepted"}

    with pytest.raises(cw.NotAllowed, match="only proposed records"):
        cw.merge([], [Reviewed("d", "L", "string_match")])
    for field, value in (("reviewer", "me"), ("reviewed_at", "2026-11-25")):

        class Signed(cw.Proposal):
            def record(self, f: str = field, v: str = value) -> dict[str, object]:
                return {**super().record(), f: v}

        with pytest.raises(cw.NotAllowed):
            cw.merge([], [Signed("d", "L", "string_match")])


def test_exports_are_new_complete_files_never_overwritten(tmp_path: Path) -> None:
    assert cw.latest_export(tmp_path) == []
    when = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
    first = cw.write_export(tmp_path, [{"record_id": "a", "state": "accepted"}], now=when)
    assert first == tmp_path / "condition_crosswalk" / "20261125T060000Z-proposals.ndjson"
    rec = json.loads(first.read_text().splitlines()[0])
    assert list(rec) == list(cw.FIELDS) and rec["record_id"] == "a" and rec["label"] is None
    with pytest.raises(FileExistsError, match="never overwritten"):
        cw.write_export(tmp_path, [], now=when)
    later = cw.write_export(tmp_path, [{"record_id": "b"}], now=datetime(2026, 11, 26, tzinfo=UTC))
    assert first.exists() and [r["record_id"] for r in cw.latest_export(tmp_path)] == ["b"]
    assert later.name.startswith("20261126")


def test_main_proposes_from_the_catalog_and_dev_then_writes_nothing_new(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = toy_root(tmp_path / "reg")
    out = tmp_path / "cp"
    dev = dev_file(tmp_path, question("legionellosis!", ["toy_cases"]))
    assert cw.main(["--root", str(root), "--to", str(out), "--dev", str(dev)]) == 0
    assert "Wrote 3 new proposed records to" in capsys.readouterr().out
    records = cw.latest_export(out)
    assert sorted((r["label"], r["proposed_by"]) for r in records) == [
        ("Legionellosis", "dev_eval_synonym"), ("Legionellosis", "string_match"), ("Syphilis", "string_match")]  # fmt: skip
    assert all(r["state"] == "proposed" for r in records)
    assert cw.main(["--root", str(root), "--to", str(out), "--dev", str(dev)]) == 0
    assert capsys.readouterr().out == "No new proposals; nothing written.\n"
    assert len(list((out / "condition_crosswalk").glob("*.ndjson"))) == 1


def test_main_writes_to_the_control_plane_directory_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HDR_CONTROL_PLANE", str(tmp_path / "plane"))
    assert cw.main(["--root", str(toy_root(tmp_path / "reg"))]) == 0
    assert capsys.readouterr().out.startswith("Wrote 2 new proposed records to ")
    assert {r["proposed_by"] for r in cw.latest_export(tmp_path / "plane")} == {"string_match"}
    out = tmp_path / "one"
    cw.write_export(
        out, [cw.Proposal("d", "L", "string_match").record()], now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    monkeypatch.setenv("HDR_CONTROL_PLANE", str(out))
    assert cw.main(["--root", str(toy_root(tmp_path / "reg2"))]) == 0
    assert capsys.readouterr().out.startswith("Wrote 2 new proposed records")
    assert len(cw.latest_export(out)) == 3
