"""Publication outlook: how often presented abstracts became full papers, per conference, base rate only.

The path from a fellow's idea to a paper has three stages, shown one row each with the evidence behind it:

1. idea → finished study: unknown (no published rate);
2. finished study → accepted abstract: unknown unless the meeting publishes its acceptance rate;
3. presented abstract → full paper: the conference's verified base rate, or unknown.

A stage with no data says "unknown" and is never multiplied into anything, so no overall chance is computed. The
frame is per conference (publication_base_rates.yaml is keyed by it): a conference without its own verified base
rate gets "unknown", never another meeting's rate or a pooled one. With no conference given, the tool shows the
per-conference table, not a pooled number. The feature arguments are checked and recorded but not used until a
model passes its calibration gate (docs/publication-outlook.md).

Correctness rule: never show a number that looks more precise than its evidence. A rate comes only as a coarse
natural frequency inside its 95% interval, with its n and source; there is no bare point estimate, and this is
never called a probability that an idea will be published.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from functools import cache
from importlib import resources
from typing import Any, Literal

import yaml

from health_data_registry.tools import Result, message

Fmt = Literal["csv", "json"]
Out = str | dict[str, Any]

BASIS = "base rate only"
MODEL_VERSION = "none"
CALIBRATION = "not yet assessed"
UNKNOWN = "unknown"
NO_BASE_RATE = "unknown: no verified base rate yet"
FEATURES_NOT_USED = "features recorded but not used until the model passes its calibration gate"
NOT_A_PROBABILITY = (
    "This is a publication outlook (base rate only), never a probability that your idea will be published."
)
NO_PRODUCT = (
    "No overall chance is computed: each stage is shown on its own, and a stage without data is never "
    "multiplied in."
)
NOT_YOU = (
    "It is the average for all abstracts at that meeting, not a prediction for your idea: your mentor, your "
    "team, protected time and finishing the study matter most, and none are in the data."
)

# Study designs the outlook records. Anything else is refused in plain words rather than silently kept.
DESIGNS = (
    "case report",
    "case series",
    "cross-sectional",
    "ecological",
    "case-control",
    "retrospective cohort",
    "prospective cohort",
    "randomized trial",
    "quality improvement",
    "systematic review",
    "modelling",
    "other",
)

COLUMNS = [
    "conference",
    "stage",
    "step",
    "status",
    "outlook",
    "event",
    "n",
    "interval_95",
    "interval_method",
    "source",
    "basis",
    "model_version",
    "calibration",
]
STEPS = {
    1: "idea → finished study",
    2: "finished study → accepted abstract",
    3: "presented abstract → full paper",
}


# ------------------------------------------------------------------------------------------ base rates


@dataclass(frozen=True)
class BaseRate:
    id: str
    role: str
    status: str
    conference: str
    short_citation: str
    pmid: str
    doi: str
    event_definition: str
    meeting: str
    years: str
    sampling: str
    follow_up: str
    n: int
    k: int | None
    proportion: float
    low: float
    high: float
    interval_method: str
    subgroups: tuple[tuple[str, int, int], ...] = ()


@dataclass(frozen=True)
class Conference:
    name: str
    full_name: str
    aliases: tuple[str, ...]
    acceptance_rate: str
    abstract_to_paper: str  # a base-rate id, or "unknown"


def _flat(text: Any) -> str:
    return " ".join(str(text).split())


class BaseRateError(ValueError):
    """The shipped base-rate list contradicts itself; the message says where in plain words."""


_PERCENT = re.compile(r"^\s*(\d+(?:\.\d+)?)%")


def _check_reported(where: str, k: int, n: int, reported: Any) -> None:
    """A stated percentage agrees with k / n to within its own rounding (one decimal: 26.6% for 236/887)."""
    m = _PERCENT.match(str(reported or ""))
    if m is None:
        return
    stated = float(m.group(1))
    decimals = len(m.group(1).partition(".")[2])
    if abs(100 * k / n - stated) > 0.5 * 10**-decimals + 1e-9:
        raise BaseRateError(
            f"{where}: {k} of {n} is {100 * k / n:.2f}%, but the list says {stated}%; the base-rate list must be "
            "corrected before it is used"
        )


def validate_base_rate(raw: dict[str, Any]) -> None:
    """Refuse a base rate whose numbers disagree: proportions and bounds in [0, 1], low <= estimate <= high,
    k within n, and every stated proportion or percentage equal to k / n within rounding."""
    rid = str(raw.get("id", "?"))
    n, k = int(raw["n"]), raw["k"]
    p = float(raw["estimate"]["proportion"])
    low, high = float(raw["interval"]["low"]), float(raw["interval"]["high"])
    if not all(0.0 <= x <= 1.0 for x in (p, low, high)) or not low <= p <= high:
        raise BaseRateError(
            f"{rid}: the proportion and its interval must lie between 0 and 1 with low <= estimate <= high "
            f"(got {p}, {low}-{high})"
        )
    if n <= 0:
        raise BaseRateError(f"{rid}: n must be a positive number of abstracts (got {n})")
    if k != UNKNOWN:
        k = int(k)
        if not 0 <= k <= n:
            raise BaseRateError(f"{rid}: k = {k} must be between 0 and n = {n}")
        if abs(k / n - p) > 0.00005 + 1e-12:  # the proportion is kept to 4 decimals
            raise BaseRateError(f"{rid}: {k} / {n} = {k / n:.4f}, but the list gives the proportion as {p}")
        _check_reported(rid, k, n, raw["estimate"].get("reported"))
    for s in raw.get("subgroups", []):
        sn, sk = int(s["n"]), int(s["k"])
        if not 0 <= sk <= sn:
            raise BaseRateError(f"{rid} ({s['label']}): k = {sk} must be between 0 and n = {sn}")
        _check_reported(f"{rid} ({s['label']})", sk, sn, s.get("reported"))


def _base_rate(raw: dict[str, Any]) -> BaseRate:
    validate_base_rate(raw)
    k = raw["k"]
    return BaseRate(
        id=raw["id"],
        role=raw["role"],
        status=raw["status"],
        conference=str(raw["conference"]),
        short_citation=raw["short_citation"],
        pmid=str(raw["pmid"]),
        doi=str(raw["doi"]),
        event_definition=_flat(raw["event_definition"]),
        meeting=raw["frame"]["meeting"],
        years=str(raw["frame"]["years"]),
        sampling=_flat(raw["frame"]["sampling"]),
        follow_up=raw["frame"]["follow_up"],
        n=int(raw["n"]),
        k=None if k == UNKNOWN else int(k),
        proportion=float(raw["estimate"]["proportion"]),
        low=float(raw["interval"]["low"]),
        high=float(raw["interval"]["high"]),
        interval_method=raw["interval"]["method"],
        subgroups=tuple((s["label"], int(s["n"]), int(s["k"])) for s in raw.get("subgroups", [])),
    )


@dataclass(frozen=True)
class BaseRates:
    rates: dict[str, BaseRate]
    conferences: tuple[Conference, ...]


@cache
def load_base_rates() -> BaseRates:
    """The reviewed base-rate list shipped with the package, keyed by conference."""
    text = resources.files("health_data_registry").joinpath("publication_base_rates.yaml").read_text("utf-8")
    raw = yaml.safe_load(text)
    rates = {r["id"]: _base_rate(r) for r in raw["base_rates"]}
    conferences = tuple(
        Conference(
            name=c["name"],
            full_name=c["full_name"],
            aliases=tuple(str(a).lower() for a in c.get("aliases", [])),
            acceptance_rate=str(c["acceptance_rate"]),
            abstract_to_paper=str(c["abstract_to_paper"]),
        )
        for c in raw["conferences"]
    )
    for c in conferences:  # a conference may only point at a verified rate measured at that same conference
        if c.abstract_to_paper != UNKNOWN and rates[c.abstract_to_paper].conference != c.name:
            raise ValueError(f"{c.name} borrows {c.abstract_to_paper}, measured at another meeting")
    return BaseRates(rates, conferences)


def find_conference(name: str) -> Conference | None:
    key = " ".join(name.strip().lower().split())
    for c in load_base_rates().conferences:
        if key in (c.name.lower(), c.full_name.lower(), *c.aliases):
            return c
    return None


# ------------------------------------------------------------------------------------------ arithmetic


def wilson_interval(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson score interval for k events in n (95% by default)."""
    if n <= 0 or not 0 <= k <= n:
        raise ValueError("need 0 <= k <= n and n > 0")
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _pct(x: float) -> int:
    """Whole percent, half up: an interval is never shown to a decimal place."""
    return math.floor(x * 100 + 0.5)


