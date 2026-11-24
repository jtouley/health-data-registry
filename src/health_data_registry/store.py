"""The snapshot lake: raw bytes kept hash-addressed, one normalised snapshot per dataset version.

Layout under the lake root ($HDR_LAKE, default ./lake):

    raw/<sha256>                         every byte fetched from a publisher, never rewritten (ADR 0005)
    datasets/<id>/<snapshot_id>/
        data.parquet                     the normalised table, for publishing
        data.duckdb                      the same table, attached read-only by the server
        manifest.json                    provenance: URLs, retrieved_at, sha256s, contract version
    datasets/<id>/LATEST                 the snapshot_id the server reads

Normalisation is driven only by the contract: rename each publisher field to its column, turn the
codebook's missing codes into NULL, cast to the declared type. Nothing else changes a value.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
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

    @staticmethod
    def read(path: Path) -> Manifest:
        return Manifest(**json.loads(path.read_text(encoding="utf-8")))


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
        else:
            expr = f"CAST(NULLIF(trim({src}), '') AS {c.physical_type})"
        parts.append(f"{expr} AS {_q(c.name)}")
    return ",\n  ".join(parts)


def write_raw(lake: Path, fetches: Sequence[RawFetch]) -> None:
    raw = lake / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for f in fetches:
        if hashlib.sha256(f.content).hexdigest() != f.sha256:
            raise ValueError(f"sha256 mismatch for {f.url}")
        target = raw / f.sha256
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
) -> Manifest:
    """Normalise newline-delimited JSON record files (publisher field names, raw values) into a snapshot."""
    if not fetches:
        raise ValueError("a snapshot needs at least one fetch")
    write_raw(lake, fetches)
    retrieved = max(f.retrieved_at for f in fetches).astimezone(UTC)
    digest = hashlib.sha256("".join(f.sha256 for f in fetches).encode()).hexdigest()[:12]
    snapshot_id = f"{retrieved:%Y%m%dT%H%M%SZ}-{digest}"
    out = lake / "datasets" / ds.id / snapshot_id
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
        )
        (tmp / "manifest.json").write_text(json.dumps(asdict(manifest), indent=2) + "\n", encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            shutil.rmtree(out)
        tmp.replace(out)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    latest = out.parent / "LATEST"
    latest.write_text(snapshot_id + "\n", encoding="utf-8")
    return manifest


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
    path = base / sid
    return path, Manifest.read(path / "manifest.json")


def write_ndjson(path: Path, records: Sequence[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps({k: (None if v is None else str(v)) for k, v in r.items()}) + "\n")
