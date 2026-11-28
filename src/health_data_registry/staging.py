"""Contract-driven staging: the one typed projection from raw publisher records, read only from the contract.

The layers follow sources -> staging -> intermediate -> marts (the dbt project in transform/):

    raw           the publisher's bytes, kept by sha256 (store.write_raw); an adapter only fetches and parses them
                  into records keyed by the publisher's own field names, every value as published text
    staging       this module: rename, type, blank the codebook's missing codes. Nothing per source but
                  declared extensions. The dbt
                  staging models are generated from the same projection (transform_gen.py), so the lake, the
                  live in-memory table and dbt type every column the same way; a source's declared
                  `staging` extensions (stage_entry) run inside the same projection
    intermediate  business rules (total rows, renamed labels, partial places, conformed keys), in dbt
    marts         dimensions and one fact per source, in dbt; metrics read them

What staging applies, and the contract property each comes from:

    column names          schema.properties[].name, read from customProperties.sourceField
    types                 physicalType, which must agree with logicalType (registry.LOGICAL_TYPES); a count with a
                          fraction stops the load instead of becoming a different count
    missing-value codes   customProperties.missingCodes become NULL (never on a flag column: hard rule 4)
    thousands marks       customProperties.groupSeparator ("1,204" read as 1204; numeric columns only)
    required columns      required: true; an empty value stops the load, on the lake and the live path alike
                          (a row with no place or year can't be placed, and hard rule 1 forbids a guess)
    declared fields       every customProperties.sourceField must appear in a full load, unless the contract
                          marks the column mayBeEmpty; a renamed or dropped publisher field stops the load
                          instead of loading as an all-empty column
    code lists            customProperties.flagCodes; a code the contract doesn't list is kept as published and
                          reported, so every answer from the table says so

The column descriptions and code lists travel with the table as DuckDB comments and as Parquet key-value
metadata (SILVER_METADATA_KEY), so the hub copy carries them too.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import duckdb

from health_data_registry.quality import QualityReport, rules_for, run_rules
from health_data_registry.registry import CatalogEntry, Contract

SILVER_METADATA_KEY = "health_data_registry.silver"
SILVER_LAYOUT_VERSION = "1"


class StagingRefused(ValueError):
    """A load staging refused. Nothing is kept: the table it was building is dropped before this is raised."""


class PublisherDataRefused(StagingRefused):
    """The publisher's values break the reviewed contract (an empty required value, a count with a fraction).

    The follow-up is a report to the publisher, or a look at the data, not a contract edit.
    """


class ContractLayoutRefused(StagingRefused):
    """The publisher's layout no longer matches the contract (a declared field never appears).

    The follow-up is a contract review: the publisher renamed or dropped a field.
    """


FRACTION_MARK = "has a fraction, so it isn't a whole count"


@dataclass(frozen=True)
class SilverColumn:
    """One typed column, every field read from the contract."""

    name: str
    physical_type: str
    logical_type: str
    description: str
    source_field: str
    unit: str | None = None
    codes: tuple[str, ...] = ()
    missing_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StagingReport:
    """What one staging run found: its rows, and codes the contract doesn't list (column -> codes)."""

    row_count: int
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
            "unlisted_codes": {k: list(v) for k, v in sorted(self.unlisted_codes.items())},
        }


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def silver_schema(contract: Contract) -> tuple[SilverColumn, ...]:
    """The typed table's columns, in contract order."""
    return tuple(
        SilverColumn(
            name=c.name,
            physical_type=c.physical_type,
            logical_type=c.logical_type,
            description=c.description,
            source_field=c.source_field,
            unit=c.unit,
            codes=tuple(code for code, _meaning in c.flag_codes),
            missing_codes=c.missing_codes,
        )
        for c in contract.columns
    )


def typed_select(contract: Contract) -> str:
    """The contract-driven projection from raw publisher fields (all read as text) to typed columns."""
    parts = []
    for c in contract.columns:
        src = _q(c.source_field)
        if c.missing_codes:
            codes = ", ".join(_lit(m) for m in c.missing_codes)
            src = f"CASE WHEN trim({src}) IN ({codes}) THEN NULL ELSE {src} END"
        if c.group_separator:
            # The publisher's thousands mark ("1,204"): read without it. The raw text keeps it as printed.
            src = f"replace(trim({src}), {_lit(c.group_separator)}, '')"
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
            msg = _lit(f"the publisher's data has a value in {c.name} that {FRACTION_MARK}")
            expr = f"CASE WHEN {dec} IS NULL THEN NULL WHEN {dec} = trunc({dec}) THEN CAST({dec} AS {c.physical_type}) ELSE error({msg}) END"
        else:
            expr = f"CAST(NULLIF(trim({src}), '') AS {c.physical_type})"
        parts.append(f"{expr} AS {_q(c.name)}")
    return ",\n  ".join(parts)


