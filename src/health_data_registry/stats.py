"""Pure statistics for the analysis tools: standard library only, no I/O, no data access.

Every function takes plain numbers and returns plain numbers, so each can be checked against a published
worked example. The methods and their sources:

- Exact Poisson interval for a count (Garwood 1936), from chi-square quantiles:
  lower = chi2(alpha/2; 2k) / 2, upper = chi2(1 - alpha/2; 2k + 2) / 2.
- Rate ratio with the log method on Poisson counts, and rate difference with the normal approximation on
  Poisson variances (Rothman KJ, Epidemiology: An Introduction, 2nd ed., 2012; Rothman, Greenland and Lash,
  Modern Epidemiology, 3rd ed., 2008, ch. 14).
- Annual percent change from a log-linear Poisson regression on year, fitted by Newton-Raphson, with the
  standard error scaled up for overdispersion (quasi-Poisson, Pearson chi-square over degrees of freedom).
- Direct age standardisation with the gamma interval of Fay MP, Feuer EJ. Confidence intervals for directly
  standardized rates: a method based on the gamma distribution. Stat Med 1997;16:791-801, as NCHS uses.
- Historical limits for notifiable diseases: Stroup DF, Williamson GD, Herndon JL, Karon JM. Detection of
  aberrations in the occurrence of notifiable diseases surveillance data. Stat Med 1989;8:323-329, the
  method behind the MMWR notifiable disease tables' Figure I.

The incomplete gamma function follows the series and continued-fraction forms in Press et al., Numerical
Recipes, 3rd ed., section 6.2; quantiles are found by safeguarded Newton steps inside a bisection bracket.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

_EPS = 1e-15
_MAX_ITER = 1000
_TINY = 1e-300


class StatsError(ValueError):
    """The numbers can't support this method (e.g. a zero count where a logarithm is needed)."""


@dataclass(frozen=True)
class Estimate:
    value: float
    lower: float
    upper: float


# ---------------------------------------------------------------------------------- distributions


def _gamma_series(a: float, x: float) -> float:
    """Regularised lower incomplete gamma P(a, x) by its power series (good for x < a + 1)."""
    term = 1.0 / a
    total = term
    ap = a
    for _ in range(_MAX_ITER):
        ap += 1.0
        term *= x / ap
        total += term
        if abs(term) < abs(total) * _EPS:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gamma_cf(a: float, x: float) -> float:
    """Regularised upper incomplete gamma Q(a, x) by its continued fraction (modified Lentz; x >= a + 1)."""
    b = x + 1.0 - a
    c = 1.0 / _TINY
    d = 1.0 / b
    h = d
    for i in range(1, _MAX_ITER):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < _TINY:
            d = _TINY
        c = b + an / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def gamma_cdf(x: float, shape: float) -> float:
    """P(X <= x) for X ~ Gamma(shape, scale 1)."""
    if shape <= 0:
        raise StatsError("gamma shape must be positive")
    if x <= 0:
        return 0.0
    if x < shape + 1.0:
        return _gamma_series(shape, x)
    return 1.0 - _gamma_cf(shape, x)


def gamma_quantile(p: float, shape: float) -> float:
    """The x with P(X <= x) = p for X ~ Gamma(shape, scale 1)."""
    if not 0.0 < p < 1.0:
        raise StatsError("probability must be strictly between 0 and 1")
    if shape <= 0:
        raise StatsError("gamma shape must be positive")
    lo, hi = 0.0, max(1.0, shape)
    while gamma_cdf(hi, shape) < p:
        lo, hi = hi, hi * 2.0
    x = 0.5 * (lo + hi)
    for _ in range(200):
        f = gamma_cdf(x, shape) - p
        if f > 0:
            hi = x
        else:
            lo = x
        dens = math.exp((shape - 1.0) * math.log(x) - x - math.lgamma(shape)) if x > 0 else 0.0
        step = x - f / dens if dens > 0 else lo - 1.0
        x = step if lo < step < hi else 0.5 * (lo + hi)
        if hi - lo <= 1e-14 * max(1.0, x) or abs(f) < 1e-16:
            break
    return x


