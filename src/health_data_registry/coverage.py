"""What each source covers: places, conditions and years, read only from what is declared about it.

One function, ``declared_coverage``, answers it for every tool, so routing never guesses coverage from words. A
source whose coverage isn't declared covers "unknown", and an unknown never counts as a fit.

Where it reads today, in order:

- places: the contract's ``conformedDimensions`` (conformed place keys as geo defines them: USA, US-<state FIPS>,
  US-<state FIPS><county FIPS>, or a declared part of one, e.g. US-26163/detroit). Nothing else, and never the
  catalog's ``level`` or ``country``: those say who publishes a source, not where its rows are (a federal network
  can cover a few counties in some states, and an international abstract list covers no country). A source with no
  conformed places covers "unknown" until its coverage is declared (#223).
- conditions: the catalog's condition labels and aliases, its GBD crosswalk, and the semantic model's series.
- years: the catalog's ``years``.

The seam: once the transform's ``dim_dataset_coverage`` mart carries each source's place, condition and time keys,
``declared_coverage`` reads them from the mart instead; its callers don't change.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from health_data_registry import geo
from health_data_registry.registry import Dataset

# Words that appear in condition labels but never name a condition on their own: a fit needs a word that does.
GENERIC_WORDS = frozenset(
    [
        "a",
        "acute",
        "ages",
        "all",
        "and",
        "associated",
        "case",
        "cases",
        "chronic",
        "clinical",
        "combined",
        "confirmed",
        "count",
        "counts",
        "daily",
        "death",
        "deaths",
        "department",
        "disease",
        "diseases",
        "emergency",
        "estimate",
        "estimates",
        "for",
        "health",
        "hospitalisations",
        "hospitalizations",
        "in",
        "incidence",
        "infection",
        "infectious",
        "infections",
        "invasive",
        "isolates",
        "lower",
        "net",
        "of",
        "other",
        "percent",
        "per",
        "population",
        "prevalence",
        "probable",
        "rate",
        "rates",
        "reported",
        "resistant",
        "screening",
        "share",
        "sites",
        "spp",
        "syndrome",
        "the",
        "total",
        "unspecified",
        "upper",
        "virus",
        "viral",
        "visits",
        "weekly",
        "year",
        "years",
        # Words a question uses around its condition that never name one ("How many cases ...", "Did it rise ...").
        "adults",
        "among",
        "children",
        "decline",
        "declining",
        "did",
        "fall",
        "give",
        "how",
        "im",
        "increase",
        "increased",
        "increasing",
        "many",
        "me",
        "men",
        "much",
        "number",
        "numbers",
        "rise",
        "risen",
        "rising",
        "rose",
        "show",
        "studying",
        "what",
        "women",
        # Population groups a question may name: who, never what condition.
        "asian",
        "black",
        "hispanic",
        "white",
    ]
)

# Clinical abbreviations a question may use for a condition, read as the condition's own words. Routing only: an
# abbreviation never changes which rows are returned or any number.
ABBREVIATIONS: Mapping[str, str] = {
    "tb": "tuberculosis",
    "hcv": "hepatitis c",
    "hbv": "hepatitis b",
    "flu": "influenza",
    "mrsa": "methicillin-resistant staphylococcus aureus",
    "cdi": "clostridioides difficile",
}


@dataclass(frozen=True)
class Coverage:
    """A source's declared coverage. `place_keys` is None when no place coverage is declared (unknown)."""

    place_keys: frozenset[str] | None
    place_words: str  # the declared places in plain words, for answers
    condition_words: frozenset[str]  # the specific words of the conditions it declares; empty means unknown
    year_start: int
    year_end: int | None

    def holds(self, key: str) -> bool | None:
        """Does the declared coverage contain the place `key`? None when nothing is declared.

        A city a publisher reports apart from its state (Detroit, New York City) is held only when the city itself
        is declared: a country or state declaration says which states a source reports, never which cities it
        reports apart from them, so a nationwide source is "unknown" for a city, never a fit (#111)."""
        if self.place_keys is None:
            return None
        if not any(geo.within(outer, key) for outer in self.place_keys):
            return False
        if geo.is_city(key) and not any(geo.is_city(o) and geo.within(o, key) for o in self.place_keys):
            return None
        return True

    def holds_years(self, start: int | None, end: int | None) -> bool:
        """Do the declared years overlap the years asked for (either end may be open)?"""
        last = self.year_end if self.year_end is not None else 9999
        return not ((start is not None and start > last) or (end is not None and end < self.year_start))

    @property
    def nationwide(self) -> bool:
        """Declares a whole country or more, so a question that names no place still fits it."""
        return geo.NATION_KEY in (self.place_keys or ())


