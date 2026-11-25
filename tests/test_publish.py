"""export-public: the hub release, listed-not-hosted cards, pre-publish checks and the upload. Synthetic data only."""

from __future__ import annotations

import json
import re
import shutil
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
import yaml

from health_data_registry import cli, publish
from health_data_registry.product import load_ports
from health_data_registry.publish import (
    PublishCheckFailed,
    check_release,
    export_public,
    read_front_matter,
    sha256_file,
    stage,
    suppression_problems,
)
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, latest_snapshot, write_ndjson
from tests.conftest import CATALOG, CONTRACT, RECORDS, SEMANTIC, raw_fetch

RUN = "2026-11-24"
LISTED_TERMS = "https://www.who.int/about/policies/publishing/data-policy/terms-and-conditions"

ODPS = """\
apiVersion: v1.1.0
kind: DataProduct
id: toy-hub
name: Toy hub
version: 0.1.0
outputPorts:
  - {name: toy_cases, type: tables, version: 0.1.0, contractId: toy_cases,
     customProperties: [{property: hosted, value: true}]}
  - {name: toy_listed, type: tables, version: 0.1.0, contractId: toy_listed,
     customProperties: [{property: hosted, value: false}]}
"""


def write_hub_registry(root: Path, *, areas: str = "[respiratory, stis]", licence_text: bool = True) -> Path:
    for d in ("catalog", "contracts", "semantic", "licenses"):
        (root / d).mkdir(parents=True, exist_ok=True)
    cat = CATALOG.replace(
        "research_areas: [infectious_disease]\n", f"research_areas: [infectious_disease]\nid_areas: {areas}\n"
    )
    (root / "catalog" / "toy_cases.yaml").write_text(cat)
    (root / "contracts" / "toy_cases.odcs.yaml").write_text(CONTRACT)
    (root / "semantic" / "toy_cases.yaml").write_text(SEMANTIC)
    listed = cat.replace("toy_cases", "toy_listed").replace(
        "licence: {id: public-domain-us-federal, redistribution: true, "
        'terms_url: "https://www.cdc.gov/other/agencymaterials.html"}',
        f'licence: {{id: CC-BY-NC-SA-3.0-IGO, redistribution: false, terms_url: "{LISTED_TERMS}"}}',
    )
    listed = listed.replace("hosting: public\n", "")
    assert "redistribution: false" in listed and "hosting:" not in listed
    (root / "catalog" / "toy_listed.yaml").write_text(listed)
    (root / "contracts" / "toy_listed.odcs.yaml").write_text(CONTRACT.replace("toy_cases", "toy_listed"))
    (root / "semantic" / "toy_listed.yaml").write_text(SEMANTIC.replace("toy_cases", "toy_listed"))
    (root / "contracts" / "health-data-registry.odps.yaml").write_text(ODPS)
    if licence_text:
        (root / "licenses" / "public-domain-us-federal.md").write_text(
            "US federal works are public domain.\n"
        )
    return root


def make_lake(lake: Path, registry: Registry, tmp: Path, *, partial: bool = False) -> Path:
    lake.mkdir(parents=True, exist_ok=True)
    f = tmp / "page.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(
        lake,
        registry.get("toy_cases"),
        [raw_fetch("https://data.cdc.gov/resource/toy0-toy0.json", RECORDS)],
        [f],
        partial=partial,
    )
    return lake


@pytest.fixture
def hub_registry(tmp_path: Path) -> Registry:
    return load_registry(write_hub_registry(tmp_path / "reg"))


@pytest.fixture
def hub_lake(tmp_path: Path, hub_registry: Registry) -> Path:
    return make_lake(tmp_path / "lake", hub_registry, tmp_path)


