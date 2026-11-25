"""What each source covers: places, conditions and years, read only from what is declared about it.

One function, ``declared_coverage``, answers it for every tool, so routing never guesses coverage from words. A
source whose coverage isn't declared covers "unknown", and an unknown never counts as a fit.

Where it reads today, in order:

- places: the contract's ``conformedDimensions`` (the transform's conformed place keys: USA, US-<state FIPS>,
  US-<state FIPS><county FIPS>, or a declared part of one, e.g. US-26163/detroit), else the catalog entry's own
  declaration that a federal source covers its whole country (``level: federal``, ``country: US``) or that an
  international source covers every country (``level: international``, no ``country``). Nothing else: a city or
  state source with no conformed places declared covers "unknown".
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
        "infections",
        "invasive",
        "isolates",
        "lower",
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
    ]
)


@dataclass(frozen=True)
class Coverage:
    """A source's declared coverage. `place_keys` is None when no place coverage is declared (unknown);
    `everywhere` when it declares every country."""

    place_keys: frozenset[str] | None
    everywhere: bool
    place_words: str  # the declared places in plain words, for answers
    condition_words: frozenset[str]  # the specific words of the conditions it declares; empty means unknown
    year_start: int
    year_end: int | None

    def holds(self, key: str) -> bool | None:
        """Does the declared coverage contain the place `key`? None when nothing is declared."""
        if self.everywhere:
            return True
        if self.place_keys is None:
            return None
        return any(geo.within(outer, key) for outer in self.place_keys)

    def holds_years(self, start: int | None, end: int | None) -> bool:
        """Do the declared years overlap the years asked for (either end may be open)?"""
        last = self.year_end if self.year_end is not None else 9999
        return not ((start is not None and start > last) or (end is not None and end < self.year_start))

    @property
    def nationwide(self) -> bool:
        """Declares a whole country or more, so a question that names no place still fits it."""
        return self.everywhere or geo.NATION_KEY in (self.place_keys or ())


def words(text: str) -> set[str]:
    """Lower-case words, hyphenated ones also split ("influenza-associated" gives "influenza" too). A single
    letter is kept with the word after it ("Q fever" gives "q fever", "C. auris" gives "c auris")."""
    low = text.lower().replace(".", " ")
    out: set[str] = {f"{a} {b}" for a, b in re.findall(r"(?<![a-z0-9])([b-hj-z])\s+([a-z][a-z\-]*)", low)}
    for w in re.findall(r"[a-z0-9][a-z0-9\-]*", low):
        out.add(w)
        out.update(p for p in w.split("-") if p)
    return {w for w in out if len(w) > 1}


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
    everywhere = e.level == "international" and e.country is None
    if e.level == "federal" and e.country == "US":
        keys.add(geo.NATION_KEY)
    if everywhere:
        place_words = "every country"
    elif keys:
        place_words = " and ".join(sorted(_place_name(k) for k in keys))
    else:
        place_words = "no places declared yet"
    parts = [*e.conditions.labels, *(a.alias for a in e.conditions.aliases)]
    parts += [f"{c.cause} {c.pathogen or ''}" for c in e.gbd.causes]
    parts += [s.label for s in ds.semantic.series]
    if "infectious_disease" not in e.research_areas:
        parts = []  # a source outside infectious disease (a population denominator) declares no condition
    # The publisher's and places' own names sit in labels ("... (Detroit Health Department)"): never a condition.
    not_conditions = words(f"{e.publisher} {e.agency} {place_words}") | GENERIC_WORDS
    return Coverage(
        place_keys=frozenset(keys) if keys or everywhere else None,
        everywhere=everywhere,
        place_words=place_words,
        condition_words=frozenset(w for w in words(" ".join(parts)) - not_conditions if not w.isdigit()),
        year_start=e.years.start,
        year_end=e.years.end,
    )
