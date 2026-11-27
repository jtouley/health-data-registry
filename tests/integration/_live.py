"""Shared helpers for live contract tests: fetch a small slice from the publisher at test time.

Nothing fetched here is written to disk. Requests go only to the catalog's own host, over HTTPS,
with plain urllib (which honours HTTPS_PROXY and SSL_CERT_FILE when a sandbox needs them).
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 60


def load(dataset_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return (catalog entry, contract, semantic model) as plain dicts."""
    cat = yaml.safe_load((ROOT / "catalog" / f"{dataset_id}.yaml").read_text(encoding="utf-8"))
    con = yaml.safe_load((ROOT / cat["contract"]).read_text(encoding="utf-8"))
    sem = yaml.safe_load((ROOT / cat["semantic_model"]).read_text(encoding="utf-8"))
    return cat, con, sem


def get_json(cat: Mapping[str, Any], url: str) -> Any:
    parts = urllib.parse.urlsplit(url)
    assert parts.scheme == "https", url
    assert parts.hostname == cat["source"]["host"], f"{parts.hostname} is not the catalog host"
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    headers = {"Accept": "application/json", "User-Agent": "health-data-registry-tests"}
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 (https and host asserted above)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=ctx) as resp:  # noqa: S310 (https asserted)
        return json.loads(resp.read())


def with_query(base: str, params: Mapping[str, str]) -> str:
    return base + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def schema(con: Mapping[str, Any]) -> dict[str, Any]:
    return con["schema"][0]


def custom(item: Mapping[str, Any]) -> dict[str, Any]:
    return {c["property"]: c["value"] for c in item.get("customProperties") or []}


def properties(con: Mapping[str, Any]) -> list[dict[str, Any]]:
    return schema(con)["properties"]


def check_structure(con: Mapping[str, Any], rows: list[dict[str, Any]], served_fields: Iterable[str]) -> None:
    """Every sourceField is served; required fields are filled; measures hold numbers or missingCodes."""
    assert rows, "publisher returned no rows for the test slice"
    served = set(served_fields)
    for p in properties(con):
        cp = custom(p)
        src = cp["sourceField"]
        assert src in served, f"{p['name']}: source field {src!r} is no longer served"
        if p.get("required"):
            empty = [r for r in rows if r.get(src) in (None, "")]
            assert not empty, f"{p['name']}: required field empty in {len(empty)} rows"
        if cp["role"] == "measure":
            allowed = {str(c) for c in cp.get("missingCodes") or []}
            for r in rows:
                v = r.get(src)
                if v in (None, ""):
                    continue
                try:
                    float(v)
                except (TypeError, ValueError):
                    assert str(v) in allowed, (
                        f"{p['name']}: non-numeric value {v!r} is not a declared missing code"
                    )


def duckdb_metric(
    con: Mapping[str, Any],
    sem: Mapping[str, Any],
    rows: list[dict[str, Any]],
    metric: str,
    filters: Mapping[str, Any],
) -> float | None:
    """Compute a metric the way the semantic model says: rename and cast per contract, then SQL."""
    props = properties(con)
    db = duckdb.connect()
    db.execute("CREATE TABLE raw (j JSON)")
    db.executemany("INSERT INTO raw VALUES (?)", [[json.dumps(r)] for r in rows])
    cols = []
    for p in props:
        src = custom(p)["sourceField"].replace("'", "''")
        typ = p["physicalType"]
        if typ == "BIGINT":  # publishers send '176.0' for counts
            expr = f"TRY_CAST(TRY_CAST(j->>'$.\"{src}\"' AS DOUBLE) AS BIGINT)"
        else:
            expr = f"TRY_CAST(j->>'$.\"{src}\"' AS {typ})"
        cols.append(f'{expr} AS "{p["name"]}"')
    db.execute(f"CREATE TABLE t AS SELECT {', '.join(cols)} FROM raw")
    m = next(x for x in sem["metrics"] if x["name"] == metric)
    measure = next(x for x in sem["measures"] if x["name"] == m["measure"])
    where = list(m.get("filters") or [])
    total = custom(schema(con)).get("totalRows", "none")
    if m["exclude_total_rows"] and total != "none":
        where.append(f"NOT coalesce(({total}), false)")
    params = []
    for col, val in filters.items():
        where.append(f'"{col}" = ?')
        params.append(val)
    agg = "sum" if m["aggregation"] == "sum" else "any_value"
    sql = f'SELECT {agg}("{measure["column"]}"), count(*) FROM t WHERE {" AND ".join(where) or "true"}'
    value, n = db.execute(sql, params).fetchone()
    if m["aggregation"] == "none":
        assert n == 1, f"{metric} with {dict(filters)} matched {n} rows; a non-summed metric must pin one row"
    return None if value is None else float(value)
