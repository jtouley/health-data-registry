"""The hosted dataset card, the per-source manifest and the citation, to the character (issue #213).

The card is what a clinician reads on the open data hub before using a hosted copy, and the citation is what they
paste into a paper; both are public text, so a change to either should be a decision, not an accident. To change
the card on purpose, rewrite the golden file from the new output (`HDR_WRITE_GOLDEN=1 uv run pytest
tests/test_publish_card.py`) and review the diff.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

import pytest

from health_data_registry import publish
from health_data_registry.registry import Dataset, load_registry
from health_data_registry.store import Manifest
from tests.test_publish import write_hub_registry

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "golden" / "hosted_card_toy.md"


def _manifest(ds: Dataset) -> Manifest:
    return Manifest(
        dataset=ds.id, snapshot_id="20261124T000000Z-000000000000", contract_version=ds.contract.version,
        semantic_version=ds.semantic.version, catalog_status=ds.entry.status,
        retrieved_at="2026-11-24T08:30:00+00:00", source_urls=["https://data.cdc.gov/resource/a.json"],
        raw_sha256=["0" * 64], row_count=12, licence=ds.entry.licence.id, redistribution=True, partial=False,
    )  # fmt: skip


@pytest.fixture
def toy(tmp_path: Path) -> Dataset:
    return load_registry(write_hub_registry(tmp_path / "reg")).get("toy_cases")


def _card(ds: Dataset, repo: str | None) -> str:
    return publish.hosted_card(
        ds, _manifest(ds), release="2026-11-24", run_date="2026-11-24", rows=12, sha="a" * 64, repo=repo,
        size=2048,
    )  # fmt: skip


def test_the_hosted_card_is_exactly_the_reviewed_text(toy: Dataset) -> None:
    card = _card(toy, "org/hub")
    if os.environ.get("HDR_WRITE_GOLDEN") == "1":
        GOLDEN.write_text(card, encoding="utf-8")
    assert card == GOLDEN.read_text(encoding="utf-8")


def test_a_card_without_a_hub_repo_points_at_the_file_in_the_release(toy: Dataset) -> None:
    card = _card(toy, None)
    assert "hf://datasets/" not in card
    assert f"FROM '{publish.data_path(toy.id)}'" in card or f"'{publish.data_path(toy.id)}'" in card


def test_the_source_manifest_names_the_release_the_source_and_its_terms(toy: Dataset) -> None:
    m = _manifest(toy)
    listed = publish._source_manifest(
        toy, release="r1", run_date="2026-11-24", port=None, snap=m, hosted=False
    )
    assert list(listed) == ["release_id", "run_date", "source", "hosted", "contract", "data_product", "licence",
                            "citation", "snapshot", "quality", "files"]  # fmt: skip
    assert (listed["release_id"], listed["run_date"], listed["source"]) == ("r1", "2026-11-24", "toy_cases")
    assert listed["licence"]["terms_url"] == toy.entry.licence.terms_url
    assert listed["snapshot"] is None  # a listed source carries no snapshot, even when one exists
    assert listed["citation"] == publish.citation(toy, m)
    hosted = publish._source_manifest(
        toy, release="r1", run_date="2026-11-24", port=None, snap=m, hosted=True
    )
    assert hosted["snapshot"] == asdict(m)


def test_the_citation_fills_in_the_access_date_or_says_how_to() -> None:
    ds = load_registry(ROOT).get("cdc_haicviz_cdi")
    assert "(accessed <retrieved_at>)" in ds.entry.citation
    m = _manifest(ds)
    assert publish.citation(ds, m) == ds.entry.citation.replace("<retrieved_at>", "2026-11-24")
    assert publish.citation(ds, None) == ds.entry.citation.replace(
        "<retrieved_at>", "the date you downloaded it"
    )
    listed = publish._source_manifest(
        ds, release="r1", run_date="2026-11-24", port=None, snap=m, hosted=False
    )
    assert "(accessed 2026-11-24)" in listed["citation"]


def test_a_card_without_a_country_and_with_an_access_date_in_its_citation(toy: Dataset) -> None:
    from dataclasses import replace

    entry = toy.entry.model_copy(update={"country": None, "citation": "CDC. Toy (accessed <retrieved_at>)."})
    ds = replace(toy, entry=entry)
    card = _card(ds, "org/hub")
    assert "Geography: nation, state.\n" in card
    assert "Cite as: CDC. Toy (accessed 2026-11-24).\n" in card
