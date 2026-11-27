"""What each source covers: places, conditions and years, read only from what is declared about it.

One function, ``declared_coverage``, answers it for every tool, so routing never guesses coverage from words. A
source whose coverage isn't declared covers "unknown", and an unknown never counts as a fit.

Where it reads today, in order:

- places: the contract's coverage block (#223: which places, whole or some counties, over which years, on which
  publisher basis) and its schema's ``conformedDimensions`` place keys, both parsed and checked by the registry
  against dim_place (contracts/models/dim_place.odcs.yaml, the one list of place keys). Nothing else, and never the
  catalog's ``level`` or ``country``: those say who publishes a source, not where its rows are (a federal network
  can cover a few counties in some states, and an international abstract list covers no country). A source with no
  declared places covers "unknown".
- conditions: the catalog's condition labels and aliases, its GBD crosswalk, and the semantic model's series.
- years: the catalog's ``years``.

The same coverage blocks feed the dbt models (``make contracts`` writes them into ``coverage_declarations``; then
``dim_dataset_coverage``, an SCD2 dimension, and ``fct_dataset_place_year_coverage``, one row per place and year).
``holds`` applies the rule that fact applies: the most specific declaration of a place decides, a place inside a
declared one counts only at a level the source reports (its catalog ``geography``), and a city counts only when
declared itself. The dimension replaces places only; conditions and years still come from the catalog and the
semantic model. The stdio server has no dbt build to read, so this module reads the contracts the marts are built
from; its callers don't change.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

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
    # Places declared as only some of their counties: key -> the counties (None when the publisher doesn't list them)
    partial: Mapping[str, tuple[str, ...] | None] = field(default_factory=dict)
    levels: frozenset[str] = (
        frozenset()
    )  # the levels the source's rows report (catalog geography); empty: any

    def _declared_for(self, key: str) -> str | None:
        """The most specific declared place that is `key` or contains it."""
        k: str | None = key
        while k is not None:
            if k in (self.place_keys or ()):
                return k
            k = geo.parent_key(k)
        return None

    def holds(self, key: str) -> bool | None:
        """Does the declared coverage contain the place `key`? None when nothing is declared, or when it can't say.

        The most specific declaration decides. A place inside a declared one is held only at a level the source
        reports (a state-level source doesn't report a county), and inside a place declared as some of its counties
        only when that county is listed (unknown when the counties aren't). A city a publisher reports apart from
        its state (Detroit, New York City) is held only when the city itself is declared: a country or state
        declaration says which states a source reports, never which cities it reports apart from them (#111)."""
        if self.place_keys is None:
            return None
        outer = self._declared_for(key)
        if outer is None:
            return False
        if geo.is_city(key) and outer != key:
            return None
        if outer == key:
            return True
        if self.levels and geo.level_of(key) not in self.levels:
            return None
        if outer in self.partial:
            counties = self.partial[outer]
            if counties is None:
                return None
            return any(geo.within(c, key) for c in counties)
        return True

    def partial_note(self, key: str) -> str | None:
        """Plain words for a held place this source covers only part of, or None for a whole place."""
        outer = self._declared_for(key) if self.place_keys is not None else None
        if outer != key or key not in self.partial:
            return None
        counties = self.partial[key]
        some = f"{len(counties)} of its counties" if counties else "some of its counties, not listed"
        return f"its {geo.name_of(key)} rows cover only part of {geo.name_of(key)} ({some}), not all of it"

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
    pairs += re.findall(r"([a-z][a-z\-]*)\s+([b-hj-z])(?![a-z0-9\-])", low)  # "hepatitis b"
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


def _place_name(key: str) -> str:
    return "the United States" if key == geo.NATION_KEY else geo.name_of(key)


def declared_coverage(ds: Dataset) -> Coverage:
    """The source's declared coverage (see the module docstring for where each part is read)."""
    e = ds.entry
    decl = ds.contract.coverage
    keys = {p.key for p in ds.contract.conformed_places if p.key} | {
        p.place_key for p in (decl.places if decl else ())
    }
    partial = {p.place_key: p.counties for p in (decl.places if decl else ()) if p.part == "counties"}
    names = [
        _place_name(k) + (" (part of it)" if k in partial else "") for k in sorted(keys, key=_place_name)
    ]
    place_words = (
        (", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0])
        if names
        else ("no places declared yet")
    )
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
        partial=partial,
        levels=frozenset(e.geography),
    )
