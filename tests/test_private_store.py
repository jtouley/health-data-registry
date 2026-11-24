"""The private tier's store rules: only contract columns, keyed upserts, coverage records, hub reads, purge."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import naming, private
from health_data_registry.registry import Registry, load_registry

INDEX = "croi_abstracts_index"
TEXT = "croi_abstracts_text"


@pytest.fixture(scope="module")
def registry() -> Registry:
    return load_registry()


def text_row(n: str = "101", year: int = 2025, body: str = "Background\nA cohort.") -> dict[str, Any]:
    return {
        "abstract_number": n,
        "year": year,
        "abstract_text": body,
        "page_url": f"https://www.croiconference.org/abstract/1-{year}/",
        "retrieved_at": "2026-11-24T12:00:00+00:00",
        "page_sha256": "0" * 64,
    }


def test_rows_are_upserted_on_the_primary_key(tmp_path: Path, registry: Registry) -> None:
    ds = registry.get(TEXT)
    assert private.primary_key(ds) == ("abstract_number", "year")
    assert private.write_rows(tmp_path, ds, "year=2025", [text_row("101"), text_row("102")]) == 2
    assert private.write_rows(tmp_path, ds, "year=2025", [text_row("101", body="changed")]) == 2
    got = private.read_rows(tmp_path, ds, downloader=lambda *_: None)
    assert got is not None and got.origin == "local"
    assert {r["abstract_number"]: r["abstract_text"] for r in got.rows} == {
        "101": "changed",
        "102": "Background\nA cohort.",
    }
    folder = private.area(tmp_path, TEXT)
    assert folder.stat().st_mode & 0o777 == 0o700
    assert (folder / "year=2025.jsonl").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("row", "code"),
    [
        ({**text_row(), "poster_pdf": "bytes"}, "undeclared_field"),
        ({**text_row(), "abstract_number": ""}, "missing_required"),
        ({**text_row(), "page_url": None}, "missing_required"),
    ],
)
def test_only_contract_columns_and_required_values_are_stored(
    tmp_path: Path, registry: Registry, row: dict[str, Any], code: str
) -> None:
    with pytest.raises(private.PrivateRefused) as e:
        private.write_rows(tmp_path, registry.get(TEXT), "year=2025", [row])
    assert e.value.code == code
    assert not list(tmp_path.rglob("*.jsonl"))


@pytest.mark.parametrize("partition", ["../x", "Year=2025", "", "a/b"])
def test_bad_partitions_are_refused(tmp_path: Path, registry: Registry, partition: str) -> None:
    with pytest.raises(private.PrivateRefused) as e:
        private.write_rows(tmp_path, registry.get(TEXT), partition, [text_row()])
    assert e.value.code == "bad_partition"


def test_public_sources_have_no_private_store(tmp_path: Path, registry: Registry) -> None:
    ds = registry.get("cdc_nndss_weekly")
    assert not private.is_private(ds)
    with pytest.raises(private.PrivateRefused) as e:
        private.write_rows(tmp_path, ds, "x", [])
    assert e.value.code == "not_private_tier"
    with pytest.raises(private.PrivateRefused):
        private.write_coverage(tmp_path, ds, {})
    with pytest.raises(private.PrivateRefused):
        private.area(tmp_path, "../etc")


def test_coverage_merges_per_partition(tmp_path: Path, registry: Registry) -> None:
    ds = registry.get(INDEX)
    assert private.read_coverage(tmp_path, ds) is None
    private.write_rows(tmp_path, registry.get(TEXT), "year=2025", [text_row()])
    private.write_coverage(tmp_path, ds, {"year=2025": {"total": 3, "done": 1, "complete": False}})
    assert private.read_coverage(tmp_path, ds) is None  # no index rows held locally: not this store's record
    idx = {**text_row(), "title": "T"}
    del idx["abstract_text"]
    private.write_rows(tmp_path, ds, "year=2025", [idx])
    private.write_coverage(tmp_path, ds, {"year=2024": {"total": 2, "done": 2, "complete": True}})
    assert private.read_coverage(tmp_path, ds) == {
        "year=2025": {"total": 3, "done": 1, "complete": False},
        "year=2024": {"total": 2, "done": 2, "complete": True},
    }
    (private.area(tmp_path, INDEX) / private.COVERAGE_FILE).write_text("[1, 2]")
    assert private.read_coverage(tmp_path, ds) is None


def test_hub_rows_and_coverage_are_read_when_nothing_is_local(
    tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = registry.get(TEXT)
    monkeypatch.setenv(naming.OWNER_ENV, "org")
    monkeypatch.delenv(private.HF_TOKEN_ENV, raising=False)
    assert private.read_rows(tmp_path, ds, downloader=lambda *_: None) is None  # no token: links only
    monkeypatch.setenv(private.HF_TOKEN_ENV, "hf_read")
    asked: list[tuple[str, str]] = []

    def download(repo: str, token: str, local: Path) -> None:
        asked.append((repo, token))
        local.mkdir(parents=True, exist_ok=True)
        (local / "year=2025.jsonl").write_text(json.dumps(text_row()) + "\n")
        (local / private.COVERAGE_FILE).write_text(
            json.dumps({"year=2025": {"total": 1, "done": 1, "complete": True}})
        )

    private._hub_read_at.clear()
    got = private.read_rows(tmp_path, ds, downloader=download)
    assert got is not None and got.origin == "hub" and len(got.rows) == 1
    assert asked == [("org/health-data-registry-private-croi-abstracts-text", "hf_read")]
    assert private.read_coverage(tmp_path, ds) == {"year=2025": {"total": 1, "done": 1, "complete": True}}
    assert private.read_rows(tmp_path, ds, downloader=download) is not None
    assert len(asked) == 1  # fresh for an hour: not downloaded again

    def gone(*_: object) -> None:
        raise OSError("gone")

    private._hub_read_at.clear()
    assert private.read_rows(tmp_path / "other", ds, downloader=gone) is None


def test_purge_deletes_every_private_copy(
    tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(naming.OWNER_ENV, raising=False)
    private.write_rows(tmp_path, registry.get(TEXT), "year=2025", [text_row()])
    private.hub_cache(tmp_path, TEXT).mkdir(parents=True)
    lines = private.purge(tmp_path, TEXT)
    assert len(lines) == 2 and not private.area(tmp_path, TEXT).exists()
    assert private.purge(tmp_path, TEXT) == [f"{TEXT} had no private copy on this computer"]

    class Api:
        deleted: list[str] = []

        def delete_repo(self, repo: str, *, repo_type: str, missing_ok: bool) -> None:
            assert repo_type == "dataset" and missing_ok
            self.deleted.append(repo)

    api = Api()
    out = private.purge(tmp_path, TEXT, api=api, owner="org")
    assert api.deleted == ["org/health-data-registry-private-croi-abstracts-text"]
    assert out == ["deleted the private hub dataset org/health-data-registry-private-croi-abstracts-text"]
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert "was not deleted" in private.purge(tmp_path, TEXT, owner="org")[0]


def test_every_answer_carries_the_terms(registry: Registry) -> None:
    lines = private.notes(registry.get(TEXT))
    assert lines[0].startswith("Abstract text © CROI Foundation")
    assert any(line.startswith("Licence: croi-abstract-text") for line in lines)
    assert any(line.startswith("How to cite:") for line in lines)
    assert any(line.startswith("Suppression:") for line in lines)
