"""Weekly sitemap snapshots, kept on Hugging Face so the crawlers know which publisher pages exist and changed.

A publisher's sitemap lists every page it serves, with the date each last changed. Taking one copy a week and
comparing it with the last gives the crawlers their queue: pages that are new, changed or gone. Nothing here is
data about people; a sitemap is a public index of a website.

Copies go to a private Hugging Face dataset (default `<HDR_PRIVATE_HF_OWNER>/health-data-registry-ops-sitemaps`,
named by naming.py), never to git:

    sitemaps/<host>/<name>/<YYYY-MM-DD>.xml.gz    the sitemap as fetched (gzipped), one per run
    sitemaps/<host>/<name>/<YYYY-MM-DD>.json      every page and its lastmod from that day's run, plus provenance
    sitemaps/<host>/<name>/latest.json            the same, for the newest run
    sitemaps/<host>/<name>/<YYYY-MM-DD>.diff.json pages new, changed and removed since the last run on an earlier
                                                  day; a second run the same day diffs against that same earlier
                                                  run, so the day's diff is never emptied

Two fetchers, both reading robots.txt first, both at least PACE_S (10 s) apart per host, and both sending the
repo's own honest user agent:

- `browser` (the default): headless Chromium through `browser_fetch.py`. michigan.gov's edge network (Akamai)
  refuses plain HTTPS clients from cloud servers, and serves the same public sitemap to a real browser. The
  maintainer allowed this on government hosts, under stop conditions (docs/adr/0013).
- `http`: the guarded fetcher (`fetch.py`), for hosts that answer it, or where the browser fetcher isn't
  installed yet.

Hosts: a seed is fetched only from a host a catalog entry names, or one ADR 0013 records as an exception until
its catalog entry lands. Child sitemaps must be HTTPS on the seed's own host.

A refusal (a 401/403/429, an edge "Access Denied" or challenge page, a CAPTCHA, a robots.txt that disallows the
path or that the host refuses to show) is final: the seed is reported as not taken and nothing is retried another
way. A maintainer can instead upload a copy they downloaded in their own browser (`--from-file`). This job never
disguises itself.

Run it with `health-data-registry sitemaps snapshot --help`. Uploading needs HF_TOKEN with write access.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib
import io
import json
import os
import sys
import time
import zlib
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
BROWSER_MODULE = "health_data_registry.browser_fetch"
FETCHERS = ("browser", "http")
NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
MAX_SITEMAP_BYTES = 100_000_000
# The sitemaps protocol caps one sitemap at 50 MB uncompressed (sitemaps.org/protocol.html); a gzipped sitemap is
# never expanded past it.
MAX_UNCOMPRESSED_BYTES = 52_428_800
MAX_CHILD_SITEMAPS = 200
# The government-host policy (ADR 0013): at least this many seconds between two requests to one host.
PACE_S = 10.0


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

# Hard rule 6: fetches go only to catalogued publisher hosts. These two seed hosts aren't named by a catalog entry
# yet (the Michigan entries come with PR #19), so the maintainer recorded them as an exception in
# docs/adr/0013-browser-fetcher-on-government-hosts.md. Adding a host here needs an amendment to that ADR.
ADR_0013_HOSTS: frozenset[str] = frozenset({"www.michigan.gov", "www.kentcountymi.gov"})


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


def _gunzip(content: bytes) -> bytes:
    """A gzipped sitemap, expanded no further than MAX_UNCOMPRESSED_BYTES; a damaged file is a SitemapError."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as fh:
            out = fh.read(MAX_UNCOMPRESSED_BYTES + 1)
    except (OSError, EOFError, zlib.error) as e:
        raise SitemapError(f"a damaged gzip file: {e}") from e
    if len(out) > MAX_UNCOMPRESSED_BYTES:
        raise SitemapError(f"expands past {MAX_UNCOMPRESSED_BYTES} bytes, the sitemaps protocol's limit")
    return out


def parse(content: bytes) -> tuple[list[Page], list[str]]:
    """The pages of a <urlset>, or the child sitemap URLs of a <sitemapindex>. Raises SitemapError otherwise."""
    if content[:2] == b"\x1f\x8b":
        content = _gunzip(content)
    elif len(content) > MAX_UNCOMPRESSED_BYTES:
        raise SitemapError(f"larger than {MAX_UNCOMPRESSED_BYTES} bytes, the sitemaps protocol's limit")
    if content[:512].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        # An edge "Access Denied" or challenge page, or a CAPTCHA: a block, never retried another way (ADR 0013).
        raise SitemapError(
            "the host answered with a web page, not a sitemap (an access-denied or challenge page)"
        )
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
            where = urlsplit(child)
            if where.scheme != "https":
                raise SitemapError(f"{seed.url} points to a sitemap that is not HTTPS: {child}")
            if (where.hostname or "").lower() != seed.host:
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


