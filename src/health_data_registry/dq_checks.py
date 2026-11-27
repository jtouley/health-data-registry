"""The data-quality checks a contract declares for dbt to run, and the one registry that maps each to its test.

The ODCS contract is the only place a check is declared. A check is a `custom` quality rule with engine
`dbt-expectations`, on the schema object (a table check) or on a property (a column check), with its rule id,
dimension, severity and description, like every other contract rule:

    quality:
      - id: rows_raw_to_staging
        type: custom
        engine: dbt-expectations
        implementation: {check: row_count_matches, model: staging, compare: raw}
        dimension: consistency
        severity: error
        description: "Staging has exactly as many rows as the raw load it types."

`implementation.check` names a check type in CHECK_TYPES. Each type is data: the dbt-expectations test it runs,
whether it sits on the table or a column, the parameters it takes (each of a kind in PARAM_KINDS) and the test's
arguments as templates over those parameters. Every check names the layer it runs on (`model`): which layer a test
runs on is part of the declaration, so it lives in the contract, never in a default here. transform_gen.py writes
the dbt test from the type and the contract's values; nothing else lists checks.

Adding a check type is one CHECK_TYPES entry plus contract YAML: no new code path. A contract naming a type that
isn't registered, a parameter the type doesn't take, or a value of the wrong kind is refused when the registry
loads, so `health-data-registry validate` and every test catch it, and the contract and dbt can't drift apart.
So is a check on a contract dbt never builds: a source whose licence keeps it out of the lake (registry.py), or a
model contract (transform_gen.model_contracts).

Freshness reads its cadence from the contract's ODCS `slaProperties` (the `sla` parameter names one, e.g.
frequency): the cadence is a service level, the check that holds the data to it is a quality rule. It compares the
load time with dbt_date's clock (now, in UTC), so its result depends on the day it runs as well as on the data: on
the synthetic fixtures (all loaded 2026-01-08) more freshness warnings appear as the calendar passes each cadence,
with no change to code or data. A change in that warning count is not a data change.

These checks run only in the dbt build; the read path (quality.run_rules) runs the library and sql rules. So a
value range declared here (values_between) is not checked on a snapshot or live read served outside the dbt build;
a range that must hold on the read path too is declared as a `sql` rule instead, which runs in both places. A check
asserts only what the publisher's codebook or an exact structural fact states (hard rule 1).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

ENGINE = "dbt-expectations"
DIMENSIONS = frozenset(
    {"accuracy", "completeness", "conformity", "consistency", "coverage", "timeliness", "uniqueness"}
)
SEVERITIES = frozenset({"error", "warn"})

# The layers a check can sit on (models) and compare with: each one's dbt relation for a dataset id.
LAYERS: dict[str, str] = {
    "raw": "source('raw', '{dataset}')",
    "staging": "ref('stg_{dataset}')",
    "intermediate": "ref('int_{dataset}')",
}
# The typed layers: a check runs on one of these, and a column total compares only these (the raw layer holds
# every field as text, hard rule 15, so it has no number to sum).
MODEL_LAYERS = frozenset({"staging", "intermediate"})
# A rule id becomes part of a dbt test name (<dataset>_<id>), which the release gate matches by name.
RULE_ID = re.compile(r"^[a-z][a-z0-9_]*$")

# ODCS slaProperties units for a cadence, as dbt's (datepart, multiplier).
SLA_UNITS: dict[str, tuple[str, int]] = {
    "d": ("day", 1),
    "day": ("day", 1),
    "days": ("day", 1),
    "w": ("day", 7),
    "week": ("day", 7),
    "weeks": ("day", 7),
    "m": ("month", 1),
    "month": ("month", 1),
    "months": ("month", 1),
    "y": ("year", 1),
    "year": ("year", 1),
    "years": ("year", 1),
}
# The load time every row carries: its snapshot id starts with the retrieval time, in UTC (store.py). try_strptime,
# so a malformed id reads as no load time (an empty max, which the freshness test reads as stale: a warning),
# never as an error that would hold the build on a warn-severity check.
SNAPSHOT_TIME_SQL = "try_strptime(left(_snapshot_id, 16), '%Y%m%dT%H%M%SZ')"


class CheckInvalid(ValueError):
    """A contract's dbt-expectations check doesn't fit the registry (caught when the registry loads)."""


def _number(value: Any) -> bool:
    """A finite number: NaN or infinity as a bound would make a range check vacuous."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


# How each kind of parameter is checked: True when the value is acceptable. A layer is a name, never a list.
PARAM_KINDS: dict[str, Callable[[Any], bool]] = {
    "layer": lambda v: isinstance(v, str) and v in LAYERS,
    "typed_layer": lambda v: isinstance(v, str) and v in MODEL_LAYERS,
    "model": lambda v: isinstance(v, str) and v in MODEL_LAYERS,
    "number": _number,
    "sla": lambda v: isinstance(v, str) and bool(v),
}


