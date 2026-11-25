"""Running totals with late reports and a next-year table (the shape of the NNDSS weekly tables).

Regression: adding up weekly counts under-counts badly, because late reports land in the year-to-date
column, not in the week (Michigan C. auris 2023 read 62 that way; its own year-to-date was 103).

Review findings covered here, each on synthetic data shaped like the real case:
1. a running total is labelled with the week its number comes from (Minnesota C. auris 2025: 1 at week 38);
2. an empty restatement never replaces a real count (California C. auris 2022: 495, next table flagged N);
3. a restatement covers a year only once it reaches that year's own last week (2025 has 53 weeks);
4. the year's own total and next year's figure are both returned, and neither is called final;
8. the publisher's flags come back as published, with the codebook's words;
10. a renamed area (Northern Mariana Islands) joins its next-year table.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry import tools
from health_data_registry.query import Engine, QueryRefused, compile_metric
from health_data_registry.registry import Registry, RegistryError, load_registry
from health_data_registry.staging import PublisherDataRefused
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import CATALOG as BASE_CATALOG
from tests.conftest import raw_fetch, write_registry

CATALOG = BASE_CATALOG.replace("location_column: reporting_area", "location_column: area")

FLAG_CODES = (
    '{property: flagCodes, value: {"-": "no reported cases", "U": "unavailable", '
    '"N": "not reportable in this jurisdiction"}}'
)

CONTRACT = f"""\
apiVersion: v3.0.2
kind: DataContract
id: toy_cases
version: 0.1.0
status: draft
customProperties:
  - {{property: unreliableRule, value: {{kind: none, description: "None stated (toy)."}}}}