def chi2_quantile(p: float, df: float) -> float:
    """Chi-square quantile: a chi-square with df degrees of freedom is 2 x Gamma(df / 2)."""
    return 2.0 * gamma_quantile(p, df / 2.0)


def normal_quantile(p: float) -> float:
    """Standard normal quantile by bisection on the error function (exact to about 1e-12)."""
    if not 0.0 < p < 1.0:
        raise StatsError("probability must be strictly between 0 and 1")
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if 0.5 * math.erfc(-mid / math.sqrt(2.0)) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _alpha(level: float) -> float:
    if not 0.0 < level < 1.0:
        raise StatsError("confidence level must be between 0 and 1")
    return 1.0 - level


# ---------------------------------------------------------------------------------- counts and rates


def poisson_exact_ci(count: int, level: float = 0.95) -> tuple[float, float]:
    """Exact (Garwood) interval for the mean of a Poisson count."""
    if count < 0 or int(count) != count:
        raise StatsError("a count must be a whole number, zero or more")
    a = _alpha(level)
    lower = 0.0 if count == 0 else chi2_quantile(a / 2.0, 2.0 * count) / 2.0
    upper = chi2_quantile(1.0 - a / 2.0, 2.0 * count + 2.0) / 2.0
    return lower, upper


def crude_rate(count: int, population: float, per: float = 100_000.0, level: float = 0.95) -> Estimate:
    """Count / population x per, with the exact Poisson interval (population treated as known)."""
    if population <= 0:
        raise StatsError("population must be positive")
    lo, hi = poisson_exact_ci(count, level)
    k = per / population
    return Estimate(count * k, lo * k, hi * k)


def rate_ratio(a: int, pop_a: float, b: int, pop_b: float, level: float = 0.95) -> Estimate:
    """(a / pop_a) / (b / pop_b) with the log-method interval: SE(log RR) = sqrt(1/a + 1/b)."""
    if a <= 0 or b <= 0:
        raise StatsError("a rate ratio needs at least one case in each group")
    if pop_a <= 0 or pop_b <= 0:
        raise StatsError("population must be positive")
    rr = (a / pop_a) / (b / pop_b)
    half = normal_quantile(1.0 - _alpha(level) / 2.0) * math.sqrt(1.0 / a + 1.0 / b)
    return Estimate(rr, rr * math.exp(-half), rr * math.exp(half))


def rate_difference(
    a: int, pop_a: float, b: int, pop_b: float, per: float = 100_000.0, level: float = 0.95
) -> Estimate:
    """a / pop_a - b / pop_b (per `per`), with SE = sqrt(a / pop_a^2 + b / pop_b^2) (Poisson variances)."""
    if a < 0 or b < 0:
        raise StatsError("counts can't be negative")
    if pop_a <= 0 or pop_b <= 0:
        raise StatsError("population must be positive")
    rd = (a / pop_a - b / pop_b) * per
    se = math.sqrt(a / pop_a**2 + b / pop_b**2) * per
    half = normal_quantile(1.0 - _alpha(level) / 2.0) * se
    return Estimate(rd, rd - half, rd + half)


# ---------------------------------------------------------------------------------- trend


@dataclass(frozen=True)
class TrendFit:
    apc: float  # annual percent change
    lower: float
    upper: float
    slope: float  # log rate change per year
    se: float  # standard error of the slope, after any overdispersion scaling
    dispersion: float  # Pearson chi-square / (n - 2); 1 means no extra-Poisson variation
    n: int


