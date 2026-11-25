"""The read-only tools, as plain functions (server.py exposes them over MCP).

The spec's one tool list: list_sources, search_datasets, describe_dataset, suggest_datasets, get_series,
get_aligned_with_gbd, get_rate, get_provenance, list_caveats, find_prior_work, and query_sql as a guarded
escape hatch. Results are CSV by default (as the GBD connector's are), followed by plain lines saying where
the numbers came from. Every refusal is written for a clinician: what happened, and what to try next.
"""

from __future__ import annotations

import csv
import difflib
import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple

import duckdb

from health_data_registry import geo
from health_data_registry.deadline import DeadlineExceeded
from health_data_registry.paging import paged, respond
from health_data_registry.query import Engine, QueryRefused, next_step_for
from health_data_registry.registry import Dataset, Registry, Series, UnknownDataset, norm_label, spelling_key

Fmt = Literal["csv", "json"]
Out = str | dict[str, Any]
GBD_LAST_YEAR = 2023

_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "could",
        "data",
        "dataset",
        "datasets",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "our",
        "project",
        "public",
        "rising",
        "show",
        "support",
        "that",
        "the",
        "their",
        "there",
        "this",
        "to",
        "use",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "any",
        "rate",
        "rates",
        "trend",
        "trends",
        "compare",
        "compared",
        "versus",
        "vs",
        "since",
    ]
)

_GEO_WORDS = {
    "county": "county", "counties": "county", "city": "city", "zip": "zip", "state": "state", "states": "state",
    "national": "nation", "nationally": "nation", "country": "country", "countries": "country",
    "global": "country", "worldwide": "country",
}  # fmt: skip

NO_PATIENT_DATA = "Never put patient information in the chat."


# ------------------------------------------------------------------------------------------ result shape


@dataclass
class Result:
    """A table plus the lines a clinician needs to trust it: source, date, method, caveats."""

    columns: list[str]
    rows: list[list[Any]]
    notes: list[str] = field(default_factory=list)
    # Named parts beside the table (e.g. presentation_rules): their own keys in JSON, note lines in CSV.
    extra: dict[str, list[str]] = field(default_factory=dict)

    def render(self, fmt: Fmt = "csv") -> Out:
        self.notes = list(dict.fromkeys(self.notes))  # one dataset with several series repeats its notes
        if fmt == "json":
            return {"columns": self.columns, "rows": self.rows, "notes": self.notes, **self.extra}
        self.notes += [f"{k}: {v}" for k, vs in self.extra.items() for v in vs]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(self.columns)
        w.writerows(["" if v is None else v for v in r] for r in self.rows)
        text = buf.getvalue()
        if self.notes:
            text += "\n" + "\n".join(f"# {n}" for n in self.notes) + "\n"
        return text


PRESENTATION_RULES = [
    "Never add up rates, percents or estimates, and never add year-to-date (running) totals across weeks: a "
    "year-to-date figure is read at one week.",
    "Different condition labels (clinical and screening, confirmed and probable, or any two labels) are different "
    "things: show them apart and never add them.",
    "Report a change, ratio, difference or share only when a tool returned it (compare_rates, get_trend), quoted as "
    "returned; never work one out yourself.",
    "Never give a number from memory or from another source. If no tool returned a number, there is no number.",
    "Give every number with its source, retrieved_at and the caveats that came with it.",
]

# Optional parts of a decline, in the order a clinician reads them, and how each is introduced in plain text.
_DECLINE_PARTS = {
    "did_you_mean": "Did you mean (close spellings of what you asked; check one means what you meant before "
    "using it)",
    "nearest_served": "Nearest served here",
    "served_elsewhere": "Served elsewhere",
    "outside_registry": "Outside this registry",
    "available": "Values that exist",
}


def _part_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return "; ".join(f"{k}: {_part_text(v)}" for k, v in value.items())
    if isinstance(value, list | tuple):
        return "; ".join(_part_text(v) for v in value)
    return str(value)


def message(text: str, fmt: Fmt = "csv", **meta: Any) -> Out:
    """A message with no table. A decline (meta `refused`) always carries next_step, plus where they apply
    did_you_mean, nearest_served, served_elsewhere, outside_registry and available; empty parts are left out."""
    if "refused" in meta:
        meta["next_step"] = meta.get("next_step") or next_step_for(meta["refused"])
        meta = {k: v for k, v in meta.items() if v not in (None, "", [], {})}
    if fmt == "json":
        return {"message": text, **meta}
    if "refused" not in meta:
        return text
    lines = [text]
    if meta["next_step"] not in text:
        lines.append(f"Next step: {meta['next_step']}")
    lines += [f"{label}: {_part_text(meta[k])}" for k, label in _DECLINE_PARTS.items() if k in meta]
    return "\n".join(lines)


def plain(e: Exception) -> str:
    """Any failure in plain words with a next step. Never a stack trace or a status code."""
    code = getattr(e, "code", None)
    if code == "no_snapshot":
        return f"{e} Run the Something's wrong helper, which walks you through loading this source again."
    if isinstance(e, UnknownDataset):
        # Only the registry's own message is echoed; any other KeyError would leak an internal name.
        return f"{e}. Use search_datasets to see what is available."
    if isinstance(e, QueryRefused | DeadlineExceeded):
        return str(e)
    return (
        "The data for this question couldn't be read, so no numbers are shown. Try again; if it keeps "
        "happening, run the Something's wrong helper and mention this question."
    )


def refusal(e: Exception, fmt: Fmt = "csv") -> Out:
    code = getattr(e, "code", None) or ("unknown_dataset" if isinstance(e, UnknownDataset) else "error")
    step = getattr(e, "next_step", None)
    if isinstance(e, UnknownDataset):
        step = "Use search_datasets to find the source_id you want, then ask again with it."
    elif code == "error":
        step = "Try again; if it keeps happening, run the Something's wrong helper and mention this question."
    return message(plain(e), fmt, refused=code, next_step=step)


# ------------------------------------------------------------------------------------------ helpers


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9\-]*", text.lower().replace(".", ""))
    return {w for w in words if w not in _STOP and len(w) > 1}


def _haystack(ds: Dataset) -> set[str]:
    e = ds.entry
    parts = [e.title, e.grain, " ".join(e.research_areas), e.agency, e.publisher]
    parts += [f"{c.condition.replace('_', ' ')} {c.cause} {c.pathogen or ''}" for c in e.gbd.causes]
    parts += [f"{s.condition.replace('_', ' ')} {s.label}" for s in ds.semantic.series]
    parts += [f"{m.name.replace('_', ' ')} {m.description}" for m in ds.semantic.metrics]
    parts += [*e.conditions.labels, *(a.alias for a in e.conditions.aliases)]
    return _tokens(" ".join(parts))


