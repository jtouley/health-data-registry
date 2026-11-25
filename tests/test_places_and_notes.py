"""Places, notes and refusals from the independent review (findings 5, 6, 7, 9, 11; source research b, c)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry import geo, query, tools
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, RegistryError, UnknownDataset, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import CATALOG, CONTRACT, RECORDS, SEMANTIC, raw_fetch, write_registry

# Ohio's rows cover only part of the state; a third series is one pooled figure with no place breakdown.
PARTIAL_CONTRACT = CONTRACT.replace(
    "customProperties: [{property: role, value: dimension}, {property: sourceField, value: states}]",
    "customProperties:\n"
    "          - {property: role, value: dimension}\n"
    "          - {property: sourceField, value: states}\n"
    '          - {property: partialPlaces, value: {"OHIO": "the Ohio sites only, not the whole state"}}',
).replace(
    '  - {property: unreliableRule, value: {kind: none, description: "None stated (toy)."}}\n',
    "  - {property: unreliableRule, value: {kind: min_count, threshold: 20, description: "
    '"Rates from fewer than 20 cases are flagged."}}\n',
)
POOLED_SEMANTIC = (
    SEMANTIC
    + """\
  - condition: syphilis
    label: Syphilis (pooled sites)
    metric: case_count
    measure: cases
    filters: {condition: Syphilis}
    place_coverage: the toy sites combined, not by state
    time_dimension: year
