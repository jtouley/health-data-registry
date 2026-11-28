# SPDX-License-Identifier: Apache-2.0
# Adapted from the author's earlier MIT-licensed work (see NOTICE).
"""Read-only SQL guard and engine sandbox: refusals, regeneration, limits, joins, escapes, timeout."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from health_data_registry import sql_guard
from health_data_registry.sql_guard import GuardedQuery, SqlRefused, guard_sql, harden, run_guarded

TABLES: dict[str, frozenset[str]] = {
    "deaths": frozenset({"state", "year", "cause", "deaths"}),
    "pop": frozenset({"state", "year", "population"}),
    "vax": frozenset({"state", "year", "rate"}),
}
JOINS = frozenset({frozenset({"deaths", "pop"})})


def guard(sql: str, max_rows: int = 5000) -> GuardedQuery:
    return guard_sql(sql, allowed_tables=TABLES, declared_joins=JOINS, max_rows=max_rows)


def refused(sql: str) -> SqlRefused:
    with pytest.raises(SqlRefused) as caught:
        guard(sql)
    assert caught.value.message
    assert caught.value.code
    return caught.value


@pytest.fixture
def con(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Views over a Parquet snapshot (the production shape), then hardened."""
    connection = duckdb.connect(":memory:")
    parquet = tmp_path / "deaths.parquet"
    connection.execute(
        "COPY (SELECT 'S' || (i % 5) AS state, 2000 + (i % 3) AS year, 'c' || (i % 2) AS cause, i AS deaths "
        f"FROM range(30) t(i)) TO '{parquet}' (FORMAT parquet)"
    )
    connection.execute(f"CREATE VIEW deaths AS SELECT * FROM read_parquet('{parquet}')")
    connection.execute(
        "CREATE TABLE pop_t AS SELECT 'S' || i AS state, 2000 AS year, 100 * (i + 1) AS population FROM range(5) t(i)"
    )
    connection.execute("CREATE VIEW pop AS SELECT * FROM pop_t")
    connection.execute("CREATE VIEW vax AS SELECT 'S1' AS state, 2000 AS year, 0.5 AS rate")
    harden(connection, allowed_paths=[str(parquet)], memory_limit="512MB", threads=1)
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# Accepted queries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT state, sum(deaths) AS total FROM deaths GROUP BY state ORDER BY total DESC",
        "select d.state, d.deaths / p.population as rate from deaths d join pop p on d.state = p.state and d.year = p.year",
        "SELECT * FROM deaths JOIN pop USING (state, year)",
        "WITH c AS (SELECT state, deaths FROM deaths WHERE year = 2001) SELECT state, avg(deaths) FROM c GROUP BY ALL",
        "SELECT state FROM deaths UNION ALL SELECT state FROM pop",
        "SELECT state FROM deaths INTERSECT SELECT state FROM pop",
        "SELECT state FROM deaths EXCEPT SELECT state FROM pop",
        "SELECT state, rank() OVER (PARTITION BY year ORDER BY deaths DESC) AS r FROM deaths QUALIFY r <= 3",
        "SELECT state, lag(deaths) OVER w AS prev FROM deaths WINDOW w AS (PARTITION BY state ORDER BY year)",
        "SELECT state, count(*) FILTER (WHERE deaths > 10) AS n, count(DISTINCT cause) FROM deaths GROUP BY 1 HAVING count(*) > 1",
        "SELECT median(deaths), quantile_cont(deaths, 0.9), stddev(deaths), min(year), max(year) FROM deaths",
        "SELECT year(make_date(year, 1, 1)), date_trunc('month', DATE '2020-05-03'), extract(year FROM DATE '2020-01-01') FROM deaths",
        "SELECT CASE WHEN deaths > 5 THEN 'high' ELSE 'low' END AS band, coalesce(cause, '?') FROM deaths",
        "SELECT CAST(year AS VARCHAR), year::DOUBLE, round(deaths / 3, 2), upper(state) || lower(cause) FROM deaths",
        "SELECT a FROM (SELECT state AS a FROM deaths) AS s WHERE a LIKE 'S%'",
        "SELECT state FROM deaths d WHERE EXISTS (SELECT 1 FROM pop p WHERE p.state = d.state)",
        "SELECT state, (SELECT max(population) FROM pop WHERE pop.state = deaths.state) FROM deaths",
        "SELECT d1.state FROM deaths d1 JOIN deaths d2 ON d1.state = d2.state",
        "SELECT d.* FROM deaths AS d",
        "SELECT * EXCLUDE (cause) FROM deaths",
        "FROM deaths",
        "SELECT 1 + 1 AS two",
        'SELECT "State", DEATHS FROM Deaths',
        "SELECT state FROM deaths LIMIT 10 OFFSET 5",
    ],
)
def test_guard_readOnlySelect_accepted(sql: str) -> None:
    q = guard(sql)
    assert duckdb.extract_statements(q.sql)[0].type == duckdb.StatementType.SELECT
    assert q.limit <= 5000


