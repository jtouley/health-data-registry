"""US place names: one place, whatever the publisher calls it ("Michigan", "MI", "MICHIGAN", FIPS 26).

GBD location names for US states are the plain state names, and "United States" for the nation
(the names the IHME GBD connector accepts in `locations=[...]`).

Below the state, `local_place` resolves Michigan's 83 counties ("Wayne", "Wayne County", FIPS 26163) and the
City of Detroit ("Detroit", "City of Detroit", place FIPS 2622000), the places the Michigan dashboards report.
GBD has no location for a US county or city, so those carry `gbd=None`, and `place` (which keys rows to GBD
locations) still resolves only the nation and the states. A label such as "Wayne County (excluding Detroit)"
covers part of a county and is not resolved.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Place:
    name: str  # also the GBD location name, for the nation and the states
    postal: str
    fips: str
    level: str  # nation | state | county | city
    gbd: str | None = None  # the GBD location name, where GBD has the place (nation and states only)
    state: str | None = None  # the postal code of the state a county or city is in


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

PLACES: tuple[Place, ...] = (Place("United States", "US", "00", "nation", gbd="United States"),) + tuple(
    Place(n, p, f, "state", gbd=n) for n, p, f in _STATES
)

# Michigan's counties in FIPS order (26001 Alcona ... 26165 Wexford: odd codes, alphabetical, "St." as "Saint").
_MI_COUNTIES = (
    "Alcona", "Alger", "Allegan", "Alpena", "Antrim", "Arenac", "Baraga", "Barry", "Bay", "Benzie", "Berrien",
    "Branch", "Calhoun", "Cass", "Charlevoix", "Cheboygan", "Chippewa", "Clare", "Clinton", "Crawford", "Delta",
    "Dickinson", "Eaton", "Emmet", "Genesee", "Gladwin", "Gogebic", "Grand Traverse", "Gratiot", "Hillsdale",
    "Houghton", "Huron", "Ingham", "Ionia", "Iosco", "Iron", "Isabella", "Jackson", "Kalamazoo", "Kalkaska", "Kent",
    "Keweenaw", "Lake", "Lapeer", "Leelanau", "Lenawee", "Livingston", "Luce", "Mackinac", "Macomb", "Manistee",
    "Marquette", "Mason", "Mecosta", "Menominee", "Midland", "Missaukee", "Monroe", "Montcalm", "Montmorency",
    "Muskegon", "Newaygo", "Oakland", "Oceana", "Ogemaw", "Ontonagon", "Osceola", "Oscoda", "Otsego", "Ottawa",
    "Presque Isle", "Roscommon", "Saginaw", "St. Clair", "St. Joseph", "Sanilac", "Schoolcraft", "Shiawassee",
    "Tuscola", "Van Buren", "Washtenaw", "Wayne", "Wexford",
)  # fmt: skip

LOCAL_PLACES: tuple[Place, ...] = tuple(
    Place(f"{n} County, Michigan", "MI", f"26{2 * i + 1:03d}", "county", state="MI")
    for i, n in enumerate(_MI_COUNTIES)
) + (Place("Detroit, Michigan", "MI", "2622000", "city", state="MI"),)

# No "total": a publisher's TOTAL row (NNDSS) also counts territories and non-US residents, so it isn't the US.
_ALIASES = {"usa": "US", "us": "US", "u.s.": "US", "united states": "US", "us residents": "US", "u.s. residents": "US",
            "national": "US", "nation": "US", "new york state": "NY"}  # fmt: skip

_BY_KEY: dict[str, Place] = {}
for _p in PLACES:
    for _k in (_p.name, _p.postal, _p.fips):
        _BY_KEY[_k.lower()] = _p


def _key(text: str) -> str:
    return " ".join(str(text).strip().lower().replace("_", " ").split())


def place(text: str) -> Place | None:
    """The place a publisher's or a clinician's label means, or None if it isn't a US state or the nation."""
    key = _key(text)
    if key in _ALIASES:
        key = _ALIASES[key].lower()
    return _BY_KEY.get(key)


_LOCAL: dict[str, Place] = {}
for _p in LOCAL_PLACES:
    if _p.level == "county":
        _short = _p.name.removesuffix(" County, Michigan")
        _names = {_short, f"{_short} County", f"{_short} Co.", f"{_short} County, MI", _p.name}
        if _short.startswith("St. "):
            _names |= {n.replace("St. ", "Saint ", 1) for n in _names} | {
                n.replace("St. ", "St ", 1) for n in _names
            }
    else:
        _names = {"Detroit", "City of Detroit", "Detroit City", "Detroit, MI", _p.name}
    for _k in (*_names, _p.fips):
        _LOCAL[_key(_k)] = _p


def local_place(text: str) -> Place | None:
    """A county or city in Michigan (the Michigan dashboards' places), else the state or nation `place` finds.

    A bare county name ("Wayne") means the Michigan county: only Michigan's local places are known so far.
    """
    return _LOCAL.get(_key(text)) or place(text)
