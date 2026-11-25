"""US place names: one place, whatever the publisher calls it ("Michigan", "MI", "MICHIGAN", FIPS 26).

GBD location names for US states are the plain state names, and "United States" for the nation
(the names the IHME GBD connector accepts in `locations=[...]`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Place:
    name: str  # also the GBD location name
    postal: str
    fips: str
    level: str  # nation | state


_STATES = [
    ("Alabama", "AL", "01"), ("Alaska", "AK", "02"), ("Arizona", "AZ", "04"), ("Arkansas", "AR", "05"),
    ("California", "CA", "06"), ("Colorado", "CO", "08"), ("Connecticut", "CT", "09"), ("Delaware", "DE", "10"),
    ("District of Columbia", "DC", "11"), ("Florida", "FL", "12"), ("Georgia", "GA", "13"), ("Hawaii", "HI", "15"),
    ("Idaho", "ID", "16"), ("Illinois", "IL", "17"), ("Indiana", "IN", "18"), ("Iowa", "IA", "19"),
    ("Kansas", "KS", "20"), ("Kentucky", "KY", "21"), ("Louisiana", "LA", "22"), ("Maine", "ME", "23"),
    ("Maryland", "MD", "24"), ("Massachusetts", "MA", "25"), ("Michigan", "MI", "26"), ("Minnesota", "MN", "27"),
    ("Mississippi", "MS", "28"), ("Missouri", "MO", "29"), ("Montana", "MT", "30"), ("Nebraska", "NE", "31"),
    ("Nevada", "NV", "32"), ("New Hampshire", "NH", "33"), ("New Jersey", "NJ", "34"), ("New Mexico", "NM", "35"),
    ("New York", "NY", "36"), ("North Carolina", "NC", "37"), ("North Dakota", "ND", "38"), ("Ohio", "OH", "39"),
    ("Oklahoma", "OK", "40"), ("Oregon", "OR", "41"), ("Pennsylvania", "PA", "42"), ("Rhode Island", "RI", "44"),
    ("South Carolina", "SC", "45"), ("South Dakota", "SD", "46"), ("Tennessee", "TN", "47"), ("Texas", "TX", "48"),
    ("Utah", "UT", "49"), ("Vermont", "VT", "50"), ("Virginia", "VA", "51"), ("Washington", "WA", "53"),
    ("West Virginia", "WV", "54"), ("Wisconsin", "WI", "55"), ("Wyoming", "WY", "56"), ("Puerto Rico", "PR", "72"),
]  # fmt: skip

PLACES: tuple[Place, ...] = (Place("United States", "US", "00", "nation"),) + tuple(
    Place(n, p, f, "state") for n, p, f in _STATES
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

# The transform's conformed place keys (dim_place): USA for the nation, US-<state FIPS> for a state,
# US-<state FIPS><county FIPS> for a county, and <bigger place's key>/<name> for a declared part of one.
NATION_KEY = "USA"


@dataclass(frozen=True)
class City:
    name: str
    key: str  # its conformed key: the part of a bigger place it is (dim_place declares the City of Detroit)
    aliases: tuple[str, ...] = ()


# Only the cities a catalogued source reports apart from its state. Detroit's key is dim_place's own; New York
# City's says only that it is part of New York State, which is all a coverage check needs.
CITIES: tuple[City, ...] = (
    City("Detroit", "US-26163/detroit", ("city of detroit",)),
    City("New York City", "US-36/nyc", ("nyc", "new york city")),
)


def key_of(p: Place) -> str:
    """A US place's conformed place key."""
    return NATION_KEY if p.level == "nation" else f"US-{p.fips}"


def parent_key(key: str) -> str | None:
    """The place a conformed key is part of, read from the key itself; None for the nation or a country."""
    if "/" in key:
        return key.rsplit("/", 1)[0]
    if len(key) == 8 and key.startswith("US-"):  # a county: US-<2-digit state><3-digit county>
        return key[:5]
    if len(key) == 5 and key.startswith("US-"):  # a state
        return NATION_KEY
    return None


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
