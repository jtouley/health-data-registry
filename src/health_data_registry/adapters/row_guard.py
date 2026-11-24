"""Refuse any table or map layer whose grain is one person (hard rule 4), whatever adapter reads it.

Dashboards and open-data portals sometimes publish a case list behind an aggregate chart: one row per case with
that person's age, sex, onset date, town or address (Macomb County's ArcGIS layer Recovered_Cases_0902 is one).
This registry serves population-level counts only, so such a table is never read, even when it is public.
``adapters.pages`` runs this check on every page of every portal (Socrata, https_csv, WHO GHO); the Tableau
adapter runs it on each worksheet, by its captions, before anything is projected.

Field names are read as words: Tableau's wrappers (``ATTR(Sex)``, ``MDY(Onset Date)``, ``YEAR(...)``) are taken
off first, and a field matches on any of its words, so ``Patient Gender`` and ``Residence ZIP`` are person
fields as much as ``Sex`` and ``ZIP``. A table is refused when any of these holds:

- a field directly identifies or locates a person: a name, street address, date of birth, phone, e-mail, a
  case, patient, person or record identifier, or a medical record number;
- it has a per-person date (onset, diagnosis, report, specimen, recovery, death ...) next to an exact age or
  sex: with or without a count, that is close to one person per row;
- it has two or more person-level fields (an exact age, sex, race, ethnicity, ZIP code, city of residence, a
  per-person date) and no count or rate field, so each row can only be one case;
- it has two or more person-level fields and most of its counts (more than half of the values read) are 1: one
  row per case again. A count-like name ("Value", "Number", "Total") never silences this; only the values do.

Aggregate tables pass: "Age Group, Sex, Cases" has grouped ages and real counts; "Year, Rate" has no
person-level field. The check reads field names (and, for the last rule, the count values), and a refusal
names fields only, never a value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final


class RowLevelRefused(ValueError):
    """The table's grain is one person, so it is not read (hard rule 4)."""


# Tableau's aggregation and date-part wrappers: "ATTR(Sex)", "MDY(Onset Date)", "AGG(Age)" describe the field
# inside them, so the guard reads that field. SUM/CNT/AVG stay: they make a count (see _AGGREGATED).
_WRAPPER: Final = re.compile(
    r"^\s*(attr|mdy|day|month|year|week|quarter|qtr|agg|min|max|my|tdy|usr|none)\s*\((.*)\)\s*$", re.I
)
# Tableau's own suffixes on a caption: its display text, and a repeated caption's number.
_SUFFIX: Final = re.compile(r"\s*(\(alias\)|#\d+)\s*$", re.I)


def unwrap(field: str) -> str:
    """The field a Tableau caption describes: "ATTR(MDY(Onset Date)) (alias)" -> "Onset Date"."""
    text = str(field).strip()
    while True:
        bare = _SUFFIX.sub("", text).strip()
        m = _WRAPPER.match(bare)
        if m is None:
            return bare
        text = m.group(2)


