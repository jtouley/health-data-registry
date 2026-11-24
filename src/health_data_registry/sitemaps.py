"""Weekly sitemap snapshots, kept on Hugging Face so the crawlers know which publisher pages exist and changed.

A publisher's sitemap lists every page it serves, with the date each last changed. Taking one copy a week and
comparing it with the last gives the crawlers their queue: pages that are new, changed or gone. Nothing here is
data about people; a sitemap is a public index of a website.

Copies go to a private Hugging Face dataset (default `jtouley/health-data-registry-ops-sitemaps`, named by
naming.py), never to git:

    sitemaps/<host>/<name>/<YYYY-MM-DD>.xml.gz    the sitemap as fetched (gzipped), one per run
    sitemaps/<host>/<name>/latest.json            every page and its lastmod from the newest run, plus provenance
    sitemaps/<host>/<name>/<YYYY-MM-DD>.diff.json pages new, changed and removed since the previous run

Two fetchers, both reading robots.txt first and both sending the repo's own honest user agent:

- `browser` (the default): headless Chromium through `browser_fetch.py`. michigan.gov's edge network (Akamai)
  refuses plain HTTPS clients from cloud servers, and serves the same public sitemap to a real browser.
- `http`: the guarded fetcher (`fetch.py`), for hosts that answer it, or where the browser fetcher isn't
  installed yet.

A refusal (a 401/403, an edge "Access Denied" page, a robots.txt that disallows the path) is final: the seed is
reported as not taken and nothing is retried another way. A maintainer can instead upload a copy they downloaded
in their own browser (`--from-file`). This job never disguises itself.

Run it with `health-data-registry sitemaps snapshot --help`. Uploading needs HF_TOKEN with write access.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib
import json
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from health_data_registry import naming
from health_data_registry.crawl import Robots
from health_data_registry.fetch import FetchRefused

OPS_JOB = "sitemaps"
DEFAULT_REPO = naming.ops_dataset(OPS_JOB)
BROWSER_MODULE = "health_data_registry.browser_fetch"
FETCHERS = ("browser", "http")
NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
MAX_SITEMAP_BYTES = 100_000_000
MAX_CHILD_SITEMAPS = 200


@dataclass(frozen=True)
class Seed:
    """One sitemap to follow: `name` is a short stable label, `url` the sitemap's address."""

    name: str
    url: str

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


# The sitemaps the Michigan crawlers start from. michigan.gov's robots.txt (User-agent: *) disallows only /rss/,
# /podcast/, /emichigan/ and /minewswire/ and lists its sitemaps. kentcountymi.gov redirects to www.
SEEDS: tuple[Seed, ...] = (
    Seed("mdhhs", "https://www.michigan.gov/mdhhs/sitemap.xml"),
    Seed("emergingdiseases", "https://www.michigan.gov/emergingdiseases/sitemap.xml"),
    Seed("kentcounty", "https://www.kentcountymi.gov/sitemap.xml"),
)


class SitemapError(ValueError):
    """A sitemap that can't be read as one; nothing is uploaded for it."""


class BrowserUnavailable(RuntimeError):
    """The browser fetcher isn't part of this install yet."""


@dataclass(frozen=True)
class Page:
    loc: str
    lastmod: str | None


@dataclass
class Snapshot:
    seed: Seed
    taken_on: date
    raw: bytes  # the sitemap bytes as fetched (an index's children are concatenated in order)
    pages: list[Page]
    retrieved_at: datetime
    source: str  # "publisher" or "maintainer_file"
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        self.sha256 = hashlib.sha256(self.raw).hexdigest()


def parse(content: bytes) -> tuple[list[Page], list[str]]:
    """The pages of a <urlset>, or the child sitemap URLs of a <sitemapindex>. Raises SitemapError otherwise."""
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    try:
        root = ElementTree.fromstring(content)  # noqa: S314 - stdlib parser; no DTDs or external entities in sitemaps
    except ElementTree.ParseError as e:
        raise SitemapError(f"not XML: {e}") from e
    tag = root.tag.removeprefix(NS)
    if tag == "urlset":
        pages = []
        for u in root.iter(f"{NS}url"):
            loc = (u.findtext(f"{NS}loc") or "").strip()
            if loc:
                lastmod = (u.findtext(f"{NS}lastmod") or "").strip() or None
                pages.append(Page(loc, lastmod))
        return pages, []
    if tag == "sitemapindex":
        children = [(s.findtext(f"{NS}loc") or "").strip() for s in root.iter(f"{NS}sitemap")]
        return [], [c for c in children if c]
    raise SitemapError(f"expected <urlset> or <sitemapindex>, found <{tag}>")


Getter = Callable[[str], tuple[bytes, datetime]]


