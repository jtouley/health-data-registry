"""choose_output_format: which output fits a project now, as a repeatable, probability-based Choice.

The answer is a decision under stated priorities, never a prediction that the work will be published, and never a
ranking of ideas, fellows or projects. Its parts, all from the reviewed rubric (format_rubric.yaml):

- Inputs are structured answers only (fixed values and numbers), never read from free text (hard rule 9).
- Each criterion gives every format a level (at most 5) by a deterministic rule with a named basis. A criterion
  whose evidence is unknown is left out for every format and named; nothing is imputed.
- Gate: a format at level 1 on contribution fit can't rank first; it is still scored and shown, flagged, with
  the reason (ADR 0018: show everything; qualify, don't decide).
- Weights: unit weights (Dawes 1979), or rank-order centroid weights from the priorities given (Barron and
  Barrett 1996). Each draw takes its weights from a Dirichlet centred on the declared ones, and each sourced
  rate from its Beta(k + 1, n - k + 1) posterior, with a fixed seed (stats.py has the samplers and sources).
- probabilities: the share of draws in which each format ranks first (a tie splits the draw evenly). They sum to
  1 across all formats, and a left-out format gets 0.
- confidence: (p_max - 1/n) / (1 - 1/n) over the n formats left after the gate (TypeSafe's Choice definition):
  how settled the answer is, not whether it is right. review: act, discuss with your mentor, or too close to call,
  from the rubric's thresholds.
- flip_condition: on the declared weights (no simulation), the change to one weight between 0.25x and 4x, nearest
  1x, that would put another format first.
- Every answer carries the rubric's version, status and sha256, the draws and the seed, so it can be reproduced.
"""

from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache, lru_cache
from importlib import resources
from typing import Any, Literal

import yaml

from health_data_registry import publication, stats
from health_data_registry.tools import message

Fmt = Literal["csv", "json"]
Out = str | dict[str, Any]

RUBRIC_FILE = "format_rubric.yaml"
BASIS_KINDS = ("reporting_guideline", "venue_instructions", "published_study", "published_method",
               "maintainer_declaration")  # fmt: skip
RULE_KINDS = ("table", "bands", "fixed", "unknown")
PRIORITIES = ("speed", "citable_record", "reach", "effort")
NOT_A_PREDICTION = (
    "A decision under these stated priorities, not a prediction that your work will be published."
)
CONFIDENCE_MEANS = "Confidence says how settled this choice is under the rubric, not whether it is right."
NOT_A_RANKING = (
    "This compares formats for one project only. It never ranks ideas, fellows or projects, and the publication "
    "outlook's base rate is used only to compare formats for this project."
)
REVIEW_ACT = "act"
REVIEW_DISCUSS = "discuss with your mentor"
REVIEW_CLOSE = "too close to call: decide with your mentor"
# How each review value reads in the notes; the JSON keeps the short value.
REVIEW_SAYS = {REVIEW_ACT: "act on it", REVIEW_DISCUSS: REVIEW_DISCUSS, REVIEW_CLOSE: REVIEW_CLOSE}
NEXT_STEP = (
    'Answer each question with one of the listed values; the "Which format should I aim for?" helper asks them '
    "one at a time."
)
DRAFT_NOTE = (
    "Draft: the maintainer hasn't reviewed this rubric yet. Every level, band, weight spread and threshold marked "
    "pending review may change, and so may this answer."
)


class RubricError(ValueError):
    """The rubric contradicts itself or names something without a source; the message says where."""

    code = "rubric_error"
    next_step = (
        "The rubric this tool reads needs fixing by the maintainer; the data tools still work. Run the Something's "
        "wrong helper and mention this question."
    )


# ------------------------------------------------------------------------------------------ the rubric


@dataclass(frozen=True)
class Basis:
    kind: str
    cites: tuple[str, ...]
    text: str
    status: str  # "proposed" for a declaration not yet reviewed, else ""

    def describe(self, sources: dict[str, dict[str, Any]]) -> str:
        label = {
            "maintainer_declaration": "maintainer's declaration"
            + (", pending review" if self.status == "proposed" else ""),
            "venue_instructions": "venues' author instructions",
            "reporting_guideline": "reporting guideline",
            "published_study": "published study",
            "published_method": "published method",
        }[self.kind]
        cited = "; ".join(_short(sources[c]) for c in self.cites)
        return f"{label}: {self.text} Sources: {cited}" if cited else f"{label}: {self.text}"


@dataclass(frozen=True)
class Venue:
    name: str
    text: str


@dataclass(frozen=True)
class Format:
    id: str
    label: str
    guideline: str
    venues: tuple[Venue, ...]


@dataclass(frozen=True)
class Criterion:
    id: str
    label: str
    kind: str  # table, bands, fixed, unknown
    basis: Basis
    legend: dict[int, str]
    input: str = ""
    table: dict[str, dict[str, int]] = field(default_factory=dict)
    bands: tuple[tuple[int, int | None, dict[str, int]], ...] = ()
    fixed: dict[str, int] = field(default_factory=dict)
    upside: dict[str, Any] | None = None
    unknown_reason: str = ""
    requires: str = ""
    gate: bool = False
    priority: str = ""


