"""The snapshot lake: raw bytes kept hash-addressed, one normalised snapshot per dataset version.

Layout under the lake root ($HDR_LAKE, default ./lake), partitioned by source and run date:

    raw/<sha256>                         every byte fetched from a publisher, never rewritten (ADR 0005)
    datasets/<id>/run_date=<YYYY-MM-DD>/<snapshot_id>/
        data.parquet                     the normalised table, for publishing
        data.duckdb                      the same table, attached read-only by the server
        manifest.json                    provenance: URLs, retrieved_at, sha256s, contract version
    datasets/<id>/LATEST                 the snapshot_id the server reads
    fetch_log/source=<id>/run_date=<YYYY-MM-DD>.jsonl
                                         one row per fetch attempt, ok or error: URL, time, status, bytes,
                                         sha256 and the error in plain words

A snapshot directory is never overwritten: its id is the fetch time plus a hash of the raw bytes and the
contract and semantic versions, so the same id always means the same inputs. Snapshots written before the
run-date partition (datasets/<id>/<snapshot_id>/) are still read.

Normalisation is driven only by the contract: rename each publisher field to its column, turn the
codebook's missing codes into NULL, cast to the declared type. Nothing else changes a value. Flag columns
are never missing-coded (the registry refuses it): the publisher's flags are kept as published.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from health_data_registry.registry import Contract, Dataset

_SNAPSHOT_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{12}$")


@dataclass(frozen=True)
class RawFetch:
    """One publisher response, as fetched."""

    url: str
    content: bytes
    sha256: str
    retrieved_at: datetime


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
    ) -> dict[str, Any]:
        row = {
            "source": self.source,
            "url": url,
            "attempted_at": when.astimezone(UTC).isoformat(),
            "status": "ok" if ok else "error",
            "http_status": http_status,
            "bytes": size,
            "sha256": sha256,
            "error": None if ok else (error or "the fetch failed for an unknown reason"),
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


def lake_root() -> Path:
    return Path(os.environ.get("HDR_LAKE", "lake")).resolve()


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def select_list(contract: Contract) -> str:
    """The contract-driven projection from raw publisher fields (all read as text) to typed columns."""
    parts = []
    for c in contract.columns:
        src = _q(c.source_field)
        if c.missing_codes:
            codes = ", ".join(_lit(m) for m in c.missing_codes)
            src = f"CASE WHEN trim({src}) IN ({codes}) THEN NULL ELSE {src} END"
        if c.physical_type == "BOOLEAN":
            expr = f"CASE lower(trim({src})) WHEN 'true' THEN true WHEN 'false' THEN false ELSE NULL END"
        elif c.physical_type == "VARCHAR":
            expr = src
        elif c.physical_type in ("DATE", "TIMESTAMP"):
            # Socrata floating timestamps look like 2024-01-06T00:00:00.000
            expr = f"CAST(CAST(NULLIF(trim({src}), '') AS TIMESTAMP) AS {c.physical_type})"
        elif c.physical_type in ("BIGINT", "INTEGER"):
            # Counts arrive as text, "31" or "31.0" (NNDSS from 2025). A plain cast would round "1.5" to 2, so
            # anything with a fraction stops the load instead of becoming a different count.
            dec = f"CAST(NULLIF(trim({src}), '') AS DECIMAL(38, 6))"
            msg = _lit(f"{c.name}: a value has a fraction, so it isn't a whole count; check the contract")
            expr = f"CASE WHEN {dec} IS NULL THEN NULL WHEN {dec} = trunc({dec}) THEN CAST({dec} AS {c.physical_type}) ELSE error({msg}) END"
        else:
            expr = f"CAST(NULLIF(trim({src}), '') AS {c.physical_type})"
        parts.append(f"{expr} AS {_q(c.name)}")
    return ",\n  ".join(parts)


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
        _point_latest(lake, ds.id, snapshot_id)
        return Manifest.read(out / "manifest.json")
    tmp = Path(tempfile.mkdtemp(prefix=f".{ds.id}-", dir=lake))
    try:
        sources = sorted({c.source_field for c in ds.contract.columns})
        cols = "{" + ", ".join(f"{_lit(s)}: 'VARCHAR'" for s in sources) + "}"
        files = "[" + ", ".join(_lit(str(p)) for p in records_files) + "]"
        con = duckdb.connect(str(tmp / "data.duckdb"))
        try:
            con.execute(
                f"CREATE TABLE {_q(ds.id)} AS SELECT\n  {select_list(ds.contract)}\n"
                f"FROM read_json({files}, format='newline_delimited', columns={cols})"
            )
            row = con.execute(f"SELECT count(*) FROM {_q(ds.id)}").fetchone()  # noqa: S608 - quoted contract identifiers, bound values
            n = int(row[0]) if row else 0
            _check_required(con, ds)
            con.execute(f"COPY {_q(ds.id)} TO {_lit(str(tmp / 'data.parquet'))} (FORMAT parquet)")
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
            row_count=n,
            licence=ds.entry.licence.id,
            redistribution=ds.entry.licence.redistribution,
            partial=partial,
            notes=list(notes),
        )
        (tmp / "manifest.json").write_text(json.dumps(asdict(manifest), indent=2) + "\n", encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(out)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    _point_latest(lake, ds.id, snapshot_id)
    return manifest


def _point_latest(lake: Path, dataset_id: str, snapshot_id: str) -> None:
    """Swap LATEST in one step (write a temporary file, then os.replace), so a crash never leaves it empty."""
    latest = lake / "datasets" / dataset_id / "LATEST"
    tmp = latest.with_name(f".LATEST.{os.getpid()}.tmp")
    tmp.write_text(snapshot_id + "\n", encoding="utf-8")
    os.replace(tmp, latest)


def _check_required(con: duckdb.DuckDBPyConnection, ds: Dataset) -> None:
    for c in ds.contract.columns:
        if c.required:
            row = con.execute(f"SELECT count(*) FROM {_q(ds.id)} WHERE {_q(c.name)} IS NULL").fetchone()  # noqa: S608 - quoted contract identifiers, bound values
            nulls = int(row[0]) if row else 0
            if nulls:
                raise ValueError(
                    f"{ds.id}: required column {c.name} has {nulls} empty values; check the contract"
                )


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
