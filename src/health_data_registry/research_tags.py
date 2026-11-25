"""Our own rule-based tags for conference abstracts in the research index: fixed word rules, never a model.

The research index (CROI and the DOI-published conferences: IDWeek, SHEA Spring, APIC, ICID) tags every
abstract the same way, from its title and, where we hold it, its text. A tag is a hint for finding prior work,
not a fact about the study: a missing tag doesn't mean the topic is absent (ADR 0006: no model output is stored).

The rules live in adapters/extensions/tag_rules.py, the one copy two engines read: here in Python for the DOI
harvest, and as SQL in the shared staging projection for sources that name the ``rule_tags`` extension (CROI's
listing crawl).

The condition, design, data-source and geography rules started as the CROI adapter's (wip/croi-index), with
conditions for healthcare-associated infection and stewardship added for the infection-prevention meetings.
"""

from __future__ import annotations

import re

from health_data_registry.adapters.extensions.tag_rules import (  # noqa: F401 - the tables, re-exported
    _CASE_SENSITIVE_PLACES,
    CONDITION_RULES,
    DATA_SOURCE_RULES,
    DESIGN_RULES,
    GEOGRAPHY_RULES,
    RULE_SETS,
    pattern,
)

__all__ = [
    "CONDITION_RULES",
    "DATA_SOURCE_RULES",
    "DESIGN_RULES",
    "GEOGRAPHY_RULES",
    "RULE_SETS",
    "pattern",
    "tag_record",
    "tags",
]


def tags(
    rules: dict[str, tuple[str, ...]], text: str, *, case_sensitive: frozenset[str] = frozenset()
) -> str:
    """Every tag whose rule matches the text, sorted and joined with '; '."""
    hits = []
    for tag, patterns in rules.items():
        flags = 0 if tag in case_sensitive else re.I
        if any(re.search(p, text, flags) for p in patterns):
            hits.append(tag)
    return "; ".join(sorted(hits))


def tag_record(title: str, text: str | None) -> dict[str, str]:
    """The four tag columns of the research index for one abstract."""
    words = f"{title}\n{text or ''}"
    return {
        "condition_tags": tags(CONDITION_RULES, words),
        "design_tags": tags(DESIGN_RULES, words),
        "data_source_tags": tags(DATA_SOURCE_RULES, words),
        "geography_tags": tags(GEOGRAPHY_RULES, words, case_sensitive=_CASE_SENSITIVE_PLACES),
    }