def _hub_json(api: Any, repo_id: str, path: str) -> dict[str, Any] | None:
    from huggingface_hub.errors import EntryNotFoundError, RepositoryNotFoundError

    try:
        local = api.hf_hub_download(repo_id, path, repo_type="dataset")
    except (EntryNotFoundError, RepositoryNotFoundError):
        return None
    doc: dict[str, Any] = json.loads(Path(local).read_text(encoding="utf-8"))
    return doc


def previous_pages(api: Any, repo_id: str, seed: Seed, today: date) -> tuple[list[Page] | None, str | None]:
    """The pages and date of the newest snapshot taken BEFORE `today`; (None, None) when there isn't one.

    latest.json is that snapshot unless a run already happened today; then its `previous_taken_on` names the
    earlier day, whose dated page list is read instead. A second run on one day so diffs against the same earlier
    run as the first, and the day's diff keeps every page the first run found.
    """
    doc = _hub_json(api, repo_id, f"{_prefix(seed)}/latest.json")
    if doc is not None and doc.get("taken_on", "") >= today.isoformat():
        before = doc.get("previous_taken_on")
        doc = _hub_json(api, repo_id, f"{_prefix(seed)}/{before}.json") if before else None
    if doc is None:
        return None, None
    return [Page(p["loc"], p.get("lastmod")) for p in doc["pages"]], doc.get("taken_on")


def upload(api: Any, repo_id: str, snaps: Iterable[Snapshot]) -> list[dict[str, Any]]:
    """One commit per run: each snapshot's gzipped sitemap, page lists and diff. Returns the diffs."""
    from huggingface_hub import CommitOperationAdd

    # Created private on the first run if missing; an existing dataset that is public is never written to.
    try:
        naming.assert_private(api, repo_id)
    except naming.NotPrivate as e:
        raise SitemapError(str(e)) from e
    ops, summary = [], []
    for snap in snaps:
        prefix = _prefix(snap.seed)
        day = snap.taken_on.isoformat()
        before, since = previous_pages(api, repo_id, snap.seed, snap.taken_on)
        change = diff(before, snap.pages)
        report = {"seed": snap.seed.name, "taken_on": day, "pages": len(snap.pages)}
        report.update({k: len(v) for k, v in change.items()})
        summary.append(report)
        page_list = json.dumps(latest_document(snap) | {"previous_taken_on": since}, indent=1).encode()
        files = {
            f"{prefix}/{day}.xml.gz": gzip.compress(snap.raw, mtime=0),
            f"{prefix}/{day}.json": page_list,
            f"{prefix}/latest.json": page_list,
            f"{prefix}/{day}.diff.json": json.dumps(
                {"taken_on": day, "since": since, **change}, indent=1
            ).encode(),
        }
        ops += [CommitOperationAdd(path_in_repo=p, path_or_fileobj=b) for p, b in files.items()]
    if ops:
        api.create_commit(
            repo_id=repo_id, repo_type="dataset", operations=ops,
            commit_message="Sitemap snapshots " + ", ".join(f"{r['seed']} {r['taken_on']}" for r in summary),
        )  # fmt: skip
    return summary


def catalogued_hosts(registry: Any = None) -> frozenset[str]:
    """Every host a catalog entry names: its data host and the hosts of its landing, data, codebook and terms pages."""
    if registry is None:
        from health_data_registry.registry import load_registry

        registry = load_registry()
    found: set[str] = set()
    for ds in registry:
        e = ds.entry
        found.add(e.source.host.lower())
        for url in (e.source.landing_url, e.source.api_url, e.codebook_url, e.licence.terms_url):
            found.add((urlsplit(url).hostname or "").lower())
    return frozenset(found - {""})


def allowed_hosts(seeds: Iterable[Seed] = SEEDS, *, registry: Any = None) -> frozenset[str]:
    """The seed hosts that may be fetched: those a catalog entry names, plus ADR 0013's recorded exceptions."""
    allowed = catalogued_hosts(registry) | ADR_0013_HOSTS
    return frozenset(s.host for s in seeds if s.host in allowed)


