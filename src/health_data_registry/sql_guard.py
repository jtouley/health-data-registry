# SPDX-License-Identifier: Apache-2.0
# Adapted from the author's earlier MIT-licensed work (see NOTICE).
"""Read-only SQL over catalogued public datasets: a static guard, then a locked-down engine.

``query_sql`` is the escape hatch for questions no metric covers, so the SQL it runs is typed by a
model or a person. Two layers stand between that text and anything but a read of the catalogue:

1. :func:`guard_sql` parses the text with sqlglot (DuckDB dialect) and fails closed. Every node in
   the tree must be of an allowed type, and every function must be on an **allowlist** of scalar,
   aggregate and window functions, so a function this module has never heard of (a new
   ``read_*``, a ``*_scan``, ``getenv``) is refused rather than passed through. It accepts exactly
   one ``SELECT`` (``WITH``, ``UNION``/``INTERSECT``/``EXCEPT``, subqueries, joins and window
   functions included), reads only catalogued tables, checks columns where they can be resolved,
   allows a join between two different catalogued tables only when that pair is declared, and
   injects or clamps the outer ``LIMIT``. What runs is SQL regenerated from the checked tree
   (comments dropped), never the caller's text, and DuckDB's own parser must agree that it is a
   single ``SELECT``.
2. :func:`harden` locks the connection the views live on: no file or network access (except the
   exact files the views read, via ``allowed_paths``), no extension install or autoload, capped
   memory, threads and temp spill, then ``lock_configuration`` so no statement can undo it. Even a
   query that slipped past the guard cannot read ``/etc/passwd``, COPY, ATTACH a file or INSTALL.
   :func:`run_guarded` re-checks that it was handed one ``SELECT`` and interrupts it at a deadline.

Refusals raise :class:`SqlRefused` with a short machine ``code`` and a sentence a clinician can act
on. Messages never quote a cell value or a file path.

Changes from the source: the source admitted one aggregate shape over a single patient table (the
privacy rules of a patient-data app); this module admits general read-only SELECTs over public,
population-level tables, so joins, CTEs, set operations and window functions are allowed and the
privacy-specific checks (patient ID, PHI coarsening, quantile grid) are gone. The engine settings
follow the source's sandbox and its design record (ADR013 section 3: ``allowed_paths``, extension
flags, ``max_temp_directory_size='0B'``, ``lock_configuration``), and its engine-level escape tests
are ported. ``polars``/``structlog`` are replaced by plain tuples and stdlib ``logging``.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Final

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, traverse_scope

from health_data_registry.registry import label_key

logger = logging.getLogger(__name__)

MAX_SQL_CHARS: Final = 20_000
MAX_AST_NODES: Final = 3_000
DEFAULT_MEMORY_LIMIT: Final = "2GB"
DEFAULT_THREADS: Final = 2

_DIALECT: Final = "duckdb"


class SqlRefused(Exception):
    """The query was not run. ``code`` is for programs; ``message`` is for the person asking."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GuardedQuery:
    """A query that passed :func:`guard_sql`: the SQL to run, the catalogued tables it reads, and its
    row limit."""

    sql: str
    tables: frozenset[str]
    limit: int


# ---------------------------------------------------------------------------
# What a query may contain
# ---------------------------------------------------------------------------


def _classes(*names: str) -> tuple[type[exp.Expr], ...]:
    """The sqlglot expression classes with these names that exist in the installed version.

    Looked up by name so the module imports on every sqlglot in the supported range; a name the
    installed version lacks is simply not allowed (fail closed).
    """
    found: list[type[exp.Expr]] = []
    for name in names:
        cls = getattr(exp, name, None)
        if isinstance(cls, type) and issubclass(cls, exp.Expr):
            found.append(cls)
    return tuple(found)


# Query structure: clauses, references, literals and operators. No function is in this list.
_STRUCTURE: Final = _classes(
    "Select", "Union", "Intersect", "Except", "Subquery", "With", "CTE", "From", "Join", "Table",
    "TableAlias", "Where", "Group", "Having", "Qualify", "Order", "Ordered", "Limit", "Offset",
    "Alias", "Column", "Identifier", "Star", "Literal", "Boolean", "Null", "Paren", "Tuple",
    "Distinct", "Filter", "Window", "WindowSpec", "Interval", "DataType", "PropertyEQ", "Bracket",
    "Add", "Sub", "Mul", "Div", "IntDiv", "Mod", "Neg", "Pow", "DPipe",
    "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "NullSafeEQ", "NullSafeNEQ", "Is", "In", "Between",
    "Like", "ILike", "SimilarTo", "And", "Or", "Not", "Xor", "Any", "All", "Exists",
    "BitwiseAnd", "BitwiseOr", "BitwiseXor", "BitwiseNot", "BitwiseLeftShift", "BitwiseRightShift",
    "Case", "If", "Cast", "TryCast",
)  # fmt: skip

