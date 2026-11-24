"""Golden statistics for stats.py, each computed two independent ways, plus property and metamorphic tests.

Synthetic numbers only. Published values used as golden answers:

- Garwood exact Poisson 95% limits for k = 0, 1, 2, 5, 10, 20 (Garwood F. Fiducial limits for the Poisson
  distribution. Biometrika 1936;28:437-442; the same values are tabulated in Ulm K. A simple method to calculate
  the confidence interval of a standardized mortality ratio. Am J Epidemiol 1990;131:373-375).
- Chi-square quantiles from any standard table: chi2(0.95; 1) = 3.841, chi2(0.975; 10) = 20.483,
  chi2(0.025; 10) = 3.247.
- Rate ratio and difference: Rothman KJ, Epidemiology: An Introduction (Oxford University Press), the breast
  cancer after fluoroscopy example: 41 cases in 28,010 person-years vs 15 in 19,017; IRR 1.86 with 90% CI
  1.13 to 3.05, and IRD 68 per 100,000 person-years with 90% CI 17 to 118.
- The 2000 US standard population: NCHS Healthy People 2010 Statistical Notes no. 20, table 2 (11 groups),
  and NCI SEER's 19-group standard million.
"""

from __future__ import annotations

import math
import statistics
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from health_data_registry import stats

Z95 = 1.959963984540054  # standard normal 0.975 quantile, from tables
Z90 = 1.6448536269514722


# ---------------------------------------------------------------- an independent exact Poisson interval


def _poisson_cdf(k: int, mu: float) -> float:
    """P(X <= k) by summing the probabilities, each in log space so large means don't underflow."""
    if mu == 0:
        return 1.0
    return math.fsum(math.exp(i * math.log(mu) - mu - math.lgamma(i + 1)) for i in range(k + 1))


def _bisect(f, lo: float, hi: float) -> float:  # type: ignore[no-untyped-def]
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def poisson_ci_by_cdf(k: int, alpha: float = 0.05) -> tuple[float, float]:
    """The exact interval by its definition: P(X >= k; L) = alpha/2 and P(X <= k; U) = alpha/2."""
    upper = _bisect(lambda mu: _poisson_cdf(k, mu) - alpha / 2, 0.0, 10.0 * k + 20.0)
    if k == 0:
        return 0.0, upper
    lower = _bisect(lambda mu: alpha / 2 - (1.0 - _poisson_cdf(k - 1, mu)), 0.0, 10.0 * k + 20.0)
    return lower, upper


GARWOOD = {  # k: (lower, upper), 95%
    0: (0.0, 3.689),
    1: (0.0253, 5.572),
    2: (0.2422, 7.225),
    5: (1.623, 11.668),
    10: (4.795, 18.390),
    20: (12.217, 30.888),
}


@pytest.mark.parametrize("k", sorted(GARWOOD))
def test_garwood_matches_published_table_and_cdf_inversion(k: int) -> None:
    lo, hi = stats.poisson_exact_ci(k)
    pub_lo, pub_hi = GARWOOD[k]
    assert lo == pytest.approx(pub_lo, abs=6e-4)
    assert hi == pytest.approx(pub_hi, abs=6e-4)
    ind_lo, ind_hi = poisson_ci_by_cdf(k)
    assert lo == pytest.approx(ind_lo, rel=1e-9, abs=1e-12)
    assert hi == pytest.approx(ind_hi, rel=1e-9)


@pytest.mark.parametrize("k", [3, 37, 250, 1000])
def test_garwood_large_counts_two_ways(k: int) -> None:
    assert stats.poisson_exact_ci(k) == pytest.approx(poisson_ci_by_cdf(k), rel=1e-8)
    lo90, hi90 = stats.poisson_exact_ci(k, level=0.90)
    assert (lo90, hi90) == pytest.approx(poisson_ci_by_cdf(k, alpha=0.10), rel=1e-8)


