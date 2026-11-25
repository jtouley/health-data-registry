"""The snapshot lake: raw bytes kept hash-addressed, one normalised snapshot per dataset version.

Layout under the lake root, partitioned by source and run date. The root is $HDR_LAKE when it is set to a full
path, else a fixed per-user data folder (default_lake): never the folder the tool happens to be started in, so a
lake/ planted in a folder a clinician opens is never read (issue #74).

    raw/<sha256>                         every byte fetched from a publisher, never rewritten (ADR 0005)
    datasets/<id>/run_date=<YYYY-MM-DD>/<snapshot_id>/
        data.parquet                     the normalised table, for publishing
        data.duckdb                      the same table, attached read-only by the server
        manifest.json                    provenance: URLs, retrieved_at, sha256s (raw bytes and the two data
                                         files), contract version
    datasets/<id>/LATEST                 the snapshot_id the server reads
    raw_records/<id>/<snapshot_id>.ndjson
                                         the parsed publisher records (every value as published text) that the
                                         dbt staging models in transform/ read; full loads only, never partial
    fetch_log/source=<id>/run_date=<YYYY-MM-DD>.jsonl
                                         one row per fetch attempt, ok or error: URL, time, status, bytes,
                                         sha256 and the error in plain words

A snapshot directory is never overwritten: its id is the fetch time plus a hash of the raw bytes and the
contract and semantic versions, so the same id always means the same inputs. Snapshots written before the
run-date partition (datasets/<id>/<snapshot_id>/) are still read.

The snapshot table is built by the one generic, contract-driven staging step (staging.py), the same
projection the dbt staging models are generated from: rename each publisher field to its column, turn the
codebook's missing codes into NULL, cast to the declared type. Nothing else changes a value. Flag columns are never missing-coded (the registry refuses it): the publisher's flags are kept as
published.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from health_data_registry.registry import Dataset
from health_data_registry.staging import stage, write_parquet

_SNAPSHOT_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{12}$")
APP_NAME = "health-data-registry"
# The data files of a snapshot whose sha256 the manifest records and every read checks.
SNAPSHOT_FILES = ("data.duckdb", "data.parquet")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawFetch:
    """One publisher response, as fetched."""

    url: str  # the catalogued address the bytes answer (for a dashboard, the view, never a one-time session URL)
    content: bytes
    sha256: str
    retrieved_at: datetime
    # Where the bytes were actually captured when that differs from ``url`` (a dashboard's data request); kept in
    # the fetch log beside the source URL, never cited.
    capture_url: str | None = None


@dataclass(frozen=True)
class Manifest:
    dataset: str
    snapshot_id: str
    contract_version: str
    semantic_version: str
    catalog_status: str
    retrieved_at: str
    source_urls: list[str]
    raw_sha256: list[str]
    row_count: int
    licence: str
    redistribution: bool
    partial: bool
    # Where the server read this snapshot from: "lake" (a maintainer's local lake), "hub" (a published release
    # on the open data hub) or "publisher" (fetched from the publisher at run time into the local cache).
    origin: str = "lake"
    release_id: str | None = None
    release_ref: str | None = None  # the immutable hub reference read (release-<run date> tag or commit)
    run_date: str | None = None
    release_contract_version: str | None = None
    release_files: dict[str, str] = field(default_factory=dict)  # path in the release -> sha256
    notes: list[str] = field(default_factory=list)  # plain-words notes shown with every answer
    quality: dict[str, Any] = field(
        default_factory=dict
    )  # the release's data-test results (publish.quality_section)
    # The staging run: layout version, rows, and codes the contract doesn't list (staging.py).
    silver: dict[str, Any] = field(default_factory=dict)
    # Dashboard sources: the sha256 of each worksheet's column list read ("<view>/<worksheet>" -> sha256), so a
    # redesign is visible in provenance as well as stopping the load.
    layouts: dict[str, str] = field(default_factory=dict)
    # Lake snapshots: the sha256 of each data file (SNAPSHOT_FILES). The server checks data.duckdb when it connects;
    # export-public checks both before publishing (hub.verify_lake_snapshot).
    files: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def read(path: Path) -> Manifest:
        return Manifest(**json.loads(path.read_text(encoding="utf-8")))


def run_date_of(snapshot_id: str) -> str:
    """The run date partition of a snapshot id (20261124T120000Z-... -> 2026-11-24)."""
    return f"{snapshot_id[0:4]}-{snapshot_id[4:6]}-{snapshot_id[6:8]}"


def snapshot_dir(lake: Path, dataset_id: str, snapshot_id: str) -> Path:
    """Where a snapshot lives: datasets/<id>/run_date=<date>/<snapshot_id>, or the older unpartitioned path."""
    base = lake / "datasets" / dataset_id
    new = base / f"run_date={run_date_of(snapshot_id)}" / snapshot_id
    old = base / snapshot_id
    return old if old.is_dir() and not new.is_dir() else new


class FetchLog:
    """Append-only log of fetch attempts: fetch_log/source=<id>/run_date=<YYYY-MM-DD>.jsonl, one row each."""

    def __init__(self, lake: Path, source: str) -> None:
        self.lake = lake
        self.source = source

    def path(self, when: datetime) -> Path:
        return (
            self.lake
            / "fetch_log"
            / f"source={self.source}"
            / f"run_date={when.astimezone(UTC):%Y-%m-%d}.jsonl"
        )

    def record(
        self,
        url: str,
        *,
        ok: bool,
        when: datetime,
        http_status: int | None = None,
        size: int | None = None,
        sha256: str | None = None,
        error: str | None = None,
        note: str | None = None,
        capture_url: str | None = None,
    ) -> dict[str, Any]:
        """Append one attempt. ``note`` records an access-policy decision on it (e.g. a robots.txt override)."""
        row = {
            "source": self.source,
            "url": url,
            "attempted_at": when.astimezone(UTC).isoformat(),
            "status": "ok" if ok else "error",
            "http_status": http_status,
            "bytes": size,
            "sha256": sha256,
            "error": None if ok else (error or "the fetch failed for an unknown reason"),
            **({"note": note} if note else {}),
            **({"capture_url": capture_url} if capture_url else {}),
        }
        target = self.path(when)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return row


def read_fetch_log(lake: Path, source: str) -> list[dict[str, Any]]:
    """Every logged fetch attempt for one source, oldest run date first."""
    rows: list[dict[str, Any]] = []
    for p in sorted((lake / "fetch_log" / f"source={source}").glob("run_date=*.jsonl")):
        rows += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def default_lake() -> Path:
    """The per-user data folder the lake lives in when HDR_LAKE is not set (platformdirs' user_data_dir layout).

    Linux and other Unix: $XDG_DATA_HOME/health-data-registry/lake (when $XDG_DATA_HOME is a full path), else
    ~/.local/share/health-data-registry/lake. macOS: ~/Library/Application Support/health-data-registry/lake.
    Windows: %LOCALAPPDATA%\\health-data-registry\\lake.
    """
    home = Path.home()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        base = Path(local) if local and Path(local).is_absolute() else home / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "")
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".local" / "share"
    return base / APP_NAME / "lake"


def lake_root() -> Path:
    """$HDR_LAKE when it is a full path, else default_lake(); never a path relative to the working folder.

    A relative HDR_LAKE (such as "lake") would read whatever folder the tool is started in, so it is ignored with
    a warning in the log, as other mistyped settings are, and the per-user folder is used instead.
    """
    env = os.environ.get("HDR_LAKE", "").strip()
    if env:
        path = Path(env).expanduser()
        if path.is_absolute():
            return path.resolve()
        logger.warning("HDR_LAKE=%r is not a full path; using %s instead", env, default_lake())
    return default_lake()


def sha256_file(path: Path) -> str:
    # hashlib.file_digest reads in chunks itself; a hand-written read loop gave mutmut a mutant that never ends.
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_raw(lake: Path, fetches: Sequence[RawFetch]) -> None:
    raw = lake / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for f in fetches:
        target = raw / f.sha256
        if target.exists() and not f.content:
            continue  # already stored when the page arrived (refresh streams pages to disk)
        if hashlib.sha256(f.content).hexdigest() != f.sha256:
            raise ValueError(f"sha256 mismatch for {f.url}")
        if not target.exists():
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(f.content)
            tmp.replace(target)


def build_snapshot(
    lake: Path,
    ds: Dataset,
    fetches: Sequence[RawFetch],
    records_files: Sequence[Path],
    *,
    partial: bool = False,
    layouts: dict[str, str] | None = None,
    notes: Sequence[str] = (),
) -> Manifest:
    """Normalise newline-delimited JSON record files (publisher field names, raw values) into a snapshot."""
    if not fetches:
        raise ValueError("a snapshot needs at least one fetch")
    write_raw(lake, fetches)
    retrieved = max(f.retrieved_at for f in fetches).astimezone(UTC)
    inputs = "".join(f.sha256 for f in fetches) + f"|{ds.contract.version}|{ds.semantic.version}|{partial}"
    digest = hashlib.sha256(inputs.encode()).hexdigest()[:12]
    snapshot_id = f"{retrieved:%Y%m%dT%H%M%SZ}-{digest}"
    out = snapshot_dir(lake, ds.id, snapshot_id)
    if (out / "manifest.json").is_file():  # the same inputs were snapshotted already: never overwrite
        if not partial and ds.entry.licence.redistribution:
            keep_raw_records(
                lake, ds, snapshot_id, records_files
            )  # a lake from before raw_records gets them now
        _point_latest(lake, ds.id, snapshot_id)
        return Manifest.read(out / "manifest.json")
    tmp = Path(tempfile.mkdtemp(prefix=f".{ds.id}-", dir=lake))
    try:
        sources = sorted({c.source_field for c in ds.contract.columns})
        cols = "{" + ", ".join(f"{_lit(s)}: 'VARCHAR'" for s in sources) + "}"
        files = "[" + ", ".join(_lit(str(p)) for p in records_files) + "]"
        con = duckdb.connect(str(tmp / "data.duckdb"))
        try:
            raw = f"read_json({files}, format='newline_delimited', columns={cols})"
            # A full load must carry every declared field: read_json fills a missing one with NULL, so a renamed
            # publisher field would otherwise load as an all-empty column. A partial sample is not checked.
            report = stage(
                con, ds.contract, raw, table=ds.id, present=None if partial else fields_in(con, files)
            )
            write_parquet(con, ds.contract, ds.id, str(tmp / "data.parquet"))
        finally:
            con.close()
        manifest = Manifest(
            dataset=ds.id,
            snapshot_id=snapshot_id,
            contract_version=ds.contract.version,
            semantic_version=ds.semantic.version,
            catalog_status=ds.entry.status,
            retrieved_at=retrieved.isoformat(),
            source_urls=[f.url for f in fetches],
            raw_sha256=[f.sha256 for f in fetches],
            row_count=report.row_count,
            licence=ds.entry.licence.id,
            redistribution=ds.entry.licence.redistribution,
            partial=partial,
            layouts=dict(layouts or {}),
            notes=[*notes, *report.notes()],
            silver=report.as_dict(),
            files={name: sha256_file(tmp / name) for name in SNAPSHOT_FILES},
        )
        (tmp / "manifest.json").write_text(json.dumps(asdict(manifest), indent=2) + "\n", encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(out)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    if not partial and ds.entry.licence.redistribution:
        keep_raw_records(lake, ds, snapshot_id, records_files)
    _point_latest(lake, ds.id, snapshot_id)
    return manifest


def fields_in(con: duckdb.DuckDBPyConnection, files: str) -> set[str] | None:
    """Every field name in a load's records (`files` is a SQL list of paths), or None when it has no records."""
    rows = con.execute(
        f"SELECT DISTINCT unnest(json_keys(json)) FROM read_ndjson_objects({files})"  # noqa: S608 - quoted paths
    ).fetchall()
    return {str(r[0]) for r in rows} or None


def keep_raw_records(lake: Path, ds: Dataset, snapshot_id: str, records_files: Sequence[Path]) -> Path:
    """Keep one full load's parsed records where the dbt staging models read them; written once, never rewritten.

    Refuses a source whose licence forbids keeping a copy (ADR 0009), whatever the caller checked. A failed copy
    leaves no temporary file behind.
    """
    if not ds.entry.licence.redistribution:
        raise ValueError(
            f"{ds.id}: its licence forbids keeping a copy, so its records are never kept on disk"
        )
    target = lake / "raw_records" / ds.id / f"{snapshot_id}.ndjson"
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as out:
            for p in records_files:
                with p.open("rb") as fh:
                    shutil.copyfileobj(fh, out)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def _point_latest(lake: Path, dataset_id: str, snapshot_id: str) -> None:
    """Swap LATEST in one step (write a temporary file, then os.replace), so a crash never leaves it empty."""
    latest = lake / "datasets" / dataset_id / "LATEST"
    tmp = latest.with_name(f".LATEST.{os.getpid()}.tmp")
    tmp.write_text(snapshot_id + "\n", encoding="utf-8")
    os.replace(tmp, latest)


def latest_snapshot(lake: Path, dataset_id: str) -> tuple[Path, Manifest] | None:
    base = lake / "datasets" / dataset_id
    pointer = base / "LATEST"
    if not pointer.is_file():
        return None
    sid = pointer.read_text(encoding="utf-8").strip()
    if not _SNAPSHOT_ID.fullmatch(sid):
        raise ValueError(f"{pointer}: not a snapshot id")
    path = snapshot_dir(lake, dataset_id, sid)
    return path, Manifest.read(path / "manifest.json")


def write_ndjson(path: Path, records: Sequence[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps({k: (None if v is None else str(v)) for k, v in r.items()}) + "\n")