def poisson_trend(
    years: Sequence[float],
    counts: Sequence[float],
    populations: Sequence[float] | None = None,
    level: float = 0.95,
) -> TrendFit:
    """Fit log E[count] = log(population) + a + b * year by maximum likelihood; APC = 100 (e^b - 1).

    Without populations the offset is zero (a trend in counts). The interval is Wald on b, with the standard
    error multiplied by sqrt(dispersion) when the Pearson dispersion exceeds 1 (quasi-Poisson).
    """
    n = len(years)
    if n != len(counts) or (populations is not None and len(populations) != n):
        raise StatsError("years, counts and populations must line up")
    if n < 3:
        raise StatsError("a trend needs at least three points")
    if len(set(years)) < 2:
        raise StatsError("a trend needs at least two different years")
    if any(c < 0 for c in counts):
        raise StatsError("counts can't be negative")
    pops = list(populations) if populations is not None else [1.0] * n
    if any(p <= 0 for p in pops):
        raise StatsError("population must be positive")
    total = float(sum(counts))
    if total <= 0:
        raise StatsError("every count is zero, so there is no trend to estimate")
    t_mean = sum(years) / n
    t = [y - t_mean for y in years]
    logp = [math.log(p) for p in pops]
    a = math.log(total / sum(pops))
    b = 0.0
    converged = False
    for _ in range(100):
        mu = [math.exp(lp + a + b * ti) for lp, ti in zip(logp, t, strict=True)]
        u0 = sum(c - m for c, m in zip(counts, mu, strict=True))
        u1 = sum((c - m) * ti for c, m, ti in zip(counts, mu, t, strict=True))
        i00 = sum(mu)
        i01 = sum(m * ti for m, ti in zip(mu, t, strict=True))
        i11 = sum(m * ti * ti for m, ti in zip(mu, t, strict=True))
        det = i00 * i11 - i01 * i01
        if det <= 0:
            raise StatsError("the counts can't support a trend fit")
        da = (i11 * u0 - i01 * u1) / det
        db = (i00 * u1 - i01 * u0) / det
        a += da
        b += db
        if abs(b) > 20:
            raise StatsError("the counts can't support a trend fit (all cases fall at one end)")
        if abs(da) < 1e-12 and abs(db) < 1e-12:
            converged = True
            break
    if not converged:
        raise StatsError("the trend fit did not settle on an answer")
    mu = [math.exp(lp + a + b * ti) for lp, ti in zip(logp, t, strict=True)]
    i00 = sum(mu)
    i01 = sum(m * ti for m, ti in zip(mu, t, strict=True))
    i11 = sum(m * ti * ti for m, ti in zip(mu, t, strict=True))
    var_b = i00 / (i00 * i11 - i01 * i01)
    dispersion = (
        sum((c - m) ** 2 / m for c, m in zip(counts, mu, strict=True)) / (n - 2) if n > 2 else 1.0
    )
    se = math.sqrt(var_b * max(1.0, dispersion))
    half = normal_quantile(1.0 - _alpha(level) / 2.0) * se
    return TrendFit(
        apc=100.0 * math.expm1(b),
        lower=100.0 * math.expm1(b - half),
        upper=100.0 * math.expm1(b + half),
        slope=b,
        se=se,
        dispersion=dispersion,
        n=n,
    )


# ---------------------------------------------------------------------------------- age adjustment


def direct_standardize(
    counts: Sequence[int],
    populations: Sequence[float],
    standard: Sequence[float],
    per: float = 100_000.0,
    level: float = 0.95,
) -> Estimate:
    """Directly age-standardised rate with the Fay-Feuer gamma interval.

    w_i = standard_i / (sum(standard) x population_i); rate y = sum w_i d_i; v = sum w_i^2 d_i;
    lower = Gamma quantile(alpha/2; shape y^2/v, scale v/y); upper uses y + w_max and v + w_max^2
    (Fay and Feuer 1997, equations 4-5). With one age group this is exactly the Garwood interval.
    """
    if not (len(counts) == len(populations) == len(standard)) or not counts:
        raise StatsError("counts, populations and standard weights must line up, one per age group")
    if any(c < 0 or int(c) != c for c in counts):
        raise StatsError("counts must be whole numbers, zero or more")
    if any(p <= 0 for p in populations):
        raise StatsError("every age group needs a positive population")
    if any(s < 0 for s in standard) or sum(standard) <= 0:
        raise StatsError("standard weights must be non-negative and not all zero")
    a = _alpha(level)
    total_std = float(sum(standard))
    w = [s / (total_std * p) for s, p in zip(standard, populations, strict=True)]
    y = sum(wi * d for wi, d in zip(w, counts, strict=True))
    v = sum(wi * wi * d for wi, d in zip(w, counts, strict=True))
    wm = max(w)
    lower = 0.0 if y == 0 else gamma_quantile(a / 2.0, y * y / v) * (v / y)
    upper = gamma_quantile(1.0 - a / 2.0, (y + wm) ** 2 / (v + wm * wm)) * ((v + wm * wm) / (y + wm))
    return Estimate(y * per, lower * per, upper * per)