def _years(ds: Dataset) -> str:
    return f"{ds.entry.years.start}-{ds.entry.years.end or 'present'}"


def _years_overlap(ds: Dataset, start: int | None, end: int | None) -> bool:
    y = ds.entry.years
    hi = y.end if y.end is not None else 9999
    return not ((start is not None and start > hi) or (end is not None and end < y.start))


def _freshness(engine: Engine | None, ds: Dataset) -> str:
    if engine is None:
        return ""
    engine.connect()
    m = engine.manifests.get(ds.id)
    return m.retrieved_at[:10] if m else "not fetched yet"


PROVENANCE_COLUMNS = [
    "source_url", "retrieved_at", "raw_sha256", "version", "licence", "citation", "contract_status",
]  # fmt: skip


def provenance_fields(ds: Dataset, engine: Engine | None) -> dict[str, str]:
    """Hard rule 8 for a source, without downloading anything: where it comes from, when it was fetched (if it
    has been), the raw files' sha256, the snapshot and contract version, the licence, citation and review state."""
    m = None
    if engine is not None:
        engine.connect()
        m = engine.manifests.get(ds.id)
    hashes = list(m.raw_sha256 or []) if m else []
    sha = " ".join(hashes[:2]) + (
        f" (+{len(hashes) - 2} more; get_provenance lists all)" if len(hashes) > 2 else ""
    )
    return {
        "source_url": ds.entry.source.landing_url,
        "retrieved_at": str(m.retrieved_at)[:19] if m else "not fetched yet",
        "raw_sha256": sha if hashes else (_no_hash(m) if m else "not fetched yet"),
        "version": f"snapshot {m.snapshot_id}; contract {m.contract_version}"
        if m
        else f"contract {ds.contract.version}",
        "licence": f"{ds.entry.licence.id} ({ds.entry.licence.terms_url})",
        "citation": ds.entry.citation,
        "contract_status": ds.entry.status,
    }


def _no_hash(m: Any) -> str:
    """What to say when a manifest lists no raw file hash: only a live read has none by design (hard rule 8)."""
    if m.origin == "publisher":
        return "read live, not stored"
    return "missing: this stored copy lists no raw file hash; run the Something's wrong helper"


# Words too general to name a condition on their own: a label is matched by its specific words (the pathogen or
# disease), never by these alone. "population" would otherwise pull in an indicator name, "acute" dozens of labels.
STOP_WORDS = frozenset(
    [
        "a",
        "acute",
        "all",
        "and",
        "case",
        "cases",
        "chronic",
        "clinical",
        "confirmed",
        "disease",
        "diseases",
        "fever",
        "for",
        "in",
        "infection",
        "infections",
        "invasive",
        "of",
        "other",
        "per",
        "population",
        "probable",
        "screening",
        "syndrome",
        "the",
        "total",
        "unspecified",
        "virus",
        "viral",
        "year",
    ]
)


def _specific_words(text: str) -> set[str]:
    return {w for w in norm_label(text).split() if w not in STOP_WORDS}


def series_for(
    registry: Registry,
    condition: str,
    measure: str | None = None,
    source_id: str | None = None,
    *,
    kind: Literal["plain", "weekly", "age"] = "plain",
) -> list[tuple[Dataset, Series]]:
    """Series whose condition code or clinical label matches the words asked for.

    kind "plain" (the default, used by get_series) leaves out weekly and age-specific series, so a year is never
    built by adding up provisional weeks; "weekly" and "age" return only those (check_signal, age_adjust).
    """
    want = norm_label(condition)
    found: list[tuple[Dataset, Series]] = []

    def fits(s: Series) -> bool:
        s_kind = "weekly" if s.week_dimension else "age" if s.age_dimension else "plain"
        return s_kind == kind and (measure is None or s.measure == measure)

    for ds in registry:
        if source_id and ds.id != source_id:
            continue
        named = [s for s in ds.semantic.series if _names_match(want, {s.condition, s.label})]
        if named:
            found += [(ds, s) for s in named if fits(s)]
            continue
        # Any other label the publisher uses (the catalog's vocabulary) reads through the generic series.
        for label in catalogued_labels(ds, condition):
            found += [(ds, s) for s in ds.condition_series(label) if fits(s)]
    return found


def _names_match(want: str, names: set[str]) -> bool:
    keys = {norm_label(n) for n in names}
    return bool(want) and (want in keys or any(re.search(rf"\b{re.escape(want)}\b", n) for n in keys))


def catalogued_labels(ds: Dataset, condition: str) -> list[str]:
    """The catalog labels the words name, each a separate series: every spelling the publisher uses of the label
    asked for (or of a declared alias), else, for 3 characters or more that include a specific word (not only
    words like "acute", "virus" or "population"), the labels holding those words whole."""
    exact = ds.labels_for(condition)
    if exact:
        return exact
    want = norm_label(condition)
    if len(want) < 3 or not _specific_words(want):
        return []
    return [x for x in ds.condition_labels if _names_match(want, {x})]


def spelling_notes(labels: Sequence[str]) -> list[str]:
    """A note for each condition the publisher spells more than one way (e.g. in different years): the spellings
    are shown as separate rows, never merged, until a maintainer declares them one condition (hard rule 1)."""
    groups: dict[str, list[str]] = {}
    for x in dict.fromkeys(labels):
        groups.setdefault(spelling_key(x), []).append(x)
    return [
        f"The publisher spells this condition {len(g)} ways ({'; '.join(repr(x) for x in g)}), often in "
        "different years. Each spelling is shown as its own rows and never added to the other; read them "
        "together for the full span of years."
        for g in groups.values()
        if len(g) > 1
    ]


def _all_labels(registry: Registry) -> dict[str, str]:
    """Every condition label served here (catalog vocabulary and named series), normalised -> as published."""
    out: dict[str, str] = {}
    for ds in registry:
        for x in [*ds.condition_labels, *(s.label for s in ds.semantic.series)]:
            out.setdefault(norm_label(x), x)
    return out


def unknown_condition(registry: Registry, condition: str, fmt: Fmt, what: str = "") -> Out:
    """No source serves these words: say so, with spelling suggestions (did_you_mean), labels sharing a word
    (nearest_served) and, when the words name a GBD cause in a crosswalk, that cause by name (outside_registry).
    GBD is never fetched."""
    want = norm_label(condition)
    labels = _all_labels(registry)
    close = difflib.get_close_matches(want, list(labels), n=3, cutoff=0.75)
    did_you_mean = [labels[k] for k in close]
    # Only a label sharing a specific word (the pathogen or disease, never "virus" or "infection") is offered.
    words = {w for w in _specific_words(want) if len(w) >= 4}
    nearest = [v for k, v in sorted(labels.items()) if words & set(k.split()) and v not in did_you_mean][:8]
    outside = _gbd_causes_named(registry, condition)
    return message(
        f"No loaded source reports '{condition}'" + (f" as {what}" if what else "") + ".",
        fmt,
        refused="unknown_condition",
        did_you_mean=did_you_mean,
        nearest_served=nearest,
        outside_registry=outside,
    )