@dataclass(frozen=True)
class Rubric:
    version: str
    status: str
    sha256: str
    inputs: dict[str, dict[str, str]]
    formats: tuple[Format, ...]
    criteria: tuple[Criterion, ...]
    sources: dict[str, dict[str, Any]]
    default_weights: Basis
    priority_weights: Basis
    concentration: float
    dirichlet: Basis
    draws: int
    seed: int
    flip_low: float
    flip_high: float
    simulation: Basis
    act: float
    discuss: float
    review: Basis
    open_questions: tuple[str, ...]

    @property
    def format_ids(self) -> tuple[str, ...]:
        return tuple(f.id for f in self.formats)

    def criterion(self, cid: str) -> Criterion:
        return next(c for c in self.criteria if c.id == cid)


def _short(src: dict[str, Any]) -> str:
    citation = " ".join(str(src["citation"]).split())
    where = (
        src.get("pmid") and f"PMID {src['pmid']}" or src.get("doi") and f"doi {src['doi']}" or src.get("url")
    )
    return f"{citation} ({where})"


def _basis(where: str, raw: Any, sources: dict[str, dict[str, Any]]) -> Basis:
    if not isinstance(raw, dict):
        raise RubricError(f"{where} has no basis: every weight, rule and threshold must name its source")
    kind = raw.get("kind")
    if kind not in BASIS_KINDS:
        raise RubricError(f"{where}: basis kind {kind!r} is not one of {', '.join(BASIS_KINDS)}")
    cites = tuple(str(c) for c in raw.get("cites") or ())
    missing = [c for c in cites if c not in sources]
    if missing:
        raise RubricError(f"{where} cites {', '.join(missing)}, which the rubric's sources don't list")
    text = " ".join(str(raw.get("text") or "").split())
    if not text:
        raise RubricError(f"{where}: the basis must say in words why")
    if kind != "maintainer_declaration" and not cites:
        raise RubricError(f"{where}: a {kind.replace('_', ' ')} basis must cite its source")
    status = str(raw.get("status") or "")
    if kind == "maintainer_declaration" and status not in ("proposed", "reviewed"):
        raise RubricError(f"{where}: a maintainer's declaration must say whether it is proposed or reviewed")
    return Basis(kind, cites, text, status)


def _levels(where: str, raw: Any, formats: Sequence[str]) -> dict[str, int]:
    if not isinstance(raw, dict) or set(raw) != set(formats):
        raise RubricError(f"{where}: give a level for exactly these formats: {', '.join(formats)}")
    out = {}
    for f in formats:
        v = raw[f]
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 5:
            raise RubricError(f"{where}: the level for {f} must be a whole number from 1 to 5 (got {v!r})")
        out[f] = v
    return out


def _venue(where: str, raw: dict[str, Any], sources: dict[str, dict[str, Any]]) -> Venue:
    name = str(raw["name"])
    if raw.get("limits") == "unknown":
        reason = " ".join(str(raw.get("reason") or "").split())
        if not reason:
            raise RubricError(f"{where} ({name}): an unknown limit must say why")
        return Venue(name, f"{name}: limits unknown ({reason})")
    src = raw.get("source")
    if src not in sources:
        raise RubricError(f"{where} ({name}): its limits must cite a listed source (got {src!r})")
    for key in ("words", "abstract_words", "figures_tables", "references"):
        if key not in raw:
            raise RubricError(f"{where} ({name}): missing {key}")
    checked = sources[src].get("checked", "")
    text = (
        f"{name}: {raw['words']} words, abstract {raw['abstract_words']} words, figures and tables "
        f"{raw['figures_tables']}, references {raw['references']} ({src.replace('_', ' ')}, checked {checked})"
    )
    return Venue(name, text)


def _criterion(raw: dict[str, Any], rubric_inputs: dict[str, dict[str, str]], formats: Sequence[str],
               sources: dict[str, dict[str, Any]]) -> Criterion:  # fmt: skip
    cid = str(raw["id"])
    where = f"criterion {cid}"
    basis = _basis(where, raw.get("basis"), sources)
    rule = raw.get("rule") or {}
    kind = rule.get("kind")
    if kind not in RULE_KINDS:
        raise RubricError(f"{where}: rule kind {kind!r} is not one of {', '.join(RULE_KINDS)}")
    priority = str(raw.get("priority") or "")
    if priority and priority not in PRIORITIES:
        raise RubricError(f"{where}: priority {priority!r} is not on the menu ({', '.join(PRIORITIES)})")
    common: dict[str, Any] = {
        "id": cid, "label": str(raw["label"]), "kind": kind, "basis": basis, "gate": bool(raw.get("gate")),
        "priority": priority, "requires": str(raw.get("requires") or ""),
    }  # fmt: skip
    if kind == "unknown":
        reason = " ".join(str(rule.get("reason") or "").split())
        if not reason:
            raise RubricError(f"{where}: an unknown rule must say why it can't be scored")
        return Criterion(**common, legend={}, unknown_reason=reason)
    used: set[int] = set()
    table: dict[str, dict[str, int]] = {}
    bands: tuple[tuple[int, int | None, dict[str, int]], ...] = ()
    fixed: dict[str, int] = {}
    upside: dict[str, Any] | None = None
    inp = str(rule.get("input") or "")
    if kind == "table":
        if inp == "needs_methods_section":
            values: list[str] = ["true", "false"]
        elif inp in rubric_inputs:
            values = list(rubric_inputs[inp])
        else:
            raise RubricError(f"{where}: input {inp!r} is not one of the rubric's inputs")
        levels = {str(k): v for k, v in (rule.get("levels") or {}).items()}
        if set(levels) != set(values):
            raise RubricError(f"{where}: give levels for exactly these values of {inp}: {', '.join(values)}")
        table = {v: _levels(f"{where} ({v})", levels[v], formats) for v in values}
        used = {lv for row in table.values() for lv in row.values()}
    elif kind == "bands":
        if inp not in ("worked_examples", "weeks_to_deadline"):
            raise RubricError(f"{where}: bands read a whole-number input, not {inp!r}")
        out = []
        expect = 0
        raw_bands = rule.get("bands") or []
        for i, b in enumerate(raw_bands):
            lo, hi = b.get("min"), b.get("max")
            if lo != expect:
                raise RubricError(f"{where}: band {i + 1} must start at {expect} so the bands leave no gap")
            last = i == len(raw_bands) - 1
            if (hi is None) != last or (hi is not None and hi < lo):
                raise RubricError(
                    f"{where}: only the last band is open-ended, and each band's max is >= its min"
                )
            out.append(
                (
                    int(lo),
                    None if hi is None else int(hi),
                    _levels(f"{where} band {i + 1}", b["levels"], formats),
                )
            )
            expect = 0 if hi is None else int(hi) + 1
        if not out:
            raise RubricError(f"{where}: a bands rule needs at least one band")
        bands = tuple(out)
        used = {lv for _, _, row in bands for lv in row.values()}
    else:  # fixed
        fixed = _levels(where, rule.get("levels"), formats)
        used = set(fixed.values())
        if rule.get("upside") is not None:
            upside = dict(rule["upside"])
            if upside.get("format") not in formats or upside.get("rate") != "conference_abstract_to_paper":
                raise RubricError(
                    f"{where}: an upside names one format and the conference's abstract-to-paper rate"
                )
            lv = upside.get("level")
            if not isinstance(lv, int) or not fixed[upside["format"]] < lv <= 5:
                raise RubricError(
                    f"{where}: the upside level must be above the format's own level, at most 5"
                )
            used.add(lv)
    legend = {int(k): " ".join(str(v).split()) for k, v in (raw.get("legend") or {}).items()}
    missing = sorted(lv for lv in used if not legend.get(lv))
    if missing:
        raise RubricError(f"{where}: the legend must describe levels {missing} as situations")
    return Criterion(**common, legend=legend, input=inp, table=table, bands=bands, fixed=fixed, upside=upside)


