"""The private tier: per-source stores for sources whose terms forbid public redistribution.

A source is private-tier when its catalog entry says ``private_store: true`` (and its contract says
``tier: private``). Its rows live only in that source's own store, never in the public lake area, a public
release or git:

    lake/private/<source_id>/<partition>.jsonl      written by the maintainer's crawl (CLI or API only)
    lake/private/<source_id>/<partition>.manifest.json   its row count, sha256 (its version), completeness
    lake/private/<source_id>/raw/<sha256>           the raw API responses those rows came from (owner-only)
    lake/private/_hub/<source_id>/                  read-only copy of the source's private Hugging Face dataset

The hosted server reads one private Hugging Face dataset per source,
``<owner>/health-data-registry-private-<source-id>`` (named by ``naming.private_dataset``), so each source can
be deleted on its own. The owner comes from ``HDR_PRIVATE_HF_OWNER`` and the read-only token
from ``HDR_PRIVATE_HF_TOKEN`` (a Space secret): it is never shown to users and nobody signs in. This module
never creates or uploads to a Hugging Face dataset; the maintainer does that.

Read order: the local private area, then the source's private hub dataset, then nothing, in which case each
tool falls back to the source's live links with a plain note. ``purge`` deletes one source's private copies.

Only contract columns are ever written, and every row must carry the contract's primary key, so an
undeclared field (a poster PDF, a page's HTML) can never slip into a store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from health_data_registry import __version__, naming
from health_data_registry.registry import Dataset
from health_data_registry.store import RawFetch

PRIVATE_DIR = "private"
HUB_CACHE_DIR = "_hub"
HF_OWNER_ENV = naming.OWNER_ENV
HF_TOKEN_ENV = "HDR_PRIVATE_HF_TOKEN"  # noqa: S105 - the name of an environment variable, not a secret
HUB_TTL_S = 3600.0
RAW_DIR = "raw"  # raw API responses of a private-tier source: lake/private/<id>/raw/<sha256>
MANIFEST_SUFFIX = ".manifest.json"

_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_PARTITION = re.compile(r"^[a-z0-9][a-z0-9_=.-]{0,63}$")


class PrivateRefused(ValueError):
    """A private-store rule was broken; ``code`` is for programs, the message is plain words."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PrivateRows:
    rows: list[dict[str, Any]]
    origin: str  # "local" or "hub"
    manifests: list[dict[str, Any]] = field(default_factory=list)  # one per partition (private.write_rows)
    revision: str | None = None  # the hub commit read (hub only)

    @property
    def complete(self) -> bool:
        """Every partition was read in full (a sample, or a partition without a manifest, is not)."""
        return bool(self.manifests) and all(m.get("complete") is True for m in self.manifests)

    @property
    def version(self) -> str:
        """The rows' version: a digest of every partition's sha256, in partition order."""
        joined = "".join(str(m.get("sha256")) for m in self.manifests)
        return hashlib.sha256(joined.encode()).hexdigest()[:12]


Downloader = Callable[[str, str, Path], "str | None"]  # (repo_id, token, local_dir) -> the revision read


def private_root(lake: Path) -> Path:
    return lake / PRIVATE_DIR


def _check_id(source_id: str) -> str:
    if not _SOURCE_ID.fullmatch(source_id):
        raise PrivateRefused(
            "bad_source_id", f"{source_id!r} is not a source id (snake_case letters and digits)"
        )
    return source_id


def area(lake: Path, source_id: str) -> Path:
    """The local private area of one source."""
    return private_root(lake) / _check_id(source_id)


def hub_cache(lake: Path, source_id: str) -> Path:
    return private_root(lake) / HUB_CACHE_DIR / _check_id(source_id)


def hf_repo(source_id: str, owner: str | None = None) -> str | None:
    """The private Hugging Face dataset of one source, or None when no owner is configured."""
    found = naming.owner(owner)
    return naming.private_dataset(_check_id(source_id), found) if found else None


def is_private(ds: Dataset) -> bool:
    return ds.entry.tier == "private"


def require_private(ds: Dataset) -> None:
    if not is_private(ds):
        raise PrivateRefused(
            "not_private_tier",
            f"{ds.id} is not a private-tier source (its tier is {ds.entry.tier}), so it has no private store",
        )


