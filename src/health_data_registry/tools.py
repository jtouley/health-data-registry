"""The read-only tools, as plain functions (server.py exposes them over MCP).

The spec's one tool list: list_sources, search_datasets, describe_dataset, suggest_datasets, get_series,
get_aligned_with_gbd, get_rate, get_provenance, list_caveats, find_prior_work, and query_sql as a guarded
escape hatch. Results are CSV by default (as the GBD connector's are), followed by plain lines saying where
the numbers came from. Every refusal is written for a clinician: what happened, and what to try next.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from health_data_registry import geo
from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Dataset, Registry, Series

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

    def render(self, fmt: Fmt = "csv") -> Out:
        self.notes = list(dict.fromkeys(self.notes))  # one dataset with several series repeats its notes
        if fmt == "json":
            return {"columns": self.columns, "rows": self.rows, "notes": self.notes}
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(self.columns)
        w.writerows(["" if v is None else v for v in r] for r in self.rows)
        text = buf.getvalue()
        if self.notes:
            text += "\n" + "\n".join(f"# {n}" for n in self.notes) + "\n"
        return text


def message(text: str, fmt: Fmt = "csv", **meta: Any) -> Out:
    return {"message": text, **meta} if fmt == "json" else text


def plain(e: Exception) -> str:
    """Any failure in plain words with a next step. Never a stack trace or a status code."""
    code = getattr(e, "code", None)
    if code == "no_snapshot":
        return (
            f"{e} If someone else set this up for you, ask them to run that step; or use the help_me_fix_it "
            "helper, which walks you through it."
        )
    if code == "unknown_dataset" or isinstance(e, KeyError):
        detail = e.args[0] if e.args else ""
        return f"{detail}. Use search_datasets to see what is available."
    if isinstance(e, QueryRefused):
        return str(e)
    return (
        "Something went wrong while reading the data, so no numbers are shown. Try again; if it keeps "
        "happening, use the help_me_fix_it helper and mention this question."
    )


def refusal(e: Exception, fmt: Fmt = "csv") -> Out:
    return message(plain(e), fmt, refused=getattr(e, "code", "error"))


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


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


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
    want = _norm(condition)
    found = []
    for ds in registry:
        if source_id and ds.id != source_id:
            continue
        for s in ds.semantic.series:
            s_kind = "weekly" if s.week_dimension else "age" if s.age_dimension else "plain"
            if s_kind != kind:
                continue
            names = {_norm(s.condition), _norm(s.label)}
            hit = want and (want in names or any(re.search(rf"\b{re.escape(want)}\b", n) for n in names))
            if hit and (measure is None or s.measure == measure):
                found.append((ds, s))
    return found


def _known_conditions(registry: Registry) -> str:
    labels = sorted({s.label for ds in registry for s in ds.semantic.series})
    return ", ".join(labels) if labels else "none yet"


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
             _freshness(engine, ds), e.licence.id, e.status, ds.contract.version,
             engine.provenance(ds.id)["data_from"] if engine else ""]
        )  # fmt: skip
    notes = (
        ["review = draft means no maintainer has checked this source's contract yet."]
        if rows
        else ["No source matches; try without a condition or geography."]
    )
    cols = [
        "source_id",
        "title",
        "publisher",
        "years",
        "geography",
        "updated",
        "last_fetched",
        "licence",
        "review",
        "contract_version",
        "data_from",
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
        ranked.append((len(hits), e.id, [e.id, e.title, e.agency, _years(ds), " ".join(e.geography), reason]))
    ranked.sort(key=lambda t: (-t[0], t[1]))
    rows = [r for _, _, r in ranked[: max(1, min(limit, 20))]]
    notes = ["Next: describe_dataset with a source_id to see what it can and can't answer."]
    if not rows:
        notes = ["No dataset matched. Try a broader topic, or suggest_datasets with your full question."]
    return Result(["source_id", "title", "agency", "years", "geography", "why_it_fits"], rows, notes).render(
        fmt
    )


def describe_dataset(
    registry: Registry, source_id: str, engine: Engine | None = None, *, fmt: Fmt = "csv"
) -> Out:
    try:
        ds = registry.get(source_id)
    except KeyError as err:
        return refusal(err, fmt)
    e, c, s = ds.entry, ds.contract, ds.semantic
    counts = [
        f"{m.name}: {m.kind}" + (" (added up)" if m.kind == "count" else " (never added up)")
        for m in s.measures
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
            }
            for col in c.columns
        ],  # fmt: skip
        "total_rows": c.total_rows or "none: every row is one group",
        "counts_and_rates": counts,
        "caveats": e.caveats,
        "open_questions": list(c.open_questions),
        "how_to_cite": e.citation,
        "licence": f"{e.licence.id} ({e.licence.terms_url})",
        "review": e.status,
        "last_fetched": _freshness(engine, ds),
        "provenance": engine.provenance(ds.id) if engine else None,
        "hosting": e.tier,
        "private_store": _private_store_line(ds, engine),
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
    lines += ["", "What each column means:"] + [f"- {col.name}: {col.description}" for col in c.columns]
    lines += ["", f"Total rows: {info['total_rows']}", "Counts and rates: " + "; ".join(counts)]
    if e.caveats:
        lines += ["", "What this data can't tell you:"] + [f"- {x}" for x in e.caveats]
    if c.open_questions:
        lines += ["", "Not yet confirmed with the publisher:"] + [f"- {x}" for x in c.open_questions]
    lines += ["", f"How to cite: {e.citation}", f"Licence: {info['licence']}", f"Review status: {e.status}"]
    if info["private_store"]:
        lines.append(info["private_store"])
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


def _private_store_line(ds: Dataset, engine: Engine | None) -> str | None:
    """For a private-tier source: how many rows its private store holds, or the plain fallback note."""
    from health_data_registry import private
    from health_data_registry.store import lake_root

    if not private.is_private(ds):
        return None
    found = private.read_rows(engine.lake if engine is not None else lake_root(), ds)
    if found is None:
        return (
            "Private store: empty on this server (not built yet, or deleted), so answers give links to "
            f"{ds.entry.publisher}'s own pages instead."
        )
    where = "this computer's private area" if found.origin == "local" else "its private hub dataset"
    return f"Private store: {len(found.rows)} rows, read from {where}; never published."


def suggest_datasets(registry: Registry, question: str, *, fmt: Fmt = "csv") -> Out:
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
        supports = "; ".join(f"{s.label} ({s.measure})" for s in ds.semantic.series[:4])
        rows.append([e.id, "yes" if not why_not else "no", "; ".join(why_not) or "fits", supports, len(hits)])
    rows.sort(key=lambda r: (r[1] != "yes", -int(r[4]), r[0]))
    notes = [
        f"Read as: condition words {sorted(topic_words)}, places {states or 'none'}, years {years or 'any'}.",
        "Public data shows what and where; your hospital's own data, kept in approved systems, answers why.",
        NO_PATIENT_DATA,
    ]
    return Result(
        ["source_id", "can_answer", "why", "what_it_supports"], [r[:4] for r in rows], notes
    ).render(fmt)


# ------------------------------------------------------------------------------------------ series


def _match_places(
    engine: Engine, ds: Dataset, s: Series, locations: Sequence[str]
) -> tuple[dict[Any, str], list[str]]:
    """Map the dataset's own location labels to the places asked for: (raw label -> place name, unmatched)."""
    if s.location_dimension is None:
        return {}, []
    col = ds.semantic.dimension(s.location_dimension).column
    raw_values = engine.distinct_values(ds.id, col)

    def key(text: str) -> str:
        p = geo.place(text)
        return p.name if p else _norm(text)

    wanted = {loc: key(loc) for loc in locations}
    out: dict[Any, str] = {}
    for raw in raw_values:
        k = key(str(raw))
        if not locations or k in wanted.values():
            p = geo.place(str(raw))
            out[raw] = p.name if p else str(raw)
    got = {key(v) for v in out.values()}
    return out, [a for a, k in wanted.items() if k not in got]


