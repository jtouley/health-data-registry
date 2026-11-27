"""The private store (private.py) and how the harvester and the read path use it. Synthetic rows only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest

from health_data_registry import doi_harvest, naming, private, research_tags
from health_data_registry.adapters import doi_abstracts as doi
from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import RawFetch
from tests.test_doi_abstracts import FakeApis, cr_item, crossref_body, raw, two_pages

DOI_A, DOI_B = "10.1093/ofid/ofaf695.001", "10.1093/ofid/ofaf695.002"


@pytest.fixture
def reg() -> Registry:
    return load_registry()


def a_row(doi_: str, **over: Any) -> dict[str, Any]:
    r: dict[str, Any] = {
        "doi": doi_, "conference": "IDWeek", "year": 2025, "granularity": "abstract", "title": "T",
        "doi_url": f"https://doi.org/{doi_}", "page_url": f"https://doi.org/{doi_}", "abstract_tier": "link_only",
        "metadata_sources": "crossref", "metadata_terms": "x", "source_url": "https://api.crossref.org/x",
        "raw_sha256": "0" * 64, "retrieved_at": "2026-11-24T12:00:00+00:00",
    }  # fmt: skip
    r.update(over)
    return r


# ------------------------------------------------------------------------------------------------ write


def test_write_rows_upserts_projects_and_records_a_manifest(reg: Registry, tmp_path: Path) -> None:
    ds = reg.get("idweek_abstracts")
    assert private.write_rows(tmp_path, ds, "year=2025", [a_row(DOI_A)]) == 1
    folder = private.area(tmp_path, ds.id)
    assert private.private_root(tmp_path).stat().st_mode & 0o777 == 0o700
    assert folder.stat().st_mode & 0o777 == 0o700
    assert (folder / "year=2025.jsonl").stat().st_mode & 0o777 == 0o600
    m = private.read_manifest(folder, "year=2025")
    assert m is not None and m["rows"] == 1 and m["complete"] is False and m["partition"] == "year=2025"
    assert m["contract_version"] == ds.contract.version and m["harvester_version"]
    # A stored row with a column the contract no longer has loses it on the next write.
    path = folder / "year=2025.jsonl"
    stale = json.loads(path.read_text())
    stale["dropped_column"] = "old"
    path.write_text(json.dumps(stale) + "\n")
    assert private.write_rows(tmp_path, ds, "year=2025", [a_row(DOI_B)], complete=True) == 2
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["doi"] for r in rows] == [DOI_A, DOI_B] and all("dropped_column" not in r for r in rows)
    assert private.read_manifest(folder, "year=2025")["complete"] is True  # type: ignore[index]
    # A sample upserted onto a full read stays complete; a replace makes it exactly the rows given.
    private.write_rows(tmp_path, ds, "year=2025", [a_row(DOI_A, title="U")])
    assert private.read_manifest(folder, "year=2025")["complete"] is True  # type: ignore[index]
    assert private.write_rows(tmp_path, ds, "year=2025", [a_row(DOI_B)], replace=True) == 1
    assert private.read_manifest(folder, "year=2025")["complete"] is False  # type: ignore[index]
    assert set(private.stored_rows(tmp_path, ds, "year=2025")) == {(DOI_B,)}
    assert private.stored_rows(tmp_path, ds, "year=1999") == {}


def test_a_doi_is_stored_once_across_the_source(reg: Registry, tmp_path: Path) -> None:
    ds = reg.get("idweek_abstracts")
    private.write_rows(tmp_path, ds, "year=2024", [a_row(DOI_A, year=2024)])
    with pytest.raises(private.PrivateRefused) as e:
        private.write_rows(tmp_path, ds, "year=2025", [a_row(DOI_A), a_row(DOI_B)])
    assert e.value.code == "duplicate_key" and "year=2024" in str(e.value) and DOI_A in str(e.value)
    private.write_rows(tmp_path, ds, "year=2024", [a_row(DOI_A, title="again")])  # the same partition is fine
    with pytest.raises(private.PrivateRefused, match="bad_partition|not a partition"):
        private.write_rows(tmp_path, ds, "../x", [a_row(DOI_A)])


def test_raw_responses_stay_in_the_private_area(reg: Registry, tmp_path: Path) -> None:
    ds = reg.get("idweek_abstracts")
    r = raw("https://api.crossref.org/x", b"abstract text inside")
    private.write_raw_private(tmp_path, ds, [r, r])
    stored = private.area(tmp_path, ds.id) / "raw" / r.sha256
    assert stored.read_bytes() == b"abstract text inside" and stored.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "raw").exists()
    bad = RawFetch(url=r.url, content=b"other", sha256=r.sha256, retrieved_at=r.retrieved_at)
    with pytest.raises(private.PrivateRefused, match="don't match"):
        private.write_raw_private(tmp_path, ds, [bad])
    stored.chmod(0o600)
    stored.write_bytes(b"abstract text insid")  # cut short on disk: refused on reuse, never trusted
    with pytest.raises(private.PrivateRefused, match="no longer holds the bytes it is named for") as e:
        private.write_raw_private(tmp_path, ds, [r])
    assert e.value.code == "raw_mismatch" and stored.read_bytes() == b"abstract text insid"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(r.content)
    stored.unlink()
    stored.symlink_to(elsewhere)  # the right bytes behind a link: refused, never followed
    with pytest.raises(private.PrivateRefused, match="is a link to another file") as e:
        private.write_raw_private(tmp_path, ds, [r])
    assert e.value.code == "raw_mismatch" and stored.is_symlink()
    with pytest.raises(private.PrivateRefused):
        private.write_raw_private(tmp_path, next(d for d in reg if d.entry.tier != "private"), [r])


# ------------------------------------------------------------------------------------------------ read


def test_read_rows_reports_completeness_and_version(reg: Registry, tmp_path: Path) -> None:
    ds = reg.get("idweek_abstracts")
    assert private.read_rows(tmp_path, ds) is None
    private.write_rows(tmp_path, ds, "year=2025", [a_row(DOI_A)], complete=True)
    got = private.read_rows(tmp_path, ds)
    assert got is not None and got.origin == "local" and got.complete and got.revision is None
    v1 = got.version
    private.write_rows(tmp_path, ds, "year=2024", [a_row(DOI_B, year=2024)])
    got2 = private.read_rows(tmp_path, ds)
    assert got2 is not None and not got2.complete and got2.version != v1 and len(got2.rows) == 2
    (private.area(tmp_path, ds.id) / "year=2024.manifest.json").unlink()
    got3 = private.read_rows(tmp_path, ds)
    assert got3 is not None and not got3.complete
    assert {m["partition"] for m in got3.manifests} == {"year=2024", "year=2025"}
    assert not private.PrivateRows([], "local").complete


def test_the_hub_read_checks_privacy_pins_and_swaps_the_cache(
    reg: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = reg.get("idweek_abstracts")
    monkeypatch.setenv(naming.OWNER_ENV, "org")
    monkeypatch.setenv(private.HF_TOKEN_ENV, "secret")
    private._hub_read_at.clear()
    files = {
        "year=2025.jsonl": json.dumps(a_row(DOI_A)) + "\n",
        "year=2024.jsonl": json.dumps(a_row(DOI_B)) + "\n",
    }
    asked: list[tuple[str, str]] = []

    def download(repo: str, token: str, local: Path) -> str:
        asked.append((repo, token))
        for name, body in files.items():
            (local / name).write_text(body)
        return "abc123"

    got = private.read_rows(tmp_path, ds, downloader=download)
    assert got is not None and got.origin == "hub" and got.revision == "abc123" and len(got.rows) == 2
    assert asked == [("org/health-data-registry-private-idweek-abstracts", "secret")]
    cache = private.hub_cache(tmp_path, ds.id)
    assert cache.stat().st_mode & 0o777 == 0o700 and cache.parent.stat().st_mode & 0o777 == 0o700
    assert (cache / "year=2025.jsonl").stat().st_mode & 0o777 == 0o600
    # Within the TTL the cache is read again without downloading.
    assert private.read_rows(tmp_path, ds, downloader=download) is not None and len(asked) == 1
    # After it, a file removed upstream is gone here too.
    private._hub_read_at.clear()
    del files["year=2024.jsonl"]
    got2 = private.read_rows(tmp_path, ds, downloader=download)
    assert got2 is not None and [r["doi"] for r in got2.rows] == [DOI_A]
    assert not (cache / "year=2024.jsonl").exists()
    # A failing download keeps nothing half-written and falls back to live links.
    private._hub_read_at.clear()

    def broken(repo: str, token: str, local: Path) -> str:
        (local / "half.jsonl").write_text("{")
        raise OSError("gone")

    assert private.read_rows(tmp_path, ds, downloader=broken) is None
    assert not cache.with_name(f".{cache.name}.new").exists()
    # A hub dataset that isn't private is refused, never read.
    private._hub_read_at.clear()

    def public(repo: str, token: str, local: Path) -> str:
        raise private.PrivateRefused("hub_not_private", "public")

    with pytest.raises(private.PrivateRefused):
        private.read_rows(tmp_path, ds, downloader=public)
    monkeypatch.delenv(private.HF_TOKEN_ENV)
    assert private.read_rows(tmp_path / "other", ds, downloader=download) is None
    private._hub_read_at.clear()


def test_require_private_repo() -> None:
    def api(**info: Any) -> Any:
        return SimpleNamespace(repo_info=lambda repo, repo_type: SimpleNamespace(**info))

    assert private.require_private_repo("o/r", api(private=True, sha="abc")) == "abc"
    assert private.require_private_repo("o/r", api(private=True, sha=None)) is None
    for bad in (api(private=False, sha="abc"), api(sha="abc"), api(private="yes")):
        with pytest.raises(private.PrivateRefused) as e:
            private.require_private_repo("o/r", bad)
        assert e.value.code == "hub_not_private" and "o/r is not a private dataset" in str(e.value)


# ------------------------------------------------------------------------------------------------ harvest


def test_an_enricher_answering_in_another_shape_is_noted_not_fatal(reg: Registry, tmp_path: Path) -> None:
    class Odd(FakeApis):
        def __call__(self, url: str) -> RawFetch:
            if urlsplit(url).hostname == doi.OPENALEX_HOST:
                self.urls.append(url)
                return raw(url, b'{"error": "maintenance"}')
            return super().__call__(url)

    s = doi_harvest.harvest(reg, tmp_path, "idweek", year=2025, get=Odd(two_pages()))
    assert s.rows == 3 and s.skipped[0].startswith("OpenAlex could not be read (OpenAlex answered in a shape")


def test_a_degraded_rerun_keeps_what_the_missing_api_gave_before(reg: Registry, tmp_path: Path) -> None:
    ds = reg.get("idweek_abstracts")
    page = [crossref_body([cr_item(1, abstract=None, license=None)], cursor=None)]
    doi_harvest.harvest(reg, tmp_path, "idweek", year=2025, get=FakeApis(list(page)))
    (before,) = private.read_rows(tmp_path, ds).rows  # type: ignore[union-attr]
    assert before["pubmed_pmid"] == "41000001" and before["abstract_text"] == "Cohort in Kenya"
    assert before["europepmc_source_url"] is not None
    # Europe PMC is down and OpenAlex is skipped: the stored row keeps their values, text and provenance.
    s = doi_harvest.harvest(reg, tmp_path, "idweek", year=2025, enrich=False, get=FakeApis(list(page)))
    assert s.complete
    (after,) = private.read_rows(tmp_path, ds).rows  # type: ignore[union-attr]
    for col in (
        "pubmed_pmid", "pmcid", "abstract_text", "abstract_tier", "abstract_licence", "openalex_source_url",
        "openalex_raw_sha256", "europepmc_source_url", "europepmc_raw_sha256", "condition_tags", "geography_tags",
    ):  # fmt: skip
        assert after[col] == before[col], col
    assert after["metadata_sources"] == "crossref; openalex; europepmc"
    assert after["metadata_terms"] == before["metadata_terms"]
    assert after["geography_tags"] == "kenya"


def test_keep_stored_rules(reg: Registry) -> None:
    sup = reg.get("idweek_abstracts").entry.source.supplements[-1]
    new = a_row(DOI_A, title="Antiseptic bathing", abstract_text=None, pubmed_pmid="9")
    old = a_row(
        DOI_A, abstract_text="kenya sepsis", abstract_tier="public", abstract_licence="cc-by", pubmed_pmid="1",
        pmcid="PMC1", metadata_sources="crossref; europepmc", europepmc_source_url="https://www.ebi.ac.uk/x",
        europepmc_raw_sha256="1" * 64, europepmc_retrieved_at="t",
    )  # fmt: skip
    assert doi_harvest.keep_stored(new, None, sup=sup, missed={"europepmc"}) is new
    assert doi_harvest.keep_stored(new, old, sup=sup, missed=set()) is new
    out = doi_harvest.keep_stored(new, old, sup=sup, missed={"europepmc"})
    assert out["pubmed_pmid"] == "9" and out["pmcid"] == "PMC1"  # a value the new row has wins
    assert (out["abstract_text"], out["abstract_tier"], out["abstract_licence"]) == (
        "kenya sepsis",
        "public",
        "cc-by",
    )
    assert out["condition_tags"] == "sepsis" and out["geography_tags"] == "kenya"  # tags from the kept text
    assert (
        out["europepmc_source_url"] == "https://www.ebi.ac.uk/x"
        and out["metadata_sources"] == "crossref; europepmc"
    )
    # OpenAlex missed but it gave the old row nothing: not listed as a source.
    only_oa = doi_harvest.keep_stored(new, old, sup=sup, missed={"openalex"})
    assert only_oa["metadata_sources"] == "crossref" and only_oa.get("openalex_source_url") is None
    # A link-only year, or a stored row whose text was link only, never brings text back.
    link_only = sup.model_copy(update={"text_policy": "link_only"})
    assert doi_harvest.keep_stored(new, old, sup=link_only, missed={"europepmc"})["abstract_text"] is None
    old_lo = {**old, "abstract_tier": "link_only"}
    assert doi_harvest.keep_stored(new, old_lo, sup=sup, missed={"europepmc"})["abstract_text"] is None


def test_a_complete_run_replaces_the_year_and_a_sample_only_upserts(reg: Registry, tmp_path: Path) -> None:
    ds = reg.get("idweek_abstracts")
    doi_harvest.harvest(reg, tmp_path, "idweek", year=2025, enrich=False, get=FakeApis(two_pages()))
    assert {r["doi"] for r in private.read_rows(tmp_path, ds).rows} == {  # type: ignore[union-attr]
        "10.1093/ofid/ofaf695.001", "10.1093/ofid/ofaf695.003", "10.1093/ofid/ofaf695.004",
    }  # fmt: skip
    # A sample run upserts and the year stays marked complete.
    doi_harvest.harvest(
        reg, tmp_path, "idweek", year=2025, max_records=1, enrich=False, get=FakeApis(two_pages())
    )
    got = private.read_rows(tmp_path, ds)
    assert got is not None and len(got.rows) == 3 and got.complete
    # DOI 004 left the supplement (now excluded): a full run removes it.
    sup = ds.entry.source.supplements[-1]
    ds.entry.source.supplements[-1] = sup.model_copy(
        update={"exclude_dois": [*sup.exclude_dois, "10.1093/ofid/ofaf695.004"]}
    )
    try:
        doi_harvest.harvest(reg, tmp_path, "idweek", year=2025, enrich=False, get=FakeApis(two_pages()))
    finally:
        ds.entry.source.supplements[-1] = sup
    assert "10.1093/ofid/ofaf695.004" not in {r["doi"] for r in private.read_rows(tmp_path, ds).rows}  # type: ignore[union-attr]


def test_the_harvest_caps_its_crawl_delay_wait() -> None:
    assert doi_harvest.CRAWL_DELAY_BUDGET_S == 3600.0


def test_sepsis_and_influenza_need_whole_words() -> None:
    def cond(title: str) -> str:
        return research_tags.tag_record(title, None)["condition_tags"]

    assert cond("Chlorhexidine antiseptic bathing to reduce CLABSI") == "clabsi"
    assert cond("Aseptic technique and antisepsis audits") == ""
    assert cond("Septic shock in adults") == cond("Neonatal sepsis") == "sepsis"
    assert cond("Parainfluenza in children") == "" and cond("Influenza vaccine uptake") == "influenza"


# ------------------------------------------------------------------------------------------------ read path


def test_the_read_path_serves_private_sources_from_their_store(reg: Registry, tmp_path: Path) -> None:
    ds = reg.get("idweek_abstracts")
    lake = tmp_path / "lake"
    eng = Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)
    with pytest.raises(QueryRefused) as e:
        eng.manifest(ds.id)
    assert e.value.code == "private_store_unavailable" and "https://academic.oup.com/ofid" in str(e.value)
    assert eng.provenance(ds.id)["data_from"].startswith("not loaded yet: the source's private store")
    private.write_rows(
        lake, ds, "year=2025",
        [a_row(DOI_A, openalex_source_url="https://api.openalex.org/w", openalex_raw_sha256="2" * 64)],
    )  # fmt: skip
    eng = Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)
    m = eng.manifest(ds.id)
    assert (
        m.origin == "private" and m.partial and m.snapshot_id.startswith("private-") and not m.redistribution
    )
    assert m.source_urls == ["https://api.crossref.org/x", "https://api.openalex.org/w"]
    assert m.raw_sha256 == ["0" * 64, "2" * 64] and m.row_count == 1
    assert m.notes[0].startswith("These rows come from a sample harvest")
    # The contract's data checks ran on the store's rows, and their record is kept, as for a live table.
    assert m.quality["tool"] == "contract-rules" and m.quality["results"]
    out = eng.query_sql(f"SELECT doi, year FROM {ds.id}")
    assert out["rows"] == [[DOI_A, "2025"]] or out["rows"] == [[DOI_A, 2025]]
    assert any("Licence:" in c for c in out["caveats"])
    assert eng.provenance(ds.id)["data_from"].startswith("the source's private store, version private-")


def test_a_private_table_whose_checks_did_not_all_run_is_refused(
    reg: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #222, the private tier: a check that could not run leaves the table unanswered, in words that don't
    blame a publisher, and the next question reads the store again."""
    from health_data_registry import staging

    ds = reg.get("idweek_abstracts")
    lake = tmp_path / "lake"
    private.write_rows(lake, ds, "year=2025", [a_row(DOI_A)])
    real = staging.run_rules
    runs: list[int] = []

    def flaky(con: Any, table: str, rules: Any) -> Any:
        runs.append(1)
        report = real(con, table, rules)
        if len(runs) > 1:
            return report
        first = report.results[0]
        return type(report)(
            report.results[1:], ({"test": first["test"], "error": "boom"},), report.expected_checks
        )

    monkeypatch.setattr(staging, "run_rules", flaky)
    eng = Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)
    with pytest.raises(QueryRefused) as e:
        eng.manifest(ds.id)
    assert e.value.code == "quality_not_run"
    assert str(e.value) == (
        f"The table for {ds.id} was read, but not all of its data checks could run, so no numbers are shown for "
        "it. Other sources are not affected."
    )
    assert ds.id not in eng.manifests and ds.id not in eng._private
    assert eng.manifest(ds.id).origin == "private" and ds.id not in eng._unchecked


