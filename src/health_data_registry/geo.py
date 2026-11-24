"""US place names: one place, whatever the publisher calls it ("Michigan", "MI", "MICHIGAN", FIPS 26).

GBD location names for US states are the plain state names, and "United States" for the nation
(the names the IHME GBD connector accepts in `locations=[...]`).
"""

from __future__ import annotations

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

_ALIASES = {"usa": "US", "us": "US", "u.s.": "US", "united states": "US", "us residents": "US", "u.s. residents": "US",
            "national": "US", "nation": "US", "total": "US", "new york state": "NY"}  # fmt: skip

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