def test_chi2_and_normal_quantiles_match_tables() -> None:
    assert stats.chi2_quantile(0.95, 1) == pytest.approx(3.841459, abs=1e-6)
    assert stats.chi2_quantile(0.975, 10) == pytest.approx(20.483177, abs=1e-6)
    assert stats.chi2_quantile(0.025, 10) == pytest.approx(3.246973, abs=1e-6)
    assert stats.normal_quantile(0.975) == pytest.approx(Z95, abs=1e-10)
    assert stats.normal_quantile(0.95) == pytest.approx(Z90, abs=1e-10)
    assert stats.normal_quantile(0.5) == pytest.approx(0.0, abs=1e-12)


def _gamma_cdf_by_integration(x: float, shape: float, n: int = 20000) -> float:
    """P(a, x) by Simpson's rule, independent of stats.py: on t directly when the integrand is smooth (a >= 1),
    else on u = t^a, which removes the singularity at 0."""
    if shape >= 1:
        top, h = x, x / n

        def f(t: float) -> float:
            return t ** (shape - 1) * math.exp(-t) if t > 0 else (1.0 if shape == 1 else 0.0)

        scale = 1.0
    else:
        top = x**shape
        h = top / n

        def f(t: float) -> float:
            return math.exp(-(t ** (1.0 / shape)))

        scale = 1.0 / shape
    s = sum(f(i * h) * (1 if i in (0, n) else 4 if i % 2 else 2) for i in range(n + 1))
    return s * h / 3.0 * scale / math.gamma(shape)


@pytest.mark.parametrize("shape,p", [(0.37, 0.025), (2.6, 0.975), (7.25, 0.5), (1.0, 0.9)])
def test_gamma_quantile_against_numerical_integration(shape: float, p: float) -> None:
    q = stats.gamma_quantile(p, shape)
    assert _gamma_cdf_by_integration(q, shape) == pytest.approx(p, abs=1e-6)


def test_gamma_cdf_closed_forms() -> None:
    # Gamma(1) is the exponential; Gamma(1/2) at x is erf(sqrt(x)).
    for x in (0.1, 1.0, 3.0, 12.0):
        assert stats.gamma_cdf(x, 1.0) == pytest.approx(1 - math.exp(-x), rel=1e-12)
        assert stats.gamma_cdf(x, 0.5) == pytest.approx(math.erf(math.sqrt(x)), rel=1e-12)
    assert stats.gamma_cdf(0.0, 2.0) == 0.0
    assert stats.gamma_cdf(-1.0, 2.0) == 0.0