def test_export_to_directory_manifest_hashes_and_rows(
    tmp_path: Path, hub_registry: Registry, hub_lake: Path
) -> None:
    out = tmp_path / "hub"
    rel = export_public(hub_registry, hub_lake, str(out), run_date=RUN)
    assert rel.hosted == ["toy_cases"] and rel.listed == ["toy_listed"] and rel.tag is None
    m = json.loads((out / "manifest.json").read_text())
    assert m["release_id"].startswith(f"{RUN}-") and m["run_date"] == RUN and m["tag"] == f"release-{RUN}"
    assert m["data_product"] == {"id": "toy-hub", "version": "0.1.0"}
    src = m["sources"]["toy_cases"]
    assert src["contract"] == {"id": "toy_cases", "version": "0.1.0", "status": "draft"}
    assert src["licence"]["id"] == "public-domain-us-federal" and src["licence"]["redistribution"] is True
    assert src["data_product"] == {"id": "toy-hub", "port": "toy_cases", "version": "0.1.0"}
    data = out / "toy_cases" / "data" / "toy_cases.parquet"
    assert src["files"]["data/toy_cases.parquet"] == {"sha256": sha256_file(data), "rows": len(RECORDS)}
    assert m["files"]["toy_cases/data/toy_cases.parquet"]["sha256"] == sha256_file(data)
    for name, meta in src["files"].items():
        assert sha256_file(out / "toy_cases" / name) == meta["sha256"]
    assert json.loads((out / "toy_cases" / "manifest.json").read_text()) == src
    assert (out / "toy_cases" / "LICENSE.md").read_text() == "US federal works are public domain.\n"
    # The data file is the snapshot's, byte for byte.
    snap_dir, snap = latest_snapshot(hub_lake, "toy_cases") or (None, None)
    assert snap_dir is not None and snap is not None
    assert sha256_file(snap_dir / "data.parquet") == sha256_file(data)
    assert src["snapshot"]["snapshot_id"] == snap.snapshot_id
    # The same inputs give the same release id.
    again = export_public(hub_registry, hub_lake, str(tmp_path / "hub2"), run_date=RUN)
    assert again.release_id == rel.release_id
    assert export_public(hub_registry, hub_lake, str(tmp_path / "h3"), run_date="2026-12-01").release_id != (
        rel.release_id
    )
    assert any("toy_listed" in line and "listed" in line for line in rel.summary())


def test_restricted_source_has_card_but_no_data(
    tmp_path: Path, hub_registry: Registry, hub_lake: Path
) -> None:
    out = tmp_path / "hub"
    export_public(hub_registry, hub_lake, str(out), run_date=RUN)
    files = sorted(p.relative_to(out / "toy_listed").as_posix() for p in (out / "toy_listed").rglob("*"))
    assert files == ["README.md", "manifest.json"]
    sm = json.loads((out / "toy_listed" / "manifest.json").read_text())
    assert sm["hosted"] is False and sm["snapshot"] is None and list(sm["files"]) == ["README.md"]
    card = (out / "toy_listed" / "README.md").read_text()
    head = read_front_matter(card)
    assert "configs" not in head and "listed-not-hosted" in head["tags"]
    assert head["license_name"] == "CC-BY-NC-SA-3.0-IGO" and head["license_link"] == LISTED_TERMS
    assert "reads it live from the publisher each time you ask" in card and "keeps no copy" in card
    assert "https://data.cdc.gov/d/toy0-toy0" in card and LISTED_TERMS in card
    assert "Cite as: CDC. Toy table." in card
    hub = read_front_matter((out / "README.md").read_text())
    assert [c["config_name"] for c in hub["configs"]] == ["toy_cases"]
    assert "listed, not hosted" in (out / "README.md").read_text()


def test_dataset_card_contents(tmp_path: Path, hub_registry: Registry, hub_lake: Path) -> None:
    out = tmp_path / "hub"
    export_public(hub_registry, hub_lake, str(out), run_date=RUN)
    card = (out / "toy_cases" / "README.md").read_text()
    head = read_front_matter(card)
    assert head["license"] == "other" and head["license_name"] == "public-domain-us-federal"
    assert head["license_link"] == "https://www.cdc.gov/other/agencymaterials.html"
    for tag in [
        "infectious-disease",
        "respiratory-infections",
        "sexually-transmitted-infections",
        "legionellosis",
        "syphilis",
        "lower-respiratory-infections",
        "geography-state",
        "country-us",
        "publisher-cdc",
    ]:
        assert tag in head["tags"], tag
    assert head["configs"] == [
        {
            "config_name": "toy_cases",
            "data_files": [{"split": "train", "path": "toy_cases/data/toy_cases.parquet"}],
        }
    ]
    for text in [
        "## What one row is\n\nOne row per reporting area, condition and year.",
        "CSTE 2005 (09-ID-04); case status: confirmed and probable",
        "Years 2022 to 2024. Geography: nation, state (US).",
        "never filled in, back-calculated or set to zero",
        "`cases`: the publisher's codes '-', 'N' read as empty",
        "- Counts for the latest year are provisional.",
        "contract is a draft",
        "Not yet confirmed: Is 'N' a true zero or not notifiable?",
        "Cite as: CDC. Toy table. https://data.cdc.gov/d/toy0-toy0",
        "Licence: public-domain-us-federal",
        f"`@release-{RUN}`",
        f"| `toy_cases/data/toy_cases.parquet` | {len(RECORDS)} |",
        "| `rate_per_100k` | Cases per 100,000 residents, as published. |",
    ]:
        assert text in card, text
    hub = read_front_matter((out / "README.md").read_text())
    assert "respiratory-infections" in hub["tags"]


