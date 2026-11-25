"""Statistics for the behaviour eval: Wilson intervals, McNemar's test, the DEV-TEST gap and per-class tables.

Formulas are the rubric's (eval/README.md). The Wilson interval is the one in
`health_data_registry.evaluation.wilson`, reused so the two can't drift.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from health_data_registry.evaluation import wilson

from .scorer import CLASSES, Protocol_

__all__ = [
    "ClassRow",
    "Gap",
    "McNemar",
    "class_table",
    "dev_test_gap",
    "mcnemar",
    "mcnemar_exact_p",
    "wilson",
]

PASS_COLUMN: dict[str, str] = {"A": "pass", "B": "pass_strict"}  # the results file's column per protocol
EXACT_BELOW = 25  # rubric: exact binomial test when b + c < 25, else chi-square with continuity correction


@dataclass(frozen=True)
class McNemar:
    """Paired before/after PASS on the same ids. b = regressions (PASS to FAIL), c = fixes (FAIL to PASS)."""

    a: int
    b: int
    c: int
    d: int
    p_value: float
    method: Literal["exact", "chi2", "none"]
    regressed: tuple[str, ...]
    fixed: tuple[str, ...]

    @property
    def net(self) -> int:
        """Fixes minus regressions."""
        return self.c - self.b


def mcnemar_exact_p(b: int, c: int) -> float:
    """Exact two-sided McNemar: p = min(1, 2 * P[X <= min(b, c)]), X ~ Binomial(b + c, 1/2). b + c = 0 gives 1."""
    if b < 0 or c < 0:
        raise ValueError("discordant counts can't be negative")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / float(2**n)
    return min(1.0, 2 * tail)


def _chi2_p(b: int, c: int) -> float:
    """Chi-square with continuity correction, 1 df: (|b - c| - 1)^2 / (b + c); P = erfc(sqrt(x / 2))."""
    x = (abs(b - c) - 1) ** 2 / (b + c)
    return math.erfc(math.sqrt(x / 2))


def mcnemar(before: Mapping[str, bool], after: Mapping[str, bool]) -> McNemar:
    """McNemar's test on paired PASS/FAIL. Both runs must score exactly the same ids, or it raises."""
    if set(before) != set(after):
        raise ValueError("before and after must score the same question ids (same split, same labels)")
    a = b = c = d = 0
    regressed: list[str] = []
    fixed: list[str] = []
    for qid in sorted(before):
        x, y = bool(before[qid]), bool(after[qid])
        if x and y:
            a += 1
        elif x:
            b += 1
            regressed.append(qid)
        elif y:
            c += 1
            fixed.append(qid)
        else:
            d += 1
    method: Literal["exact", "chi2", "none"]
    if b + c == 0:
        p, method = 1.0, "none"
    elif b + c < EXACT_BELOW:
        p, method = mcnemar_exact_p(b, c), "exact"
    else:
        p, method = _chi2_p(b, c), "chi2"
    return McNemar(a, b, c, d, p, method, tuple(regressed), tuple(fixed))


@dataclass(frozen=True)
class Gap:
    """DEV pass rate minus TEST pass rate, with Newcombe's hybrid score interval built from Wilson limits."""

    dev_rate: float
    test_rate: float
    difference: float
    low: float
    high: float


def dev_test_gap(dev_passes: int, dev_n: int, test_passes: int, test_n: int, z: float = 1.96) -> Gap:
    """Newcombe (1998) method 10 for the difference of two independent proportions, p1 = DEV, p2 = TEST.

    low = d - sqrt((p1 - l1)^2 + (u2 - p2)^2), high = d + sqrt((u1 - p1)^2 + (p2 - l2)^2), where (l, u) are
    each split's Wilson limits. A gap whose interval excludes 0 suggests tuning leaked into DEV.
    """
    for k, n in ((dev_passes, dev_n), (test_passes, test_n)):
        if n <= 0 or not 0 <= k <= n:
            raise ValueError("each split needs n > 0 and 0 <= passes <= n")
    p1, p2 = dev_passes / dev_n, test_passes / test_n
    l1, u1 = wilson(dev_passes, dev_n, z)
    l2, u2 = wilson(test_passes, test_n, z)
    d = p1 - p2
    return Gap(
        p1,
        p2,
        d,
        d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2),
        d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2),
    )


@dataclass(frozen=True)
class ClassRow:
    """One row of the rubric's per-class table."""

    label: str
    n: int
    passes: int
    rate: float
    low: float
    high: float
    mean_total: float
    fabrication_fails: int


def class_table(rows: Iterable[Mapping[str, object]], protocol: Protocol_ = "A") -> list[ClassRow]:
    """Per expected class, then "Overall". Classes with no questions are left out, as the baseline did.

    Each row needs `expected_class`, `total`, `fabricated` and the protocol's result: `pass` for A,
    `pass_strict` for B.
    """
    column = PASS_COLUMN[protocol]
    rows = list(rows)
    out: list[ClassRow] = []
    for label in (*CLASSES, "Overall"):
        group = rows if label == "Overall" else [r for r in rows if r["expected_class"] == label]
        n = len(group)
        if n == 0:
            continue
        k = sum(bool(r[column]) for r in group)
        lo, hi = wilson(k, n)
        total = sum(_as_int(r["total"]) for r in group)
        fab = sum(bool(r["fabricated"]) for r in group)
        out.append(ClassRow(label, n, k, k / n, lo, hi, total / n, fab))
    return out


def _as_int(v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("total must be an integer 0-6")
    return v