def parse_rubric(text: str) -> Rubric:
    """Load and cross-check the rubric; any weight, rule, limit or threshold without a source is refused.

    A missing key, a value of the wrong type or a file that isn't YAML is refused as a RubricError too, so
    `validate` reports it in words instead of a traceback."""
    try:
        return _parse_rubric(text)
    except RubricError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError, yaml.YAMLError) as e:
        if isinstance(e, KeyError):
            what = f"missing {e}"
        else:
            what = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise RubricError(
            f"the rubric is malformed ({what}); compare it with the shipped format_rubric.yaml"
        ) from e


def _parse_rubric(text: str) -> Rubric:
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise RubricError("the rubric is empty or isn't a YAML mapping")
    if raw.get("schema_version") != 1:
        raise RubricError("the rubric's schema_version must be 1")
    sources = {str(k): dict(v) for k, v in (raw.get("sources") or {}).items()}
    for sid, s in sources.items():
        if not s.get("citation") or not (s.get("pmid") or s.get("doi") or s.get("url")):
            raise RubricError(f"source {sid}: needs a citation and a PMID, DOI or URL")
    inputs = {k: {str(a): str(b) for a, b in v.items()} for k, v in (raw.get("inputs") or {}).items()}
    if set(inputs) != {"contribution", "data_maturity"}:
        raise RubricError("the rubric's inputs must list the values of contribution and data_maturity")
    formats = tuple(
        Format(
            id=str(f["id"]), label=str(f["label"]), guideline=str(f["guideline"]),
            venues=tuple(_venue(f"format {f['id']}", v, sources) for v in f.get("venues") or ()),
        )
        for f in raw["formats"]
    )  # fmt: skip
    ids = [f.id for f in formats]
    if len(set(ids)) != len(ids) or len(ids) < 2:
        raise RubricError("the rubric needs at least two formats, each with its own id")
    criteria = tuple(_criterion(c, inputs, ids, sources) for c in raw["criteria"])
    cids = [c.id for c in criteria]
    if len(set(cids)) != len(cids):
        raise RubricError("each criterion needs its own id")
    gates = [c for c in criteria if c.gate]
    if len(gates) != 1 or not gates[0].table:
        raise RubricError("exactly one criterion, a table rule, is the contribution-fit gate")
    for value, row in gates[0].table.items():
        if all(lv == 1 for lv in row.values()):
            raise RubricError(f"the gate leaves no format for contribution {value}")
    named = {c.priority for c in criteria if c.priority}
    if named != set(PRIORITIES):
        raise RubricError(
            f"each priority on the menu ({', '.join(PRIORITIES)}) must name exactly one criterion"
        )
    w = raw["weights"]
    if w["default"].get("scheme") != "unit" or w["priorities"].get("scheme") != "rank_order_centroid":
        raise RubricError("weights: the default scheme is unit and the priorities scheme rank_order_centroid")
    conc = float(w["dirichlet"]["concentration"])
    if not conc > 0:
        raise RubricError("weights: the Dirichlet concentration must be a positive number")
    sim = raw["simulation"]
    draws, seed = int(sim["draws"]), int(sim["seed"])
    low, high = float(sim["flip_multipliers"]["low"]), float(sim["flip_multipliers"]["high"])
    if draws < 1 or not 0 < low < 1 < high:
        raise RubricError("simulation: draws >= 1 and flip multipliers low < 1 < high")
    rv = raw["review"]
    act, discuss = float(rv["act"]), float(rv["discuss"])
    if not 0 < discuss < act <= 1:
        raise RubricError("review: need 0 < discuss < act <= 1")
    if raw.get("status") not in ("draft", "reviewed"):
        raise RubricError("the rubric's status is draft or reviewed")
    return Rubric(
        version=str(raw["version"]),
        status=str(raw["status"]),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        inputs=inputs,
        formats=formats,
        criteria=criteria,
        sources=sources,
        default_weights=_basis("weights.default", w["default"].get("basis"), sources),
        priority_weights=_basis("weights.priorities", w["priorities"].get("basis"), sources),
        concentration=conc,
        dirichlet=_basis("weights.dirichlet", w["dirichlet"].get("basis"), sources),
        draws=draws,
        seed=seed,
        flip_low=low,
        flip_high=high,
        simulation=_basis("simulation", sim.get("basis"), sources),
        act=act,
        discuss=discuss,
        review=_basis("review", rv.get("basis"), sources),
        open_questions=tuple(" ".join(str(q).split()) for q in raw.get("open_questions") or ()),
    )