def test_hub_card_links_repo_when_publishing_to_hub(
    hub_registry: Registry, hub_lake: Path, tmp_path: Path
) -> None:
    calls: list[tuple[Path, str, dict[str, Any]]] = []

    def fake(out: Path, dest: str, release: dict[str, Any]) -> str:
        calls.append((out, dest, release))
        return "release-x"

    rel = export_public(
        hub_registry,
        hub_lake,
        "hf://datasets/org/hub",
        run_date=RUN,
        uploader=fake,
        staging=tmp_path / "st",
        api=FakeApi(tmp=tmp_path),
    )
    assert (
        rel.tag == "release-x" and calls[0][1] == "hf://datasets/org/hub" and calls[0][0] == tmp_path / "st"
    )
    card = (tmp_path / "st" / "toy_cases" / "README.md").read_text()
    assert "select * from 'hf://datasets/org/hub/toy_cases/data/toy_cases.parquet' limit 10;" in card
    assert any(line == "tagged release-x" for line in rel.summary())


def test_hub_target_needs_token(hub_registry: Registry, hub_lake: Path) -> None:
    with pytest.raises(PublishCheckFailed, match="HF_TOKEN"):
        export_public(hub_registry, hub_lake, "hf://datasets/org/hub", run_date=RUN)


def _fill_suppressed(path: Path) -> None:
    con = duckdb.connect(":memory:")
    tmp = path.with_suffix(".tmp.parquet")
    con.execute(
        f"COPY (SELECT * REPLACE (coalesce(cases, 0) AS cases) FROM read_parquet('{path}')) TO '{tmp}' (FORMAT parquet)"
    )
    con.close()
    tmp.replace(path)


def test_filled_in_suppressed_cell_fails_the_check(
    tmp_path: Path, hub_registry: Registry, hub_lake: Path
) -> None:
    out = tmp_path / "hub"
    stage(hub_registry, hub_lake, out, run_date=RUN, repo=None)
    assert check_release(hub_registry, hub_lake, out) == []
    _fill_suppressed(out / "toy_cases" / "data" / "toy_cases.parquet")
    problems = check_release(hub_registry, hub_lake, out)
    assert any("toy_cases.cases: 1 suppressed or missing cells were filled in" in p for p in problems)
    assert any("does not match its sha256" in p for p in problems)


def test_suppression_problems_names_each_kind(tmp_path: Path, hub_lake: Path) -> None:
    snap_dir = (latest_snapshot(hub_lake, "toy_cases") or (tmp_path, None))[0]
    db = snap_dir / "data.duckdb"
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{db}' AS s (READ_ONLY)")

    def export(sql: str) -> Path:
        p = tmp_path / f"x{abs(hash(sql))}.parquet"
        con.execute(f"COPY ({sql}) TO '{p}' (FORMAT parquet)")
        return p

    assert suppression_problems(db, "toy_cases", export("SELECT * FROM s.toy_cases")) == []
    emptied = export(
        "SELECT * REPLACE (CASE WHEN year = 2022 THEN NULL ELSE cases END AS cases) FROM s.toy_cases"
    )
    assert suppression_problems(db, "toy_cases", emptied) == [
        "toy_cases.cases: 1 published values were emptied"
    ]
    changed = export("SELECT * REPLACE (cases + 1 AS cases) FROM s.toy_cases")
    assert suppression_problems(db, "toy_cases", changed) == [
        "toy_cases: 5 snapshot rows are changed or missing in the published table"
    ]
    fewer = export("SELECT * FROM s.toy_cases WHERE cases IS NOT NULL")
    assert suppression_problems(db, "toy_cases", fewer)[0].startswith("toy_cases.cases: ")
    dropped = export("SELECT * EXCLUDE (rate_per_100k) FROM s.toy_cases")
    assert "published columns differ" in suppression_problems(db, "toy_cases", dropped)[0]
    con.close()