# ---------------------------------------------------------------------------------- aberration detection


@dataclass(frozen=True)
class Signal:
    current: float
    mean: float
    sd: float
    ratio: float  # current / mean of the historical totals
    lower_limit: float  # in ratio units: 1 - 2 SD / mean (never below 0)
    upper_limit: float  # 1 + 2 SD / mean
    flag: str  # "above", "below" or "within"


def historical_limits(current: float, history: Sequence[float]) -> Signal:
    """Stroup et al. 1989: compare the current 4-week total with 15 historical 4-week totals.

    The ratio current / mean(history) is flagged when it lies beyond 1 +/- 2 SD / mean, the historical limits
    in ratio units, which is the same as the current total lying beyond mean +/- 2 SD.
    """
    if len(history) < 2:
        raise StatsError("historical limits need at least two historical totals")
    if current < 0 or any(h < 0 for h in history):
        raise StatsError("counts can't be negative")
    n = len(history)
    mean = sum(history) / n
    if mean <= 0:
        raise StatsError("no cases in any comparison period, so the ratio is undefined")
    sd = math.sqrt(sum((h - mean) ** 2 for h in history) / (n - 1))
    ratio = current / mean
    upper = 1.0 + 2.0 * sd / mean
    lower = max(0.0, 1.0 - 2.0 * sd / mean)
    flag = "above" if ratio > upper else "below" if ratio < lower else "within"
    return Signal(current, mean, sd, ratio, lower, upper, flag)


# ---------------------------------------------------------------------------------- standard populations

# 2000 US standard population (Census P25-1130), as age-adjustment weights per million.
# 11 groups: Klein RJ, Schoenborn CA. Age adjustment using the 2000 projected U.S. population. Healthy People
# 2010 Statistical Notes no. 20. NCHS, 2001, table 2, distribution #1 (https://www.cdc.gov/nchs/data/statnt/statnt20.pdf).
# 19 groups: NCI SEER, Standard Populations - 19 Age Groups (https://seer.cancer.gov/stdpopulations/stdpop.19ages.html).
# Ages are (lowest, highest) in completed years; None means "and over". Both checked live 2026-11-24.
US2000_STD_11: tuple[tuple[int, int | None, int], ...] = (
    (0, 0, 13_818), (1, 4, 55_317), (5, 14, 145_565), (15, 24, 138_646), (25, 34, 135_573),
    (35, 44, 162_613), (45, 54, 134_834), (55, 64, 87_247), (65, 74, 66_037), (75, 84, 44_842),
    (85, None, 15_508),
)  # fmt: skip
US2000_STD_19: tuple[tuple[int, int | None, int], ...] = (
    (0, 0, 13_818), (1, 4, 55_317), (5, 9, 72_533), (10, 14, 73_032), (15, 19, 72_169), (20, 24, 66_478),
    (25, 29, 64_529), (30, 34, 71_044), (35, 39, 80_762), (40, 44, 81_851), (45, 49, 72_118),
    (50, 54, 62_716), (55, 59, 48_454), (60, 64, 38_793), (65, 69, 34_264), (70, 74, 31_773),
    (75, 79, 26_999), (80, 84, 17_842), (85, None, 15_508),
)  # fmt: skip