# Scalar, aggregate and window functions sqlglot knows by type. None reads a file, a URL, a
# setting, a variable or the catalog, and none returns a table.
_FUNCTIONS: Final = _classes(
    # aggregates
    "Count", "CountIf", "Sum", "Avg", "Min", "Max", "Median", "PercentileCont", "PercentileDisc",
    "Quantile", "ApproxQuantile", "ApproxDistinct", "Stddev", "StddevPop", "StddevSamp",
    "Variance", "VariancePop", "Corr", "CovarPop", "CovarSamp", "AnyValue", "ArgMax", "ArgMin",
    "First", "Last", "LogicalAnd", "LogicalOr", "GroupConcat", "ArrayAgg", "Mode", "Kurtosis",
    "Skewness", "RegrSlope", "RegrIntercept", "RegrR2", "RegrCount",
    # window
    "RowNumber", "Rank", "DenseRank", "PercentRank", "CumeDist", "Ntile", "Lag", "Lead",
    "FirstValue", "LastValue", "NthValue",
    # numbers
    "Abs", "Ceil", "Floor", "Round", "Trunc", "Sqrt", "Cbrt", "Exp", "Ln", "Log", "Log2", "Log10",
    "Sign", "Greatest", "Least", "Coalesce", "Nullif", "SafeDivide", "IsNan", "IsInf",
    # text
    "Lower", "Upper", "Trim", "Length", "Substring", "Replace", "Concat", "ConcatWs", "Left",
    "Right", "SplitPart", "StrPosition", "Pad", "Initcap", "Reverse", "StartsWith", "EndsWith",
    "Contains", "RegexpLike", "RegexpFullMatch", "RegexpILike", "RegexpExtract", "RegexpReplace",
    "Levenshtein",
    # dates and times
    "Year", "Quarter", "Month", "Week", "WeekOfYear", "Day", "DayOfMonth", "DayOfWeek",
    "DayOfWeekIso", "DayOfYear", "Hour", "Minute", "Second", "Extract", "TimestampTrunc",
    "DateTrunc", "DateDiff", "DateAdd", "DateSub", "TimestampDiff", "TimestampAdd", "TimestampSub",
    "TimeToStr", "StrToTime", "StrToDate", "TsOrDsToDate", "CurrentDate", "CurrentTimestamp",
    "Date", "DateFromParts", "LastDay", "MonthsBetween",
    # lists and structs built inline (no table output)
    "Array", "Struct", "ArraySize", "ArrayContains",
)  # fmt: skip

# DuckDB functions sqlglot does not type (they parse as Anonymous), by lower-case name.
_ANONYMOUS_FUNCTIONS: Final = frozenset(
    {
        "date_part", "datepart", "make_date", "make_timestamp", "epoch", "dayname", "monthname",
        "yearweek", "isodow", "isoyear", "century", "decade", "era", "to_years", "to_months",
        "to_days", "age", "try_strptime", "today", "now", "current_localtimestamp",
        "ifnull", "round_even", "even", "pi", "isfinite", "gcd", "lcm", "factorial", "fdiv", "fmod",
        "lcase", "ucase", "ltrim", "rtrim", "lpad", "rpad", "instr", "strpos", "position", "prefix",
        "suffix", "strip_accents", "regexp_split_to_array", "string_split", "str_split", "printf",
        "format", "bit_length", "char_length", "len", "list_value", "array_length",
        "fsum", "favg", "sumkahan", "kahan_sum", "product", "geomean", "geometric_mean", "entropy",
        "mad", "quantile", "quantile_disc", "quantile_cont", "bool_and", "bool_or", "string_agg",
        "list", "max_by", "min_by", "arg_max_null", "arg_min_null", "count_star", "var_samp",
        "var_pop", "stddev_samp", "stddev_pop", "regr_avgx", "regr_avgy", "regr_sxx", "regr_syy",
        "regr_sxy",
    }
)  # fmt: skip