schema:
  - name: toy_cases
    physicalName: toy_cases
    customProperties: [{{property: totalRows, value: "upper(area) = 'US RESIDENTS'"}}]
    properties:
      - name: area
        physicalType: VARCHAR
        logicalType: string
        required: true
        description: Reporting area.
        customProperties:
          - {{property: role, value: dimension}}
          - {{property: renamedLabels, value: {{"Commonwealth of Northern Mariana Islands": "Northern Mariana Islands"}}}}
          - {{property: partialPlaces, value: {{"New York": "New York State excluding New York City"}}}}
      - {{name: year, physicalType: INTEGER, logicalType: integer, required: true, description: MMWR year., customProperties: [{{property: role, value: dimension}}]}}
      - {{name: week, physicalType: INTEGER, logicalType: integer, required: true, description: MMWR week., customProperties: [{{property: role, value: dimension}}]}}
      - {{name: wk, physicalType: BIGINT, logicalType: integer, description: Cases first reported for the week., customProperties: [{{property: role, value: measure}}]}}
      - {{name: ytd, physicalType: BIGINT, logicalType: integer, description: Year-to-date cases., customProperties: [{{property: role, value: measure}}]}}
      - {{name: ytd_flag, physicalType: VARCHAR, logicalType: string, description: Why ytd is empty., customProperties: [{{property: role, value: attribute}}, {{property: flagFor, value: ytd}}, {FLAG_CODES}]}}
      - {{name: prior_ytd, physicalType: BIGINT, logicalType: integer, description: Last year's cases to the same week., customProperties: [{{property: role, value: measure}}]}}
      - {{name: prior_flag, physicalType: VARCHAR, logicalType: string, description: Why prior_ytd is empty., customProperties: [{{property: role, value: attribute}}, {{property: flagFor, value: prior_ytd}}, {FLAG_CODES}]}}
"""

SEMANTIC = """\
dataset: toy_cases
version: 0.1.0
dimensions:
  - {name: state, column: area, type: categorical}
  - {name: year, column: year, type: time}
  - {name: week, column: week, type: time}
measures:
  - {name: wk, column: wk, kind: count}
  - {name: ytd, column: ytd, kind: running_count}
  - {name: prior_ytd, column: prior_ytd, kind: running_count}
metrics:
  - name: annual
    description: Annual cases
    measure: ytd
    aggregation: cumulative_latest
    order_by: week
    restated_by: {measure: prior_ytd, resolution_policy: require_domain_review}
  - name: annual_own_only
    description: Annual cases, no restatement
    measure: ytd
    aggregation: cumulative_latest
    order_by: week
  - {name: weekly, description: Weekly cases, measure: wk, aggregation: sum}
series:
  - {condition: legionellosis, label: Legionellosis, metric: annual, measure: cases, location_dimension: state, time_dimension: year}
"""


def rec(
    area: str, year: int, week: int, wk: int | None, ytd: Any, prior: Any, flag: str | None = None,
    pflag: str | None = None,
) -> dict[str, object]:  # fmt: skip
    return {"area": area, "year": year, "week": week, "wk": wk, "ytd": ytd, "ytd_flag": flag,
            "prior_ytd": prior, "prior_flag": pflag}  # fmt: skip


# Toy "years": 2022 and 2024 have 3 weeks, 2023 has 4 (a 53-week year), 2025 is still open at week 2.
RECORDS = [
    # Michigan 2022: weekly counts add to 3, late reports bring the running total to 10. Through 2024 the
    # publisher's flag is '-' even beside a count; it means nothing there.
    rec("MICHIGAN", 2022, 1, 1, 4, None, "-", "-"),
    rec("MICHIGAN", 2022, 2, 1, 7, None, "-", "-"),
    rec("MICHIGAN", 2022, 3, 1, "10.0", None, "-", "-"),  # NNDSS from 2025 sends counts as text like "10.0"
    rec("Michigan", 2023, 1, 2, 5, 6),  # the publisher re-cased the label this year
    rec("Michigan", 2023, 2, None, 9, 9),
    rec("Michigan", 2023, 3, 1, 15, 12),
    rec("Michigan", 2023, 4, 1, 16, 12),
    rec("Michigan", 2024, 1, 1, 2, 7),
    rec("Michigan", 2024, 2, 1, 3, 11),
    rec("Michigan", 2024, 3, 0, 3, 14),  # the 2024 table reaches week 3 of 2023's 4
    rec("Michigan", 2025, 1, 1, 1, 3),
    rec("Michigan", 2025, 2, None, None, 4, "U"),  # running total stops: last number is week 1's
    # Ohio 2023 has a count; the 2024 table's prior-year column is empty with flag N, and must not replace it.
    rec("OHIO", 2023, 1, 1, 1, None),
    rec("OHIO", 2023, 4, 1, 8, None),
    rec("OHIO", 2024, 1, None, None, None, "-", "N"),
    rec("OHIO", 2024, 3, None, None, None, "-", "N"),
    rec("Ohio", 2025, 1, 2, 2, None, None, "N"),
    rec("Ohio", 2025, 2, 3, 5, None, None, "N"),
    # The territory is renamed in 2025.
    rec("NORTHERN MARIANA ISLANDS", 2024, 1, 1, 1, None),
    rec("NORTHERN MARIANA ISLANDS", 2024, 3, 1, 2, None),
    rec("Commonwealth of Northern Mariana Islands", 2025, 1, 0, 0, 2),
    rec("Commonwealth of Northern Mariana Islands", 2025, 2, 1, 1, 3),
    rec("New York", 2024, 3, 1, 6, None),
    rec("US RESIDENTS", 2023, 4, 9, 999, None),
]


def _registry(tmp_path: Path, semantic: str = SEMANTIC) -> Registry:
    return load_registry(
        write_registry(tmp_path / "reg", catalog=CATALOG, contract=CONTRACT, semantic=semantic)
    )


def _setup(tmp_path: Path, records: list[dict[str, object]] = RECORDS) -> tuple[Registry, Engine]:
    reg = _registry(tmp_path)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, records)
    build_snapshot(
        lake, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/resource/x.json", records)], [f]
    )
    return reg, Engine(registry=reg, lake=lake)


def _series(reg: Registry, eng: Engine, places: list[str], **kw: Any) -> dict[str, Any]:
    out = tools.get_series(reg, eng, condition="legionellosis", locations=places, fmt="json", **kw)
    assert isinstance(out, dict)
    return out


OWN, NEXT = "year_end_ytd", "subsequent_year_restatement"
UNV = "unverified_restatement: as published in next year's table"
NOT_FINAL = "; not a final disease count"


def rev(year: int, status: str) -> str:
    """The period_status of a subsequent_year_restatement row, as get_series labels it."""
    return (
        f"CDC's revised provisional figure, from the {year + 1} table ({status}); kept apart from the year-end "
        "figure, never merged with it"
    )


def _obs(out: dict[str, Any]) -> list[list[Any]]:
    """location, year, value, observation_type, period_status, flag."""
    return [[r[0], r[1], r[2], r[5], r[6], r[7]] for r in out["rows"]]


def test_each_year_keeps_its_own_total_and_next_years_figure_as_separate_observations(tmp_path: Path) -> None:
    reg, eng = _setup(tmp_path)
    out = _series(reg, eng, ["Michigan"])
    assert out["columns"][:8] == ["location", "year", "value", "lower", "upper", "observation_type",
                                  "period_status", "flag"]  # fmt: skip
    assert _obs(out) == [
        # Own total 10 (not the weekly sum 3); next year's figure 12 is a separate, unverified observation.
        ["Michigan", 2022, 10, OWN, "provisional: as published at year end (through week 3)", None],
        ["Michigan", 2022, 12, NEXT, rev(2022, f"{UNV} (through week 4){NOT_FINAL}"), None],
        # A 4-week (53-week-like) year: the 2024 table reaches week 3 only, so it doesn't cover the year.
        ["Michigan", 2023, 16, OWN, "provisional: as published at year end (through week 4)", None],
        ["Michigan", 2023, 14, NEXT,
         rev(2023, f"{UNV} (through week 3); covers weeks 1-3 of 4 only, not the whole year{NOT_FINAL}"), None],
        ["Michigan", 2024, 3, OWN, "provisional: as published at year end (through week 3)", None],
        ["Michigan", 2024, 4, NEXT,
         rev(2024, f"{UNV} (through week 2); covers weeks 1-2 of 3 only, not the whole year{NOT_FINAL}"), None],
        # The number comes from week 1; week 2 is flagged U, and the row says so in the codebook's words.
        ["Michigan", 2025, 1, OWN, "incomplete: running total last published at week 1 of 2; later weeks have no "
         "number (flag at week 2: 'U' = unavailable), so this is not the year total", "U"],
    ]  # fmt: skip
    assert not any(str(c).startswith("final") or " final:" in str(c) for r in out["rows"] for c in r)
    notes = " ".join(out["notes"])
    assert "it is not a final disease count, and neither is final until a domain reviewer approves" in notes
    assert "'-' = no reported cases; 'U' = unavailable; 'N' = not reportable in this jurisdiction" in notes


def test_empty_restatement_never_replaces_a_count(tmp_path: Path) -> None:
    reg, eng = _setup(tmp_path)
    out = _series(reg, eng, ["Ohio"])
    no_number = "unverified_restatement: next year's table has no number for this year (flag: 'N' = not reportable in this jurisdiction)"
    assert [r[1:] for r in _obs(out)] == [
        [2023, 8, OWN, "provisional: as published at year end (through week 4)", None],
        [2023, None, NEXT, rev(2023, no_number), "N"],
        [2024, None, OWN, "no number published through week 3 (flag: '-' = no reported cases)", "-"],
        [2024, None, NEXT, rev(2024, no_number), "N"],
        [2025, 5, OWN, "provisional: running total so far (through week 2)", None],
    ]  # fmt: skip


def test_renamed_area_joins_its_next_year_table(tmp_path: Path) -> None:
    reg, eng = _setup(tmp_path)
    for asked in (["Northern Mariana Islands"], ["Commonwealth of Northern Mariana Islands"]):
        out = _series(reg, eng, asked)
        assert [r[:4] for r in _obs(out)] == [
            ["Northern Mariana Islands", 2024, 2, OWN],
            ["Northern Mariana Islands", 2024, 3, NEXT],
            ["Northern Mariana Islands", 2025, 1, OWN],
        ]


def test_period_filter_still_sees_next_years_table(tmp_path: Path) -> None:
    reg, eng = _setup(tmp_path)
    out = _series(reg, eng, ["Michigan"], year_start=2023, year_end=2023)
    assert [r[:4] for r in _obs(out)] == [["Michigan", 2023, 16, OWN], ["Michigan", 2023, 14, NEXT]]


def test_weekly_counts_are_never_the_annual_figure(tmp_path: Path) -> None:
    reg, eng = _setup(tmp_path)
    cm = compile_metric(reg.get("toy_cases"), "annual", group_by=["state", "year"])
    assert '"wk"' not in cm.sql and "sum(" not in cm.sql
    weekly = eng.query_metric("toy_cases", "weekly", filters={"state": ["MICHIGAN"]}, group_by=["year"])
    assert [r[:2] for r in weekly["rows"]] == [[2022, 3]]  # the trap the running total avoids


def test_without_a_restatement_no_year_is_said_to_be_over(tmp_path: Path) -> None:
    reg, eng = _setup(tmp_path)
    out = eng.query_metric("toy_cases", "annual_own_only", filters={"state": ["MICHIGAN", "Michigan"]},
                           group_by=["state", "year"])  # fmt: skip
    assert out["columns"] == ["state", "year", "annual_own_only", "period_status", "flag"]
    assert [r[:4] for r in out["rows"]][:2] == [
        ["MICHIGAN", 2022, 10, "provisional: running total so far (through week 3)"],
        ["MICHIGAN", 2023, 16, "provisional: running total so far (through week 4)"],
    ]
    assert "restatement" not in out["method"]
    assert out["method"] == (
        "Running total (ytd) read at the last week of each year that has a number, labelled with that week; "
        "publisher total rows excluded. Weekly counts are never added up, because late reports land in the "
        "running total, not the week."
    )


def test_method_says_the_two_figures_are_never_swapped(tmp_path: Path) -> None:
    reg, _ = _setup(tmp_path)
    cm = compile_metric(reg.get("toy_cases"), "annual", group_by=["state", "year"])
    assert cm.columns == ["state", "year", "annual", "period_status", "flag", "restated", "restated_status",
                          "restated_flag"]  # fmt: skip
    assert cm.method == (
        "Running total (ytd) read at the last week of each year that has a number, labelled with that week; "
        "next year's table's figure for the same year (prior_ytd) is kept as a separate observation, an "
        "unverified restatement, with the week it reaches. The two are never swapped or combined and neither is "
        "final until a domain reviewer approves that they count the same thing; publisher total rows excluded. "
        "Weekly counts are never added up, because late reports land in the running total, not the week."
    )


def test_a_restatement_needs_a_resolution_policy(tmp_path: Path) -> None:
    bad = SEMANTIC.replace(", resolution_policy: require_domain_review", "")
    with pytest.raises(RegistryError, match="resolution_policy"):
        _registry(tmp_path, bad)
    final = SEMANTIC.replace("require_domain_review", "treat_as_final")
    with pytest.raises(RegistryError, match="resolution_policy"):
        _registry(tmp_path / "x", final)


def test_total_rows_are_left_out(tmp_path: Path) -> None:
    reg, eng = _setup(tmp_path)
    out = eng.query_metric("toy_cases", "annual", group_by=["state", "year"])
    assert all(r[0] != "US RESIDENTS" for r in out["rows"])


@pytest.mark.parametrize(
    ("group_by", "period", "msg"),
    [
        (["state"], None, "is a running total, so it is read once per year; group by year."),
        (
            ["state", "week"],
            {"dimension": "week"},
            "annual can't be grouped by week, the within-period order.",
        ),
    ],
)
def test_cumulative_refusals(tmp_path: Path, group_by: list[str], period: Any, msg: str) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(QueryRefused) as e:
        compile_metric(reg.get("toy_cases"), "annual", group_by=group_by, period=period)
    assert e.value.code == "needs_period"
    assert str(e.value).endswith(msg)


@pytest.mark.parametrize("kind", ["rate", "count", "maximum"])
def test_cumulative_needs_a_running_count(tmp_path: Path, kind: str) -> None:
    bad = SEMANTIC.replace(
        "- {name: ytd, column: ytd, kind: running_count}", f"- {{name: ytd, column: ytd, kind: {kind}}}"
    )
    with pytest.raises(RegistryError, match="needs a running_count measure"):
        _registry(tmp_path, bad)


def test_a_restatement_must_be_a_running_count(tmp_path: Path) -> None:
    bad = SEMANTIC.replace(
        "- {name: prior_ytd, column: prior_ytd, kind: running_count}",
        "- {name: prior_ytd, column: prior_ytd, kind: count}",
    )
    with pytest.raises(RegistryError, match="restated_by needs a running_count"):
        _registry(tmp_path, bad)


def test_a_running_total_without_its_place_grouped_is_refused(tmp_path: Path) -> None:
    """Regression (dbt review 1): grouped by year alone, arg_max picked one area's total (2023: 16, not 24)."""
    _, eng = _setup(tmp_path)
    with pytest.raises(QueryRefused) as e:
        eng.query_metric("toy_cases", "annual", group_by=["year"])
    assert e.value.code == "needs_grouping" and "group by state or filter each to one value" in str(e.value)
    with pytest.raises(QueryRefused):
        eng.query_metric("toy_cases", "annual", filters={"state": ["Ohio", "OHIO"]}, group_by=["year"])
    one = eng.query_metric("toy_cases", "annual", filters={"state": "OHIO"}, group_by=["year"])
    assert [r[:2] for r in one["rows"]][0] == [2023, 8]  # Ohio; Michigan is 16, so 24 in all, never 16


YTD_SEMANTIC = SEMANTIC.replace(
    "  - {name: weekly,",
    "  - {name: ytd_sum, description: Year to date at one week, measure: ytd, aggregation: sum,\n"
    "     single_value_of: [week]}\n  - {name: weekly,",
)


def test_a_summed_running_total_needs_one_week(tmp_path: Path) -> None:
    """dbt review 2: year to date is added across places only at a single week, never across weeks."""
    reg = _registry(tmp_path, YTD_SEMANTIC)
    ds = reg.get("toy_cases")
    for kw in ({}, {"filters": {"week": [1, 2]}}, {"filters": {"week": 1}, "group_by": ["week"]}):
        with pytest.raises(QueryRefused) as e:
            compile_metric(ds, "ytd_sum", **kw)  # type: ignore[arg-type]
        assert e.value.code == "needs_single_value" and "count the same cases many times" in str(e.value)
    assert "sum(" in compile_metric(ds, "ytd_sum", filters={"week": 3}, group_by=["year"]).sql
    with pytest.raises(RegistryError, match="name the dimension it is read at in single_value_of"):
        _registry(tmp_path / "a", YTD_SEMANTIC.replace("\n     single_value_of: [week]", ""))
    with pytest.raises(RegistryError, match="single_value_of names unknown dimension"):
        _registry(tmp_path / "b", YTD_SEMANTIC.replace("single_value_of: [week]", "single_value_of: [day]"))
    with pytest.raises(RegistryError, match="would sum a maximum"):
        _registry(
            tmp_path / "c",
            YTD_SEMANTIC.replace(
                "- {name: ytd, column: ytd, kind: running_count}", "- {name: ytd, column: ytd, kind: maximum}"
            )
            .replace("aggregation: cumulative_latest", "aggregation: none")
            .replace("    order_by: week\n", "")
            .replace("    restated_by: {measure: prior_ytd, resolution_policy: require_domain_review}\n", ""),
        )


def test_flags_are_kept_as_published(tmp_path: Path) -> None:
    _, eng = _setup(tmp_path)
    rows = eng.connect().execute("SELECT DISTINCT ytd_flag FROM toy_cases ORDER BY 1").fetchall()
    assert rows == [("-",), ("U",), (None,)]


def test_a_flag_column_cant_be_missing_coded(tmp_path: Path) -> None:
    bad = CONTRACT.replace(
        "{property: flagFor, value: ytd}",
        '{property: flagFor, value: ytd}, {property: missingCodes, value: ["U"]}',
    )
    with pytest.raises(RegistryError, match="flag column keeps its codes"):
        load_registry(write_registry(tmp_path, catalog=CATALOG, contract=bad, semantic=SEMANTIC))


def test_text_counts_with_a_fraction_stop_the_load(tmp_path: Path) -> None:
    with pytest.raises(
        PublisherDataRefused,
        match="^toy_cases: the publisher's data has a value in ytd that has a fraction, so it isn't a whole count$",
    ):
        _setup(tmp_path, [rec("Michigan", 2025, 1, 1, "1.5", None)])
    assert not list(tmp_path.rglob("data.duckdb"))  # nothing kept


def test_compiled_running_total_sql_is_exact(tmp_path: Path) -> None:
    """The whole compiled query, pinned: a change to it should be a reviewed change to tests/golden."""
    reg = _registry(tmp_path)
    cm = compile_metric(
        reg.get("toy_cases"), "annual", group_by=["state", "year"], period={"start": 2023, "end": 2023}
    )
    golden = (
        (Path(__file__).parent / "golden" / "cumulative_annual.sql").read_text().strip().replace("\n", " ")
    )
    assert cm.sql == golden
    assert cm.params == [2023, 2023]


SEQ_SEMANTIC = SEMANTIC.replace(
    "  - {name: week, column: week, type: time}\n",
    "  - {name: week, column: week, type: time}\n  - {name: epi_seq, column: week, type: time}\n"
    "  - {name: span_id, column: year, type: time}\n",
).replace(
    "  - {name: weekly,",
    "  - name: by_seq\n    description: Running total by another order\n    measure: ytd\n"
    "    aggregation: cumulative_latest\n    order_by: epi_seq\n    exclude_total_rows: false\n  - {name: weekly,",
)


def test_other_period_and_order_names_read_plainly(tmp_path: Path) -> None:
    records = [
        rec("Ohio", 2023, 1, 1, 1, None),
        rec("Ohio", 2023, 2, 1, 2, None),
        rec("US RESIDENTS", 2023, 2, 1, 9, None),
    ]
    reg = _registry(tmp_path, SEQ_SEMANTIC)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, records)
    build_snapshot(
        lake, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/resource/x.json", records)], [f]
    )
    eng = Engine(registry=reg, lake=lake)
    out = eng.query_metric(
        "toy_cases", "by_seq", group_by=["state", "span_id"], period={"dimension": "span_id"}
    )
    assert [r[:4] for r in out["rows"]] == [
        ["OHIO", 2023, 2, "provisional: running total so far (through epi seq 2)"],
        [
            "US RESIDENTS",
            2023,
            9,
            "provisional: running total so far (through epi seq 2)",
        ],  # totals kept here
    ]
    assert out["method"] == (
        "Running total (ytd) read at the last epi seq of each period that has a number, labelled with that epi "
        "seq. Weekly counts are never added up, because late reports land in the running total, not the week."
    )
    with pytest.raises(QueryRefused) as e:
        compile_metric(reg.get("toy_cases"), "by_seq", group_by=["state"], period={"dimension": "span_id"})
    assert str(e.value) == "by_seq is a running total, so it is read once per span_id; group by span_id."


ALL_CODES = (
    '{property: flagCodes, value: {"-": "no reported cases", "U": "unavailable", '
    '"N": "not reportable in this jurisdiction", "NN": "not nationally notifiable", '
    '"NP": "nationally notifiable but not published", "NC": "not calculated"}}, '
    '{property: noCasesCode, value: "-"}'
)
BY_FLAG = {"-": "OHIO", "U": "MAINE", "N": "IOWA", "NN": "UTAH", "NP": "IDAHO", "NC": "KANSAS"}


@pytest.mark.parametrize("code", list(BY_FLAG))
def test_no_flagged_cell_is_a_bare_zero_or_a_bare_empty(tmp_path: Path, code: str) -> None:
    """Every NNDSS flag: '-' (no reported cases, by the codebook) reads as 0 with its flag; every other code
    stays empty with its flag. No flagged cell comes back as a bare 0 or a bare empty value."""
    contract = CONTRACT.replace(
        f"{{property: flagFor, value: ytd}}, {FLAG_CODES}", f"{{property: flagFor, value: ytd}}, {ALL_CODES}"
    )
    assert contract != CONTRACT
    reg = load_registry(
        write_registry(tmp_path / "reg", catalog=CATALOG, contract=contract, semantic=SEMANTIC)
    )
    area = BY_FLAG[code]
    records = [rec(area, 2023, w, None, None, None, code) for w in (1, 2, 3)]
    records += [rec("MICHIGAN", 2023, w, 1, w, None) for w in (1, 2, 3)]
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, records)
    build_snapshot(
        lake, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/resource/x.json", records)], [f]
    )
    eng = Engine(registry=reg, lake=lake)
    out = tools.get_series(reg, eng, condition="legionellosis", locations=[area], fmt="json")
    assert isinstance(out, dict)
    cols = out["columns"]
    (row,) = [dict(zip(cols, r, strict=True)) for r in out["rows"]]
    assert row["flag"] == code
    if code == "-":
        assert row["value"] == 0 and "no reported cases" in row["period_status"]
    else:
        assert row["value"] is None and row["period_status"].startswith("no number published")