def test_guard_runs_onHardenedConnection(con: duckdb.DuckDBPyConnection) -> None:
    q = guard(
        "SELECT d.state, sum(d.deaths) AS total, max(p.population) AS pop FROM deaths d JOIN pop p USING (state) GROUP BY 1 ORDER BY 1"
    )
    columns, rows = run_guarded(con, q)
    assert columns == ["state", "total", "pop"]
    assert rows[0] == ("S0", 0 + 5 + 10 + 15 + 20 + 25, 100)
    assert q.tables == frozenset({"deaths", "pop"})


def test_guard_regeneratesSqlWithoutComments() -> None:
    q = guard("SELECT /* hidden */ state -- note\nFROM deaths")
    assert "hidden" not in q.sql and "note" not in q.sql
    assert q.sql == "SELECT state FROM deaths LIMIT 5000"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT state FROM deaths", 100),
        ("SELECT state FROM deaths LIMIT 10", 10),
        ("SELECT state FROM deaths LIMIT 100000", 100),
        ("SELECT state FROM deaths UNION SELECT state FROM pop", 100),
        ("SELECT state FROM deaths UNION SELECT state FROM pop LIMIT 7", 7),
    ],
)
def test_guard_limit_injectedOrClamped(sql: str, expected: int) -> None:
    q = guard(sql, max_rows=100)
    assert q.limit == expected
    assert q.sql.endswith(f"LIMIT {expected}")


@pytest.mark.parametrize("sql", ["SELECT -*-6", "SELECT -* - 6 FROM deaths", "SELECT ~* FROM deaths"])
def test_guard_signedStar_refused(sql: str) -> None:
    # Found by fuzz_sql_guard: "-*" was accepted and written back as DuckDB's unknown operator "-*".
    assert refused(sql).code == "parse_error"


def test_guard_limitInsideSubquery_untouched() -> None:
    q = guard("SELECT * FROM (SELECT state FROM deaths LIMIT 999999) s", max_rows=50)
    assert "LIMIT 999999" in q.sql and q.sql.endswith("LIMIT 50")


