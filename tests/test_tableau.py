"""The Tableau adapter on tiny hand-written bootstrapSession answers (synthetic values, real structure)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from health_data_registry.adapters import Page, pages, publisher_fields
from health_data_registry.adapters import tableau as tb
from health_data_registry.adapters.row_guard import RowLevelRefused
from health_data_registry.registry import CatalogEntry, TableauView
from health_data_registry.store import RawFetch

DATA = {
    "integer": [2019, 2020, 120, 95, 40, 3],
    "real": [1500.5, 1484.25, 505.75],
    "cstring": ["Detroit", "Michigan", "<6", "12", "Feb 10, 2026"],
}


def col(caption: str | None, dtype: str | None, panes: list[int], cols: list[int]) -> dict[str, Any]:
    c: dict[str, Any] = {"paneIndices": panes, "columnIndices": cols, "fn": "[x]"}
    if caption is not None:
        c["fieldCaption"] = caption
    if dtype is not None:
        c["dataType"] = dtype
    return c


def pane(*columns: tuple[list[int], list[int]]) -> dict[str, Any]:
    return {"vizPaneColumns": [{"valueIndices": v, "aliasIndices": a} for v, a in columns]}


YEARLY = {
    "vizDataColumns": [
        col(None, None, [0], [0]),  # Tableau's tuple id: no caption, never read
        col("YEAR(Year)", "integer", [0], [1]),
        col("Location", "cstring", [0], [2]),
        col("SUM(Cases)", "integer", [0], [3]),
        col("SUM(Rate)", "real", [0], [4]),
    ],
    "paneColumnsList": [
        pane(([], []), ([0, 1, 0], []), ([], [0, 0, -2]), ([], [2, 3, 4]), ([0, 1, 2], [])),
    ],
}
# A count kept as text by the dashboard, suppressed in one row
SUPPRESSED = {
    "vizDataColumns": [
        col("Age Range", "cstring", [0], [0]),
        col("AGG(Count Suppressed)", "cstring", [0], [1]),
    ],
    "paneColumnsList": [pane(([], [0, 1]), ([], [2, 3]))],
}
# A label that looks like a marker ("< 20" age band) is a label: never flagged
LABELS = {
    "vizDataColumns": [col("Age Range", "cstring", [0], [0])],
    "paneColumnsList": [pane(([2], [2]))],
}
TIMESTAMP = {
    "vizDataColumns": [col("DAY(Date_Updated)", "cstring", [0], [0])],
    "paneColumnsList": [pane(([], [4]))],
}
PERSONS = {
    "vizDataColumns": [col("Age", "integer", [0], [0]), col("Sex", "cstring", [0], [1])],
    "paneColumnsList": [pane(([2, 3], []), ([], [0, 1]))],
}


def boot(sheets: dict[str, Any], data: dict[str, list[Any]] | None = None, *, zones: list[str] = ()) -> bytes:
    first = {
        "sheetName": "TB",
        "worldUpdate": {"applicationPresModel": {"workbookPresModel": {"dashboardPresModel": {
            "zones": {str(i): {"worksheet": z} for i, z in enumerate(zones)} | {"99": {"name": "a text zone"}}
        }}}},
    }  # fmt: skip
    data = DATA if data is None else data
    second = {"secondaryInfo": {"presModelMap": {
        "dataDictionary": {"presModelHolder": {"genDataDictionaryPresModel": {"dataSegments": {
            "0": {"dataColumns": [{"dataType": k, "dataValues": v} for k, v in data.items()]}
        }}}},
        "vizData": {"presModelHolder": {"genPresModelMapPresModel": {"presModelMap": {
            name: {"presModelHolder": {"genVizDataPresModel": {"paneColumnsData": cd}}} for name, cd in sheets.items()
        }}}},
    }}}  # fmt: skip
    out = ""
    for chunk in (first, second):
        text = json.dumps(chunk)
        out += f"{len(text)};{text}"
    return out.encode()


def test_yearly_worksheet_is_read_as_published() -> None:
    ws = tb.read_worksheets(boot({"TByear": YEARLY}))["TByear"]
    assert [c.caption for c in ws.columns] == ["YEAR(Year)", "Location", "SUM(Cases)", "SUM(Rate)"]
    assert ws.records == (
        {"YEAR(Year)": 2019, "Location (alias)": "Detroit", "SUM(Cases) (alias)": 120, "SUM(Rate)": 1500.5},
        {"YEAR(Year)": 2020, "Location (alias)": "Detroit", "SUM(Cases) (alias)": 95, "SUM(Rate)": 1484.25},
        # a negative alias index points into the string list (-2 -> "Michigan")
        {"YEAR(Year)": 2019, "Location (alias)": "Michigan", "SUM(Cases) (alias)": 40, "SUM(Rate)": 505.75},
    )


def test_suppressed_count_is_kept_as_a_flag_never_a_number() -> None:
    ws = tb.read_worksheets(boot({"Age": SUPPRESSED}))["Age"]
    assert ws.records[0] == {
        "Age Range (alias)": "Detroit", "AGG(Count Suppressed) (alias)": None, "AGG(Count Suppressed) flag": "<6"
    }  # fmt: skip
    assert ws.records[1] == {"Age Range (alias)": "Michigan", "AGG(Count Suppressed) (alias)": "12"}


def test_suppressed_value_field_is_emptied_with_its_alias() -> None:
    rec = tb._flag_suppressed({"SUM(Cases)": "<6", "SUM(Cases) (alias)": "<6", "SUM(Rate)": 3.5})
    assert rec == {"SUM(Cases)": None, "SUM(Cases) (alias)": None, "SUM(Cases) flag": "<6", "SUM(Rate)": 3.5}


def test_suppressed_alias_leaves_a_numeric_value_alone() -> None:
    rec = tb._flag_suppressed({"SUM(Cases)": 3, "SUM(Cases) (alias)": "<6"})
    assert rec == {"SUM(Cases)": 3, "SUM(Cases) (alias)": None, "SUM(Cases) flag": "<6"}


def test_a_label_that_looks_like_a_marker_stays_a_label() -> None:
    ws = tb.read_worksheets(boot({"Ages": LABELS}, {"cstring": ["a", "b", "< 20"]}))["Ages"]
    assert ws.records == ({"Age Range": "< 20", "Age Range (alias)": "< 20"},)


@pytest.mark.parametrize("marker", ["<6", "< 6", "<11", ">=5", "≤5", " <6 ", "<0.5"])
def test_suppression_markers(marker: str) -> None:
    assert tb.SUPPRESSED.match(marker)


@pytest.mark.parametrize("value", ["6", "Detroit", "<", "<six", "6<", "1,204"])
def test_not_suppression_markers(value: str) -> None:
    assert not tb.SUPPRESSED.match(value)


def test_layout_hash_follows_the_columns() -> None:
    ws = tb.read_worksheets(boot({"TByear": YEARLY}))["TByear"]
    text = json.dumps([["YEAR(Year)", "integer"], ["Location", "cstring"], ["SUM(Cases)", "integer"],
                       ["SUM(Rate)", "real"]], separators=(",", ":"))  # fmt: skip
    assert ws.layout_hash == hashlib.sha256(text.encode()).hexdigest()
    renamed = json.loads(json.dumps(YEARLY))
    renamed["vizDataColumns"][3]["fieldCaption"] = "SUM(Case Count)"
    assert tb.read_worksheets(boot({"TByear": renamed}))["TByear"].layout_hash != ws.layout_hash


def test_dashboard_worksheets_from_zones() -> None:
    chunks = tb.split_chunks(boot({}, zones=["TByear", "TB Age", "TByear"]))
    assert tb.dashboard_worksheets(chunks) == ["TB Age", "TByear"]
    assert tb.dashboard_worksheets([{"worldUpdate": {}}]) == []


def test_worksheets_without_data_are_left_out() -> None:
    empty = {"vizDataColumns": [col("Logo", "cstring", [0], [0])], "paneColumnsList": [pane(([], []))]}
    assert set(tb.read_worksheets(boot({"Logo": empty, "Stamp": TIMESTAMP}))) == {"Stamp"}


def test_a_column_in_two_panes_is_read_in_both() -> None:
    two = {
        "vizDataColumns": [
            col("YEAR(Year)", "integer", [0, 1], [0, 0]),
            col("SUM(Cases)", "integer", [1], [1]),
        ],
        "paneColumnsList": [pane(([0], [])), pane(([1], []), ([2], []))],
    }
    ws = tb.read_worksheets(boot({"Two": two}))["Two"]
    assert ws.records == ({"YEAR(Year)": 2019}, {"YEAR(Year)": 2020, "SUM(Cases)": 120})


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"", "empty"),
        (b"<html>", "no <length>; prefix"),
        (b"50;{}", "cut short"),
        (b"3;{x}", "not valid JSON"),
        (b"2;[]", "not a JSON object"),
    ],
)
def test_not_a_bootstrap_session(body: bytes, message: str) -> None:
    with pytest.raises(tb.TableauLayoutError, match=message):
        tb.split_chunks(body)


def test_split_chunks_reads_text_and_bytes_alike() -> None:
    assert tb.split_chunks('2;{}7;{"a":1}') == tb.split_chunks(b'2;{}7;{"a":1}') == [{}, {"a": 1}]


def test_ragged_pane_is_refused() -> None:
    ragged = {
        "vizDataColumns": [col("A", "integer", [0], [0]), col("B", "integer", [0], [1])],
        "paneColumnsList": [pane(([0, 1], []), ([2], []))],
    }
    with pytest.raises(tb.TableauLayoutError, match="different lengths"):
        tb.read_worksheets(boot({"R": ragged}))


def test_index_outside_the_dictionary_is_refused() -> None:
    bad = {"vizDataColumns": [col("A", "integer", [0], [0])], "paneColumnsList": [pane(([99], []))]}
    with pytest.raises(tb.TableauLayoutError, match="outside the data dictionary"):
        tb.read_worksheets(boot({"B": bad}))


def test_column_pointing_past_its_pane_is_refused() -> None:
    bad = {"vizDataColumns": [col("A", "integer", [0], [3])], "paneColumnsList": [pane(([0], []))]}
    with pytest.raises(tb.TableauLayoutError, match="past its pane"):
        tb.read_worksheets(boot({"B": bad}))


def test_segments_are_joined_in_numeric_order() -> None:
    second = {"secondaryInfo": {"presModelMap": {"dataDictionary": {"presModelHolder": {
        "genDataDictionaryPresModel": {"dataSegments": {
            "10": {"dataColumns": [{"dataType": "integer", "dataValues": [3]}]},
            "2": {"dataColumns": [{"dataType": "integer", "dataValues": [2]}]},
        }}}}}}}  # fmt: skip
    assert tb._data_dictionary([second]) == {"integer": [2, 3]}


# --------------------------------------------------------------------------------------------- pages


def entry(
    worksheets: dict[str, str], views: tuple[str, ...] = ("TB",), suppressed_by: dict[str, str] | None = None
) -> CatalogEntry:
    return CatalogEntry.model_validate({
        "id": "toy_dashboard", "title": "Toy dashboard", "agency": "City", "publisher": "City health department",
        "level": "city", "research_areas": ["infectious_disease"], "years": {"start": 2019},
        "geography": ["city"], "shape": "rate_table", "grain": "One row per place and year.", "refresh": "annual",
        "licence": {"id": "no-terms-stated", "redistribution": False, "terms_url": "https://example.gov/"},
        "citation": "City.", "codebook_url": "https://example.gov/",
        "source": {"portal": "tableau", "host": "dash.example.gov", "dataset": "Toy", "government_host": True,
                   "browser": True, "landing_url": "https://dash.example.gov/views/Toy/Main",
                   "api_url": "https://dash.example.gov/views/Toy",
                   "tableau": {"views": [{"view": v, "worksheets": worksheets,
                                          "suppressed_by": suppressed_by or {}} for v in views]}},
        "contract": "contracts/toy_dashboard.odcs.yaml", "semantic_model": "semantic/toy_dashboard.yaml",
        "status": "draft",
    })  # fmt: skip


def raw(url: str, body: bytes) -> RawFetch:
    return RawFetch(url=url, content=body, sha256=hashlib.sha256(body).hexdigest(),
                    retrieved_at=datetime(2026, 11, 24, tzinfo=UTC))  # fmt: skip


YEARLY_HASH = tb.read_worksheets(boot({"TByear": YEARLY}))["TByear"].layout_hash


def serve(body: bytes, seen: list[str] | None = None) -> Any:
    def get(url: str) -> RawFetch:
        if seen is not None:
            seen.append(url)
        return raw(url, body)

    return get


def test_pages_read_the_catalogued_worksheets_with_their_layout() -> None:
    seen: list[str] = []
    got = list(
        pages(entry({"TByear": YEARLY_HASH}), serve(boot({"TByear": YEARLY, "Stamp": TIMESTAMP}), seen))
    )
    assert seen == ["https://dash.example.gov/views/Toy/TB?:embed=y&:showVizHome=no"]
    (page,) = got
    assert isinstance(page, Page) and page.layout == {"TB/TByear": YEARLY_HASH}
    assert len(page.records) == 3 and all(
        r["view"] == "TB" and r["worksheet"] == "TByear" for r in page.records
    )


def test_pages_keep_only_the_fields_asked_for_and_stop_at_max_rows() -> None:
    e = entry({"TByear": YEARLY_HASH}, views=("TB", "TB2"))
    got = list(tb.pages(e, serve(boot({"TByear": YEARLY})), max_rows=4, fields=["worksheet", "SUM(Rate)"]))
    assert [len(p.records) for p in got] == [3, 1]
    assert got[0].records[0] == {"worksheet": "TByear", "SUM(Rate)": 1500.5}
    assert list(tb.pages(e, serve(boot({"TByear": YEARLY})), max_rows=0))[0].records == []


def test_a_redesigned_worksheet_stops_the_load() -> None:
    with pytest.raises(tb.TableauLayoutError, match="redesigned"):
        list(tb.pages(entry({"TByear": "0" * 64}), serve(boot({"TByear": YEARLY}))))


def test_a_missing_worksheet_stops_the_load() -> None:
    with pytest.raises(tb.TableauLayoutError, match="no longer has the worksheet"):
        list(tb.pages(entry({"Gone": YEARLY_HASH}), serve(boot({"TByear": YEARLY}))))


def test_a_person_level_worksheet_is_never_read() -> None:
    h = tb.read_worksheets(boot({"Cases": PERSONS}))["Cases"].layout_hash
    with pytest.raises(RowLevelRefused):
        list(tb.pages(entry({"Cases": h}), serve(boot({"Cases": PERSONS}))))


def test_publisher_fields_lists_the_catalogued_worksheets_fields() -> None:
    fields = publisher_fields(
        entry({"TByear": YEARLY_HASH, "Gone": YEARLY_HASH}), serve(boot({"TByear": YEARLY}))
    )
    assert fields == {
        "view",
        "worksheet",
        "YEAR(Year)",
        "Location (alias)",
        "SUM(Cases) (alias)",
        "SUM(Rate)",
    }


def test_view_url_never_names_a_workbook_file() -> None:
    url = tb.view_url(entry({"TByear": YEARLY_HASH}), "TB")
    assert url.startswith("https://dash.example.gov/views/Toy/TB?") and ".twb" not in url


@given(st.lists(st.integers(min_value=0, max_value=10_000), min_size=1, max_size=30))
def test_counts_round_trip_unchanged(counts: list[int]) -> None:
    sheet = {
        "vizDataColumns": [col("SUM(Cases)", "integer", [0], [0])],
        "paneColumnsList": [pane((list(range(len(counts))), []))],
    }
    ws = tb.read_worksheets(boot({"S": sheet}, {"integer": counts}))["S"]
    assert [r["SUM(Cases)"] for r in ws.records] == counts


# A daily count the chart plots as a number, with a tooltip that shows it as "<16" on the same mark
CURVE = {
    "vizDataColumns": [
        col("YEAR(Year)", "integer", [0], [0]),
        col("AGG(Case Count)", "integer", [0], [1]),
        col("AGG(Tooltip Cases)", "cstring", [0], [2]),
    ],
    "paneColumnsList": [pane(([0, 1], []), ([2, 5], []), ([], [2, 3]))],
}
CURVE_DATA = {"integer": [2019, 2020, 120, 95, 40, 3], "cstring": ["Detroit", "Michigan", "<16", "12"]}
CURVE_HASH = tb.read_worksheets(boot({"Curve": CURVE}, CURVE_DATA))["Curve"].layout_hash


def test_a_count_shown_suppressed_on_its_mark_is_never_read_as_a_number() -> None:
    e = entry({"Curve": CURVE_HASH}, suppressed_by={"AGG(Case Count)": "AGG(Tooltip Cases)"})
    (page,) = tb.pages(e, serve(boot({"Curve": CURVE}, CURVE_DATA)))
    first, second = page.records
    assert first["AGG(Case Count)"] is None and first["AGG(Case Count) flag"] == "<16"
    assert second["AGG(Case Count)"] == 3 and "AGG(Case Count) flag" not in second
    assert "AGG(Case Count) flag" in tb.publisher_fields(e, serve(boot({"Curve": CURVE}, CURVE_DATA)))


def test_without_a_declaration_the_count_is_read_as_published() -> None:
    (page,) = tb.pages(entry({"Curve": CURVE_HASH}), serve(boot({"Curve": CURVE}, CURVE_DATA)))
    assert [r["AGG(Case Count)"] for r in page.records] == [120, 3]
    assert page.records[0]["AGG(Tooltip Cases) flag"] == "<16"
    assert "AGG(Case Count) flag" not in tb.publisher_fields(
        entry({"Curve": CURVE_HASH}), serve(boot({"Curve": CURVE}, CURVE_DATA))
    )


def test_apply_suppressed_by_empties_the_alias_too() -> None:
    rec = {"AGG(N)": 4, "AGG(N) (alias)": 4, "AGG(T) flag": "<6", "Other": 1}
    assert tb.apply_suppressed_by(rec, {"AGG(N)": "AGG(T)"}) == {
        "AGG(N)": None, "AGG(N) (alias)": None, "AGG(N) flag": "<6", "AGG(T) flag": "<6", "Other": 1
    }  # fmt: skip
    only_alias = {"AGG(N) (alias)": 4, "AGG(T) flag": "<6"}
    assert tb.apply_suppressed_by(only_alias, {"AGG(N)": "AGG(T)"}) == {
        "AGG(N) (alias)": None, "AGG(N) flag": "<6", "AGG(T) flag": "<6"
    }  # fmt: skip
    unflagged = {"AGG(N)": 4, "AGG(T) flag": ""}
    assert tb.apply_suppressed_by(unflagged, {"AGG(N)": "AGG(T)"}) == unflagged
    assert tb.apply_suppressed_by(rec, {}) == rec


@pytest.mark.parametrize("bad", [{"": "AGG(T)"}, {"AGG(N)": " "}, {"AGG(N)": "AGG(N)"}])
def test_suppressed_by_must_name_two_fields(bad: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="suppressed_by"):
        TableauView(view="V", worksheets={"S": "a" * 64}, suppressed_by=bad)


def test_suppressed_by_accepts_two_different_fields() -> None:
    v = TableauView(view="V", worksheets={"S": "a" * 64}, suppressed_by={"AGG(N)": "AGG(T)"})
    assert v.suppressed_by == {"AGG(N)": "AGG(T)"}