def series_rows(
    engine: Engine,
    ds: Dataset,
    s: Series,
    locations: Sequence[str],
    year_start: int | None,
    year_end: int | None,
) -> tuple[list[tuple[str, Any, Any, str, Any, Any]], list[str], str]:
    places, unmatched = _match_places(engine, ds, s, locations)
    if s.location_dimension and not places:
        return [], unmatched, ""
    filters: dict[str, Any] = dict(s.filters)
    group_by = [s.time_dimension]
    if s.location_dimension:
        filters[s.location_dimension] = list(places)
        group_by = [s.location_dimension, s.time_dimension]
    out = engine.query_metric(
        ds.id,
        s.metric,
        filters=filters,
        group_by=group_by,
        period={"dimension": s.time_dimension, "start": year_start, "end": year_end},
    )
    rows: list[tuple[str, Any, Any, str, Any, Any]] = []
    cols = list(out["columns"])
    at = {c: i for i, c in enumerate(cols)}
    n_keys = 2 if s.location_dimension else 1
    for r in out["rows"]:
        status = str(r[at["period_status"]]) if "period_status" in at else ""
        lo = r[at["lower"]] if "lower" in at else None
        hi = r[at["upper"]] if "upper" in at else None
        value = r[n_keys]
        if s.location_dimension:
            label = places.get(r[0]) or (lambda p: p.name if p else str(r[0]))(geo.place(str(r[0])))
            rows.append((label, r[1], value, status, lo, hi))
        else:
            rows.append(("as published", r[0], value, status, lo, hi))
    return rows, unmatched, str(out["method"])


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
) -> Out:
    if sex not in (None, "", "both"):
        return message(
            "None of the loaded sources split this by sex yet, so only both-sexes values are available. "
            "Ask again without sex, or use list_sources to look for a source that does.",
            fmt,
            refused="no_sex_split",
        )
    matches = series_for(registry, condition, measure, source_id)
    if not matches:
        return message(
            f"No loaded source reports '{condition}'"
            + (f" as {measure}" if measure else "")
            + f". Conditions available now: {_known_conditions(registry)}. "
            "Try one of those, or suggest_datasets with your full question.",
            fmt,
            refused="unknown_condition",
        )
    columns = ["location", "year", "value", "lower", "upper", "period_status", "measure", "condition",
               "case_status", "case_definition", "source_id", "retrieved_at"]  # fmt: skip
    rows: list[list[Any]] = []
    notes: list[str] = []
    for ds, s in matches:
        try:
            got, unmatched, method = series_rows(engine, ds, s, locations, year_start, year_end)
            m = engine.manifest(ds.id)
        except Exception as e:  # noqa: BLE001 - one source failing must not hide the others' rows
            notes.append(f"{ds.id}: {plain(e)}")
            continue
        for place, year, value, status, lo, hi in got:
            rows.append(
                [place, year, value, lo, hi, status, s.measure, s.label, s.case_status or "", s.case_definition or "",
                 ds.id, m.retrieved_at[:19]]
            )  # fmt: skip
        if unmatched:
            notes.append(f"{ds.id} has no rows for: {', '.join(unmatched)}.")
        notes.append(f"{ds.id}: {method} Source: {ds.entry.source.landing_url}")
        notes += [f"{ds.id}: {c}" for c in ds.entry.caveats[:3]]
        if ds.entry.status != "reviewed":
            notes.append(
                f"{ds.id}: this source's contract is still a draft; no maintainer has reviewed it yet."
            )
        if m.partial:
            notes.append(f"{ds.id}: the local copy is a partial sample, not the full dataset.")
    if len({s.measure for _, s in matches}) > 1:
        notes.insert(
            0, "Rows mix measures (see the measure column); never compare or add them across measures."
        )
    notes.append(
        "lower and upper are the publisher's own uncertainty bounds, shown only where published; observed counts "
        "have none. Empty values mean the publisher marked them missing or suppressed; they are not zeros."
    )
    return Result(columns, rows, notes).render(fmt)


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