# Keyword arguments sqlglot keeps as Var: units, and ALL in GROUP BY ALL / ORDER BY ALL.
_VAR_PARENTS: Final = _classes(
    "Extract", "TimestampTrunc", "DateTrunc", "DateDiff", "DateAdd", "DateSub", "TimestampDiff",
    "TimestampAdd", "TimestampSub", "Interval", "Group", "Ordered", "WindowSpec", "LastDay",
)  # fmt: skip

_ALLOWED: Final = _STRUCTURE + _FUNCTIONS

# Clauses of a SELECT (and of a set operation) the guard understands; any other is refused.
_SELECT_CLAUSES: Final = frozenset(
    {"with_", "expressions", "distinct", "from_", "joins", "where", "group", "having", "qualify",
     "windows", "order", "limit", "offset", "this", "expression", "by_name", "side", "kind", "on"}
)  # fmt: skip
_TABLE_CLAUSES: Final = frozenset({"this", "alias"})

READ_ONLY = "Only SELECT queries can run: the data is read-only."
ONE_STATEMENT = "Send one SELECT query at a time."
TIMED_OUT = "The query took too long and was stopped. Narrow it with WHERE, or ask for fewer groups."


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def guard_sql(
    sql: str,
    *,
    allowed_tables: Mapping[str, frozenset[str]],
    declared_joins: frozenset[frozenset[str]] = frozenset(),
    max_rows: int = 5000,
    non_additive: Mapping[str, frozenset[str]] | None = None,
    ordered: bool = False,
) -> GuardedQuery:
    """Check ``sql`` and return the SQL that may run.

    Args:
        sql: one query, as typed.
        allowed_tables: catalogued table (view) name -> its column names. Names compare
            case-insensitively, as DuckDB's do.
        declared_joins: pairs of table names a contract declares joinable.
        max_rows: the most rows the outer query may return; a larger or missing LIMIT becomes this.
        non_additive: catalogued table name -> the columns whose values overlap or count different things
            (the semantic model's non_additive dimensions). A sum over such a table must filter each of them
            to one value or group by it, the same rule the metric compiler applies (hard rule 2).
        ordered: give the outer query a total order, so the rows and the rows the LIMIT keeps come in the same
            order on every run (answers cut into pages need that): ``ORDER BY ALL`` when it has no ORDER BY, and
            otherwise every output column after its own keys as tie-breakers.

    Raises:
        SqlRefused: anything outside the read-only shape, with a plain-language reason.
        ValueError: ``max_rows`` is not positive.
    """
    if max_rows < 1:
        raise ValueError(f"max_rows must be positive, got {max_rows!r}")
    tables = {name.casefold(): frozenset(c.casefold() for c in cols) for name, cols in allowed_tables.items()}
    joins = frozenset(frozenset(t.casefold() for t in pair) for pair in declared_joins)
    loose = {
        name.casefold(): frozenset(c.casefold() for c in cols) for name, cols in (non_additive or {}).items()
    }

    tree = _parse(sql)
    _check_nodes(tree)
    _check_tables(tree, tables)
    scopes = _scopes(tree)
    used: set[str] = set()
    for scope in scopes:
        _check_scope_sources(scope, tables, used)
        _check_columns(scope, tables)
        _check_joins(scope, tables, joins)
        _check_sums(scope, tables, loose)
    limit = _apply_limit(tree, max_rows)
    if ordered:
        _total_order(tree)

    for node in tree.walk():
        node.comments = None  # the generated SQL carries no caller text outside literals and names
    generated = tree.sql(dialect=_DIALECT)
    _require_single_select(generated)
    return GuardedQuery(sql=generated, tables=frozenset(used), limit=limit)


def _total_order(tree: exp.Query) -> None:
    """Make the outer ORDER BY a total order, so ties never come back in a different order on another run.

    No ORDER BY: ``ORDER BY ALL``. An ORDER BY of its own (which may have ties, e.g. ``ORDER BY year`` over many
    places): its keys first, then every output column by position, or, when the select list has a star, every
    input column (``COLUMNS(*)``), which decides every output column.
    """
    order = tree.args.get("order")
    if order is None:
        tree.set("order", exp.Order(expressions=[exp.Ordered(this=exp.var("ALL"), nulls_first=False)]))
        return
    if any(isinstance(o.this, exp.Var) and o.this.name.upper() == "ALL" for o in order.expressions):
        return  # already total
    selects = tree.selects
    if any(
        isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
        for e in selects
    ):
        if isinstance(tree, exp.Select):
            star = exp.Anonymous(this="COLUMNS", expressions=[exp.Star()])
            order.append("expressions", exp.Ordered(this=star, nulls_first=False))
        return  # a set operation over a star: its own order stands (its output columns can't be counted here)
    for i in range(1, len(selects) + 1):
        order.append("expressions", exp.Ordered(this=exp.Literal.number(i), nulls_first=False))


