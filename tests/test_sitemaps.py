"""Sitemap snapshots: parse, follow indexes, diff week to week, upload to the hub. Synthetic sitemaps only."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from huggingface_hub.errors import EntryNotFoundError

from health_data_registry import sitemaps as sm

WHEN = datetime(2026, 11, 24, tzinfo=UTC)
SEED = sm.Seed("demo", "https://www.example.gov/demo/sitemap.xml")


def urlset(*pages: tuple[str, str | None]) -> bytes:
    rows = "".join(
        f"<url><loc>{loc}</loc>{f'<lastmod>{mod}</lastmod>' if mod else ''}</url>" for loc, mod in pages
    )
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>'.encode()


def index(*children: str) -> bytes:
    rows = "".join(f"<sitemap><loc>{c}</loc></sitemap>" for c in children)
    return f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</sitemapindex>'.encode()


class FakeApi:
    """The slice of huggingface_hub.HfApi this module uses, kept in memory."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.commits: list[str] = []
        self.created: list[tuple[str, bool]] = []
        self.tmp: Path | None = None

    def create_repo(self, repo_id: str, *, repo_type: str, private: bool, exist_ok: bool) -> None:
        self.created.append((repo_id, private))

    def hf_hub_download(self, repo_id: str, filename: str, *, repo_type: str) -> str:
        if filename not in self.files:
            raise EntryNotFoundError(filename)
        assert self.tmp is not None
        out = self.tmp / filename.replace("/", "_")
        out.write_bytes(self.files[filename])
        return str(out)

    def create_commit(
        self, *, repo_id: str, repo_type: str, operations: list[Any], commit_message: str
    ) -> None:
        for op in operations:
            self.files[op.path_in_repo] = op.path_or_fileobj
        self.commits.append(commit_message)


@pytest.fixture
def api(tmp_path: Path) -> FakeApi:
    a = FakeApi()
    a.tmp = tmp_path
    return a


@pytest.mark.parametrize("gzipped", [False, True], ids=["plain", "gzip"])
def test_parse_urlset_pagesAndLastmod_readInOrder(gzipped: bool) -> None:
    # Arrange
    body = urlset(("https://www.example.gov/a", "2026-11-16"), ("https://www.example.gov/b", None))
    content = gzip.compress(body) if gzipped else body

    # Act
    pages, children = sm.parse(content)

    # Assert
    assert pages == [
        sm.Page("https://www.example.gov/a", "2026-11-16"),
        sm.Page("https://www.example.gov/b", None),
    ]
    assert children == []


@pytest.mark.parametrize(
    "content", [b"not xml", b"<html><body>Access Denied</body></html>"], ids=["garbage", "error-page"]
)
def test_parse_notASitemap_raises(content: bytes) -> None:
    with pytest.raises(sm.SitemapError):
        sm.parse(content)


def test_take_sitemapIndex_followsChildrenOnSameHost() -> None:
    # Arrange
    a, b = "https://www.example.gov/demo/a.xml", "https://www.example.gov/demo/b.xml"
    served = {
        SEED.url: index(a, b),
        a: urlset(("https://www.example.gov/1", "2026-01-01")),
        b: urlset(("https://www.example.gov/2", "2026-02-01")),
    }

    # Act
    snap = sm.take(SEED, lambda url: (served[url], WHEN), today=date(2026, 11, 24))

    # Assert
    assert [p.loc for p in snap.pages] == ["https://www.example.gov/1", "https://www.example.gov/2"]
    assert snap.source == "publisher"


@pytest.mark.parametrize(
    ("child", "message"),
    [
        ("https://evil.example.com/x.xml", "another host"),
        ("https://www.example.gov/demo/nested.xml", "nested"),
    ],
    ids=["other-host", "nested-index"],
)
def test_take_unsafeIndex_refused(child: str, message: str) -> None:
    served = {SEED.url: index(child), child: index("https://www.example.gov/deeper.xml")}
    with pytest.raises(sm.SitemapError, match=message):
        sm.take(SEED, lambda url: (served[url], WHEN), today=date(2026, 11, 24))