def test_guard_nonPositiveMaxRows_valueError() -> None:
    with pytest.raises(ValueError, match="positive"):
        guard("SELECT 1", max_rows=0)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("", "empty"),
        ("   \n ", "empty"),
        ("-- just a comment", "empty"),
        ("SELECT 1 " + "+ 1 " * 6000, "too_long"),
        ("SELEC state FROM deaths", "parse_error"),
        ("SELECT (", "parse_error"),
        ("SELECT 1; SELECT 2", "multiple_statements"),
        ("SELECT * FROM deaths; DROP TABLE deaths", "multiple_statements"),
    ],
)
def test_guard_malformed_refused(sql: str, code: str) -> None:
    assert refused(sql).code == code


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE deaths",
        "DELETE FROM deaths",
        "UPDATE deaths SET deaths = 0",
        "INSERT INTO deaths VALUES ('x', 1, 'c', 1)",
        "CREATE TABLE t AS SELECT 1",
        "CREATE VIEW v AS SELECT 1",
        "CREATE SECRET s (TYPE s3, KEY_ID 'a', SECRET 'b')",
        "ALTER TABLE deaths ADD COLUMN x INT",
        "TRUNCATE deaths",
        "PRAGMA database_list",
        "SET enable_external_access = true",
        "RESET lock_configuration",
        "ATTACH 'x.db' AS x",
        "DETACH x",
        "COPY deaths TO 'out.csv'",
        "COPY (SELECT 1) TO 'out.parquet'",
        "EXPORT DATABASE 'dir'",
        "IMPORT DATABASE 'dir'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "FORCE INSTALL httpfs",
        "CALL pragma_version()",
        "DESCRIBE deaths",
        "SUMMARIZE deaths",
        "SHOW TABLES",
        "EXPLAIN SELECT 1",
        "USE memory",
        "CHECKPOINT",
        "VACUUM",
        "BEGIN TRANSACTION",
        "PIVOT deaths ON cause USING sum(deaths)",
        "WITH x AS (SELECT 1) DELETE FROM deaths",
        "(SELECT 1)",
        "SELECT 1 INTO t",
    ],
)
def test_guard_notASelect_refused(sql: str) -> None:
    assert refused(sql).code in {"not_select", "parse_error", "multiple_statements"}


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT * FROM read_csv('/etc/passwd')", "table_function"),
        ("SELECT * FROM read_csv_auto('/etc/passwd')", "table_function"),
        ("SELECT * FROM read_parquet('https://example.com/x.parquet')", "table_function"),
        ("SELECT * FROM read_json('x.json')", "table_function"),
        ("SELECT * FROM read_json_auto('x.json')", "table_function"),
        ("SELECT * FROM read_text('/etc/passwd')", "table_function"),
        ("SELECT * FROM read_blob('/etc/passwd')", "table_function"),
        ("SELECT * FROM glob('/*')", "table_function"),
        ("SELECT * FROM query('SELECT 1')", "table_function"),
        ("SELECT * FROM query_table('deaths')", "table_function"),
        ("SELECT * FROM sqlite_scan('x.db', 't')", "table_function"),
        ("SELECT * FROM parquet_scan('x')", "table_function"),
        ("SELECT * FROM duckdb_settings()", "table_function"),
        ("SELECT * FROM range(10)", "table_function"),
        ("SELECT * FROM generate_series(1, 10)", "table_function"),
        ("SELECT * FROM unnest([1, 2])", "forbidden_function"),
        ("SELECT read_text('/etc/passwd')", "forbidden_function"),
        ("SELECT content FROM deaths, read_text('/etc/passwd')", "table_function"),
        ("SELECT getenv('HOME')", "forbidden_function"),
        ("SELECT getvariable('x')", "forbidden_function"),
        ("SELECT current_setting('threads')", "forbidden_function"),
        ("SELECT version()", "forbidden_function"),
        ("SELECT list_transform([1], x -> x + 1)", "forbidden_function"),
        ("SELECT COLUMNS('.*') FROM deaths", "forbidden_function"),
        ("SELECT state FROM deaths WHERE state IN (SELECT * FROM read_csv('x'))", "table_function"),
        ("WITH c AS (SELECT * FROM read_parquet('x')) SELECT * FROM c", "table_function"),
        ("SELECT main.read_text('x')", "forbidden_construct"),
        ("SELECT json_extract('{}', '$.a')", "forbidden_function"),
        ("SELECT $1", "parameter"),
        ("SELECT ?", "parameter"),
        ("SELECT * FROM (VALUES (1)) v(a)", "forbidden_construct"),
        ("SELECT * FROM deaths TABLESAMPLE 10%", "forbidden_construct"),
        ("SELECT * FROM deaths USING SAMPLE 10", "forbidden_construct"),
        ("SELECT * FROM deaths, LATERAL (SELECT 1)", "forbidden_construct"),
        ("SELECT * FROM deaths PIVOT (sum(deaths) FOR cause IN ('a'))", "forbidden_construct"),
        ("SELECT * FROM deaths AT (VERSION => 1)", "forbidden_construct"),
    ],
)
def test_guard_fileOrForbiddenFunction_refused(sql: str, code: str) -> None:
    assert refused(sql).code == code


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT * FROM secrets", "unknown_table"),
        ("SELECT * FROM '/etc/passwd'", "unknown_table"),
        ('SELECT * FROM "data.csv"', "unknown_table"),
        ("SELECT * FROM main.deaths", "unknown_table"),
        ("SELECT * FROM memory.main.deaths", "unknown_table"),
        ("SELECT * FROM information_schema.tables", "unknown_table"),
        ("SELECT * FROM duckdb_tables", "unknown_table"),
        ("WITH deaths AS (SELECT 1 AS x) SELECT * FROM deaths", "forbidden_construct"),
        ("SELECT * FROM (WITH c AS (SELECT 1) SELECT * FROM c) s, c", "unknown_table"),
        ("SELECT nope FROM deaths", "unknown_column"),
        ("SELECT d.nope FROM deaths d", "unknown_column"),
        ("SELECT x.state FROM deaths d", "unknown_column"),
        ("SELECT state FROM deaths WHERE population > 1", "unknown_column"),
        ("SELECT deaths.population FROM deaths JOIN pop USING (state)", "unknown_column"),
        ("SELECT nope", "unknown_column"),
        ("SELECT state FROM deaths WHERE state IN (SELECT nope FROM vax)", "unknown_column"),
        ("SELECT x.* FROM deaths d", "unknown_column"),
    ],
)
def test_guard_unknownTableOrColumn_refused(sql: str, code: str) -> None:
    assert refused(sql).code == code


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM deaths JOIN vax USING (state)",
        "SELECT * FROM deaths, vax",
        "SELECT * FROM deaths CROSS JOIN vax",
        "SELECT * FROM deaths POSITIONAL JOIN vax",
        "SELECT * FROM pop NATURAL JOIN vax",
        "WITH c AS (SELECT * FROM deaths) SELECT * FROM c JOIN vax ON c.state = vax.state",
        "SELECT * FROM (SELECT * FROM deaths) d JOIN (SELECT * FROM vax) v ON d.state = v.state",
        "SELECT state FROM deaths WHERE state IN (SELECT state FROM vax)",
        "SELECT state FROM deaths d WHERE EXISTS (SELECT 1 FROM vax v WHERE v.state = d.state)",
        "SELECT (SELECT max(rate) FROM vax) FROM deaths",
        "SELECT * FROM deaths JOIN pop USING (state) JOIN vax USING (state)",
        "SELECT * FROM (SELECT state FROM deaths UNION SELECT state FROM vax) u JOIN pop USING (state)",
    ],
)
def test_guard_undeclaredJoin_refused(sql: str) -> None:
    error = refused(sql)
    assert error.code == "undeclared_join"
    assert "vax" in error.message


