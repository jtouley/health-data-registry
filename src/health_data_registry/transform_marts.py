"""The conformed keys in intermediate, and the marts, generated from the contracts (`make contracts`).

Every stored source's intermediate model carries the same conformed columns on every row (CONFORMED), read only
from its contract's conformedDimensions (conformed.py):

    condition   condition_key (<dataset>:<label as published>), the label, and condition_case_status and
                condition_case_subtype as separate attributes. Concept ids are not here: dim_condition takes them
                only from accepted records of the control plane's crosswalk, so with no export every label is
                unresolved.
    place       place_key (USA; US-<state FIPS>; US-<state FIPS><county FIPS>; or <parent key>/<dataset>:<label>
                for a label that covers only part of its parent), the parent, a partial-area flag and a note.
                A label the contract can't resolve keeps a null key and place_status 'unresolved'.
    time        the MMWR year and week (from the publisher's MMWR columns, or a week-ending date read against the
                MMWR calendar), time_key, its grain, and the contract's dateBasis.

The marts (transform/models/marts/):

    fct_<id>              one fact per stored source with measures: its intermediate rows with dataset_id and
                          counted_unit. Each fact reads one source; no fact is a union, so counts with different
                          counted units are never in one table.
    dim_condition         one row per published condition label, with its concept ids when an accepted crosswalk
                          record gives them, else mapping_state 'unresolved'.
    dim_place             the gazetteer (task 84.3): the nation and every place declared in its contract, every
                          state and county in the Census code files, and every partial area the facts use.
    dim_time              every MMWR week of the calendar and every year (dim_time.sql, hand-written).
    dim_dataset_coverage  one row per fact: what one count is, its date basis, its years, places and
                          conditions, and how many rows have no resolved place or condition.
"""

# ruff: noqa: S608 - every SQL string here is a dbt model generated from reviewed contract identifiers and texts,
# each one quoted by q() or lit(); nothing comes from a user or a request.
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from health_data_registry import geo
from health_data_registry.conformed import NATION_KEY, Conformed
from health_data_registry.registry import Contract, Dataset, Registry, label_key

MARTS_DIR = Path("transform/models/marts")
CONTROL_PLANE = Path("contracts/control_plane")
CROSSWALK = "condition_crosswalk"
CROSSWALK_MODEL = "stg_control_plane__condition_crosswalk"
FIPS_STATE = "census_fips_state"
FIPS_COUNTY = "census_fips_county"
DIM_PLACE_CONTRACT = Path("contracts/models/dim_place.odcs.yaml")

# (name, dbt type, description): the columns every intermediate model adds, in this order.
CONFORMED: tuple[tuple[str, str, str], ...] = (
    (
        "condition_key",
        "varchar",
        "The condition's key in dim_condition: the source id and the label as published.",
    ),
    ("condition_label", "varchar", "The publisher's condition label, exactly as published."),
    (
        "condition_case_status",
        "varchar",
        "Case status (for example confirmed, probable), kept apart from the condition: from the contract.",
    ),
    (
        "condition_case_subtype",
        "varchar",
        "Case subtype (for example clinical or screening for C. auris), kept apart from the condition: declared "
        "per label in the contract; never added to another subtype.",
    ),
    (
        "place_key",
        "varchar",
        "The place's key in dim_place; empty when the contract does not resolve the label.",
    ),
    (
        "place_label",
        "varchar",
        "The place as the publisher names it (a renamed area shows its earlier name).",
    ),
    (
        "place_parent_key",
        "varchar",
        "For a label that covers only part of a place, the key of the place it is part of; never add the part "
        "to its parent.",
    ),
    (
        "place_is_partial",
        "boolean",
        "True when the publisher's place covers only part of the place it is named after or sits in (for "
        "example NNDSS New York, which excludes New York City).",
    ),
    (
        "place_note",
        "varchar",
        "Which part a partial area covers, or why the place is unresolved, in plain words.",
    ),
    ("place_status", "varchar", "resolved, partial_area or unresolved."),
    ("mmwr_year", "bigint", "MMWR year, when the publisher counts by MMWR week."),
    ("mmwr_week", "bigint", "MMWR week (1 to 53), when the publisher counts by MMWR week."),
    (
        "time_key",
        "varchar",
        "The period's key in dim_time: <year>-W<week> for an MMWR week, Y<year> for a year.",
    ),
    ("time_grain", "varchar", "mmwr_week, year or none."),
    ("date_basis", "varchar", "Which date the publisher counts a case by, from the contract (dateBasis)."),
)
FACT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("dataset_id", "varchar", "The catalogued source every row of this fact comes from."),
    (
        "counted_unit",
        "varchar",
        "What one count in this fact is, from the contract (countedUnit); facts with different units are never "
        "put in one table.",
    ),
)
HELPERS = ("_p_label", "_p_whole", "_p_decl_key", "_p_decl_parent", "_p_decl_note", "_p_note", "_t_year",
           "_t_week")  # fmt: skip


