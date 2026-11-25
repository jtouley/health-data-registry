"""Contract-driven staging (silver): one generic step from raw publisher records to a typed table.

The layers follow sources -> staging -> marts -> metrics:

    raw      the publisher's bytes, kept by sha256 (store.write_raw); an adapter only fetches and parses them
             into records keyed by the publisher's own field names, every value as published text
    silver   this module: one table per dataset whose every column, type, code list and derived flag comes
             from the dataset's ODCS contract and from nothing else. There is no staging code per source.
    metrics  semantic/<id>.yaml, compiled by query.py over silver

What staging applies, and the contract property each comes from:

    column names           schema.properties[].name, read from customProperties.sourceField
    logical, physical type  logicalType and physicalType (a count with a fraction stops the load)
    missing-value codes    customProperties.missingCodes become NULL (never on a flag column: hard rule 4)
    required columns       required: true; an empty value stops the load
    code lists             customProperties.flagCodes; a code the contract doesn't list is reported, never changed
    total rows             schema customProperties.totalRows becomes the boolean column is_total_row. Total rows
                           stay in silver, because some metrics read the publisher's own margins; aggregation
                           excludes them (hard rule 2)
    renamedLabels          <column>__label: both names of a renamed label read as the contract's earlier label
    partialPlaces          <column>__partial_note: the contract's note for a place that covers only part of the
                           place it is named after, else empty

Nothing else changes a value. The column descriptions and the contract's code lists travel with silver as
DuckDB comments and as Parquet key-value metadata (SILVER_METADATA_KEY), so they reach the hub copy too.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

import duckdb

from health_data_registry.registry import RESERVED_COLUMN, Column, Contract, label_key

TOTAL_ROW = RESERVED_COLUMN
LABEL_SUFFIX = "__label"
PARTIAL_SUFFIX = "__partial_note"
SILVER_METADATA_KEY = "health_data_registry.silver"
SILVER_LAYOUT_VERSION = "1"


@dataclass(frozen=True)
class SilverColumn:
    """One column of a silver table, every field read from the contract."""

    name: str
    physical_type: str
    logical_type: str
    description: str
    # The contract property a derived column comes from (totalRows, renamedLabels, partialPlaces); None for a
    # column the contract declares itself.
    derived_from: str | None = None
    source_column: str | None = None
    unit: str | None = None
    codes: tuple[str, ...] = ()
    missing_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StagingReport:
    """What one staging run found: rows, total rows, and codes the contract doesn't list (column -> codes)."""

    row_count: int
    total_row_count: int
    unlisted_codes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def notes(self) -> list[str]:
        """Plain-words notes for every answer from this table: a code outside the contract is a data problem."""
        return [
            f"The publisher's {col} column holds the code{'s' if len(codes) > 1 else ''} "
            f"{', '.join(repr(c) for c in codes)}, which the reviewed contract doesn't list, so what "
            f"{'they mean' if len(codes) > 1 else 'it means'} is not known here."
            for col, codes in sorted(self.unlisted_codes.items())
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "layout_version": SILVER_LAYOUT_VERSION,
            "row_count": self.row_count,
            "total_row_count": self.total_row_count,
            "unlisted_codes": {k: list(v) for k, v in sorted(self.unlisted_codes.items())},
        }


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def silver_schema(contract: Contract) -> tuple[SilverColumn, ...]:
    """The silver table's columns in order: the contract's own, then those derived from its properties."""
    cols = [
        SilverColumn(
            name=c.name,
            physical_type=c.physical_type,
            logical_type=c.logical_type,
            description=c.description,
            unit=c.unit,
            codes=tuple(code for code, _meaning in c.flag_codes),
            missing_codes=c.missing_codes,
        )
        for c in contract.columns
    ]
    cols.append(
        SilverColumn(
            name=TOTAL_ROW,
            physical_type="BOOLEAN",
            logical_type="boolean",
            description=(
                "True on the publisher's total and summary rows (the contract's totalRows), which are never "
                "added to the rows they sum."
                if contract.total_rows
                else "Always false: the contract declares that this table has no total rows."
            ),
            derived_from="totalRows",
        )
    )
    for c in contract.columns:
        if c.renamed_labels:
            cols.append(
                SilverColumn(
                    name=c.name + LABEL_SUFFIX,
                    physical_type="VARCHAR",
                    logical_type="string",
                    description=f"{c.name} with each renamed label shown as the earlier label it continues "
                    "(the contract's renamedLabels), so every year of that place or condition reads the same.",
                    derived_from="renamedLabels",
                    source_column=c.name,
                )
            )
        if c.partial_places:
            cols.append(
                SilverColumn(
                    name=c.name + PARTIAL_SUFFIX,
                    physical_type="VARCHAR",
                    logical_type="string",
                    description=f"Why the {c.name} label covers only part of the place it is named after (the "
                    "contract's partialPlaces); empty for a whole place.",
                    derived_from="partialPlaces",
                    source_column=c.name,
                )
            )
    return tuple(cols)


def silver_column_names(contract: Contract) -> tuple[str, ...]:
    return tuple(c.name for c in silver_schema(contract))


def typed_select(contract: Contract) -> str:
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