def test_guard_declaredJoin_caseInsensitive() -> None:
    q = guard_sql(
        "SELECT * FROM DEATHS JOIN Vax USING (state)",
        allowed_tables=TABLES,
        declared_joins=frozenset({frozenset({"Deaths", "VAX"})}),
    )
    assert q.tables == frozenset({"deaths", "vax"})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT state FROM deaths LIMIT 10 PERCENT",
        "SELECT state FROM deaths LIMIT 1 + 1",
        "SELECT state FROM deaths LIMIT (SELECT 1)",
        "SELECT state FROM deaths LIMIT 5 OFFSET 1 + 1",
        "SELECT state FROM deaths FETCH FIRST 5 ROWS ONLY",
        "SELECT state FROM deaths LIMIT -1",
        "SELECT state FROM deaths LIMIT 'a'",
    ],
)
def test_guard_badLimit_refused(sql: str) -> None:
    assert refused(sql).code in {"bad_limit", "forbidden_construct", "parse_error"}


def test_guard_hugeTree_tooComplex() -> None:
    sql = "SELECT " + ", ".join(["deaths"] * 2000) + " FROM deaths"
    assert refused(sql).code == "too_complex"


def test_checkClauses_unknownClause_refused() -> None:
    select = sql_guard.exp.select("1").from_("deaths")
    select.set("sort", sql_guard.exp.Sort(expressions=[sql_guard.exp.column("state")]))
    with pytest.raises(SqlRefused) as caught:
        sql_guard._check_clauses(select)
    assert caught.value.code == "forbidden_construct"


def test_checkClauses_into_notSelect() -> None:
    select = sql_guard.exp.select("1")
    select.set("into", sql_guard.exp.Into(this=sql_guard.exp.to_table("t")))
    with pytest.raises(SqlRefused) as caught:
        sql_guard._check_clauses(select)
    assert caught.value.code == "not_select"


def test_checkNodes_intoNode_notSelect() -> None:
    with pytest.raises(SqlRefused) as caught:
        sql_guard._check_nodes(sql_guard.exp.Into(this=sql_guard.exp.to_table("t")))
    assert caught.value.code == "not_select"


# ---------------------------------------------------------------------------
# Engine: hardening and run_guarded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "SELECT * FROM read_text('{secret}')",
        "SELECT * FROM read_csv('{secret}')",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM glob('{dir}/*')",
        "SELECT * FROM read_parquet('https://example.com/data.parquet')",
        "COPY (SELECT 1) TO '{dir}/out.csv'",
        "ATTACH '{dir}/x.db' AS x",
        "EXPORT DATABASE '{dir}/export'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET enable_external_access = true",
        "SET allowed_paths = ['/']",
        "SET memory_limit = '100GB'",
        "RESET lock_configuration",
    ],
)
def test_harden_escapesFailAtTheEngine(con: duckdb.DuckDBPyConnection, tmp_path: Path, template: str) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("not for SQL")
    sql = template.format(secret=secret, dir=tmp_path)
    with pytest.raises(duckdb.Error):
        con.execute(sql).fetchall()
    assert not (tmp_path / "out.csv").exists()
    assert not (tmp_path / "x.db").exists()


def test_harden_settingsLocked(con: duckdb.DuckDBPyConnection) -> None:
    settings_now = dict(
        con.execute(
            "SELECT name, value FROM duckdb_settings() WHERE name IN "
            "('enable_external_access', 'lock_configuration', 'autoload_known_extensions', "
            "'autoinstall_known_extensions', 'threads')"
        ).fetchall()
    )
    assert settings_now == {
        "enable_external_access": "false",
        "lock_configuration": "true",
        "autoload_known_extensions": "false",
        "autoinstall_known_extensions": "false",
        "threads": "1",
    }