def absent_fields(contract: Contract, present: set[str]) -> list[str]:
    """Declared publisher fields that never appear in a load, leaving out columns the contract marks mayBeEmpty."""
    return sorted({c.source_field for c in contract.columns if not c.may_be_empty} - present)


def stage(
    con: duckdb.DuckDBPyConnection,
    contract: Contract,
    source: str,
    *,
    table: str,
    temp: bool = False,
    present: set[str] | None = None,
) -> StagingReport:
    """Build the typed table `table` from `source` (a relation of text fields) and check it against the contract.

    `present` is every field name seen in the load's records, when the caller knows them: a declared field that
    never appears refuses the load (ContractLayoutRefused). An empty required value or a count with a fraction
    refuses it too (PublisherDataRefused). A refused load leaves no table behind. A flag code the contract
    doesn't list is kept as published and reported.
    """
    if present is not None and (absent := absent_fields(contract, present)):
        raise ContractLayoutRefused(
            f"{contract.id}: the publisher's records no longer have the field{'s' if len(absent) > 1 else ''} "
            f"{', '.join(absent)}, which the contract reads; the publisher may have renamed "
            f"{'them' if len(absent) > 1 else 'it'}, so check the contract"
        )
    kind = "TEMP TABLE" if temp else "TABLE"
    try:
        try:
            con.execute(f"CREATE {kind} {_q(table)} AS SELECT\n  {typed_select(contract)}\nFROM {source}")  # noqa: S608 - quoted contract identifiers
        except duckdb.Error as e:
            if FRACTION_MARK in str(e):
                raise PublisherDataRefused(f"{contract.id}: {str(e).split('Error: ', 1)[-1].strip()}") from e
            raise
        for c in contract.columns:
            if c.required:
                row = con.execute(f"SELECT count(*) FROM {_q(table)} WHERE {_q(c.name)} IS NULL").fetchone()  # noqa: S608 - quoted contract identifiers
                nulls = int(row[0]) if row else 0
                if nulls:
                    raise PublisherDataRefused(
                        f"{contract.id}: the publisher's data has {nulls} empty value{'s' if nulls > 1 else ''} "
                        f"in {c.name}, which the contract requires"
                    )
    except Exception:
        con.execute(f"DROP TABLE IF EXISTS {_q(table)}")
        raise
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
    row = con.execute(f"SELECT count(*) FROM {_q(table)}").fetchone()  # noqa: S608 - quoted identifier
    if not temp:
        _comment(con, contract, table)
    return StagingReport(row_count=int(row[0]) if row else 0, unlisted_codes=unlisted)


@dataclass(frozen=True)
class Staged:
    """One staging run and the contract's quality rules run on its result."""

    staging: StagingReport
    quality: QualityReport

    def notes(self) -> list[str]:
        """Every plain-words note for answers from this table: unlisted codes, then data-check failures."""
        return [*self.staging.notes(), *self.quality.caveats()]


def stage_checked(
    con: duckdb.DuckDBPyConnection,
    contract: Contract,
    source: str,
    *,
    table: str,
    temp: bool = False,
    present: set[str] | None = None,
    entry: CatalogEntry | None = None,
) -> Staged:
    """Stage `source` into `table` (stage's checks, `present` included; with `entry`, after its staging
    extensions, as stage_entry), then run every quality rule of the contract on it: the live path. Lake snapshots
    are checked read-only after the file is closed (check_snapshot)."""
    if entry is None:
        report = stage(con, contract, source, table=table, temp=temp, present=present)
    else:
        report = stage_entry(con, entry, contract, source, table=table, temp=temp, present=present)

    return Staged(report, run_rules(con, table, rules_for(contract)))


def check_snapshot(path: str, contract: Contract, table: str) -> QualityReport:
    """Run the contract's quality rules on a closed snapshot file, opened read-only and locked down.

    Rule SQL comes from a contract, which may be model-drafted (hard rule 3): besides the guard that checked it
    at load time, the file it runs on can't be written, and the connection can't reach files or the network.
    """
    from health_data_registry.sql_guard import harden

    con = duckdb.connect(path, read_only=True)
    try:
        harden(con)
        return run_rules(con, table, rules_for(contract))
    finally:
        con.close()


