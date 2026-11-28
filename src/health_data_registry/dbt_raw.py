"""The dbt-duckdb plugin that hands dbt each stored source's latest full load (transform/profiles.yml).

Each staging model reads `source('raw', <id>)`. When dbt builds, this plugin resolves that source to one file: the
load the dataset's LATEST pointer names (datasets/<id>/LATEST, the pointer the MCP server reads too), kept at
raw_records/<id>/<snapshot id>.ndjson. Every field comes as published text. Every row also carries its snapshot
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
    columns = "{" + ", ".join(f"{_lit(f)}: 'VARCHAR'" for f in fields) + "}"
    select = ", ".join([*(_q(f) for f in fields), f"{_lit(sid)} AS {_q(SNAPSHOT_COLUMN)}"])
    sha = " ".join(manifest.raw_sha256)
    return (
        f"SELECT {select}, {_lit(sha)} AS {_q(SHA256_COLUMN)} "  # noqa: S608 - quoted identifiers and literals
        f"FROM read_json({_lit(str(path))}, format='newline_delimited', columns={columns})",
        None,
    )


class Plugin(BasePlugin):
    """Resolves `source('raw', <id>)` to the load LATEST names (see the module docstring)."""

    def initialize(self, plugin_config: dict[str, Any]) -> None:
        self.lake = Path(str(plugin_config.get("lake") or "../lake"))
        self._con: duckdb.DuckDBPyConnection | None = None

    def configure_connection(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._con = conn  # the relation load() returns must read the database dbt writes to
        # Made once here: dbt loads sources on parallel threads, and two of them creating it at once conflict.
        conn.execute("CREATE SCHEMA IF NOT EXISTS raw")

    def load(self, source_config: SourceConfig) -> duckdb.DuckDBPyRelation:
        if self._con is None:
            raise RuntimeError("the raw-records plugin has no DuckDB connection yet")
        fields = [str(f) for f in source_config.meta.get("source_fields") or []]
        sql, warning = load_sql(self.lake, source_config.identifier, fields)
        if warning:
            print(f"WARNING: {warning}", file=sys.stderr)
        return self._con.cursor().sql(sql)
