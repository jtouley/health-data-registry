"""The hosted read path: published hub releases and the run-time cache for restricted sources.

Where the server reads each source from (query.Engine decides, in this order):

1. The local lake ($HDR_LAKE), when it holds a snapshot: maintainers and CI, after `refresh`.
2. A redistributable source: the published release on the open data hub ($HDR_HUB, hf://datasets/<org>/<name>,
   optionally pinned with @release-<run date>; a local directory stands in for the Hub in tests). Only the
   Parquet file a question needs is downloaded, into the local cache, and it is checked against the release
   manifest's sha256 before use. No token is needed for a public dataset.
3. A restricted source (licence.redistribution false, e.g. WHO GHO): fetched from the publisher at run time,
   the first time it is asked about, into the local cache, with the same contract normalisation.

The cache lives at $HDR_CACHE (default ~/.cache/health-data-registry).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from health_data_registry.publish import HF_PREFIX, data_path, hf_repo_id, sha256_file
from health_data_registry.registry import Dataset
from health_data_registry.store import Manifest

# The project's public hub, once the maintainer has created it; until then set HDR_HUB.
DEFAULT_HUB: str | None = None


class HubError(ValueError):
    """The hub release can't be used for this source; the message says why in plain words."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def cache_root() -> Path:
    env = os.environ.get("HDR_CACHE")
    return Path(env).expanduser().resolve() if env else Path.home() / ".cache" / "health-data-registry"


def hub_from_env() -> str | None:
    return os.environ.get("HDR_HUB") or DEFAULT_HUB


def _split(url: str) -> tuple[str, str | None]:
    """hf://datasets/<org>/<name>[@rev] -> (<org>/<name>, rev)."""
    repo = hf_repo_id(url)
    rest = url.removeprefix(HF_PREFIX).strip("/")
    rev = rest.split("@", 1)[1].split("/")[0] if "@" in rest else None
    return repo, rev


@dataclass
class Hub:
    url: str
    cache: Path
    _release: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def is_remote(self) -> bool:
        return self.url.startswith(HF_PREFIX)

    def _cache_dir(self) -> Path:
        if self.is_remote:
            repo, rev = _split(self.url)
            return self.cache / "hub" / repo.replace("/", "--") / (rev or "main")
        return self.cache / "hub" / "local" / Path(self.url).resolve().name

    def fetch(self, path_in_repo: str) -> Path:
        """One file of the release, in the local cache (downloaded on first use)."""
        target = self._cache_dir() / path_in_repo
        if self.is_remote:
            from huggingface_hub import hf_hub_download

            repo, rev = _split(self.url)
            try:
                got = hf_hub_download(
                    repo,
                    path_in_repo,
                    repo_type="dataset",
                    revision=rev,
                    local_dir=str(self._cache_dir()),
                )
            except Exception as e:  # noqa: BLE001 - every download failure becomes one plain refusal
                raise HubError(
                    "hub_unreachable",
                    f"The open data hub ({self.url}) could not be reached to download {path_in_repo}. Check the "
                    "internet connection and try again; nothing was changed on this computer.",
                ) from e
            return Path(got)
        src = Path(self.url) / path_in_repo
        if not src.is_file():
            raise HubError("hub_missing_file", f"The hub release at {self.url} has no file {path_in_repo}.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        return target

    def release(self) -> dict[str, Any]:
        if self._release is None:
            path = self.fetch("manifest.json")
            try:
                self._release = dict(json.loads(path.read_text(encoding="utf-8")))
            except ValueError as e:
                raise HubError(
                    "hub_bad_manifest", f"The hub release at {self.url} has a damaged manifest.json."
                ) from e
        return self._release

    def snapshot(self, ds: Dataset) -> tuple[Path, Manifest]:
        """Download (or reuse) one source's Parquet file, verify it, and describe it as a Manifest."""
        rel = self.release()
        sm = (rel.get("sources") or {}).get(ds.id)
        if not sm or not sm.get("hosted") or not sm.get("snapshot"):
            raise HubError(
                "not_on_hub",
                f"{ds.entry.title} is not hosted in the open data hub release {rel.get('release_id', '')}.",
            )
        path = data_path(ds.id)
        meta = (rel.get("files") or {}).get(path)
        if not meta:
            raise HubError("hub_bad_manifest", f"The hub release's manifest does not list {path}.")
        local = self.fetch(path)
        if sha256_file(local) != meta["sha256"]:
            local.unlink(missing_ok=True)
            raise HubError(
                "hub_hash_mismatch",
                f"The downloaded copy of {ds.entry.title} does not match the release's fingerprint (sha256), so "
                "it was not used. It may have been damaged in transit; ask again to download it afresh, and "
                "report it if it keeps happening.",
            )
        snap = dict(sm["snapshot"])
        allowed = set(Manifest.__dataclass_fields__)
        m = Manifest(**{k: v for k, v in snap.items() if k in allowed})
        release_cv = str((sm.get("contract") or {}).get("version", ""))
        notes = list(m.notes)
        if release_cv != ds.contract.version:
            notes.append(
                f"The hub release was built with contract version {release_cv} of {ds.id}, but this server reads "
                f"version {ds.contract.version}. Column meanings may differ: update the server, or pin the hub "
                "to a release with the same contract version."
            )
        files = {f"{ds.id}/{k}": str(v["sha256"]) for k, v in (sm.get("files") or {}).items()}
        m = Manifest(
            **{
                **{k: getattr(m, k) for k in allowed},
                "origin": "hub",
                "release_id": str(rel.get("release_id", "")),
                "run_date": str(rel.get("run_date", "")),
                "release_contract_version": release_cv,
                "release_files": files,
                "notes": notes,
            }
        )
        return local, m
