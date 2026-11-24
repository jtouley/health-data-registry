"""Weekly sitemap snapshots, kept on Hugging Face so the crawlers know which publisher pages exist and changed.

A publisher's sitemap lists every page it serves, with the date each last changed. Taking one copy a week and
comparing it with the last gives the crawlers their queue: pages that are new, changed or gone. Nothing here is
data about people; a sitemap is a public index of a website.

Copies go to a private Hugging Face dataset (default `jtouley/health-data-registry-ops-sitemaps`), never to git:

    sitemaps/<host>/<name>/<YYYY-MM-DD>.xml.gz    the sitemap as fetched (gzipped), one per run
    sitemaps/<host>/<name>/latest.json            every page and its lastmod from the newest run, plus provenance
    sitemaps/<host>/<name>/<YYYY-MM-DD>.diff.json pages new, changed and removed since the previous run

Fetches go through the repo's guarded fetcher and read robots.txt first (`crawl.Robots`), with the repo's own
user agent. A host whose robots.txt answers 401/403 is treated as "no automated access" and skipped with a plain
reason; michigan.gov's edge network does this to cloud servers, so this job runs from a network it lets through,
or uses a copy a maintainer downloaded (`--from-file`). It never disguises itself.

Run it with `python -m health_data_registry.sitemaps --help`. Uploading needs HF_TOKEN with write access.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

DEFAULT_REPO = "jtouley/health-data-registry-ops-sitemaps"
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
# /podcast/, /emichigan/ and /minewswire/ and lists these sitemaps.
SEEDS: tuple[Seed, ...] = (
    Seed("mdhhs", "https://www.michigan.gov/mdhhs/sitemap.xml"),
    Seed("emergingdiseases", "https://www.michigan.gov/emergingdiseases/sitemap.xml"),
)


class SitemapError(ValueError):
    """A sitemap that can't be read as one; nothing is uploaded for it."""


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

    api.create_repo(repo_id, repo_type="dataset", private=True, exist_ok=True)
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


def publisher_getter() -> Getter:
    """robots.txt first, then the guarded fetcher, with the repo's honest user agent."""
    from health_data_registry.crawl import Robots
    from health_data_registry.fetch import fetch

    hosts = frozenset(s.host for s in SEEDS)
    robots = Robots(hosts)

    def get(url: str) -> tuple[bytes, datetime]:
        robots.wait_turn(url)
        r = fetch(url, allowed_hosts=hosts, max_bytes=MAX_SITEMAP_BYTES)
        if r.status != 200:
            raise SitemapError(f"{url} answered with status {r.status}")
        return r.content, r.retrieved_at

    return get


def main(argv: Sequence[str] | None = None, *, api: Any = None, get: Getter | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m health_data_registry.sitemaps", description=__doc__.split("\n")[0]
    )
    p.add_argument("--repo", default=DEFAULT_REPO, help="Hugging Face dataset to write to (private)")
    p.add_argument("--seed", action="append", help="only these seed names (default: all)")
    p.add_argument(
        "--from-file", action="append", default=[], metavar="NAME=PATH",
        help="use a sitemap a maintainer downloaded for seed NAME instead of fetching it",
    )  # fmt: skip
    args = p.parse_args(argv)
    from health_data_registry.fetch import FetchRefused

    seeds = {s.name: s for s in SEEDS}
    unknown = set(args.seed or []) - set(seeds)
    files: dict[str, Path] = {}
    for item in args.from_file:
        name, _, path = item.partition("=")
        files[name] = Path(path)
    unknown |= set(files) - set(seeds)
    if unknown:
        p.error(f"unknown seed(s): {', '.join(sorted(unknown))}; known: {', '.join(seeds)}")
    chosen = [seeds[n] for n in (args.seed or seeds)]
    today = datetime.now(UTC).date()
    snaps, failed = [], []
    getter = get
    for seed in chosen:
        try:
            if seed.name in files:
                snaps.append(from_file(seed, files[seed.name], today=today))
            else:
                getter = getter or publisher_getter()
                snaps.append(take(seed, getter, today=today))
        except (FetchRefused, SitemapError, OSError) as e:
            failed.append(seed.name)
            print(f"{seed.name}: not taken: {getattr(e, 'message', e)}", file=sys.stderr)
    if snaps:
        if api is None:
            import os

            from huggingface_hub import HfApi

            api = HfApi(token=os.environ.get("HF_TOKEN"))
        for r in upload(api, args.repo, snaps):
            print(
                f"{r['seed']} {r['taken_on']}: {r['pages']} pages; "
                f"{r['new']} new, {r['changed']} changed, {r['removed']} removed"
            )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