def test_the_read_path_refuses_an_unsafe_store_in_plain_words(
    reg: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = reg.get("idweek_abstracts")

    def refuse(lake: Path, d: Any, **kw: Any) -> Any:
        raise private.PrivateRefused("hub_not_private", "o/r is not a private dataset")

    monkeypatch.setattr(private, "read_rows", refuse)
    eng = Engine(registry=reg, lake=tmp_path, hub=None, fetch_at_run_time=False)
    with pytest.raises(QueryRefused) as e:
        eng.manifest(ds.id)
    assert e.value.code == "private_store_refused" and "o/r" not in str(e.value)
    assert "tell the maintainer" in str(e.value)


def test_rows_must_match_the_contract(reg: Registry, tmp_path: Path) -> None:
    ds = reg.get("idweek_abstracts")
    with pytest.raises(private.PrivateRefused) as e:
        private.write_rows(tmp_path, ds, "year=2025", [a_row(DOI_A, poster_pdf="x")])
    assert e.value.code == "undeclared_field" and "poster_pdf is not a column" in str(e.value)
    with pytest.raises(private.PrivateRefused) as e2:
        private.write_rows(tmp_path, ds, "year=2025", [a_row(DOI_A, title="")])
    assert e2.value.code == "missing_required" and "a row has no title" in str(e2.value)
    assert not (private.area(tmp_path, ds.id) / "year=2025.jsonl").exists()


def test_notes_carry_the_terms(reg: Registry) -> None:
    ds = reg.get("idweek_abstracts")
    lines = private.notes(ds)
    assert lines[0].startswith("Abstracts published in Open Forum Infectious Diseases")
    assert (
        lines[1].startswith(f"Licence: {ds.entry.licence.id} (") and "each abstract under its own" in lines[1]
    )
    assert any(x.startswith("Suppression: ") for x in lines) and lines[-1].startswith("How to cite: ")


def test_purge_deletes_every_private_copy(
    reg: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = reg.get("idweek_abstracts")
    monkeypatch.delenv(naming.OWNER_ENV, raising=False)
    assert private.purge(tmp_path, ds.id) == [f"{ds.id} had no private copy on this computer"]
    private.write_rows(tmp_path, ds, "year=2025", [a_row(DOI_A)])
    private.hub_cache(tmp_path, ds.id).mkdir(parents=True)
    deleted: list[tuple[str, str]] = []
    api = SimpleNamespace(delete_repo=lambda repo, repo_type, missing_ok: deleted.append((repo, repo_type)))
    done = private.purge(tmp_path, ds.id, api=api, owner="org")
    assert done[0].startswith("deleted the local private area") and done[1].startswith(
        "deleted the hub cache"
    )
    assert done[2] == "deleted the private hub dataset org/health-data-registry-private-idweek-abstracts"
    assert deleted == [("org/health-data-registry-private-idweek-abstracts", "dataset")]
    assert not private.area(tmp_path, ds.id).exists()
    assert private.purge(tmp_path, ds.id, owner="org") == [
        "the private hub dataset org/health-data-registry-private-idweek-abstracts was not deleted: set HF_TOKEN "
        "to a write token and rerun"
    ]