def _gbd_causes_named(registry: Registry, text: str) -> list[str]:
    """GBD causes (by name, from the catalogs' crosswalks) that the words name as whole words: where modelled
    burden for them lives. Only the name is given; this server never fetches GBD."""
    said = f" {norm_label(text)} "
    causes = sorted(
        {c.cause for ds in registry for c in ds.entry.gbd.causes if f" {norm_label(c.cause)} " in said}
    )
    return [f"For modelled burden of {c} (GBD cause '{c}'), ask the IHME GBD connector." for c in causes]


# ------------------------------------------------------------------------------------------ catalog tools


def list_sources(
    registry: Registry,
    engine: Engine | None = None,
    *,
    condition: str | None = None,
    geography_level: str | None = None,
    fmt: Fmt = "csv",
) -> Out:
    rows = []
    for ds in registry:
        e = ds.entry
        if geography_level and geography_level not in e.geography:
            continue
        if condition and not series_for(registry, condition, None, ds.id):
            continue
        rows.append(
            [e.id, e.title, e.publisher, _years(ds), " ".join(e.geography), e.refresh,
             _freshness(engine, ds), e.status, ds.contract.version,
             engine.provenance(ds.id)["data_from"] if engine else "",
             *provenance_fields(ds, engine).values()]
        )  # fmt: skip
    notes = (
        ["review = draft means no maintainer has checked this source's contract yet."]
        if rows
        else [
            "No source matches; try without a condition or geography.",
            "Next step: " + next_step_for("unknown_condition"),
        ]
    )
    cols = [
        "source_id",
        "title",
        "publisher",
        "years",
        "geography",
        "updated",
        "last_fetched",
        "review",
        "contract_version",
        "data_from",
        *PROVENANCE_COLUMNS,
    ]
    return Result(cols, rows, notes).render(fmt)


def search_datasets(
    registry: Registry,
    *,
    topic: str = "",
    id_area: str | None = None,
    geography: str | None = None,
    year_start: int | None = None,
    year_end: int | None = None,
    limit: int = 5,
    engine: Engine | None = None,
    fmt: Fmt = "csv",
) -> Out:
    want = _tokens(topic)
    ranked: list[tuple[int, str, list[Any]]] = []
    for ds in registry:
        e = ds.entry
        if id_area and id_area not in e.research_areas:
            continue
        if geography and geography not in e.geography:
            continue
        if not _years_overlap(ds, year_start, year_end):
            continue
        hits = sorted(want & _haystack(ds))
        if want and not hits:
            continue
        reason = ("mentions " + ", ".join(hits) + "; " if hits else "") + e.grain
        ranked.append(
            (len(hits), e.id, [e.id, e.title, e.agency, _years(ds), " ".join(e.geography), reason,
                               *provenance_fields(ds, engine).values()])
        )  # fmt: skip
    ranked.sort(key=lambda t: (-t[0], t[1]))
    rows = [r for _, _, r in ranked[: max(1, min(limit, 20))]]
    if not rows:
        return message(
            f"No dataset matched '{topic}'" + (" with those filters" if id_area or geography else "") + ".",
            fmt,
            refused="no_dataset_matched",
            next_step="Try a broader topic, or suggest_datasets with your whole question.",
            outside_registry=_gbd_causes_named(registry, topic),
        )
    notes = ["Next: describe_dataset with a source_id to see what it can and can't answer."]
    cols = ["source_id", "title", "agency", "years", "geography", "why_it_fits", *PROVENANCE_COLUMNS]
    return Result(cols, rows, notes).render(fmt)


