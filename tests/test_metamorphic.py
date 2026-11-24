"""Metamorphic and property tests over synthetic tables (no public data is committed; spec: Privacy).

The facts a clinician gets must not change when the publisher renames fields, reorders rows or adds a
total row, and must equal an independent oracle computed in plain Python.
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from health_data_registry import tools
from health_data_registry.query import Engine
from health_data_registry.registry import load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import CONTRACT, raw_fetch, write_registry

STATES = ["MICHIGAN", "OHIO", "INDIANA"]
CONDS = ["Legionellosis", "Syphilis"]

record = st.fixed_dictionaries(
    {
        "states": st.sampled_from(STATES),
        "year": st.sampled_from(["2022", "2023", "2024"]),
        "label": st.sampled_from(CONDS),
        "m1": st.one_of(st.integers(0, 5000).map(str), st.sampled_from(["N", "-"])),
        "m2": st.just(""),
    }
)


def _series(records: list[dict[str, Any]], contract: str = CONTRACT) -> list[list[Any]]:
    with tempfile.TemporaryDirectory() as d:
        root = write_registry(Path(d) / "reg", contract=contract)
        reg = load_registry(root)
        lake = Path(d) / "lake"
        lake.mkdir()
        f = Path(d) / "p.ndjson"
        write_ndjson(f, records)
        build_snapshot(
            lake, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/resource/x.json", records)], [f]
        )
        out = tools.get_series(
            reg, Engine(registry=reg, lake=lake), condition="legionellosis", measure="cases",
            locations=["Michigan", "Ohio", "Indiana"], fmt="json",
        )  # fmt: skip
        assert isinstance(out, dict)
        return [r[:3] for r in out["rows"]]


def _oracle(records: list[dict[str, Any]]) -> list[list[Any]]:
    acc: dict[tuple[str, int], int | None] = {}
    for r in records:
        if r["label"] != "Legionellosis" or r["states"] == "US RESIDENTS":
            continue
        key = (r["states"].title(), int(r["year"]))
        v = None if r["m1"] in ("N", "-") else int(r["m1"])
        prev = acc.get(key)
        acc[key] = v if prev is None else (prev if v is None else prev + v)
    return [[k[0], k[1], v] for k, v in sorted(acc.items())]


SETTINGS = settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(st.lists(record, min_size=1, max_size=30))
def test_matches_independent_oracle(records: list[dict[str, Any]]) -> None:
    assert _series(records) == _oracle(records)


@SETTINGS
@given(st.lists(record, min_size=1, max_size=30), st.randoms(use_true_random=False))
def test_row_order_does_not_matter(records: list[dict[str, Any]], rnd: random.Random) -> None:
    shuffled = records[:]
    rnd.shuffle(shuffled)
    assert _series(records) == _series(shuffled)


@SETTINGS
@given(st.lists(record, min_size=1, max_size=30), st.integers(0, 10**6))
def test_total_rows_never_leak_into_sums(records: list[dict[str, Any]], total: int) -> None:
    with_total = [
        *records,
        {"states": "US RESIDENTS", "year": "2023", "label": "Legionellosis", "m1": str(total), "m2": ""},
    ]
    assert _series(records) == _series(with_total)


@SETTINGS
@given(st.lists(record, min_size=1, max_size=20))
def test_renamed_publisher_fields_give_same_facts(records: list[dict[str, Any]]) -> None:
    renamed = [{("cases_reported" if k == "m1" else k): v for k, v in r.items()} for r in records]
    contract = CONTRACT.replace(
        "{property: sourceField, value: m1}", "{property: sourceField, value: cases_reported}"
    )
    assert _series(renamed, contract) == _series(records)


@SETTINGS
@given(st.lists(record.filter(lambda r: r["m1"] in ("N", "-")), min_size=1, max_size=10))
def test_missing_codes_never_become_zero(records: list[dict[str, Any]]) -> None:
    assert all(v is None for _, _, v in _series(records))