def _comment(con: duckdb.DuckDBPyConnection, contract: Contract, table: str) -> None:
    """Carry the contract's descriptions into the DuckDB file, where tools and people can read them."""
    schema = (contract.raw.get("schema") or [{}])[0]
    desc = str(schema.get("description") or "").strip()
    if desc:
        con.execute(f"COMMENT ON TABLE {_q(table)} IS {_lit(desc)}")
    for c in contract.columns:
        con.execute(f"COMMENT ON COLUMN {_q(table)}.{_q(c.name)} IS {_lit(c.description)}")


def silver_metadata(contract: Contract) -> str:
    """The typed layout as JSON, written into the Parquet file so a hub copy carries its contract facts."""
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


# ------------------------------------------------------------------------------------------ staging extensions
# A source may name `staging` extensions (adapters.extensions): SQL steps over the raw relation, such as a pivot of
# a wide file. They run here, inside the one shared projection, so the lake, the live in-memory table and the
# generated dbt staging model apply the same SQL to the same raw records; only declared extensions, nothing else
# per source. Each step declares its cardinality, checked at the raw-to-staging boundary.


def _steps(entry: CatalogEntry) -> list[tuple[Any, Any]]:
    from health_data_registry.adapters.extensions import resolved

    return resolved(entry.source.extensions, "staging")


def raw_fields(entry: CatalogEntry, contract_fields: Sequence[str]) -> list[str]:
    """The raw fields a load hands on: the contract's source fields, except those a staging extension makes,
    plus the fields each extension reads."""
    steps = _steps(entry)
    if not steps:
        return list(contract_fields)
    produced: set[str] = set()
    inputs: set[str] = set()
    for ext, args in steps:
        ins = list(ext.input_fields(args))
        produced |= set(ext.run(args, "raw", ins).fields) - set(ins)
        inputs |= set(ins)
    return sorted(inputs | (set(contract_fields) - produced))


def staged_source(
    entry: CatalogEntry, relation: str, present: set[str] | None
) -> tuple[str, set[str] | None, int]:
    """The relation after the source's staging extensions, the fields it then has (None when not known), and how
    many staged rows one raw row makes.

    With ``present`` None (the generated dbt model, a ``--max-rows`` sample) each step is given only the fields
    it declares it reads, so its layout check can't see a missing or extra column: the full load already refused
    such a file before LATEST moved, and dbt reads only what LATEST names. ``made_fields`` gives the fields the
    steps make in either case, for the row guard.
    """
    sql, fields, rows_per_row = relation, present, 1
    for ext, args in _steps(entry):
        step = ext.run(args, sql, sorted(fields) if fields is not None else list(ext.input_fields(args)))
        sql, rows_per_row = step.sql, rows_per_row * step.rows_per_row
        fields = set(step.fields) if fields is not None else None
    return sql, fields, rows_per_row


def made_fields(entry: CatalogEntry) -> set[str]:
    """The fields the source's staging steps make from the fields they declare they read: known without a load,
    so the row guard on what an extension makes runs on every load, a partial sample included."""
    fields: set[str] = set()
    for ext, args in _steps(entry):
        ins = list(ext.input_fields(args))
        fields = (fields - set(ins)) | set(ext.run(args, "raw", sorted(fields | set(ins))).fields)
    return fields


def stage_entry(
    con: duckdb.DuckDBPyConnection,
    entry: CatalogEntry,
    contract: Contract,
    source: str,
    *,
    table: str,
    temp: bool = False,
    present: set[str] | None = None,
) -> StagingReport:
    """``stage`` after the entry's staging extensions, with the row guard on what they make and their declared
    cardinality checked (staged rows = raw rows x rows_per_row), so a step that drops or repeats rows is refused."""
    from health_data_registry.adapters.extensions import ExtensionRefused
    from health_data_registry.adapters.row_guard import check_grain

    if not _steps(entry):
        return stage(con, contract, source, table=table, temp=temp, present=present)
    try:
        sql, fields, rows_per_row = staged_source(entry, source, present)
    except ExtensionRefused as e:
        raise ContractLayoutRefused(f"{contract.id}: {e}") from e
    check_grain(f"{entry.id} (staged)", sorted(fields if fields is not None else made_fields(entry)))
    row = con.execute(f"SELECT count(*) FROM {source}").fetchone()  # noqa: S608 - a relation built by the caller
    raw_rows = int(row[0]) if row else 0
    report = stage(con, contract, sql, table=table, temp=temp, present=fields)
    if report.row_count != raw_rows * rows_per_row:
        con.execute(f"DROP TABLE IF EXISTS {_q(table)}")
        raise ContractLayoutRefused(
            f"{contract.id}: staging made {report.row_count} rows from {raw_rows}, not the {raw_rows * rows_per_row} "
            "its declared steps make; nothing was kept"
        )
    return report
