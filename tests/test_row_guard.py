"""The row-level guard (hard rule 4): a table whose grain is one person is never read, by any adapter."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from health_data_registry.adapters.row_guard import (
    RowLevelRefused,
    _ones_share,
    check_grain,
    is_identifier,
    is_measure_field,
    is_person_field,
    person_kind,
    unwrap,
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
    assert "counts 1 on most rows" in str(e.value)


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


def test_ones_share_skips_blank_cells_and_text_and_reads_thousands_separators() -> None:
    assert _ones_share([None, "", " ", "1", 1.0, "<6", True]) == (2, 2)
    assert _ones_share(["", None]) == (0, 0)  # nothing to read is not "every row is one"
    assert _ones_share(["1,"]) == (1, 1)  # a separator is dropped before the value is read
    assert _ones_share(["1", "2", "1,204"]) == (1, 3)


def test_refusals_list_every_field_that_gave_the_table_away() -> None:
    with pytest.raises(RowLevelRefused, match=r"\(Patient_Name, Street_Address\)"):
        check_grain("t", ["Patient_Name", "Street_Address", "Cases"])
    with pytest.raises(RowLevelRefused, match=r"\(Age, Sex for each row"):
        check_grain("t", ["Age", "Sex"])
    with pytest.raises(RowLevelRefused, match=r"next to Age, Sex, so each row"):
        check_grain("t", ["Age", "Sex", "Count"], [{"Count": 1}, {"Count": "1"}])
    with pytest.raises(RowLevelRefused, match=r"date \(Onset Date\) next to their Age, Sex"):
        check_grain("t", ["Age", "Sex", "Onset Date", "Cases"])


# ------------------------------------------------------------------- the reviewers' bypasses (PR 19 review)


def test_tableau_case_list_captions_are_read_through_their_wrappers() -> None:
    # One mark per case: ATTR(Age), ATTR(Sex), MDY(Onset Date), ATTR(City). Every caption used to slip past.
    fields = ["ATTR(Age)", "ATTR(Sex)", "MDY(Onset Date)", "ATTR(City)"]
    assert [person_kind(f) for f in fields] == ["age", "sex", "date", "city"]
    with pytest.raises(RowLevelRefused):
        check_grain("Case list", fields, [{"ATTR(Age)": 40, "ATTR(Sex)": "F"}])


def test_mostly_ones_is_one_row_per_case_even_with_one_two() -> None:
    fields = ["Age", "Sex", "Onset Date", "SUM(Number of Records)"]
    rows = [{"SUM(Number of Records)": 1}] * 99 + [{"SUM(Number of Records)": 2}]
    with pytest.raises(RowLevelRefused):
        check_grain("t", fields, rows)
    # without the date, the rule on counts alone still refuses it
    with pytest.raises(RowLevelRefused, match="counts 1 on most rows"):
        check_grain("t", ["Age", "Sex", "SUM(Number of Records)"], rows)


def test_person_fields_match_on_word_parts() -> None:
    fields = ["Patient Gender", "Age at Onset", "Residence ZIP", "Days Hospitalized"]
    assert [person_kind(f) for f in fields] == ["sex", "age", "zip", None]
    with pytest.raises(RowLevelRefused):
        check_grain("t", fields)


def test_a_measure_like_name_does_not_silence_the_guard() -> None:
    with pytest.raises(RowLevelRefused):
        check_grain("t", ["Age", "Sex", "Onset Date", "Value"])
    rows = [{"Age": 30, "Sex": "F", "Value": 1}, {"Age": 31, "Sex": "M", "Value": 1}]
    with pytest.raises(RowLevelRefused, match="counts 1 on most rows"):
        check_grain("t", ["Age", "Sex", "Value"], rows)


def test_a_rate_of_one_is_not_read_as_a_count_of_one() -> None:
    rows = [{"Rate": 1.0}, {"Rate": 1.0}]
    check_grain("t", ["Sex", "Race", "Rate"], rows)


def test_more_than_half_is_most() -> None:
    fields = ["Age", "Sex", "Count"]
    check_grain("t", fields, [{"Count": 1}, {"Count": 5}])  # half: not most
    with pytest.raises(RowLevelRefused):
        check_grain("t", fields, [{"Count": 1}, {"Count": 1}, {"Count": 5}])


@pytest.mark.parametrize(
    ("caption", "bare"),
    [("ATTR(Sex)", "Sex"), ("MDY(Onset Date)", "Onset Date"), ("ATTR(MDY(Onset Date)) (alias)", "Onset Date"),
     ("Age #2", "Age"), ("SUM(Cases)", "SUM(Cases)"), ("year ( Year )", "Year"), ("Location", "Location")],
)  # fmt: skip
def test_unwrap(caption: str, bare: str) -> None:
    assert unwrap(caption) == bare


@pytest.mark.parametrize(
    ("field", "kind"),
    [("Race", "race"), ("Hispanic or Latino", "ethnicity"), ("Res Zip", "zip"), ("Postal Code", "zip"),
     ("Town", "city"), ("Date of Death", "date"), ("Specimen Collection Date", "date"), ("Report_Date", "date"),
     ("SUM(Age)", None), ("CNT(Sex)", None), ("Year", None), ("WeekEnding Date", None), ("Diagnosis1", None),
     ("Confirmed & Probably Disease Date", None), ("County", None), ("c_Case Indicator", None)],
)  # fmt: skip
def test_person_kinds(field: str, kind: str | None) -> None:
    assert person_kind(field) == kind


# ------------------------------------------------------------------- every portal's pages are guarded


def _entry(portal: str):  # type: ignore[no-untyped-def]
    from tests.test_tableau import entry

    e = entry({"TByear": "a" * 64})
    return e.model_copy(update={"source": e.source.model_copy(update={"portal": portal})})


def _serve(monkeypatch: pytest.MonkeyPatch, kind: str, page: object) -> None:
    """Make the registered type ``kind`` hand back ``page``, so the core's guard is what is tested."""
    import dataclasses

    from health_data_registry import adapters

    t = adapters.TYPES[kind]
    monkeypatch.setitem(adapters.TYPES, kind, dataclasses.replace(t, pages=lambda read: iter([page])))


