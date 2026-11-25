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


REPO = "me/health-data-registry-ops-sitemaps"
HOSTS = frozenset({"www.example.gov"})


@pytest.fixture(autouse=True)
def example_owner_and_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The private owner is `me`, and the synthetic host www.example.gov is allowed like an ADR 0013 host."""
    monkeypatch.setenv("HDR_PRIVATE_HF_OWNER", "me")
    monkeypatch.setattr(sm, "ADR_0013_HOSTS", sm.ADR_0013_HOSTS | HOSTS)


class NoWait:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def browser(fake: Any) -> sm.Getter:
    return sm.browser_getter(module=fake, hosts=HOSTS, pace=sm.Pace(sleep=NoWait()))


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
    sm.upload(api, REPO, [week1])
    report = sm.upload(api, REPO, [week2])

    # Assert
    prefix = "sitemaps/www.example.gov/demo"
    assert api.created == [(REPO, True)] * 2
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
    assert set(sm.ADR_0013_HOSTS) >= {s.host for s in sm.SEEDS}


def test_defaultRepo_readsThePrivateOwnerWhenTheCommandRuns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HDR_PRIVATE_HF_OWNER")
    assert sm.default_repo() == "jtouley/health-data-registry-ops-sitemaps"
    monkeypatch.setenv("HDR_PRIVATE_HF_OWNER", "org")
    assert sm.default_repo() == "org/health-data-registry-ops-sitemaps"


def test_main_noRepo_writesToThePrivateOwnersDataset(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "SEEDS", (SEED,))
    assert sm.main([], api=api, get=lambda url: (urlset(("https://www.example.gov/a", "1")), WHEN)) == 0
    assert api.created == [(REPO, True)]


def test_upload_publicRepo_refusedBeforeAnyCommit(api: FakeApi) -> None:
    api.public = True
    snap = sm.Snapshot(SEED, date(2026, 11, 24), urlset(("https://www.example.gov/a", "1")),
                       [sm.Page("https://www.example.gov/a", "1")], WHEN, "publisher")  # fmt: skip
    with pytest.raises(sm.SitemapError, match="is public"):
        sm.upload(api, REPO, [snap])
    assert api.commits == []


def test_upload_otherOwner_refusedBeforeCreatingAnything(api: FakeApi) -> None:
    snap = sm.Snapshot(SEED, date(2026, 11, 24), urlset(("https://www.example.gov/a", "1")),
                       [sm.Page("https://www.example.gov/a", "1")], WHEN, "publisher")  # fmt: skip
    with pytest.raises(sm.SitemapError, match="not a private or ops dataset of me"):
        sm.upload(api, "someone-else/health-data-registry-ops-sitemaps", [snap])
    assert api.created == [] and api.commits == []


def test_upload_sameDayRerun_keepsTheDaysDiff(api: FakeApi) -> None:
    """A second run on one day diffs against the last earlier day, so the day's diff is never emptied."""
    page = sm.Page

    def snap(day: date, pages: list[sm.Page]) -> sm.Snapshot:
        return sm.Snapshot(SEED, day, urlset(*((p.loc, p.lastmod) for p in pages)), pages, WHEN, "publisher")

    monday, next_monday = date(2026, 11, 16), date(2026, 11, 23)
    sm.upload(api, REPO, [snap(monday, [page("https://www.example.gov/a", "1")])])
    week = [page("https://www.example.gov/a", "2"), page("https://www.example.gov/b", None)]
    sm.upload(api, REPO, [snap(next_monday, week)])
    # The re-run the same day (e.g. after --from-file) finds one more page.
    report = sm.upload(api, REPO, [snap(next_monday, [*week, page("https://www.example.gov/c", None)])])

    prefix = "sitemaps/www.example.gov/demo"
    change = json.loads(api.files[f"{prefix}/2026-11-23.diff.json"])
    assert change["since"] == "2026-11-16"
    assert change["new"] == ["https://www.example.gov/b", "https://www.example.gov/c"]
    assert change["changed"] == ["https://www.example.gov/a"]
    assert report[0]["new"] == 2
    latest = json.loads(api.files[f"{prefix}/latest.json"])
    assert (latest["taken_on"], latest["previous_taken_on"]) == ("2026-11-23", "2026-11-16")
    assert json.loads(api.files[f"{prefix}/2026-11-23.json"]) == latest


def test_upload_sameDayRerun_withoutAnEarlierDay_listsEveryPageAsNew(api: FakeApi) -> None:
    first = sm.Snapshot(
        SEED, date(2026, 11, 24), b"x", [sm.Page("https://www.example.gov/a", "1")], WHEN, "publisher"
    )
    sm.upload(api, REPO, [first])
    again = sm.upload(api, REPO, [first])
    assert again[0]["new"] == 1 and again[0]["removed"] == 0


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
    snap = sm.take(SEED, browser(fake), today=date(2026, 11, 24))

    # Assert
    assert [c[0] for c in fake.calls] == [ROBOTS, SEED.url]
    assert fake.calls[0][1] == frozenset({"www.example.gov"})
    assert [p.loc for p in snap.pages] == ["https://www.example.gov/a"]
    assert snap.retrieved_at == WHEN


