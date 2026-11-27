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
from typing import Any, Literal, NamedTuple

from health_data_registry import geo
from health_data_registry.deadline import DeadlineExceeded
from health_data_registry.paging import paged, respond
from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Dataset, Registry, Series, UnknownDataset

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
        return f"{e} Run the Something's wrong helper, which walks you through turning it back on."
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
    """Map the dataset's own location labels to the places asked for: (raw label -> place name, unmatched).

    A label the publisher renamed between years (declared in the contract) matches the place its earlier
    label does, so both years come back under one name.
    """
    assert s.location_dimension is not None  # noqa: S101 - callers check; a series without places has none
    col = ds.contract.column(ds.semantic.dimension(s.location_dimension).column)
    raw_values = engine.distinct_values(ds.id, col.name)

    def key(text: str) -> str:
        p = geo.place(text)
        return p.name if p else _norm(col.canonical_label(text))

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
        return message(
            f"No loaded source reports '{condition}'"
            + (f" as {measure}" if measure else "")
            + f". Conditions available now: {_known_conditions(registry)}. "
            "Try one of those, or suggest_datasets with your full question.",
            fmt,
            refused="unknown_condition",
        )
    columns = ["location", "year", "value", "lower", "upper", "observation_type", "period_status", "flag",
               "measure", "condition", "case_status", "case_definition", "source_id", "retrieved_at"]  # fmt: skip
    rows: list[list[Any]] = []
    notes: list[str] = []
    for ds, s in matches:
        try:
            got, unmatched, method = series_rows(engine, ds, s, locations, year_start, year_end)
            m = engine.manifest(ds.id)
        except Exception as e:  # noqa: BLE001 - one source failing must not hide the others' rows
            notes.append(f"{ds.id}: {plain(e)}")
            continue
        for r in got:
            rows.append(
                [r.place, r.year, r.value, r.lower, r.upper, r.observation_type, r.status, r.flag, s.measure, s.label, s.case_status or "", s.case_definition or "", ds.id, m.retrieved_at[:19]]
            )  # fmt: skip
        if unmatched:
            notes.append(f"{ds.id} has no rows for: {', '.join(unmatched)}.")
        for place, note in sorted({(r.place, r.partial) for r in got if r.partial}):
            notes.append(f"{ds.id}: '{place}' here is {note}.")
        notes += _dataset_notes(ds, m, method, got)
    if len({s.measure for _, s in matches}) > 1:
        notes.insert(
            0, "Rows mix measures (see the measure column); never compare or add them across measures."
        )
    notes.append(
        "lower and upper are the publisher's own uncertainty bounds, shown only where published; observed counts "
        "have none. An empty value is never a zero: where the publisher gives a reason, it is in the flag column."
    )
    return Result(columns, rows, notes)


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
        return message(
            f"No loaded source reports '{condition}'. Conditions available now: {_known_conditions(registry)}.",
            fmt,
            refused="unknown_condition",
        )
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
        )
    sources = [out["source"]] if out.get("source") else list(out.get("sources") or [])
    notes = [f"SQL run: {out['sql']}", f"Method: {out['method']}"]
    for src in sources:  # every table the query read, never only the first
        ds = engine.registry.get(src["dataset"])
        notes.append(provenance_note(ds, engine.manifest(src["dataset"])))
    return Result(out["columns"], out["rows"], notes + list(out.get("caveats", [])))