def test_card_without_citation_fails(tmp_path: Path, hub_registry: Registry, hub_lake: Path) -> None:
    out = tmp_path / "hub"
    stage(hub_registry, hub_lake, out, run_date=RUN, repo=None)
    for sid in ("toy_cases", "toy_listed"):
        card = out / sid / "README.md"
        card.write_text(card.read_text().replace("Cite as: CDC. Toy table.", "Cite as: someone."))
    problems = check_release(hub_registry, hub_lake, out)
    assert "toy_cases: the card does not carry the publisher's citation" in problems
    assert "toy_listed: the card does not carry the publisher's citation" in problems


def test_missing_id_area_tag_fails(tmp_path: Path, hub_registry: Registry, hub_lake: Path) -> None:
    out = tmp_path / "hub"
    stage(hub_registry, hub_lake, out, run_date=RUN, repo=None)
    card = out / "toy_cases" / "README.md"
    card.write_text(card.read_text().replace("- respiratory-infections\n", ""))
    assert check_release(hub_registry, hub_lake, out) == [
        "toy_cases: the hub does not list it under its ID area tag respiratory-infections",
        "toy_cases: README.md does not match its sha256 in manifest.json",
    ]
    reg = load_registry(write_hub_registry(tmp_path / "noarea", areas="[]"))
    with pytest.raises(PublishCheckFailed, match=r"toy_cases: no ID area is recorded"):
        export_public(reg, hub_lake, str(tmp_path / "o2"), run_date=RUN)


def test_licence_must_be_recorded(tmp_path: Path, hub_lake: Path) -> None:
    reg = load_registry(write_hub_registry(tmp_path / "nolic", licence_text=False))
    with pytest.raises(PublishCheckFailed, match=r"licenses/public-domain-us-federal\.md"):
        export_public(reg, hub_lake, str(tmp_path / "o"), run_date=RUN)


def test_snapshot_under_other_licence_or_partial_fails(tmp_path: Path, hub_registry: Registry) -> None:
    lake = make_lake(tmp_path / "plake", hub_registry, tmp_path, partial=True)
    with pytest.raises(PublishCheckFailed, match="partial development sample"):
        export_public(hub_registry, lake, str(tmp_path / "o"), run_date=RUN)
    lake2 = make_lake(tmp_path / "lake2", hub_registry, tmp_path)
    snap_dir, _ = latest_snapshot(lake2, "toy_cases") or (tmp_path, None)
    mpath = snap_dir / "manifest.json"
    doc = json.loads(mpath.read_text())
    mpath.write_text(json.dumps(doc | {"licence": "something-else"}))
    with pytest.raises(PublishCheckFailed, match="taken under licence something-else"):
        export_public(hub_registry, lake2, str(tmp_path / "o2"), run_date=RUN)


@pytest.mark.parametrize(
    ("row_count", "problem"),
    [
        (0, "the snapshot has no rows; an empty table is never published"),
        (-1, "the snapshot has no rows; an empty table is never published"),
        (999, "rows but its snapshot's manifest says 999"),
    ],
)
def test_empty_or_miscounted_snapshot_is_never_published(
    tmp_path: Path, hub_registry: Registry, row_count: int, problem: str
) -> None:
    """build-data-pipeline: the rows published must match the snapshot's manifest, and never be zero."""
    lake = make_lake(tmp_path / "lake", hub_registry, tmp_path)
    snap_dir, manifest = latest_snapshot(lake, "toy_cases") or (tmp_path, None)
    assert manifest is not None and manifest.row_count > 0
    mpath = snap_dir / "manifest.json"
    mpath.write_text(json.dumps(json.loads(mpath.read_text()) | {"row_count": row_count}))
    with pytest.raises(PublishCheckFailed, match=re.escape(problem)):
        export_public(hub_registry, lake, str(tmp_path / "o"), run_date=RUN)


def test_no_snapshot_stops_the_export(tmp_path: Path, hub_registry: Registry) -> None:
    with pytest.raises(PublishCheckFailed, match=r"no snapshot .*health-data-registry fetch toy_cases"):
        export_public(hub_registry, tmp_path / "empty", str(tmp_path / "o"), run_date=RUN)
    with pytest.raises(PublishCheckFailed, match="YYYY-MM-DD"):
        export_public(hub_registry, tmp_path / "empty", str(tmp_path / "o"), run_date="24/11/2026")


