"""Table reconciliation as metadata (adapters/reconcile.py): table-driven over small hand-written tables."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError

from health_data_registry.adapters.reconcile import ReconcileRefused, Row, failed_checks, is_total
from health_data_registry.source_config import Reconcile

H = ["2023", "2024"]


def row(group: str, label: str, *values: float | None, grand: bool = False, headers: list[str] = H) -> Row:
    return Row(group, label, dict(zip(headers, values, strict=True)), grand_total=grand)


@dataclass
class Case:
    name: str
    rule: dict[str, Any]
    rows: list[Row]
    want: list[int] = field(default_factory=list)  # indexes of the rows that fail a check
    contains: str | None = None  # text one failure must hold
    refused: str | None = None
    headers: list[str] = field(default_factory=lambda: list(H))


SUBTOTAL = {"total_rows": "(?i)(sub)?totals?\\b.*"}
GROUP = [row("Enteric", "Salmonellosis", 10, 12), row("Enteric", "Shigellosis", 5, 3)]

CASES = [
    Case("a subtotal that adds up passes", SUBTOTAL, [*GROUP, row("Enteric", "Total", 15, 15)]),
    Case(
        "a subtotal that doesn't add up is flagged on the total row",
        SUBTOTAL,
        [*GROUP, row("Enteric", "Total", 15, 16)],
        want=[2],
        contains="Enteric Total: 2024 is 16, but the 2 rows it totals sum to 15",
        refused="disagree",
    ),
    Case(
        "within max_failed_rows the failure is recorded, not refused",
        {**SUBTOTAL, "max_failed_rows": 1},
        [*GROUP, row("Enteric", "Total", 15, 16)],
        want=[2],
    ),
    Case(
        "a subtotal covers its own group only",
        {**SUBTOTAL, "max_failed_rows": 5},
        [*GROUP, row("Enteric", "Subtotal", 15, 15), row("Vaccine", "Pertussis", 1, 1), row("Vaccine", "Total", 1, 1)],
    ),
    Case(
        "the structural grand total covers every non-total row",
        SUBTOTAL,
        [*GROUP, row("Enteric", "Total", 15, 15), row("Vaccine", "Pertussis", 2, 2), row("", "Total", 17, 17, grand=True)],
    ),
    Case(
        "a total with no group covers every row",
        {**SUBTOTAL, "max_failed_rows": 5},
        [*GROUP, row("", "Total", 99, 15)],
        want=[2],
    ),
    Case(
        "a label merely containing the word is not a total",
        SUBTOTAL,
        [row("Other", "Totally made up", 1, 1), row("Other", "Total", 1, 1)],
    ),
    Case("a suppressed part skips that check", SUBTOTAL, [*GROUP[:1], row("Enteric", "Shigellosis", None, 3),
                                                          row("Enteric", "Total", 99, 15)]),  # fmt: skip
    Case("a suppressed total skips that check", SUBTOTAL, [*GROUP, row("Enteric", "Total", None, 15)]),
    Case(
        "the tolerance counts near sums as equal",
        {**SUBTOTAL, "tolerance": 0.05},
        [row("G", "a", 0.1, 0.2), row("G", "b", 0.2, 0.2), row("G", "Total", 0.3, 0.44)],
    ),
    Case(
        "only declared columns are summed down",
        {**SUBTOTAL, "columns": ["2023"]},
        [*GROUP, row("Enteric", "Total", 15, 999)],
    ),
    Case("no total_rows: only the grand total is checked", {}, [*GROUP, row("Enteric", "Total", 1, 1)]),
    Case(
        "column sums pair a total with its parts by key",
        {"column_sums": [{"total": "YTD (\\d{4})", "parts": "Q[1-4]_(\\d{4})"}], "max_failed_rows": 5},
        [
            row("G", "a", 1, 2, 3, 9, 10, headers=["Q1_2025", "Q2_2025", "YTD 2025", "Q1_2026", "YTD 2026"]),
            row("G", "b", 1, 2, 4, 9, 9, headers=["Q1_2025", "Q2_2025", "YTD 2025", "Q1_2026", "YTD 2026"]),
        ],
        want=[0, 1],
        contains="YTD 2025 is 4, but Q1_2025, Q2_2025 sum to 3",
        headers=["Q1_2025", "Q2_2025", "YTD 2025", "Q1_2026", "YTD 2026"],
    ),
    Case(
        "a total column with no parts is not checked",
        {"column_sums": [{"total": "YTD (\\d{4})", "parts": "Q[1-4]_(\\d{4})"}]},
        [row("G", "a", 1, 7, headers=["Q1_2025", "YTD 2026"])],
        headers=["Q1_2025", "YTD 2026"],
    ),
]  # fmt: skip


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_reconcile(case: Case) -> None:
    rule = Reconcile.model_validate(case.rule)
    if case.refused is not None and len(case.want) > rule.max_failed_rows:
        with pytest.raises(ReconcileRefused, match=case.refused) as e:
            failed_checks(rule, case.rows, case.headers)
        assert case.contains is None or case.contains in str(e.value)
        return
    got = failed_checks(rule, case.rows, case.headers)
    assert [i for i, f in enumerate(got) if f] == case.want
    if case.contains is not None:
        assert any(case.contains in c for f in got for c in f)


def test_a_grand_total_is_a_total_whatever_its_label() -> None:
    rule = Reconcile()
    assert is_total(rule, row("", "All diseases", 1, 1, grand=True))
    assert not is_total(rule, row("", "Total", 1, 1))


@pytest.mark.parametrize(
    ("rule", "match"),
    [
        ({"tolerance": -1}, "greater than or equal to 0"),
        ({"max_failed_rows": -1}, "greater than or equal to 0"),
        ({"max_failed_rows": 5000}, "less than or equal to 1000"),
        ({"column_sums": [{"total": "YTD \\d{4}", "parts": "Q(\\d)"}]}, "exactly 1 group"),
        ({"total_rows": "(a)\\1"}, "linear time"),
        ({"columns": ["(?<=x)y"]}, "linear time"),
        ({"total_row": "Total"}, "Extra inputs"),
    ],
)
def test_reconcile_config_refusals(rule: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        Reconcile.model_validate(rule)
