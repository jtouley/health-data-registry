"""US place names: one place, whatever the publisher calls it ("Michigan", "MI", "MICHIGAN", FIPS 26).

GBD location names for US states are the plain state names, and "United States" for the nation
(the names the IHME GBD connector accepts in `locations=[...]`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml


@dataclass(frozen=True)
class Place:
    name: str  # also the GBD location name
    postal: str
    fips: str
    level: str  # nation | state


PLACE_CONTRACT = Path("contracts") / "models" / "dim_place.odcs.yaml"


def _place_contract() -> Path:
    """dim_place's contract: under HDR_REGISTRY when it has one, else the repo checkout, else the wheel's copy."""
    here = Path(__file__).resolve()
    env = os.environ.get("HDR_REGISTRY")
    roots = ([Path(env)] if env else []) + [here.parents[2], here.parent / "_registry"]
    for root in roots:
        if (root / PLACE_CONTRACT).is_file():
            return root / PLACE_CONTRACT
    raise RuntimeError(f"no {PLACE_CONTRACT} found: the place keys are defined there")


def _declared_places() -> Mapping[str, Any]:
    """The `places` property of contracts/models/dim_place.odcs.yaml: the one list of fixed place keys."""
    doc = yaml.safe_load(_place_contract().read_text(encoding="utf-8"))
    for cp in doc.get("customProperties") or []:
        if cp.get("property") == "places":
            return cast(Mapping[str, Any], cp["value"])
    raise RuntimeError(f"{PLACE_CONTRACT} declares no places")


_DECLARED = _declared_places()
_NATION = _DECLARED["nation"]
_STATES = [(str(n), str(p), str(f)) for n, p, f in _DECLARED["states"]]

PLACES: tuple[Place, ...] = (
    Place(str(_NATION["name"]), str(_NATION["postal"]), str(_NATION["fips"]), "nation"),
    *(Place(n, p, f, "state") for n, p, f in _STATES),
)

# No "total": a publisher's TOTAL row (NNDSS) also counts territories and non-US residents, so it isn't the US.
_ALIASES = {"usa": "US", "us": "US", "u.s.": "US", "united states": "US", "us residents": "US", "u.s. residents": "US",
            "national": "US", "nation": "US", "new york state": "NY"}  # fmt: skip

_BY_KEY: dict[str, Place] = {}
for _p in PLACES:
    for _k in (_p.name, _p.postal, _p.fips):
        _BY_KEY[_k.lower()] = _p


def place(text: str) -> Place | None:
    """The place a publisher's or a clinician's label means, or None if it isn't a US state or the nation."""
    key = " ".join(str(text).strip().lower().replace("_", " ").split())
    if key in _ALIASES:
        key = _ALIASES[key].lower()
    return _BY_KEY.get(key)


def containing_state(text: str) -> Place | None:
    """The state a smaller place is written in ("Travis County, Texas", "Travis County TX"), or None.

    Read only from the words given: the last comma-separated part, else the last one or two words, must name a
    state. A bare county or city name is never guessed into a state, and a state name alone is not "contained".
    """
    raw = str(text).strip()
    if place(raw) is not None:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    candidates = [parts[-1]] if len(parts) > 1 else []
    words = raw.replace(",", " ").split()
    candidates += [" ".join(words[-n:]) for n in (3, 2, 1) if len(words) > n]
    for c in candidates:
        p = place(c)
        if p is not None and p.level == "state":
            return p
    return None


# --------------------------------------------------------------------------------------------- place keys

# Conformed place keys: USA for the nation, US-<state FIPS> for a state, US-<state FIPS><county FIPS> for a county,
# <bigger place's key>/<name> for a declared part of one, an ISO 3166-1 alpha-3 code for any other country, and
# WHO-<region code> for a WHO region. The fixed keys are declared once, in dim_place's contract (read above); county
# keys follow the grammar, and dim_place lists the ones the Census file has.
NATION_KEY = str(_NATION["key"])
KEY_GRAMMAR = re.compile(r"^(?:USA|US-\d{2}(?:\d{3})?(?:/[a-z0-9-]+)?|[A-Z]{3}|WHO-[A-Z]{3,4})$")


@dataclass(frozen=True)
class City:
    name: str
    key: str  # its conformed key: the part of a bigger place it is
    aliases: tuple[str, ...] = ()


# Only the cities a catalogued source reports apart from its state (dim_place's contract says why each key is so).
CITIES: tuple[City, ...] = tuple(
    City(str(c["name"]), str(c["key"]), tuple(str(a) for a in c.get("aliases") or ()))
    for c in _DECLARED["cities"]
)
WHO_REGIONS: tuple[tuple[str, str], ...] = tuple(
    (str(r["key"]), str(r["name"])) for r in _DECLARED["whoRegions"]
)
COUNTRIES: tuple[tuple[str, str], ...] = tuple((str(k), str(n)) for k, n in _DECLARED["countries"])
# Territories other than Puerto Rico: keys only, never read from a question's words (their GBD names aren't confirmed).
TERRITORIES: tuple[tuple[str, str], ...] = tuple((f"US-{f}", str(n)) for n, _p, f in _DECLARED["territories"])