def test_take_emptySitemap_refused() -> None:
    with pytest.raises(sm.SitemapError, match="no pages"):
        sm.take(SEED, lambda url: (urlset(), WHEN), today=date(2026, 11, 24))


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        (None, [("a", "1")], {"new": ["a"], "changed": [], "removed": []}),
        (
            [("a", "1"), ("b", "1")],
            [("a", "2"), ("c", "1")],
            {"new": ["c"], "changed": ["a"], "removed": ["b"]},
        ),
        ([("a", None)], [("a", None)], {"new": [], "changed": [], "removed": []}),
        ([("a", None)], [("a", "1")], {"new": [], "changed": ["a"], "removed": []}),
    ],
    ids=["first-run", "mixed", "unchanged-no-lastmod", "lastmod-appears"],
)
def test_diff_weekToWeek_newChangedRemoved(
    previous: Any, current: Any, expected: dict[str, list[str]]
) -> None:
    prev = None if previous is None else [sm.Page(*p) for p in previous]
    assert sm.diff(prev, [sm.Page(*p) for p in current]) == expected


def test_upload_twoWeeks_privateRepoDatedCopiesAndDiff(api: FakeApi) -> None:
    # Arrange
    week1 = sm.Snapshot(SEED, date(2026, 11, 17), urlset(("https://www.example.gov/a", "1")),
                        [sm.Page("https://www.example.gov/a", "1")], WHEN, "maintainer_file")  # fmt: skip
    week2 = sm.Snapshot(SEED, date(2026, 11, 24), urlset(("https://www.example.gov/a", "2")),
                        [sm.Page("https://www.example.gov/a", "2"), sm.Page("https://www.example.gov/b", None)],
                        WHEN, "publisher")  # fmt: skip

    # Act
    sm.upload(api, "me/ops-sitemaps", [week1])
    report = sm.upload(api, "me/ops-sitemaps", [week2])

    # Assert
    prefix = "sitemaps/www.example.gov/demo"
    assert api.created == [("me/ops-sitemaps", True)] * 2
    assert {f"{prefix}/2026-11-17.xml.gz", f"{prefix}/2026-11-24.xml.gz", f"{prefix}/latest.json"} <= set(
        api.files
    )
    assert gzip.decompress(api.files[f"{prefix}/2026-11-24.xml.gz"]) == week2.raw
    change = json.loads(api.files[f"{prefix}/2026-11-24.diff.json"])
    assert (change["new"], change["changed"], change["removed"]) == (
        ["https://www.example.gov/b"],
        ["https://www.example.gov/a"],
        [],
    )
    latest = json.loads(api.files[f"{prefix}/latest.json"])
    assert (latest["taken_on"], latest["source"], len(latest["pages"])) == ("2026-11-24", "publisher", 2)
    assert report == [
        {"seed": "demo", "taken_on": "2026-11-24", "pages": 2, "new": 1, "changed": 1, "removed": 0}
    ]


def test_main_fromFile_usesMaintainerCopyWithoutFetching(
    api: FakeApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setattr(sm, "SEEDS", (SEED,))
    f = tmp_path / "demo.xml"
    f.write_bytes(urlset(("https://www.example.gov/a", "1")))

    def no_fetch(url: str) -> tuple[bytes, datetime]:
        raise AssertionError("must not fetch when a file is given")

    # Act
    code = sm.main(["--repo", "me/x", "--from-file", f"demo={f}"], api=api, get=no_fetch)

    # Assert
    assert code == 0
    assert json.loads(api.files["sitemaps/www.example.gov/demo/latest.json"])["source"] == "maintainer_file"


def test_main_publisherRefuses_reportsAndExitsNonZero(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    from health_data_registry.fetch import FetchRefused

    monkeypatch.setattr(sm, "SEEDS", (SEED,))

    def refused(url: str) -> tuple[bytes, datetime]:
        raise FetchRefused(
            "robots_disallowed", "robots.txt at www.example.gov asks automated tools not to read this"
        )

    # Act
    code = sm.main(["--repo", "me/x"], api=api, get=refused)

    # Assert
    assert code == 1
    assert api.commits == []
    assert "demo: not taken: robots.txt" in capsys.readouterr().err


def test_main_unknownSeed_isAUsageError(api: FakeApi) -> None:
    with pytest.raises(SystemExit):
        sm.main(["--seed", "nope"], api=api)