@dataclass(frozen=True)
class CheckType:
    """One kind of check: the dbt-expectations test it runs and how its arguments come from the contract."""

    test: str
    scope: str  # "table" (on the schema object) or "column" (on a property)
    params: Mapping[str, str]  # parameter -> kind (PARAM_KINDS)
    arguments: Mapping[str, Any]  # test argument -> template; "{p}" alone keeps the value's type
    required: frozenset[str] = frozenset()
    one_of: frozenset[str] = frozenset()  # at least one of these must be given


# Parameter kinds that name a layer: each is written as that layer's dbt relation.
RELATION_KINDS = frozenset({"layer", "typed_layer"})

# The registry. Every template placeholder is a parameter, or one of: column (the quoted column name), and for a
# `layer` or `typed_layer` parameter p, p as its dbt relation; for an `sla` parameter, sla_datepart and
# sla_interval. Every type takes and requires `model`, the layer it runs on. A column check whose test takes
# dbt's own `column_name` argument is written under that column in the model YAML (dbt then knows the column is
# tested); every other check is written on the model.
CHECK_TYPES: dict[str, CheckType] = {
    "row_count_matches": CheckType(
        test="dbt_expectations.expect_table_row_count_to_equal_other_table",
        scope="table",
        params={"model": "model", "compare": "layer"},
        arguments={"compare_model": "{compare}"},
        required=frozenset({"model", "compare"}),
    ),
    "total_matches": CheckType(
        test="dbt_expectations.expect_table_aggregation_to_equal_other_table",
        scope="column",
        params={"model": "model", "compare": "typed_layer"},
        arguments={"expression": "sum({column})", "compare_model": "{compare}"},
        required=frozenset({"model", "compare"}),
    ),
    "values_between": CheckType(
        test="dbt_expectations.expect_column_values_to_be_between",
        scope="column",
        params={"model": "model", "min": "number", "max": "number"},
        arguments={
            "column_name": "{column}",
            "min_value": "{min}",
            "max_value": "{max}",
            "row_condition": "{column} is not null",
        },
        required=frozenset({"model"}),
        one_of=frozenset({"min", "max"}),
    ),
    "load_within_sla": CheckType(
        test="dbt_expectations.expect_row_values_to_have_recent_data",
        scope="table",
        params={"model": "model", "sla": "sla"},
        arguments={
            "column_name": SNAPSHOT_TIME_SQL,
            "datepart": "{sla_datepart}",
            "interval": "{sla_interval}",
        },
        required=frozenset({"model", "sla"}),
    ),
}


@dataclass(frozen=True)
class Check:
    """One dbt-expectations check a contract declares."""

    id: str
    check: str
    description: str
    dimension: str
    severity: str
    column: str | None
    params: Mapping[str, Any] = field(default_factory=dict)
    sla: tuple[str, int] | None = None  # (datepart, interval) of the SLA property it reads

    @property
    def model(self) -> str:
        return str(self.params["model"])

    @property
    def on_column(self) -> bool:
        """True when dbt runs the test under its column (the test takes dbt's own column_name argument)."""
        return self.column is not None and "column_name" in CHECK_TYPES[self.check].arguments


def is_custom(item: Any) -> bool:
    """True for a custom quality rule: parse_checks validates each one against the registry."""
    return isinstance(item, Mapping) and item.get("type") == "custom"


def is_check(item: Any) -> bool:
    """True for a dbt-expectations check (custom, engine dbt-expectations): the read path leaves it to dbt."""
    return is_custom(item) and item.get("engine") == ENGINE


def _sla(doc: Mapping[str, Any], name: str, where: str) -> tuple[str, int]:
    for p in doc.get("slaProperties") or []:
        if isinstance(p, Mapping) and p.get("property") == name:
            value, unit = p.get("value"), str(p.get("unit") or "").lower()
            if (
                not (isinstance(value, int) and not isinstance(value, bool) and value > 0)
                or unit not in SLA_UNITS
            ):
                raise CheckInvalid(
                    f"{where}: slaProperties {name} needs a whole positive value and a unit "
                    f"({', '.join(sorted(SLA_UNITS))})"
                )
            datepart, factor = SLA_UNITS[unit]
            return datepart, value * factor
    raise CheckInvalid(f"{where}: names slaProperties {name}, which the contract doesn't declare")