def describe_dataset(
    registry: Registry, source_id: str, engine: Engine | None = None, *, fmt: Fmt = "csv"
) -> Out:
    try:
        ds = registry.get(source_id)
    except KeyError as err:
        return refusal(err, fmt)
    e, c, s = ds.entry, ds.contract, ds.semantic
    counts = [
        f"{m.name}: {m.kind}"
        + {
            "count": " (added up)",
            "running_count": " (a running total: added across places only at one week, never across weeks)",
        }.get(m.kind, " (never added up)")
        for m in s.measures
    ]
    running = [
        f"{m.name}: observation_type year_end_ytd is the running total ({m.measure}) at the last week of each "
        "year that has a number, always provisional ('provisional: as published at year end (through week N)', "
        "'provisional: running total so far', or 'incomplete' when later weeks have no number)"
        + (
            f"; observation_type subsequent_year_restatement is next year's table's figure for the same year "
            f"({m.restated_by.measure}), with the week it reaches, marked unverified_restatement. An unverified "
            "restatement is not a final disease count: the publisher doesn't say the two count the same thing, so "
            f"both are kept as separate observations and neither is final (resolution policy: "
            f"{m.restated_by.resolution_policy})"
            if m.restated_by
            else ""
        )
        for m in s.metrics
        if m.aggregation == "cumulative_latest"
    ]
    info: dict[str, Any] = {
        "source_id": e.id,
        "title": e.title,
        "publisher": e.publisher,
        "one_row_is": e.grain,
        "years": _years(ds),
        "geography": e.geography,
        "conditions": [
            {
                "condition": x.label,
                "measure": x.measure,
                "case_status": x.case_status,
                "case_definition": x.case_definition,
            }
            for x in s.series
        ],  # fmt: skip
        "columns": [
            {
                "name": col.name,
                "means": col.description,
                "unit": col.unit,
                "missing_codes": list(col.missing_codes),
                "flag_codes": dict(col.flag_codes),
                "partial_places": dict(col.partial_places),
                "renamed_labels": dict(col.renamed_labels),
            }
            for col in c.columns
        ],  # fmt: skip
        "total_rows": c.total_rows or "none: every row is one group",
        "counts_and_rates": counts,
        "running_totals": running,
        "unreliable_values": c.unreliable_rule,
        "geography_basis": c.geography_basis,
        "rate_denominator": c.rate_denominator,
        "caveats": e.caveats,
        "open_questions": list(c.open_questions),
        "how_to_cite": e.citation,
        "licence": f"{e.licence.id} ({e.licence.terms_url})",
        "review": e.status,
        "last_fetched": _freshness(engine, ds),
        "provenance": engine.provenance(ds.id) if engine else None,
        **provenance_fields(ds, engine),
        "condition_labels": ds.condition_labels,
        "condition_labels_from": e.conditions.source,
        "condition_aliases": [a.model_dump() for a in e.conditions.aliases],
        "dimension_values": dimension_values(ds, engine),
        "presentation_rules": PRESENTATION_RULES,
    }
    if fmt == "json":
        return info
    lines = [
        f"{e.title} ({e.id})",
        f"Publisher: {e.publisher}",
        f"One row is: {e.grain}",
        f"Years: {info['years']}. Places: {', '.join(e.geography)}. Last fetched: {info['last_fetched'] or 'n/a'}.",
        "",
        "Conditions you can ask for with get_series:",
    ]
    for x in s.series:
        lines.append(
            f"- {x.label}: {x.measure}"
            + (f"; case status: {x.case_status}" if x.case_status else "")
            + (f"; case definition: {x.case_definition}" if x.case_definition else "")
        )
    if not s.series:
        lines.append("- none mapped yet (query_sql can still read the table)")
    if ds.condition_labels and s.generic_series:
        lines += [
            "",
            f"You can ask for any of these conditions, named exactly as {e.agency} publishes them:",
            "; ".join(ds.condition_labels),
            f"Where these names come from: {e.conditions.source}",
        ]
    lines += ["", "Values you can filter on (dimension_values):"]
    for name, dv in info["dimension_values"].items():
        shown = ", ".join(str(v) for v in dv["values"]) or "none listed"
        lines.append(f"- {name} ({dv['from']}): {shown}")
    lines += ["", "What each column means:"] + [f"- {col.name}: {col.description}" for col in c.columns]
    for col in c.columns:
        if col.flag_codes:
            lines.append(f"  {col.name} codes: " + "; ".join(f"'{k}' = {v}" for k, v in col.flag_codes))
        lines += [f"  '{k}' is {v}." for k, v in col.partial_places if k != "*"]
        lines += [f"  Every {col.name} place is {v}." for k, v in col.partial_places if k == "*"]
        lines += [f"  '{k}' continues '{v}' (renamed by the publisher)." for k, v in col.renamed_labels]
    lines += ["", f"Total rows: {info['total_rows']}", "Counts and rates: " + "; ".join(counts)]
    lines += [f"Running totals: {r}." for r in running]
    lines.append(f"Values marked unreliable or suppressed: {info['unreliable_values']}")
    lines += [f"Places are by: {c.geography_basis}"] if c.geography_basis else []
    lines += [f"Rate denominator: {c.rate_denominator}"] if c.rate_denominator else []
    if e.caveats:
        lines += ["", "What this data can't tell you:"] + [f"- {x}" for x in e.caveats]
    if c.open_questions:
        lines += ["", "Not yet confirmed with the publisher:"] + [f"- {x}" for x in c.open_questions]
    lines += ["", f"How to cite: {e.citation}", f"Licence: {info['licence']}", f"Review status: {e.status}"]
    lines.append(
        "Provenance: "
        + "; ".join(f"{k} {info[k]}" for k in PROVENANCE_COLUMNS if k not in ("licence", "citation"))
    )
    lines += ["", "When you present numbers from this source:"] + [f"- {r}" for r in PRESENTATION_RULES]
    if prov := info["provenance"]:
        lines += [
            f"Contract: {prov['contract_id']} version {prov['contract_version']}"
            + (
                f"; data product {prov['data_product']}, port {prov['output_port']}"
                if prov["data_product"]
                else ""
            ),
            f"Data from: {prov['data_from']}"
            + (
                f" (built with contract version {prov['release_contract_version']})"
                if prov["release_id"]
                else ""
            ),
            "",
            "Add it to a Claude project:",
            *prov["add_to_claude"],
        ]
    return "\n".join(lines) + "\n"


MAX_DIMENSION_VALUES = 300
NO_VALUES = "not available: no copy of the data is on this computer yet, and the contract lists none"


def _contract_values(ds: Dataset, column: str) -> tuple[list[Any], str]:
    """A column's documented values (enum) from the contract, else its examples, saying which."""
    for p in (ds.contract.raw.get("schema") or [{}])[0].get("properties") or []:
        if p.get("name") != column:
            continue
        enum = [x.get("value") if isinstance(x, Mapping) else x for x in p.get("enum") or []]
        if enum:
            return enum, "the contract's documented values"
        if p.get("examples"):
            return list(p["examples"]), "examples from the contract, not the full list"
    return [], NO_VALUES


def dimension_values(ds: Dataset, engine: Engine | None) -> dict[str, dict[str, Any]]:
    """The values each filterable dimension takes: the catalog's labels for the condition dimension, else the
    values in a copy already on this computer, else what the contract documents. Read from the data, never
    invented, and never downloaded: describing a source fetches nothing (Engine.loaded_values)."""
    out: dict[str, dict[str, Any]] = {}
    for d in ds.semantic.dimensions:
        values: list[Any]
        if d.name == ds.entry.conditions.dimension:
            values, src = ds.condition_labels, "the catalog's condition labels (the publisher's own)"
        elif engine is not None and (raw := engine.loaded_values(ds.id, d.column)) is not None:
            values = [v if isinstance(v, int | float | str) else str(v) for v in raw]
            src = "the loaded copy of the data"
        else:
            values, src = _contract_values(ds, d.column)
        out[d.name] = {
            "column": d.column,
            "values": values[:MAX_DIMENSION_VALUES],
            "from": src
            + (
                f"; first {MAX_DIMENSION_VALUES} of {len(values)}"
                if len(values) > MAX_DIMENSION_VALUES
                else ""
            ),
        }
    return out