def interval_text(low: float, high: float) -> str:
    return f"{_pct(low)}–{_pct(high)}%"


def natural_frequency(p: float, low: float, high: float) -> str:
    """The simplest 'about a in b' (b up to 10) that lies inside the interval; coarse on purpose.

    Rounding 1/p alone can land outside the interval (37% would read "1 in 3"), so the wording is checked
    against the interval it sits next to. If no simple fraction fits, only the interval is given.
    """
    best: Fraction | None = None
    for b in range(2, 11):
        for a in range(1, b):
            f = Fraction(a, b)
            if f.denominator == b and low <= f <= high and (best is None or abs(f - p) < abs(best - p)):
                best = f
        if best is not None:
            return f"about {best.numerator} in {best.denominator}"
    return "a share"


# ------------------------------------------------------------------------------------------ the outlook


@dataclass(frozen=True)
class Stage:
    conference: str
    stage: int
    status: str  # "unknown", NO_BASE_RATE, or BASIS
    outlook: str
    event: str = ""
    n: int | None = None
    interval: str = ""
    interval_method: str = ""
    source: str = ""

    @property
    def basis(self) -> str:
        return BASIS if self.status == BASIS else "none: no data for this stage"

    def row(self) -> list[Any]:
        return [
            self.conference,
            self.stage,
            STEPS[self.stage],
            self.status,
            self.outlook,
            self.event,
            self.n,
            self.interval,
            self.interval_method,
            self.source,
            self.basis,
            MODEL_VERSION,
            CALIBRATION,
        ]