"""
)


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Registry, Engine]:
    reg = load_registry(
        write_registry(tmp_path / "reg", contract=PARTIAL_CONTRACT, semantic=POOLED_SEMANTIC, catalog=CATALOG)
    )
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(
        lake, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/resource/t.json", RECORDS)], [f]
    )
    return reg, Engine(registry=reg, lake=lake)


def _json(out: Any) -> dict[str, Any]:
    assert isinstance(out, dict)
    return out


# ---------------------------------------------------------------- 5: a source with no place breakdown


def test_places_asked_of_a_pooled_source_return_nothing_with_a_plain_note(
    setup: tuple[Registry, Engine],
) -> None:
    reg, eng = setup
    out = _json(
        tools.get_aligned_with_gbd(reg, eng, condition="syphilis", locations=["Michigan"], fmt="json")
    )
    # Only the state-level series answers; the pooled figure is never passed off as Michigan.
    assert {r[2] for r in out["rows"]} == {"Michigan"}
    assert (
        "toy_cases: Syphilis (pooled sites) from this source is one figure for the toy sites combined, not by "
        "state, so nothing is shown for Michigan." in " ".join(out["notes"])
    )
    series = _json(tools.get_series(reg, eng, condition="syphilis", locations=["Michigan"], fmt="json"))
    assert all(r[9] == "Syphilis" for r in series["rows"])


def test_a_pooled_source_never_gets_a_gbd_location(setup: tuple[Registry, Engine]) -> None:
    reg, eng = setup
    out = _json(tools.get_aligned_with_gbd(reg, eng, condition="syphilis", fmt="json"))
    pooled = [r for r in out["rows"] if r[2] == "the toy sites combined, not by state"]
    assert pooled and all(r[0] == "" and r[1] == "" and r[-1] == "" for r in pooled)
    assert "as published" not in str(out)
    assert any("is one figure for the toy sites combined, not by state, so it has no GBD location" in n
               for n in out["notes"])  # fmt: skip


# ---------------------------------------------------------------- 6: partial places


def test_partial_places_are_not_keyed_to_a_whole_state(setup: tuple[Registry, Engine]) -> None:
    reg, eng = setup
    out = _json(tools.get_aligned_with_gbd(reg, eng, condition="legionellosis", locations=["Michigan", "Ohio"],
                                           fmt="json"))  # fmt: skip
    cols = out["columns"]
    rows = [dict(zip(cols, r, strict=True)) for r in out["rows"] if r[cols.index("measure")] == "cases"]
    ohio = [r for r in rows if r["location_as_published"] == "Ohio"]
    assert ohio and all(r["gbd_location_name"] == "" and r["fips"] == "" and r["suggested_gbd_call"] == ""
                        for r in ohio)  # fmt: skip
    mich = [r for r in rows if r["location_as_published"] == "Michigan"]
    assert all(r["gbd_location_name"] == "Michigan" for r in mich)
    assert "'Michigan'" in mich[0]["suggested_gbd_call"] and "Ohio" not in mich[0]["suggested_gbd_call"]
    assert (
        "toy_cases: 'Ohio' is the Ohio sites only, not the whole state, so it is not keyed to a GBD location."
        in (out["notes"])
    )
    series = _json(tools.get_series(reg, eng, condition="legionellosis", locations=["Ohio"], fmt="json"))
    assert "toy_cases: 'Ohio' here is the Ohio sites only, not the whole state." in series["notes"]


def test_aligned_carries_the_same_dataset_notes_as_get_series(setup: tuple[Registry, Engine]) -> None:
    reg, eng = setup
    aligned = _json(tools.get_aligned_with_gbd(reg, eng, condition="legionellosis", locations=["Michigan"],
                                               fmt="json"))  # fmt: skip
    series = _json(tools.get_series(reg, eng, condition="legionellosis", locations=["Michigan"], fmt="json"))
    shared = [n for n in series["notes"] if n.startswith("toy_cases: ")]
    assert shared and all(n in aligned["notes"] for n in shared)


def test_wildcard_partial_place_and_registry_checks(tmp_path: Path) -> None:
    star = CONTRACT.replace(
        "{property: role, value: dimension}, {property: sourceField, value: states}",
        "{property: role, value: dimension}, {property: sourceField, value: states}, "
        '{property: partialPlaces, value: {"*": "a site catchment"}}',
    )
    reg = load_registry(write_registry(tmp_path / "a", contract=star))
    col = reg.get("toy_cases").contract.column("reporting_area")
    assert col.partial_note("Michigan") == "a site catchment"
    plain = load_registry(write_registry(tmp_path / "b")).get("toy_cases").contract.column("reporting_area")
    assert plain.partial_note("Michigan") is None
    empty = star.replace('{"*": "a site catchment"}', '{"*": ""}')
    with pytest.raises(RegistryError, match="non-empty text"):
        load_registry(write_registry(tmp_path / "c", contract=empty))
    stray = CONTRACT.replace(
        "{property: role, value: measure}, {property: sourceField, value: m2}",
        '{property: role, value: measure}, {property: sourceField, value: m2}, {property: flagCodes, value: {"U": "x"}}',
    )
    with pytest.raises(RegistryError, match="flags no column"):
        load_registry(write_registry(tmp_path / "d", contract=stray))


# ---------------------------------------------------------------- 7: no silent truncation


def test_a_result_over_the_cap_is_refused_not_cut(
    setup: tuple[Registry, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    reg, eng = setup
    monkeypatch.setattr(query, "MAX_ROWS", 1)
    out = _json(tools.get_series(reg, eng, condition="legionellosis", measure="cases", fmt="json"))
    assert out["rows"] == []
    assert (
        "toy_cases: Legionellosis has more than 1 rows for this question (every place, every period), so nothing "
        "is shown rather than a table cut off part way. Ask again for fewer places or a shorter span of years."
    ) in out["notes"]
    raw = eng.query_metric("toy_cases", "case_count", group_by=["state", "condition"])
    assert raw["truncated"] is True and raw["caveats"][0] == "Only the first 1 rows are shown."


# ---------------------------------------------------------------- 9, 11: place aliases, plain refusals


def test_total_is_not_the_united_states() -> None:
    assert geo.place("TOTAL") is None and geo.place("Total") is None
    us = geo.place("U.S. Residents")
    assert us is not None and us.name == "United States"


def test_only_the_registry_unknown_dataset_message_is_echoed(registry: Registry) -> None:
    with pytest.raises(UnknownDataset) as e:
        registry.get("nope")
    assert (
        tools.plain(e.value)
        == "unknown dataset 'nope'; catalogued: toy_cases. Use search_datasets to see what is available."
    )
    leaked = tools.plain(KeyError("internal_column_name"))
    assert "internal_column_name" not in leaked and leaked.startswith(
        "The data for this question couldn't be read"
    )


# ---------------------------------------------------------------- source research b, c


def test_caveats_name_the_c_auris_source_and_the_unreliable_rule(setup: tuple[Registry, Engine]) -> None:
    reg, eng = setup
    rows = _json(tools.list_caveats(reg, fmt="json"))["rows"]
    assert ["toy_cases", "unreliable values", "Rates from fewer than 20 cases are flagged."] in rows
    text = " ".join(r[2] for r in rows)
    assert "counts cases by the week they were reported" in text and "isolates, not cases" in text
    assert "specimen collection date" in text
    info = _json(tools.describe_dataset(reg, "toy_cases", eng, fmt="json"))
    assert info["unreliable_values"] == "Rates from fewer than 20 cases are flagged."
    plain = load_registry(write_registry(Path(str(reg.root) + "-plain")))
    assert ["toy_cases", "unreliable values", "None stated (toy)."] in _json(
        tools.list_caveats(plain, fmt="json")
    )["rows"]
    assert "Values marked unreliable or suppressed: None stated (toy)." in str(
        tools.describe_dataset(plain, "toy_cases")
    )


# ---------------------------------------------------------------- staff review: geography basis, denominators


def test_unresolved_geography_basis_blocks_gbd_keys(tmp_path: Path) -> None:
    blocked = CONTRACT.replace(
        "  - {property: openQuestions",
        '  - {property: geographyBasis, value: "unresolved: facility location or patient residence"}\n'
        '  - {property: rateDenominator, value: "residents per 100,000"}\n'
        "  - {property: openQuestions",
    )
    reg = load_registry(write_registry(tmp_path / "reg", contract=blocked))
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(
        lake, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/resource/t.json", RECORDS)], [f]
    )
    eng = Engine(registry=reg, lake=lake)
    out = _json(
        tools.get_aligned_with_gbd(reg, eng, condition="legionellosis", locations=["Michigan"], fmt="json")
    )
    assert out["rows"] and all(r[0] == "" and r[1] == "" and r[-1] == "" for r in out["rows"])
    notes = out["notes"]
    assert any("Comparison is blocked until a maintainer resolves it." in n for n in notes)
    assert "toy_cases: places are by unresolved: facility location or patient residence." in notes
    assert "toy_cases: rate denominator: residents per 100,000." in notes
    assert not any("as real" in n for n in notes)
    assert any(
        "establish how the two differ in case definition, ascertainment, geography" in n for n in notes
    )
    text = str(tools.describe_dataset(reg, "toy_cases"))
    assert "Places are by: unresolved: facility location or patient residence" in text
    assert "Rate denominator: residents per 100,000" in text