def words(text: str) -> set[str]:
    """Lower-case words, hyphenated ones also split ("influenza-associated" gives "influenza" too). A single
    letter is kept with the word next to it ("Q fever" gives "q fever", "C. auris" gives "c auris", "Hepatitis B,
    acute" gives "hepatitis b")."""
    low = text.lower().replace(".", " ").replace("'", "")
    pairs = re.findall(r"(?<![a-z0-9])([b-hj-z])\s+([a-z][a-z\-]*)", low)  # "q fever"
    pairs += re.findall(r"([a-z][a-z\-]*)\s+([b-hj-z])(?![a-z0-9])", low)  # "hepatitis b"
    out: set[str] = {f"{a} {b}" for a, b in pairs if a not in GENERIC_WORDS and b not in GENERIC_WORDS}
    for w in re.findall(r"[a-z0-9][a-z0-9\-]*", low):
        out.add(w)
        out.update(p for p in w.split("-") if p)
    return {w for w in out if len(w) > 1}


def question_words(text: str) -> set[str]:
    """A question's words (words), with each clinical abbreviation it uses also read as the condition's words."""
    got = words(text)
    for short, full in ABBREVIATIONS.items():
        if short in got:
            got |= words(full)
    return got


def condition_hits(asked: set[str], declared: frozenset[str]) -> set[str]:
    """The condition words a question shares with a source. A word the question qualifies with a letter
    ("hepatitis c") is no hit on a source that declares it only with other letters ("hepatitis b")."""
    hits = asked & declared
    for w in list(hits):
        letters = {x for x in asked if x.startswith(f"{w} ") and len(x) == len(w) + 2}
        theirs = {x for x in declared if x.startswith(f"{w} ") and len(x) == len(w) + 2}
        if letters and theirs and not letters & theirs:
            hits.discard(w)
    return hits


def _conformed_places(ds: Dataset) -> list[str]:
    """The place keys the contract's conformedDimensions declares: a fixed place, or the keys its labels are."""
    schema = (ds.contract.raw.get("schema") or [{}])[0]
    for cp in schema.get("customProperties") or []:
        if not isinstance(cp, Mapping) or cp.get("property") != "conformedDimensions":
            continue
        place: Mapping[str, Any] = (cp.get("value") or {}).get("place") or {}
        decl = [place.get("fixed") or {}, *(place.get("places") or {}).values()]
        return [str(d["key"]) for d in decl if isinstance(d, Mapping) and d.get("key")]
    return []


def _place_name(key: str) -> str:
    if key == geo.NATION_KEY:
        return "the United States"
    for c in geo.CITIES:
        if c.key == key:
            return c.name
    for p in geo.PLACES:
        if geo.key_of(p) == key:
            return p.name
    return key


def declared_coverage(ds: Dataset) -> Coverage:
    """The source's declared coverage (see the module docstring for where each part is read)."""
    e = ds.entry
    keys = set(_conformed_places(ds))
    place_words = " and ".join(sorted(_place_name(k) for k in keys)) if keys else "no places declared yet"
    parts = [*e.conditions.labels, *(a.alias for a in e.conditions.aliases)]
    parts += [f"{c.cause} {c.pathogen or ''}" for c in e.gbd.causes]
    parts += [s.label for s in ds.semantic.series]
    if "infectious_disease" not in e.research_areas:
        parts = []  # a source outside infectious disease (a population denominator) declares no condition
    # The agency's and places' own names sit in labels ("... (Detroit Health Department)"): never a condition. The
    # publisher's full name is not dropped: it can name the conditions it covers ("RSV-NET", "respiratory").
    not_conditions = words(f"{e.agency} {place_words}") | GENERIC_WORDS
    return Coverage(
        place_keys=frozenset(keys) if keys else None,
        place_words=place_words,
        condition_words=frozenset(w for w in words(" ".join(parts)) - not_conditions if not w.isdigit()),
        year_start=e.years.start,
        year_end=e.years.end,
    )
