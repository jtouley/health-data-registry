"""Sitemap snapshots: parse, follow indexes, diff week to week, upload to the hub. Synthetic sitemaps only."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from huggingface_hub.errors import EntryNotFoundError

from health_data_registry import cli
from health_data_registry import sitemaps as sm
from health_data_registry.fetch import FetchRefused

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
        self.public = False

    def create_repo(self, repo_id: str, *, repo_type: str, private: bool, exist_ok: bool) -> None:
        assert exist_ok and repo_type == "dataset"
        self.created.append((repo_id, private))

    def repo_info(self, repo_id: str, *, repo_type: str) -> Any:
        return SimpleNamespace(private=not self.public)

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
    code = sm.main(
        ["--repo", "me/health-data-registry-ops-sitemaps", "--from-file", f"demo={f}"], api=api, get=no_fetch
    )

    # Assert
    assert code == 0
    assert json.loads(api.files["sitemaps/www.example.gov/demo/latest.json"])["source"] == "maintainer_file"


def test_main_publisherRefuses_reportsAndExitsNonZero(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    monkeypatch.setattr(sm, "SEEDS", (SEED,))

    def refused(url: str) -> tuple[bytes, datetime]:
        raise FetchRefused(
            "robots_disallowed", "robots.txt at www.example.gov asks automated tools not to read this"
        )

    # Act
    code = sm.main(["--repo", "me/health-data-registry-ops-sitemaps"], api=api, get=refused)

    # Assert
    assert code == 1
    assert api.commits == []
    assert "demo: not taken: robots.txt" in capsys.readouterr().err


def test_main_unknownSeed_isAUsageError(api: FakeApi) -> None:
    with pytest.raises(SystemExit):
        sm.main(["--seed", "nope"], api=api)


def test_seeds_michiganAndKentCounty_namedByNamingPy() -> None:
    assert {s.name: s.url for s in sm.SEEDS} == {
        "mdhhs": "https://www.michigan.gov/mdhhs/sitemap.xml",
        "emergingdiseases": "https://www.michigan.gov/emergingdiseases/sitemap.xml",
        "kentcounty": "https://www.kentcountymi.gov/sitemap.xml",
    }
    assert sm.DEFAULT_REPO == "jtouley/health-data-registry-ops-sitemaps"


def test_upload_publicRepo_refusedBeforeAnyCommit(api: FakeApi) -> None:
    api.public = True
    snap = sm.Snapshot(SEED, date(2026, 11, 24), urlset(("https://www.example.gov/a", "1")),
                       [sm.Page("https://www.example.gov/a", "1")], WHEN, "publisher")  # fmt: skip
    with pytest.raises(sm.SitemapError, match="is public"):
        sm.upload(api, "me/ops-sitemaps", [snap])
    assert api.commits == []


class FakeBrowser:
    """The slice of browser_fetch this module uses: answers by URL, or raises what the URL maps to."""

    def __init__(self, served: dict[str, bytes | FetchRefused]) -> None:
        self.served = served
        self.calls: list[tuple[str, frozenset[str]]] = []

    def browser_fetch(self, url: str, *, allowed_hosts: frozenset[str], max_bytes: int) -> Any:
        self.calls.append((url, allowed_hosts))
        assert max_bytes == sm.MAX_SITEMAP_BYTES
        answer = self.served[url]
        if isinstance(answer, FetchRefused):
            raise answer
        return SimpleNamespace(page=SimpleNamespace(content=answer, retrieved_at=WHEN))


ROBOTS = "https://www.example.gov/robots.txt"


def test_browserGetter_readsRobotsFirstThenSitemap(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.setattr(sm, "SEEDS", (SEED,))
    fake = FakeBrowser(
        {
            ROBOTS: b"User-agent: *\nDisallow: /private/\n",
            SEED.url: urlset(("https://www.example.gov/a", None)),
        }
    )

    # Act
    snap = sm.take(SEED, sm.browser_getter(module=fake), today=date(2026, 11, 24))

    # Assert
    assert [c[0] for c in fake.calls] == [ROBOTS, SEED.url]
    assert fake.calls[0][1] == frozenset({"www.example.gov"})
    assert [p.loc for p in snap.pages] == ["https://www.example.gov/a"]
    assert snap.retrieved_at == WHEN


def test_browserGetter_robotsDisallows_refusedWithoutFetching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "SEEDS", (SEED,))
    fake = FakeBrowser({ROBOTS: b"User-agent: *\nDisallow: /demo/\n"})
    with pytest.raises(FetchRefused) as e:
        sm.browser_getter(module=fake)(SEED.url)
    assert e.value.code == "robots_disallowed"
    assert [c[0] for c in fake.calls] == [ROBOTS]


@pytest.mark.parametrize("status", [404, 410])
def test_browserGetter_noRobotsFile_allowsEverything(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    monkeypatch.setattr(sm, "SEEDS", (SEED,))
    fake = FakeBrowser(
        {
            ROBOTS: FetchRefused("http_error", "gone", status=status),
            SEED.url: urlset(("https://www.example.gov/a", None)),
        }
    )
    body, _ = sm.browser_getter(module=fake)(SEED.url)
    assert sm.parse(body)[0] == [sm.Page("https://www.example.gov/a", None)]


@pytest.mark.parametrize(
    "refusal",
    [
        FetchRefused("blocked", "www.example.gov refused this program's request", status=403),
        FetchRefused("http_error", "server error", status=503),
        FetchRefused("http_error", "no status"),
    ],
    ids=["blocked", "server-error", "no-status"],
)
def test_browserGetter_robotsRefusedOrBroken_isFinal(
    monkeypatch: pytest.MonkeyPatch, refusal: FetchRefused
) -> None:
    monkeypatch.setattr(sm, "SEEDS", (SEED,))
    fake = FakeBrowser({ROBOTS: refusal})
    with pytest.raises(FetchRefused) as e:
        sm.browser_getter(module=fake)(SEED.url)
    assert e.value is refusal
    assert [c[0] for c in fake.calls] == [ROBOTS]


def test_browserRobots_readsEachHostOnce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "SEEDS", (SEED,))
    fake = FakeBrowser({ROBOTS: b"", SEED.url: urlset(("https://www.example.gov/a", None))})
    get = sm.browser_getter(module=fake)
    get(SEED.url)
    get(SEED.url)
    assert [c[0] for c in fake.calls] == [ROBOTS, SEED.url, SEED.url]


def test_browserGetter_moduleMissing_saysSoPlainly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "BROWSER_MODULE", "health_data_registry.no_such_module")
    with pytest.raises(sm.BrowserUnavailable, match="--fetcher http"):
        sm.browser_getter()


def test_main_browserMissing_exits2WithoutUpload(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sm, "SEEDS", (SEED,))
    monkeypatch.setattr(sm, "BROWSER_MODULE", "health_data_registry.no_such_module")
    assert sm.main(["--repo", "me/health-data-registry-ops-sitemaps"], api=api) == 2
    assert "--fetcher http" in capsys.readouterr().err
    assert api.created == []


@pytest.mark.parametrize(("fetcher", "chosen"), [("browser", "browser"), ("http", "http")])
def test_main_fetcherFlag_picksTheGetter(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch, fetcher: str, chosen: str
) -> None:
    # Arrange
    monkeypatch.setattr(sm, "SEEDS", (SEED,))
    used: list[str] = []

    def make(kind: str) -> Any:
        def factory() -> sm.Getter:
            used.append(kind)
            return lambda url: (urlset(("https://www.example.gov/a", "1")), WHEN)

        return factory

    monkeypatch.setattr(sm, "browser_getter", make("browser"))
    monkeypatch.setattr(sm, "publisher_getter", make("http"))

    # Act
    code = sm.main(["--repo", "me/health-data-registry-ops-sitemaps", "--fetcher", fetcher], api=api)

    # Assert
    assert code == 0 and used == [chosen]
    assert api.created == [("me/health-data-registry-ops-sitemaps", True)]


def test_main_partialRefusal_uploadsTheRestAndExits1(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = sm.Seed("other", "https://www.example.gov/other/sitemap.xml")
    monkeypatch.setattr(sm, "SEEDS", (SEED, other))

    def get(url: str) -> tuple[bytes, datetime]:
        if url == other.url:
            raise FetchRefused("blocked", "www.example.gov refused this program's request")
        return urlset(("https://www.example.gov/a", "1")), WHEN

    assert sm.main(["--repo", "me/health-data-registry-ops-sitemaps"], api=api, get=get) == 1
    assert len(api.commits) == 1 and "demo 20" in api.commits[0]
    captured = capsys.readouterr()
    assert "other: not taken: www.example.gov refused" in captured.err
    assert captured.out.startswith("demo ")


def test_cli_sitemapsSnapshot_runsWithoutTheRegistry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Arrange: a broken registry path proves the subcommand never loads it.
    monkeypatch.setenv("HDR_REGISTRY", str(tmp_path / "missing"))
    seen: dict[str, Any] = {}

    def fake_snapshot(args: Any, *, error: Any) -> int:
        seen.update(vars(args))
        return 7

    monkeypatch.setattr(sm, "snapshot", fake_snapshot)

    # Act
    code = cli.main(
        [
            "sitemaps",
            "snapshot",
            "--repo",
            "me/health-data-registry-ops-sitemaps",
            "--fetcher",
            "http",
            "--seed",
            "mdhhs",
        ]
    )

    # Assert
    assert code == 7
    assert (seen["repo"], seen["fetcher"], seen["seed"], seen["from_file"]) == (
        "me/health-data-registry-ops-sitemaps",
        "http",
        ["mdhhs"],
        [],
    )


def test_addArguments_defaultsAndBadFetcher() -> None:
    p = snapshot_args([])
    assert (p["repo"], p["fetcher"], p["seed"]) == (sm.DEFAULT_REPO, "browser", None)
    with pytest.raises(SystemExit):
        cli.main(["sitemaps", "snapshot", "--fetcher", "curl"])
    with pytest.raises(SystemExit):
        cli.main(["sitemaps"])


def snapshot_args(argv: list[str]) -> dict[str, Any]:
    import argparse

    p = argparse.ArgumentParser()
    sm.add_arguments(p)
    return vars(p.parse_args(argv))


@pytest.mark.parametrize(
    "repo",
    ["me/x", "me/health-data-registry", "me/health-data-registry-ops-croi-crawl", "noslash", "bad owner/x"],
)
def test_main_repoOutsideTheConvention_isAUsageError(api: FakeApi, repo: str) -> None:
    with pytest.raises(SystemExit):
        sm.main(["--repo", repo], api=api, get=lambda url: (urlset(("https://www.example.gov/a", "1")), WHEN))
    assert api.created == []


def test_checkedRepo_acceptsOnlyTheOpsSitemapsName() -> None:
    name = "org-2/health-data-registry-ops-sitemaps"
    assert sm.checked_repo(name) == name
    with pytest.raises(ValueError, match="use org-2/health-data-registry-ops-sitemaps"):
        sm.checked_repo(name + "-public")
    with pytest.raises(ValueError, match="not a Hugging Face user"):
        sm.checked_repo("bad owner/health-data-registry-ops-sitemaps")