def _parse(sql: str) -> exp.Query:
    if not sql.strip():
        raise SqlRefused("empty", "The query is empty.")
    if len(sql) > MAX_SQL_CHARS:
        raise SqlRefused("too_long", f"The query is longer than {MAX_SQL_CHARS:,} characters. Split it up.")
    _require_single_select(sql)
    try:
        trees = sqlglot.parse(sql, read=_DIALECT)
    except (SqlglotError, ValueError, RecursionError) as e:
        raise SqlRefused("parse_error", "The query is not valid SQL.") from e
    statements = [t for t in trees if t is not None]
    if len(statements) != 1:
        raise SqlRefused("multiple_statements", ONE_STATEMENT)
    tree = statements[0]
    if not isinstance(tree, exp.Select | exp.SetOperation):
        raise SqlRefused("not_select", READ_ONLY)
    if sum(1 for _ in tree.walk()) > MAX_AST_NODES:
        raise SqlRefused("too_complex", "The query is too long to check. Split it into smaller questions.")
    return tree


def _require_single_select(sql: str) -> None:
    """DuckDB's own parser must see exactly one SELECT (defence in depth against a misparse)."""
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as e:
        raise SqlRefused("parse_error", "The query is not valid SQL.") from e
    if len(statements) != 1:
        raise SqlRefused("multiple_statements" if statements else "empty", ONE_STATEMENT)
    try:
        kind = statements[0].type
    except ValueError as e:  # a statement type this duckdb build has no enum member for
        raise SqlRefused("not_select", READ_ONLY) from e
    if kind != duckdb.StatementType.SELECT:
        raise SqlRefused("not_select", READ_ONLY)


def _function_name(node: exp.Expr) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name)
    if isinstance(node, exp.Func):
        return node.sql_name().lower()
    return node.key


# Statement words never used as a bare name (SELECT 1 COPY parses as an alias named copy): refused so the query
# that runs never even reads like a write, attach or install. A quoted name ("update") is an ordinary name: it is
# regenerated quoted, so it can't read as a statement.
_STATEMENT_WORDS = frozenset(
    {"copy", "drop", "attach", "detach", "install", "load", "pragma", "export", "import", "call", "insert",
     "update", "delete", "create", "alter"}
)  # fmt: skip


def _check_nodes(tree: exp.Expr) -> None:
    """Fail closed: every node is known structure or an allowlisted function."""
    for node in tree.walk():
        if (
            isinstance(node, exp.Identifier)
            and not node.args.get("quoted")  # a quoted name stays quoted in the SQL that runs
            and str(node.name).casefold() in _STATEMENT_WORDS
        ):
            raise SqlRefused(
                "forbidden_construct",
                f"{node.name} can't be used as a name here: choose another name for the column or table.",
            )
        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            raise SqlRefused(
                "table_function",
                "Only catalogued tables can be read: table functions and file or web reads are not allowed.",
            )
        if isinstance(node, exp.Fetch | exp.LimitOptions):
            raise SqlRefused("bad_limit", "Use a plain LIMIT with a whole number of rows.")
        if isinstance(node, exp.Placeholder | exp.Parameter):
            raise SqlRefused(
                "parameter", "Write values into the query itself: parameters like ? or $1 are not used."
            )
        if isinstance(node, exp.Var):
            if isinstance(node.parent, _VAR_PARENTS):
                continue
            raise SqlRefused("forbidden_construct", f"This is not supported here: {node.name[:40]}.")
        if isinstance(node, exp.Anonymous):
            if str(node.name).casefold() in _ANONYMOUS_FUNCTIONS:
                continue
            raise SqlRefused(
                "forbidden_function", f"The function {_function_name(node)}() is not allowed here."
            )
        if isinstance(node, _ALLOWED):
            if isinstance(node, exp.Select | exp.SetOperation):
                _check_clauses(node)
            continue
        if isinstance(node, exp.Func):
            raise SqlRefused(
                "forbidden_function", f"The function {_function_name(node)}() is not allowed here."
            )
        if isinstance(node, exp.Into):
            raise SqlRefused("not_select", READ_ONLY)
        raise SqlRefused(
            "forbidden_construct", f"This part of the query is not supported: {node.key.upper()}."
        )