@pytest.mark.parametrize("portal", ["socrata", "https_csv", "who_gho", "tableau"])
def test_every_portals_pages_pass_the_guard(portal: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from health_data_registry import adapters
    from tests.test_tableau import raw

    entry = _entry(portal)
    rows = [{"age": "40", "sex": "F", "onset_date": "2026-01-02", "cases": "1"}]
    page = adapters.Page(fetch=raw("https://dash.example.gov/x", b"x"), records=rows)
    _serve(monkeypatch, entry.source.kind, page)
    with pytest.raises(RowLevelRefused, match=r"toy_dashboard \(dash.example.gov\)"):
        list(adapters.pages(entry, lambda u: page.fetch))
    fine = adapters.Page(fetch=page.fetch, records=[{"year": "2024", "cases": "1"}])
    _serve(monkeypatch, entry.source.kind, fine)
    assert list(adapters.pages(entry, lambda u: page.fetch)) == [fine]


def test_tableau_pages_are_guarded_per_worksheet_and_again_by_the_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from health_data_registry import adapters
    from tests.test_tableau import raw

    # Hard rule 4 has no opt-out (ADR 0016): the core guards each worksheet a page declares, and the page itself.
    sheet = adapters.GuardTable(
        "TB/TByear", ["ATTR(Age)", "ATTR(Sex)", "MDY(Onset Date)"], [{"ATTR(Age)": "40"}]
    )
    page = adapters.Page(
        fetch=raw("https://dash.example.gov/x", b"x"), records=[{"year": "2024"}], tables=(sheet,)
    )
    _serve(monkeypatch, "tableau", page)
    with pytest.raises(RowLevelRefused, match=r"TB/TByear"):
        list(adapters.pages(_entry("tableau"), lambda u: page.fetch))
    projected = adapters.Page(
        fetch=page.fetch, records=[{"ATTR(Age)": 1, "ATTR(Sex)": "F", "MDY(Onset Date)": "x"}]
    )
    _serve(monkeypatch, "tableau", projected)
    with pytest.raises(RowLevelRefused):
        list(adapters.pages(_entry("tableau"), lambda u: page.fetch))