def _paper_stage(c: Conference) -> Stage:
    if c.abstract_to_paper == UNKNOWN:
        return Stage(
            c.name,
            3,
            NO_BASE_RATE,
            f"Unknown: no verified base rate yet for {c.name}. Other meetings' rates and pooled reviews are not "
            "borrowed for it.",
        )
    r = load_base_rates().rates[c.abstract_to_paper]
    if r.k is None:  # pragma: no cover - a conference's own base rate always has a count
        raise ValueError(f"{r.id} has no count")
    shown = interval_text(r.low, r.high)
    text = (
        f"Of {r.n} sampled {r.meeting} {r.years.replace('-', '–')} abstracts, {r.k} were full papers within the "
        f"source's follow-up ({r.follow_up}): {natural_frequency(r.k / r.n, r.low, r.high)} (95% interval "
        f"{shown}). Source: {r.short_citation}; n = {r.n}; calibration: {BASIS}. This is one meeting and one "
        f"field (infectious diseases). {NOT_YOU} The chance of finishing the study and of the abstract being "
        "accepted is unknown."
    )
    return Stage(
        c.name,
        3,
        BASIS,
        text,
        event=r.event_definition,
        n=r.n,
        interval=shown,
        interval_method=r.interval_method,
        source=f"{r.short_citation}, PMID {r.pmid}, doi {r.doi}",
    )


def stages(c: Conference) -> list[Stage]:
    """One row per stage for one conference. There is deliberately no function that multiplies them."""
    accept = (
        f"Unknown. No acceptance rate for {c.name} has been verified from the meeting itself, so this stage is not "
        "estimated and not multiplied in."
    )
    return [
        Stage(
            c.name,
            1,
            UNKNOWN,
            "Unknown. There is no published rate for how often a fellow's idea becomes a finished study, so this "
            "stage is not estimated and not multiplied in.",
        ),
        Stage(c.name, 2, UNKNOWN, accept),
        _paper_stage(c),
    ]