def primary_key(ds: Dataset) -> tuple[str, ...]:
    keys = tuple(
        str(p["name"])
        for obj in ds.contract.raw.get("schema") or []
        for p in obj.get("properties") or []
        if p.get("primaryKey")
    )
    if not keys:
        raise PrivateRefused("no_primary_key", f"{ds.id}: its contract marks no primaryKey column")
    return keys


# ------------------------------------------------------------------------------------------------ write


def _clean(ds: Dataset, row: Mapping[str, Any]) -> dict[str, Any]:
    allowed = ds.contract.column_names
    extra = sorted(set(row) - allowed)
    if extra:
        raise PrivateRefused(
            "undeclared_field",
            f"{ds.id}: {', '.join(extra)} is not a column of its contract, so it is never stored",
        )
    for c in ds.contract.columns:
        if c.required and row.get(c.name) in (None, ""):
            raise PrivateRefused("missing_required", f"{ds.id}: a row has no {c.name}")
    return {c.name: row.get(c.name) for c in ds.contract.columns}


def _key(row: Mapping[str, Any], keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(row[k]) for k in keys)


def _project(ds: Dataset, row: Mapping[str, Any]) -> dict[str, Any]:
    """A stored row onto the current contract's columns: a column the contract dropped is dropped from it too."""
    return {c.name: row.get(c.name) for c in ds.contract.columns}


def _private_dir(path: Path) -> Path:
    """Make a folder readable by its owner only."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _read_partition(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _manifest_path(folder: Path, partition: str) -> Path:
    return folder / f"{partition}{MANIFEST_SUFFIX}"


def stored_rows(lake: Path, ds: Dataset, partition: str) -> dict[tuple[str, ...], dict[str, Any]]:
    """One partition's rows as stored now, by primary key (empty when it has none)."""
    require_private(ds)
    keys = primary_key(ds)
    return {_key(r, keys): r for r in _read_partition(area(lake, ds.id) / f"{partition}.jsonl")}


def write_raw_private(lake: Path, ds: Dataset, fetches: Iterable[RawFetch]) -> None:
    """Keep raw API responses in the source's own private area (``raw/<sha256>``, owner-only), never in the shared
    lake/raw/: a response can carry text whose licence keeps it private."""
    require_private(ds)
    _private_dir(private_root(lake))
    folder = _private_dir(area(lake, ds.id) / RAW_DIR)
    for f in fetches:
        if hashlib.sha256(f.content).hexdigest() != f.sha256:
            raise PrivateRefused("sha256_mismatch", f"the bytes from {f.url} don't match their sha256")
        target = folder / f.sha256
        if not target.exists():
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(f.content)
            tmp.chmod(0o600)
            tmp.replace(target)


def write_rows(
    lake: Path,
    ds: Dataset,
    partition: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    complete: bool = False,
    replace: bool = False,
) -> int:
    """Write rows into one partition file of the source's private area. Returns its size.

    By default the rows are upserted by primary key. ``replace`` (a complete run) makes the partition exactly
    these rows, so a DOI that has left the supplement, or text kept under an earlier policy, is removed. Stored
    rows are projected onto the current contract's columns. A key already stored in another partition of the
    source is refused (the primary key is unique across the source). A manifest beside the partition
    (``<partition>.manifest.json``) records its row count, sha256, whether it was read in full, the time and the
    contract and harvester versions: its sha256 is the partition's version.
    """
    require_private(ds)
    if not _PARTITION.fullmatch(partition):
        raise PrivateRefused("bad_partition", f"{partition!r} is not a partition name")
    keys = primary_key(ds)
    _private_dir(private_root(lake))
    folder = _private_dir(area(lake, ds.id))
    target = folder / f"{partition}.jsonl"
    current: dict[tuple[str, ...], dict[str, Any]] = {}
    if not replace:
        current = {_key(r, keys): _project(ds, r) for r in _read_partition(target)}
    new = [_clean(ds, row) for row in rows]
    elsewhere = {
        _key(r, keys): p.name.removesuffix(".jsonl")
        for p in sorted(folder.glob("*.jsonl"))
        if p != target
        for r in _read_partition(p)
    }
    clash = sorted({k for r in new if (k := _key(r, keys)) in elsewhere})
    if clash:
        raise PrivateRefused(
            "duplicate_key",
            f"{ds.id}: {', '.join('/'.join(k) for k in clash[:3])} is already stored in "
            f"{elsewhere[clash[0]]}; one row per {', '.join(keys)} across the source",
        )
    for clean in new:
        current[_key(clean, keys)] = clean
    body = "".join(json.dumps(current[k], ensure_ascii=False, sort_keys=True) + "\n" for k in sorted(current))
    before = read_manifest(folder, partition)
    manifest = {
        "partition": partition,
        "rows": len(current),
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        # A sample upserted onto a partition read in full keeps every DOI the full read listed.
        "complete": bool(complete or (not replace and before is not None and before.get("complete") is True)),
        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "contract_version": ds.contract.version,
        "harvester_version": __version__,
    }
    tmp = folder / f".{partition}.jsonl.tmp"
    tmp.write_text(body, encoding="utf-8")
    tmp.chmod(0o600)
    mtmp = folder / f".{partition}{MANIFEST_SUFFIX}.tmp"
    mtmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    mtmp.chmod(0o600)
    tmp.replace(target)
    mtmp.replace(_manifest_path(folder, partition))
    return len(current)


