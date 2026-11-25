"""The fuzz harnesses' known-finding skips match only their filed issue, and fuzz/requirements.txt matches uv.lock.

Atheris isn't installed here (the harnesses run in ClusterFuzzLite's image), so a stand-in module lets each harness
import; TestOneInput is then called directly with the issues' reproducers and with failures they must not hide.
"""

from __future__ import annotations

import contextlib
import csv
import importlib.util
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from sqlglot.errors import OptimizeError, ParseError

from health_data_registry.sql_guard import GuardedQuery

ROOT = Path(__file__).resolve().parents[1]
FUZZ = ROOT / "fuzz"


@pytest.fixture
def load(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Any]:
    """Load a harness fresh, with a stand-in atheris module."""
    atheris = ModuleType("atheris")
    atheris.instrument_imports = contextlib.nullcontext  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "atheris", atheris)

    def _load(name: str) -> Any:
        spec = importlib.util.spec_from_file_location(name, FUZZ / f"{name}.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, mod)
        spec.loader.exec_module(mod)
        return mod

    return _load


# --- fuzz_sql_guard ---------------------------------------------------------------------------------------------

SQL_KNOWN = {
    "#56 lone surrogate": ("KNOWN_LONE_SURROGATE", "SELECT 1 AS \udcff"),
    "#57 repeated alias": ("KNOWN_OPTIMIZE_ERROR", "SELECT d.state FROM deaths d JOIN deaths d ON true"),
    # the input the PR fuzzing run found: the same error, first read in _check_columns
    "#57 repeated alias, column check": (
        "KNOWN_OPTIMIZE_ERROR",
        "WITH t AS (SELECT state, ye+ar FROM pop) SELECT t.s~t-ate, count(*) FROM t JOIN deaths d ON dOVERte = "
        "t.state AND d JOIN deaths d ON dOVERte =e",
    ),
    "#71 initcap": ("KNOWN_REWRITES", "SELECT initcap(state) FROM deaths"),
    "#71 two-argument length": ("KNOWN_REWRITES", "SELECT length(state, year) FROM deaths"),
    "#71 current_date(x)": ("KNOWN_REWRITES", "SELECT current_date(state) FROM deaths"),
    "#71 current_timestamp(x, y)": ("KNOWN_REWRITES", "SELECT current_timestamp(state, NULL) FROM deaths"),
    "#72 initcap()": ("KNOWN_GENERATE_PARSE_ERROR", "SELECT initcap() FROM deaths"),
    "#77 minus tilde": ("KNOWN_MINUS_TILDE", "SELECT -~9PysLRECURSIVEsS"),
    "#77 minus tilde tilde": ("KNOWN_MINUS_TILDE", "SELECT - ~ ~state FROM deaths"),
}


def _bytes(sql: str) -> bytes:
    return sql.encode("utf-8", errors="surrogateescape")


@pytest.mark.parametrize("flag,sql", SQL_KNOWN.values(), ids=SQL_KNOWN.keys())
def test_sql_known_findings_are_skipped_only_while_flagged(
    load: Callable[[str], Any], flag: str, sql: str
) -> None:
    h = load("fuzz_sql_guard")
    h.TestOneInput(_bytes(sql))  # skipped: the issue is open
    setattr(h, flag, False)
    with pytest.raises(Exception):  # noqa: B017 - each issue escapes as its own type
        h.TestOneInput(_bytes(sql))


def test_sql_valid_and_refused_inputs_pass(load: Callable[[str], Any]) -> None:
    h = load("fuzz_sql_guard")
    for path in sorted((FUZZ / "corpus" / "fuzz_sql_guard").iterdir()):
        h.TestOneInput(path.read_bytes())


def _guard_raising(e: Exception) -> Callable[..., GuardedQuery]:
    def guard(*_a: object, **_k: object) -> GuardedQuery:
        raise e

    return guard


@pytest.mark.parametrize(
    "sql,error",
    [
        # the right type but not raised where the issue is, or not on its input shape
        ("SELECT 1 AS \udcff", TypeError("extract_statements(): incompatible function arguments")),
        ("SELECT state FROM deaths", TypeError("extract_statements(): incompatible function arguments")),
        ("SELECT state FROM deaths", OptimizeError("Unknown column: x")),
        ("SELECT initcap(state) FROM deaths", ParseError("Required keyword: 'expression' missing")),
        ("SELECT state FROM deaths", KeyError("x")),
    ],
)
def test_sql_unrelated_failures_still_fail(
    load: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch, sql: str, error: Exception
) -> None:
    h = load("fuzz_sql_guard")
    monkeypatch.setattr(h, "guard_sql", _guard_raising(error))
    with pytest.raises(type(error)):
        h.TestOneInput(_bytes(sql))


def test_sql_known_rewrite_does_not_excuse_another_function(
    load: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = load("fuzz_sql_guard")
    accepted = GuardedQuery(
        sql="SELECT array_to_string(list_value(state), ''), version() FROM deaths LIMIT 5",
        tables=frozenset({"deaths"}),
        limit=5,
    )
    monkeypatch.setattr(h, "guard_sql", lambda *_a, **_k: accepted)
    with pytest.raises(h.Finding, match="'version'"):
        h.TestOneInput(b"SELECT initcap(state) FROM deaths")
    with pytest.raises(h.Finding, match="'array_to_string'"):
        h.TestOneInput(b"SELECT state FROM deaths")  # no initcap in the text: nothing is excused


def test_sql_cte_names_resolve_per_scope(load: Callable[[str], Any]) -> None:
    h = load("fuzz_sql_guard")
    h.check_accepted("WITH c AS (SELECT state FROM deaths) SELECT * FROM c LIMIT 5")
    with pytest.raises(h.Finding, match="uncatalogued table duckdb_settings"):
        h.check_accepted(
            "SELECT * FROM (WITH duckdb_settings AS (SELECT state FROM deaths) SELECT * FROM duckdb_settings) t, "
            "duckdb_settings LIMIT 5"
        )
    with pytest.raises(h.Finding, match="undeclared join between deaths and vax"):
        h.check_accepted(
            "WITH c AS (SELECT state FROM deaths), d AS (SELECT * FROM c) SELECT * FROM d JOIN vax USING (state) LIMIT 5"
        )


# --- fuzz_json_adapters ------------------------------------------------------------------------------------------

JSON_58 = [b"[1]", b"[]", b'{"columns":[1]}', b'{"columns":[{}]}', b'{"value":[1]}', b"[" * 100_000]
TWELVE = b"[" + b",".join(b'{"a":1}' for _ in range(12)) + b"]"


@pytest.mark.parametrize("body", JSON_58, ids=lambda b: b[:20].decode())
def test_json_wrong_shape_is_skipped_only_while_flagged(load: Callable[[str], Any], body: bytes) -> None:
    h = load("fuzz_json_adapters")
    h.TestOneInput(body)
    h.KNOWN_WRONG_SHAPE = False
    with pytest.raises((AttributeError, TypeError, KeyError, RecursionError)):
        h.TestOneInput(body)


def test_json_socrata_over_max_rows_is_skipped_only_while_flagged(load: Callable[[str], Any]) -> None:
    h = load("fuzz_json_adapters")
    h.TestOneInput(TWELVE)  # issue #73
    h.KNOWN_SOCRATA_OVER_MAX_ROWS = False
    with pytest.raises(h.OverMaxRows, match="12 records"):
        h.TestOneInput(TWELVE)


def test_json_over_max_rows_from_who_gho_still_fails(
    load: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = load("fuzz_json_adapters")

    def pages(*_a: object, **_k: object) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(records=[{"a": 1}] * 12)

    monkeypatch.setattr(h.who_gho, "pages", pages)
    with pytest.raises(h.OverMaxRows):
        h.TestOneInput(b'{"value":[]}')


def test_json_same_error_raised_outside_the_adapter_still_fails(
    load: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = load("fuzz_json_adapters")

    def pages(*_a: object, **_k: object) -> Iterator[SimpleNamespace]:
        raise AttributeError("'int' object has no attribute 'items'")
        yield  # pragma: no cover

    monkeypatch.setattr(h.socrata, "pages", pages)
    with pytest.raises(AttributeError):
        h.TestOneInput(b"[1]")


def test_json_seeds_pass(load: Callable[[str], Any]) -> None:
    h = load("fuzz_json_adapters")
    for path in sorted((FUZZ / "corpus" / "fuzz_json_adapters").iterdir()):
        h.TestOneInput(path.read_bytes())


# --- fuzz_sitemap_parse ------------------------------------------------------------------------------------------

SITEMAP_59 = [
    b'<?xml version="1.0" encoding="x"?><urlset/>',
    b'<?xml version="1.0" encoding="utf-32"?><urlset/>',
]


@pytest.mark.parametrize("body", SITEMAP_59)
def test_sitemap_encoding_errors_are_skipped_only_while_flagged(
    load: Callable[[str], Any], body: bytes
) -> None:
    h = load("fuzz_sitemap_parse")
    h.TestOneInput(body)
    h.KNOWN_ENCODING_ERRORS = False
    with pytest.raises((LookupError, ValueError)):
        h.TestOneInput(body)


@pytest.mark.parametrize(
    "body,error",
    [
        (b'<?xml version="1.0" encoding="x"?><urlset/>', ValueError("bad encoding in the gzip stream")),
        (b"<urlset/>", LookupError("unknown encoding: x")),  # no encoding declared: not #59's input
        (b'<?xml version="1.0" encoding="x"?><urlset/>', LookupError("no codec search function")),
    ],
)
def test_sitemap_unrelated_failures_still_fail(
    load: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch, body: bytes, error: Exception
) -> None:
    h = load("fuzz_sitemap_parse")
    monkeypatch.setattr(h, "parse", _guard_raising(error))
    with pytest.raises(type(error)):
        h.TestOneInput(body)


def test_sitemap_seeds_pass(load: Callable[[str], Any]) -> None:
    h = load("fuzz_sitemap_parse")
    for path in sorted((FUZZ / "corpus" / "fuzz_sitemap_parse").iterdir()):
        h.TestOneInput(path.read_bytes())


# --- fuzz_https_csv ----------------------------------------------------------------------------------------------

CSV_60 = b"a\n" + b"x" * 140_000 + b"\n"


def test_csv_field_limit_is_skipped_only_while_flagged(load: Callable[[str], Any]) -> None:
    h = load("fuzz_https_csv")
    h.TestOneInput(CSV_60)
    h.KNOWN_CSV_ERROR = False
    with pytest.raises(csv.Error):
        h.TestOneInput(CSV_60)


def test_csv_unrelated_csv_error_still_fails(
    load: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = load("fuzz_https_csv")
    monkeypatch.setattr(
        h.https_csv, "records", _guard_raising(csv.Error("new-line character seen in unquoted field"))
    )
    with pytest.raises(csv.Error):
        h.TestOneInput(CSV_60)


def test_csv_pivot_matches_the_catalog_and_reaches_the_key_refusal(load: Callable[[str], Any]) -> None:
    import yaml

    h = load("fuzz_https_csv")
    catalog = yaml.safe_load((ROOT / "catalog" / "census_county_pop.yaml").read_text())
    assert h._PIVOT.model_dump() == {"wide_to_long": catalog["source"]["reshape"]["wide_to_long"]}
    pivot = next(e for e in h.ENTRIES if e.source.reshape is not None)
    seeds = FUZZ / "corpus" / "fuzz_https_csv"
    assert len(h.https_csv.records(pivot, (seeds / "wide.csv").read_bytes())) == 12
    with pytest.raises(ValueError, match="its layout lists exactly"):
        h.https_csv.records(pivot, (seeds / "wide_wrong_years.csv").read_bytes())
    for path in sorted(seeds.iterdir()):
        h.TestOneInput(path.read_bytes())


# --- fuzz/requirements.txt ---------------------------------------------------------------------------------------


@pytest.fixture
def reqs(monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location(
        "fuzz_requirements", ROOT / "scripts" / "fuzz_requirements.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "fuzz_requirements", mod)
    spec.loader.exec_module(mod)
    return mod


def test_fuzz_requirements_match_uv_lock(reqs: Any) -> None:
    assert reqs.mismatches() == [], "run `make fuzz-requirements` and commit fuzz/requirements.txt"
    assert {"duckdb", "sqlglot", "pydantic", "pyyaml"} <= set(reqs.pinned_versions())
    assert reqs.main(["x", "check"]) == 0


def test_fuzz_requirements_check_catches_drift(reqs: Any, tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "duckdb"\nversion = "2.0.1"\n\n[[package]]\nname = "SQLGlot"\nversion = "1.0"\n'
    )
    req = tmp_path / "requirements.txt"
    req.write_text(
        "duckdb==2.0.1 \\\n    --hash=sha256:00\nsqlglot==2.0 \\\n    --hash=sha256:00\nextra_pkg==1\n"
    )
    assert reqs.mismatches(lock, req) == [
        "extra-pkg==1 is in requirements.txt but not in uv.lock",
        "sqlglot==2.0 in requirements.txt, but uv.lock has 1.0",
    ]
    assert reqs.main(["x", "bogus"]) == 2


@pytest.mark.parametrize(
    "name,want",
    [
        ("_require_single_select", "_require_single_select"),
        ("x__require_single_select__mutmut_orig", "_require_single_select"),
        ("x_guard_sql__mutmut_12", "guard_sql"),
    ],
)
def test_sql_raised_in_readsMutmutsRenamedFunctionsAsTheOriginal(
    load: Callable[[str], Any], name: str, want: str
) -> None:
    # mutmut copies sql_guard with renamed functions; the known-finding checks must still see the original names
    assert load("fuzz_sql_guard")._MUTMUT_NAME.sub(r"\1", name) == want