@pytest.mark.parametrize(
    "call",
    [
        lambda: stats.gamma_quantile(0.0, 1.0),
        lambda: stats.gamma_quantile(1.0, 1.0),
        lambda: stats.gamma_quantile(0.5, 0.0),
        lambda: stats.gamma_cdf(1.0, 0.0),
        lambda: stats.normal_quantile(1.0),
        lambda: stats.normal_quantile(0.0),
        lambda: stats.poisson_exact_ci(-1),
        lambda: stats.poisson_exact_ci(2.5),  # type: ignore[arg-type]
        lambda: stats.poisson_exact_ci(3, level=1.0),
        lambda: stats.poisson_exact_ci(3, level=0.0),
        lambda: stats.crude_rate(3, 0),
        lambda: stats.rate_ratio(0, 10, 3, 10),
        lambda: stats.rate_ratio(3, 10, 0, 10),
        lambda: stats.rate_ratio(3, 0, 3, 10),
        lambda: stats.rate_ratio(3, 10, 3, 0),
        lambda: stats.rate_difference(-1, 10, 3, 10),
        lambda: stats.rate_difference(1, 10, -3, 10),
        lambda: stats.rate_difference(1, 0, 3, 10),
        lambda: stats.rate_difference(1, 10, 3, 0),
    ],
)
def test_bad_inputs_are_refused(call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(stats.StatsError):
        call()


# ---------------------------------------------------------------- rates, ratio, difference


def test_crude_rate_is_count_over_population_with_scaled_garwood() -> None:
    est = stats.crude_rate(10, 250_000)
    assert est.value == pytest.approx(4.0)
    assert est.lower == pytest.approx(4.795388696 / 2.5, rel=1e-8)
    assert est.upper == pytest.approx(18.390356042 / 2.5, rel=1e-8)
    per_thousand = stats.crude_rate(10, 250_000, per=1000)
    assert per_thousand.value == pytest.approx(0.04)


def test_rate_ratio_and_difference_rothman_example() -> None:
    rr = stats.rate_ratio(41, 28_010, 15, 19_017, level=0.90)
    rd = stats.rate_difference(41, 28_010, 15, 19_017, level=0.90)
    # Published (rounded): IRR 1.86 (1.13-3.05); IRD 68 (17-118) per 100,000 person-years.
    assert (round(rr.value, 2), round(rr.lower, 2), round(rr.upper, 2)) == (1.86, 1.13, 3.05)
    assert rd.value == pytest.approx(68, abs=0.51)  # 67.4995; the book prints 68 from rounded rates
    assert (round(rd.lower), round(rd.upper)) == (17, 118)
    # Second way: by hand from the formulas, with the tabled z.
    ir1, ir0 = 41 / 28_010, 15 / 19_017
    se_log = math.sqrt(1 / 41 + 1 / 15)
    assert rr.value == pytest.approx(ir1 / ir0, rel=1e-12)
    assert rr.lower == pytest.approx(ir1 / ir0 * math.exp(-Z90 * se_log), rel=1e-9)
    assert rr.upper == pytest.approx(ir1 / ir0 * math.exp(Z90 * se_log), rel=1e-9)
    se_d = math.sqrt(41 / 28_010**2 + 15 / 19_017**2) * 1e5
    assert rd.value == pytest.approx((ir1 - ir0) * 1e5, rel=1e-12)
    assert rd.lower == pytest.approx((ir1 - ir0) * 1e5 - Z90 * se_d, rel=1e-9)
    assert rd.upper == pytest.approx((ir1 - ir0) * 1e5 + Z90 * se_d, rel=1e-9)


@settings(max_examples=60, deadline=None)
@given(
    a=st.integers(1, 5000),
    b=st.integers(1, 5000),
    pa=st.floats(1e3, 1e8),
    pb=st.floats(1e3, 1e8),
)
def test_ratio_and_difference_symmetries(a: int, b: int, pa: float, pb: float) -> None:
    fwd, back = stats.rate_ratio(a, pa, b, pb), stats.rate_ratio(b, pb, a, pa)
    assert fwd.value == pytest.approx(1 / back.value, rel=1e-9)
    assert fwd.lower == pytest.approx(1 / back.upper, rel=1e-9)
    assert fwd.lower < fwd.value < fwd.upper
    d1, d2 = stats.rate_difference(a, pa, b, pb), stats.rate_difference(b, pb, a, pa)
    assert d1.value == pytest.approx(-d2.value, rel=1e-9, abs=1e-9)
    assert d1.lower == pytest.approx(-d2.upper, rel=1e-9, abs=1e-9)
    assert d1.lower <= d1.value <= d1.upper


@settings(max_examples=60, deadline=None)
@given(k=st.integers(0, 3000))
def test_garwood_brackets_the_count_and_grows_with_it(k: int) -> None:
    lo, hi = stats.poisson_exact_ci(k)
    lo2, hi2 = stats.poisson_exact_ci(k + 1)
    assert lo <= k < hi
    assert lo < lo2 and hi < hi2
    assert stats.poisson_exact_ci(k, level=0.9)[1] < hi


# ---------------------------------------------------------------- trend


def _profile_slope(years: list[float], counts: list[float], pops: list[float]) -> tuple[float, float]:
    """Independent fit: maximise the profile log-likelihood in b (alpha has a closed form), then its curvature."""

    def profile(b: float) -> float:
        lam = [p * math.exp(b * y) for p, y in zip(pops, years, strict=True)]
        a = math.log(sum(counts) / sum(lam))
        return sum(c * (a + math.log(m)) - math.exp(a) * m for c, m in zip(counts, lam, strict=True))

    lo, hi = -3.0, 3.0
    for _ in range(300):  # ternary search on a concave function
        m1, m2 = lo + (hi - lo) / 3, hi - (hi - lo) / 3
        if profile(m1) < profile(m2):
            lo = m1
        else:
            hi = m2
    b = 0.5 * (lo + hi)
    h = 1e-4
    curv = (profile(b + h) - 2 * profile(b) + profile(b - h)) / h**2
    return b, math.sqrt(-1.0 / curv)


def test_trend_matches_profile_likelihood_fit() -> None:
    years = [2016.0, 2017.0, 2018.0, 2019.0, 2020.0, 2021.0]
    counts = [120.0, 131.0, 150.0, 149.0, 171.0, 190.0]
    pops = [1.00e6, 1.01e6, 1.03e6, 1.04e6, 1.05e6, 1.07e6]
    fit = stats.poisson_trend(years, counts, pops)
    b, se = _profile_slope([y - 2018.5 for y in years], counts, pops)
    assert fit.slope == pytest.approx(b, abs=1e-7)
    assert fit.dispersion < 1  # these counts are not overdispersed, so the Poisson SE stands
    assert fit.se == pytest.approx(se, rel=1e-4)
    assert fit.apc == pytest.approx(100 * math.expm1(b), rel=1e-6)
    assert fit.lower == pytest.approx(100 * math.expm1(b - Z95 * se), rel=1e-4)
    assert fit.upper == pytest.approx(100 * math.expm1(b + Z95 * se), rel=1e-4)
    assert fit.n == 6


def test_trend_recovers_an_exact_exponential_and_widens_for_overdispersion() -> None:
    years = [2010, 2011, 2012, 2013, 2014]
    counts = [100 * 1.05**i for i in range(5)]
    exact = stats.poisson_trend(years, counts)
    assert exact.apc == pytest.approx(5.0, abs=1e-9)
    assert exact.dispersion == pytest.approx(0.0, abs=1e-12)
    noisy = stats.poisson_trend(years, [100, 160, 90, 170, 115])
    plain_var = noisy.se**2 / noisy.dispersion
    assert noisy.dispersion > 1
    _b, se_poisson = _profile_slope([y - 2012 for y in years], [100, 160, 90, 170, 115], [1.0] * 5)
    assert math.sqrt(plain_var) == pytest.approx(se_poisson, rel=1e-4)


def test_trend_metamorphic_relations() -> None:
    years = [2015, 2016, 2017, 2018, 2019]
    counts = [40.0, 44.0, 47.0, 55.0, 58.0]
    pops = [2e5, 2.02e5, 2.05e5, 2.06e5, 2.1e5]
    base = stats.poisson_trend(years, counts, pops)
    scaled_pop = stats.poisson_trend(years, counts, [p * 7 for p in pops])
    assert scaled_pop.apc == pytest.approx(base.apc, rel=1e-9)
    reversed_years = stats.poisson_trend([-y for y in years], counts, pops)
    assert reversed_years.slope == pytest.approx(-base.slope, rel=1e-9)
    shifted = stats.poisson_trend([y + 100 for y in years], counts, pops)
    assert shifted.apc == pytest.approx(base.apc, rel=1e-9)
    doubled = stats.poisson_trend(years, [2 * c for c in counts], pops)
    assert doubled.apc == pytest.approx(base.apc, rel=1e-9)
    # Twice the counts at the same dispersion-free fit: the Poisson SE shrinks by sqrt(2) (before any scaling).
    assert doubled.se**2 / max(1, doubled.dispersion) == pytest.approx(
        base.se**2 / max(1, base.dispersion) / 2, rel=1e-6
    )
    narrower = stats.poisson_trend(years, counts, pops, level=0.80)
    assert base.lower < narrower.lower < narrower.upper < base.upper


@pytest.mark.parametrize(
    "years,counts,pops",
    [
        ([2020, 2021], [1.0, 2.0], None),  # fewer than three points
        ([2020, 2020, 2020], [1.0, 2.0, 3.0], None),  # one year only
        ([2020, 2021, 2022], [0.0, 0.0, 0.0], None),  # nothing to fit
        ([2020, 2021, 2022], [1.0, -2.0, 3.0], None),
        ([2020, 2021, 2022], [1.0, 2.0, 3.0], [1.0, 0.0, 1.0]),
        ([2020, 2021, 2022], [1.0, 2.0], None),
        ([2020, 2021, 2022], [1.0, 2.0, 3.0], [1.0, 1.0]),
        ([2020, 2021, 2022], [0.0, 0.0, 9.0], None),  # all at one end: the slope runs off to infinity
    ],
)
def test_trend_refuses_what_it_cannot_fit(years, counts, pops) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(stats.StatsError):
        stats.poisson_trend(years, counts, pops)


# ---------------------------------------------------------------- age adjustment


def test_standard_populations_match_their_sources() -> None:
    assert sum(w for *_, w in stats.US2000_STD_11) == 1_000_000
    assert sum(w for *_, w in stats.US2000_STD_19) == 1_000_000
    eleven = {(lo, hi): w for lo, hi, w in stats.US2000_STD_11}
    assert eleven[(15, 24)] == 138_646 and eleven[(75, 84)] == 44_842 and eleven[(85, None)] == 15_508
    # The 19 groups add up to the 11 groups within the one-per-million rounding NCHS describes.
    for lo, hi, w in stats.US2000_STD_11:
        parts = [
            x for a, b, x in stats.US2000_STD_19 if a >= lo and (hi is None or (b is not None and b <= hi))
        ]
        assert abs(sum(parts) - w) <= 1


def test_direct_standardisation_by_hand_and_single_group_reduces_to_garwood() -> None:
    counts = [2, 1, 3, 10, 14, 20, 25, 30, 40, 52, 30]
    pops = [9_000, 38_000, 101_000, 99_000, 95_000, 120_000, 110_000, 70_000, 52_000, 33_000, 11_000]
    std = [w for *_, w in stats.US2000_STD_11]
    est = stats.direct_standardize(counts, pops, std)
    by_hand = sum(Fraction(d, n) * Fraction(w, 1_000_000) for d, n, w in zip(counts, pops, std, strict=True))
    assert est.value == pytest.approx(float(by_hand) * 1e5, rel=1e-12)
    assert est.lower < est.value < est.upper
    # Fay-Feuer with one group is exactly the Garwood interval for that group's rate.
    one = stats.direct_standardize([10], [250_000], [1.0])
    assert (one.lower, one.upper) == pytest.approx((4.795388696 / 2.5, 18.390356042 / 2.5), rel=1e-8)
    zero = stats.direct_standardize([0], [100_000], [1.0])
    assert zero.value == 0 and zero.lower == 0 and zero.upper == pytest.approx(3.688879, rel=1e-6)


def test_fay_feuer_formula_two_ways() -> None:
    counts, pops, std = [4, 9, 30], [20_000, 50_000, 40_000], [0.3, 0.5, 0.2]
    est = stats.direct_standardize(counts, pops, std, per=1.0)
    w = [s / p for s, p in zip(std, pops, strict=True)]
    y = sum(wi * d for wi, d in zip(w, counts, strict=True))
    v = sum(wi * wi * d for wi, d in zip(w, counts, strict=True))
    wm = max(w)
    # Chi-square form of Fay and Feuer (1997), eq. 4-5, solved with the independent integrator.
    lo_q = _bisect(lambda x: 0.025 - _gamma_cdf_by_integration(x, y * y / v), 1e-9, 50.0)
    hi_shape = (y + wm) ** 2 / (v + wm * wm)
    hi_q = _bisect(lambda x: 0.975 - _gamma_cdf_by_integration(x, hi_shape), 1e-9, 80.0)
    assert est.value == pytest.approx(y, rel=1e-12)
    assert est.lower == pytest.approx(lo_q * v / y, rel=1e-5)
    assert est.upper == pytest.approx(hi_q * (v + wm * wm) / (y + wm), rel=1e-5)


@settings(max_examples=40, deadline=None)
@given(
    rate=st.floats(1e-5, 1e-2),
    counts=st.lists(st.integers(1, 5000), min_size=11, max_size=11),
)
def test_equal_age_specific_rates_give_that_rate(rate: float, counts: list[int]) -> None:
    """Metamorphic: if every age group has the same rate, the adjusted rate is that rate, whatever the weights."""
    pops = [c / rate for c in counts]
    est = stats.direct_standardize(counts, pops, [w for *_, w in stats.US2000_STD_11], per=1.0)
    assert est.value == pytest.approx(rate, rel=1e-9)
    assert est.lower < est.value < est.upper
    crude = stats.crude_rate(sum(counts), sum(pops), per=1.0)
    assert crude.value == pytest.approx(rate, rel=1e-9)


@pytest.mark.parametrize(
    "counts,pops,std",
    [([1], [1, 2], [1]), ([], [], []), ([1, 2], [10, 0], [1, 1]), ([-1], [10], [1]), ([1.5], [10], [1]),
     ([1], [10], [0]), ([1, 1], [10, 10], [1, -1])],
)  # fmt: skip
def test_direct_standardisation_refuses_bad_inputs(counts, pops, std) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(stats.StatsError):
        stats.direct_standardize(counts, pops, std)


# ---------------------------------------------------------------- historical limits


def test_historical_limits_by_hand() -> None:
    history = [10, 12, 9, 11, 14, 8, 10, 13, 12, 9, 11, 10, 12, 15, 9]
    mean, sd = statistics.mean(history), statistics.stdev(history)
    sig = stats.historical_limits(20, history)
    assert (sig.mean, sig.sd) == pytest.approx((mean, sd), rel=1e-12)
    assert sig.ratio == pytest.approx(20 / mean)
    assert sig.upper_limit == pytest.approx(1 + 2 * sd / mean)
    assert sig.lower_limit == pytest.approx(1 - 2 * sd / mean)
    assert sig.flag == "above"  # 20 > mean + 2 SD = 15.0
    assert stats.historical_limits(11, history).flag == "within"
    assert stats.historical_limits(5, history).flag == "below"
    # Exactly on the limit is not beyond it.
    on_edge = mean + 2 * sd
    assert stats.historical_limits(on_edge, history).flag == "within"
    wide = stats.historical_limits(0, [0, 0, 30, 0, 0])
    assert wide.lower_limit == 0.0 and wide.flag == "within"


@settings(max_examples=40, deadline=None)
@given(
    hist=st.lists(st.integers(1, 500), min_size=15, max_size=15),
    cur=st.integers(0, 1000),
    k=st.integers(2, 9),
)
def test_historical_limits_ignore_scale(hist: list[int], cur: int, k: int) -> None:
    a = stats.historical_limits(cur, hist)
    b = stats.historical_limits(cur * k, [h * k for h in hist])
    assert b.ratio == pytest.approx(a.ratio, rel=1e-9)
    assert b.upper_limit == pytest.approx(a.upper_limit, rel=1e-9)
    assert b.flag == a.flag


@pytest.mark.parametrize("cur,hist", [(3, [1]), (3, [0, 0, 0]), (-1, [1, 2]), (1, [1, -2])])
def test_historical_limits_refuse(cur, hist) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(stats.StatsError):
        stats.historical_limits(cur, hist)


# ---------------------------------------------------------------- sparse counts: exact conditional rate ratio

# Clopper-Pearson 95% limits from published tables (Clopper CJ, Pearson ES. Biometrika 1934;26:404-413;
# reproduced e.g. in Vollset SE. Confidence intervals for a binomial proportion. Stat Med 1993;12:809-824).
CLOPPER_PEARSON = {
    (0, 10): (0.0, 0.3085),
    (1, 10): (0.0025, 0.4450),
    (5, 10): (0.1871, 0.8129),
    (2, 20): (0.0123, 0.3170),
    (10, 10): (0.6915, 1.0),
}


def _beta_cdf_by_integration(p: float, a: float, b: float, n: int = 20000) -> float:
    """I_p(a, b) for whole a, b >= 1 by Simpson's rule on the beta density; independent of stats.py."""
    if p <= 0:
        return 0.0
    h = p / n
    log_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)

    def f(t: float) -> float:
        if t <= 0:
            return 1.0 / math.exp(log_beta) if a == 1 else 0.0
        if t >= 1:
            return 1.0 / math.exp(log_beta) if b == 1 else 0.0
        return math.exp((a - 1) * math.log(t) + (b - 1) * math.log1p(-t) - log_beta)

    return sum(f(i * h) * (1 if i in (0, n) else 4 if i % 2 else 2) for i in range(n + 1)) * h / 3.0


