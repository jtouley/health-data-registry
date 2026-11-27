"""Hugging Face repo names follow one convention, built in one place (docs/dev/naming.md)."""

from __future__ import annotations

import pytest

from health_data_registry import naming


def test_every_name_follows_the_convention() -> None:
    assert naming.public_dataset() == "jtouley/health-data-registry"
    assert naming.space() == "jtouley/health-data-registry-mcp"
    assert (
        naming.private_dataset("croi_abstracts_index")
        == "jtouley/health-data-registry-private-croi-abstracts-index"
    )
    assert (
        naming.private_dataset("croi_abstracts_text", "org")
        == "org/health-data-registry-private-croi-abstracts-text"
    )
    assert naming.ops_dataset("croi-crawl") == "jtouley/health-data-registry-ops-croi-crawl"
    assert naming.ops_dataset("raw-archive", "org") == "org/health-data-registry-ops-raw-archive"
    assert naming.source_slug("nndss_weekly") == "nndss-weekly"


@pytest.mark.parametrize(
    "bad", ["", "Croi", "croi-index", "_croi", "croi__index", "croi_", "c", "croi index", "x" * 64]
)
def test_bad_source_ids_are_refused(bad: str) -> None:
    with pytest.raises(naming.BadName):
        naming.private_dataset(bad)


@pytest.mark.parametrize("bad", ["", "Croi-crawl", "croi_crawl", "croi--crawl", "-croi", "croi-", "1croi"])
def test_bad_job_names_are_refused(bad: str) -> None:
    with pytest.raises(naming.BadName):
        naming.ops_dataset(bad)


def test_owner_comes_from_the_argument_then_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(naming.OWNER_ENV, raising=False)
    assert naming.owner() is None
    assert naming.owner("  ") is None
    monkeypatch.setenv(naming.OWNER_ENV, " org ")
    assert naming.owner() == "org"
    assert naming.owner("other") == "other"
    assert naming.owner("") is None
    for bad in ("a/b", "-x", "x y", "x" * 97):
        with pytest.raises(naming.BadName):
            naming.owner(bad)
    with pytest.raises(naming.BadName, match="no Hugging Face owner"):
        naming.public_dataset("")


@pytest.mark.parametrize(
    ("argv", "out"),
    [
        (["public"], "jtouley/health-data-registry\n"),
        (["space"], "jtouley/health-data-registry-mcp\n"),
        (["private", "croi_abstracts_index"], "jtouley/health-data-registry-private-croi-abstracts-index\n"),
        (["--owner", "org", "ops", "croi-crawl"], "org/health-data-registry-ops-croi-crawl\n"),
    ],
)
def test_cli_prints_one_name(
    argv: list[str], out: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(naming.OWNER_ENV, raising=False)
    assert naming.main(argv) == 0
    assert capsys.readouterr().out == out


def test_cli_reads_the_owner_variable_and_refuses_bad_names(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(naming.OWNER_ENV, "org")
    assert naming.main(["ops", "raw-archive"]) == 0
    assert capsys.readouterr().out == "org/health-data-registry-ops-raw-archive\n"
    assert naming.main(["ops", "Raw_Archive"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not a job name" in captured.err


def test_cli_help_and_missing_kind(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as done:
        naming.main(["--help"])
    assert done.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "usage: python -m health_data_registry.naming" in text
    assert "Print one Hugging Face repo id." in text
    assert f"default: ${naming.OWNER_ENV}, else {naming.DEFAULT_OWNER}" in text
    with pytest.raises(SystemExit) as done:
        naming.main([])
    assert done.value.code == 2
    assert "required: kind" in capsys.readouterr().err


def test_cli_owner_flag_reaches_every_kind(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(naming.OWNER_ENV, raising=False)
    assert naming.main(["--owner", "org", "private", "croi_abstracts_text"]) == 0
    assert naming.main(["--owner", "org", "public"]) == 0
    assert naming.main(["--owner", "org", "space"]) == 0
    assert capsys.readouterr().out.split() == [
        "org/health-data-registry-private-croi-abstracts-text",
        "org/health-data-registry",
        "org/health-data-registry-mcp",
    ]


def test_refusals_name_the_bad_part() -> None:
    with pytest.raises(naming.BadName, match=r"^'a/b' is not a Hugging Face user or organisation name$"):
        naming.owner("a/b")
    with pytest.raises(naming.BadName, match=r"^no Hugging Face owner given$"):
        naming.space("")
    with pytest.raises(naming.BadName, match=r"^'Croi' is not a catalog source id \(snake_case"):
        naming.source_slug("Croi")