@pytest.mark.parametrize(
    ("kwargs"),
    [{"threads": 0}, {"memory_limit": "1GB'; SET x='"}, {"memory_limit": "2GB2"}, {"memory_limit": "GB"},
     {"memory_limit": "2 PB"}, {"memory_limit": ""}],
)  # fmt: skip
def test_harden_badArguments_valueError(kwargs: dict[str, object]) -> None:
    with duckdb.connect(":memory:") as connection, pytest.raises(ValueError):
        harden(connection, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "sql",
    ["COPY deaths TO 'out.csv'", "SELECT 1; SELECT 2", "CREATE TABLE t AS SELECT 1", "PRAGMA database_list"],
)
def test_runGuarded_handBuiltNonSelect_refused(con: duckdb.DuckDBPyConnection, sql: str) -> None:
    with pytest.raises(SqlRefused) as caught:
        run_guarded(con, GuardedQuery(sql=sql, tables=frozenset(), limit=10))
    assert caught.value.code in {"not_select", "multiple_statements"}


def test_runGuarded_handBuiltFileRead_refusedByEngine(con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(SqlRefused) as caught:
        run_guarded(
            con, GuardedQuery(sql="SELECT * FROM read_text('/etc/passwd')", tables=frozenset(), limit=10)
        )
    assert caught.value.code == "not_allowed"
    assert "/etc" not in caught.value.message


def test_runGuarded_slowQuery_interruptedAtTimeout(con: duckdb.DuckDBPyConnection) -> None:
    q = GuardedQuery(
        sql="SELECT count(*) FROM range(100000000000) a, range(1000000) b", tables=frozenset(), limit=1
    )
    with pytest.raises(SqlRefused) as caught:
        run_guarded(con, q, timeout_s=0.2)
    assert caught.value.code == "timeout"
    # the connection is usable afterwards, and a pending interrupt does not leak into the next query
    assert run_guarded(con, guard("SELECT count(*) AS n FROM deaths")) == (["n"], [(30,)])


def test_runGuarded_fetchesAtMostLimit(con: duckdb.DuckDBPyConnection) -> None:
    q = GuardedQuery(sql="SELECT state FROM deaths", tables=frozenset({"deaths"}), limit=3)
    _, rows = run_guarded(con, q)
    assert len(rows) == 3


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT CAST(state AS INTEGER) FROM deaths", "query_failed"),
        ("SELECT nope FROM deaths", "query_failed"),
        ("SELECT * FROM nowhere", "query_failed"),
        ("SELECT 1 / 0 = 'x'::INT", "query_failed"),
    ],
)
def test_runGuarded_engineError_plainMessage(con: duckdb.DuckDBPyConnection, sql: str, code: str) -> None:
    with pytest.raises(SqlRefused) as caught:
        run_guarded(con, GuardedQuery(sql=sql, tables=frozenset(), limit=10))
    assert caught.value.code == code


def test_runGuarded_outOfMemory_plainMessage(con: duckdb.DuckDBPyConnection) -> None:
    sql = "SELECT list(i) FROM range(200000000) t(i)"
    with pytest.raises(SqlRefused) as caught:
        run_guarded(con, GuardedQuery(sql=sql, tables=frozenset(), limit=1), timeout_s=30)
    assert caught.value.code in {"too_much_memory", "timeout"}