def suggest_datasets(
    registry: Registry, question: str, *, engine: Engine | None = None, fmt: Fmt = "csv"
) -> Out:
    q = question.lower()
    want = _tokens(q)
    states = sorted(
        {
            p.name
            for p in geo.PLACES
            if p.level == "state" and re.search(rf"\b{re.escape(p.name.lower())}\b", q)
        }
    )
    levels = {lvl for w, lvl in _GEO_WORDS.items() if re.search(rf"\b{w}\b", q)}
    if states:
        levels.add("state")
    years = [int(y) for y in re.findall(r"\b(19[5-9]\d|20[0-4]\d)\b", q)]
    y0, y1 = (min(years), max(years)) if years else (None, None)
    place_words = {w for st in states for w in st.lower().split()}
    topic_words = want - set(_GEO_WORDS) - place_words - {str(y) for y in years}
    rows: list[list[Any]] = []
    for ds in registry:
        e = ds.entry
        hits = sorted(topic_words & _haystack(ds))
        why_not = []
        if topic_words and not hits:
            why_not.append("doesn't cover this condition")
        missing = sorted(lvl for lvl in levels if lvl not in e.geography)
        if missing:
            why_not.append(f"goes down to {', '.join(e.geography)} only, not {', '.join(missing)}")
        if years and not _years_overlap(ds, y0, y1):
            why_not.append(f"covers {_years(ds)} only")
        named = [f"{s.label} ({s.measure})" for s in ds.semantic.series[:4]]
        labels = [x for x in ds.condition_labels if _tokens(x) & topic_words][:6]
        supports = "; ".join(dict.fromkeys(labels + named))
        rows.append([e.id, "yes" if not why_not else "no", "; ".join(why_not) or "fits", supports, len(hits),
                     *provenance_fields(ds, engine).values()])  # fmt: skip
    rows.sort(key=lambda r: (r[1] != "yes", -int(r[4]), r[0]))
    notes = [
        f"Read as: condition words {sorted(topic_words)}, places {states or 'none'}, years {years or 'any'}.",
        "Public data shows what and where; your hospital's own data, kept in approved systems, answers why.",
        NO_PATIENT_DATA,
    ]
    if not any(r[1] == "yes" for r in rows):
        notes.append("Next step: " + next_step_for("unknown_condition"))
        notes += [
            f"Outside this registry: {x}" for x in _gbd_causes_named(registry, " ".join(sorted(topic_words)))
        ]
    return Result(
        ["source_id", "can_answer", "why", "what_it_supports", *PROVENANCE_COLUMNS],
        [r[:4] + r[5:] for r in rows],
        notes,
        extra={"presentation_rules": PRESENTATION_RULES},
    ).render(fmt)


# ------------------------------------------------------------------------------------------ series


def _match_places(
    engine: Engine, ds: Dataset, s: Series, locations: Sequence[str]
) -> tuple[dict[Any, str], list[str]]:
    """Map the dataset's own location labels to the places asked for: (raw label -> place name, unmatched).

    A label the publisher renamed between years (declared in the contract) matches the place its earlier
    label does, so both years come back under one name.
    """
    assert s.location_dimension is not None  # noqa: S101 - callers check; a series without places has none
    col = ds.contract.column(ds.semantic.dimension(s.location_dimension).column)
    raw_values = engine.distinct_values(ds.id, col.name)

    def key(text: str) -> str:
        p = geo.place(text)
        return p.name if p else norm_label(col.canonical_label(text))

    wanted = {loc: key(loc) for loc in locations}
    out: dict[Any, str] = {}
    for raw in raw_values:
        k = key(str(raw))
        if not locations or k in wanted.values():
            out[raw] = k if geo.place(str(raw)) else col.display_label(str(raw))
    got = {key(v) for v in out.values()}
    return out, [a for a, k in wanted.items() if k not in got]


class SeriesRow(NamedTuple):
    place: str
    year: Any
    value: Any
    status: str
    lower: Any
    upper: Any
    flag: Any = None
    # as_published; or, for a running total, year_end_ytd (the year's own figure) and
    # subsequent_year_restatement (next year's table's figure for it: unverified, never a final count)
    observation_type: str = "as_published"
    partial: str | None = None  # the contract's note when the place label covers only part of that place


def series_rows(
    engine: Engine,
    ds: Dataset,
    s: Series,
    locations: Sequence[str],
    year_start: int | None,
    year_end: int | None,
) -> tuple[list[SeriesRow], list[str], str]:
    if s.location_dimension is None and locations:
        raise QueryRefused(
            "no_place_breakdown",
            f"{s.label} from this source is one figure for "
            f"{s.place_coverage or 'the publisher areas combined, not by state'}, so nothing is shown for "
            f"{', '.join(locations)}. Ask again without "
            "places to see the combined figures, or use list_sources to find a source with state values.",
        )
    places: dict[Any, str] = {}
    unmatched: list[str] = []
    filters: dict[str, Any] = dict(s.filters)
    group_by = [s.time_dimension]
    if s.location_dimension:
        places, unmatched = _match_places(engine, ds, s, locations)
        if not places:
            return [], unmatched, ""
        filters[s.location_dimension] = list(places)
        group_by = [s.location_dimension, s.time_dimension]
    out = engine.query_metric(
        ds.id,
        s.metric,
        filters=filters,
        group_by=group_by,
        period={"dimension": s.time_dimension, "start": year_start, "end": year_end},
    )
    if out.get("truncated"):
        raise QueryRefused(
            "too_many_rows",
            f"{s.label} has more than {len(out['rows'])} rows for this question"
            + ("" if locations else " (every place, every period)")
            + ", so nothing is shown rather than a table cut off part way. Ask again for fewer places or a "
            "shorter span of years.",
        )
    rows: list[SeriesRow] = []
    at = {c: i for i, c in enumerate(out["columns"])}

    def get(r: Sequence[Any], name: str) -> Any:
        return r[at[name]] if name in at else None

    n_keys = 2 if s.location_dimension else 1
    loc_col = (
        ds.contract.column(ds.semantic.dimension(s.location_dimension).column)
        if s.location_dimension
        else None
    )
    running = "period_status" in at
    # Running totals come back keyed by the label key, other metrics by the raw label: map both to the place.
    by_key = {loc_col.canonical_label(str(k)): v for k, v in places.items()} if loc_col else {}
    for r in out["rows"]:
        label, partial = s.place_coverage or "as published", None
        if loc_col is not None:
            raw = str(r[0])
            label = places.get(r[0]) or by_key.get(loc_col.canonical_label(raw)) or raw
            partial = loc_col.partial_note(raw) if geo.place(label) else None
        year = r[n_keys - 1]
        # Every row says how final it is, so a running total part way through a year never reads as final.
        status = str(get(r, "period_status") or "") or (
            "the publisher marks no provisional or final status for this row"
        )
        if get(r, "suppressed_rows"):
            n_empty = int(get(r, "suppressed_rows"))
            status = (
                f"no total: {n_empty} value{'s' if n_empty != 1 else ''} suppressed or missing in the source"
            )
        rows.append(
            SeriesRow(label, year, r[n_keys], status, get(r, "lower"), get(r, "upper"),
                      get(r, "flag"), "year_end_ytd" if running else "as_published", partial)
        )  # fmt: skip
        r_status = str(get(r, "restated_status") or "")
        if r_status.startswith("unverified_restatement"):
            revised = (
                f"{ds.entry.agency}'s revised provisional figure, from the {int(year) + 1} table "
                f"({r_status}); kept apart from the year-end figure, never merged with it"
            )
            rows.append(
                SeriesRow(label, year, get(r, "restated"), revised, None, None, get(r, "restated_flag"),
                          "subsequent_year_restatement", partial)
            )  # fmt: skip
    return rows, unmatched, str(out["method"])


