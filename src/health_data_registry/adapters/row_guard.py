"""Refuse any table or map layer whose grain is one person (hard rule 4), whatever adapter reads it.

Dashboards and open-data portals sometimes publish a case list behind an aggregate chart: one row per case with
that person's age, sex, onset date, town or address (Macomb County's ArcGIS layer Recovered_Cases_0902 is one).
This registry serves population-level counts only, so such a table is never read, even when it is public.

A table is refused when any of these holds:

- a field directly identifies or locates a person: a name, street address, date of birth, phone, e-mail, a
  case, patient, person or record identifier, or a medical record number;
- it has two or more person-level fields (an exact age, sex, race, ethnicity, ZIP code, city of residence, a
  per-person date such as onset, diagnosis, report, recovery or death) and no count or rate field, so each row
  can only be one case;
- it has a count field whose every value is 1 next to two or more person-level fields: one row per case again.

Aggregate tables pass: "Age Group, Sex, Cases" has a count; "Year, Rate" has no person-level field. The check
reads field names only (and, for the last rule, the count values), so a refusal never needs to look at a row.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final


class RowLevelRefused(ValueError):
    """The table's grain is one person, so it is not read (hard rule 4)."""


def _norm(field: str) -> str:
    """snake_case words of a field name: "Onset_Date", "onsetDate" and "Onset Date" all become "onset_date"."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(field).strip())
    return "_".join(w for w in re.split(r"[^a-z0-9]+", s.lower()) if w)


_IDENTIFIERS: Final = re.compile(
    r"(^|_)(first_name|last_name|full_name|patient_name|name_of_patient|surname|street|street_address|"
    r"address|addr|home_address|residence_address|dob|date_of_birth|birth_date|birthdate|phone|telephone|"
    r"email|e_mail|ssn|mrn|medical_record(_number)?|case_id|caseid|case_number|case_no|patient_id|person_id|"
    r"record_id|individual_id|client_id)($|_)"
)
_AGGREGATE_WORDS: Final = re.compile(r"(^|_)(group|groups|range|band|bands|bracket|category|cat|grp)($|_)")
_PERSON_FIELDS: Final = (
    re.compile(r"^(age|age_years|age_yrs|patient_age|case_age|age_at_(onset|diagnosis|death))$"),
    re.compile(r"^(sex|gender|patient_sex|case_sex)$"),
    re.compile(r"^(race|patient_race|case_race)$"),
    re.compile(r"^(ethnicity|hispanic|patient_ethnicity)$"),
    re.compile(r"^(zip|zip_code|zipcode|postal_code|zip5|res_zip)$"),
    re.compile(r"^(city|town|res_city|city_of_residence|municipality)$"),
    re.compile(
        r"^(onset|onset_date|date_of_onset|symptom_onset(_date)?|diagnosis_date|date_of_diagnosis|dx_date|"
        r"report_date|reported_date|recovered|recovered_date|recovery_date|date_recovered|death_date|"
        r"date_of_death|dod|hospitalized_date|admit_date|admission_date|specimen_date|test_date)$"
    ),
)
_MEASURE: Final = re.compile(
    r"(^|_)(count|counts|cases|case_count|deaths|number|num|n|total|totals|sum|cnt|freq|frequency|rate|rates|"
    r"percent|pct|proportion|incidence|prevalence|records|number_of_records|value|visits|admissions|"
    r"hospitalizations)($|_)"
)
# Tableau and SQL write an aggregated field as "SUM(Cases)", "CNT(Id)", "AGG(Rate)": a count by construction.
_AGGREGATED: Final = re.compile(r"^\s*(sum|cnt|cntd|agg|avg|count|countd|attr\s*\(\s*sum)\s*\(", re.I)


def is_identifier(field: str) -> bool:
    """A field that names, locates or uniquely numbers one person."""
    return bool(_IDENTIFIERS.search(_norm(field)))


def is_person_field(field: str) -> bool:
    """A field that describes one person (an exact age, sex, onset date, home town ...), not a group of people."""
    n = _norm(field)
    if _AGGREGATE_WORDS.search(n):
        return False
    return any(p.fullmatch(n) for p in _PERSON_FIELDS)


def is_measure_field(field: str) -> bool:
    """A field that counts or rates a group (Cases, Rate per 100,000, SUM(Number of Records))."""
    return bool(_AGGREGATED.match(str(field))) or bool(_MEASURE.search(_norm(field)))


def _all_ones(values: Iterable[Any]) -> bool:
    seen = False
    for v in values:
        if v is None or str(v).strip() == "":
            continue
        try:
            if float(str(v).replace(",", "")) != 1.0:
                return False
        except ValueError:
            return False
        seen = True
    return seen


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
    person = [f for f in fields if is_person_field(f)]
    if len(person) < 2:
        return
    measures = [f for f in fields if is_measure_field(f) and f not in person]
    if not measures:
        raise RowLevelRefused(
            f"{name} has one row per case ({', '.join(person)} for each row, and no count), so it is not read: "
            "this registry serves population counts only."
        )
    if rows and all(_all_ones(r.get(m) for r in rows) for m in measures):
        raise RowLevelRefused(
            f"{name} counts 1 on every row next to {', '.join(person)}, so each row is one case; it is not read."
        )
