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
import re
import sys
import traceback
from collections.abc import Iterator, Mapping
from typing import Any

import atheris

with atheris.instrument_imports():
    from health_data_registry.sql_guard import SqlRefused, guard_sql

import duckdb
from sqlglot.errors import OptimizeError, ParseError

TABLES: dict[str, frozenset[str]] = {
    "deaths": frozenset({"state", "year", "cause", "deaths"}),
    "pop": frozenset({"state", "year", "population"}),
    "vax": frozenset({"state", "year", "rate"}),
}
JOINS = frozenset({frozenset({"deaths", "pop"})})
MAX_ROWS = 100
# Known findings, filed with reproducers; skipped so the harness keeps finding new ones. Each skip matches only its
# issue's exact failure (exception type, raising function, message and input shape); any other failure still
# crashes the run. Set each to False when its fix lands.
KNOWN_LONE_SURROGATE = True  # issue #56
KNOWN_OPTIMIZE_ERROR = True  # issue #57
KNOWN_REWRITES = True  # issue #71
KNOWN_GENERATE_PARSE_ERROR = True  # issue #72
KNOWN_MINUS_TILDE = True  # issue #77

# Issue #77: sqlglot writes "- ~x" as "-~x", which DuckDB lexes as one operator "-~" (or "-~~", ...). Excused only
# when the caller's text has a "~".
_MINUS_TILDE = frozenset("-" + "~" * k for k in range(1, 9))

# Issue #71: functions the guard accepts by name that sqlglot rewrites, when it writes DuckDB SQL, into functions
# (and a lambda) nobody allowlisted. Each pattern is matched against the caller's text; the rewritten names are
# allowed only in an accepted query whose text names the function that produces them.
_REWRITES: tuple[tuple[re.Pattern[str], frozenset[str], frozenset[str]], ...] = (
    (
        re.compile(r"\binitcap\b", re.IGNORECASE),
        frozenset({"array_to_string", "list_transform", "regexp_extract_all", "chr"}),
        frozenset({"LAMBDA"}),
    ),
    (
        re.compile(r"\b(?:len|length|char_length|character_length)\b", re.IGNORECASE),
        frozenset({"typeof", "octet_length"}),
        frozenset(),
    ),
    (
        re.compile(r"\b(?:current_date|current_timestamp|today)\b", re.IGNORECASE),
        frozenset({"timezone"}),
        frozenset(),
    ),
)

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


# The CTEs visible at a point in DuckDB's tree: name -> (body, the CTEs visible inside that body). A query node's
# cte_map is visible only inside that node, so a CTE defined in a subquery doesn't cover a name outside it.
CteScope = Mapping[str, tuple[Any, "CteScope"]]


def _scoped(node: Any, scope: CteScope) -> Iterator[tuple[dict[str, Any], CteScope]]:
    """Every dict in the tree, with the CTEs visible at it."""
    if isinstance(node, dict):
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict) and cte_map.get("map"):
            inner: dict[str, tuple[Any, CteScope]] = dict(scope)
            for item in cte_map["map"]:
                # A CTE's body sees the CTEs of its own WITH (itself, when recursive, and its siblings) and the outer
                # ones: a superset of what DuckDB binds, so the check errs towards a finding, never a pass.
                inner[str(item["key"]).casefold()] = (item["value"]["query_node"], inner)
            scope = inner
        yield node, scope
        for v in node.values():
            yield from _scoped(v, scope)
    elif isinstance(node, list):
        for v in node:
            yield from _scoped(v, scope)


def _reach(node: Any, scope: CteScope, seen: frozenset[str] = frozenset()) -> set[str]:
    """Every catalogued table a part of the tree reads, through the CTEs it names."""
    found: set[str] = set()
    for d, here in _scoped(node, scope):
        if d.get("type") == "BASE_TABLE":
            name = str(d.get("table_name", "")).casefold()
            if name in TABLES:
                found.add(name)
            if name in here and name not in seen:
                body, body_scope = here[name]
                found |= _reach(body, body_scope, seen | {name})
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


def _check_joins(select: dict[str, Any], ctes: CteScope) -> None:
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


