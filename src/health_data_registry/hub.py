"""The hosted read path: published hub releases and the run-time cache for restricted sources.

Where the server reads each source from (query.Engine decides, in this order):

1. The local lake ($HDR_LAKE), when it holds a snapshot: maintainers and CI, after `refresh`.
2. A redistributable source: the published release on the open data hub ($HDR_HUB, hf://datasets/<org>/<name>,
   read at an immutable release: pinned with @release-<run date>, or else the newest release tag; a branch such
   as main is refused; a local directory stands in for the Hub in tests). Only the Parquet file a question needs
   is downloaded, into a bounded local cache ($HDR_CACHE_MAX_GB, default 5, least recently used evicted), and it
   is checked against the release manifest's sha256 before use. A release built with another contract version
   than the installed contract is refused (fail closed). No token is needed for a public dataset.
3. A restricted source (licence.redistribution false, e.g. WHO GHO): served live by live.py, fetched from the
   publisher per request, held in memory only for a few minutes, and never written to disk.

The cache lives at $HDR_CACHE (default ~/.cache/health-data-registry). Clients keep only normalised tables
there; the publishers' raw bytes stay in the maintainers' lake.

What the sha256 check proves: that the downloaded file is the one the release manifest names. It does not prove
that the manifest, the hub account or the publisher is trustworthy. Release integrity and publisher provenance
are separate controls; signed manifests (e.g. Sigstore) and build attestations (SLSA provenance) for each
release would add them, and are not in place yet.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from health_data_registry.publish import HF_PREFIX, data_path, hf_repo_id, sha256_file
from health_data_registry.registry import Dataset
from health_data_registry.store import Manifest

# The project's public hub. Until its first release-<run date> tag exists, reading from it is refused plainly.
DEFAULT_HUB: str | None = "hf://datasets/jtouley/health-data-registry"
# HDR_HUB=off reads no hub at all (tests, and maintainers working only from their own lake).
HUB_OFF = frozenset({"off", "none", "0"})


class HubError(ValueError):
    """The hub release can't be used for this source; the message says why in plain words."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def cache_root() -> Path:
    env = os.environ.get("HDR_CACHE")
    return Path(env).expanduser().resolve() if env else Path.home() / ".cache" / "health-data-registry"


def hub_from_env() -> str | None:
    env = os.environ.get("HDR_HUB", "").strip()
    if env.casefold() in HUB_OFF:
        return None
    return env or DEFAULT_HUB