def provenance_note(ds: Dataset, m: Any) -> str:
    """One line with everything hard rule 8 asks of a result, for one source: where the numbers come from, when
    they were fetched, the raw files' sha256, the version, the contract status, the licence and the citation."""
    hashes = list(m.raw_sha256 or [])
    sha = (
        "raw file sha256 "
        + ", ".join(hashes[:2])
        + (f" and {len(hashes) - 2} more (get_provenance lists every file)" if len(hashes) > 2 else "")
        if hashes
        else "read live, not stored (no stored raw file)"
        if m.origin == "publisher"
        else "raw file sha256 " + _no_hash(m)
    )
    release = f"; hub release {m.release_ref} ({m.release_id})" if getattr(m, "release_id", None) else ""
    return (
        f"Provenance for {ds.id}: {ds.entry.source.landing_url}; retrieved_at {str(m.retrieved_at)[:19]}; {sha}; "
        f"snapshot {m.snapshot_id}; contract version {m.contract_version} (contract_status: {m.catalog_status})"
        f"{release}; licence {ds.entry.licence.id} ({ds.entry.licence.terms_url}); cite as: {ds.entry.citation}"
    )


def _dataset_notes(ds: Dataset, m: Any, method: str, got: Sequence[SeriesRow]) -> list[str]:
    """The lines every answer from a dataset carries: method, provenance, caveats, review state, flags."""
    notes = [f"{ds.id}: {method}", provenance_note(ds, m)]
    notes += [f"{ds.id}: {c}" for c in ds.entry.caveats[:3]]
    if ds.entry.status != "reviewed":
        notes.append(f"{ds.id}: this source's contract is still a draft; no maintainer has reviewed it yet.")
    if m.partial:
        notes.append(f"{ds.id}: the local copy is a partial sample, not the full dataset.")
    if any(r.observation_type == "subsequent_year_restatement" for r in got):
        notes.append(
            f"{ds.id}: each year has two observations. year_end_ytd is the year's own running total at the last "
            f"week that has a number (provisional). subsequent_year_restatement is {ds.entry.agency}'s revised "
            "provisional figure for the same year, from the next year's table (unverified_restatement): the "
            "publisher doesn't say they count the same thing, so it is not a final disease count, and neither is "
            "final until a domain reviewer approves them as one."
        )
    if ds.contract.geography_basis:
        notes.append(f"{ds.id}: places are by {ds.contract.geography_basis}.")
    if ds.contract.rate_denominator:
        notes.append(f"{ds.id}: rate denominator: {ds.contract.rate_denominator}.")
    pairs = dict(p for col in ds.contract.columns for p in col.flag_codes)
    if pairs:
        notes.append(
            f"{ds.id}: the flag column gives the publisher's reason for an empty value: "
            + "; ".join(f"'{c}' = {t}" for c, t in pairs.items())
            + ". '-' means no cases were reported, which is different from data that are unavailable or not "
            "published."
        )
    return notes


@paged("get_series")
def get_series(
    registry: Registry,
    engine: Engine,
    *,
    condition: str,
    locations: Sequence[str] = (),
    year_start: int | None = None,
    year_end: int | None = None,
    measure: str | None = None,
    sex: str | None = None,
    source_id: str | None = None,
    fmt: Fmt = "csv",
) -> Result | Out:
    if sex not in (None, "", "both"):
        return message(
            "None of the loaded sources split this by sex yet, so only both-sexes values are available. "
            "Ask again without sex, or use list_sources to look for a source that does.",
            fmt,
            refused="no_sex_split",
        )
    matches = series_for(registry, condition, measure, source_id)
    if not matches:
        return unknown_condition(registry, condition, fmt, measure or "")
    columns = ["location", "year", "value", "lower", "upper", "observation_type", "period_status", "flag",
               "measure", "condition", "case_status", "case_definition", "source_id", "retrieved_at"]  # fmt: skip
    rows: list[list[Any]] = []
    notes: list[str] = []
    ran: list[tuple[Dataset, Series, list[str]]] = []
    failed = False
    for ds, s in matches:
        try:
            got, unmatched, method = series_rows(engine, ds, s, locations, year_start, year_end)
            m = engine.manifest(ds.id)
        except DeadlineExceeded:
            raise  # out of time: no partial answer, whose pages could not be matched up later
        except Exception as e:  # noqa: BLE001 - one source failing must not hide the others' rows
            notes.append(f"{ds.id}: {plain(e)}")
            failed = True
            continue
        ran.append((ds, s, unmatched))
        for r in got:
            rows.append(
                [r.place, r.year, r.value, r.lower, r.upper, r.observation_type, r.status, r.flag, s.measure, s.label, s.case_status or "", s.case_definition or "", ds.id, m.retrieved_at[:19]]
            )  # fmt: skip
        if unmatched:
            notes.append(f"{ds.id} has no rows for: {', '.join(unmatched)}.")
        for place, note in sorted({(r.place, r.partial) for r in got if r.partial}):
            notes.append(f"{ds.id}: '{place}' here is {note}.")
        notes += _dataset_notes(ds, m, method, got)
    if not rows and ran and not failed:
        return matched_nothing(engine, ran, condition, locations, year_start, year_end, fmt)
    if len({s.measure for _, s in matches}) > 1:
        notes.insert(
            0, "Rows mix measures (see the measure column); never compare or add them across measures."
        )
    labels = sorted({s.label for _, s in matches})
    conditions = {spelling_key(x) for x in labels}
    if len(conditions) > 1:
        notes.insert(
            0,
            f"Rows cover {len(conditions)} different condition labels (see the condition column): they are "
            "different things, so never add them together.",
        )
    notes[0:0] = spelling_notes(labels)
    notes.append(
        "lower and upper are the publisher's own uncertainty bounds, shown only where published; observed counts "
        "have none. An empty value is never a zero: where the publisher gives a reason, it is in the flag column."
    )
    return Result(columns, rows, notes)