def _check_clauses(node: exp.Expr) -> None:
    used = {k for k, v in node.args.items() if v not in (None, [], False)}
    extra = used - _SELECT_CLAUSES
    if "into" in extra:
        raise SqlRefused("not_select", READ_ONLY)
    if extra:
        raise SqlRefused(
            "forbidden_construct",
            f"This part of the query is not supported: {', '.join(sorted(extra)).upper()}.",
        )


def _check_tables(tree: exp.Expr, tables: Mapping[str, frozenset[str]]) -> None:
    """Every table reference is a catalogued table or a CTE of the query, written plainly."""
    ctes = {cte.alias_or_name.casefold() for cte in tree.find_all(exp.CTE)}
    shadowing = ctes & set(tables)
    if shadowing:
        raise SqlRefused(
            "forbidden_construct",
            f"A WITH name can't reuse the table name {sorted(shadowing)[0]}: choose another name.",
        )
    for table in tree.find_all(exp.Table):
        extra = {k for k, v in table.args.items() if v not in (None, [], False)} - _TABLE_CLAUSES
        if table.args.get("db") or table.args.get("catalog"):
            raise SqlRefused("unknown_table", f"Name the table on its own, without a schema: {table.name}.")
        if extra:
            raise SqlRefused(
                "forbidden_construct",
                f"Read the table {table.name} as it is (no sampling, pivots or options).",
            )
        name = table.name.casefold()
        if name not in tables and name not in ctes:
            raise SqlRefused("unknown_table", f"There is no catalogued table named {table.name}.")


def _scopes(tree: exp.Expr) -> list[Scope]:
    try:
        return list(traverse_scope(tree))
    except (SqlglotError, ValueError, RecursionError) as e:  # pragma: no cover - defensive
        raise SqlRefused("parse_error", "The query's structure could not be checked.") from e


def _check_scope_sources(scope: Scope, tables: Mapping[str, frozenset[str]], used: set[str]) -> None:
    for _node, source in scope.selected_sources.values():
        if isinstance(source, exp.Table):
            name = source.name.casefold()
            if name not in tables:  # a CTE name used outside the WITH that defines it
                raise SqlRefused("unknown_table", f"There is no catalogued table named {source.name}.")
            used.add(name)


def _resolve_qualifier(scope: Scope | None, qualifier: str) -> exp.Table | Scope | None:
    """The source a ``qualifier.column`` names, looking outward for correlated subqueries."""
    wanted = qualifier.casefold()
    while scope is not None:
        for name, (_node, source) in scope.selected_sources.items():
            if name.casefold() == wanted:
                return source
        scope = scope.parent
    return None


def _output_aliases(scope: Scope) -> set[str]:
    select = scope.expression
    if not isinstance(select, exp.Select):
        return set()
    return {p.alias.casefold() for p in select.expressions if isinstance(p, exp.Alias)}


def _unqualified_known(scope: Scope, name: str, tables: Mapping[str, frozenset[str]]) -> bool:
    """False only when the column is certainly unknown: every source in reach is a catalogued table
    and none of them, nor an output alias, has that name."""
    current: Scope | None = scope
    while current is not None:
        if name in _output_aliases(current):
            return True
        sources = [source for _node, source in current.selected_sources.values()]
        if any(not isinstance(s, exp.Table) for s in sources):
            return True  # a derived table or CTE: its columns are not resolved here
        if any(
            name in tables.get(s.name.casefold(), frozenset()) for s in sources if isinstance(s, exp.Table)
        ):
            return True
        current = current.parent
    return False


def _check_columns(scope: Scope, tables: Mapping[str, frozenset[str]]) -> None:
    if isinstance(scope.expression, exp.SetOperation):
        return  # ORDER BY of a set operation names its output columns
    stars = [c for c in scope.stars if isinstance(c, exp.Column)]
    for column in [*scope.columns, *stars]:
        if column.find_ancestor(exp.Select, exp.SetOperation) is not scope.expression:
            continue  # inside a subquery: checked in that subquery's own scope
        qualifier = column.table
        if qualifier:
            source = _resolve_qualifier(scope, qualifier)
            if source is None:
                raise SqlRefused(
                    "unknown_column", f"There is no table or alias named {qualifier} in this query."
                )
            if isinstance(column.this, exp.Star) or not isinstance(source, exp.Table):
                continue
            if column.name.casefold() not in tables.get(source.name.casefold(), frozenset()):
                raise SqlRefused(
                    "unknown_column", f"The table {source.name} has no column named {column.name}."
                )
            continue
        if isinstance(column.this, exp.Star):
            continue
        if not _unqualified_known(scope, column.name.casefold(), tables):
            raise SqlRefused(
                "unknown_column", f"There is no column named {column.name} in the tables this query reads."
            )