def _check(doc: Mapping[str, Any], item: Mapping[str, Any], column: str | None, where: str) -> Check:
    rid = str(item.get("id") or "").strip()
    if not rid:
        raise CheckInvalid(f"{where}: every quality rule needs an id")
    if not RULE_ID.match(rid):
        raise CheckInvalid(f"{where}: rule id {rid!r} must be lower-case letters, digits and underscores")
    where = f"{where}.{rid}"
    if item.get("engine") != ENGINE:
        raise CheckInvalid(f"{where}: a custom rule's engine must be {ENGINE}")
    impl = item.get("implementation")
    if not isinstance(impl, Mapping):
        raise CheckInvalid(f"{where}: implementation must name a check and its parameters")
    name = str(impl.get("check") or "")
    if name not in CHECK_TYPES:
        raise CheckInvalid(
            f"{where}: check {name!r} is not a registered check type ({', '.join(sorted(CHECK_TYPES))}); "
            "add it to dq_checks.CHECK_TYPES first"
        )
    ctype = CHECK_TYPES[name]
    if (ctype.scope == "column") != (column is not None):
        raise CheckInvalid(f"{where}: {name} is a {ctype.scope} check; put it on the {ctype.scope}")
    params = {k: v for k, v in impl.items() if k != "check"}
    unknown = sorted(set(params) - set(ctype.params))
    if unknown:
        raise CheckInvalid(f"{where}: {name} takes no parameter {unknown[0]}")
    for k, v in params.items():
        if not PARAM_KINDS[ctype.params[k]](v):
            raise CheckInvalid(f"{where}: {name} parameter {k} has a value of the wrong kind ({v!r})")
    missing = sorted(ctype.required - set(params))
    if missing:
        raise CheckInvalid(f"{where}: {name} needs {missing[0]}")
    if ctype.one_of and not ctype.one_of & set(params):
        raise CheckInvalid(f"{where}: {name} needs at least one of {', '.join(sorted(ctype.one_of))}")
    if "min" in params and "max" in params and params["min"] > params["max"]:
        raise CheckInvalid(f"{where}: min is above max")
    model = params["model"]
    if any(ctype.params[k] in RELATION_KINDS and v == model for k, v in params.items()):
        raise CheckInvalid(f"{where}: a check compares {model} with another layer, not with itself")
    dimension, severity = str(item.get("dimension") or ""), str(item.get("severity") or "")
    if dimension not in DIMENSIONS:
        raise CheckInvalid(f"{where}: dimension must be one of {', '.join(sorted(DIMENSIONS))}")
    if severity not in SEVERITIES:
        raise CheckInvalid(f"{where}: severity must be error or warn")
    description = str(item.get("description") or "").strip()
    if not description:
        raise CheckInvalid(f"{where}: every quality rule needs a description quoting its source")
    sla = next((_sla(doc, str(v), where) for k, v in params.items() if ctype.params[k] == "sla"), None)
    return Check(rid, name, description, dimension, severity, column, params, sla)


def parse_checks(doc: Mapping[str, Any], where: str) -> tuple[Check, ...]:
    """Every dbt-expectations check in an ODCS contract, on the schema object and on its properties."""
    schema = (doc.get("schema") or [{}])[0]
    checks = [_check(doc, q, None, where) for q in schema.get("quality") or [] if is_custom(q)]
    for p in schema.get("properties") or []:
        name = str(p.get("name"))
        checks += [_check(doc, q, name, f"{where}.{name}") for q in p.get("quality") or [] if is_custom(q)]
    return tuple(checks)


def _fill(template: Any, values: Mapping[str, Any]) -> Any:
    """A template filled from `values`; a template that is one placeholder keeps the value's own type."""
    if not isinstance(template, str):
        return template
    if template.startswith("{") and template.endswith("}") and template[1:-1] in values:
        return values[template[1:-1]]
    return template.format(**values)


def _placeholders(template: Any) -> set[str]:
    import string

    if not isinstance(template, str):
        return set()
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def dbt_test(check: Check, dataset: str, contract: str) -> dict[str, Any]:
    """The dbt test a check runs (a data_tests entry), named <dataset>_<rule id> and carrying the rule id and the
    contract that declares it, like every other generated test. A test dbt runs under its column (on_column) leaves
    out column_name: dbt passes it."""
    ctype = CHECK_TYPES[check.check]
    values: dict[str, Any] = {"dataset": dataset}
    for k, v in check.params.items():
        values[k] = LAYERS[v].format(dataset=dataset) if ctype.params[k] in RELATION_KINDS else v
    if check.column is not None:
        values["column"] = '"' + check.column.replace('"', '""') + '"'
    if check.sla is not None:
        values["sla_datepart"], values["sla_interval"] = check.sla
    arguments = {
        arg: _fill(t, values)
        for arg, t in ctype.arguments.items()
        if _placeholders(t) <= set(values) and not (check.on_column and arg == "column_name")
    }
    return {
        ctype.test: {
            "name": f"{dataset}_{check.id}",
            "arguments": arguments,
            "config": {
                "severity": check.severity,
                "meta": {
                    "dimension": check.dimension,
                    "rule": check.id,
                    "contract": contract,
                    "check": check.check,
                    "checks": check.description,
                },
            },
        }
    }
