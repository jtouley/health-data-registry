"""The dbt-duckdb plugin that hands dbt each stored source's pinned full load (transform/profiles.yml).

Each staging model reads `source('raw', <id>)`. When dbt builds, this plugin resolves that source to one file, kept
at raw_records/<id>/<snapshot id>.ndjson. With a build pin (HDR_BUILD_PIN, written by `pin-build` before dbt runs;
build_manifest.py), it reads only the load the pin names, and refuses a records file whose sha256 differs from the
pinned one: dbt never resolves LATEST, so what a build read is exactly what its build manifest says. Without a pin
(a quick local run), it reads the load the dataset's LATEST pointer names (datasets/<id>/LATEST, the pointer the
MCP server reads too). Every field comes as published text. Every row also carries its snapshot
id and the sha256 of the raw bytes it was parsed from (hard rule 8), so marts and metrics built on staging can
name their source. Older loads are never read.

When the lake has no load dbt can read, the source is empty and a plain warning says why and what to run, so
`dbt build` still builds every other model. That happens when the source was never refreshed, when LATEST names a
partial sample (only full loads are kept for dbt), or when the load predates kept records.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb
from dbt.adapters.duckdb.plugins import BasePlugin
from dbt.adapters.duckdb.utils import SourceConfig

from health_data_registry.build_manifest import (
    SNAPSHOT_ID,
    BuildManifestError,
    read_pin,
    records_path,
    sha256_file,
)
from health_data_registry.store import latest_snapshot

# The provenance columns on every raw and staging row, after the publisher's fields.
SNAPSHOT_COLUMN = "_snapshot_id"
SHA256_COLUMN = "_raw_sha256"
PROVENANCE = (SNAPSHOT_COLUMN, SHA256_COLUMN)


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def empty_sql(fields: Sequence[str]) -> str:
    """No rows, with every raw field and provenance column as text."""
    cols = ", ".join(f"CAST(NULL AS VARCHAR) AS {_q(f)}" for f in [*fields, *PROVENANCE])
    return f"SELECT {cols} WHERE false"


def load_sql(lake: Path, dataset_id: str, fields: Sequence[str]) -> tuple[str, str | None]:
    """The SQL that reads the load LATEST names, and a plain warning when there is none to read."""
    model = f"stg_{dataset_id}"
    try:
        found = latest_snapshot(lake, dataset_id)
    except (OSError, ValueError) as e:
        return empty_sql(fields), (
            f"{dataset_id}: the lake's LATEST pointer or its snapshot can't be read ({e}), so {model} is empty. "
            f"Run `health-data-registry refresh {dataset_id}` to rebuild it."
        )
    if found is None:
        return empty_sql(fields), (
            f"{dataset_id} has no load in the lake at {lake} yet, so {model} is empty. "
            f"Run `health-data-registry refresh {dataset_id}` to fill it."
        )
    manifest = found[1]
    sid = manifest.snapshot_id
    if manifest.partial:
        return empty_sql(fields), (
            f"{dataset_id}: LATEST names {sid}, a partial sample, and dbt reads only full loads, so {model} is "
            f"empty. Run `health-data-registry refresh {dataset_id}` without --max-rows to fill it."
        )
    path = lake / "raw_records" / dataset_id / f"{sid}.ndjson"
    if not path.is_file():
        return empty_sql(fields), (
            f"{dataset_id}: LATEST names {sid}, whose records were not kept for dbt, so {model} is empty. "
            f"Run `health-data-registry refresh {dataset_id}` to keep them."
        )
    return read_sql(path, sid, manifest.raw_sha256, fields), None


def read_sql(path: Path, snapshot_id: str, raw_sha256: Sequence[str], fields: Sequence[str]) -> str:
    """Every field of one records file as text, with its snapshot id and raw sha256s on each row."""
    columns = "{" + ", ".join(f"{_lit(f)}: 'VARCHAR'" for f in fields) + "}"
    select = ", ".join([*(_q(f) for f in fields), f"{_lit(snapshot_id)} AS {_q(SNAPSHOT_COLUMN)}"])
    sha = " ".join(raw_sha256)
    return (
        f"SELECT {select}, {_lit(sha)} AS {_q(SHA256_COLUMN)} "  # noqa: S608 - quoted identifiers and literals
        f"FROM read_json({_lit(str(path))}, format='newline_delimited', columns={columns})"
    )


class PinnedLoadRefused(RuntimeError):
    """The pinned load is missing or no longer has the bytes the build pinned; dbt fails the source."""


def pinned_sql(
    lake: Path, dataset_id: str, fields: Sequence[str], inputs: dict[str, Any]
) -> tuple[str, str | None]:
    """The SQL that reads the load the build pin names, after checking its sha256; a plain warning when the pin
    names none. Raises PinnedLoadRefused when the source isn't pinned or its file is gone or changed."""
    model = f"stg_{dataset_id}"
    entry = inputs.get(dataset_id)
    if not isinstance(entry, dict):
        raise PinnedLoadRefused(
            f"{dataset_id} is not in the build's pin, so {model} was not built; run `health-data-registry "
            "pin-build` again before dbt"
        )
    sid, records = entry.get("snapshot_id"), entry.get("records")
    if records is None:
        what = f"snapshot {sid}, which has no full load kept for dbt" if sid else "no load"
        return empty_sql(fields), (
            f"{dataset_id}: the build's pin names {what}, so {model} is empty. Run `health-data-registry refresh "
            f"{dataset_id}` (without --max-rows), then pin the build again."
        )
    if not (isinstance(sid, str) and SNAPSHOT_ID.fullmatch(sid)):
        raise PinnedLoadRefused(f"{dataset_id}: the build's pin names {sid!r}, which is not a snapshot id")
    rel = records_path(dataset_id, sid)
    path = lake / rel
    if not path.is_file() or sha256_file(path) != records.get("sha256"):
        raise PinnedLoadRefused(
            f"{rel} is missing or no longer holds the bytes the build pinned (snapshot {sid}), so nothing was "
            "read and the build fails. Restore the file from the private raw archive, or pin the build again."
        )
    return read_sql(path, sid, entry.get("raw_sha256") or [], fields), None


class Plugin(BasePlugin):
    """Resolves `source('raw', <id>)` to the pinned load, or without a pin the one LATEST names (module docstring)."""

    def initialize(self, plugin_config: dict[str, Any]) -> None:
        self.lake = Path(str(plugin_config.get("lake") or "../lake"))
        self._con: duckdb.DuckDBPyConnection | None = None
        pin = str(plugin_config.get("pin") or "")
        self.inputs: dict[str, Any] | None = None
        if pin:
            try:
                self.inputs = read_pin(Path(pin))["inputs"]
            except BuildManifestError as e:
                raise PinnedLoadRefused(f"{e}; run `health-data-registry pin-build` before dbt") from e

    def configure_connection(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._con = conn  # the relation load() returns must read the database dbt writes to
        # Made once here: dbt loads sources on parallel threads, and two of them creating it at once conflict.
        conn.execute("CREATE SCHEMA IF NOT EXISTS raw")

    def load(self, source_config: SourceConfig) -> duckdb.DuckDBPyRelation:
        if self._con is None:
            raise RuntimeError("the raw-records plugin has no DuckDB connection yet")
        fields = [str(f) for f in source_config.meta.get("source_fields") or []]
        if self.inputs is not None:
            sql, warning = pinned_sql(self.lake, source_config.identifier, fields, self.inputs)
        else:
            sql, warning = load_sql(self.lake, source_config.identifier, fields)
        if warning:
            print(f"WARNING: {warning}", file=sys.stderr)
        return self._con.cursor().sql(sql)