def rubric_text() -> str:
    return resources.files("health_data_registry").joinpath(RUBRIC_FILE).read_text("utf-8")


@cache
def load_rubric() -> Rubric:
    """The rubric shipped with the package, cross-checked."""
    return parse_rubric(rubric_text())


# ------------------------------------------------------------------------------------------ inputs


def _norm(value: str) -> str:
    return "_".join(value.strip().lower().replace("/", " ").replace("-", " ").split())


ALIASES = {"methods": "methods_standard", "methods_or_standard": "methods_standard", "standard": "methods_standard", "preliminary": "preliminary_counts",
           "frozen": "frozen_dataset", "case_report": "case", "case_series": "case"}  # fmt: skip


def _enum(name: str, value: Any, allowed: dict[str, str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    v = _norm(value)
    v = ALIASES.get(v, v)
    return v if v in allowed else None


def _gave(value: Any) -> str:
    """The value a clinician gave, as plain text for a refusal; nothing when it was left out."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if isinstance(value, list | tuple):
        return f" You gave: {', '.join(str(v) for v in value)}."
    return f" You gave: {value}."


def _whole(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


@dataclass(frozen=True)
class Inputs:
    contribution: str
    data_maturity: str
    worked_examples: int
    needs_methods_section: bool
    weeks_to_deadline: int | None
    conference: str | None  # the base-rate file's own name for it
    priorities: tuple[str, ...]


def _check_inputs(
    rubric: Rubric,
    contribution: Any,
    data_maturity: Any,
    worked_examples: Any,
    needs_methods_section: Any,
    weeks_to_deadline: Any,
    conference: Any,
    priorities: Any,
) -> Inputs | str:
    """The inputs, normalised; or the plain-words reason they can't be used."""
    c = _enum("contribution", contribution, rubric.inputs["contribution"])
    if c is None:
        return (
            f"What kind of work is this? contribution must be one of: {_menu(rubric.inputs['contribution'])}."
            + _gave(contribution)
        )
    d = _enum("data_maturity", data_maturity, rubric.inputs["data_maturity"])
    if d is None:
        return (
            f"How far along is the data? data_maturity must be one of: {_menu(rubric.inputs['data_maturity'])}."
            + _gave(data_maturity)
        )
    n = _whole(worked_examples)
    if n is None or n < 0:
        return (
            "worked_examples must be a whole number, 0 or more: how many worked examples (cases, places or "
            "analyses shown in full) the piece needs." + _gave(worked_examples)
        )
    if not isinstance(needs_methods_section, bool):
        return (
            "needs_methods_section must be true or false: does a reader need a methods section to trust the "
            "numbers?" + _gave(needs_methods_section)
        )
    weeks: int | None = None
    if weeks_to_deadline is not None:
        weeks = _whole(weeks_to_deadline)
        if weeks is None or weeks < 0:
            return (
                "weeks_to_deadline must be a whole number of weeks, 0 or more, before the named conference's "
                f"deadline, or left out.{_gave(weeks_to_deadline)} If the deadline has passed, leave it out."
            )
    conf: str | None = None
    if conference is not None:
        if not isinstance(conference, str):
            return f"conference must be a conference's name, such as IDWeek, or left out.{_gave(conference)}"
        if conference.strip():
            found = publication.find_conference(conference)
            if found is None:
                return (
                    f'"{conference}" isn\'t a conference this tool knows. Use one of: '
                    f"{', '.join(publication.conference_names())}; or leave it out."
                )
            conf = found.name
    if priorities is None or priorities == "":
        prio: tuple[str, ...] = ()
    else:
        if isinstance(priorities, str):
            priorities = [priorities]
        if not isinstance(priorities, list | tuple):
            return f"priorities must be a list chosen from: {', '.join(PRIORITIES)}; or left out."
        out = []
        for p in priorities:
            v = _norm(p) if isinstance(p, str) else ""
            if v not in PRIORITIES:
                return (
                    f'"{p}" isn\'t on the priorities menu. Choose from: {", ".join(PRIORITIES)}, most important '
                    "first; or leave priorities out to use the rubric's equal weights."
                )
            if v in out:
                return f"{v} is listed twice in priorities. Name each priority once, most important first."
            out.append(v)
        prio = tuple(out)
    return Inputs(c, d, n, needs_methods_section, weeks, conf, prio)


def _menu(values: dict[str, str]) -> str:
    return "; ".join(f"{k} ({v})" for k, v in values.items())


# ------------------------------------------------------------------------------------------ scoring


@dataclass(frozen=True)
class Rate:
    label: str
    k: int
    n: int
    source: str


@dataclass(frozen=True)
class Evaluated:
    criterion: Criterion
    input_text: str
    rule_text: str
    levels: dict[str, int]  # the level each format certainly gets
    rate: Rate | None = None  # with an upside: the sourced rate that may lift one format


def _rate_for(conference: str | None) -> tuple[Rate | None, str]:
    if conference is None:
        return None, "no conference was named, so the chance an abstract becomes a full paper is unknown"
    conf = publication.find_conference(conference)
    if conf is None or conf.abstract_to_paper == publication.UNKNOWN:
        return None, f"no verified base rate yet for how often {conference} abstracts become full papers"
    r = publication.load_base_rates().rates[conf.abstract_to_paper]
    if r.k is None:  # a rate without its count is unknown here, never read as 0 of n
        return None, f"the base rate for {conference} abstracts has no verified count of papers yet"
    return Rate(f"{conference} abstracts that became full papers", r.k, r.n,
                f"{r.short_citation}, PMID {r.pmid}, doi {r.doi}"), ""  # fmt: skip


def evaluate(rubric: Rubric, inp: Inputs) -> tuple[list[Evaluated], list[dict[str, str]]]:
    """Each criterion's levels for these inputs, and the criteria (or parts) not used, with the reason."""
    used: list[Evaluated] = []
    not_used: list[dict[str, str]] = []
    for c in rubric.criteria:
        if c.kind == "unknown":
            not_used.append(
                {"criterion": c.id, "label": c.label, "kind": "unknown", "reason": c.unknown_reason}
            )
            continue
        if c.requires == "conference" and inp.conference is None:
            not_used.append({"criterion": c.id, "label": c.label, "kind": "not used",
                             "reason": "no conference was named, so there is no deadline"})  # fmt: skip
            continue
        if c.kind == "table":
            value = (
                str(inp.needs_methods_section).lower()
                if c.input == "needs_methods_section"
                else getattr(inp, c.input)
            )
            used.append(Evaluated(c, f"{c.input} = {value}", f"table: {c.input} {value}", c.table[value]))
        elif c.kind == "bands":
            value = getattr(inp, c.input)
            if value is None:
                not_used.append({"criterion": c.id, "label": c.label, "kind": "not used",
                                 "reason": f"{c.input.replace('_', ' ')} was not given"})  # fmt: skip
                continue
            for lo, hi, row in c.bands:
                if value >= lo and (hi is None or value <= hi):
                    band = f"{lo}+" if hi is None else (f"{lo}" if lo == hi else f"{lo}-{hi}")
                    used.append(Evaluated(c, f"{c.input} = {value}", f"band: {c.input} {band}", row))
                    break
        else:  # fixed, maybe with a sourced upside
            rate = None
            text = "by format"
            if c.upside is not None:
                rate, why = _rate_for(inp.conference)
                up_label = _label(rubric, c.upside["format"])
                if rate is None:
                    not_used.append({
                        "criterion": f"{c.id} ({c.upside['format']} upside)",
                        "label": f"{c.label}: the chance a {up_label} later becomes a full paper",
                        "kind": "unknown",
                        "reason": f"{why}; the {up_label} keeps only the record it certainly gives, never an "
                        "imputed rate",
                    })  # fmt: skip
                else:
                    text += (f"; {up_label} rises to level {c.upside['level']} with the sourced chance of a later "
                             "full paper")  # fmt: skip
            used.append(Evaluated(c, "the format itself", text, c.fixed, rate))
    return used, not_used


def gate(rubric: Rubric, inp: Inputs) -> tuple[list[str], list[dict[str, str]]]:
    """The formats left after the contribution-fit gate, and those left out with the reason."""
    g = next(c for c in rubric.criteria if c.gate)
    row = g.table[inp.contribution]
    keep = [f for f in rubric.format_ids if row[f] > 1]
    out = [
        {
            "format": f,
            "reason": f"{g.legend[1]} (contribution = {inp.contribution}); its scores are still shown",
        }
        for f in rubric.format_ids
        if row[f] == 1
    ]
    return keep, out


def declared_weights(rubric: Rubric, criteria: Sequence[str], priorities: Sequence[str]) -> dict[str, float]:
    """Unit weights, or rank-order centroid weights: the named priorities first in order, the rest tied.

    ROC weight for rank i of K: (1/K) * sum_{j=i..K} 1/j (Barron and Barrett 1996). Tied criteria each get the
    mean of the centroid weights of the ranks they share. Weights are normalised to sum to 1.
    """
    k = len(criteria)
    if k == 0:
        return {}
    by_priority = {c.priority: c.id for c in rubric.criteria if c.priority}
    ranked = [by_priority[p] for p in priorities if by_priority[p] in criteria]
    if not ranked:
        return {c: 1.0 / k for c in criteria}
    roc = [sum(1.0 / j for j in range(i, k + 1)) / k for i in range(1, k + 1)]
    out = {c: roc[i] for i, c in enumerate(ranked)}
    rest = [c for c in criteria if c not in out]
    if rest:
        tied = sum(roc[len(ranked) :]) / len(rest)
        out.update(dict.fromkeys(rest, tied))
    total = sum(out.values())
    return {c: out[c] / total for c in criteria}


@dataclass(frozen=True)
class Simulation:
    wins: dict[str, float]
    composites: dict[str, list[float]]
    rate_means: dict[str, float]  # criterion id -> mean of the drawn rate


def _level(e: Evaluated, f: str, p: float | None) -> float:
    lv = float(e.levels[f])
    if e.rate is not None and p is not None and e.criterion.upside and e.criterion.upside["format"] == f:
        return (
            lv + (float(e.criterion.upside["level"]) - lv) * p
        )  # expected level: the upside level with chance p
    return lv


def simulate(
    rubric: Rubric, used: Sequence[Evaluated], formats: Sequence[str], weights: dict[str, float]
) -> Simulation:
    """Fixed-seed draws: Dirichlet weights around the declared ones, Beta posteriors for sourced rates.

    Only `formats` (those the fit gate keeps) compete for first place, but every format in the rubric gets its
    scores, so a format left out is still shown with its numbers and the reason (ADR 0018)."""
    shown = rubric.format_ids
    rng = stats.SplitMix64(rubric.seed)
    ids = [e.criterion.id for e in used]
    alphas = [rubric.concentration * weights[c] for c in ids]
    wins = dict.fromkeys(formats, 0.0)
    comps: dict[str, list[float]] = {f: [] for f in shown}
    rate_sum = {e.criterion.id: 0.0 for e in used if e.rate is not None}
    for _ in range(rubric.draws):
        w = stats.dirichlet_draw(rng, alphas)
        ps: dict[str, float] = {}
        for e in used:
            if e.rate is not None:
                ps[e.criterion.id] = stats.beta_draw(rng, e.rate.k + 1, e.rate.n - e.rate.k + 1)
                rate_sum[e.criterion.id] += ps[e.criterion.id]
        score = {f: sum(wi * _level(e, f, ps.get(e.criterion.id)) for wi, e in zip(w, used, strict=True))
                 for f in shown}  # fmt: skip
        best = max(score[f] for f in formats)
        top = [f for f in formats if score[f] >= best - 1e-12]
        for f in top:
            wins[f] += 1.0 / len(top)
        for f in shown:
            comps[f].append(score[f])
    return Simulation(wins, comps, {c: s / rubric.draws for c, s in rate_sum.items()})


def _mean_level(e: Evaluated, f: str) -> float:
    p = (
        None if e.rate is None else (e.rate.k + 1) / (e.rate.n + 2)
    )  # posterior mean of Beta(k + 1, n - k + 1)
    return _level(e, f, p)


def flip_condition(rubric: Rubric, used: Sequence[Evaluated], formats: Sequence[str],
                   weights: dict[str, float], choice: str | None = None) -> dict[str, Any]:  # fmt: skip
    """On the declared weights: the one-weight multiplier in [low, high], nearest 1x, that puts another format first.

    Scaling weight c by m changes every format's total by the same normaliser, so the order follows
    S_f(m) = A_f + m * w_c * L_fc, with A_f the rest of the total. Another format g passes the top format t
    once m crosses m* = (A_t - A_g) / (w_c * (L_gc - L_tc)), which is exact; nothing is simulated.

    `choice` is the format the draws put first. When the declared weights put another format first, no flip is
    given: the sentence says the two disagree, so it never contradicts the choice shown beside it.
    """
    totals = {f: sum(weights[e.criterion.id] * _mean_level(e, f) for e in used) for f in formats}
    top = max(formats, key=lambda f: (totals[f], -formats.index(f)))
    result: dict[str, Any] = {"declared_top": top, "found": False}
    if len(formats) < 2:
        result["text"] = "Only one format is left in the ranking, so no weight change can alter the choice."
        return result
    if choice is not None and choice != top:
        result["text"] = (
            f"On the rubric's weights exactly as written, {_label(rubric, top)} would rank first; "
            f"{_label(rubric, choice)} ranks first in more of the {rubric.draws:,} draws, where the weights are "
            "allowed to vary. Because the two disagree, no single weight change is named."
        )
        return result
    best: tuple[float, str, str, float] | None = None
    for e in used:
        c = e.criterion.id
        wc = weights[c]
        for g in formats:
            if g == top:
                continue
            lt, lg = _mean_level(e, top), _mean_level(e, g)
            if lg == lt or wc == 0:
                continue
            a_t, a_g = totals[top] - wc * lt, totals[g] - wc * lg
            m = (a_t - a_g) / (wc * (lg - lt))
            if not rubric.flip_low <= m <= rubric.flip_high or math.isclose(m, 1.0):
                continue
            # Past m* in the direction that helps g: larger m if g scores higher on c, smaller if lower.
            if (lg > lt) != (m > 1.0):
                continue
            dist = abs(math.log(m))
            if best is None or dist < best[0]:
                best = (dist, c, g, m)
    if best is None:
        result["text"] = (
            f"No single weight change between {rubric.flip_low:g}x and {rubric.flip_high:g}x puts another format "
            f"ahead of {_label(rubric, top)}."
        )
        return result
    _, c, g, m = best
    more = m > 1.0
    crit = rubric.criterion(c).label
    result.update(
        found=True, criterion=c, format=g, multiplier=round(m, 2), direction="more" if more else "less"
    )
    result["text"] = (
        f"If '{crit}' counted for {'more' if more else 'less'} than {m:.2f} times its weight in the rubric, "
        f"{_label(rubric, g)} would rank first instead of {_label(rubric, top)}."
    )
    return result


def _label(rubric: Rubric, fid: str) -> str:
    return next(f.label for f in rubric.formats if f.id == fid)


def _percentile(sorted_xs: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of sorted values."""
    i = min(len(sorted_xs) - 1, max(0, math.ceil(q * len(sorted_xs)) - 1))
    return sorted_xs[i]


def review_band(rubric: Rubric, p_max: float) -> str:
    if p_max >= rubric.act:
        return REVIEW_ACT
    if p_max >= rubric.discuss:
        return REVIEW_DISCUSS
    return REVIEW_CLOSE


# ------------------------------------------------------------------------------------------ the answer


def _score(e: Evaluated, f: str, sim: Simulation) -> tuple[float, dict[int, float]]:
    base = e.levels[f]
    if e.rate is not None and e.criterion.upside and e.criterion.upside["format"] == f:
        p = sim.rate_means[e.criterion.id]
        up = int(e.criterion.upside["level"])
        return base + (up - base) * p, {base: 1.0 - p, up: p}
    return float(base), {base: 1.0}


def _decide(rubric_sha: str, inp: Inputs) -> dict[str, Any]:
    """The decision for these inputs: a fresh copy each call, so no caller can change a cached answer."""
    return copy.deepcopy(_decide_cached(rubric_sha, inp))


@lru_cache(maxsize=256)
def _decide_cached(rubric_sha: str, inp: Inputs) -> dict[str, Any]:
    rubric = load_rubric()
    used, not_used = evaluate(rubric, inp)
    formats, excluded = gate(rubric, inp)
    ids = [e.criterion.id for e in used]
    weights = declared_weights(rubric, ids, inp.priorities)
    sim = simulate(rubric, used, formats, weights)
    probabilities = {f: sim.wins.get(f, 0.0) / rubric.draws for f in rubric.format_ids}
    p_max = max(probabilities.values())
    choice = next(f for f in rubric.format_ids if probabilities[f] == p_max)
    flip = flip_condition(rubric, used, formats, weights, choice)
    priorities_unused = []
    by_priority = {c.priority: c for c in rubric.criteria if c.priority}
    for p in inp.priorities:
        if by_priority[p].id not in ids:
            priorities_unused.append(p)
    # ADR 0018: the choice is not changed, but a lowest level on any criterion it was scored on is said plainly.
    lowest = [
        {"criterion": e.criterion.id, "label": e.criterion.label, "legend": e.criterion.legend[1]}
        for e in used
        if not e.criterion.gate and e.levels[choice] == 1 and 1 in e.criterion.legend
    ]
    format_rows = []
    for f in rubric.formats:
        comp = sorted(sim.composites.get(f.id, []))
        format_rows.append({
            "format": f.id,
            "label": f.label,
            "status": "compared" if f.id in formats else "left out of the ranking (shown, not ranked)",
            "reason": next((x["reason"] for x in excluded if x["format"] == f.id), ""),
            "p_first": probabilities[f.id],
            "wins": sim.wins.get(f.id, 0.0),
            "draws": rubric.draws,
            "score_interval_95": [round(_percentile(comp, 0.025), 2), round(_percentile(comp, 0.975), 2)] if comp else None,
            "guideline": f.guideline,
            "venues": [v.text for v in f.venues],
        })  # fmt: skip
    scores = []
    for e in used:
        for fid in rubric.format_ids:
            mean, probs = _score(e, fid, sim)
            src = e.criterion.basis.describe(rubric.sources)
            if e.rate is not None and e.criterion.upside and e.criterion.upside["format"] == fid:
                src += (f" Rate: {e.rate.label}, {e.rate.k} of {e.rate.n}, as Beta({e.rate.k + 1}, "
                        f"{e.rate.n - e.rate.k + 1}); {e.rate.source}.")  # fmt: skip
            scores.append({
                "type": "score",
                "format": fid,
                "criterion": e.criterion.id,
                "label": e.criterion.label,
                "input": e.input_text,
                "rule": e.rule_text,
                "score": round(mean, 4),
                "probabilities": {str(k): round(v, 4) for k, v in sorted(probs.items())},
                "legend": {str(k): v for k, v in sorted(e.criterion.legend.items())},
                "weight": round(weights[e.criterion.id], 4),
                "source": src,
                "gate": e.criterion.gate,
            })  # fmt: skip
    return {
        "choice": choice,
        "probabilities": probabilities,
        "confidence": stats.choice_confidence(p_max, len(formats)),
        "review": review_band(rubric, p_max),
        "flip": flip,
        "not_used": not_used,
        "excluded": excluded,
        "weights": weights,
        "priorities_unused": priorities_unused,
        "choice_lowest_levels": lowest,
        "formats": format_rows,
        "scores": scores,
        "has_rate": any(e.rate is not None for e in used),
    }


def _refuse(text: str, fmt: Fmt) -> Out:
    return message(text, fmt, refused="unknown_input", next_step=NEXT_STEP)


def _notes(rubric: Rubric, inp: Inputs, d: dict[str, Any]) -> list[str]:
    choice_label = _label(rubric, d["choice"])
    p = d["probabilities"][d["choice"]]
    flip = d["flip"]
    flip_text = flip["text"]
    if d["review"] == REVIEW_CLOSE and not flip["found"] and flip["declared_top"] == d["choice"]:
        flip_text += (
            " The closeness comes from several weights changing together across the draws, not from any one of "
            "them."
        )
    notes = []
    if d["review"] == REVIEW_CLOSE:
        notes.append(f"What would change it: {flip_text}")
    notes += [
        f"Choice: {choice_label} ranks first in {p:.0%} of {rubric.draws:,} draws (seed {rubric.seed}); "
        f"confidence {d['confidence']:.2f}; review: {REVIEW_SAYS[d['review']]}.",
        CONFIDENCE_MEANS,
        NOT_A_PREDICTION,
    ]
    if rubric.status == "draft":
        notes.append(DRAFT_NOTE)
    for x in d["choice_lowest_levels"]:
        notes.append(f"Note: {choice_label} is at the lowest level on '{x['label']}': {x['legend']}.")
    if d["review"] != REVIEW_CLOSE:
        notes.append(f"What would change it: {flip_text}")
    notes.append(
        f"A probability is the share of {rubric.draws:,} reasonable weightings under which a format ranks first. "
        "Method: each draw takes the weights from a Dirichlet centred on the rubric's weights (concentration "
        f"{rubric.concentration:g})"
        + (", and each sourced rate from its Beta posterior" if d["has_rate"] else "")
        + ". Most levels come from declared rules, not measured rates, so the answer is only as good as those "
        "rules."
    )
    ranked = [pr for pr in inp.priorities if pr not in d["priorities_unused"]]
    if ranked:
        notes.append(
            f"Weights: your priorities ({', '.join(ranked)}) in order, then the rest tied; "
            + rubric.priority_weights.describe(rubric.sources)
        )
    else:
        notes.append(
            ("Weights: none of your priorities can be scored yet" if inp.priorities else
             "Weights: no priorities were given")
            + ", so the rubric's defaults apply: "
            + rubric.default_weights.describe(rubric.sources)
        )  # fmt: skip
    for pr in d["priorities_unused"]:
        notes.append(
            f"You named {pr} as a priority, but it can't be scored yet, so it was left out of the weights."
        )
    notes += [
        f"Left out of the ranking: {_label(rubric, x['format'])}. {_sentence(x['reason'])}"
        for x in d["excluded"]
    ]
    notes += [f"{x['kind'].capitalize()}: {x['label']}. {_sentence(x['reason'])}" for x in d["not_used"]]
    if inp.contribution == "case":
        notes.append("A case report is its own format and isn't compared here; ask the journal for its case-report "
                     "instructions.")  # fmt: skip
    notes += [
        NOT_A_RANKING,
        f"Rubric {rubric.version} ({rubric.status}: every proposed declaration awaits maintainer review), sha256 "
        f"{rubric.sha256}. The same inputs and rubric always give the same answer.",
    ]
    return notes


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


def choose_output_format(
    contribution: Any = None,
    data_maturity: Any = None,
    worked_examples: Any = None,
    needs_methods_section: Any = None,
    weeks_to_deadline: Any = None,
    conference: Any = None,
    priorities: Any = None,
    fmt: Fmt = "csv",
) -> Out:
    """Which output format fits this project now, as a Choice with per-criterion Scores."""
    rubric = load_rubric()
    inp = _check_inputs(rubric, contribution, data_maturity, worked_examples, needs_methods_section,
                        weeks_to_deadline, conference, priorities)  # fmt: skip
    if isinstance(inp, str):
        return _refuse(inp, fmt)
    d = _decide(rubric.sha256, inp)
    notes = _notes(rubric, inp, d)
    if fmt == "json":
        return {
            "type": "choice",
            "choice": d["choice"],
            "probabilities": d["probabilities"],
            "confidence": round(d["confidence"], 4),
            "confidence_means": CONFIDENCE_MEANS,
            "review": d["review"],
            "flip_condition": d["flip"],
            "not_used": d["not_used"],
            "excluded": d["excluded"],
            "choice_lowest_levels": d["choice_lowest_levels"],
            "weights": {k: round(v, 4) for k, v in d["weights"].items()},
            "inputs": {
                "contribution": inp.contribution,
                "data_maturity": inp.data_maturity,
                "worked_examples": inp.worked_examples,
                "needs_methods_section": inp.needs_methods_section,
                "weeks_to_deadline": inp.weeks_to_deadline,
                "conference": inp.conference,
                "priorities": list(inp.priorities),
            },  # fmt: skip
            "formats": d["formats"],
            "scores": d["scores"],
            "rubric_version": rubric.version,
            "rubric_status": rubric.status,
            "rubric_sha256": rubric.sha256,
            "draws": rubric.draws,
            "seed": rubric.seed,
            "notes": notes,
        }
    return _csv(d, notes)


CSV_COLUMNS = ["row", "format", "criterion", "p_first", "draws", "score", "score_interval_95",
               "level_probabilities", "input", "rule", "weight", "status", "venues", "legend", "source"]  # fmt: skip


def _csv(d: dict[str, Any], notes: list[str]) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in d["formats"]:
        interval = (
            ""
            if r["score_interval_95"] is None
            else f"{r['score_interval_95'][0]}–{r['score_interval_95'][1]}"
        )
        w.writerow(["format", r["format"], "", f"{r['p_first']:.4f}", r["draws"], "", interval, "", "",
                    f"reporting guideline: {r['guideline']}", "", r["status"], " | ".join(r["venues"]), "",
                    ""])  # fmt: skip
    for s in d["scores"]:
        w.writerow([
            "score", s["format"], s["criterion"], "", "", f"{s['score']:.2f}", "",
            "; ".join(f"{k}: {v:.4f}" for k, v in s["probabilities"].items()), s["input"], s["rule"],
            f"{s['weight']:.4f}", "gate" if s["gate"] else "used", "",
            "; ".join(f"{k}: {v}" for k, v in s["legend"].items()), s["source"],
        ])  # fmt: skip
    return buf.getvalue() + "\n" + "\n".join(f"# {n}" for n in notes) + "\n"