def _reachable(
    scope: Scope, tables: Mapping[str, frozenset[str]], seen: frozenset[int] = frozenset()
) -> set[str]:
    """Every catalogued table a scope reads, through its CTEs, derived tables and subqueries."""
    if id(scope) in seen:  # a recursive CTE refers to itself
        return set()
    seen = seen | {id(scope)}
    found: set[str] = set()
    for _node, source in scope.selected_sources.values():
        if isinstance(source, exp.Table):
            if source.name.casefold() in tables:
                found.add(source.name.casefold())
        else:
            found |= _reachable(source, tables, seen)
    for child in (*scope.subquery_scopes, *scope.set_operation_scopes):
        found |= _reachable(child, tables, seen)
    return found


def _check_joins(
    scope: Scope, tables: Mapping[str, frozenset[str]], joins: frozenset[frozenset[str]]
) -> None:
    """Rows of two different catalogued tables are combined only when the pair is declared.

    The inputs of one SELECT are its FROM/JOIN sources and its subqueries (IN, EXISTS, scalar);
    a set operation stacks rows rather than combining them, so its branches are checked on their own.
    """
    if isinstance(scope.expression, exp.SetOperation):
        return
    inputs = [
        _reachable(source, tables) if isinstance(source, Scope) else {source.name.casefold()} & set(tables)
        for _node, source in scope.selected_sources.values()
    ]
    inputs += [_reachable(child, tables) for child in scope.subquery_scopes]
    for i, left in enumerate(inputs):
        for right in inputs[i + 1 :]:
            for a in sorted(left):
                for b in sorted(right):
                    if a != b and frozenset({a, b}) not in joins:
                        raise SqlRefused(
                            "undeclared_join",
                            f"The tables {a} and {b} can't be combined: their contracts don't declare a join "
                            "between them, so rows may not line up. Query each one on its own.",
                        )


# Aggregates that add values up. Other aggregates (count, avg, min, max) don't add a column's values together.
_SUM_FUNCTIONS: Final = frozenset({"fsum", "sumkahan", "kahan_sum"})


def _bare(node: exp.Expr) -> exp.Expr:
    while isinstance(node, exp.Lower | exp.Upper | exp.Trim | exp.Paren):
        node = node.this
    return node


def _conjuncts(node: exp.Expr) -> Iterator[exp.Expr]:
    node = node.unnest()
    if isinstance(node, exp.And):
        yield from _conjuncts(node.left)
        yield from _conjuncts(node.right)
    else:
        yield node


def pinned_columns(condition: exp.Expr | None) -> frozenset[tuple[str, str]]:
    """(qualifier, column) pairs a condition fixes to one value, both casefolded ('' when unqualified).

    A column is pinned by `column = 'literal'` (the column may be wrapped in lower, upper or trim) or by
    `column IN (...)` whose literals are one label spelled several ways (registry.label_key: case, edge
    spaces and dots, e.g. 'US RESIDENTS' and 'U.S. RESIDENTS'), joined by AND. Anything under OR or NOT, or a
    comparison with several different values, pins nothing. The metric compiler (registry.filter_pins) and
    query_sql both use this one rule.
    """
    pinned: set[tuple[str, str]] = set()
    if condition is None:
        return frozenset()
    for c in _conjuncts(condition):
        if isinstance(c, exp.EQ):
            pair = [_bare(c.left), _bare(c.right)]
        elif isinstance(c, exp.In) and c.expressions and not c.args.get("query"):
            values = c.expressions
            if not all(isinstance(v, exp.Literal) for v in values):
                continue
            if len({label_key(v.name) for v in values}) != 1:
                continue
            pair = [_bare(c.this), values[0]]
        else:
            continue
        cols = [x for x in pair if isinstance(x, exp.Column)]
        if len(cols) == 1 and any(isinstance(x, exp.Literal) for x in pair):
            pinned.add((cols[0].table.casefold(), cols[0].name.casefold()))
    return frozenset(pinned)