def test_browserGetter_robotsDisallows_refusedWithoutFetching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "SEEDS", (SEED,))
    fake = FakeBrowser({ROBOTS: b"User-agent: *\nDisallow: /demo/\n"})
    with pytest.raises(FetchRefused) as e:
        browser(fake)(SEED.url)
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
    body, _ = browser(fake)(SEED.url)
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
        browser(fake)(SEED.url)
    assert e.value is refusal
    assert [c[0] for c in fake.calls] == [ROBOTS]


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("code", ["http_error", "blocked"])
def test_browserGetter_robotsRefusalStatus_isABlock(
    monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    """A 401, 403 or 429 for robots.txt is a block, never 'no robots.txt' (ADR 0013)."""
    fake = FakeBrowser({ROBOTS: FetchRefused(code, "refused", status=status), SEED.url: urlset(("x", None))})
    with pytest.raises(FetchRefused) as e:
        browser(fake)(SEED.url)
    assert (e.value.code, e.value.status) == ("blocked", status)
    assert [c[0] for c in fake.calls] == [ROBOTS]


@pytest.mark.parametrize("status", [400, 405, 451])
def test_browserGetter_otherRobotsClientError_isNotPermission(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    refusal = FetchRefused("http_error", "odd", status=status)
    fake = FakeBrowser({ROBOTS: refusal, SEED.url: urlset(("x", None))})
    with pytest.raises(FetchRefused) as e:
        browser(fake)(SEED.url)
    assert e.value is refusal
    assert [c[0] for c in fake.calls] == [ROBOTS]


def test_pace_keepsTenSecondsBetweenRequestsToOneHost() -> None:
    now = [100.0]
    slept = NoWait()
    pace = sm.Pace(sleep=slept, clock=lambda: now[0])
    pace.wait("https://www.example.gov/a")
    now[0] = 103.0
    pace.wait("https://www.example.gov/b")
    pace.wait("https://other.example.gov/a")
    now[0] = 200.0
    pace.wait("https://www.example.gov/c")
    assert slept.slept == [7.0]
    assert sm.PACE_S == 10.0


def test_browserGetter_pacesEveryRequest() -> None:
    fake = FakeBrowser({ROBOTS: b"", SEED.url: urlset(("https://www.example.gov/a", None))})
    slept = NoWait()
    get = sm.browser_getter(module=fake, hosts=HOSTS, pace=sm.Pace(sleep=slept, clock=lambda: 0.0))
    get(SEED.url)
    assert slept.slept == [10.0]  # robots.txt, then the sitemap 10 s later


def test_browserRobots_readsEachHostOnce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "SEEDS", (SEED,))
    fake = FakeBrowser({ROBOTS: b"", SEED.url: urlset(("https://www.example.gov/a", None))})
    get = browser(fake)
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
        def factory(*, hosts: frozenset[str]) -> sm.Getter:
            assert hosts == HOSTS
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
    assert (p["repo"], p["fetcher"], p["seed"]) == (None, "browser", None)
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


def test_parse_gzipBomb_refusedPastTheProtocolLimit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "MAX_UNCOMPRESSED_BYTES", 1000)
    body = urlset(*((f"https://www.example.gov/{i}", None) for i in range(100)))
    assert len(body) > 1000
    with pytest.raises(sm.SitemapError, match="expands past 1000 bytes"):
        sm.parse(gzip.compress(body))
    with pytest.raises(sm.SitemapError, match="larger than 1000 bytes"):
        sm.parse(body)


def test_parse_exactlyAtTheLimit_isRead(monkeypatch: pytest.MonkeyPatch) -> None:
    body = urlset(("https://www.example.gov/a", None))
    monkeypatch.setattr(sm, "MAX_UNCOMPRESSED_BYTES", len(body))
    assert len(sm.parse(gzip.compress(body))[0]) == 1
    assert len(sm.parse(body)[0]) == 1


def test_parse_protocolLimitIs50MB() -> None:
    assert sm.MAX_UNCOMPRESSED_BYTES == 52_428_800


@pytest.mark.parametrize(
    "content",
    [gzip.compress(urlset(("https://www.example.gov/a", None)))[:-12], b"\x1f\x8b" + b"\x00" * 40],
    ids=["truncated", "garbage"],
)
def test_parse_damagedGzip_isASitemapError(content: bytes) -> None:
    with pytest.raises(sm.SitemapError, match="damaged gzip"):
        sm.parse(content)


@pytest.mark.parametrize(
    "content",
    [
        b"<!DOCTYPE html><html><body>Please verify you are a human</body></html>",
        b"  <HTML><body>x</body></HTML>",
    ],
    ids=["challenge", "uppercase"],
)
def test_parse_webPage_isABlock(content: bytes) -> None:
    with pytest.raises(sm.SitemapError, match="challenge page"):
        sm.parse(content)


