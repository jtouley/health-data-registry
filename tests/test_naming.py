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
    monkeypatch.delenv(naming.PUBLIC_OWNER_ENV, raising=False)
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
    assert (
        f"default: ${naming.PUBLIC_OWNER_ENV} for public and space, ${naming.OWNER_ENV} otherwise; "
        f"else {naming.DEFAULT_OWNER}"
    ) in text
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


def test_public_names_take_their_own_owner_variable(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """HDR_HF_OWNER names the public dataset and the Space; HDR_PRIVATE_HF_OWNER only private and ops ones."""
    monkeypatch.setenv(naming.PUBLIC_OWNER_ENV, "pub")
    monkeypatch.setenv(naming.OWNER_ENV, "priv")
    for kind in (["public"], ["space"], ["private", "croi_abstracts_text"], ["ops", "sitemaps"]):
        assert naming.main(kind) == 0
    assert capsys.readouterr().out.split() == [
        "pub/health-data-registry",
        "pub/health-data-registry-mcp",
        "priv/health-data-registry-private-croi-abstracts-text",
        "priv/health-data-registry-ops-sitemaps",
    ]
    monkeypatch.delenv(naming.PUBLIC_OWNER_ENV)
    assert naming.main(["public"]) == 0
    assert capsys.readouterr().out == "jtouley/health-data-registry\n"


class FakeHub:
    def __init__(self, private: bool | None) -> None:
        self.private = private
        self.created: list[tuple[str, str, bool, bool]] = []

    def create_repo(self, repo_id: str, *, repo_type: str, private: bool, exist_ok: bool) -> None:
        self.created.append((repo_id, repo_type, private, exist_ok))

    def repo_info(self, repo_id: str, *, repo_type: str) -> object:
        return type("Info", (), {"private": self.private})()


def test_assert_private_creates_private_and_refuses_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(naming.OWNER_ENV, raising=False)
    hub = FakeHub(private=True)
    repo = "jtouley/health-data-registry-ops-sitemaps"
    assert naming.assert_private(hub, repo) == repo
    assert hub.created == [(repo, "dataset", True, True)]
    for state in (False, None):
        with pytest.raises(naming.NotPrivate, match="is public"):
            naming.assert_private(
                FakeHub(private=state), "jtouley/health-data-registry-private-croi-abstracts-text"
            )


@pytest.mark.parametrize(
    "repo",
    [
        "jtouley/health-data-registry",  # the public dataset
        "jtouley/health-data-registry-mcp",
        "someone-else/health-data-registry-ops-raw-archive",  # owner not HDR_PRIVATE_HF_OWNER
        "jtouley/health-data-registry-ops-",
        "jtouley/health-data-registry-ops-Raw",
        "jtouley/other-ops-raw-archive",
    ],
)
def test_assert_private_refuses_any_other_owner_or_name(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(naming.OWNER_ENV, raising=False)
    hub = FakeHub(private=True)
    with pytest.raises(naming.NotPrivate, match="not a private or ops dataset of jtouley"):
        naming.assert_private(hub, repo)
    assert hub.created == []


def test_assert_private_owner_is_the_private_owner_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(naming.OWNER_ENV, "org")
    with pytest.raises(naming.NotPrivate, match="of org"):
        naming.assert_private(FakeHub(private=True), "jtouley/health-data-registry-ops-sitemaps")
    assert naming.assert_private(FakeHub(private=True), "org/health-data-registry-ops-sitemaps")


def test_assert_private_cli(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(naming.OWNER_ENV, raising=False)
    repo = "jtouley/health-data-registry-ops-raw-archive"
    assert naming.main(["assert-private", repo], api=FakeHub(private=True)) == 0
    assert capsys.readouterr().out == f"{repo} is private\n"
    assert naming.main(["assert-private", repo], api=FakeHub(private=False)) == 1
    assert "is public" in capsys.readouterr().err
    monkeypatch.setenv(naming.OWNER_ENV, "bad/owner")
    assert naming.main(["assert-private", repo], api=FakeHub(private=True)) == 1
    assert "not a Hugging Face user" in capsys.readouterr().err