@pytest.mark.parametrize("timeout_s", [0, -1.0, float("nan")])
def test_runGuarded_nonPositiveTimeout_valueError(con: duckdb.DuckDBPyConnection, timeout_s: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        run_guarded(con, guard("SELECT 1"), timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

FORBIDDEN_FUNCTIONS = (
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_json_objects",
    "read_ndjson", "read_text", "read_blob", "glob", "query", "query_table", "sqlite_scan",
    "sqlite_attach", "postgres_scan", "parquet_scan", "parquet_metadata", "parquet_schema",
    "delta_scan", "iceberg_scan", "getenv", "getvariable", "current_setting", "duckdb_settings",
    "duckdb_tables", "duckdb_secrets", "pragma_version", "load_extension", "range",
    "generate_series", "read_xlsx", "st_read", "http_get", "unnest", "which_secret", "set_variable",
)  # fmt: skip
FORBIDDEN_STATEMENTS = (
    "DROP TABLE deaths", "DELETE FROM deaths", "UPDATE deaths SET deaths = 0",
    "INSERT INTO deaths SELECT * FROM deaths", "CREATE TABLE z AS SELECT 1", "CREATE OR REPLACE VIEW deaths AS SELECT 1",
    "CREATE SECRET s (TYPE s3)", "CREATE MACRO m(a) AS a", "ALTER VIEW deaths RENAME TO d", "PRAGMA version",
    "SET threads = 64", "SET GLOBAL enable_external_access = true", "RESET memory_limit", "ATTACH 'x.db'",
    "DETACH db", "COPY deaths TO 'x.csv'", "COPY deaths FROM 'x.csv'", "EXPORT DATABASE 'd'",
    "IMPORT DATABASE 'd'", "INSTALL httpfs", "LOAD httpfs", "CALL pragma_version()", "CHECKPOINT",
    "TRUNCATE deaths", "USE memory", "DESCRIBE deaths", "SUMMARIZE deaths", "UPDATE EXTENSIONS",
)  # fmt: skip

_IDENT = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True)
_WS = st.sampled_from([" ", "  ", "\n", "\t", " \n\t "])
_COMMENT = st.sampled_from(["", " /* c */ ", " -- c\n", "/**/", " /* ; DROP TABLE deaths; */ "])


def _random_case(draw: st.DrawFn, word: str) -> str:
    flips = draw(st.lists(st.booleans(), min_size=len(word), max_size=len(word)))
    return "".join(c.upper() if f else c.lower() for c, f in zip(word, flips, strict=True))


@st.composite
def _sep(draw: st.DrawFn) -> str:
    return draw(_WS) + draw(_COMMENT) + draw(_WS)


@st.composite
def forbidden_function_query(draw: st.DrawFn) -> str:
    fn = _random_case(draw, draw(st.sampled_from(FORBIDDEN_FUNCTIONS)))
    arg = draw(st.sampled_from(["'/etc/passwd'", "'https://example.com/x'", "", "'a', 'b'", "1", "state"]))
    alias = draw(_IDENT)
    call = f"{fn}{draw(st.sampled_from(['', ' ']))}({arg})"
    s = draw(_sep())
    shape = draw(st.integers(0, 5))
    if shape == 0:
        return f'SELECT{s}{call} AS "{alias}"{s}FROM deaths'
    if shape == 1:
        return f"SELECT{s}*{s}FROM{s}{call}"
    if shape == 2:
        return f"SELECT state FROM deaths WHERE state IN (SELECT{s}{call})"
    if shape == 3:
        return f'WITH "{alias}" AS (SELECT * FROM {call}) SELECT * FROM "{alias}"'
    if shape == 4:
        return f"SELECT state FROM deaths{s}UNION ALL{s}SELECT{s}{call}"
    return f'SELECT d.state FROM deaths d JOIN {call} AS "{alias}" ON true'


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(forbidden_function_query())
def test_property_forbiddenFunction_neverAccepted(sql: str) -> None:
    with pytest.raises(SqlRefused):
        guard(sql)


@st.composite
def forbidden_statement(draw: st.DrawFn) -> str:
    statement = draw(st.sampled_from(FORBIDDEN_STATEMENTS))
    words = statement.split(" ")
    s = draw(_sep())
    body = s.join(_random_case(draw, w) if not w.startswith("'") else w for w in words)
    shape = draw(st.integers(0, 3))
    if shape == 0:
        return body
    if shape == 1:
        return f"SELECT 1;{s}{body}"
    if shape == 2:
        return f"{body};{s}SELECT 1"
    return f"{s}{body}{s};"


@settings(max_examples=300)
@given(forbidden_statement())
def test_property_forbiddenStatement_neverAccepted(sql: str) -> None:
    with pytest.raises(SqlRefused):
        guard(sql)


@st.composite
def benign_query(draw: st.DrawFn) -> tuple[str, int | None]:
    alias = draw(_IDENT)
    col_alias = draw(_IDENT)
    s = draw(_sep())
    kw = {
        w: _random_case(draw, w)
        for w in ("select", "from", "where", "group", "by", "order", "limit", "as", "and")
    }
    col = _random_case(draw, draw(st.sampled_from(sorted(TABLES["deaths"]))))
    agg = _random_case(draw, draw(st.sampled_from(["sum", "avg", "count", "max", "min", "median"])))
    limit = draw(st.none() | st.integers(0, 10**9))
    sql = (
        f'{kw["select"]}{s}"{alias}".{col}{s}{kw["as"]} "{col_alias}",{s}{agg}("{alias}".deaths){s}'
        f'{kw["from"]}{s}deaths{s}{kw["as"]}{s}"{alias}"{s}'
        f'{kw["where"]}{s}"{alias}".year >= {draw(st.integers(1900, 2100))}{s}'
        f"{kw['group']} {kw['by']}{s}1{s}{kw['order']} {kw['by']}{s}2"
    )
    if limit is not None:
        sql += f"{s}{kw['limit']}{s}{limit}"
    return sql, limit


@settings(max_examples=150, deadline=None)
@given(benign_query())
def test_property_benignQuery_acceptedClampedAndRunnable(case: tuple[str, int | None]) -> None:
    sql, limit = case
    q = guard(sql, max_rows=500)
    assert q.limit == (500 if limit is None else min(limit, 500))
    assert q.tables == frozenset({"deaths"})
    statements = duckdb.extract_statements(q.sql)
    assert len(statements) == 1 and statements[0].type == duckdb.StatementType.SELECT
    assert "/*" not in q.sql and "--" not in q.sql
    # regenerating a guarded query is stable
    assert guard(q.sql, max_rows=500).sql == q.sql
    with duckdb.connect(":memory:") as connection:
        connection.execute("CREATE TABLE deaths (state VARCHAR, year INT, cause VARCHAR, deaths INT)")
        harden(connection)
        columns, _ = run_guarded(connection, q)
    assert len(columns) == 2


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=200))
def test_property_arbitraryText_refusedOrSingleSelect(text: str) -> None:
    try:
        q = guard(text)
    except SqlRefused as e:
        assert e.code and e.message
        return
    statements = duckdb.extract_statements(q.sql)
    assert len(statements) == 1 and statements[0].type == duckdb.StatementType.SELECT
    lowered = q.sql.lower()
    for fn in FORBIDDEN_FUNCTIONS:
        assert f"{fn}(" not in lowered


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            [
                "SELECT",
                "FROM",
                "deaths",
                "vax",
                "state",
                "*",
                ",",
                "(",
                ")",
                ";",
                "read_csv('x')",
                "DROP",
                "TABLE",
                "JOIN",
                "ON",
                "UNION",
                "WITH",
                "c",
                "AS",
                "LIMIT",
                "1",
                "glob('*')",
                "COPY",
                "TO",
                "'f'",
                "WHERE",
                "=",
            ]
        ),
        min_size=1,
        max_size=14,
    )
)
def test_property_tokenSoup_neverAcceptsForbidden(tokens: list[str]) -> None:
    sql = " ".join(tokens)
    try:
        q = guard(sql)
    except SqlRefused:
        return
    lowered = q.sql.lower()
    assert "read_csv" not in lowered and "glob" not in lowered
    assert not any(w in lowered.split() for w in ("drop", "copy"))
    if "vax" in q.tables:
        assert "deaths" not in q.tables or "union" in lowered