class Pace:
    """At least PACE_S seconds between two requests to one host (ADR 0013), whatever robots.txt allows."""

    def __init__(
        self,
        seconds: float = PACE_S,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.seconds, self.sleep, self.clock = seconds, sleep, clock
        self.last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if host in self.last:
            due = self.last[host] + self.seconds - self.clock()
            if due > 0:
                self.sleep(due)
        self.last[host] = self.clock()


def publisher_getter(*, hosts: frozenset[str] | None = None, pace: Pace | None = None) -> Getter:
    """Plain HTTPS: robots.txt first, then the guarded fetcher, with the repo's honest user agent."""
    from health_data_registry.fetch import fetch

    allowed = allowed_hosts() if hosts is None else hosts
    robots = Robots(allowed)
    paced = pace or Pace()

    def get(url: str) -> tuple[bytes, datetime]:
        robots.wait_turn(url)
        paced.wait(url)
        r = fetch(url, allowed_hosts=allowed, max_bytes=MAX_SITEMAP_BYTES)
        if r.status != 200:
            raise SitemapError(f"{url} answered with status {r.status}")
        return r.content, r.retrieved_at

    return get


Reader = Callable[[str], tuple[bytes, datetime]]


# The only answers for robots.txt that mean "there is no robots.txt", so everything is allowed (RFC 9309).
ROBOTS_ABSENT = frozenset({404, 410})
# Answers that are a refusal: the seed is not taken (ADR 0013).
ROBOTS_REFUSED = frozenset({401, 403, 429})


class BrowserRobots(Robots):
    """crawl.Robots, with robots.txt read by the same browser as the sitemap.

    Only a 404 or 410 for robots.txt allows everything. A 401, 403 or 429 (as "http_error" or "blocked") raises
    FetchRefused("blocked"), and any other refusal or a 5xx is raised as it came: the seed is not taken.
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
            if e.code == "http_error" and e.status in ROBOTS_ABSENT:
                rp.parse([])  # no robots.txt, so everything is allowed (RFC 9309)
            elif e.code == "http_error" and e.status in ROBOTS_REFUSED:
                raise FetchRefused(
                    "blocked", f"{host} refused to show its robots.txt (status {e.status})", status=e.status
                ) from e
            else:
                raise
        self.rules[host] = rp
        return rp


def browser_getter(
    *, module: Any = None, hosts: frozenset[str] | None = None, pace: Pace | None = None
) -> Getter:
    """Headless Chromium (browser_fetch.py) with the honest user agent; robots.txt read the same way first."""
    if module is None:
        try:
            module = importlib.import_module(BROWSER_MODULE)
        except ImportError as e:
            raise BrowserUnavailable(
                "The browser fetcher isn't in this version yet. Use --fetcher http, or wait for it to be merged."
            ) from e
    allowed = allowed_hosts() if hosts is None else hosts
    paced = pace or Pace()

    def read(url: str) -> tuple[bytes, datetime]:
        paced.wait(url)
        page = module.browser_fetch(url, allowed_hosts=allowed, max_bytes=MAX_SITEMAP_BYTES).page
        return page.content, page.retrieved_at

    robots = BrowserRobots(allowed, read)

    def get(url: str) -> tuple[bytes, datetime]:
        robots.wait_turn(url)
        return read(url)

    return get


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--repo",
        help="Hugging Face dataset to write to (private; default: <HDR_PRIVATE_HF_OWNER>'s ops-sitemaps)",
    )
    p.add_argument("--seed", action="append", help="only these seed names (default: all)")
    p.add_argument(
        "--fetcher", choices=FETCHERS, default="browser",
        help="browser (headless Chromium, the default) or http (plain HTTPS, refused by some hosts)",
    )  # fmt: skip
    p.add_argument(
        "--from-file", action="append", default=[], metavar="NAME=PATH",
        help="use a sitemap a maintainer downloaded for seed NAME instead of fetching it (NAME must be chosen)",
    )  # fmt: skip


def default_repo() -> str:
    """The sitemap dataset of HDR_PRIVATE_HF_OWNER (else jtouley), read when the command runs."""
    return naming.ops_dataset(OPS_JOB, naming.private_owner())


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
        repo = checked_repo(args.repo or default_repo())
    except ValueError as e:  # naming.BadName from HDR_PRIVATE_HF_OWNER is a ValueError too
        error(str(e))
        return 2
    seeds = {s.name: s for s in SEEDS}
    unknown = set(args.seed or []) - set(seeds)
    files: dict[str, Path] = {}
    for item in args.from_file:
        name, eq, path = item.partition("=")
        if not eq or not name or not path.strip():
            error(f"--from-file {item!r}: give it as NAME=PATH, e.g. mdhhs=~/Downloads/sitemap.xml")
            return 2
        files[name] = Path(path).expanduser()
    unknown |= set(files) - set(seeds)
    if unknown:
        error(f"unknown seed(s): {', '.join(sorted(unknown))}; known: {', '.join(seeds)}")
        return 2
    chosen = [seeds[n] for n in (args.seed or seeds)]
    left_out = set(files) - {s.name for s in chosen}
    if left_out:
        error(
            f"--from-file names {', '.join(sorted(left_out))}, which --seed leaves out; add --seed for each, "
            "or drop --seed"
        )
        return 2
    allowed = allowed_hosts(chosen) if get is None else frozenset(s.host for s in chosen)
    today = datetime.now(UTC).date()
    snaps, failed = [], []
    getter = get
    for seed in chosen:
        try:
            if seed.name in files:
                snaps.append(from_file(seed, files[seed.name], today=today))
                continue
            if seed.host not in allowed:
                raise SitemapError(
                    f"{seed.host} is not a catalogued host or an ADR 0013 exception (hard rule 6)"
                )
            if getter is None:
                kind = browser_getter if args.fetcher == "browser" else publisher_getter
                getter = kind(hosts=allowed)
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