def test_private_hosting_is_listed_not_hosted_and_refused_if_staged(tmp_path: Path) -> None:
    """A redistributable source that isn't `hosting: public` gets no data files, and staged ones are refused."""
    root = write_hub_registry(tmp_path / "reg")
    cat = root / "catalog" / "toy_cases.yaml"
    cat.write_text(cat.read_text().replace("hosting: public\n", ""))
    reg = load_registry(root)
    assert reg.get("toy_cases").entry.hosting == "private"
    lake = make_lake(tmp_path / "lake", reg, tmp_path)
    out = tmp_path / "hub"
    stage(reg, lake, out, run_date=RUN, repo=None)
    assert not (out / "toy_cases" / "data").exists()
    card = (out / "toy_cases" / "README.md").read_text()
    assert "listed, not hosted" in card and "stays private until a maintainer" in card
    assert check_release(reg, lake, out) == []
    (out / "toy_cases" / "data").mkdir()
    shutil.copyfile(lake / "datasets" / "toy_cases" / "LATEST", out / "toy_cases" / "data" / "x.parquet")
    assert check_release(reg, lake, out) == [
        "toy_cases: it is not `hosting: public` in its catalog entry but data files were staged"
    ]


def test_restricted_data_staged_by_mistake_fails(
    tmp_path: Path, hub_registry: Registry, hub_lake: Path
) -> None:
    out = tmp_path / "hub"
    stage(hub_registry, hub_lake, out, run_date=RUN, repo=None)
    (out / "toy_listed" / "data").mkdir()
    shutil.copyfile(
        out / "toy_cases" / "data" / "toy_cases.parquet", out / "toy_listed" / "data" / "x.parquet"
    )
    assert check_release(hub_registry, hub_lake, out) == [
        "toy_listed: its terms forbid redistribution but data files were staged"
    ]
    shutil.rmtree(out / "toy_listed" / "data")
    (out / "toy_cases" / "data" / "toy_cases.parquet").unlink()
    problems = check_release(hub_registry, hub_lake, out)
    assert "toy_cases: the data file is missing" in problems


def test_checklist_samples_rows_for_manual_check(
    tmp_path: Path, hub_registry: Registry, hub_lake: Path
) -> None:
    out = tmp_path / "hub"
    export_public(hub_registry, hub_lake, str(out), run_date=RUN)
    text = (out / publish.CHECKLIST).read_text()
    assert text.startswith("# Publish checklist: release ")
    assert "Publisher's page: https://data.cdc.gov/d/toy0-toy0" in text
    assert text.count("- [ ] ") == publish.SAMPLE_ROWS
    assert "toy_listed" not in text and "## Toy notifiable condition" in text
    assert publish.CHECKLIST not in [p.name for p in publish.upload_files(out)]


def test_helpers() -> None:
    assert publish.hf_repo_id("hf://datasets/org/name@release-2026-11-24/x") == "org/name"
    for bad in ("hf://datasets/org", "s3://b/p", "hf://datasets//name"):
        with pytest.raises(ValueError):
            publish.hf_repo_id(bad)
    assert publish.release_tag(RUN) == "release-2026-11-24"
    assert publish.release_id(RUN, ["b", "a"]) == publish.release_id(RUN, ["a", "b"])
    assert publish.release_id(RUN, ["a"]) != publish.release_id(RUN, ["b"])
    assert len(publish.release_id(RUN, ["a"])) == len(RUN) + 13
    assert publish.slug("Syphilis, Primary and secondary") == "syphilis-primary-and-secondary"
    assert read_front_matter("no header") == {}