def get_aligned_with_gbd(
    registry: Registry,
    engine: Engine,
    *,
    condition: str,
    locations: Sequence[str] = (),
    year_start: int | None = None,
    year_end: int | None = None,
    fmt: Fmt = "csv",
) -> Out:
    matches = series_for(registry, condition)
    if not matches:
        return message(
            f"No loaded source reports '{condition}'. Conditions available now: {_known_conditions(registry)}.",
            fmt,
            refused="unknown_condition",
        )
    columns = ["gbd_location_name", "fips", "year", "condition", "observed_value", "period_status", "measure",
               "case_status", "source_id", "retrieved_at", "gbd_cause", "gbd_pathogen", "suggested_gbd_call"]  # fmt: skip
    rows: list[list[Any]] = []
    notes = [
        "This server never calls GBD and never judges GBD against local data: run the suggested call with the "
        "IHME GBD connector and compare them yourself.",
        "GBD values are modelled, with 95% uncertainty intervals; observed counts have none. Treat a gap as real "
        "only if the observed value falls outside GBD's interval.",
        f"GBD 2023 ends in {GBD_LAST_YEAR}; later observed years have no GBD match.",
        "Don't compare age-standardised GBD rates with crude or US 2000-standardised local rates.",
    ]
    for ds, s in matches:
        xw = next((g for g in ds.entry.gbd.causes if g.condition == s.condition), None)
        if xw is None:
            notes.append(f"{ds.id}: '{s.label}' has no GBD cause mapping in its catalog entry yet.")
            continue
        try:
            got, unmatched, _ = series_rows(engine, ds, s, locations, year_start, year_end)
            m = engine.manifest(ds.id)
        except Exception as e:  # noqa: BLE001 - one source failing must not hide the others' rows
            notes.append(f"{ds.id}: {plain(e)}")
            continue
        call = gbd_call(xw.cause, xw.pathogen, sorted({p for p, *_ in got}), year_start, year_end)
        for place, year, value, status, _lo, _hi in got:
            p = geo.place(place)
            rows.append(
                [place, p.fips if p else "", year, s.label, value, status, s.measure, s.case_status or "",
                 ds.id, m.retrieved_at[:19], xw.cause, xw.pathogen or "", call]
            )  # fmt: skip
        if xw.pathogen:
            notes.append(
                f"{s.label}: GBD models {xw.pathogen} as a share of {xw.cause}; pathogen shares don't add up to "
                "the cause total."
            )
        if unmatched:
            notes.append(f"{ds.id} has no rows for: {', '.join(unmatched)}.")
    return Result(columns, rows, notes).render(fmt)