# ------------------------------------------------------------------------------------------------ read


def read_manifest(folder: Path, partition: str) -> dict[str, Any] | None:
    path = _manifest_path(folder, partition)
    if not path.is_file():
        return None
    found = json.loads(path.read_text(encoding="utf-8"))
    return found if isinstance(found, dict) else None


def _read_folder(folder: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every partition's rows and its manifest (a partition with none gets one that isn't complete). The sha256
    is always that of the bytes read, whatever the manifest says."""
    rows: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    for p in sorted(folder.glob("*.jsonl")):
        body = p.read_bytes()
        rows += [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]
        partition = p.name.removesuffix(".jsonl")
        m = read_manifest(folder, partition) or {"partition": partition, "complete": False}
        manifests.append({**m, "sha256": hashlib.sha256(body).hexdigest()})
    return rows, manifests


def require_private_repo(repo_id: str, api: Any) -> str | None:
    """Refuse a hub dataset that isn't private; return the commit it is at now, so a read can be pinned to it."""
    info = api.repo_info(repo_id, repo_type="dataset")
    if getattr(info, "private", None) is not True:
        raise PrivateRefused(
            "hub_not_private",
            f"{repo_id} is not a private dataset, so nothing is read from it; make it private on the hub first",
        )
    sha = getattr(info, "sha", None)
    return str(sha) if sha else None


def _hf_download(repo_id: str, token: str, local_dir: Path) -> str | None:  # pragma: no cover - network
    from huggingface_hub import HfApi, snapshot_download

    revision = require_private_repo(repo_id, HfApi(token=token))
    snapshot_download(
        repo_id,
        repo_type="dataset",
        token=token,
        local_dir=str(local_dir),
        revision=revision,
        allow_patterns=["*.jsonl", f"*{MANIFEST_SUFFIX}"],
    )
    return revision


_hub_read_at: dict[str, float] = {}
_hub_revision: dict[str, str | None] = {}


def _swap_in(lake: Path, ds: Dataset, repo: str, token: str, downloader: Downloader) -> None:
    """Download into a fresh folder and swap it in whole, so a file removed upstream is gone here too."""
    cache = hub_cache(lake, ds.id)
    _private_dir(private_root(lake))
    _private_dir(cache.parent)
    fresh = cache.with_name(f".{cache.name}.new")
    if fresh.exists():
        shutil.rmtree(fresh)
    _private_dir(fresh)
    try:
        revision = downloader(repo, token, fresh)
        for f in fresh.rglob("*"):
            f.chmod(0o700 if f.is_dir() else 0o600)
    except BaseException:
        shutil.rmtree(fresh, ignore_errors=True)
        raise
    if cache.exists():
        shutil.rmtree(cache)
    fresh.rename(cache)
    _hub_revision[repo] = revision