def q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def lit(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def key_sql(expr: str) -> str:
    """SQL for registry.label_key on an expression: upper case, edge spaces trimmed, dots removed."""
    return f"regexp_replace(upper(trim({expr})), '[.]', '', 'g')"


def slug_sql(expr: str) -> str:
    """A label as a key part: lower case, every run of other characters one dash, no dash at either end."""
    return f"trim(regexp_replace(lower(trim({expr})), '[^a-z0-9]+', '-', 'g'), '-')"


def has_measures(ds: Dataset) -> bool:
    return any(c.role == "measure" for c in ds.contract.columns)


def _case(expr: str, pairs: list[tuple[str, str | None]]) -> str:
    whens = " ".join(f"WHEN {lit(k)} THEN {lit(v)}" for k, v in pairs if v is not None)
    return f"CASE {expr} {whens} ELSE NULL END" if whens else "NULL"


def conformed_parts(ds: Dataset) -> tuple[list[tuple[str, str]], list[str], list[tuple[str, str]]]:
    """(helper columns, joins, conformed columns) for one source's intermediate model, each as (name, SQL)."""
    c: Conformed = ds.contract.conformed
    col = ds.contract.column
    joins: list[str] = []
    helpers: list[tuple[str, str]] = []

    # ---- place
    p = c.place
    label = "NULL"
    whole = "NULL"
    if p.match in ("state_name", "iso3") and p.column is not None:
        named = f"ruled.{q(p.column + '_label')}" if col(p.column).renamed_labels else f"ruled.{q(p.column)}"
        label = named
        if p.match == "state_name":
            joins.append(
                f"left join {{{{ ref('stg_{FIPS_STATE}') }}}} as place_state\n"
                f"  on {key_sql(named)} = {key_sql('place_state.state_name')}"
            )
            whole = (
                "CASE WHEN place_state.state_fips IS NULL THEN NULL ELSE 'US-' || place_state.state_fips END"
            )
        else:
            whole = (
                f"CASE WHEN regexp_full_match(upper(trim({named})), '[A-Z]{{3}}') THEN upper(trim({named})) "
                "ELSE NULL END"
            )
    elif p.match == "fips" and p.state_column is not None:
        st = f"ruled.{q(p.state_column)}"
        joins.append(
            f"left join {{{{ ref('stg_{FIPS_STATE}') }}}} as place_state\n  on place_state.state_fips = {st}"
        )
        if p.county_column is not None:
            co = f"ruled.{q(p.county_column)}"
            joins.append(
                f"left join {{{{ ref('stg_{FIPS_COUNTY}') }}}} as place_county\n"
                f"  on place_county.state_fips = {st} and place_county.county_fips = {co}"
            )
            whole = (
                "CASE WHEN place_county.county_fips IS NOT NULL "
                "THEN 'US-' || place_county.state_fips || place_county.county_fips "
                f"WHEN ({co} IS NULL OR {co} = '000') AND place_state.state_fips IS NOT NULL "
                "THEN 'US-' || place_state.state_fips ELSE NULL END"
            )
            label = (
                "CASE WHEN place_county.county_fips IS NOT NULL "
                "THEN place_county.county_name || ', ' || place_state.state_name "
                f"WHEN ({co} IS NULL OR {co} = '000') THEN place_state.state_name "
                f"ELSE 'FIPS ' || {st} || coalesce({co}, '') END"
            )
        else:
            whole = (
                "CASE WHEN place_state.state_fips IS NULL THEN NULL ELSE 'US-' || place_state.state_fips END"
            )
            label = f"coalesce(place_state.state_name, 'FIPS ' || {st})"
    elif p.match == "fixed" and p.fixed is not None:
        label = lit(p.fixed.label)
    decls = [(label_key(d.label), d) for d in p.places]
    lkey = key_sql(label)
    decl_key = _case(lkey, [(k, d.key) for k, d in decls])
    decl_parent = _case(lkey, [(k, d.parent) for k, d in decls])
    decl_note = _case(lkey, [(k, d.note) for k, d in decls])
    if p.match == "fixed" and p.fixed is not None:
        decl_key, decl_parent, decl_note = lit(p.fixed.key), lit(p.fixed.parent), lit(p.fixed.note)
    note = (
        f"ruled.{q(p.column + '_partial_note')}"
        if p.column is not None and col(p.column).partial_places
        else "NULL"
    )
    helpers += [
        ("_p_label", label),
        ("_p_whole", whole),
        ("_p_decl_key", decl_key),
        ("_p_decl_parent", decl_parent),
        ("_p_decl_note", decl_note),
        ("_p_note", note),
    ]

    # ---- time
    t = c.time
    if t.grain == "mmwr_date" and t.date_column is not None:
        d = f"ruled.{q(t.date_column)}"
        joins.append(
            "left join {{ ref('mmwr_calendar') }} as cal\n"
            f"  on cast(try_cast(trim(cast({d} as varchar)) as timestamp) as date) "
            "between cal.week_start and cal.week_end"
        )
        helpers += [("_t_year", "cal.mmwr_year"), ("_t_week", "cal.mmwr_week")]
    elif t.grain == "mmwr_week" and t.year_column and t.week_column:
        helpers += [
            ("_t_year", f"try_cast(ruled.{q(t.year_column)} AS BIGINT)"),
            ("_t_week", f"try_cast(ruled.{q(t.week_column)} AS BIGINT)"),
        ]
    elif t.grain == "year" and t.year_column:
        helpers += [("_t_year", f"try_cast(ruled.{q(t.year_column)} AS BIGINT)"), ("_t_week", "NULL")]
    else:
        helpers += [("_t_year", "NULL"), ("_t_week", "NULL")]
    weekly = t.grain in ("mmwr_week", "mmwr_date")

    # ---- condition
    cond = c.condition
    clabel = f"keyed.{q(cond.column)}" if cond.column is not None else lit(cond.label)
    ckey = f"CASE WHEN {clabel} IS NULL THEN NULL ELSE {lit(ds.id + ':')} || {clabel} END"
    status = f"keyed.{q(cond.case_status_column)}" if cond.case_status_column else lit(cond.case_status)
    subtype = _case(clabel, list(cond.case_subtypes)) if cond.case_subtypes else "NULL"

    partial = (
        "(keyed._p_decl_key IS NULL AND (keyed._p_decl_parent IS NOT NULL OR keyed._p_note IS NOT NULL))"
    )
    parent = "coalesce(keyed._p_decl_parent, keyed._p_whole)"
    place_key = (
        "CASE WHEN keyed._p_label IS NULL THEN NULL "
        "WHEN keyed._p_decl_key IS NOT NULL THEN keyed._p_decl_key "
        f"WHEN {partial} THEN CASE WHEN {parent} IS NULL THEN NULL "
        f"ELSE {parent} || {lit('/' + ds.id + ':')} || {slug_sql('keyed._p_label')} END "
        "ELSE keyed._p_whole END"
    )
    unresolved = lit(
        p.unresolved
        or "unresolved: the contract does not declare this place label and it is not a name or code in the "
        "Census code files"
    )
    columns = [
        ("condition_key", ckey),
        ("condition_label", clabel),
        ("condition_case_status", status),
        ("condition_case_subtype", subtype),
        ("place_key", place_key),
        ("place_label", "keyed._p_label"),
        ("place_parent_key", f"CASE WHEN {partial} THEN {parent} ELSE NULL END"),
        ("place_is_partial", f"coalesce({partial}, false)"),
        (
            "place_note",
            f"CASE WHEN {partial} AND {parent} IS NOT NULL THEN coalesce(keyed._p_decl_note, keyed._p_note) "
            f"WHEN ({place_key}) IS NULL THEN {unresolved} ELSE NULL END",
        ),
        (
            "place_status",
            f"CASE WHEN ({place_key}) IS NULL THEN 'unresolved' WHEN {partial} THEN 'partial_area' "
            "ELSE 'resolved' END",
        ),
        ("mmwr_year", "keyed._t_year" if weekly else "CAST(NULL AS BIGINT)"),
        ("mmwr_week", "keyed._t_week" if weekly else "CAST(NULL AS BIGINT)"),
        (
            "time_key",
            "CASE WHEN keyed._t_year IS NULL OR keyed._t_week IS NULL THEN NULL "
            "ELSE CAST(keyed._t_year AS VARCHAR) || '-W' || lpad(CAST(keyed._t_week AS VARCHAR), 2, '0') END"
            if weekly
            else (
                "CASE WHEN keyed._t_year IS NULL THEN NULL ELSE 'Y' || CAST(keyed._t_year AS VARCHAR) END"
                if t.grain == "year"
                else "CAST(NULL AS VARCHAR)"
            ),
        ),
        ("time_grain", lit("mmwr_week" if weekly else t.grain)),
        ("date_basis", lit(t.date_basis)),
    ]
    return helpers, joins, columns


def check_clash(contract: Contract, derived: list[str]) -> None:
    taken = contract.column_names | set(derived)
    clash = [name for name, *_ in CONFORMED if name in taken]
    if clash:
        raise ValueError(f"{contract.id}: the contract already has a column named {clash[0]}")


def intermediate_body(ds: Dataset, derived_sql: str, derived_names: list[str]) -> str:
    """The intermediate model's SQL after its header: contract rules, then the conformed keys."""
    check_clash(ds.contract, derived_names)
    helpers, joins, columns = conformed_parts(ds)
    helper_sql = ",\n    ".join(f"{sql} AS {q(name)}" for name, sql in helpers)
    types = {n: t for n, t, _d in CONFORMED}
    out_sql = ",\n  ".join(f"CAST({sql} AS {types[name].upper()}) AS {q(name)}" for name, sql in columns)
    join_sql = ("\n  " + "\n  ".join(joins)) if joins else ""
    excluded = ", ".join(q(h) for h, _ in helpers)
    return (
        "with stg as (\n"
        f"    select * from {{{{ ref('stg_{ds.id}') }}}}\n"
        "),\n\n"
        "ruled as (\n"
        f"    select\n      stg.*,\n      {derived_sql}\n    from stg\n"
        "),\n\n"
        "keyed as (\n"
        f"    select\n    ruled.*,\n    {helper_sql}\n  from ruled{join_sql}\n"
        ")\n\n"
        f"select\n  keyed.* exclude ({excluded}),\n  {out_sql}\nfrom keyed\n"
    )


def conformed_yml_columns() -> list[dict[str, Any]]:
    return [
        {"name": n, "data_type": t, "description": d, "meta": {"conformed": True}} for n, t, d in CONFORMED
    ]


# --------------------------------------------------------------------------------------------- facts


def fact_sql(ds: Dataset, header: str) -> str:
    return (
        f"-- {header}\n"  # noqa: S608 - generated from quoted contract identifiers
        "-- One fact per source: never a union, so counts with different counted units never share a table.\n"
        "select\n"
        f"  {lit(ds.id)} as dataset_id,\n"
        f"  {lit(ds.contract.conformed.counted_unit)} as counted_unit,\n"
        "  int.*\n"
        f"from {{{{ ref('int_{ds.id}') }}}} as int\n"
    )


def _relationship(model: str, column: str, to: str, field: str) -> dict[str, Any]:
    return {
        "relationships": {
            "name": f"{model}_{column}_in_{to}",
            "arguments": {"to": f"ref('{to}')", "field": field},
            "config": {"where": f"{column} is not null"},
        }
    }


def fact_yml(ds: Dataset, columns: list[dict[str, Any]], header: str) -> str:
    """The fact's enforced schema: the intermediate columns, the fact's two, and its key relationships."""
    model = f"fct_{ds.id}"
    tests = {
        "condition_key": _relationship(model, "condition_key", "dim_condition", "condition_key"),
        "place_key": _relationship(model, "place_key", "dim_place", "place_key"),
        "time_key": _relationship(model, "time_key", "dim_time", "time_key"),
    }
    cols = [{"name": n, "data_type": t, "description": d, "meta": {"fact": True}} for n, t, d in FACT_COLUMNS]
    for c in columns:
        c = dict(c)
        if c["name"] in tests:
            c["data_tests"] = [tests[c["name"]]]
        cols.append(c)
    schema = (ds.contract.raw.get("schema") or [{}])[0]
    desc = ds.contract.raw.get("description") or {}
    entry = {
        "name": model,
        "description": (
            f"{str(schema.get('description') or ds.entry.title).strip()} One count is: "
            f"{ds.contract.conformed.counted_unit}. Dates are by: {ds.contract.conformed.time.date_basis}."
        ),
        "config": {"materialized": "table", "contract": {"enforced": True}, "tags": ["marts", "fact"]},
        "meta": {
            "contract": ds.entry.contract,
            "contract_id": ds.contract.id,
            "contract_version": ds.contract.version,
            "contract_status": ds.contract.status,
            "reviewed": ds.reviewed,
            "context": context(ds),
            "purpose": str(desc.get("purpose") or "").strip(),
            "usage": str(desc.get("usage") or "").strip(),
            "limitations": str(desc.get("limitations") or "").strip(),
        },
        "columns": cols,
    }
    return dump(header, {"version": 2, "models": [entry]})


def context(ds: Dataset) -> dict[str, Any]:
    """The contract context every answer from this source carries, as dbt metadata."""
    c = ds.contract
    return {
        "counted_unit": c.conformed.counted_unit,
        "date_basis": c.conformed.time.date_basis,
        "geography_basis": c.geography_basis,
        "rate_denominator": c.rate_denominator,
        "case_status": c.conformed.condition.case_status,
        "unreliable_rule": c.unreliable_rule,
        "licence": ds.entry.licence.id,
        "open_questions": [q.id for q in c.open_questions if q.is_open],
        "blocking_questions": [q.id for q in c.blocking_questions],
    }


def dump(header: str, doc: Mapping[str, Any]) -> str:
    body = yaml.safe_dump(dict(doc), sort_keys=False, allow_unicode=True, width=110)
    return f"# {header}\n{body}"


# --------------------------------------------------------------------------------------------- dimensions


def facts(registry: Registry) -> list[Dataset]:
    return [ds for ds in registry if ds.entry.licence.redistribution and has_measures(ds)]


def dim_condition_sql(registry: Registry, header: str) -> str:
    parts, listed = [], []
    for ds in facts(registry):
        if not ds.contract.conformed.condition.applies:
            continue
        parts.append(
            f"select {lit(ds.id)} as dataset_id, condition_key, condition_label,\n"  # noqa: S608 - quoted catalog identifiers
            "        string_agg(distinct condition_case_status, '; ' order by condition_case_status) as case_status,\n"
            "        string_agg(distinct condition_case_subtype, '; ' order by condition_case_subtype) as case_subtype,\n"
            "        count(*) as row_count\n"
            f"    from {{{{ ref('int_{ds.id}') }}}}\n"
            "    where condition_key is not null\n"
            "    group by condition_key, condition_label"
        )
        labels = ds.entry.conditions.labels or (
            [ds.contract.conformed.condition.label] if ds.contract.conformed.condition.label else []
        )
        listed += [f"({lit(ds.id)}, {lit(x)})" for x in labels if x]
    labels_sql = "\n    union all\n    ".join(parts) or (
        "select cast(null as varchar) as dataset_id, cast(null as varchar) as condition_key, "
        "cast(null as varchar) as condition_label, cast(null as varchar) as case_status, "
        "cast(null as varchar) as case_subtype, cast(0 as bigint) as row_count where false"
    )
    listed_sql = (
        "select * from (values\n        "
        + ",\n        ".join(listed)
        + "\n    ) as t(dataset_id, condition_label)"
        if listed
        else "select cast(null as varchar) as dataset_id, cast(null as varchar) as condition_label where false"
    )
    return (
        f"-- {header}\n"
        "-- Concept ids come only from accepted crosswalk records that name their reviewer, date and source;\n"
        "-- every other label is unresolved. Proposed records are counted for review and change nothing.\n"
        f"with labels as (\n    {labels_sql}\n),\n\n"
        f"listed as (\n    {listed_sql}\n),\n\n"
        f"crosswalk as (\n    select * from {{{{ ref('{CROSSWALK_MODEL}') }}}}\n),\n\n"
        "accepted as (\n"
        "    select * from crosswalk\n"
        "    where state = 'accepted' and synonym is null and reviewer is not null and reviewed_at is not null\n"
        "        and source is not null\n"
        "    qualify row_number() over (partition by dataset, label order by reviewed_at desc, record_id desc) = 1\n"
        "),\n\n"
        "proposed as (\n"
        "    select dataset, label, count(*) as proposals from crosswalk where state = 'proposed'\n"
        "    group by dataset, label\n"
        ")\n\n"
        "select\n"
        "    l.condition_key,\n"
        "    l.dataset_id,\n"
        "    l.condition_label,\n"
        "    l.case_status,\n"
        "    l.case_subtype,\n"
        "    li.condition_label is not null as is_listed,\n"
        "    a.mesh_id,\n"
        "    a.ncbi_taxon_id,\n"
        "    a.gbd_cause,\n"
        "    case when a.record_id is not null then 'accepted' else 'unresolved' end as mapping_state,\n"
        "    a.source as mapping_source,\n"
        "    a.reviewer as mapping_reviewed_by,\n"
        "    a.reviewed_at as mapping_reviewed_at,\n"
        "    coalesce(p.proposals, 0) as open_proposals,\n"
        "    l.row_count\n"
        "from labels as l\n"
        "left join listed as li on li.dataset_id = l.dataset_id and li.condition_label = l.condition_label\n"
        "left join accepted as a on a.dataset = l.dataset_id and a.label = l.condition_label\n"
        "left join proposed as p on p.dataset = l.dataset_id and p.label = l.condition_label\n"
    )


PLACE_COLUMNS = ("place_key", "place_name", "place_level", "parent_key", "is_partial_area", "place_note", "fips",
                 "postal", "iso3", "m49", "gbd_location_name", "never_add_with", "key_source")  # fmt: skip


def _empty_places() -> str:
    typed = ", ".join(
        f"cast(null as {'boolean' if c == 'is_partial_area' else 'varchar'}) as {c}" for c in PLACE_COLUMNS
    )
    return f"select {typed} where false"


def declared_places(root: Path) -> list[dict[str, Any]]:
    """The places dim_place's own contract declares (the nation, sub-county jurisdictions), each citing a source."""
    path = root / DIM_PLACE_CONTRACT
    if not path.is_file():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    schema = (doc.get("schema") or [{}])[0]
    found = [
        p.get("value") for p in schema.get("customProperties") or [] if p.get("property") == "declaredPlaces"
    ]
    if len(found) != 1 or not isinstance(found[0], list):
        raise ValueError(f"{DIM_PLACE_CONTRACT}: declare the places once, as a list (declaredPlaces)")
    out = []
    for item in found[0]:
        unknown = set(item) - {"key", "name", "level", "parent", "note", "iso3", "m49", "gbdLocationName",
                               "neverAddWith", "source"}  # fmt: skip
        if unknown or not item.get("key") or not item.get("name") or not item.get("source"):
            raise ValueError(f"{DIM_PLACE_CONTRACT}: a declared place needs key, name and source: {item}")
        out.append(item)
    keys = [x["key"] for x in out]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{DIM_PLACE_CONTRACT}: two declared places share a key")
    return out


def dim_place_sql(registry: Registry, header: str) -> str:
    declared = declared_places(registry.root)
    rows = []
    for d in declared:
        partial = d.get("parent") is not None and d.get("level") != "nation" and d.get("note") is not None
        never = d.get("neverAddWith") or []
        rows.append(
            "("
            + ", ".join(
                [
                    lit(d["key"]),
                    lit(d["name"]),
                    lit(d.get("level") or "partial_area"),
                    lit(d.get("parent")),
                    "true" if partial else "false",
                    lit(d.get("note")),
                    "NULL",
                    "NULL",
                    lit(d.get("iso3")),
                    lit(d.get("m49")),
                    lit(d.get("gbdLocationName")),
                    lit(", ".join(never) if never else None),
                    lit(d["source"]),
                ]
            )
            + ")"
        )
    gbd = ",\n        ".join(f"({lit(p.fips)}, {lit(p.name)})" for p in geo.PLACES if p.level == "state")
    cols = ", ".join(PLACE_COLUMNS)
    partial_parts = [
        f"select place_key, min(place_label) as place_name, 'partial_area' as place_level,\n"  # noqa: S608 - quoted catalog identifiers
        f"        min(place_parent_key) as parent_key, true as is_partial_area, min(place_note) as place_note,\n"
        f"        cast(null as varchar) as fips, cast(null as varchar) as postal, cast(null as varchar) as iso3,\n"
        f"        cast(null as varchar) as m49, cast(null as varchar) as gbd_location_name,\n"
        f"        cast(null as varchar) as never_add_with, {lit(ds.id)} as key_source\n"
        f"    from {{{{ ref('int_{ds.id}') }}}}\n"
        "    where place_status = 'partial_area'\n"
        "    group by place_key"
        for ds in facts(registry)
    ]
    partial_sql = "\n    union all\n    ".join(partial_parts) or _empty_places()
    return (
        f"-- {header}\n"
        "-- The gazetteer: declared places (contracts/models/dim_place.odcs.yaml), the Census state and county\n"
        "-- code files, and the partial areas the facts use. GBD location names are geo.py's (states, DC, PR).\n"
        + "with declared as (\n    "
        + (
            "select * from (values\n        " + ",\n        ".join(rows) + f"\n    ) as t({cols})"
            if rows
            else _empty_places()
        )
        + "\n),\n\n"
        + f"gbd_names as (\n    select * from (values\n        {gbd}\n    ) as t(state_fips, gbd_location_name)\n),\n\n"
        "states as (\n"
        "    select 'US-' || s.state_fips as place_key, s.state_name as place_name, 'state' as place_level,\n"
        f"        {lit(NATION_KEY)} as parent_key, false as is_partial_area, cast(null as varchar) as place_note,\n"
        "        s.state_fips as fips, s.postal, cast(null as varchar) as iso3, cast(null as varchar) as m49,\n"
        "        g.gbd_location_name, cast(null as varchar) as never_add_with,\n"
        f"        {lit(FIPS_STATE)} as key_source\n"
        f"    from {{{{ ref('stg_{FIPS_STATE}') }}}} as s\n"
        "    left join gbd_names as g on g.state_fips = s.state_fips\n"
        "),\n\n"
        "counties as (\n"
        "    select 'US-' || c.state_fips || c.county_fips as place_key, c.county_name as place_name,\n"
        "        'county' as place_level, 'US-' || c.state_fips as parent_key, false as is_partial_area,\n"
        "        cast(null as varchar) as place_note, c.state_fips || c.county_fips as fips, c.postal,\n"
        "        cast(null as varchar) as iso3, cast(null as varchar) as m49,\n"
        "        cast(null as varchar) as gbd_location_name,\n"
        "        d.never_add_with,\n"
        f"        {lit(FIPS_COUNTY)} as key_source\n"
        f"    from {{{{ ref('stg_{FIPS_COUNTY}') }}}} as c\n"
        "    left join declared as d on d.place_key = 'US-' || c.state_fips || c.county_fips\n"
        "),\n\n"
        f"partial_areas as (\n    {partial_sql}\n)\n\n"
        f"select {cols} from declared\n"
        "where place_key not in (select place_key from counties)\n"
        f"union all\nselect {cols} from states\n"
        f"union all\nselect {cols} from counties\n"
        f"union all\nselect {cols} from partial_areas\n"
        "where place_key not in (select place_key from declared)\n"
    )


def dim_dataset_coverage_sql(registry: Registry, header: str) -> str:
    parts = []
    for ds in facts(registry):
        c = ds.contract
        review = (
            "reviewed"
            if ds.reviewed
            else (
                f"draft: {len(c.blocking_questions)} open question(s) could change a number"
                if c.blocking_questions
                else "draft: no maintainer has reviewed the contract yet"
            )
        )
        parts.append(
            "select\n"  # noqa: S608 - quoted catalog identifiers
            f"    {lit(ds.id)} as dataset_id,\n"
            f"    {lit(ds.entry.title)} as title,\n"
            f"    {lit(c.conformed.counted_unit)} as counted_unit,\n"
            f"    {lit(c.conformed.time.date_basis)} as date_basis,\n"
            f"    {lit(c.geography_basis)} as geography_basis,\n"
            f"    {lit(c.version)} as contract_version,\n"
            f"    {lit(review)} as review_state,\n"
            f"    {lit(ds.entry.licence.id)} as licence,\n"
            "    count(*) as row_count,\n"
            "    min(f.time_key) as first_time_key,\n"
            "    max(f.time_key) as last_time_key,\n"
            "    count(distinct f.condition_key) as conditions,\n"
            "    count(distinct f.place_key) as places,\n"
            "    count(*) filter (where f.place_status = 'unresolved') as unresolved_place_rows,\n"
            "    count(*) filter (where f.place_status = 'partial_area') as partial_area_rows,\n"
            "    count(*) filter (where dc.mapping_state = 'unresolved') as unresolved_condition_rows\n"
            f"from {{{{ ref('fct_{ds.id}') }}}} as f\n"
            "left join {{ ref('dim_condition') }} as dc on dc.condition_key = f.condition_key"
        )
    return f"-- {header}\n" + "\nunion all\n".join(parts) + "\n"


# --------------------------------------------------------------------------------------------- control plane


def control_plane_contracts(root: Path) -> list[tuple[Path, Contract]]:
    from health_data_registry.registry import parse_contract

    out = []
    for path in sorted((root / CONTROL_PLANE).glob("*.odcs.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        out.append(
            (path.relative_to(root), parse_contract(doc, expected_id=path.name.removesuffix(".odcs.yaml")))
        )
    return out


def control_plane_source(contracts: list[tuple[Path, Contract]]) -> dict[str, Any]:
    tables = []
    for path, contract in contracts:
        fields = sorted({c.source_field for c in contract.columns})
        columns = ", ".join(f"'{f}': 'VARCHAR'" for f in fields)
        base = "{{ env_var('HDR_CONTROL_PLANE', '../lake/control_plane') }}/" + contract.id + "/*.ndjson"
        tables.append(
            {
                "name": contract.id,
                "description": f"The control plane's latest export of {contract.id}: every value as text. "
                "Absent until the control plane exports one; then the staging model is empty.",
                "meta": {
                    "contract": str(path),
                    "raw_contract_version": contract.version,
                    "optional": True,
                    "glob": base,
                    "external_location": "read_json('"
                    + base
                    + "', format='newline_delimited', filename=true, columns={{ '{{' }}"
                    + columns
                    + "{{ '}}' }})",
                },
                "columns": [{"name": f, "data_type": "varchar"} for f in fields],
            }
        )
    return {
        "name": "control_plane",
        "description": "Exports of the control plane (task 83), each with an ODCS contract in contracts/control_plane/.",
        "tables": tables,
    }


def control_plane_staging_sql(contract: Contract, header: str) -> str:
    from health_data_registry.staging import typed_select

    return (
        f"-- {header}\n"
        "-- The latest export only. optional_source gives an empty table while no export exists.\n"
        "with raw as (\n"
        f"    select * from {{{{ optional_source('control_plane', '{contract.id}') }}}}\n"
        "),\n\n"
        "latest as (\n"
        "    select * from raw where filename = (select max(filename) from raw)\n"
        ")\n\n"
        f"select\n  {typed_select(contract)}\nfrom latest\n"
    )