def get_rate(
    registry: Registry,
    engine: Engine,
    *,
    condition: str,
    locations: Sequence[str] = (),
    year: int,
    fmt: Fmt = "csv",
) -> Out:
    """Crude rate per 100,000 with an exact Poisson interval: see analysis.get_rate."""
    from health_data_registry import analysis

    return analysis.get_rate(registry, engine, condition=condition, locations=locations, year=year, fmt=fmt)


# ------------------------------------------------------------------------------------------ provenance


def get_provenance(
    registry: Registry, engine: Engine, *, source_id: str, year: int | None = None, fmt: Fmt = "csv"
) -> Out:
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
    return Result(cols, rows, notes).render(fmt)


GEOGRAPHY_TRAPS = [
    "Out-Wayne County reports exclude the City of Detroit, which has its own health department.",
    "NNDSS counts cases by state of residence; some CDC tracking pages count by the state of the facility.",
    "C. auris clinical cases and screening (colonisation) cases are different series; never add them.",
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
        if ds.entry.status != "reviewed":
            rows.append([ds.id, "review", "Contract is a draft; no maintainer has reviewed it yet."])
    rows += [["all", "geography", t] for t in GEOGRAPHY_TRAPS]
    return Result(["source_id", "kind", "caveat"], rows).render(fmt)


INDEX_SOURCES = ("croi_abstracts_index",)  # the research landscape index; more conferences join later
TEXT_SOURCE = {"croi_abstracts_index": "croi_abstracts_text"}
EXCERPT_CHARS = 280
MAX_PRIOR_WORK = 50
_YEARS = re.compile(r"^\s*(\d{4})?\s*(?:(-|–|to)\s*(\d{4})?)?\s*$")


def _not_built(condition: str, geography: str | None, fmt: Fmt) -> Out:
    where = f" in {geography}" if geography else ""
    return message(
        "The index of conference abstracts, case reports and health department reports isn't built yet, so "
        f"nothing is shown rather than a partial list. For now, search PubMed (and OpenEvidence if you use it) "
        f"for '{condition}'{where}, and check IDWeek abstracts in the Open Forum Infectious Diseases supplements.",
        fmt,
        refused="index_not_built",
    )


def year_range(years: str | None) -> tuple[int | None, int | None]:
    """'2023', '2020-2024', '2020-' or '-2019' as a closed or open range; ValueError otherwise."""
    if not years:
        return None, None
    m = _YEARS.match(years)
    if not m or not (m.group(1) or m.group(3)):
        raise ValueError(f"'{years}' isn't a year or a range of years like 2020-2024")
    lo = int(m.group(1)) if m.group(1) else None
    hi = int(m.group(3)) if m.group(3) else (None if m.group(2) else lo)
    return lo, hi


def _rule_tags(rules: dict[str, tuple[str, ...]], text: str) -> set[str]:
    return {t for t, pats in rules.items() if any(re.search(p, text, re.I) for p in pats)}


def _matches(want: str, tags: str, *texts: str) -> bool:
    """A row matches when its rule-based tags include the asked term's tag, or the words appear in it."""
    from health_data_registry.adapters import croi

    asked = _rule_tags({**croi.CONDITION_RULES, **croi.DESIGN_RULES, **croi.DATA_SOURCE_RULES,
                        **croi.GEOGRAPHY_RULES}, want)  # fmt: skip
    have = {t.strip() for t in (tags or "").split(";") if t.strip()}
    if asked & have:
        return True
    pattern = rf"\b{re.escape(want.strip())}\b"
    return any(t and re.search(pattern, t, re.I) for t in texts)


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """The opening of an abstract's body (its section labels dropped), cut at a word, never the whole text."""
    lines = [
        ln
        for ln in text.splitlines()
        if ln.strip().lower() not in {"background", "methods", "results", "conclusions", "conclusion"}
    ]
    body = " ".join(lines).strip()
    cap = min(limit, len(body) * 2 // 3)  # never the whole text, however short
    if cap < 40:
        return ""
    return body[:cap].rsplit(" ", 1)[0] + " …"


def find_prior_work(
    registry: Registry,
    *,
    condition: str,
    geography: str | None = None,
    years: str | None = None,
    dataset: str | None = None,
    fmt: Fmt = "csv",
    lake: Path | None = None,
) -> Out:
    from health_data_registry import private
    from health_data_registry.store import lake_root

    lake = lake if lake is not None else lake_root()
    try:
        lo, hi = year_range(years)
    except ValueError as e:
        return message(f"{e}. Give one year (2023) or a range (2020-2024).", fmt, refused="bad_years")
    rows: list[list[Any]] = []
    notes: list[str] = []
    held = 0
    for sid in INDEX_SOURCES:
        ds = registry.datasets.get(sid)
        if ds is None or not private.is_private(ds):
            continue
        index = private.read_rows(lake, ds)
        if index is None:
            continue
        held += len(index.rows)
        text_ds = registry.datasets.get(TEXT_SOURCE.get(sid, ""))
        texts: dict[tuple[str, int], str] = {}
        text_found = (
            private.read_rows(lake, text_ds) if text_ds is not None and private.is_private(text_ds) else None
        )
        for t in text_found.rows if text_found else []:
            texts[(str(t["abstract_number"]), int(t["year"]))] = str(t.get("abstract_text") or "")
        per_year: dict[int, int] = {}
        for r in index.rows:
            y = int(r["year"])
            per_year[y] = per_year.get(y, 0) + 1
            if (lo is not None and y < lo) or (hi is not None and y > hi):
                continue
            body = texts.get((str(r["abstract_number"]), y), "")
            if not _matches(condition, r.get("condition_tags") or "", r.get("title") or "", body):
                continue
            if geography and not _matches(
                geography, r.get("geography_tags") or "", r.get("title") or "", body
            ):
                continue
            if dataset and not _matches(dataset, r.get("data_source_tags") or "", r.get("title") or "", body):
                continue
            rows.append(
                ["CROI", y, r["abstract_number"], r["title"], r.get("session_type") or "", r["page_url"],
                 r.get("poster_url") or "", excerpt(body) if body else ""]
            )  # fmt: skip
        coverage = ", ".join(f"{y}: {n}" for y, n in sorted(per_year.items()))
        notes.append(
            "CROI abstracts: the full text and posters are on CROI's site; open each link to read them. "
            f"Abstracts held in this index by year: {coverage}."
        )
        if text_found is None:
            notes.append(
                "Excerpts aren't available here (the private text store is empty or was deleted), so only the links "
                "are shown."
            )
        notes += private.notes(ds)
        if text_found is not None and text_ds is not None:
            notes += private.notes(text_ds)
    if not held:
        return _not_built(condition, geography, fmt)
    rows.sort(key=lambda r: (-int(r[1]), str(r[2])))
    if len(rows) > MAX_PRIOR_WORK:
        notes.insert(
            0, f"Showing the {MAX_PRIOR_WORK} most recent of {len(rows)} matches; narrow the years or place."
        )
        rows = rows[:MAX_PRIOR_WORK]
    if not rows:
        notes.insert(0, f"No indexed CROI abstract matches '{condition}'" + (f" in {geography}" if geography else "")
                     + (f" for {years}" if years else "") + ". Search PubMed too: the index covers CROI only.")  # fmt: skip
    else:
        notes.insert(0, "Matches come from fixed word rules over each abstract's title and text, not from reading "
                     "it; check each one. The index covers CROI only, so search PubMed too.")  # fmt: skip
    cols = [
        "conference",
        "year",
        "abstract_number",
        "title",
        "session_type",
        "link",
        "poster_link",
        "excerpt",
    ]
    return Result(cols, rows, notes).render(fmt)


def query_sql(engine: Engine, sql: str, *, fmt: Fmt = "csv") -> Out:
    from health_data_registry.sql_guard import SqlRefused

    try:
        out = engine.query_sql(sql)
    except (SqlRefused, QueryRefused) as e:
        return message(
            f"That query can't run: {e} The other tools (get_series, describe_dataset) can usually answer the "
            "same question.",
            fmt,
            refused=getattr(e, "code", "refused"),
        )
    src = out.get("source") or {}
    notes = [f"SQL run: {out['sql']}", f"Method: {out['method']}"]
    if src:
        notes.append(f"Source: {src['title']}, fetched {src['retrieved_at'][:19]} ({src['landing_url']})")
    return Result(out["columns"], out["rows"], notes + list(out.get("caveats", []))).render(fmt)