class FakeApi:
    """Stands in for huggingface_hub.HfApi: published tags map to the release id in their manifest."""

    def __init__(
        self, tags: dict[str, str] | None = None, tmp: Path | None = None, changelog: str = ""
    ) -> None:
        self.tags = dict(tags or {})
        self.tmp = tmp
        self.changelog = changelog
        self.calls: list[tuple[str, Any]] = []

    def create_repo(self, repo_id: str, **kw: Any) -> None:
        self.calls.append(("create_repo", (repo_id, kw)))

    def create_commit(self, **kw: Any) -> Any:
        self.calls.append(("create_commit", kw))
        return SimpleNamespace(oid="abc123")

    def list_repo_refs(self, repo_id: str, **kw: Any) -> Any:
        assert kw == {"repo_type": "dataset"}
        return SimpleNamespace(tags=[SimpleNamespace(name=t) for t in [*self.tags, "v-other"]])

    def hf_hub_download(self, repo_id: str, filename: str, **kw: Any) -> str:
        from huggingface_hub.errors import EntryNotFoundError

        assert self.tmp is not None and kw["repo_type"] == "dataset"
        if filename == "CHANGELOG.md":
            if not self.changelog:
                raise EntryNotFoundError("none")
            p = self.tmp / "old-changelog.md"
            p.write_text(self.changelog)
            return str(p)
        p = self.tmp / "tagged-manifest.json"
        p.write_text(json.dumps({"release_id": self.tags[kw["revision"]]}))
        return str(p)

    def delete_tag(self, repo_id: str, **kw: Any) -> None:  # pragma: no cover - must never be called
        raise AssertionError("a published tag was deleted")

    def create_tag(self, repo_id: str, **kw: Any) -> None:
        self.calls.append(("create_tag", kw))


def _export(tmp_path: Path, reg: Registry, lake: Path, api: FakeApi) -> Any:
    return export_public(reg, lake, "hf://datasets/org/hub", run_date=RUN, staging=tmp_path / "st", api=api)


def test_upload_one_commit_then_new_tag(tmp_path: Path, hub_registry: Registry, hub_lake: Path) -> None:
    api = FakeApi(tmp=tmp_path)
    rel = _export(tmp_path, hub_registry, hub_lake, api)
    assert rel.tag == f"release-{RUN}" and not rel.unchanged
    names = [c[0] for c in api.calls]
    assert names == ["create_repo", "create_commit", "create_tag"]
    assert api.calls[0][1] == ("org/hub", {"repo_type": "dataset", "private": False, "exist_ok": True})
    commit = api.calls[1][1]
    paths = sorted(op.path_in_repo for op in commit["operations"])
    assert paths == [
        "CHANGELOG.md",
        "README.md",
        "manifest.json",
        "toy_cases/LICENSE.md",
        "toy_cases/README.md",
        "toy_cases/data/toy_cases.parquet",
        "toy_cases/manifest.json",
        "toy_listed/README.md",
        "toy_listed/manifest.json",
    ]
    assert commit["repo_id"] == "org/hub" and commit["repo_type"] == "dataset"
    assert commit["commit_message"] == f"Weekly release {rel.release_id} (run {RUN})"
    tag = api.calls[2][1]
    assert tag["tag"] == f"release-{RUN}" and tag["revision"] == "abc123" and tag["repo_type"] == "dataset"
    assert tag["tag_message"] == f"release {rel.release_id}" and tag["exist_ok"] is False
    log = (tmp_path / "st" / "CHANGELOG.md").read_text()
    assert log.startswith(f"# Changelog\n\n## release-{RUN}: release {rel.release_id} (run {RUN})\n")
    assert "- Hosted: toy_cases\n- Listed, not hosted: toy_listed\n" in log and "Correction" not in log
    m = json.loads((tmp_path / "st" / "manifest.json").read_text())
    assert m["tag"] == f"release-{RUN}" and m["supersedes"] is None


def test_identical_release_is_not_published_again(
    tmp_path: Path, hub_registry: Registry, hub_lake: Path
) -> None:
    first = _export(tmp_path, hub_registry, hub_lake, FakeApi(tmp=tmp_path))
    api = FakeApi({f"release-{RUN}": first.release_id}, tmp_path)
    again = _export(tmp_path, hub_registry, hub_lake, api)
    assert again.unchanged and again.tag == f"release-{RUN}" and api.calls == []
    assert f"already published as release-{RUN}; nothing uploaded" in again.summary()


