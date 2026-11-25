# SPDX-License-Identifier: Apache-2.0
"""Fuzz sql_guard.guard_sql with arbitrary text.

Property: guard_sql either refuses with SqlRefused, or returns SQL that DuckDB's own parser, read here
independently of the guard (json_serialize_sql, not sqlglot), sees as one SELECT that:

- reads only the harness's catalogued tables or its own CTEs, never a table function or a schema-qualified name;
- calls only functions on this harness's allowlist, written in DuckDB's names;
- combines rows of two different catalogued tables only when the pair is a declared join;
- ends in a LIMIT no larger than MAX_ROWS.

Any other exception, or an accepted query that breaks the property, is a finding (an uncaught exception
makes Atheris report a crash with the input).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from typing import Any

import atheris

with atheris.instrument_imports():
    from health_data_registry.sql_guard import SqlRefused, guard_sql

import duckdb
from sqlglot.errors import OptimizeError

TABLES: dict[str, frozenset[str]] = {
    "deaths": frozenset({"state", "year", "cause", "deaths"}),
    "pop": frozenset({"state", "year", "population"}),
    "vax": frozenset({"state", "year", "rate"}),
}
JOINS = frozenset({frozenset({"deaths", "pop"})})
MAX_ROWS = 100
# Known findings, filed with reproducers; skipped so the harness keeps finding new ones. Set each to False
# (or delete it) when its fix lands.
KNOWN_LONE_SURROGATE = True  # issue #56
KNOWN_OPTIMIZE_ERROR = True  # issue #57

# DuckDB's names for what the guard allows. Written out by hand, so a function that reaches DuckDB under a
# name nobody reviewed (a reader, a setting, a table function) is a finding, not a pass.
ALLOWED_FUNCTIONS = frozenset(
    {
        # aggregates
        "count", "count_star", "count_if", "countif", "sum", "avg", "mean", "min", "max", "median",
        "quantile", "quantile_cont", "quantile_disc", "approx_quantile", "approx_count_distinct",
        "stddev", "stddev_pop", "stddev_samp", "variance", "var_pop", "var_samp", "corr", "covar_pop",
        "covar_samp", "any_value", "arg_max", "arg_min", "argmax", "argmin", "max_by", "min_by",
        "arg_max_null", "arg_min_null", "first", "last", "bool_and", "bool_or", "string_agg", "listagg",
        "group_concat", "list", "array_agg", "mode", "kurtosis", "kurtosis_pop", "skewness", "regr_slope",
        "regr_intercept", "regr_r2", "regr_count", "regr_avgx", "regr_avgy", "regr_sxx", "regr_syy",
        "regr_sxy", "fsum", "favg", "sumkahan", "kahan_sum", "product", "geomean", "geometric_mean",
        "entropy", "mad",
        # window
        "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead",
        "first_value", "last_value", "nth_value",
        # numbers
        "abs", "ceil", "ceiling", "floor", "round", "trunc", "sqrt", "cbrt", "exp", "ln", "log", "log2",
        "log10", "sign", "greatest", "least", "coalesce", "nullif", "isnan", "isinf", "isfinite",
        "round_even", "even", "pi", "gcd", "lcm", "factorial", "fdiv", "fmod", "ifnull", "pow", "power",
        "divide",
        # text
        "lower", "upper", "lcase", "ucase", "trim", "ltrim", "rtrim", "length", "len", "char_length",
        "bit_length", "substring", "substr", "replace", "concat", "concat_ws", "left", "right",
        "split_part", "strpos", "instr", "position", "lpad", "rpad", "initcap", "reverse",
        "starts_with", "ends_with", "suffix", "prefix", "contains", "regexp_matches",
        "regexp_full_match", "regexp_extract", "regexp_replace", "regexp_split_to_array", "levenshtein",
        "strip_accents", "string_split", "str_split", "printf", "format", "lower_case", "upper_case",
        # dates and times
        "year", "quarter", "month", "week", "weekofyear", "day", "dayofmonth", "dayofweek", "isodow",
        "dayofyear", "hour", "minute", "second", "date_part", "datepart", "date_trunc", "datetrunc",
        "date_diff", "datediff", "date_add", "date_sub", "strftime", "strptime", "try_strptime",
        "current_date", "current_timestamp", "get_current_timestamp", "now", "today",
        "current_localtimestamp", "make_date", "make_timestamp", "last_day", "epoch", "epoch_ms",
        "dayname", "monthname", "yearweek", "isoyear", "century", "decade", "era", "to_years",
        "to_months", "to_days", "age", "time_bucket", "to_timestamp",
        # lists and structs built inline
        "list_value", "struct_pack", "row", "array_length", "list_contains", "array_contains",
        "list_has", "array_has", "array_value",
    }
)  # fmt: skip

# Operators DuckDB serialises as functions (is_operator, or the bracket and string-match helpers).
ALLOWED_OPERATORS = frozenset(
    {
        "+", "-", "*", "/", "//", "%", "**", "^", "||", "&", "|", "<<", ">>", "~", "xor", "~~", "!~~",
        "~~*", "!~~*", "~~~", "!~~~", "array_extract", "array_slice", "list_extract", "list_slice",
        "struct_extract", "struct_extract_at", "like_escape", "not_like_escape", "ilike_escape",
        "not_ilike_escape",
    }
)  # fmt: skip

# Expression classes DuckDB's tree may hold in an accepted query.
ALLOWED_CLASSES = frozenset(
    {
        "CONSTANT", "COLUMN_REF", "FUNCTION", "WINDOW", "SUBQUERY", "COMPARISON", "CONJUNCTION", "OPERATOR",
        "CASE", "CAST", "BETWEEN", "STAR", "COLLATE", "TYPE",
    }
)  # fmt: skip

# Table references (from_table types) DuckDB may hold in an accepted query.
ALLOWED_TABLE_REFS = frozenset({"BASE_TABLE", "JOIN", "SUBQUERY", "EMPTY", "EXPRESSION_LIST"})

_ORACLE = duckdb.connect(":memory:")


class Finding(AssertionError):
    """An accepted query broke the property."""


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _ctes(tree: Any) -> dict[str, list[Any]]:
    found: dict[str, list[Any]] = {}
    for d in _walk(tree):
        cte_map = d.get("cte_map")
        if isinstance(cte_map, dict):
            for item in cte_map.get("map", []):
                found.setdefault(str(item["key"]).casefold(), []).append(item["value"]["query_node"])
    return found


def _reach(node: Any, ctes: dict[str, list[Any]], seen: frozenset[str] = frozenset()) -> set[str]:
    """Every catalogued table a part of the tree reads, through the CTEs it names."""
    found: set[str] = set()
    for d in _walk(node):
        if d.get("type") == "BASE_TABLE":
            name = str(d.get("table_name", "")).casefold()
            if name in TABLES:
                found.add(name)
            if name in ctes and name not in seen:
                for body in ctes[name]:
                    found |= _reach(body, ctes, seen | {name})
    return found


def _leaves(ref: Any, conditions: list[Any]) -> list[Any]:
    if isinstance(ref, dict) and ref.get("type") == "JOIN":
        conditions.append(ref.get("condition"))
        return _leaves(ref.get("left"), conditions) + _leaves(ref.get("right"), conditions)
    return [ref]


def _direct_subqueries(node: Any) -> Iterator[dict[str, Any]]:
    """Subquery expressions in an expression tree, without descending into them."""
    if isinstance(node, dict):
        if node.get("class") == "SUBQUERY":
            yield node
            return
        for v in node.values():
            yield from _direct_subqueries(v)
    elif isinstance(node, list):
        for v in node:
            yield from _direct_subqueries(v)


def _check_joins(select: dict[str, Any], ctes: dict[str, list[Any]]) -> None:
    conditions: list[Any] = []
    leaves = _leaves(select.get("from_table"), conditions)
    inputs = [_reach(leaf, ctes) for leaf in leaves]
    exprs = [select.get(k) for k in ("select_list", "where_clause", "group_expressions", "having", "qualify")]
    exprs += [select.get("modifiers"), conditions]
    inputs += [_reach(sq, ctes) for sq in _direct_subqueries(exprs)]
    for i, left in enumerate(inputs):
        for right in inputs[i + 1 :]:
            for a in left:
                for b in right:
                    if a != b and frozenset({a, b}) not in JOINS:
                        raise Finding(f"undeclared join between {a} and {b} was accepted")


def _check_limit(root: dict[str, Any]) -> None:
    for mod in root.get("modifiers", []):
        if mod.get("type") == "LIMIT_MODIFIER":
            limit = mod.get("limit") or {}
            literal = limit.get("literal") or {}
            if limit.get("class") != "CONSTANT" or not str(literal.get("text", "")).isdigit():
                raise Finding(f"outer LIMIT is not a whole number: {limit}")
            if int(literal["text"]) > MAX_ROWS:
                raise Finding(f"outer LIMIT {literal['text']} is above {MAX_ROWS}")
            if mod.get("offset") not in (None, {}) and (mod["offset"] or {}).get("class") != "CONSTANT":
                raise Finding("OFFSET is not a constant")
            return
    raise Finding("accepted query has no outer LIMIT")


def check_accepted(sql: str) -> None:
    """The independent re-parse of SQL the guard accepted."""
    statements = duckdb.extract_statements(sql)
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise Finding("accepted SQL is not exactly one SELECT")
    doc = json.loads(_ORACLE.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])  # type: ignore[index]
    if doc.get("error") or len(doc.get("statements", [])) != 1:
        raise Finding(f"DuckDB could not re-read the accepted SQL: {doc.get('error_message')}")
    root = doc["statements"][0]["node"]
    if root.get("type") not in ("SELECT_NODE", "SET_OPERATION_NODE"):
        raise Finding(f"accepted SQL is a {root.get('type')}")
    _check_limit(root)
    ctes = _ctes(root)
    for d in _walk(root):
        cls = d.get("class")
        if isinstance(cls, str) and cls not in ALLOWED_CLASSES:
            raise Finding(f"expression {cls} ({d.get('type')}) reached DuckDB")
        kind = d.get("type")
        if "from_table" in d:
            _check_joins(d, ctes)
        if kind in ("TABLE_FUNCTION", "PIVOT", "SHOW_REF", "COLUMN_DATA", "DELIM_GET"):
            raise Finding(f"table reference {kind} reached DuckDB")
        if kind == "BASE_TABLE":
            name = str(d.get("table_name", "")).casefold()
            if d.get("schema_name") or d.get("catalog_name"):
                raise Finding(f"schema-qualified table {name} reached DuckDB")
            if name not in TABLES and name not in ctes:
                raise Finding(f"uncatalogued table {name} reached DuckDB")
        if cls in ("FUNCTION", "WINDOW"):
            fname = str(d.get("function_name", "")).casefold()
            if d.get("schema") or d.get("catalog"):
                raise Finding(f"schema-qualified function {fname} reached DuckDB")
            if fname not in ALLOWED_FUNCTIONS and fname not in ALLOWED_OPERATORS:
                raise Finding(f"function {fname!r} is not on the harness allowlist")
        if "from_table" in d and isinstance(d["from_table"], dict):
            for leaf in _leaves(d["from_table"], []):
                if isinstance(leaf, dict) and leaf.get("type") not in ALLOWED_TABLE_REFS:
                    raise Finding(f"table reference {leaf.get('type')} reached DuckDB")


def TestOneInput(data: bytes) -> None:
    # surrogateescape: undecodable bytes become lone surrogates, which a JSON-RPC client can also send
    # (a "\udcff" escape), so the guard must refuse them too.
    sql = data.decode("utf-8", errors="surrogateescape")
    if KNOWN_LONE_SURROGATE and sql != data.decode("utf-8", errors="ignore"):
        return  # issue #56: guard_sql raises TypeError on a lone surrogate; remove this skip with its fix
    try:
        q = guard_sql(sql, allowed_tables=TABLES, declared_joins=JOINS, max_rows=MAX_ROWS)
    except SqlRefused as e:
        if not (e.code and e.message):
            raise Finding("a refusal without a code or a message") from e
        return
    except OptimizeError:
        if KNOWN_OPTIMIZE_ERROR:
            return  # issue #57: a repeated alias escapes as OptimizeError; remove this skip with its fix
        raise
    if not q.tables <= set(TABLES) or not 0 <= q.limit <= MAX_ROWS:
        raise Finding(f"GuardedQuery out of bounds: {q.tables} limit {q.limit}")
    check_accepted(q.sql)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