@pytest.mark.parametrize("sql", ["SELECT 1 COPY", "SELECT 1 AS drop", "SELECT state AS attach FROM vax"])
def test_guard_statementWordAsName_refused(sql: str) -> None:
    """Regression (found by the token-soup property): a statement word as an alias is refused, never run."""
    assert refused(sql).code == "forbidden_construct"


@pytest.mark.parametrize("word", ["update", "load", "delete", "copy"])
def test_guard_quotedStatementWord_isAnOrdinaryName(word: str) -> None:
    """code-review 7: a quoted alias named like a SQL command stays a name; the bare word is still refused."""
    q = guard(f'SELECT state AS "{word}" FROM deaths')
    assert f'AS "{word}"' in q.sql
    assert refused(f"SELECT state AS {word} FROM deaths").code in {"forbidden_construct", "parse_error"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM deaths read_csv('x')",  # the token-soup case: a table alias read_csv with a column alias x
        "SELECT * FROM deaths AS sniff_csv",
        "SELECT state AS read_parquet FROM deaths",
        "SELECT state getenv FROM deaths",
        "SELECT * FROM deaths AS d(current_setting)",
        "WITH query AS (SELECT state FROM deaths) SELECT state FROM query",
        "SELECT state AS Read_CSV FROM deaths",
        "SELECT state AS duckdb_settings FROM deaths",
        "SELECT state AS pragma_version FROM deaths",
        "SELECT state AS parquet_metadata FROM deaths",
        "SELECT state AS read_ndjson_objects FROM deaths",
        "SELECT state AS st_readosm FROM deaths",  # an extension reader the installed DuckDB may not list
        "SELECT state AS ducklake_snapshots FROM deaths",
        "SELECT state AS checkpoint FROM deaths",
        "SELECT rank() OVER read_csv AS r FROM deaths WINDOW read_csv AS (ORDER BY year)",  # a window name
        "SELECT rank() OVER (read_csv) AS r FROM deaths WINDOW read_csv AS (ORDER BY year)",
    ],
)
def test_guard_functionNameAsName_refused(sql: str) -> None:
    """Regression (found by the token-soup property): a bare function name as an alias or WITH name is refused."""
    e = refused(sql)
    assert e.code == "forbidden_construct"
    assert "function" in e.message and "double quotes" in e.message


# Table functions that only make rows from their arguments and reach nothing outside the query: their names stay
# ordinary names (a clinician's "range" or "summary"). Reviewed; see sql_guard._FUNCTION_NAMES.
ROW_GENERATORS = frozenset(
    {"generate_series", "icu_calendar_names", "json_each", "json_tree", "pg_timezone_names", "range", "repeat",
     "repeat_row", "summary", "test_all_types", "test_vector_types", "unnest"}
)  # fmt: skip