def matched_nothing(
    engine: Engine,
    ran: Sequence[tuple[Dataset, Series, list[str]]],
    condition: str,
    locations: Sequence[str],
    year_start: int | None,
    year_end: int | None,
    fmt: Fmt,
) -> Out:
    """A condition that is served, filtered by places or years that match no row: decline with the values that
    exist (available), the state that contains a county or city asked for (nearest_served), and the publisher's
    own page when the years asked fall outside this copy (served_elsewhere)."""
    available: dict[str, str] = {}
    nearest: list[str] = []
    elsewhere: list[str] = []
    for ds, s, unmatched in ran:
        places: list[str] = []
        partial: set[str] = set()
        if s.location_dimension:
            by_raw = _match_places(engine, ds, s, [])[0]
            places = sorted(set(by_raw.values()))
            col = ds.contract.column(ds.semantic.dimension(s.location_dimension).column)
            # A state row that covers only part of the state (NNDSS New York leaves out New York City) may not
            # hold the place asked for, so it is never offered as the nearest thing served.
            partial = {name for raw, name in by_raw.items() if col.partial_note(str(raw))}
            available[f"{ds.id} places"] = ", ".join(places[:80]) + (" ..." if len(places) > 80 else "")
        available[f"{ds.id} years"] = _years(ds)
        for loc in unmatched:
            st = geo.containing_state(loc)
            if st is not None and st.name in places and st.name not in partial:
                nearest.append(f"{st.name}: {ds.entry.title} reports by state, not by county or city")
        if not _years_overlap(ds, year_start, year_end):
            elsewhere.append(
                f"{ds.entry.publisher}: {ds.entry.source.landing_url} (this copy covers {_years(ds)} only)"
            )
    asked = ", ".join([*(f"'{x}'" for x in locations), *(str(y) for y in (year_start, year_end) if y)])
    return message(
        f"'{condition}' is served here, but no row matches {asked or 'what was asked'}.",
        fmt,
        refused="filter_matched_nothing",
        nearest_served=list(dict.fromkeys(nearest)),
        served_elsewhere=list(dict.fromkeys(elsewhere)),
        available=available,
    )


def gbd_call(cause: str, pathogen: str | None, places: list[str], y0: int | None, y1: int | None) -> str:
    """The IHME GBD connector call that returns the modelled counterpart (parameter names as that server documents)."""
    locs = "[" + ", ".join(repr(p) for p in places) + "]"
    end = min(y1 or GBD_LAST_YEAR, GBD_LAST_YEAR)
    if pathogen:
        return (
            f"get_etiology(pathogen_name={pathogen!r}, cause_name={cause!r}, locations={locs}, "
            f"year={end}, fmt='csv', language='en')"
        )
    start = max(min(y0 or 1990, end), 1990)
    return (
        f"get_multi_location_trend(cause_name={cause!r}, locations={locs}, start_year={start}, "
        f"end_year={end}, measure='incidence', fmt='csv', language='en')"
    )


@paged("get_aligned_with_gbd")
def get_aligned_with_gbd(
    registry: Registry,
    engine: Engine,
    *,
    condition: str,
    locations: Sequence[str] = (),
    year_start: int | None = None,
    year_end: int | None = None,
    fmt: Fmt = "csv",
) -> Result | Out:
    matches = series_for(registry, condition)
    if not matches:
        return unknown_condition(registry, condition, fmt)
    columns = ["gbd_location_name", "fips", "location_as_published", "year", "condition", "observed_value",
               "observation_type", "period_status", "flag", "measure", "case_status", "source_id", "retrieved_at",
               "gbd_cause", "gbd_pathogen", "suggested_gbd_call"]  # fmt: skip
    rows: list[list[Any]] = []
    notes = [
        "This server never calls GBD and never judges GBD against local data: run the suggested call with the "
        "IHME GBD connector and compare them yourself.",
        "GBD values are modelled, with 95% uncertainty intervals; observed counts have none. GBD's interval is not "
        "a test of observed surveillance data: before reading anything into a difference, establish how the two "
        "differ in case definition, ascertainment, geography and uncertainty model.",
        f"GBD 2023 ends in {GBD_LAST_YEAR}; later observed years have no GBD match.",
        "Don't compare age-standardised GBD rates with crude or US 2000-standardised local rates.",
    ]
    for ds, s in matches:
        xw = next((g for g in ds.entry.gbd.causes if g.condition == s.condition), None)
        if xw is None:
            notes.append(f"{ds.id}: '{s.label}' has no GBD cause mapping in its catalog entry yet.")
            continue
        try:
            got, unmatched, method = series_rows(engine, ds, s, locations, year_start, year_end)
            m = engine.manifest(ds.id)
        except DeadlineExceeded:
            raise  # out of time: no partial answer, whose pages could not be matched up later
        except Exception as e:  # noqa: BLE001 - one source failing must not hide the others' rows
            notes.append(f"{ds.id}: {plain(e)}")
            continue
        # Only a whole US state or the nation has a GBD location; a partial area, the publisher's pooled sites, or
        # places whose basis (facility or residence) is unresolved are left unkeyed with the reason.
        blocked = ds.contract.geography_basis_unresolved
        keyed = {
            r.place
            for r in got
            if r.partial is None and s.location_dimension and geo.place(r.place) and not blocked
        }
        call = gbd_call(xw.cause, xw.pathogen, sorted(keyed), year_start, year_end) if keyed else ""
        for r in got:
            p = geo.place(r.place) if r.place in keyed else None
            rows.append(
                [p.name if p else "", p.fips if p else "", r.place, r.year, s.label, r.value, r.observation_type,
                 r.status, r.flag, s.measure, s.case_status or "", ds.id, m.retrieved_at[:19], xw.cause,
                 xw.pathogen or "", call if p else ""]
            )  # fmt: skip
        if got and blocked:
            notes.append(
                f"{ds.id}: no row is keyed to a GBD location, because it isn't settled whether places here are by "
                "facility location or patient residence (GBD is by residence). Comparison is blocked until a "
                "maintainer resolves it."
            )
        elif got and not s.location_dimension:
            notes.append(
                f"{ds.id}: {s.label} is one figure for {s.place_coverage or 'the publisher areas combined'}, so it "
                "has no GBD location and no suggested call."
            )
        for place, note in sorted({(r.place, r.partial) for r in got if r.partial}):
            notes.append(f"{ds.id}: '{place}' is {note}, so it is not keyed to a GBD location.")
        if s.location_dimension and not blocked:
            for place in sorted({r.place for r in got if r.partial is None and r.place not in keyed}):
                notes.append(f"{ds.id}: '{place}' has no GBD location name here, so it is not keyed.")
        if xw.pathogen:
            notes.append(
                f"{s.label}: GBD models {xw.pathogen} as a share of {xw.cause}; pathogen shares don't add up to "
                "the cause total."
            )
        if unmatched:
            notes.append(f"{ds.id} has no rows for: {', '.join(unmatched)}.")
        notes += _dataset_notes(ds, m, method, got)
    return Result(columns, rows, notes)