@pytest.mark.parametrize("x,n", sorted(CLOPPER_PEARSON))
def test_clopper_pearson_matches_tables_and_beta_integral(x: int, n: int) -> None:
    lo, hi = stats.binomial_exact_ci(x, n)
    pub_lo, pub_hi = CLOPPER_PEARSON[(x, n)]
    assert (lo, hi) == (pytest.approx(pub_lo, abs=6e-5), pytest.approx(pub_hi, abs=6e-5))
    # Second way: the limits are beta quantiles, Beta(x, n - x + 1) and Beta(x + 1, n - x).
    if x > 0:
        assert _beta_cdf_by_integration(lo, x, n - x + 1) == pytest.approx(0.025, abs=1e-6)
    if x < n:
        assert _beta_cdf_by_integration(hi, x + 1, n - x) == pytest.approx(0.975, abs=1e-6)


def test_exact_rate_ratio_two_ways_and_boundaries() -> None:
    # 3 cases in 40,000 vs 12 in 100,000: given 15 cases, a ~ Binomial(15, pi), pi = RR*40k / (RR*40k + 100k).
    est = stats.rate_ratio_exact(3, 40_000, 12, 100_000)
    lo_pi, hi_pi = stats.binomial_exact_ci(3, 15)
    assert est.value == pytest.approx((3 / 40_000) / (12 / 100_000))
    for rr, pi in ((est.lower, lo_pi), (est.upper, hi_pi)):
        assert rr * 40_000 / (rr * 40_000 + 100_000) == pytest.approx(pi, rel=1e-9)
    assert est.lower < est.value < est.upper
    # Exact limits are wider than the log method's for sparse counts.
    approx = stats.rate_ratio(3, 40_000, 12, 100_000)
    assert est.lower < approx.lower and est.upper > approx.upper
    # Zero cases in the index group: RR 0, lower 0, finite upper. Zero in the comparison group: refused.
    zero = stats.rate_ratio_exact(0, 50_000, 7, 50_000)
    assert (zero.value, zero.lower) == (0.0, 0.0) and 0 < zero.upper < math.inf
    assert zero.upper == pytest.approx(
        stats.binomial_exact_ci(0, 7)[1] / (1 - stats.binomial_exact_ci(0, 7)[1])
    )
    with pytest.raises(stats.StatsError):
        stats.rate_ratio_exact(4, 1e5, 0, 1e5)
    assert stats.SMALL_COUNT == 5