def check_accepted(
    sql: str, extra_functions: frozenset[str] = frozenset(), extra_classes: frozenset[str] = frozenset()
) -> None:
    """The independent re-parse of SQL the guard accepted (the extras are for issues #71 and #77 only)."""
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
    for d, ctes in _scoped(root, {}):
        cls = d.get("class")
        if isinstance(cls, str) and cls not in ALLOWED_CLASSES and cls not in extra_classes:
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
            if fname not in ALLOWED_FUNCTIONS | ALLOWED_OPERATORS | extra_functions:
                raise Finding(f"function {fname!r} is not on the harness allowlist")
        if "from_table" in d and isinstance(d["from_table"], dict):
            for leaf in _leaves(d["from_table"], []):
                if isinstance(leaf, dict) and leaf.get("type") not in ALLOWED_TABLE_REFS:
                    raise Finding(f"table reference {leaf.get('type')} reached DuckDB")


def _raised_in(e: BaseException) -> str | None:
    """The innermost sql_guard function the exception passed through (not a comprehension or generator frame,
    which Python 3.11, the fuzz image's version, keeps and 3.12 partly inlines)."""
    names = [
        # Under mutation tests, mutmut renames each function x_<name>__mutmut_orig (or _<n>); read the real name.
        _MUTMUT_NAME.sub(r"\1", f.name)
        for f in traceback.extract_tb(e.__traceback__)
        if f.filename.endswith("sql_guard.py") and not f.name.startswith("<")
    ]
    return names[-1] if names else None


_MUTMUT_NAME = re.compile(r"^x_(.+)__mutmut_(?:orig|\d+)$")


def _is_utf8(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def known_guard_error(sql: str, e: Exception) -> bool:
    """Whether an exception escaping guard_sql is exactly one of the filed, unfixed findings."""
    where = _raised_in(e)
    if KNOWN_LONE_SURROGATE and type(e) is TypeError and not _is_utf8(sql):
        # issue #56: duckdb's binding can't take a lone surrogate
        return where == "_require_single_select" and str(e).startswith(
            "extract_statements(): incompatible function arguments"
        )
    if KNOWN_OPTIMIZE_ERROR and type(e) is OptimizeError:
        # issue #57: two sources share an alias. sqlglot raises it from the lazy Scope.selected_sources, wherever
        # in guard_sql's scope checks that is first read (_check_scope_sources, _check_columns, ...).
        last = traceback.extract_tb(e.__traceback__)[-1]
        return (
            where is not None
            and where.startswith("_")
            and last.name == "selected_sources"
            and last.filename.replace("\\", "/").endswith("optimizer/scope.py")
            and re.fullmatch(r"Alias already used: .+", str(e)) is not None
        )
    if KNOWN_GENERATE_PARSE_ERROR and type(e) is ParseError and _REWRITES[0][0].search(sql):
        # issue #72: initcap() with no argument fails in sqlglot's generator, after _parse
        return where == "guard_sql"
    return False


def known_rewrites(sql: str) -> tuple[frozenset[str], frozenset[str]] | None:
    """Issues #71 and #77: the extra functions and classes the caller's text can produce through sqlglot's output."""
    hits = [(fns, classes) for pattern, fns, classes in _REWRITES if KNOWN_REWRITES and pattern.search(sql)]
    if KNOWN_MINUS_TILDE and "~" in sql:
        hits.append((_MINUS_TILDE, frozenset()))
    if not hits:
        return None
    return frozenset().union(*(f for f, _ in hits)), frozenset().union(*(c for _, c in hits))


def TestOneInput(data: bytes) -> None:
    # surrogateescape: undecodable bytes become lone surrogates, which a JSON-RPC client can also send
    # (a "\udcff" escape), so the guard must refuse them too.
    sql = data.decode("utf-8", errors="surrogateescape")
    try:
        q = guard_sql(sql, allowed_tables=TABLES, declared_joins=JOINS, max_rows=MAX_ROWS)
    except SqlRefused as e:
        if not (e.code and e.message):
            raise Finding("a refusal without a code or a message") from e
        return
    except Exception as e:
        if known_guard_error(sql, e):
            return
        raise
    if not q.tables <= set(TABLES) or not 0 <= q.limit <= MAX_ROWS:
        raise Finding(f"GuardedQuery out of bounds: {q.tables} limit {q.limit}")
    try:
        check_accepted(q.sql)
    except Finding:
        extras = known_rewrites(sql)
        if extras is None:
            raise
        # Only issues #71 and #77's names are excused; anything else in the same query still fails.
        check_accepted(q.sql, *extras)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