def held_rows(lake: Path, ds: Dataset) -> list[dict[str, Any]]:
    """Every row of the source held on this machine: its local area, its hub cache, and the records its own
    listing crawl keeps between runs (crawl_state/listing/<id>/), read as files. Nothing is downloaded and the hub
    cache's freshness is not touched (for the pre-publish leak check)."""
    require_private(ds)
    rows: list[dict[str, Any]] = []
    for folder in (area(lake, ds.id), hub_cache(lake, ds.id)):
        if folder.is_dir():
            rows += _read_folder(folder)[0]
    crawl = lake / "crawl_state" / "listing" / ds.id
    for item in sorted(crawl.glob("*/items/*.json")) if crawl.is_dir() else []:
        try:
            doc = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows += [dict(r) for r in doc.get("records") or [] if isinstance(r, dict)]
    return rows


def read_rows(lake: Path, ds: Dataset, *, downloader: Downloader | None = None) -> PrivateRows | None:
    """The source's private rows: local area first, then its private hub dataset; None when neither exists.

    A hub dataset that isn't private is refused (PrivateRefused), never read.
    """
    require_private(ds)
    local = area(lake, ds.id)
    if local.is_dir() and any(local.glob("*.jsonl")):
        rows, manifests = _read_folder(local)
        return PrivateRows(rows, "local", manifests)
    repo = hf_repo(ds.id)
    token = os.environ.get(HF_TOKEN_ENV, "").strip()
    if repo is None or not token:
        return None
    cache = hub_cache(lake, ds.id)
    fresh = time.monotonic() - _hub_read_at.get(repo, -HUB_TTL_S) < HUB_TTL_S
    if not (fresh and cache.is_dir()):
        try:
            _swap_in(lake, ds, repo, token, downloader or _hf_download)
        except PrivateRefused:
            raise
        except Exception:  # noqa: BLE001 - gone, unreachable or not yet created: fall back to live links
            return None
        _hub_read_at[repo] = time.monotonic()
    rows, manifests = _read_folder(cache) if cache.is_dir() else ([], [])
    return PrivateRows(rows, "hub", manifests, _hub_revision.get(repo)) if rows else None


def contract_terms(ds: Dataset) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in ds.contract.raw.get("customProperties") or []:
        if isinstance(item, Mapping) and "property" in item:
            out[str(item["property"])] = item.get("value")
    return out


def notes(ds: Dataset) -> list[str]:
    """What every answer from a private-tier source carries: attribution, licence, footnotes, suppression, citation."""
    terms = contract_terms(ds)
    e = ds.entry
    lines = []
    if terms.get("attribution"):
        lines.append(str(terms["attribution"]))
    licence = f"Licence: {e.licence.id} ({e.licence.terms_url})"
    if terms.get("licenceNote"):
        licence += f": {terms['licenceNote']}"
    lines.append(licence)
    lines += [f"Note: {c}" for c in e.caveats]
    lines.append(
        "Suppression: "
        + (
            str(terms["suppression"])
            if terms.get("suppression")
            else "the publisher declares none for this source."
        )
    )
    lines.append(f"How to cite: {e.citation}")
    return lines


# ------------------------------------------------------------------------------------------------ purge


def purge(lake: Path, source_id: str, *, api: Any = None, owner: str | None = None) -> list[str]:
    """Delete every private copy of one source: its local area, its hub cache and its private hub dataset.

    The hub dataset is deleted only when an owner is configured and a write token is in HF_TOKEN (or ``api``
    is passed). Returns one plain line per thing deleted or skipped.
    """
    done: list[str] = []
    for folder, what in (
        (area(lake, source_id), "local private area"),
        (hub_cache(lake, source_id), "hub cache"),
    ):
        if folder.exists():
            shutil.rmtree(folder)
            done.append(f"deleted the {what} {folder}")
    repo = hf_repo(source_id, owner)
    if repo is not None:
        if api is None and os.environ.get("HF_TOKEN"):
            from huggingface_hub import HfApi

            api = HfApi(token=os.environ["HF_TOKEN"])
        if api is not None:
            api.delete_repo(repo, repo_type="dataset", missing_ok=True)
            done.append(f"deleted the private hub dataset {repo}")
        else:
            done.append(
                f"the private hub dataset {repo} was not deleted: set HF_TOKEN to a write token and rerun"
            )
    _hub_read_at.pop(repo or "", None)
    return done or [f"{source_id} had no private copy on this computer"]