def get_rate(
    registry: Registry,
    engine: Engine,
    *,
    condition: str,
    locations: Sequence[str] = (),
    year: int,
    page: int | None = None,
    pin: str | None = None,
    fmt: Fmt = "csv",
) -> Out:
    """Crude rate per 100,000 with an exact Poisson interval: see analysis.get_rate."""
    from health_data_registry import analysis

    return analysis.get_rate(
        registry, engine, condition=condition, locations=locations, year=year, page=page, pin=pin, fmt=fmt
    )


# ------------------------------------------------------------------------------------------ provenance


@paged("get_provenance")
def get_provenance(
    registry: Registry, engine: Engine, *, source_id: str, year: int | None = None, fmt: Fmt = "csv"
) -> Result | Out:
    try:
        ds = registry.get(source_id)
        m = engine.manifest(source_id)
    except (KeyError, QueryRefused) as e:
        return refusal(e, fmt)
    rows: list[list[Any]] = [
        [ds.id, url, m.retrieved_at, sha, m.snapshot_id, m.contract_version]
        for url, sha in zip(m.source_urls, m.raw_sha256, strict=True)
    ]
    defs = [f"{s.label}: {s.case_definition}" for s in ds.semantic.series if s.case_definition]
    notes = [
        f"Case definitions: {'; '.join(defs)}" if defs else "Case definitions: see describe_dataset.",
        f"Cite as: {ds.entry.citation}",
        "Each content hash identifies the exact bytes fetched; the same hash means the same data.",
    ]
    prov = engine.provenance(source_id)
    notes.append(
        f"Data from: {prov['data_from']}. Contract {prov['contract_id']} version {prov['contract_version']}"
        + (
            f"; data product {prov['data_product']}, port {prov['output_port']}"
            if prov["data_product"]
            else ""
        )
        + "."
    )
    notes.append("Add it to a Claude project: " + " ".join(prov["add_to_claude"]))
    if prov["release_id"]:
        notes.append(
            f"Hub release {prov['release_id']} (run {prov['run_date']}, contract version "
            f"{prov['release_contract_version']}). Release file fingerprints (sha256): "
            + "; ".join(f"{k} {v}" for k, v in sorted(prov["release_files"].items()))
        )
    if year is not None:
        notes.insert(0, f"Every row for {year} in this source comes from the fetches listed.")
    cols = ["source_id", "url", "retrieved_at", "content_sha256", "snapshot_id", "contract_version"]
    return Result(cols, rows, notes)


GEOGRAPHY_TRAPS = [
    "Out-Wayne County reports exclude the City of Detroit, which has its own health department.",
    "NNDSS counts cases by state of residence; some CDC tracking pages count by the state of the facility.",
    "C. auris clinical cases and screening (colonisation) cases are different series; never add them.",
    "C. auris numbers differ by source: NNDSS (cdc_nndss_weekly) counts cases by the week they were reported; "
    "CDC's C. auris tracking page counts cases by specimen collection date; the AR Lab Network counts isolates, "
    "not cases. Say which one a number is from, and don't compare them as if they were the same count.",
]


def list_caveats(
    registry: Registry, *, source_id: str | None = None, condition: str | None = None, fmt: Fmt = "csv"
) -> Out:
    rows: list[list[Any]] = []
    for ds in registry:
        if source_id and ds.id != source_id:
            continue
        if condition and not series_for(registry, condition, None, ds.id):
            continue
        rows += [[ds.id, "caveat", c] for c in ds.entry.caveats]
        rows += [[ds.id, "not yet confirmed", q] for q in ds.contract.open_questions]
        rows.append([ds.id, "unreliable values", ds.contract.unreliable_rule])
        if ds.entry.status != "reviewed":
            rows.append([ds.id, "review", "Contract is a draft; no maintainer has reviewed it yet."])
    rows += [["all", "geography", t] for t in GEOGRAPHY_TRAPS]
    return Result(["source_id", "kind", "caveat"], rows).render(fmt)


def find_prior_work(
    registry: Registry,
    *,
    condition: str,
    geography: str | None = None,
    years: str | None = None,
    dataset: str | None = None,
    fmt: Fmt = "csv",
) -> Out:
    where = f" in {geography}" if geography else ""
    return message(
        "The index of conference abstracts, case reports and health department reports isn't built yet, so "
        f"nothing is shown rather than a partial list. For now, search PubMed (and OpenEvidence if you use it) "
        f"for '{condition}'{where}, and check IDWeek abstracts in the Open Forum Infectious Diseases supplements.",
        fmt,
        refused="index_not_built",
    )


def unlisted_condition_labels(registry: Registry, engine: Engine) -> dict[str, list[str] | str | None]:
    """For each source with a condition dimension: the labels its loaded copy holds that its catalog entry doesn't
    list (validate fails on any), None when there is no copy to check, or, when the copy can't be read (a corrupt
    or half-written snapshot), why in plain words; one unreadable source never stops the others being checked."""
    out: dict[str, list[str] | str | None] = {}
    for ds in registry:
        dim = ds.entry.conditions.dimension
        if dim is None:
            continue
        try:
            values = engine.distinct_values(ds.id, ds.semantic.dimension(dim).column)
        except QueryRefused:
            out[ds.id] = None
            continue
        except (duckdb.Error, OSError) as e:
            out[ds.id] = (
                f"its local snapshot could not be read ({type(e).__name__}); refresh it and run validate again"
            )
            continue
        out[ds.id] = sorted({str(v) for v in values} - set(ds.condition_labels))
    return out


def query_sql(
    engine: Engine, sql: str, *, page: int | None = None, pin: str | None = None, fmt: Fmt = "csv"
) -> Out:
    return respond(
        "query_sql", {"sql": sql, "fmt": fmt}, engine, lambda eng: _query_sql(eng, sql, fmt), page=page, pin=pin,
        fmt=fmt,
    )  # fmt: skip


def _query_sql(engine: Engine, sql: str, fmt: Fmt) -> Result | Out:
    from health_data_registry.sql_guard import SqlRefused

    try:
        out = engine.query_sql(sql)
    except (SqlRefused, QueryRefused) as e:
        return message(
            f"That query can't run: {e} The other tools (get_series, describe_dataset) can usually answer the "
            "same question.",
            fmt,
            refused=getattr(e, "code", "refused"),
            next_step="Use get_series, get_rate or describe_dataset, which answer most questions without SQL.",
        )
    sources = [out["source"]] if out.get("source") else list(out.get("sources") or [])
    notes = [f"SQL run: {out['sql']}", f"Method: {out['method']}"]
    for src in sources:  # every table the query read, never only the first
        ds = engine.registry.get(src["dataset"])
        notes.append(provenance_note(ds, engine.manifest(src["dataset"])))
    return Result(out["columns"], out["rows"], notes + list(out.get("caveats", [])))
