"""The private tier: per-source stores for sources whose terms forbid public redistribution.

A source is private-tier when its catalog entry says ``hosting: private`` (and its contract agrees). Its rows
live only in that source's own store, never in the public lake area, a public release or git:

    lake/private/<source_id>/<partition>.jsonl      written by the maintainer's crawl (CLI or API only)
    lake/private/_hub/<source_id>/                  read-only copy of the source's private Hugging Face dataset

The hosted server reads one private Hugging Face dataset per source, ``<owner>/hdr-private-<source_id>``, so
each source can be deleted on its own. The owner comes from ``HDR_PRIVATE_HF_OWNER`` and the read-only token
from ``HDR_PRIVATE_HF_TOKEN`` (a Space secret): it is never shown to users and nobody signs in. This module
never creates or uploads to a Hugging Face dataset; the maintainer does that.

Read order: the local private area, then the source's private hub dataset, then nothing, in which case each
tool falls back to the source's live links with a plain note. ``purge`` deletes one source's private copies.

Only contract columns are ever written, and every row must carry the contract's primary key, so an
undeclared field (a poster PDF, a page's HTML) can never slip into a store.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from health_data_registry.registry import Dataset

PRIVATE_DIR = "private"
HUB_CACHE_DIR = "_hub"
HF_OWNER_ENV = "HDR_PRIVATE_HF_OWNER"
HF_TOKEN_ENV = "HDR_PRIVATE_HF_TOKEN"  # noqa: S105 - the name of an environment variable, not a secret
HF_REPO_PREFIX = "hdr-private-"
HUB_TTL_S = 3600.0

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


Downloader = Callable[[str, str, Path], None]  # (repo_id, token, local_dir)


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
    owner = owner if owner is not None else os.environ.get(HF_OWNER_ENV, "").strip()
    return f"{owner}/{HF_REPO_PREFIX}{_check_id(source_id)}" if owner else None


def is_private(ds: Dataset) -> bool:
    return ds.entry.tier == "private"


def require_private(ds: Dataset) -> None:
    if not is_private(ds):
        raise PrivateRefused(
            "not_private_tier",
            f"{ds.id} is not a private-tier source (its catalog hosting is {ds.entry.tier}), so it has no "
            "private store",
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


def write_rows(lake: Path, ds: Dataset, partition: str, rows: Iterable[Mapping[str, Any]]) -> int:
    """Upsert rows into one partition file of the source's private area (by primary key). Returns its size."""
    require_private(ds)
    if not _PARTITION.fullmatch(partition):
        raise PrivateRefused("bad_partition", f"{partition!r} is not a partition name")
    keys = primary_key(ds)
    folder = area(lake, ds.id)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    target = folder / f"{partition}.jsonl"
    current: dict[tuple[str, ...], dict[str, Any]] = {}
    if target.is_file():
        for line in target.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                current[_key(r, keys)] = r
    for row in rows:
        clean = _clean(ds, row)
        current[_key(clean, keys)] = clean
    tmp = target.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for k in sorted(current):
            fh.write(json.dumps(current[k], ensure_ascii=False, sort_keys=True) + "\n")
    tmp.chmod(0o600)
    tmp.replace(target)
    return len(current)


# ------------------------------------------------------------------------------------------------ read


def _read_folder(folder: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in sorted(folder.rglob("*.jsonl")):
        rows += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def _hf_download(repo_id: str, token: str, local_dir: Path) -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id, repo_type="dataset", token=token, local_dir=str(local_dir), allow_patterns=["*.jsonl"]
    )


_hub_read_at: dict[str, float] = {}


def read_rows(lake: Path, ds: Dataset, *, downloader: Downloader | None = None) -> PrivateRows | None:
    """The source's private rows: local area first, then its private hub dataset; None when neither exists."""
    require_private(ds)
    local = area(lake, ds.id)
    if local.is_dir() and any(local.glob("*.jsonl")):
        return PrivateRows(_read_folder(local), "local")
    repo = hf_repo(ds.id)
    token = os.environ.get(HF_TOKEN_ENV, "").strip()
    if repo is None or not token:
        return None
    cache = hub_cache(lake, ds.id)
    fresh = time.monotonic() - _hub_read_at.get(repo, -HUB_TTL_S) < HUB_TTL_S
    if not (fresh and cache.is_dir()):
        try:
            (downloader or _hf_download)(repo, token, cache)
        except Exception:  # noqa: BLE001 - gone, unreachable or not yet created: fall back to live links
            return None
        _hub_read_at[repo] = time.monotonic()
    rows = _read_folder(cache) if cache.is_dir() else []
    return PrivateRows(rows, "hub") if rows else None


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
