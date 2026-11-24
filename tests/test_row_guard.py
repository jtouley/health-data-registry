"""The row-level guard (hard rule 4): a table whose grain is one person is never read, by any adapter."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from health_data_registry.adapters.row_guard import (
    RowLevelRefused,
    check_grain,
    is_identifier,
    is_measure_field,
    is_person_field,
)

# Shaped like Macomb County's ArcGIS layer Recovered_Cases_0902: its fields, with made-up values. One row per
# recovered case. It must never be ingested.
MACOMB_RECOVERED_FIELDS = [
    "OBJECTID", "Age", "Sex", "Race", "Ethnicity", "City", "Zip_Code", "Onset_Date", "Recovered_Date",
    "Hospitalized", "Status",
]  # fmt: skip
MACOMB_RECOVERED_ROWS = [
    {"OBJECTID": 1, "Age": 47, "Sex": "F", "Race": "White", "Ethnicity": "Not Hispanic", "City": "Warren",
     "Zip_Code": "48000", "Onset_Date": "2020-08-01", "Recovered_Date": "2020-08-31", "Hospitalized": "N",
     "Status": "Recovered"},
    {"OBJECTID": 2, "Age": 63, "Sex": "M", "Race": "Black", "Ethnicity": "Not Hispanic", "City": "Sterling Heights",
     "Zip_Code": "48001", "Onset_Date": "2020-08-03", "Recovered_Date": "2020-09-02", "Hospitalized": "Y",
     "Status": "Recovered"},
]  # fmt: skip


def test_macomb_recovered_cases_layer_is_refused() -> None:
    with pytest.raises(RowLevelRefused) as e:
        check_grain("Recovered_Cases_0902", MACOMB_RECOVERED_FIELDS, MACOMB_RECOVERED_ROWS)
    msg = str(e.value)
    assert "Recovered_Cases_0902" in msg and "one row per case" in msg
    assert "Warren" not in msg and "47" not in msg  # the refusal names fields, never a value


def test_macomb_layer_is_refused_from_its_fields_alone() -> None:
    with pytest.raises(RowLevelRefused):
        check_grain("Recovered_Cases_0902", MACOMB_RECOVERED_FIELDS)


@pytest.mark.parametrize(
    "field",
    ["Address", "Street_Address", "home address", "DOB", "Date_of_Birth", "BirthDate", "Case_ID", "CaseID",
     "PatientId", "MRN", "Last_Name", "first name", "Phone", "EMAIL", "record_id", "Case Number"],
)  # fmt: skip
def test_a_direct_identifier_refuses_even_an_aggregate_looking_table(field: str) -> None:
    assert is_identifier(field)
    with pytest.raises(RowLevelRefused) as e:
        check_grain("t", ["Year", "Cases", field])
    assert "identifies individual people" in str(e.value)


@pytest.mark.parametrize(
    "fields",
    [
        ["Age Group", "Sex", "Cases"],  # the classic aggregate
        ["Year", "Location", "SUM(Cases)", "SUM(Rate)"],  # Detroit's yearly worksheet
        ["Age Range", "Disease Type", "AGG(Count Suppressed)", "AGG(Counts)"],
        ["Race (group)", "CNT(Disease Type)"],
        ["Onset Date", "Case Count"],  # an epidemic curve: one person-level field, so not per person
        ["County", "Year", "Rate per 100,000"],
        ["Sex", "Race", "Number of Records"],
    ],
)
def test_aggregate_tables_pass(fields: list[str]) -> None:
    check_grain("t", fields)


def test_a_count_of_one_on_every_row_is_one_row_per_case() -> None:
    rows = [{"Age": 30, "Sex": "F", "Count": 1}, {"Age": 31, "Sex": "M", "Count": "1"}]
    with pytest.raises(RowLevelRefused) as e:
        check_grain("t", ["Age", "Sex", "Count"], rows)
    assert "counts 1 on every row" in str(e.value)


def test_a_real_count_next_to_person_fields_passes() -> None:
    rows = [{"Age": 30, "Sex": "F", "Count": 1}, {"Age": 31, "Sex": "M", "Count": "1,204"}]
    check_grain("t", ["Age", "Sex", "Count"], rows)


def test_counts_that_are_all_empty_do_not_read_as_ones() -> None:
    rows = [{"Age": 30, "Sex": "F", "Count": None}, {"Age": 31, "Sex": "M", "Count": ""}]
    check_grain("t", ["Age", "Sex", "Count"], rows)


def test_a_text_count_is_not_a_one() -> None:
    check_grain("t", ["Age", "Sex", "Count"], [{"Age": 1, "Sex": "F", "Count": "<6"}])


@pytest.mark.parametrize(
    ("field", "person"),
    [("Age", True), ("age_years", True), ("Age Group", False), ("Age_Range", False), ("AgeBand", False),
     ("onsetDate", True), ("Symptom Onset", True), ("ZIP", True), ("Sex", True), ("Gender", True),
     ("Race Category", False), ("Year", False), ("County", False)],
)  # fmt: skip
def test_person_fields(field: str, person: bool) -> None:
    assert is_person_field(field) is person


@pytest.mark.parametrize(
    ("field", "measure"),
    [("SUM(Cases)", True), ("CNTD(Id)", True), ("Rate", True), ("Case Count", True), ("Year", False),
     ("Sex", False), ("Percent of total", True)],
)  # fmt: skip
def test_measure_fields(field: str, measure: bool) -> None:
    assert is_measure_field(field) is measure


GROUPS = st.sampled_from(["Age Group", "Sex", "Race", "Ethnicity", "County", "Year", "Week", "Location"])
MEASURES = st.sampled_from(["Cases", "SUM(Cases)", "Rate", "Deaths", "Count", "AGG(Counts)"])


@given(st.lists(GROUPS, min_size=1, max_size=5, unique=True), MEASURES)
def test_any_grouped_count_table_passes(groups: list[str], measure: str) -> None:
    check_grain("t", [*groups, measure])


PERSON = st.sampled_from(["Age", "Sex", "Race", "Zip_Code", "City", "Onset_Date", "Death_Date"])


@given(st.lists(PERSON, min_size=2, max_size=7, unique=True))
def test_two_person_fields_without_a_count_are_always_refused(fields: list[str]) -> None:
    with pytest.raises(RowLevelRefused):
        check_grain("t", ["OBJECTID", *fields])