def _is_sum(node: exp.Expr) -> bool:
    return isinstance(node, exp.Sum) or (
        isinstance(node, exp.Anonymous) and str(node.name).casefold() in _SUM_FUNCTIONS
    )


def _grouped_columns(select: exp.Select) -> set[tuple[str, str]]:
    """(qualifier, column) pairs a SELECT groups by: by column, output alias, position or GROUP BY ALL."""
    group = select.args.get("group")
    if group is None:
        return set()
    outputs = select.expressions
    keys: list[exp.Expr] = list(group.expressions)
    if group.args.get("all"):
        keys += [o for o in outputs if not o.find(exp.AggFunc)]
    aliases = {o.alias.casefold(): o.this for o in outputs if isinstance(o, exp.Alias)}
    out: set[tuple[str, str]] = set()
    for key in keys:
        position = _literal_int(key)
        if position is not None and 1 <= position <= len(outputs):
            key = outputs[position - 1]
        if isinstance(key, exp.Alias):
            key = key.this
        key = _bare(key)
        if isinstance(key, exp.Column) and not key.table and key.name.casefold() in aliases:
            key = _bare(aliases[key.name.casefold()])
        if isinstance(key, exp.Column):
            out.add((key.table.casefold(), key.name.casefold()))
    return out


def _check_sums(
    scope: Scope, tables: Mapping[str, frozenset[str]], loose: Mapping[str, frozenset[str]]
) -> None:
    """Hard rule 2 for typed SQL: a sum never adds across values that overlap or count different things.

    Every column the semantic model marks non-additive, in each catalogued table the summing SELECT reads, must
    be filtered to one value in WHERE (pinned_columns) or grouped by (GROUP BY, or PARTITION BY for a window
    sum). A sum over a subquery or CTE built from such a table is refused: its rows can't be traced back here.
    """
    select = scope.expression
    if not isinstance(select, exp.Select) or not loose:
        return
    sums = [
        n for n in select.walk() if _is_sum(n) and n.find_ancestor(exp.Select, exp.SetOperation) is select
    ]
    if not sums:
        return
    where = select.args.get("where")
    pinned = pinned_columns(where.this if where is not None else None)
    grouped = _grouped_columns(select)
    for alias, (_node, source) in scope.selected_sources.items():
        if not isinstance(source, exp.Table):
            if any(loose.get(t) for t in _reachable(source, tables)):
                raise SqlRefused(
                    "non_additive",
                    "This query adds up rows of an inner query or WITH table, so it can't be checked that "
                    "values which overlap or count different things (a Total row beside its parts, different "
                    "conditions) are kept apart. Add up directly from the catalogued table instead.",
                )
            continue
        cols = loose.get(source.name.casefold(), frozenset())
        for n in sums:
            window = n.find_ancestor(exp.Window)
            held = set(grouped)
            if window is not None and window.find_ancestor(exp.Select) is select:
                held |= {
                    (p.table.casefold(), p.name.casefold())
                    for p in (_bare(x) for x in window.args.get("partition_by") or [])
                    if isinstance(p, exp.Column)
                }
            missing = sorted(c for c in cols if not ({("", c), (alias.casefold(), c)} & (pinned | held)))
            if missing:
                raise SqlRefused(
                    "non_additive",
                    f"This query adds up {source.name} across {', '.join(missing)}, whose values overlap or "
                    "count different things (a Total row beside its parts, or different conditions), so the "
                    "sum would count the same cases twice or mix different things. Filter each of them to one "
                    "value in WHERE, or add it to GROUP BY to get one row per value.",
                )


def _literal_int(node: exp.Expr | None) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string and node.name.isdigit():
        return int(node.name)
    return None


def _apply_limit(tree: exp.Query, max_rows: int) -> int:
    """Clamp the outer LIMIT to ``max_rows``, or add one. Returns the limit that will apply."""
    offset = tree.args.get("offset")
    if offset is not None and _literal_int(offset.expression) is None:
        raise SqlRefused("bad_limit", "OFFSET takes a whole number.")
    limit = tree.args.get("limit")
    if limit is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return max_rows
    if not isinstance(limit, exp.Limit) or any(
        limit.args.get(k) for k in ("limit_options", "expressions", "offset")
    ):
        raise SqlRefused("bad_limit", "Use a plain LIMIT with a whole number of rows.")
    value = _literal_int(limit.expression)
    if value is None:
        raise SqlRefused("bad_limit", "LIMIT takes a whole number.")
    effective = min(value, max_rows)
    limit.set("expression", exp.Literal.number(effective))
    return effective


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


