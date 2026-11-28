"""The fix round of #288 (ADR 0018): the qualified answers that replaced refusals, pinned by value on synthetic rows.

Each test names the review finding it answers. The served fixture's rows come from test_release_blockers; the
tests that need other rows build their own small lake.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry import analysis
from health_data_registry.query import Engine, compile_metric
from health_data_registry.registry import Registry, load_registry
from tests.smoke.fixtures import NNDSS, ROOT, nndss
from tests.test_release_blockers import CLIN, LEGIO, NNDSS_ROWS, _lake, rows, weeks

BESIDE = ("year_to_date_at_last_week", "year_to_date_week_read", "year_to_date_series_with_no_number")


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> tuple[Registry, Engine]:
    reg = load_registry(ROOT)
    return reg, _lake(tmp_path_factory.mktemp("qualified"), reg, NNDSS_ROWS)


def _beside(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[c] for c in BESIDE)


# ------------------------------------------------- the year-to-date count beside a weekly sum (dbt unit-test 1)


def test_the_year_to_date_beside_a_sum_of_several_places(served: tuple[Registry, Engine]) -> None:
    """(a) Two places in one row: each place's year-to-date count read at its own last week, then added."""
    _reg, eng = served
    out = eng.query_metric(NNDSS, "weekly_cases", filters={"condition": LEGIO, "reporting_area": ["MICHIGAN", "OHIO"]},
                           group_by=["mmwr_year"], period={"start": 2023, "end": 2024})  # fmt: skip
    got = {r["mmwr_year"]: r for r in rows(out)}
    # 2023: Michigan 20 weeks of 3 (60) and Ohio 20 weeks of 2 (40), both through week 20.
    assert got[2023]["weekly_cases"] == 100 and _beside(got[2023]) == (100, "20", 0)
    # 2024: Michigan 60 through week 20; Ohio's weeks add to 39 ('-' in week 16) and its year-to-date is 51 at
    # week 21, so the weeks read differ across the row and come back as a span.
    assert got[2024]["weekly_cases"] == 99 and got[2024]["suppressed_rows"] == 1
    assert _beside(got[2024]) == (111, "20-21", 0)


def test_the_year_to_date_beside_a_sum_with_no_grouping(served: tuple[Registry, Engine]) -> None:
    """(b) No group_by: the one row meets its year-to-date count through the CROSS JOIN branch."""
    _reg, eng = served
    out = eng.query_metric(
        NNDSS, "weekly_cases", filters={"condition": LEGIO, "reporting_area": "OHIO", "mmwr_year": 2024}
    )
    (row,) = rows(out)
    assert row["weekly_cases"] == 39 and _beside(row) == (51, "21", 0)


def test_a_place_with_no_year_to_date_number_is_counted_apart(served: tuple[Registry, Engine]) -> None:
    """A series with no year-to-date number at any week is not in the figure beside the sum, and is counted
    (using-dbt finding 2, gx finding 4): New Jersey's '-' beside Michigan's 21."""
    _reg, eng = served
    out = eng.query_metric(NNDSS, "weekly_cases", filters={"condition": CLIN, "reporting_area": ["MICHIGAN", "NEW JERSEY"],
                           "mmwr_year": 2024}, group_by=["mmwr_year"])  # fmt: skip
    (row,) = rows(out)
    assert row["weekly_cases"] == 2 and row["suppressed_rows"] == 3 and row["flag"] == "-"
    assert _beside(row) == (21, "52", 1)
    assert "year_to_date_series_with_no_number how many of them had no year-to-date number" in out["method"]


def test_the_year_to_date_beside_a_part_of_the_year(served: tuple[Registry, Engine]) -> None:
    """(d) Weeks 10-20 only: the weekly sum covers those weeks; the year-to-date count beside it is at week 20,
    counted from week 1, as the note says."""
    _reg, eng = served
    out = eng.query_metric(NNDSS, "weekly_cases", filters={"condition": LEGIO, "reporting_area": "OHIO", "mmwr_year": 2023,
                           "mmwr_week": list(range(10, 21))}, group_by=["mmwr_year"])  # fmt: skip
    (row,) = rows(out)
    assert row["weekly_cases"] == 22 and _beside(row) == (40, "20", 0)
    assert "it counts from the first week of the year" in out["method"]