def _norm(field: str) -> str:
    """snake_case words of a field name: "Onset_Date", "onsetDate" and "Onset Date" all become "onset_date"."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", unwrap(field))
    return "_".join(w for w in re.split(r"[^a-z0-9]+", s.lower()) if w)


_IDENTIFIERS: Final = re.compile(
    r"(^|_)(first_name|last_name|full_name|patient_name|name_of_patient|surname|street|street_address|"
    r"address|addr|home_address|residence_address|dob|date_of_birth|birth_date|birthdate|phone|telephone|"
    r"email|e_mail|ssn|mrn|medical_record(_number)?|case_id|caseid|case_number|case_no|patient_id|person_id|"
    r"record_id|individual_id|client_id)($|_)"
)
_AGGREGATE_WORDS: Final = re.compile(r"(^|_)(group|groups|range|band|bands|bracket|category|cat|grp)($|_)")
# Each kind of person-level field, matched on whole words anywhere in the name. Age is checked before the date
# words, so "Age at Onset" is an age.
_AGE: Final = re.compile(r"(^|_)(age|ages)($|_)")
_SEX: Final = re.compile(r"(^|_)(sex|gender)($|_)")
_PERSON_KINDS: Final = (
    ("age", _AGE),
    ("sex", _SEX),
    ("race", re.compile(r"(^|_)race($|_)")),
    ("ethnicity", re.compile(r"(^|_)(ethnicity|hispanic|latino|latinx)($|_)")),
    ("zip", re.compile(r"(^|_)(zip|zipcode|zip5|res_zip|postal|postcode)($|_)")),
    ("city", re.compile(r"(^|_)(city|town|township|municipality)($|_)")),
    (
        "date",
        re.compile(
            r"(^|_)(onset|symptom|symptoms|birth|recovered|recovery|specimen|dod|dx_date|deceased)($|_)|"
            r"(^|_)(diagnosis|report|reported|death|admit|admission|test|collection|hospitalized|"
            r"discharge|vaccination)_date($|_)|(^|_)date_of_(onset|diagnosis|death|report|admission|test)($|_)"
        ),
    ),
)
_MEASURE: Final = re.compile(
    r"(^|_)(count|counts|cases|case_count|deaths|number|num|n|total|totals|sum|cnt|freq|frequency|rate|rates|"
    r"percent|pct|proportion|incidence|prevalence|records|number_of_records|value|visits|admissions|"
    r"hospitalizations|share|ratio)($|_)"
)
# A measure that is a rate or share, not a count of people: its values are never read for "mostly ones".
_NOT_A_COUNT: Final = re.compile(
    r"(^|_)(rate|rates|percent|pct|proportion|incidence|prevalence|share|ratio|per|avg|average|mean)($|_)"
)
# Tableau and SQL write an aggregated field as "SUM(Cases)", "CNT(Id)", "AGG(Rate)": a count by construction.
_AGGREGATED: Final = re.compile(r"^\s*(sum|cnt|cntd|agg|avg|count|countd|attr\s*\(\s*sum)\s*\(", re.I)


def is_identifier(field: str) -> bool:
    """A field that names, locates or uniquely numbers one person."""
    return bool(_IDENTIFIERS.search(_norm(field)))


def person_kind(field: str) -> str | None:
    """Which kind of person-level field this is (age, sex, race, ethnicity, zip, city, date), or None."""
    if re.match(r"^\s*(sum|cnt|cntd|avg|count|countd)\s*\(", str(field), re.I):
        return None  # an aggregate of people (SUM(Number of Records)), never one person's value
    n = _norm(field)
    if _AGGREGATE_WORDS.search(n):
        return None
    return next((kind for kind, p in _PERSON_KINDS if p.search(n)), None)


def is_person_field(field: str) -> bool:
    """A field that describes one person (an exact age, sex, onset date, home town ...), not a group of people."""
    return person_kind(field) is not None


def is_measure_field(field: str) -> bool:
    """A field that counts or rates a group (Cases, Rate per 100,000, SUM(Number of Records))."""
    return bool(_AGGREGATED.match(str(field))) or bool(_MEASURE.search(_norm(field)))


def _ones_share(values: Iterable[Any]) -> tuple[int, int]:
    """(how many values read as exactly 1, how many numeric values were read); blanks and text are skipped."""
    ones = seen = 0
    for v in values:
        if v is None or isinstance(v, bool) or str(v).strip() == "":
            continue
        try:
            x = float(str(v).replace(",", ""))
        except ValueError:
            continue
        seen += 1
        ones += x == 1.0
    return ones, seen


def check_grain(name: str, fields: Sequence[str], rows: Sequence[Mapping[str, Any]] = ()) -> None:
    """Raise RowLevelRefused when the table ``name`` (with these fields, and optionally rows) is one row per person.

    The message names the table and the fields that gave it away, never a value.
    """
    ids = [f for f in fields if is_identifier(f)]
    if ids:
        raise RowLevelRefused(
            f"{name} identifies individual people ({', '.join(ids)}), so it is not read: this registry serves "
            "population counts only."
        )
    kinds = {f: k for f in fields if (k := person_kind(f)) is not None}
    person = list(kinds)
    dates = [f for f, k in kinds.items() if k == "date"]
    traits = [f for f, k in kinds.items() if k in ("age", "sex")]
    if len(person) < 2:
        return
    measures = [f for f in fields if is_measure_field(f) and f not in kinds]
    if not measures:
        raise RowLevelRefused(
            f"{name} has one row per case ({', '.join(person)} for each row, and no count), so it is not read: "
            "this registry serves population counts only."
        )
    if dates and traits:
        raise RowLevelRefused(
            f"{name} gives a person's date ({', '.join(dates)}) next to their {', '.join(traits)}, which is close "
            "to one row per person, so it is not read: this registry serves population counts only."
        )
    counts = [m for m in measures if not _NOT_A_COUNT.search(_norm(m))]
    ones = seen = 0
    for m in counts:
        o, n = _ones_share(r.get(m) for r in rows)
        ones, seen = ones + o, seen + n
    if seen and ones * 2 > seen:
        raise RowLevelRefused(
            f"{name} counts 1 on most rows next to {', '.join(person)}, so each row is one case; it is not read."
        )