def key_of(p: Place) -> str:
    """A US place's conformed place key."""
    return NATION_KEY if p.level == "nation" else f"US-{p.fips}"


def parent_key(key: str) -> str | None:
    """The place a conformed key is part of, read from the key itself; None for the nation or a country."""
    if "/" in key:
        return key.rsplit("/", 1)[0]
    if len(key) == 8 and key.startswith("US-"):  # a county: US-<2-digit state><3-digit county>
        return key[:5]
    if len(key) == 5 and key.startswith("US-"):  # a state, DC or a territory
        # A territory (FIPS above 56: Puerto Rico, Guam, ...) is its own place, related to USA (territory_of) but
        # never part of it here, so coverage never sums it into USA or takes it out of USA. USA itself is the
        # nation as each publisher reports it; what a source's national row includes is in its contract
        # (coverage.nationalScope), never assumed here.
        return None if is_territory(key) else NATION_KEY
    return None


def is_territory(key: str) -> bool:
    """Is the conformed key a US territory (state-level FIPS above 56)?"""
    return len(key) == 5 and key.startswith("US-") and key[3:].isdigit() and int(key[3:]) > 56


def territory_of(key: str) -> str | None:
    """The nation a territory belongs to (USA), or None: a relation, never containment."""
    return NATION_KEY if is_territory(key) else None


def is_city(key: str) -> bool:
    """Is the conformed key a city reported apart from its state (a declared part of a place, e.g. US-36/nyc)?"""
    return "/" in key


def within(outer: str, inner: str) -> bool:
    """Is the place `inner` the place `outer` or a part of it?"""
    k: str | None = inner
    while k is not None:
        if k == outer:
            return True
        k = parent_key(k)
    return False


def fixed_keys() -> frozenset[str]:
    """Every key dim_place's contract lists itself: the nation, states, cities, countries and WHO regions."""
    return _FIXED


def is_place_key(key: str) -> bool:
    """Is `key` a conformed place key: one dim_place's contract lists, or a county key inside a listed state?

    A county's own existence is checked in dbt, against the Census file dim_place reads (its relationships test)."""
    if key in _FIXED:
        return True
    return bool(re.fullmatch(r"US-\d{5}", key)) and key[:5] in _FIXED


def level_of(key: str) -> str:
    """The kind of place a conformed key names: nation, state, county, city, country or who_region."""
    if key == NATION_KEY:
        return "nation"
    if "/" in key:
        return "city"
    if key.startswith("WHO-"):
        return "who_region"
    if key.startswith("US-"):
        return "county" if len(key) == 8 else "state"
    return "country"


def name_of(key: str) -> str:
    """A conformed key's place name, or the key itself when it names no listed place (a county)."""
    return _NAMES.get(key, key)


# Names written out in full, matched as whole words in any case; postal codes only as written ("US", "U.S.").
_NAMED: tuple[tuple[str, str, str], ...] = tuple(
    sorted(
        [(p.name.lower(), p.name, key_of(p)) for p in PLACES]
        + [("usa", "United States", NATION_KEY), ("new york state", "New York", "US-36")]
        + [
            ("washington dc", "District of Columbia", "US-11"),
            ("washington, dc", "District of Columbia", "US-11"),
        ]
        + [(n.lower(), c.name, c.key) for c in CITIES for n in (c.name, *c.aliases)],
        key=lambda t: -len(t[0]),
    )
)
_NATION_MARKS = re.compile(r"(?<![\w.])(?:US|U\.S\.|USA|U\.S\.A\.)(?![\w])")


def places_named(text: str) -> tuple[list[tuple[str, str]], str]:
    """The places a question names, as (name, conformed key), and the text left once they are taken out.

    Longest names first, so "New York City" is never read as New York State nor "West Virginia" as Virginia.
    """
    found: list[tuple[str, str]] = []
    rest = text
    for low, name, key in _NAMED:
        pat = re.compile(rf"(?<![\w-]){re.escape(low)}(?![\w-])", re.IGNORECASE)
        if pat.search(rest):
            found.append((name, key))
            rest = pat.sub(" ", rest)
    if _NATION_MARKS.search(rest):
        found.append(("United States", NATION_KEY))
        rest = _NATION_MARKS.sub(" ", rest)
    return list(dict.fromkeys(found)), rest


def key_for(text: str) -> str | None:
    """The conformed key of one place label ("Ohio", "OH", "Detroit"), the state a smaller place is written in
    ("Travis County, Texas"), or None when the words name no place this module knows."""
    p = place(text)
    if p is not None:
        return key_of(p)
    low = " ".join(str(text).strip().lower().split())
    for c in CITIES:
        if low in (c.name.lower(), *c.aliases):
            return c.key
    st = containing_state(text)
    return key_of(st) if st is not None else None


_NAMES: dict[str, str] = {
    **{key_of(p): p.name for p in PLACES},
    **{c.key: c.name for c in CITIES},
    **dict(WHO_REGIONS),
    **dict(COUNTRIES),
    **dict(TERRITORIES),
}
_FIXED: frozenset[str] = frozenset(_NAMES)