def test_guard_functionNames_coverEveryTableFunctionOfTheInstalledDuckdb() -> None:
    """The refused names are a fixed list: a DuckDB upgrade that adds a table function fails here, for review,
    instead of quietly changing what query_sql accepts. Each new one goes in _FUNCTION_NAMES or ROW_GENERATORS."""
    with duckdb.connect(":memory:") as con:
        rows = con.execute(
            "SELECT DISTINCT function_name FROM duckdb_functions() WHERE function_type = 'table'"
        ).fetchall()
    listed = {str(name).casefold() for (name,) in rows}
    assert listed - sql_guard._FUNCTION_NAMES - ROW_GENERATORS == set()
    assert not ROW_GENERATORS & sql_guard._FUNCTION_NAMES


@pytest.mark.parametrize("fn", sorted({*FORBIDDEN_FUNCTIONS, "sniff_csv", "read_xlsx"} - ROW_GENERATORS))
def test_guard_forbiddenFunctionAsAlias_refusedUnquotedAllowedQuoted(fn: str) -> None:
    """Every forbidden function name that reaches outside the query: refused as a bare alias, an ordinary name
    when quoted."""
    assert refused(f"SELECT state AS {fn} FROM deaths").code in {"forbidden_construct", "parse_error"}
    q = guard(f'SELECT state AS "{fn}" FROM deaths')
    assert f'AS "{fn}"' in q.sql


@pytest.mark.parametrize(
    "sql",
    [
        """SELECT * FROM deaths "read_csv"("x")""",
        'SELECT state AS "glob" FROM deaths',
        'WITH "query" AS (SELECT state FROM deaths) SELECT state FROM "query"',
        'SELECT * FROM deaths AS d("current_setting", b, c, e)',
        'SELECT "range".state FROM deaths AS "range"',
        'SELECT rank() OVER "glob" AS r FROM deaths WINDOW "glob" AS (ORDER BY year)',
    ],
)
def test_guard_quotedFunctionName_isAnOrdinaryName(sql: str) -> None:
    assert guard(sql).sql.endswith("LIMIT 5000")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT state AS rate, deaths AS total FROM deaths",
        "SELECT state AS reader, deaths AS scan_count FROM deaths",  # near misses of a prefix or suffix
        "SELECT state AS my_read_x, deaths AS scanned FROM deaths",
        "SELECT year AS year_of_death, count(*) AS count FROM deaths GROUP BY 1",  # allowlisted function names
        # everyday names (code-review and sharp-edges on #101): no prefix or suffix family is refused
        "SELECT state AS care_setting, deaths AS ct_scan, deaths AS pet_scan FROM deaths",
        "SELECT deaths AS delta_deaths, deaths AS http_status, deaths AS read_count FROM deaths",
        "SELECT deaths - lag(deaths) OVER (ORDER BY year) AS delta_cases FROM deaths",
        # row generators and DuckDB's table macros and pragmas reach nothing, so their names are ordinary names
        "SELECT state AS range, deaths AS summary, year AS version FROM deaths",
        "SELECT state AS histogram, deaths AS platform, year AS show FROM deaths",
        "SELECT state AS functions, deaths AS repeat_row, year AS unnest FROM deaths",
        "WITH summary AS (SELECT state, sum(deaths) AS n FROM deaths GROUP BY 1) SELECT * FROM summary",
    ],
)
def test_guard_ordinaryAlias_accepted(sql: str) -> None:
    assert guard(sql).sql.endswith("LIMIT 5000")


def test_guard_functionNameAsColumnReference_notRefusedAsAName() -> None:
    """Only names the query defines are checked: a reference is checked against the catalogue instead."""
    assert refused("SELECT read_text FROM deaths").code == "unknown_column"


@pytest.mark.parametrize("fn", ["totally_new_function", "made_up", "my_udf"])
def test_guard_unknownFunction_refused(fn: str) -> None:
    """Fail closed: a function nobody listed is refused, not only the known-dangerous ones."""
    assert refused(f"SELECT {fn}(state) FROM deaths").code == "forbidden_function"


def test_runGuarded_onAnUnhardenedConnection_refused() -> None:
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE deaths AS SELECT 'S' AS state")
    with pytest.raises(SqlRefused) as e:
        sql_guard.run_guarded(con, guard("SELECT state FROM deaths"))
    assert e.value.code == "not_hardened"


@pytest.mark.parametrize("limit", ["512MB", "2GB", "1.5 GiB", "1TB"])
def test_harden_memoryLimit_acceptsPlainSizes(limit: str) -> None:
    with duckdb.connect(":memory:") as connection:
        harden(connection, memory_limit=limit)


def test_guard_limitAll_isClampedOrRefused() -> None:
    try:
        q = guard("SELECT state FROM deaths LIMIT ALL", max_rows=10)
    except SqlRefused as e:
        assert e.code in {"bad_limit", "forbidden_construct", "parse_error"}
    else:
        assert q.limit <= 10 and q.sql.endswith("LIMIT 10")