@pytest.mark.parametrize("a,b", [(1, 1), (1, 4), (4, 1), (2, 3), (0, 1), (0, 4)])
def test_exact_rate_ratio_under_five_cases(a: int, b: int) -> None:
    est = stats.rate_ratio_exact(a, 1e5, b, 2e5)
    lo, hi = stats.binomial_exact_ci(a, a + b)
    assert est.lower == pytest.approx(lo / (1 - lo) * 2) and est.upper == pytest.approx(hi / (1 - hi) * 2)
    assert est.lower <= est.value <= est.upper


def test_exact_and_log_rate_ratio_agree_for_large_counts() -> None:
    exact, log = stats.rate_ratio_exact(4000, 1e7, 3000, 1e7), stats.rate_ratio(4000, 1e7, 3000, 1e7)
    assert exact.lower == pytest.approx(log.lower, rel=2e-3) and exact.upper == pytest.approx(
        log.upper, rel=2e-3
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda: stats.binomial_exact_ci(3, 0),
        lambda: stats.binomial_exact_ci(-1, 5),
        lambda: stats.binomial_exact_ci(6, 5),
        lambda: stats.binomial_exact_ci(1.5, 5),  # type: ignore[arg-type]
        lambda: stats.rate_ratio_exact(-1, 1e5, 3, 1e5),
        lambda: stats.rate_ratio_exact(1, 0, 3, 1e5),
        lambda: stats.rate_ratio_exact(1, 1e5, 3, 0),
    ],
)
def test_exact_methods_refuse_bad_inputs(call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(stats.StatsError):
        call()


def test_binomial_cdf_edges() -> None:
    assert stats._binom_cdf(-1, 5, 0.3) == 0.0
    assert stats._binom_cdf(5, 5, 0.3) == 1.0
    assert stats._binom_cdf(2, 5, 0.0) == 1.0
    assert stats._binom_cdf(2, 5, 1.0) == 0.0
    # Both tails summed agree with the closed form.
    exact = sum(math.comb(9, i) * 0.4**i * 0.6 ** (9 - i) for i in range(7))
    assert stats._binom_cdf(6, 9, 0.4) == pytest.approx(exact, rel=1e-12)
    assert stats._binom_cdf(2, 9, 0.4) == pytest.approx(
        sum(math.comb(9, i) * 0.4**i * 0.6 ** (9 - i) for i in range(3)), rel=1e-12
    )


@pytest.mark.parametrize(
    "call,words",
    [
        (lambda: stats.rate_ratio(0, 10, 3, 10), "at least one case in each group"),
        (lambda: stats.rate_ratio(3, 0, 3, 10), "population must be positive"),
        (lambda: stats.rate_ratio(3, 10, 3, 0), "population must be positive"),
        (lambda: stats.rate_ratio_exact(-1, 10, 3, 10), "whole numbers, zero or more"),
        (lambda: stats.rate_ratio_exact(1, 0, 3, 10), "population must be positive"),
        (lambda: stats.rate_ratio_exact(1, 10, 3, 0), "population must be positive"),
        (lambda: stats.rate_ratio_exact(1, 10, 0, 10), "no upper limit"),
        (lambda: stats.rate_difference(-1, 10, 3, 10), "can't be negative"),
        (lambda: stats.rate_difference(1, 0, 3, 10), "population must be positive"),
        (lambda: stats.rate_difference(1, 10, 3, 0), "population must be positive"),
        (lambda: stats.binomial_exact_ci(1, 0), "0 <= x <= n and n > 0"),
        (lambda: stats.historical_limits(1, [1]), "at least two historical totals"),
        (lambda: stats.historical_limits(-1, [1, 2]), "can't be negative"),
        (lambda: stats.historical_limits(1, [0, 0]), "ratio is undefined"),
    ],
)
def test_refusals_say_why(call, words: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(stats.StatsError, match=words):
        call()


def test_small_but_valid_inputs_are_accepted() -> None:
    """Boundaries: populations below 1 person-unit, two historical totals, a count exactly at n."""
    assert stats.rate_ratio(1, 0.5, 1, 0.5).value == pytest.approx(1.0)
    assert stats.rate_ratio_exact(1, 0.5, 1, 0.5).value == pytest.approx(1.0)
    assert stats.rate_ratio_exact(1, 10, 2, 10, level=0.5).upper < stats.rate_ratio_exact(1, 10, 2, 10).upper
    assert stats.rate_difference(1, 0.5, 1, 0.5).value == pytest.approx(0.0)
    assert stats.historical_limits(5, [1, 3]).flag == "above"
    assert stats.binomial_exact_ci(3, 3)[1] == 1.0 and stats.binomial_exact_ci(3, 3)[0] < 1.0
    assert stats.binomial_exact_ci(2, 3)[1] < 1.0
    # A ratio exactly on the upper limit (mean 10, SD 2: limit 1.4) is not beyond it.
    on = stats.historical_limits(14, [8, 10, 12])
    assert on.ratio == on.upper_limit and on.flag == "within"
    assert stats.historical_limits(15, [8, 10, 12]).flag == "above"