def _rate_notes(r: BaseRate) -> list[str]:
    years = "; ".join(f"{label}: {k} of {n}" for label, n, k in r.subgroups if label.startswith(r.meeting))
    notes = [f"How the {r.n} {r.meeting} abstracts were chosen: {r.sampling}"]
    if years:
        notes.append(
            f"Follow-up differed by year ({years}); the 2018 follow-up overlapped the COVID-19 pandemic, so the "
            "rate has no single time horizon."
        )
    notes.append(
        f"The {r.meeting} base rate is a {r.status}: checked against the primary source, not yet reviewed."
    )
    return notes


def _context_note() -> str:
    s = load_base_rates().rates["scherer_2018_cochrane"]
    return (
        f"For context only, never used as any conference's rate: across many biomedical meetings, "
        f"{natural_frequency(s.proportion, s.low, s.high)} abstracts reached full publication (95% CI "
        f"{interval_text(s.low, s.high)}; {s.short_citation}, PMID {s.pmid}; {s.n:,} abstracts, varying "
        "follow-up)."
    )


def _features_note(given: dict[str, str | None]) -> str:
    recorded = "; ".join(f"{k}: {v}" for k, v in given.items() if v)
    if not recorded:
        return f"No features were given; {FEATURES_NOT_USED}."
    return f"Your inputs ({recorded}) are {FEATURES_NOT_USED}, so the outlook is the same for every idea."


def _refuse(text: str, fmt: Fmt) -> Out:
    return message(text, fmt, refused="unknown_input")


def conference_names() -> list[str]:
    return [c.name for c in load_base_rates().conferences]


def get_publication_outlook(
    conference: str | None = None,
    design: str | None = None,
    data_sources: Sequence[str] | None = None,
    condition: str | None = None,
    geography: str | None = None,
    fmt: Fmt = "csv",
) -> Out:
    """The publication outlook, base rate only. With a conference: one row per stage from idea to paper. Without:
    one row per conference for the abstract-to-paper stage, never a pooled number."""
    conf: Conference | None = None
    if conference is not None and conference.strip():
        conf = find_conference(conference)
        if conf is None:
            return _refuse(
                f'"{conference}" isn\'t a conference the outlook covers. Use one of: '
                f"{', '.join(conference_names())}; or leave the conference out to see them all side by side.",
                fmt,
            )
    if design is not None:
        d = " ".join(design.strip().lower().replace("_", " ").split())
        if d not in DESIGNS:
            return _refuse(
                f'"{design}" isn\'t a study design the outlook records. Use one of: {", ".join(DESIGNS)}; or '
                "leave the design out. It doesn't change the outlook yet: only the base rate is used.",
                fmt,
            )
        design = d
    if isinstance(data_sources, str):
        data_sources = [data_sources]
    if data_sources is not None and any(not str(s).strip() for s in data_sources):
        return _refuse(
            "One of the data sources is blank. Name each source (for example cdc_nndss_weekly), or leave them "
            "out. They don't change the outlook yet: only the base rate is used.",
            fmt,
        )
    features = _features_note(
        {
            "design": design,
            "data sources": ", ".join(data_sources) if data_sources else None,
            "condition": condition.strip() if condition else None,
            "geography": geography.strip() if geography else None,
        }
    )
    notes = [NOT_A_PROBABILITY, NO_PRODUCT, features]
    if conf is None:
        rows = [_paper_stage(c) for c in load_base_rates().conferences]
        notes.insert(
            2,
            "No conference was named, so this shows the presented-abstract-to-paper stage for each conference "
            "side by side; there is no pooled number. Stages 1 (idea to finished study) and 2 (accepted "
            "abstract) are unknown for every conference. Name a conference to see its stages.",
        )
    else:
        rows = stages(conf)
    for c in {s.conference for s in rows if s.status == BASIS}:
        found = find_conference(c)
        if found is not None:
            notes += _rate_notes(load_base_rates().rates[found.abstract_to_paper])
    if conf is not None and conf.abstract_to_paper != UNKNOWN:
        # Only beside a conference's own rate, so a pooled figure never sits next to an "unknown" to fill it.
        notes.append(_context_note())
    return Result(COLUMNS, [s.row() for s in rows], notes).render(fmt)