def _key_sql(col: str) -> str:
    """SQL for registry.label_key: upper case, edge spaces trimmed, dots removed."""
    return f"regexp_replace(upper(trim({_q(col)})), '[.]', '', 'g')"


def _label_sql(c: Column) -> str:
    names = {label_key(x): earlier for later, earlier in c.renamed_labels for x in (later, earlier)}
    whens = " ".join(f"WHEN {_lit(k)} THEN {_lit(v)}" for k, v in names.items())
    return f"CASE {_key_sql(c.name)} {whens} ELSE {_q(c.name)} END"


def _partial_sql(c: Column) -> str:
    notes = {label_key(k): v for k, v in c.partial_places}
    other = _lit(notes.pop("*")) if "*" in notes else "NULL"
    col = _q(c.name)
    if not notes:
        return f"CASE WHEN {col} IS NULL THEN NULL ELSE {other} END"
    whens = " ".join(f"WHEN {_lit(k)} THEN {_lit(v)}" for k, v in notes.items())
    return f"CASE WHEN {col} IS NULL THEN NULL ELSE CASE {_key_sql(c.name)} {whens} ELSE {other} END END"


def stage_sql(contract: Contract, source: str) -> str:
    """One SELECT that turns raw records (`source`, a relation of text fields) into the silver table."""
    derived = [f"coalesce(({contract.total_rows}), false)" if contract.total_rows else "false"]
    derived[0] += f" AS {_q(TOTAL_ROW)}"
    for c in contract.columns:
        if c.renamed_labels:
            derived.append(f"{_label_sql(c)} AS {_q(c.name + LABEL_SUFFIX)}")
        if c.partial_places:
            derived.append(f"{_partial_sql(c)} AS {_q(c.name + PARTIAL_SUFFIX)}")
    return (
        f"WITH typed AS (SELECT\n  {typed_select(contract)}\nFROM {source})\n"  # noqa: S608 - quoted contract identifiers
        f"SELECT typed.*, {', '.join(derived)} FROM typed"
    )


def stage(
    con: duckdb.DuckDBPyConnection, contract: Contract, source: str, *, table: str, temp: bool = False
) -> StagingReport:
    """Build the silver table `table` from `source` and check it against the contract.

    A required column with an empty value stops the load (the layout doesn't match the contract). A flag code
    the contract doesn't list is kept as published and reported, so answers can say so.
    """
    kind = "TEMP TABLE" if temp else "TABLE"
    con.execute(f"CREATE {kind} {_q(table)} AS {stage_sql(contract, source)}")
    for c in contract.columns:
        if c.required:
            row = con.execute(f"SELECT count(*) FROM {_q(table)} WHERE {_q(c.name)} IS NULL").fetchone()  # noqa: S608 - quoted contract identifiers
            nulls = int(row[0]) if row else 0
            if nulls:
                raise ValueError(
                    f"{contract.id}: required column {c.name} has {nulls} empty values; check the contract"
                )
    unlisted: dict[str, tuple[str, ...]] = {}
    for c in contract.columns:
        if not c.flag_codes:
            continue
        listed = ", ".join(_lit(code) for code, _m in c.flag_codes)
        rows = con.execute(
            f"SELECT DISTINCT {_q(c.name)} FROM {_q(table)} WHERE nullif(trim({_q(c.name)}), '') IS NOT NULL "  # noqa: S608 - quoted contract identifiers
            f"AND {_q(c.name)} NOT IN ({listed}) ORDER BY 1"
        ).fetchall()
        if rows:
            unlisted[c.name] = tuple(str(r[0]) for r in rows)
    counts = con.execute(
        f"SELECT count(*), count(*) FILTER (WHERE {_q(TOTAL_ROW)}) FROM {_q(table)}"  # noqa: S608 - quoted identifiers
    ).fetchone() or (0, 0)
    if not temp:
        _comment(con, contract, table)
    return StagingReport(
        row_count=int(counts[0]), total_row_count=int(counts[1] or 0), unlisted_codes=unlisted
    )


def _comment(con: duckdb.DuckDBPyConnection, contract: Contract, table: str) -> None:
    """Carry the contract's descriptions into the DuckDB file, where `describe` tools and people can read them."""
    schema = (contract.raw.get("schema") or [{}])[0]
    desc = str(schema.get("description") or "").strip()
    if desc:
        con.execute(f"COMMENT ON TABLE {_q(table)} IS {_lit(desc)}")
    for c in silver_schema(contract):
        con.execute(f"COMMENT ON COLUMN {_q(table)}.{_q(c.name)} IS {_lit(c.description)}")


def silver_metadata(contract: Contract) -> str:
    """The silver layout as JSON, written into the Parquet file so a hub copy carries its contract facts."""
    return json.dumps(
        {
            "layout_version": SILVER_LAYOUT_VERSION,
            "contract_id": contract.id,
            "contract_version": contract.version,
            "columns": [asdict(c) for c in silver_schema(contract)],
        },
        sort_keys=True,
    )


def write_parquet(con: duckdb.DuckDBPyConnection, contract: Contract, table: str, path: str) -> None:
    con.execute(
        f"COPY {_q(table)} TO {_lit(path)} (FORMAT parquet, KV_METADATA {{{_lit(SILVER_METADATA_KEY)}: "
        f"{_lit(silver_metadata(contract))}}})"
    )
