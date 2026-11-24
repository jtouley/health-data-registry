"""scripts/crawl_doi.py: the step the crawl-doi.yml workflow runs. The harvest itself is faked."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import doi_harvest
from health_data_registry.fetch import FetchRefused

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "crawl_doi.py"


def load() -> Any:
    spec = importlib.util.spec_from_file_location("crawl_doi", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_writes_the_workflow_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = load()
    seen: dict[str, Any] = {}

    def fake(registry: Any, lake: Path, conference: str, **kw: Any) -> doi_harvest.HarvestSummary:
        seen.update(kw, conference=conference, lake=lake)
        return doi_harvest.HarvestSummary(source="icid_abstracts", year=2022, listed=4, rows=4, complete=True)

    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("HDR_LAKE", str(tmp_path / "lake"))
    monkeypatch.setenv("HDR_PRIVATE_HF_OWNER", "example-org")
    monkeypatch.setattr(doi_harvest, "harvest", fake)
    assert mod.main(["icid", "--year", "2022", "--max-records", "4", "--no-enrich"]) == 0
    assert seen == {
        "conference": "icid", "lake": tmp_path / "lake", "year": 2022, "max_records": 4, "enrich": False,
    }  # fmt: skip
    assert out.read_text().splitlines() == [
        "source=icid_abstracts",
        f"folder={tmp_path / 'lake' / 'private' / 'icid_abstracts'}",
        "private_repo=example-org/health-data-registry-private-icid-abstracts",
        "complete=true",
    ]
    assert "icid_abstracts 2022: 4 DOIs listed" in capsys.readouterr().out
    monkeypatch.delenv("HDR_PRIVATE_HF_OWNER")
    monkeypatch.setattr(
        doi_harvest, "harvest", lambda *a, **k: doi_harvest.HarvestSummary(source="icid_abstracts", year=2022)
    )
    assert mod.main(["icid"]) == 0
    tail = out.read_text().splitlines()[-2:]
    assert tail == ["private_repo=jtouley/health-data-registry-private-icid-abstracts", "complete=false"]
    monkeypatch.delenv("GITHUB_OUTPUT")
    assert mod.main(["icid"]) == 0


def test_stops_in_plain_words(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    mod = load()
    assert mod.main(["croi"]) == 1
    assert "::error::harvest stopped: 'croi' is not a DOI-harvested conference" in capsys.readouterr().err

    def busy(*a: Any, **k: Any) -> Any:
        raise FetchRefused("rate_limited", "Run it again later.")

    monkeypatch.setattr(doi_harvest, "harvest", busy)
    assert mod.main(["apic"]) == 1
    assert "::error::harvest stopped: Run it again later." in capsys.readouterr().err
