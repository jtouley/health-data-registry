"""eval/stats.py against published values and brute force: Wilson, exact and chi-square McNemar, the DEV-TEST gap.

Golden values:
- Wilson score interval: Newcombe RG. Two-sided confidence intervals for the single proportion: comparison of
  seven methods. Stat Med 1998;17:857-872, table I: 81/263 gives 0.2553 to 0.3662, 15/148 gives 0.0624 to 0.1605.
- Difference of independent proportions, hybrid score (method 10): Newcombe RG. Interval estimation for the
  difference between independent proportions. Stat Med 1998;17:873-890, example (a): 56/70 - 48/80 gives
  0.0524 to 0.3339.
- Chi-square upper tail, 1 df: P(X > 2.7) = 0.1003 and P(X > 3.841) = 0.0500 (standard tables).
"""

from __future__ import annotations

import math
from fractions import Fraction

import pytest

from eval.stats import class_table, dev_test_gap, mcnemar, mcnemar_exact_p, wilson


@pytest.mark.parametrize(
    ("k", "n", "low", "high"),
    [(81, 263, 0.2553, 0.3662), (15, 148, 0.0624, 0.1605), (0, 20, 0.0, 0.1611), (20, 20, 0.8389, 1.0)],
)
def test_wilson_matches_newcombe_table(k: int, n: int, low: float, high: float) -> None:
    # Act
    lo, hi = wilson(k, n)
    # Assert
    assert lo == pytest.approx(low, abs=1e-4)
    assert hi == pytest.approx(high, abs=1e-4)


def _exact_by_fractions(b: int, c: int) -> float:
    n = b + c
    tail = sum(Fraction(math.comb(n, i), 2**n) for i in range(min(b, c) + 1))
    return float(min(Fraction(1), 2 * tail))


@pytest.mark.parametrize(
    ("b", "c", "p"),
    [(0, 0, 1.0), (1, 6, 0.125), (0, 5, 0.0625), (3, 3, 1.0), (6, 1, 0.125), (0, 1, 1.0)],
)
def test_exact_mcnemar_known_values(b: int, c: int, p: float) -> None:
    # Act / Assert
    assert mcnemar_exact_p(b, c) == pytest.approx(p)


@pytest.mark.parametrize(("b", "c"), [(b, c) for b in range(0, 13, 3) for c in range(0, 12, 4)])
def test_exact_mcnemar_equals_brute_force(b: int, c: int) -> None:
    # Act / Assert
    assert mcnemar_exact_p(b, c) == pytest.approx(_exact_by_fractions(b, c) if b + c else 1.0)


def test_exact_mcnemar_refuses_negative_counts() -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="negative"):
        mcnemar_exact_p(-1, 3)


def _paired(a: int, b: int, c: int, d: int) -> tuple[dict[str, bool], dict[str, bool]]:
    before: dict[str, bool] = {}
    after: dict[str, bool] = {}
    for kind, count, x, y in (
        ("a", a, True, True),
        ("b", b, True, False),
        ("c", c, False, True),
        ("d", d, False, False),
    ):
        for i in range(count):
            before[f"{kind}{i}"], after[f"{kind}{i}"] = x, y
    return before, after


def test_mcnemar_builds_the_table_and_lists_regressions() -> None:
    # Arrange
    before, after = _paired(a=10, b=1, c=6, d=3)
    # Act
    m = mcnemar(before, after)
    # Assert
    assert (m.a, m.b, m.c, m.d, m.net) == (10, 1, 6, 3, 5)
    assert m.method == "exact"
    assert m.p_value == pytest.approx(0.125)
    assert m.regressed == ("b0",)
    assert len(m.fixed) == 6


@pytest.mark.parametrize(
    ("b", "c", "method", "p"),
    [(10, 20, "chi2", 0.1003), (12, 13, "chi2", 1.0), (0, 0, "none", 1.0), (4, 20, "exact", None)],
)
def test_mcnemar_switches_to_chi_square_at_25_discordant(
    b: int, c: int, method: str, p: float | None
) -> None:
    # Arrange
    before, after = _paired(a=2, b=b, c=c, d=2)
    # Act
    m = mcnemar(before, after)
    # Assert
    assert m.method == method
    assert m.p_value == pytest.approx(p if p is not None else _exact_by_fractions(b, c), abs=1e-4)


def test_mcnemar_refuses_different_question_sets() -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="same question ids"):
        mcnemar({"q1": True}, {"q2": True})


def test_dev_test_gap_matches_newcombe_method_10() -> None:
    # Act
    g = dev_test_gap(56, 70, 48, 80)
    # Assert
    assert g.difference == pytest.approx(0.2)
    assert g.low == pytest.approx(0.0524, abs=1e-4)
    assert g.high == pytest.approx(0.3339, abs=1e-4)


@pytest.mark.parametrize(("k1", "n1", "k2", "n2"), [(1, 0, 1, 2), (3, 2, 1, 2), (1, 2, -1, 2)])
def test_dev_test_gap_refuses_impossible_counts(k1: int, n1: int, k2: int, n2: int) -> None:
    # Act / Assert
    with pytest.raises(ValueError):
        dev_test_gap(k1, n1, k2, n2)


def test_class_table_groups_by_class_and_skips_empty_classes() -> None:
    # Arrange
    rows = [
        {"expected_class": "ANSWER", "pass": True, "pass_strict": False, "total": 6, "fabricated": False},
        {"expected_class": "ANSWER", "pass": False, "pass_strict": False, "total": 0, "fabricated": True},
        {
            "expected_class": "OUT_OF_SCOPE",
            "pass": True,
            "pass_strict": True,
            "total": 6,
            "fabricated": False,
        },
    ]
    # Act
    a = class_table(rows, "A")
    b = class_table(rows, "B")
    # Assert
    assert [r.label for r in a] == ["ANSWER", "OUT_OF_SCOPE", "Overall"]
    assert (a[0].n, a[0].passes, a[0].mean_total, a[0].fabrication_fails) == (2, 1, 3.0, 1)
    assert (a[-1].passes, b[-1].passes) == (2, 1)
    assert (a[-1].low, a[-1].high) == wilson(2, 3)


def test_class_table_refuses_a_non_integer_total() -> None:
    # Act / Assert
    with pytest.raises(ValueError, match="integer"):
        class_table([{"expected_class": "ANSWER", "pass": True, "total": True, "fabricated": False}])
