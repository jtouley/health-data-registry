"""A scripted reference client: turns tool responses into the reply a careful client would give.

It writes only what the tools returned: each row of a series or rate table, the provenance lines that came with
it, and a decline when nothing fits. It never adds, compares or rounds numbers. The smoke test scores its replies
with eval/scorer.py, so a server change that drops provenance, merges series or loses a flag fails the same rules
the behaviour eval applies to a real client.
"""

from __future__ import annotations

from collections.abc import Sequence

from eval import numbers as num
from eval.scorer import PROVENANCE_RE

SEPARATE_SERIES = (
    "Each condition below is its own series, reported as published and never added to another; each year's "
    "running total and next year's restatement are kept apart."
)


def table_rows(response: str) -> list[dict[str, str]]:
    """The CSV rows of one response, read the way the scorer reads them (eval/numbers.py, N3)."""
    out: list[dict[str, str]] = []
    for t in num.tables(response):
        header = [h.strip() for h in t.header]
        out += [dict(zip(header, (c.strip() for c in row), strict=False)) for row in t.rows]
    return out


def provenance_lines(responses: Sequence[str]) -> list[str]:
    """Every `Provenance for <id>: ...` line the responses carried, once each, as the reply must repeat them."""
    return list(dict.fromkeys(m.group(0) for r in responses for m in PROVENANCE_RE.finditer(r)))


def _series_sentence(r: dict[str, str]) -> str:
    where = f"{r['location']} in {r['year']}, {r['condition']}"
    if not r["value"]:
        return f"{where}: no number shown; the publisher's flag is '{r['flag'] or 'none'}'."
    if r["observation_type"] == "subsequent_year_restatement":
        return (
            f"{where}: {r['value']} cases in next year's table, an unverified restatement, not a final count."
        )
    status = "provisional" if "provisional" in r["period_status"] else "as published"
    return f"{where}: {r['value']} cases ({r['observation_type']}, {status})."


def _rate_sentence(r: dict[str, str]) -> str:
    where = f"{r['location']} in {r['year']}, {r['condition']}"
    if not r["rate_per_100k"]:
        return f"{where}: no rate shown ({r['flags'] or 'no reason given'})."
    return (
        f"{where}: {r['rate_per_100k']} per 100,000 (interval {r['lower_95']} to {r['upper_95']}), from "
        f"{r['cases']} cases over a Census population of {r['population']}; provisional."
    )


def answer(responses: Sequence[str]) -> str:
    """The reply built from the series and rate tables in `responses`, then their provenance lines."""
    lines = [SEPARATE_SERIES]
    for resp in responses:
        for r in table_rows(resp):
            if "observation_type" in r and "value" in r:
                lines.append(_series_sentence(r))
            elif "rate_per_100k" in r:
                lines.append(_rate_sentence(r))
    return "\n".join(lines + provenance_lines(responses)) + "\n"


def fitting_sources(suggest_response: str) -> list[str]:
    """The datasets suggest_datasets says can answer the question."""
    return [r["source_id"] for r in table_rows(suggest_response) if r.get("can_answer") == "yes"]


def candidate_sources(suggest_response: str) -> list[str]:
    """The datasets a client would try: those that fit, and those whose only open question is that they don't
    declare their places yet (no source declares them until #223), in suggest_datasets' order."""
    return [
        r["source_id"]
        for r in table_rows(suggest_response)
        if r.get("can_answer") == "yes"
        or (
            r.get("can_answer") == "unknown"
            and r.get("why", "").startswith("this source doesn't declare which places it covers yet")
            and ";" not in r.get("why", "")
        )
    ]


def out_of_scope_answer(condition: str, place: str, suggest_response: str, series_response: str) -> str:
    """Decline when no tool offered a source or a row; otherwise name what the tools offered, as a client would."""
    offered = fitting_sources(suggest_response)
    rows = table_rows(series_response)
    if not offered and not rows:
        return (
            f"No catalogued source covers {condition} for {place}, so there is no number to give. "
            "Public health department reports or the publisher's own pages may hold it."
        )
    named = ", ".join(sorted(set(offered) | {r.get("source_id", "") for r in rows} - {""}))
    return f"{named} may answer {condition} for {place}."