_MEMORY_LIMIT = re.compile(r"\d+(\.\d+)?\s*(MB|GB|TB|MiB|GiB)")


def harden(
    con: duckdb.DuckDBPyConnection,
    *,
    allowed_paths: Iterable[str] = (),
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    threads: int = DEFAULT_THREADS,
) -> None:
    """Lock ``con`` down for untrusted reads. Call once, after the catalogue's views are created.

    Afterwards no statement on ``con`` can read or write a file or URL other than ``allowed_paths``
    (pass the exact local files the views read, e.g. cached Parquet snapshots), install or load an
    extension, spill to disk, or change any setting (``lock_configuration``).
    """
    if threads < 1:
        raise ValueError(f"threads must be positive, got {threads!r}")
    if not _MEMORY_LIMIT.fullmatch(memory_limit):
        raise ValueError(
            f"memory_limit must be a size such as '2GB' or '512MB' (MB, GB, TB, MiB, GiB), got {memory_limit!r}"
        )
    paths = [str(p) for p in allowed_paths]
    con.execute("SET autoinstall_known_extensions = false")
    con.execute("SET autoload_known_extensions = false")
    con.execute("SET allow_community_extensions = false")
    con.execute("SET allow_unsigned_extensions = false")
    if paths:
        con.execute("SET allowed_paths = ?", [paths])
    con.execute("SET enable_external_access = false")
    con.execute("SET max_temp_directory_size = '0B'")
    con.execute(f"SET memory_limit = '{memory_limit}'")
    con.execute(f"SET threads = {int(threads)}")
    con.execute("SET lock_configuration = true")


_COULD_NOT_RUN: Final[tuple[tuple[type[duckdb.Error], str, str], ...]] = (
    (
        duckdb.OutOfMemoryException,
        "too_much_memory",
        "The query needs more memory than one question may use.",
    ),
    (
        duckdb.ConversionException,
        "query_failed",
        "A value could not be converted to the type the query needs.",
    ),
    (duckdb.BinderException, "query_failed", "The query does not fit the tables' columns or types."),
    (duckdb.PermissionException, "not_allowed", "The query tried to reach something outside the catalogue."),
)


def run_guarded(
    con: duckdb.DuckDBPyConnection, q: GuardedQuery, *, timeout_s: float = 20.0
) -> tuple[list[str], list[tuple[object, ...]]]:
    """Run a guarded query on a hardened connection; return (column names, rows).

    A timer thread calls ``con.interrupt()`` at the deadline. At most ``q.limit`` rows are fetched.

    Raises:
        SqlRefused: not one SELECT (``not_select``), timed out (``timeout``) or failed in the engine.
        ValueError: ``timeout_s`` is not positive.
    """
    if not timeout_s > 0:
        raise ValueError(f"timeout_s must be positive, got {timeout_s!r}")
    _require_single_select(q.sql)  # a GuardedQuery is a plain value: re-check what it carries
    # The connection must have gone through harden(): never run on one that can reach files or the network.
    hardened = con.execute(
        "SELECT current_setting('enable_external_access') = false AND current_setting('lock_configuration')"
    ).fetchone()
    if not (hardened and hardened[0]):
        raise SqlRefused("not_hardened", "The query was not run: the data connection is not locked down.")

    lock = threading.Lock()
    finished = False

    def interrupt() -> None:
        with lock:
            if not finished:
                con.interrupt()

    timer = threading.Timer(timeout_s, interrupt)
    timer.daemon = True
    timer.start()
    try:
        cursor = con.execute(q.sql)
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchmany(q.limit)
    except duckdb.InterruptException as e:
        raise SqlRefused("timeout", TIMED_OUT) from e
    except duckdb.Error as e:
        # The engine's message can quote a cell value or a path: log the type only.
        logger.info("guarded SQL failed: %s", type(e).__name__)
        for kind, code, message in _COULD_NOT_RUN:
            if isinstance(e, kind):
                raise SqlRefused(code, message) from e
        raise SqlRefused("query_failed", "The query could not run on these tables.") from e
    finally:
        with lock:
            finished = True
        timer.cancel()
    return columns, rows