def take(seed: Seed, get: Getter, *, today: date) -> Snapshot:
    """Fetch one seed's sitemap, following a sitemap index to its children on the same host."""
    content, when = get(seed.url)
    pages, children = parse(content)
    raw = content
    if children:
        if len(children) > MAX_CHILD_SITEMAPS:
            raise SitemapError(
                f"{seed.url} lists {len(children)} child sitemaps, more than {MAX_CHILD_SITEMAPS}"
            )
        parts = [content]
        for child in children:
            if (urlsplit(child).hostname or "").lower() != seed.host:
                raise SitemapError(f"{seed.url} points to a sitemap on another host: {child}")
            body, _ = get(child)
            child_pages, grandchildren = parse(body)
            if grandchildren:
                raise SitemapError(f"{child} is itself an index; nested indexes aren't followed")
            pages.extend(child_pages)
            parts.append(body)
        raw = b"\n".join(parts)
    if not pages:
        raise SitemapError(f"{seed.url} lists no pages")
    return Snapshot(seed, today, raw, pages, when, "publisher")


def from_file(seed: Seed, path: Path, *, today: date) -> Snapshot:
    """A sitemap a maintainer downloaded in their own browser (when the publisher refuses automated access)."""
    content = path.read_bytes()
    pages, children = parse(content)
    if children:
        raise SitemapError(f"{path} is a sitemap index; download each child sitemap it lists instead")
    if not pages:
        raise SitemapError(f"{path} lists no pages")
    return Snapshot(seed, today, content, pages, datetime.now(UTC), "maintainer_file")


def diff(previous: Sequence[Page] | None, current: Sequence[Page]) -> dict[str, list[str]]:
    """Pages new, changed (lastmod moved) and removed since the previous snapshot. No previous: all are new."""
    before = {p.loc: p.lastmod for p in previous or ()}
    after = {p.loc: p.lastmod for p in current}
    return {
        "new": sorted(set(after) - set(before)),
        "changed": sorted(loc for loc in set(after) & set(before) if after[loc] != before[loc]),
        "removed": sorted(set(before) - set(after)),
    }


def _prefix(seed: Seed) -> str:
    return f"sitemaps/{seed.host}/{seed.name}"


def latest_document(snap: Snapshot) -> dict[str, Any]:
    return {
        "seed": snap.seed.name,
        "url": snap.seed.url,
        "taken_on": snap.taken_on.isoformat(),
        "retrieved_at": snap.retrieved_at.isoformat(),
        "source": snap.source,
        "sha256": snap.sha256,
        "pages": [{"loc": p.loc, "lastmod": p.lastmod} for p in snap.pages],
    }


def previous_pages(api: Any, repo_id: str, seed: Seed) -> list[Page] | None:
    """The pages from the newest snapshot on the hub, or None when there isn't one yet."""
    from huggingface_hub.errors import EntryNotFoundError, RepositoryNotFoundError

    try:
        path = api.hf_hub_download(repo_id, f"{_prefix(seed)}/latest.json", repo_type="dataset")
    except (EntryNotFoundError, RepositoryNotFoundError):
        return None
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Page(p["loc"], p.get("lastmod")) for p in doc["pages"]]


def upload(api: Any, repo_id: str, snaps: Iterable[Snapshot]) -> list[dict[str, Any]]:
    """One commit per run: each snapshot's gzipped sitemap, its latest.json and its diff. Returns the diffs."""
    from huggingface_hub import CommitOperationAdd

    # Created private on the first run if missing; an existing dataset that is public is never written to.
    api.create_repo(repo_id, repo_type="dataset", private=True, exist_ok=True)
    if not api.repo_info(repo_id, repo_type="dataset").private:
        raise SitemapError(f"{repo_id} is public; sitemap snapshots go only to a private dataset")
    ops, summary = [], []
    for snap in snaps:
        prefix = _prefix(snap.seed)
        day = snap.taken_on.isoformat()
        change = diff(previous_pages(api, repo_id, snap.seed), snap.pages)
        report = {"seed": snap.seed.name, "taken_on": day, "pages": len(snap.pages)}
        report.update({k: len(v) for k, v in change.items()})
        summary.append(report)
        files = {
            f"{prefix}/{day}.xml.gz": gzip.compress(snap.raw, mtime=0),
            f"{prefix}/latest.json": json.dumps(latest_document(snap), indent=1).encode(),
            f"{prefix}/{day}.diff.json": json.dumps({"taken_on": day, **change}, indent=1).encode(),
        }
        ops += [CommitOperationAdd(path_in_repo=p, path_or_fileobj=b) for p, b in files.items()]
    if ops:
        api.create_commit(
            repo_id=repo_id, repo_type="dataset", operations=ops,
            commit_message="Sitemap snapshots " + ", ".join(f"{r['seed']} {r['taken_on']}" for r in summary),
        )  # fmt: skip
    return summary


def _hosts() -> frozenset[str]:
    return frozenset(s.host for s in SEEDS)


def publisher_getter() -> Getter:
    """Plain HTTPS: robots.txt first, then the guarded fetcher, with the repo's honest user agent."""
    from health_data_registry.fetch import fetch

    hosts = _hosts()
    robots = Robots(hosts)

    def get(url: str) -> tuple[bytes, datetime]:
        robots.wait_turn(url)
        r = fetch(url, allowed_hosts=hosts, max_bytes=MAX_SITEMAP_BYTES)
        if r.status != 200:
            raise SitemapError(f"{url} answered with status {r.status}")
        return r.content, r.retrieved_at

    return get


