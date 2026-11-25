"""Check a printed table against its own printed totals, as metadata: no value is changed (ADR 0016, ELT).

Generic and driven by the catalog's ``reconcile`` block (source_config.Reconcile): which rows are totals, which
columns are summed down, which columns sum across, and how far apart two numbers may be and still count as equal.
Each failed check is a plain sentence recorded on the row it concerns (``failed_checks``); a table with more
failing rows than ``max_failed_rows`` was misread, so the whole document is refused (fail closed). A check with a
suppressed or blank value in it is skipped: its true sum is unknown.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from health_data_registry.adapters import LayoutError
from health_data_registry.source_config import Reconcile, match_whole

FAILED_CHECKS = "failed_checks"


class ReconcileRefused(LayoutError):
    """More rows disagree with the table's own totals than the catalog allows: the table was misread."""

    code = "totals_disagree"


@dataclass(frozen=True)
class Row:
    """One printed row: its group and label, whether it is the structural grand-total line, and its values by
    column header (None for a suppressed or blank cell)."""

    group: str
    label: str
    values: Mapping[str, float | None]
    grand_total: bool = False


def _equal(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance + 1e-9


def _name(row: Row) -> str:
    return f"{row.group} {row.label}".strip()


def is_total(rule: Reconcile, row: Row) -> bool:
    """A grand-total line, or a row whose label matches the declared subtotal pattern whole."""
    return row.grand_total or (
        rule.total_rows is not None and match_whole(rule.total_rows, row.label) is not None
    )


def _down(rule: Reconcile, rows: Sequence[Row], headers: Sequence[str]) -> dict[int, list[str]]:
    """Each total row against the sum of the rows it covers, in every declared column."""
    columns = [h for h in headers if any(match_whole(p, h) is not None for p in rule.columns)]
    out: dict[int, list[str]] = {}
    totals = [is_total(rule, r) for r in rows]
    for k, t in enumerate(rows):
        if not totals[k]:
            continue
        # A subtotal covers its own group's rows; the grand total, or a total with no group, covers every row.
        members = [
            r
            for i, r in enumerate(rows)
            if not totals[i] and (t.grand_total or not t.group or r.group == t.group)
        ]
        if not members:
            continue
        for h in columns:
            total, parts = t.values.get(h), [m.values.get(h) for m in members]
            if total is None or any(v is None for v in parts):
                continue
            expected = sum(v for v in parts if v is not None)
            if not _equal(total, expected, rule.tolerance):
                out.setdefault(k, []).append(
                    f"{_name(t)}: {h} is {total:g}, but the {len(members)} rows it totals sum to {expected:g}"
                )
    return out


def _across(rule: Reconcile, rows: Sequence[Row], headers: Sequence[str]) -> dict[int, list[str]]:
    """Each declared total column against the sum of the part columns that share its key, row by row."""
    out: dict[int, list[str]] = {}
    for pair in rule.column_sums:
        for th in headers:
            tm = match_whole(pair.total, th)
            if tm is None:
                continue
            key = tm.group(1)
            parts = [
                h for h in headers if h != th and (pm := match_whole(pair.parts, h)) and pm.group(1) == key
            ]
            if not parts:
                continue
            for k, row in enumerate(rows):
                total, vals = row.values.get(th), [row.values.get(h) for h in parts]
                if total is None or any(v is None for v in vals):
                    continue
                expected = sum(v for v in vals if v is not None)
                if not _equal(total, expected, rule.tolerance):
                    out.setdefault(k, []).append(
                        f"{_name(row)}: {th} is {total:g}, but {', '.join(parts)} sum to {expected:g}"
                    )
    return out


def failed_checks(rule: Reconcile, rows: Sequence[Row], headers: Sequence[str]) -> list[list[str]]:
    """The failed checks of each row, in row order (empty when it reconciles or can't be checked).

    Refused, whole, when more than ``max_failed_rows`` rows fail."""
    down, across = _down(rule, rows, headers), _across(rule, rows, headers)
    found = [down.get(k, []) + across.get(k, []) for k in range(len(rows))]
    failing = [f for f in found if f]
    if len(failing) > rule.max_failed_rows:
        shown = [c for f in failing for c in f][:5]
        raise ReconcileRefused(
            f"{len(failing)} rows disagree with the table's own printed totals, more than the "
            f"{rule.max_failed_rows} the catalog allows, so the table was not read: " + "; ".join(shown)
        )
    return found