def test_correction_gets_a_new_tag_and_never_moves_one(
    tmp_path: Path, hub_registry: Registry, hub_lake: Path
) -> None:
    old = f"# Changelog\n\n## release-{RUN}: release old-id (run {RUN})\n"
    api = FakeApi({f"release-{RUN}": "old-id", "release-2026-11-17": "older"}, tmp_path, changelog=old)
    rel = _export(tmp_path, hub_registry, hub_lake, api)
    assert rel.tag == f"release-{RUN}.2"
    assert [c[0] for c in api.calls] == ["create_repo", "create_commit", "create_tag"]
    assert api.calls[2][1]["tag"] == f"release-{RUN}.2"
    assert api.calls[1][1]["commit_message"].endswith(", supersedes old-id)")
    m = json.loads((tmp_path / "st" / "manifest.json").read_text())
    assert (
        m["supersedes"] == "old-id"
        and m["supersedes_tag"] == f"release-{RUN}"
        and m["tag"] == f"release-{RUN}.2"
    )
    log = (tmp_path / "st" / "CHANGELOG.md").read_text()
    assert log.index(f"## release-{RUN}.2") < log.index(f"## release-{RUN}: release old-id")
    assert f"supersedes release old-id (release-{RUN}), which stays published unchanged" in log
    assert log.count("# Changelog") == 1
    card = (tmp_path / "st" / "toy_cases" / "README.md").read_text()
    assert f"`@release-{RUN}.2`" in card
    # A third export of other data that day supersedes the .2 correction.
    api3 = FakeApi({f"release-{RUN}": "old-id", f"release-{RUN}.2": "second"}, tmp_path)
    assert _export(tmp_path, hub_registry, hub_lake, api3).tag == f"release-{RUN}.3"


def test_upload_refuses_an_existing_tag(tmp_path: Path, hub_registry: Registry, hub_lake: Path) -> None:
    _export(tmp_path, hub_registry, hub_lake, FakeApi(tmp=tmp_path))
    release = json.loads((tmp_path / "st" / "manifest.json").read_text())
    api = FakeApi({f"release-{RUN}": "someone-else"}, tmp_path)
    with pytest.raises(PublishCheckFailed, match="never moved"):
        publish.upload_hf(tmp_path / "st", "hf://datasets/org/hub", release, api=api)
    assert [c[0] for c in api.calls] == ["create_repo"]


def test_plan_release() -> None:
    assert publish.plan_release(RUN, "a", {}) == publish.ReleasePlan(tag=f"release-{RUN}")
    plan = publish.plan_release(RUN, "b", {f"release-{RUN}": "a", "release-2026-11-17": "z"})
    assert (plan.tag, plan.supersedes, plan.supersedes_tag) == (f"release-{RUN}.2", "a", f"release-{RUN}")
    same = publish.plan_release(RUN, "a", {f"release-{RUN}": "a"})
    assert same.already_published == f"release-{RUN}"
    assert publish.plan_release(RUN, "c", {f"release-{RUN}.9x": "q"}).tag == f"release-{RUN}"
    assert publish.tag_order(f"release-{RUN}") == 1 and publish.tag_order(f"release-{RUN}.12") == 12


def test_new_hub_has_no_releases(tmp_path: Path) -> None:
    from huggingface_hub.errors import RepositoryNotFoundError

    class Missing:
        def list_repo_refs(self, *a: Any, **kw: Any) -> Any:
            raise RepositoryNotFoundError(
                "no repo", response=SimpleNamespace(headers={}, status_code=404, request=None)
            )

    assert publish.published_releases(Missing(), "org/new") == ({}, "")


def test_real_product_file_matches_catalog_and_contracts() -> None:
    reg = load_registry()
    ports = load_ports(reg.root)
    assert set(ports) == {ds.contract.id for ds in reg}
    for ds in reg:
        p = ports[ds.contract.id]
        assert p.name == ds.id and p.version == ds.contract.version
        assert p.hosted == ds.entry.hosted
        assert p.product_id == "health-data-registry-hub"
        assert ds.entry.id_areas or "infectious_disease" not in ds.entry.research_areas, ds.id
    doc = yaml.safe_load((reg.root / "contracts" / "health-data-registry.odps.yaml").read_text())
    assert doc["apiVersion"] == "v1.1.0" and doc["kind"] == "DataProduct"
    # Every redistributable source has a recorded licence text to ship beside its data.
    for ds in reg:
        if ds.entry.licence.redistribution:
            assert (reg.root / "licenses" / f"{ds.entry.licence.id}.md").is_file()


def test_load_ports_rejects_other_kinds(tmp_path: Path) -> None:
    assert load_ports(tmp_path) == {}
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "health-data-registry.odps.yaml").write_text("kind: DataContract\n")
    with pytest.raises(ValueError, match="DataProduct"):
        load_ports(tmp_path)