def test_running_totals_summed_across_weeks_with_the_running_total_beside(
    served: tuple[Registry, Engine],
) -> None:
    """(e) The summed column is itself the running total: 2 + 4 + ... + 40 = 420, beside the 40 at week 20."""
    _reg, eng = served
    out = eng.query_metric(NNDSS, "year_to_date_cases", filters={"condition": LEGIO, "reporting_area": "OHIO",
                           "mmwr_year": 2023}, group_by=["mmwr_year"])  # fmt: skip
    (row,) = rows(out)
    assert row["year_to_date_cases"] == 420 and _beside(row) == (40, "20", 0)
    assert "counted again in every later week" in out["method"]


def test_the_year_to_date_falls_back_to_the_last_week_with_a_number(tmp_path: Path) -> None:
    """(c) The last week asked for has no year-to-date number: the figure beside the sum is read at the last week
    that has one, and the week it was read at says so."""
    reg = load_registry(ROOT)
    eng = _lake(tmp_path, reg, [
        nndss("OHIO", LEGIO, 2024, 1, m1="2", m3="2"),
        nndss("OHIO", LEGIO, 2024, 2, m1="3", m3="5"),
        nndss("OHIO", LEGIO, 2024, 3, m1="4", flag="-"),
    ])  # fmt: skip
    out = eng.query_metric(NNDSS, "weekly_cases", filters={"condition": LEGIO, "reporting_area": "OHIO"},
                           group_by=["mmwr_year"])  # fmt: skip
    (row,) = rows(out)
    assert row["weekly_cases"] == 9 and _beside(row) == (5, "2", 0)


# ------------------------------------------------------- running totals added across years (dbt unit-test 3)


def test_a_running_total_added_across_years_by_value(served: tuple[Registry, Engine]) -> None:
    """Each year's own year-end figure, added across the years asked for; New Jersey's '-' years counted apart,
    and its all-empty group stays empty."""
    _reg, eng = served
    out = eng.query_metric(NNDSS, "annual_cases", filters={"condition": CLIN, "reporting_area": ["NEW JERSEY", "MICHIGAN"]},
                           group_by=["reporting_area"], period={"start": 2021, "end": 2024})  # fmt: skip
    got = {r["reporting_area"]: r for r in rows(out)}
    # New Jersey: 2021 (5) + 2022 (4), with 2023 and 2024 '-' in every week.
    assert got["NEW JERSEY"]["annual_cases"] == 9 and got["NEW JERSEY"]["suppressed_rows"] == 2
    assert got["NEW JERSEY"]["flag"] == "-"
    assert got["MICHIGAN"]["annual_cases"] == 21 and got["MICHIGAN"]["suppressed_rows"] == 0
    assert "Then added up across mmwr_year" in out["method"]
    empty = eng.query_metric(NNDSS, "annual_cases", filters={"condition": CLIN, "reporting_area": "NEW JERSEY"},
                             group_by=["reporting_area"], period={"start": 2023, "end": 2024})  # fmt: skip
    (row,) = rows(empty)
    assert row["annual_cases"] is None and row["suppressed_rows"] == 2
    assert row["period_status"].startswith("no number published")


