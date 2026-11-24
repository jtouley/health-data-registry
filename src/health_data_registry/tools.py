"""The five read-only tools, as plain functions over the registry and the engine (server.py wraps them).

find_datasets and suggest_datasets only read the reviewed catalog; they never guess from a dataset's
name. A dataset is said to fit only for reasons the catalog states: its conditions, places, years and shape.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from health_data_registry.query import Engine, FilterValue, QueryRefused
from health_data_registry.registry import Dataset, Registry

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
    ]
)

US_STATES = frozenset(
    s.lower()
    for s in [
        "Alabama",
        "Alaska",
        "Arizona",
        "Arkansas",
        "California",
        "Colorado",
        "Connecticut",
        "Delaware",
        "Florida",
        "Georgia",
        "Hawaii",
        "Idaho",
        "Illinois",
        "Indiana",
        "Iowa",
        "Kansas",
        "Kentucky",
        "Louisiana",
        "Maine",
        "Maryland",
        "Massachusetts",
        "Michigan",
        "Minnesota",
        "Mississippi",
        "Missouri",
        "Montana",
        "Nebraska",
        "Nevada",
        "Ohio",
        "Oklahoma",
        "Oregon",
        "Pennsylvania",
        "Tennessee",
        "Texas",
        "Utah",
        "Vermont",
        "Virginia",
        "Washington",
        "Wisconsin",
        "Wyoming",
    ]
) | {
    "new york",
    "new jersey",
    "new mexico",
    "new hampshire",
    "north carolina",
    "south carolina",
    "north dakota",
    "south dakota",
    "rhode island",
    "west virginia",
    "district of columbia",
}

_GEO_WORDS = {
    "county": "county",
    "counties": "county",
    "city": "city",
    "zip": "zip",
    "state": "state",
    "states": "state",
    "national": "nation",
    "nationally": "nation",
    "country": "country",
    "countries": "country",
    "global": "country",
    "worldwide": "country",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9.\-]*", text.lower())
    out = {w.strip(".-") for w in words if w not in _STOP and len(w) > 1}
    return {w for w in out if w}


def _haystack(ds: Dataset) -> set[str]:
    e = ds.entry
    parts = [e.title, e.grain, " ".join(e.research_areas), e.agency, e.publisher]
    parts += [f"{c.condition.replace('_', ' ')} {c.cause} {c.pathogen or ''}" for c in e.gbd.causes]
    parts += [f"{m.name.replace('_', ' ')} {m.description}" for m in ds.semantic.metrics]
    parts += [f"{d.name.replace('_', ' ')} {d.description}" for d in ds.semantic.dimensions]
    return _tokens(" ".join(parts))


def _years_overlap(ds: Dataset, start: int | None, end: int | None) -> bool:
    y = ds.entry.years
    hi = y.end if y.end is not None else 9999
    return not ((start is not None and start > hi) or (end is not None and end < y.start))


def _summary(ds: Dataset) -> dict[str, Any]:
    e = ds.entry
    return {
        "id": e.id,
        "title": e.title,
        "agency": e.agency,
        "years": f"{e.years.start}-{e.years.end or 'present'}",
        "geography": e.geography,
        "grain": e.grain,
        "shape": e.shape,
        "licence": e.licence.id,
        "review_status": e.status,
    }


def find_datasets(
    registry: Registry,
    *,
    topic: str = "",
    research_area: str | None = None,
    geography: str | None = None,
    year_start: int | None = None,
    year_end: int | None = None,
    shape: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    want = _tokens(topic)
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for ds in registry:
        e = ds.entry
        if research_area and research_area not in e.research_areas:
            continue
        if geography and geography not in e.geography:
            continue
        if shape and shape != e.shape:
            continue
        if not _years_overlap(ds, year_start, year_end):
            continue
        hits = sorted(want & _haystack(ds))
        if want and not hits:
            continue
        reason = ("matches " + ", ".join(hits) + "; " if hits else "") + (
            f"{e.grain[0].lower() + e.grain[1:]} ({e.years.start}-{e.years.end or 'present'})"
        )
        ranked.append((len(hits), e.id, {**_summary(ds), "fit": reason}))
    ranked.sort(key=lambda t: (-t[0], t[1]))
    results = [r for _, _, r in ranked[: max(1, min(limit, 20))]]
    return {
        "datasets": results,
        "note": "No catalogued dataset matched; try a broader topic." if not results else "",
    }


def describe_dataset(registry: Registry, dataset_id: str, engine: Engine | None = None) -> dict[str, Any]:
    ds = registry.get(dataset_id)
    e, c, s = ds.entry, ds.contract, ds.semantic
    snapshot: dict[str, Any] | None = None
    if engine is not None:
        m = engine.manifests.get(dataset_id) if engine.connect() is not None else None
        if m is not None:
            snapshot = {"snapshot_id": m.snapshot_id, "retrieved_at": m.retrieved_at, "rows": m.row_count}
    return {
        **_summary(ds),
        "publisher": e.publisher,
        "one_row_is": e.grain,
        "columns": [
            {
                "name": col.name,
                "role": col.role,
                "type": col.logical_type or col.physical_type.lower(),
                "meaning": col.description,
                **({"unit": col.unit} if col.unit else {}),
                **({"missing_codes": list(col.missing_codes)} if col.missing_codes else {}),
            }
            for col in c.columns
        ],
        "total_rows": c.total_rows or "The publisher's table has no total rows; every row is one group.",
        "rates_vs_counts": [
            f"{m.name}: {m.kind}" + (" (summed)" if m.kind == "count" else " (never summed)")
            for m in s.measures
        ],
        "metrics": [
            {"name": m.name, "meaning": m.description, "aggregation": m.aggregation} for m in s.metrics
        ],
        "dimensions": [{"name": d.name, "type": d.type, "meaning": d.description} for d in s.dimensions],
        "gbd_crosswalk": [g.model_dump() for g in e.gbd.causes],
        "caveats": e.caveats,
        "open_questions": list(c.open_questions),
        "how_to_cite": e.citation,
        "terms": {
            "licence": e.licence.id,
            "url": e.licence.terms_url,
            "redistribution": e.licence.redistribution,
        },
        "codebook": e.codebook_url,
        "contract_version": c.version,
        "snapshot": snapshot,
    }


def suggest_datasets(registry: Registry, question: str) -> dict[str, Any]:
    q = question.lower()
    want = _tokens(q)
    states = sorted(s for s in US_STATES if re.search(rf"\b{re.escape(s)}\b", q))
    levels = {lvl for w, lvl in _GEO_WORDS.items() if re.search(rf"\b{w}\b", q)}
    if states:
        levels.add("state")
    years = [int(y) for y in re.findall(r"\b(19[5-9]\d|20[0-4]\d)\b", q)]
    y0, y1 = (min(years), max(years)) if years else (None, None)
    topic_words = want - set(_GEO_WORDS) - {s for st in states for s in st.split()} - {str(y) for y in years}
    can, cannot = [], []
    for ds in registry:
        e = ds.entry
        hits = sorted(topic_words & _haystack(ds))
        reasons = []
        if topic_words and not hits:
            reasons.append("it doesn't cover the condition or topic asked about")
        missing = sorted(lvl for lvl in levels if lvl not in e.geography)
        if missing:
            reasons.append(f"its finest geography is {', '.join(e.geography)}, not {', '.join(missing)}")
        if years and not _years_overlap(ds, y0, y1):
            reasons.append(f"it covers {e.years.start}-{e.years.end or 'present'} only")
        item = {
            "id": e.id,
            "title": e.title,
            "matches": hits,
            "supports": [m.description for m in ds.semantic.metrics][:4],
            "caveats": e.caveats[:3],
        }
        if reasons:
            cannot.append({**item, "why_not": "; ".join(reasons)})
        else:
            can.append(item)
    can.sort(key=lambda i: -len(i["matches"]))
    return {
        "question": question,
        "read_as": {
            "topic": sorted(topic_words),
            "places": states,
            "geography": sorted(levels),
            "years": years,
        },
        "can_answer": can,
        "cannot_answer": cannot,
        "boundary": "Public data shows what and where; your hospital's own data, kept in approved systems, "
        "answers why. Never paste patient information into the chat.",
    }


def query_metric(
    engine: Engine,
    dataset_id: str,
    metric: str,
    *,
    filters: Mapping[str, FilterValue] | None = None,
    group_by: Sequence[str] = (),
    period: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return engine.query_metric(dataset_id, metric, filters=filters, group_by=group_by, period=period)
    except QueryRefused as e:
        return {"refused": e.code, "message": str(e)}
    except KeyError as e:
        return {"refused": "unknown_dataset", "message": str(e.args[0])}


def query_sql(engine: Engine, sql: str) -> dict[str, Any]:
    from health_data_registry.sql_guard import SqlRefused

    try:
        return engine.query_sql(sql)
    except (SqlRefused, QueryRefused) as e:
        return {"refused": getattr(e, "code", "refused"), "message": str(e)}