def test_main_damagedGzipForOneSeed_otherSeedsStillUpload(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = sm.Seed("other", "https://www.example.gov/other/sitemap.xml")
    monkeypatch.setattr(sm, "SEEDS", (SEED, other))

    def get(url: str) -> tuple[bytes, datetime]:
        if url == other.url:
            return gzip.compress(urlset(("https://www.example.gov/b", None)))[:-12], WHEN
        return urlset(("https://www.example.gov/a", "1")), WHEN

    assert sm.main([], api=api, get=get) == 1
    assert len(api.commits) == 1 and "demo 20" in api.commits[0]
    assert "other: not taken: a damaged gzip file" in capsys.readouterr().err


def test_take_childOverPlainHttp_refused() -> None:
    child = "http://www.example.gov/demo/a.xml"
    served = {SEED.url: index(child), child: urlset(("https://www.example.gov/1", None))}
    with pytest.raises(sm.SitemapError, match="not HTTPS"):
        sm.take(SEED, lambda url: (served[url], WHEN), today=date(2026, 11, 24))


@pytest.mark.parametrize("value", ["demo", "demo=", "=x.xml", "demo=  "])
def test_main_fromFileWithoutNameAndPath_isAUsageError(api: FakeApi, value: str) -> None:
    with pytest.raises(SystemExit):
        sm.main(["--from-file", value], api=api, get=lambda url: (b"", WHEN))
    assert api.created == []


def test_main_fromFileForASeedLeftOut_isAUsageError(
    api: FakeApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = sm.Seed("other", "https://www.example.gov/other/sitemap.xml")
    monkeypatch.setattr(sm, "SEEDS", (SEED, other))
    f = tmp_path / "demo.xml"
    f.write_bytes(urlset(("https://www.example.gov/a", "1")))
    with pytest.raises(SystemExit):
        sm.main(["--seed", "other", "--from-file", f"demo={f}"], api=api, get=lambda url: (b"", WHEN))
    assert "--seed leaves out" in capsys.readouterr().err
    assert api.created == []


def test_snapshot_errorThatReturns_stopsBeforeFetching(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    """With an error callback that doesn't exit, a usage error still returns 2 and takes nothing."""
    monkeypatch.setattr(sm, "SEEDS", (SEED,))
    errors: list[str] = []

    def never(url: str) -> tuple[bytes, datetime]:
        raise AssertionError("nothing is fetched after a usage error")

    for argv in (["--seed", "nope"], ["--from-file", "demo"], ["--from-file", "nope=x"]):
        args = sm.argparse.Namespace(**snapshot_args(argv))
        assert sm.snapshot(args, api=api, get=never, error=errors.append) == 2
    assert len(errors) == 3 and api.created == []


def test_allowedHosts_catalogOrAdrExceptionOnly(monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry.registry import load_registry

    monkeypatch.setattr(sm, "ADR_0013_HOSTS", frozenset({"www.michigan.gov", "www.kentcountymi.gov"}))
    reg = load_registry()
    catalogued = sm.catalogued_hosts(reg)
    assert "data.cdc.gov" in catalogued and "" not in catalogued
    cdc = sm.Seed("cdc", "https://data.cdc.gov/sitemap.xml")
    assert sm.allowed_hosts((*sm.SEEDS, cdc, SEED), registry=reg) == {
        "www.michigan.gov",
        "www.kentcountymi.gov",
        "data.cdc.gov",
    }


def test_main_uncataloguedHost_notTaken(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stray = sm.Seed("stray", "https://stray.example.org/sitemap.xml")
    monkeypatch.setattr(sm, "SEEDS", (stray,))
    used: list[frozenset[str]] = []

    def factory(*, hosts: frozenset[str]) -> sm.Getter:
        used.append(hosts)
        raise AssertionError("no getter for an uncatalogued host")

    monkeypatch.setattr(sm, "publisher_getter", factory)
    assert sm.main(["--fetcher", "http"], api=api) == 1
    assert "stray: not taken: stray.example.org is not a catalogued host" in capsys.readouterr().err
    assert used == [] and api.created == []


def test_publisherGetter_pacesAndUsesTheAllowedHosts(monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry import fetch as fetch_mod

    calls: list[tuple[str, frozenset[str]]] = []

    def fake_fetch(url: str, *, allowed_hosts: frozenset[str], max_bytes: int = 0, **kw: Any) -> Any:
        calls.append((url, allowed_hosts))
        if url.endswith("robots.txt"):
            return SimpleNamespace(status=200, content=b"", retrieved_at=WHEN)
        return SimpleNamespace(
            status=200, content=urlset(("https://www.example.gov/a", None)), retrieved_at=WHEN
        )

    monkeypatch.setattr(fetch_mod, "fetch", fake_fetch)
    slept = NoWait()
    get = sm.publisher_getter(hosts=HOSTS, pace=sm.Pace(sleep=slept, clock=lambda: 0.0))
    get(SEED.url)
    get(SEED.url)
    assert [c[0] for c in calls] == [ROBOTS, SEED.url, SEED.url]
    assert all(c[1] == HOSTS for c in calls)
    assert slept.slept == [10.0]