def test_an_undeclared_dimension_added_across_gets_the_note() -> None:
    """sharp-edges finding 3: a categorical dimension the semantic model leaves undeclared fails closed, as
    non_additive_columns does, so adding running totals across it says so. (validate requires a declaration on
    every dimension of a summed metric, so this guards a model with running totals only.)"""
    from dataclasses import replace

    ds = load_registry(ROOT).get(NNDSS)
    dims = [d.model_copy(update={"non_additive": None}) if d.name == "reporting_area" else d
            for d in ds.semantic.dimensions]  # fmt: skip
    loose = replace(ds, semantic=ds.semantic.model_copy(update={"dimensions": dims}))
    cm = compile_metric(loose, "annual_cases", filters={"condition": CLIN}, group_by=["mmwr_year"])
    assert "reporting_area (the semantic model doesn't say whether these values can be added)" in cm.method
    declared = compile_metric(ds, "annual_cases", filters={"condition": CLIN}, group_by=["mmwr_year"])
    assert "declares non-additive" not in declared.method  # reporting_area is declared additive


# ---------------------------------------------------------------- populations with gaps (code-review finding 1)


def test_a_population_with_a_gap_is_named_on_the_rate(
    served: tuple[Registry, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A population that is the sum of its reported values only (one value with no number) is named on every
    rate worked out from it, instead of passing as whole. Census population is required by its contract today, so
    the gap is put in by hand here."""
    reg, eng = served
    real = analysis.populations

    def short(*a: Any, **k: Any) -> Any:
        found, years = real(*a, **k)
        return {key: (v, ds, 1) for key, (v, ds, _) in found.items()}, years

    monkeypatch.setattr(analysis, "populations", short)
    (row,) = rows(analysis.get_rate(reg, eng, condition=LEGIO, year=2024, locations=["Ohio"], fmt="json"))
    assert row["rate_per_100k"] == 4.25
    assert "the Ohio population for 2024 is the sum of the reported values only: 1 value" in row["flags"]
    assert "this rate too high" in row["flags"]


# ------------------------------------------------------------- check_signal (code-review findings 2 and 3)


def test_check_signal_with_a_whole_history_says_15(served: tuple[Registry, Engine]) -> None:
    """With every historical period in the data, the method's count is the 15 it always was."""
    reg, eng = served
    out = analysis.check_signal(reg, eng, condition=LEGIO, location="Ohio", year=2024, week=12, fmt="json")
    (row,) = rows(out)
    # 2019-2023 all have weeks 1-20, so the 15 periods are complete: 15 used and nothing named as missing.
    assert row["periods_used"].startswith("15 historical 4-week totals")
    assert any("the mean of 15 historical 4-week totals (of the 15 periods" in n for n in out["notes"])


def test_check_signal_with_history_missing(tmp_path: Path) -> None:
    """Two of the five past years have no rows: the limits use the 9 totals there are, the method says 9, and
    the 6 absent periods are named as not in the baseline (not as totals that add up some weeks)."""
    reg = load_registry(ROOT)
    data = [r for y in (2021, 2022, 2023) for r in weeks("OHIO", LEGIO, y, [2] * 20)]
    data += list(weeks("OHIO", LEGIO, 2024, [2] * 20))
    eng = _lake(tmp_path, reg, data)
    out = analysis.check_signal(reg, eng, condition=LEGIO, location="Ohio", year=2024, week=16, fmt="json")
    (row,) = rows(out)
    assert row["periods_used"] == "9 historical 4-week totals"
    assert any("the mean of 9 historical 4-week totals (of the 15 periods" in n for n in out["notes"])
    (absent,) = [n for n in out["notes"] if "not in the baseline" in n]
    assert absent.startswith("6 of the 15 historical 4-week periods have no week with a number")
    assert not any(n.startswith("Some 4-week totals add up only") for n in out["notes"])


def test_check_signal_gives_no_verdict_on_weeks_not_in_the_data_yet(tmp_path: Path) -> None:
    """Data through week 14, asked about week 16: the current total covers two of its four weeks, so no
    above/below verdict is given; the figures still come back."""
    reg = load_registry(ROOT)
    data = [r for y in range(2019, 2024) for r in weeks("OHIO", LEGIO, y, [2] * 20)]
    data += list(weeks("OHIO", LEGIO, 2024, [2] * 14))
    eng = _lake(tmp_path, reg, data)
    out = analysis.check_signal(reg, eng, condition=LEGIO, location="Ohio", year=2024, week=16, fmt="json")
    (row,) = rows(out)
    assert row["current_4_week_total"] == 4
    assert row["signal"].startswith("not judged: weeks 15, 16 of the current 4 weeks are not in the data yet")
    assert "(it would read below historical limits)" in row["signal"]


# ------------------------------------------------------------- compare_rates (code-review findings 5 and 9)


def test_a_ratio_on_a_partial_place_says_so_on_its_own_row(served: tuple[Registry, Engine]) -> None:
    """New York's count leaves out New York City but its population doesn't: the ratio and the difference say
    the comparison is biased by it, on their own rows."""
    reg, eng = served
    out = analysis.compare_rates(reg, eng, condition=LEGIO, location="New York", year=2024, other_location="Ohio",
                                 fmt="json")  # fmt: skip
    got = {r["what"]: r for r in rows(out)}
    ratio = got["rate ratio (New York 2024 / Ohio 2024)"]
    diff = got["rate difference per 100,000 (New York 2024 - Ohio 2024)"]
    for r in (ratio, diff):
        assert "New York's count covers only part of the place" in r["flags"]
        assert "partialPlaces" in r["flags"] and "this comparison is biased by it" in r["flags"]


def test_the_counterpart_lookup_never_discards_the_comparison(
    served: tuple[Registry, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the clinical rates beside a screening comparison can't be read, the comparison still comes back, with a
    note saying where to find them."""
    from health_data_registry.query import QueryRefused

    reg, eng = served
    real = analysis._rate_rows

    def flaky(registry: Registry, engine: Engine, condition: str, *a: Any, **k: Any) -> Any:
        if condition == CLIN:
            raise QueryRefused("quality_not_run", "a data check on this copy never ran")
        return real(registry, engine, condition, *a, **k)

    monkeypatch.setattr(analysis, "_rate_rows", flaky)
    out = analysis.compare_rates(reg, eng, condition="Candida auris, screening", location="Michigan", year=2024,
                                 other_year=2023, fmt="json")  # fmt: skip
    got = {r["what"]: r for r in rows(out)}
    assert got["rate ratio (Michigan 2024 / Michigan 2023)"]["estimate"] == 1.425
    assert any("couldn't be read (a data check on this copy never ran)" in n for n in out["notes"])


# ------------------------------------------------------------- the counterpart series must exist (gx finding 5)


def test_validate_refuses_a_counterpart_with_no_series(tmp_path: Path) -> None:
    import shutil

    import yaml

    from health_data_registry.registry import RegistryError

    for d in ("catalog", "contracts", "semantic"):
        shutil.copytree(ROOT / d, tmp_path / d)
    path = tmp_path / "semantic" / "cdc_nndss_weekly.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["series"] = [s for s in doc["series"] if s["condition"] != CLIN]
    doc["generic_series"] = [g for g in doc.get("generic_series") or [] if g["metric"] != "annual_cases"]
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(RegistryError, match="has no 'Candida auris, clinical' series on annual_cases"):
        load_registry(tmp_path)


def test_a_not_incidence_label_needs_its_declaration(tmp_path: Path) -> None:
    import shutil

    import yaml

    from health_data_registry.registry import RegistryError

    for d in ("catalog", "contracts", "semantic"):
        shutil.copytree(ROOT / d, tmp_path / d)
    path = tmp_path / "contracts" / "cdc_nndss_weekly.odcs.yaml"
    doc = yaml.safe_load(path.read_text())
    for col in doc["schema"][0]["properties"]:
        for prop in col.get("customProperties") or []:
            if prop.get("property") == "notIncidenceLabel":
                prop["value"] = {**prop["value"], CLIN: "not incidence either"}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(RegistryError, match="notIncidenceLabel names 'Candida auris, clinical'"):
        load_registry(tmp_path)