Reader = Callable[[str], tuple[bytes, datetime]]


class BrowserRobots(Robots):
    """crawl.Robots, with robots.txt read by the same browser as the sitemap (same rules for each answer).

    A host that blocks the browser's robots.txt request raises FetchRefused("blocked"): the seed is not taken.
    """

    def __init__(self, hosts: frozenset[str], read: Reader, **kw: Any) -> None:
        super().__init__(hosts, **kw)
        self.read = read

    def _rules(self, host: str) -> RobotFileParser | None:
        if host in self.rules:
            return self.rules[host]
        rp = RobotFileParser()
        try:
            body, _ = self.read(f"https://{host}/robots.txt")
            rp.parse(body.decode("utf-8", "replace").splitlines())
        except FetchRefused as e:
            if e.code != "http_error" or e.status is None or e.status >= 500:
                raise
            rp.parse([])  # 404 and other 4xx: no robots.txt, so everything is allowed (RFC 9309)
        self.rules[host] = rp
        return rp


def browser_getter(*, module: Any = None) -> Getter:
    """Headless Chromium (browser_fetch.py) with the honest user agent; robots.txt read the same way first."""
    if module is None:
        try:
            module = importlib.import_module(BROWSER_MODULE)
        except ImportError as e:
            raise BrowserUnavailable(
                "The browser fetcher isn't in this version yet. Use --fetcher http, or wait for it to be merged."
            ) from e
    hosts = _hosts()

    def read(url: str) -> tuple[bytes, datetime]:
        page = module.browser_fetch(url, allowed_hosts=hosts, max_bytes=MAX_SITEMAP_BYTES).page
        return page.content, page.retrieved_at

    robots = BrowserRobots(hosts, read)

    def get(url: str) -> tuple[bytes, datetime]:
        robots.wait_turn(url)
        return read(url)

    return get


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=DEFAULT_REPO, help="Hugging Face dataset to write to (private)")
    p.add_argument("--seed", action="append", help="only these seed names (default: all)")
    p.add_argument(
        "--fetcher", choices=FETCHERS, default="browser",
        help="browser (headless Chromium, the default) or http (plain HTTPS, refused by some hosts)",
    )  # fmt: skip
    p.add_argument(
        "--from-file", action="append", default=[], metavar="NAME=PATH",
        help="use a sitemap a maintainer downloaded for seed NAME instead of fetching it",
    )  # fmt: skip


def checked_repo(repo_id: str) -> str:
    """`repo_id` if it is `<owner>/health-data-registry-ops-sitemaps` (naming.py); ValueError otherwise."""
    who, _, _ = repo_id.partition("/")
    try:
        want = naming.ops_dataset(OPS_JOB, who)
    except naming.BadName as e:
        raise ValueError(f"{repo_id!r}: {e}") from e
    if repo_id != want:
        raise ValueError(f"{repo_id!r} is not the sitemap dataset; use {want} (docs/dev/naming.md)")
    return repo_id


def snapshot(
    args: argparse.Namespace, *, api: Any = None, get: Getter | None = None, error: Callable[[str], Any]
) -> int:
    """Take every chosen seed, upload what was taken in one commit. 0: all taken; 1: one or more not taken."""
    try:
        repo = checked_repo(args.repo)
    except ValueError as e:
        error(str(e))
        return 2
    seeds = {s.name: s for s in SEEDS}
    unknown = set(args.seed or []) - set(seeds)
    files: dict[str, Path] = {}
    for item in args.from_file:
        name, _, path = item.partition("=")
        files[name] = Path(path)
    unknown |= set(files) - set(seeds)
    if unknown:
        error(f"unknown seed(s): {', '.join(sorted(unknown))}; known: {', '.join(seeds)}")
    chosen = [seeds[n] for n in (args.seed or seeds)]
    today = datetime.now(UTC).date()
    snaps, failed = [], []
    getter = get
    for seed in chosen:
        try:
            if seed.name in files:
                snaps.append(from_file(seed, files[seed.name], today=today))
                continue
            if getter is None:
                getter = browser_getter() if args.fetcher == "browser" else publisher_getter()
            snaps.append(take(seed, getter, today=today))
        except BrowserUnavailable as e:
            print(str(e), file=sys.stderr)
            return 2
        except (FetchRefused, SitemapError, OSError) as e:
            failed.append(seed.name)
            print(f"{seed.name}: not taken: {getattr(e, 'message', e)}", file=sys.stderr)
    if snaps:
        if api is None:
            from huggingface_hub import HfApi

            api = HfApi(token=os.environ.get("HF_TOKEN"))
        for r in upload(api, repo, snaps):
            print(
                f"{r['seed']} {r['taken_on']}: {r['pages']} pages; "
                f"{r['new']} new, {r['changed']} changed, {r['removed']} removed"
            )
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None, *, api: Any = None, get: Getter | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m health_data_registry.sitemaps", description=__doc__.split("\n")[0]
    )
    add_arguments(p)
    return snapshot(p.parse_args(argv), api=api, get=get, error=p.error)


if __name__ == "__main__":
    raise SystemExit(main())