def test_cli_export_public_and_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    root = write_hub_registry(tmp_path / "reg")
    monkeypatch.setenv("HDR_REGISTRY", str(root))
    monkeypatch.setenv("HDR_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(cli, "_fetcher", lambda reg, log=None: lambda url: raw_fetch(url, RECORDS))
    assert cli.main(["export-public", "--to", str(tmp_path / "o"), "--run-date", RUN]) == 1
    assert "export stopped" in capsys.readouterr().err
    assert cli.main(["fetch", "toy_cases"]) == 0
    assert cli.main(["export-public", "--to", str(tmp_path / "o"), "--run-date", RUN]) == 0
    printed = capsys.readouterr().out
    assert f"release {RUN}-" in printed and "hosted toy_cases" in printed
    assert (tmp_path / "o" / "toy_cases" / "data" / "toy_cases.parquet").is_file()


def test_card_text_is_plain_yaml_header() -> None:
    text = textwrap.dedent(publish._front_matter({"a": 1}))
    assert text == "---\na: 1\n---\n"
    assert datetime.now(UTC).year >= 2026


def test_cards_say_how_to_add_to_a_claude_project(
    tmp_path: Path, hub_registry: Registry, hub_lake: Path
) -> None:
    export_public(
        hub_registry,
        hub_lake,
        "hf://datasets/org/hub",
        run_date=RUN,
        staging=tmp_path / "st",
        api=FakeApi(tmp=tmp_path),
    )
    card = (tmp_path / "st" / "toy_cases" / "README.md").read_text()
    section = card.split("## Add it to a Claude project\n\n", 1)[1]
    assert "- Plugin (coming): the clinician template will ship as a Claude Code plugin" in section
    assert f"It reads this dataset from hf://datasets/org/hub@release-{RUN}, so nothing" in section
    assert "download `toy_cases/data/toy_cases.parquet` from that release" in section
    assert "TO 'toy_cases.csv';" in section and " MB." in section
    assert "about 30 MB per file when this was written; unverified" in section
    listed = (tmp_path / "st" / "toy_listed" / "README.md").read_text()
    lsec = listed.split("## Add it to a Claude project\n\n", 1)[1]
    assert "listed, not hosted" in lsec and "reads it live from the publisher and keeps no copy" in lsec
    assert "https://data.cdc.gov/d/toy0-toy0" in lsec and "unverified" in lsec


def test_manifest_quality_section(tmp_path: Path, hub_registry: Registry, hub_lake: Path) -> None:
    out = tmp_path / "hub"
    export_public(hub_registry, hub_lake, str(out), run_date=RUN)
    m = json.loads((out / "manifest.json").read_text())
    assert m["sources"]["toy_cases"]["quality"] == {"tool": None, "results": []}
    results = {
        "toy_cases": [
            {"test": "not_null_year", "checks": "every row has a year", "status": "pass", "rows_affected": 0},
            {"test": "rates_in_range", "checks": "rates are between 0 and 100,000", "status": "warn"},
        ]
    }
    export_public(hub_registry, hub_lake, str(out), run_date=RUN, quality=results)
    q = json.loads((out / "toy_cases" / "manifest.json").read_text())["quality"]
    assert q["tool"] == "dbt"
    assert q["results"] == [
        {"test": "not_null_year", "checks": "every row has a year", "status": "pass", "rows_affected": 0},
        {
            "test": "rates_in_range",
            "checks": "rates are between 0 and 100,000",
            "status": "warn",
            "rows_affected": None,
        },
    ]
    with pytest.raises(PublishCheckFailed, match="not pass/warn/fail"):
        publish.quality_section([{"test": "x", "status": "ok"}])


def test_export_never_deletes_a_directory_it_did_not_stage(tmp_path: Path) -> None:
    """Outside review (b): `export-public --to .` must not delete the checkout."""
    from health_data_registry.publish import STAGING_MARK, PublishCheckFailed, prepare_staging

    mine = tmp_path / "checkout"
    mine.mkdir()
    (mine / "README.md").write_text("keep me")
    with pytest.raises(PublishCheckFailed, match="didn't stage"):
        prepare_staging(mine)
    assert (mine / "README.md").read_text() == "keep me"
    a_file = tmp_path / "f.txt"
    a_file.write_text("x")
    with pytest.raises(PublishCheckFailed, match="not a directory"):
        prepare_staging(a_file)
    fresh = tmp_path / "new" / "release"
    prepare_staging(fresh)
    (fresh / "old.json").write_text("{}")
    prepare_staging(fresh)  # an earlier staging is emptied
    assert sorted(p.name for p in fresh.iterdir()) == [STAGING_MARK]
    empty = tmp_path / "empty"
    empty.mkdir()
    prepare_staging(empty)
    assert (empty / STAGING_MARK).is_file()