RELEASE_TAG = re.compile(r"^release-\d{4}-\d{2}-\d{2}(\.\d+)?$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

# Releases whose contract version an installed contract can read safely, by dataset:
# {dataset_id: {release_contract_version: {installed_contract_versions}}}. Empty on purpose: until a maintainer
# reviews a pair and adds it here, any mismatch fails closed.
COMPATIBLE_CONTRACTS: dict[str, dict[str, set[str]]] = {}

DEFAULT_CACHE_MAX_BYTES = 5 * 1024**3


def cache_max_bytes() -> int:
    env = os.environ.get("HDR_CACHE_MAX_GB")
    return int(float(env) * 1024**3) if env else DEFAULT_CACHE_MAX_BYTES


def is_immutable_ref(rev: str) -> bool:
    """A release tag (release-<run date>[.<n>]) or a full commit id; never a branch such as main."""
    return bool(RELEASE_TAG.fullmatch(rev) or _COMMIT.fullmatch(rev))


def _release_order(tag: str) -> tuple[str, int]:
    date, _, n = tag.removeprefix("release-").partition(".")
    return date, int(n or 1)


def newest_release(tags: list[str]) -> str | None:
    """The newest release tag: latest run date, then the latest correction of that date."""
    found = [t for t in tags if RELEASE_TAG.fullmatch(t)]
    return max(found, key=_release_order) if found else None


def contract_compatible(dataset_id: str, release_version: str, installed_version: str) -> bool:
    if release_version == installed_version:
        return True
    return installed_version in COMPATIBLE_CONTRACTS.get(dataset_id, {}).get(release_version, set())


def _split(url: str) -> tuple[str, str | None]:
    """hf://datasets/<org>/<name>[@rev] -> (<org>/<name>, rev)."""
    repo = hf_repo_id(url)
    rest = url.removeprefix(HF_PREFIX).strip("/")
    rev = rest.split("@", 1)[1].split("/")[0] if "@" in rest else None
    return repo, rev


def evict(root: Path, max_bytes: int, keep: set[Path]) -> list[Path]:
    """Keep a cache under max_bytes: remove the least recently used files first, never one in `keep`."""
    files = [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []
    total = sum(p.stat().st_size for p in files)
    removed: list[Path] = []
    for p in sorted(files, key=lambda f: f.stat().st_atime_ns):
        if total <= max_bytes:
            break
        if p.resolve() in keep:
            continue
        total -= p.stat().st_size
        p.unlink()
        removed.append(p)
    return removed


@dataclass
class Hub:
    """One open data hub, read at an immutable release.

    An unpinned hub URL is resolved to its newest release-<run date> tag, never to a moving branch; a pin to
    anything but a release tag or a full commit id is refused, because answers from it could not be reproduced.
    The resolved reference is recorded in every result's provenance.
    """

    url: str
    cache: Path
    max_bytes: int = field(default_factory=cache_max_bytes)
    api: Any = field(default=None, repr=False)  # a huggingface_hub.HfApi; None builds one without a token
    _release: dict[str, Any] | None = field(default=None, repr=False)
    _rev: str | None = field(default=None, repr=False)

    @property
    def is_remote(self) -> bool:
        return self.url.startswith(HF_PREFIX)

    def revision(self) -> str:
        """The immutable reference this hub is read at (resolved once)."""
        if self._rev is not None:
            return self._rev
        if not self.is_remote:
            self._rev = str(self.release().get("tag") or "")
            if not RELEASE_TAG.fullmatch(self._rev):
                raise HubError("not_a_release", f"The hub copy at {self.url} is not a tagged release.")
            return self._rev
        repo, rev = _split(self.url)
        if rev is not None:
            if not is_immutable_ref(rev):
                raise HubError(
                    "not_reproducible",
                    f"The hub is set to '{rev}', which moves as new data arrive, so answers could not be "
                    "reproduced. Set HDR_HUB to a release (…@release-<date>) or leave the @ part off to use the "
                    "newest release.",
                )
            self._rev = rev
            return rev
        try:
            api = self.api
            if api is None:
                from huggingface_hub import HfApi

                api = HfApi(token=False)  # public datasets: no sign-in, ever
            tags = [t.name for t in api.list_repo_refs(repo, repo_type="dataset").tags]
        except Exception as e:  # noqa: BLE001 - every lookup failure becomes one plain refusal
            raise HubError(
                "hub_unreachable",
                f"The open data hub ({self.url}) could not be reached to find its newest release. Check the "
                "internet connection and try again.",
            ) from e
        newest = newest_release(tags)
        if newest is None:
            raise HubError(
                "no_release",
                f"The open data hub ({self.url}) has no published release yet, so there is no data to read from "
                "it. Nothing was guessed. Try again after the first release, or use get_provenance and "
                "list_sources to see what this server covers.",
            )
        self._rev = newest
        return newest

    def _cache_dir(self) -> Path:
        if self.is_remote:
            repo, _ = _split(self.url)
            return self.cache / "hub" / repo.replace("/", "--") / self.revision()
        return self.cache / "hub" / "local" / Path(self.url).resolve().name

    def fetch(self, path_in_repo: str) -> Path:
        """One file of the release, in the local cache (downloaded on first use; the cache stays bounded)."""
        if self.is_remote:
            from huggingface_hub import hf_hub_download

            repo, _ = _split(self.url)
            rev = self.revision()
            try:
                got = Path(
                    hf_hub_download(
                        repo,
                        path_in_repo,
                        repo_type="dataset",
                        revision=rev,
                        local_dir=str(self._cache_dir()),
                        token=False,  # public datasets: no sign-in, ever
                    )
                )
            except Exception as e:  # noqa: BLE001 - every download failure becomes one plain refusal
                raise HubError(
                    "hub_unreachable",
                    f"The open data hub ({self.url}) could not be reached to download {path_in_repo}. Check the "
                    "internet connection and try again; nothing was changed on this computer.",
                ) from e
        else:
            src = Path(self.url) / path_in_repo
            if not src.is_file():
                raise HubError(
                    "hub_missing_file", f"The hub release at {self.url} has no file {path_in_repo}."
                )
            local_root = self.cache / "hub" / "local" / Path(self.url).resolve().name
            got = local_root / path_in_repo
            got.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, got)
        os.utime(got)  # most recently used
        evict(self.cache / "hub", self.max_bytes, keep={got.resolve()})
        return got

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
        """Download (or reuse) one source's Parquet file, verify it, and describe it as a Manifest.

        Fails closed: a release built with another contract version than the installed one is refused unless
        COMPATIBLE_CONTRACTS lists the pair, and a file whose sha256 differs from the manifest is never read.
        """
        ref = self.revision()
        rel = self.release()
        sm = (rel.get("sources") or {}).get(ds.id)
        if not sm or not sm.get("hosted") or not sm.get("snapshot"):
            raise HubError(
                "not_on_hub",
                f"{ds.entry.title} is not hosted in the open data hub release {rel.get('release_id', '')}.",
            )
        release_cv = str((sm.get("contract") or {}).get("version", ""))
        if not contract_compatible(ds.id, release_cv, ds.contract.version):
            raise HubError(
                "contract_mismatch",
                f"The hub release {ref} was built with version {release_cv} of the {ds.id} contract, but this "
                f"server reads version {ds.contract.version}, so the columns may not mean the same thing. No "
                "numbers were read. Install the newer server, or point HDR_HUB at a release built with the "
                "same contract version.",
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
        allowed = set(Manifest.__dataclass_fields__)
        snap = {k: v for k, v in dict(sm["snapshot"]).items() if k in allowed}
        files = {f"{ds.id}/{k}": str(v["sha256"]) for k, v in (sm.get("files") or {}).items()}
        m = Manifest(
            **{
                **snap,
                "origin": "hub",
                "release_id": str(rel.get("release_id", "")),
                "release_ref": ref,
                "run_date": str(rel.get("run_date", "")),
                "release_contract_version": release_cv,
                "release_files": files,
                "notes": [],
                "quality": dict(sm.get("quality") or {}),
            }
        )
        return local, m
