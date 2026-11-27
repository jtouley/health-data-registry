"""Condition vocabulary, generic series, structured declines, provenance on discovery (task 84).

Synthetic data only. The toy registry is extended with a `conditions:` vocabulary and a generic series; the real
registry is loaded with hand-written NNDSS-shaped rows to check that any catalogued label reads through the same
aggregation rules as a named series (a running total read at one week, never weeks added up).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from health_data_registry import cli, geo, tools
from health_data_registry.query import DEFAULT_NEXT_STEP, Engine, QueryRefused, compile_metric, next_step_for
from health_data_registry.registry import Registry, RegistryError, load_registry, norm_label
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import CATALOG, RECORDS, SEMANTIC, raw_fetch, write_registry

ROOT = Path(__file__).resolve().parents[1]

# Made-up labels, shaped like a publisher's: a comma-qualified pair that share a word, and one plain label.
LABELS = ["Legionellosis", "Syphilis", "Tularemia", "Q fever, Acute", "Q fever, Chronic", "Brucellosis"]
VOCAB = """\
conditions:
  dimension: condition
  source: "toy codebook (synthetic)"
  labels: [{labels}]
  aliases:
    - {{alias: "rabbit fever", label: Tularemia, source: "toy codebook p.2 (synthetic)", review: publisher_codebook}}
""".format(labels=", ".join(f'"{x}"' for x in LABELS))
GENERIC = """\
generic_series:
  - metric: case_count
    measure: cases
    location_dimension: state
    time_dimension: year
"""
EXTRA = [
    {"states": "MICHIGAN", "year": "2023", "label": "Tularemia", "m1": "7", "m2": ""},
    {"states": "OHIO", "year": "2023", "label": "Tularemia", "m1": "5", "m2": ""},
    {"states": "US RESIDENTS", "year": "2023", "label": "Tularemia", "m1": "40", "m2": ""},
    {"states": "OHIO", "year": "2022", "label": "Q fever, Acute", "m1": "2", "m2": ""},
    {"states": "OHIO", "year": "2022", "label": "Q fever, Chronic", "m1": "1", "m2": ""},
    {"states": "MICHIGAN", "year": "2024", "label": "Brucellosis", "m1": "N", "m2": ""},
]


def vocab_catalog(vocab: str = VOCAB) -> str:
    return CATALOG.replace("contract: contracts/", textwrap.dedent(vocab) + "contract: contracts/")


def build(tmp: Path, records: list[dict[str, Any]], *, vocab: str = VOCAB) -> tuple[Registry, Engine]:
    reg = load_registry(
        write_registry(tmp / "reg", catalog=vocab_catalog(vocab), semantic=SEMANTIC + GENERIC)
    )
    lake = tmp / "lake"
    lake.mkdir(exist_ok=True)
    f = tmp / "p.ndjson"
    write_ndjson(f, records)
    build_snapshot(
        lake, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/resource/t.json", records)], [f]
    )
    return reg, Engine(registry=reg, lake=lake)


@pytest.fixture
def vocab(tmp_path: Path) -> tuple[Registry, Engine]:
    return build(tmp_path, RECORDS + EXTRA)


def _json(out: Any) -> dict[str, Any]:
    assert isinstance(out, dict)
    return out


# ------------------------------------------------------------------------------------------ registry


def test_catalog_vocabulary_loads_and_binds(vocab: tuple[Registry, Engine]) -> None:
    reg, _ = vocab
    ds = reg.get("toy_cases")
    assert ds.condition_labels == LABELS
    assert ds.labels_for("Tularemia") == ["Tularemia"]
    assert ds.labels_for("q  FEVER acute") == ["Q fever, Acute"]
    assert ds.labels_for("Rabbit fever") == ["Tularemia"]  # an alias from the (synthetic) codebook
    assert ds.labels_for("rabbit") == []
    [s] = ds.condition_series("Tularemia")
    assert (s.condition, s.label, s.metric, s.filters) == ("Tularemia", "Tularemia", "case_count",
                                                            {"condition": "Tularemia"})  # fmt: skip
    with pytest.raises(KeyError):
        ds.condition_series("Anthrax")
    assert norm_label("Jamestown Canyon  virus disease") == "jamestown canyon virus disease"


@pytest.mark.parametrize(
    ("vocab", "semantic", "expect"),
    [
        ('conditions: {dimension: condition, source: s, labels: [A, A]}\n', SEMANTIC, "twice"),
        ('conditions: {dimension: condition, source: s, labels: [A, " "]}\n', SEMANTIC, "empty label"),
        ("conditions: {dimension: condition, labels: [A]}\n", SEMANTIC, "where the labels come from"),
        ("conditions: {dimension: condition, source: s}\n", SEMANTIC, "no labels"),
        ("conditions: {dimension: condition, source: s, labels: [A], aliases: [{alias: b, label: C, source: x, "
         "review: pending_maintainer_review}]}\n", SEMANTIC, "not in conditions.labels"),
        ("conditions: {dimension: condition, source: s, labels: [A], aliases: [{alias: b, label: A, source: ' ', "
         "review: pending_maintainer_review}]}\n", SEMANTIC, "needs a source"),
        ("conditions: {dimension: year, source: s, labels: [A]}\n", SEMANTIC, "not a categorical dimension"),
        ("conditions: {dimension: nope, source: s, labels: [A]}\n", SEMANTIC, "not a categorical dimension"),
        ("conditions: {source: s, labels: [A]}\n", SEMANTIC + GENERIC, "needs conditions.dimension"),
        ("conditions: {dimension: condition, source: s, labels: [A]}\n",
         SEMANTIC + GENERIC.replace("    time_dimension: year", "    time_dimension: year\n    filters: {condition: A}"),
         "may not fix condition"),
        ("conditions: {dimension: condition, source: s, labels: [A]}\n",
         SEMANTIC + GENERIC.replace("metric: case_count", "metric: nope"), "unknown metric"),
        ("conditions: {dimension: condition, source: s, labels: [A]}\n",
         SEMANTIC + GENERIC.replace("measure: cases", "measure: rate"), "reads a count"),
    ],
)  # fmt: skip
def test_vocabulary_is_strict(tmp_path: Path, vocab: str, semantic: str, expect: str) -> None:
    with pytest.raises((RegistryError, ValueError), match=expect):
        load_registry(write_registry(tmp_path / "r", catalog=vocab_catalog(vocab), semantic=semantic))


def test_real_catalogs_list_every_named_series_condition() -> None:
    reg = load_registry(ROOT)
    for ds in reg:
        if ds.entry.conditions.dimension is None:
            continue
        assert ds.entry.conditions.source.strip()
        # A series that answers only when asked with its words (asked_with: vaccination coverage, a percent of
        # deaths) is reached by its own condition and label, never by the bare disease label, so it names itself.
        plain = [s for s in ds.semantic.series if not s.asked_with]
        missing = {s.condition for s in plain} - set(ds.condition_labels)
        assert not missing, (ds.id, missing)
        assert ds.semantic.generic_series or not plain, ds.id
        for s in ds.semantic.series:
            if s.asked_with:
                assert any(w in norm_label(s.condition).split() for w in s.asked_with), (ds.id, s.condition)


def test_every_real_catalogued_label_compiles_with_its_aggregation_rule() -> None:
    """Regression: any catalogued label reads through get_series' series lookup and compiles; a running total is
    read at the last week of each year (never weeks added), a rate or percent is never summed."""
    reg = load_registry(ROOT)
    checked = 0
    for ds in reg:
        gated = [s for s in ds.semantic.series if s.asked_with]
        for label in ds.condition_labels:
            found = tools.series_for(reg, label, source_id=ds.id)
            if not found and gated and not ds.semantic.generic_series:
                # Only gated series: the bare label gets nothing (by design); each series' own condition does.
                found = [x for s in gated for x in tools.series_for(reg, s.condition, source_id=ds.id)]
            assert found, (ds.id, label)
            for _, s in found:
                metric = ds.semantic.metric(s.metric)
                filters = dict(s.filters)
                group = [s.time_dimension] + ([s.location_dimension] if s.location_dimension else [])
                cm = compile_metric(ds, s.metric, filters=filters, group_by=group)
                kind = ds.semantic.measure(metric.measure).kind
                if metric.aggregation == "cumulative_latest":
                    assert "Weekly counts are never added up" in cm.method
                elif kind in ("rate", "percent", "estimate"):
                    assert "not summed" in cm.method and "sum(" not in cm.sql
                checked += 1
    assert checked > 150


# ------------------------------------------------------------------------------------------ get_series


def _oracle(records: list[dict[str, Any]], label: str) -> dict[tuple[str, int], int | None]:
    acc: dict[tuple[str, int], int | None] = {}
    for r in records:
        if r["label"] != label or r["states"] == "US RESIDENTS":
            continue
        place = geo.place(r["states"])
        key = (place.name if place else r["states"], int(r["year"]))
        v = int(r["m1"]) if r["m1"].isdigit() else None
        acc[key] = None if v is None or acc.get(key, 0) is None else acc.get(key, 0) + v  # type: ignore[operator]
    return acc


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(label=st.sampled_from([x for x in LABELS if x not in ("Legionellosis", "Syphilis")]))
def test_any_catalogued_label_works_through_get_series(tmp_path: Path, label: str) -> None:
    reg, eng = build(tmp_path, RECORDS + EXTRA)
    out = _json(tools.get_series(reg, eng, condition=label, fmt="json"))
    want = _oracle(RECORDS + EXTRA, label)
    if not want:  # pragma: no cover - every synthetic label has rows
        return
    at = {c: i for i, c in enumerate(out["columns"])}
    got = {(r[at["location"]], r[at["year"]]): r[at["value"]] for r in out["rows"]}
    assert got == want
    assert {r[at["condition"]] for r in out["rows"]} == {label}
    assert {r[at["source_id"]] for r in out["rows"]} == {"toy_cases"}


def test_generic_series_keeps_the_rules(vocab: tuple[Registry, Engine]) -> None:
    reg, eng = vocab
    tul = _json(tools.get_series(reg, eng, condition="rabbit fever", fmt="json"))
    # Total rows (US RESIDENTS) are excluded from the sum; places come back under their GBD names.
    assert sorted(r[:3] for r in tul["rows"]) == [["Michigan", 2023, 7], ["Ohio", 2023, 5]]
    assert any("Provenance for toy_cases:" in n for n in tul["notes"])
    bru = _json(tools.get_series(reg, eng, condition="Brucellosis", fmt="json"))
    assert bru["rows"][0][2] is None and "suppressed or missing" in bru["rows"][0][6]  # never a zero
    # A word shared by two labels returns both, labelled, never added.
    q = _json(tools.get_series(reg, eng, condition="q fever", fmt="json"))
    assert sorted((r[0], r[2], r[9]) for r in q["rows"]) == [("Ohio", 1, "Q fever, Chronic"),
                                                             ("Ohio", 2, "Q fever, Acute")]  # fmt: skip
    assert q["notes"][0].startswith("Rows cover 2 different condition labels")
    # A named series still wins over the generic one (it carries its case definition).
    leg = _json(tools.get_series(reg, eng, condition="Legionellosis", measure="cases", fmt="json"))
    assert leg["rows"][0][11] == "CSTE 2005 (09-ID-04)"
    assert tools.series_for(reg, "q", source_id="toy_cases") == []  # under 3 characters: exact labels only


def test_filter_matched_nothing_lists_what_exists(vocab: tuple[Registry, Engine]) -> None:
    reg, eng = vocab
    out = _json(tools.get_series(reg, eng, condition="Tularemia", locations=["Travis County, Texas",
                                                                            "Lake County, Ohio"], fmt="json"))  # fmt: skip
    assert out == {
        "message": "'Tularemia' is served here, but no row matches 'Travis County, Texas', 'Lake County, Ohio'.",
        "refused": "filter_matched_nothing",
        "next_step": "Ask again with one of the values listed as available.",
        "nearest_served": ["Ohio: Toy notifiable condition cases by state and year reports by state, not by "
                           "county or city"],
        "available": {"toy_cases places": "Michigan, Ohio, United States", "toy_cases years": "2022-2024"},
    }  # fmt: skip
    late = _json(
        tools.get_series(reg, eng, condition="Tularemia", year_start=2030, year_end=2031, fmt="json")
    )
    assert late["served_elsewhere"] == [
        "Centers for Disease Control and Prevention: https://data.cdc.gov/d/toy0-toy0 (this copy covers 2022-2024 "
        "only)"
    ]
    text = tools.get_series(reg, eng, condition="Tularemia", year_start=2030)
    assert isinstance(text, str) and text.splitlines()[1:3] == [
        "Next step: Ask again with one of the values listed as available.",
        "Served elsewhere: Centers for Disease Control and Prevention: https://data.cdc.gov/d/toy0-toy0 (this copy "
        "covers 2022-2024 only)",
    ]
    # A served year with no row for this label: still a decline, with no publisher link.
    assert "served_elsewhere" not in _json(tools.get_series(reg, eng, condition="Tularemia", year_start=2024,
                                                            fmt="json"))  # fmt: skip


def test_real_nndss_generic_label_reads_running_total_at_one_week(tmp_path: Path) -> None:
    from tests.test_real_contracts import nndss

    rows = [
        nndss("KANSAS", "2", m3="9", week=51, label="Tularemia"),
        nndss("KANSAS", "1", m3="10", week=52, label="Tularemia"),
        nndss("US RESIDENTS", "1", summary=True, m3="10", week=52, label="Tularemia"),
        # A full load has flag codes; with none, the codebook rule would have nothing to judge and block LATEST.
        nndss("DISTRICT OF COLUMBIA", None, flag="-", week=52),
    ]
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "n.ndjson"
    write_ndjson(f, rows)
    build_snapshot(
        lake, reg.get("cdc_nndss_weekly"), [raw_fetch("https://data.cdc.gov/resource/x.json", rows)], [f]
    )
    eng = Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)
    out = _json(tools.get_series(reg, eng, condition="Tularemia", locations=["Kansas", "United States"],
                                 source_id="cdc_nndss_weekly", fmt="json"))  # fmt: skip
    vals = sorted((r[0], r[2], r[5]) for r in out["rows"])
    # 10 at the year's last week: never 9 + 10, and never the weekly counts added (2 + 1).
    assert vals == [("Kansas", 10, "year_end_ytd"), ("United States", 10, "year_end_ytd")]


# ------------------------------------------------------------------------------------------ declines


def test_unknown_condition_declines_with_suggestions(vocab: tuple[Registry, Engine]) -> None:
    reg, eng = vocab
    out = _json(tools.get_series(reg, eng, condition="brucelosis", fmt="json"))
    assert out["refused"] == "unknown_condition" and out["did_you_mean"] == ["Brucellosis"]
    assert out["next_step"] == next_step_for("unknown_condition")
    near = _json(tools.get_series(reg, eng, condition="brucellosis abortus", fmt="json"))
    assert near["nearest_served"] == ["Brucellosis"] and "did_you_mean" not in near
    # A shared general word ("chronic", "virus", "infection") is never enough to offer a label as nearest.
    for words in ("chronic hepatitis", "Nipah virus infection"):
        assert "nearest_served" not in _json(tools.get_series(reg, eng, condition=words, fmt="json"))
    gbd = _json(tools.get_series(reg, eng, condition="Lower respiratory infections", fmt="json"))
    assert gbd["outside_registry"] == [
        "For modelled burden of Lower respiratory infections (GBD cause 'Lower respiratory infections'), ask the "
        "IHME GBD connector."
    ]
    text = tools.get_aligned_with_gbd(reg, eng, condition="brucelosis")
    assert isinstance(text, str) and text.splitlines()[-1] == (
        "Did you mean (close spellings of what you asked; check one means what you meant before using it): "
        "Brucellosis"
    )
    miss = _json(tools.search_datasets(reg, topic="cancer", fmt="json"))
    assert miss["refused"] == "no_dataset_matched" and "outside_registry" not in miss


def test_message_and_refusal_shapes() -> None:
    assert tools.message("x", "json", refused="no_rows") == {
        "message": "x", "refused": "no_rows", "next_step": DEFAULT_NEXT_STEP,
    }  # fmt: skip
    assert tools.message("x", refused="r", next_step="Do y.", did_you_mean=[], available={"a": "1"}) == (
        "x\nNext step: Do y.\nValues that exist: a: 1"
    )
    assert tools.message("Do y. now", refused="r", next_step="Do y.") == "Do y. now"  # never said twice
    assert tools.message("x", refused="r", outside_registry=["g"], served_elsewhere=("p",)) == (
        f"x\nNext step: {DEFAULT_NEXT_STEP}\nServed elsewhere: p\nOutside this registry: g"
    )
    e = QueryRefused("too_many_rows", "Too many.")
    assert e.next_step == "Ask again for fewer places or a shorter span of years."
    assert QueryRefused("too_many_rows", "x", next_step="Other.").next_step == "Other."
    assert tools.refusal(e, "json") == {"message": "Too many.", "refused": "too_many_rows",
                                        "next_step": e.next_step}  # fmt: skip
    assert tools.refusal(ValueError("x"), "json")["next_step"].startswith("Try again;")  # type: ignore[index]


def test_unknown_dataset_refusal_names_next_step(registry: Registry) -> None:
    out = _json(tools.describe_dataset(registry, "nope", fmt="json"))
    assert out["refused"] == "unknown_dataset"
    assert out["next_step"] == "Use search_datasets to find the source_id you want, then ask again with it."


@pytest.mark.parametrize(
    ("text", "state"),
    [
        ("Travis County, Texas", "Texas"),
        ("Lake County, OH", "Ohio"),
        ("Somewhere County New Mexico", "New Mexico"),
        ("A City, District of Columbia", "District of Columbia"),
        ("Ohio", None),
        ("Kansas City", None),
        ("New York City", None),
        ("United States", None),
        ("Springfield", None),
    ],
)
def test_containing_state(text: str, state: str | None) -> None:
    p = geo.containing_state(text)
    assert (p.name if p else None) == state


@given(st.sampled_from([p for p in geo.PLACES if p.level == "state"]), st.sampled_from([", ", " "]))
def test_containing_state_any_state(p: geo.Place, sep: str) -> None:
    assert geo.containing_state(f"Some County{sep}{p.name}") == p
    assert geo.containing_state(f"Some County, {p.postal}") == p


# ------------------------------------------------------------------------------------------ describe and discovery


def test_describe_returns_dimension_values_rules_and_provenance(vocab: tuple[Registry, Engine]) -> None:
    reg, eng = vocab
    d = _json(tools.describe_dataset(reg, "toy_cases", eng, fmt="json"))
    dv = d["dimension_values"]
    assert dv["condition"] == {"column": "condition", "values": LABELS,
                               "from": "the catalog's condition labels (the publisher's own)",
                               "never_added_up_because": "each is a different condition"}  # fmt: skip
    assert dv["state"] == {"column": "reporting_area", "values": ["MICHIGAN", "OHIO", "US RESIDENTS"],
                           "from": "the loaded copy of the data"}  # fmt: skip
    assert dv["year"]["values"] == [2022, 2023, 2024]
    assert d["presentation_rules"] == tools.PRESENTATION_RULES
    assert d["condition_labels_from"] == "toy codebook (synthetic)"
    assert d["condition_aliases"][0]["alias"] == "rabbit fever"
    assert d["retrieved_at"] == "2026-11-24T12:00:00" and d["contract_status"] == "draft"
    assert d["source_url"] == "https://data.cdc.gov/d/toy0-toy0" and d["version"].startswith("snapshot ")
    assert len(d["raw_sha256"]) == 64 and d["citation"].startswith("CDC. Toy table.")
    text = str(tools.describe_dataset(reg, "toy_cases", eng))
    assert "Legionellosis; Syphilis; Tularemia; Q fever, Acute; Q fever, Chronic; Brucellosis" in text
    assert "- state (the loaded copy of the data): MICHIGAN, OHIO, US RESIDENTS" in text
    assert "Provenance: source_url https://data.cdc.gov/d/toy0-toy0; retrieved_at 2026-11-24T12:00:00" in text
    assert all(f"- {r}" in text for r in tools.PRESENTATION_RULES)
    # With no copy loaded, the values fall back to the contract and say so.
    bare = _json(tools.describe_dataset(reg, "toy_cases", None, fmt="json"))
    assert bare["dimension_values"]["state"]["from"] == tools.NO_VALUES
    assert bare["retrieved_at"] == "not fetched yet" and bare["raw_sha256"] == "not fetched yet"


def test_dimension_values_from_contract_and_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = load_registry(ROOT)
    resp = tools.dimension_values(reg.get("cdc_respnet"), None)
    assert resp["virus_network"]["values"] == ["COVID-NET", "FluSurv-NET", "RSV-NET", "Combined"]
    assert resp["sex"]["from"] in ("the contract's documented values", "examples from the contract, not the full list",
                                   tools.NO_VALUES)  # fmt: skip
    hai = tools.dimension_values(reg.get("cdc_nhsn_ar_hai"), None)
    assert "Methicillin-resistant Staphylococcus aureus" in hai["phenotype"]["values"]
    monkeypatch.setattr(tools, "MAX_DIMENSION_VALUES", 2)
    nndss = tools.dimension_values(reg.get("cdc_nndss_weekly"), None)["condition"]
    assert len(nndss["values"]) == 2 and nndss["from"].endswith(
        f"; first 2 of {len(reg.get('cdc_nndss_weekly').condition_labels)}"
    )


def test_contract_values_enum_then_examples_then_nothing() -> None:
    reg = load_registry(ROOT)
    resp = reg.get("cdc_respnet")
    assert tools._contract_values(resp, "data_type") == (
        ["Weekly Rate", "Cumulative Rate", "Admitted to Intensive Care Unit", "Invasive Mechanical Ventilation",
         "In-Hospital Death", "≥1 Underlying Condition"],
        "the contract's documented values",
    )  # fmt: skip
    assert tools._contract_values(resp, "state") == (
        ["Michigan", "Overall", "FluSurv-NET"],
        "examples from the contract, not the full list",
    )
    assert tools._contract_values(reg.get("cdc_nndss_weekly"), "states")[0] == [
        "MICHIGAN", "Michigan", "US RESIDENTS", "U.S. Residents", "TOTAL",
    ]  # fmt: skip
    assert tools._contract_values(resp, "nope") == ([], tools.NO_VALUES)
    nndss = tools.dimension_values(reg.get("cdc_nndss_weekly"), None)
    assert nndss["reporting_area"] == {
        "column": "states",
        "values": ["MICHIGAN", "Michigan", "US RESIDENTS", "U.S. Residents", "TOTAL"],
        "from": "examples from the contract, not the full list",
    }


def test_discovery_carries_provenance(vocab: tuple[Registry, Engine]) -> None:
    reg, eng = vocab
    s = _json(tools.search_datasets(reg, topic="tularemia", engine=eng, fmt="json"))
    row = dict(zip(s["columns"], s["rows"][0], strict=True))
    assert row["retrieved_at"] == "2026-11-24T12:00:00" and row["contract_status"] == "draft"
    sug = _json(tools.suggest_datasets(reg, "Q fever in Ohio 2022", engine=eng, fmt="json"))
    srow = dict(zip(sug["columns"], sug["rows"][0], strict=True))
    assert srow["can_answer"] == "yes" and srow["what_it_supports"].startswith(
        "Q fever, Acute; Q fever, Chronic"
    )
    assert srow["source_url"] == "https://data.cdc.gov/d/toy0-toy0" and sug["presentation_rules"]
    csv = tools.suggest_datasets(reg, "Q fever in Ohio 2022", engine=eng)
    assert isinstance(csv, str) and f"# presentation_rules: {tools.PRESENTATION_RULES[0]}" in csv
    many = tools.provenance_fields(reg.get("toy_cases"), None)
    assert many["version"] == "contract 0.1.0"


def test_provenance_lists_two_hashes_then_says_more(vocab: tuple[Registry, Engine]) -> None:
    from dataclasses import replace

    reg, eng = vocab
    eng.connect()
    m = eng.manifests["toy_cases"]
    eng.manifests["toy_cases"] = replace(m, raw_sha256=["a" * 64, "b" * 64, "c" * 64])
    f = tools.provenance_fields(reg.get("toy_cases"), eng)
    assert f["raw_sha256"] == f"{'a' * 64} {'b' * 64} (+1 more; get_provenance lists all)"
    # A stored copy with no hash is never called a live read (hard rule 8); only a live read has none by design.
    eng.manifests["toy_cases"] = replace(m, raw_sha256=[])
    assert tools.provenance_fields(reg.get("toy_cases"), eng)["raw_sha256"].startswith(
        "missing: this stored copy"
    )
    note = tools.provenance_note(reg.get("toy_cases"), eng.manifests["toy_cases"])
    assert "raw file sha256 missing: this stored copy" in note and "read live" not in note
    eng.manifests["toy_cases"] = replace(m, raw_sha256=[], origin="publisher")
    assert tools.provenance_fields(reg.get("toy_cases"), eng)["raw_sha256"] == "read live, not stored"
    assert "read live, not stored (no stored raw file)" in tools.provenance_note(
        reg.get("toy_cases"), eng.manifests["toy_cases"]
    )


# ------------------------------------------------------------------------------------------ validate


def test_validate_fails_on_an_uncatalogued_snapshot_label(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reg, eng = build(tmp_path, RECORDS + [{"states": "OHIO", "year": "2023", "label": "Anthrax", "m1": "1",
                                            "m2": ""}])  # fmt: skip
    assert tools.unlisted_condition_labels(reg, eng) == {"toy_cases": ["Anthrax"]}
    assert cli.validate(reg, eng.lake) == 1
    err = capsys.readouterr().err
    assert "FAIL toy_cases: the snapshot has condition labels the catalog doesn't list: 'Anthrax'" in err


def test_validate_passes_and_skips(vocab: tuple[Registry, Engine], tmp_path: Path,
                                   capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    reg, eng = vocab
    assert cli.validate(reg, eng.lake) == 0
    assert "ok   toy_cases: every condition label in the snapshot is catalogued" in capsys.readouterr().out
    assert cli.validate(reg, tmp_path / "empty") == 0
    assert "skip toy_cases: no local snapshot" in capsys.readouterr().out
    assert tools.unlisted_condition_labels(load_registry(write_registry(tmp_path / "plain")), eng) == {}


def test_validate_reports_an_unreadable_snapshot_and_goes_on(
    vocab: tuple[Registry, Engine], capsys: pytest.CaptureFixture[str]
) -> None:
    reg, eng = vocab
    for db in eng.lake.rglob("data.duckdb"):
        db.write_bytes(b"not a duckdb file")  # a truncated or half-written snapshot
    fresh = Engine(registry=reg, lake=eng.lake, hub=None, fetch_at_run_time=False)
    [(ds_id, why)] = tools.unlisted_condition_labels(reg, fresh).items()
    assert ds_id == "toy_cases" and isinstance(why, str) and "could not be read" in why
    assert cli.validate(reg, eng.lake) == 1
    assert "FAIL toy_cases: its local snapshot could not be read" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------ review fixes


SPELLED = [
    "Hepatitis B, acute",
    "Hepatitis, B, acute",
    "Q fever, Acute",
    "Q fever, Chronic",
    "Q fever, Total",
]
SPELLED_ROWS = [
    {"states": "OHIO", "year": "2022", "label": "Hepatitis B, acute", "m1": "4", "m2": ""},
    {"states": "OHIO", "year": "2023", "label": "Hepatitis, B, acute", "m1": "6", "m2": ""},
    {"states": "OHIO", "year": "2022", "label": "Q fever, Acute", "m1": "2", "m2": ""},
    {"states": "OHIO", "year": "2022", "label": "Q fever, Chronic", "m1": "1", "m2": ""},
    {"states": "OHIO", "year": "2022", "label": "Q fever, Total", "m1": "3", "m2": ""},
]


@pytest.fixture
def spelled(tmp_path: Path) -> tuple[Registry, Engine]:
    vocab = VOCAB.replace(
        "labels: [{}]".format(", ".join(f'"{x}"' for x in LABELS)),
        "labels: [{}]".format(
            ", ".join(f'"{x}"' for x in ["Legionellosis", "Syphilis", "Tularemia", *SPELLED])
        ),
    )
    return build(tmp_path, RECORDS + SPELLED_ROWS, vocab=vocab)


@pytest.mark.parametrize("asked", ["Hepatitis B, acute", "Hepatitis, B, acute", "hepatitis b acute"])
def test_every_spelling_of_a_label_comes_back_apart(spelled: tuple[Registry, Engine], asked: str) -> None:
    """The publisher's two spellings of one condition: both come back, each under its own label, never added and
    never one dropped, with a note naming both."""
    reg, eng = spelled
    assert reg.get("toy_cases").labels_for(asked) == ["Hepatitis B, acute", "Hepatitis, B, acute"]
    out = _json(tools.get_series(reg, eng, condition=asked, fmt="json"))
    assert sorted((r[1], r[2], r[9]) for r in out["rows"]) == [
        (2022, 4, "Hepatitis B, acute"), (2023, 6, "Hepatitis, B, acute"),
    ]  # fmt: skip
    assert out["notes"][0].startswith("The publisher spells this condition 2 ways ('Hepatitis B, acute'; ")
    assert not any(n.startswith("Rows cover") for n in out["notes"])  # one condition, not different things


def test_a_total_label_is_never_added_to_its_parts(spelled: tuple[Registry, Engine]) -> None:
    reg, eng = spelled
    out = _json(tools.get_series(reg, eng, condition="Q fever", fmt="json"))
    got = {r[9]: r[2] for r in out["rows"]}
    assert got == {"Q fever, Acute": 2, "Q fever, Chronic": 1, "Q fever, Total": 3}  # each as published
    assert 6 not in got.values()  # never parts plus total
    assert out["notes"][0].startswith("Rows cover 3 different condition labels")


def test_spelling_notes_only_for_real_variants() -> None:
    assert tools.spelling_notes(["Q fever, Acute", "Q fever, Chronic"]) == []
    [note] = tools.spelling_notes(["SalmonellaParatyphi infection", "Salmonella Paratyphi infection", "x"])
    assert note.startswith("The publisher spells this condition 2 ways ('SalmonellaParatyphi infection'; ")


def test_general_words_never_pick_a_label() -> None:
    """A word like "population", "acute" or "virus" names no condition on its own; the specific word does."""
    reg = load_registry(ROOT)
    who, nndss = reg.get("who_gho_tb_incidence"), reg.get("cdc_nndss_weekly")
    assert tools.catalogued_labels(who, "population") == []
    for word in ("acute", "confirmed", "virus", "infection", "total", "acute virus infection"):
        assert tools.catalogued_labels(nndss, word) == [], word
    assert tools.catalogued_labels(nndss, "Q fever") == [
        "Q fever, Acute",
        "Q fever, Chronic",
        "Q fever, Total",
    ]
    assert tools.catalogued_labels(nndss, "Paratyphi infection") == [
        "Salmonella Paratyphi infection", "Salmonellosis (excluding Salmonella Typhi infection and Salmonella "
        "Paratyphi infection)",
    ]  # fmt: skip
    assert tools.catalogued_labels(nndss, "SalmonellaParatyphi infection") == [
        "Salmonella Paratyphi infection", "SalmonellaParatyphi infection",
    ]  # fmt: skip
    assert tools.series_for(reg, "population", "cases") == []


def test_nearest_served_skips_a_state_row_that_leaves_out_part_of_the_state(tmp_path: Path) -> None:
    """NNDSS 'New York' leaves out New York City, so it is never offered for Brooklyn; Kansas is whole."""
    from tests.test_real_contracts import nndss

    rows = [
        nndss("NEW YORK", "2", m3="9", label="Tularemia"),
        nndss("KANSAS", "1", m3="5", label="Tularemia"),
        nndss(
            "DISTRICT OF COLUMBIA", None, flag="-"
        ),  # a flag code, so the codebook rule has a value to judge
    ]
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "n.ndjson"
    write_ndjson(f, rows)
    build_snapshot(
        lake, reg.get("cdc_nndss_weekly"), [raw_fetch("https://data.cdc.gov/resource/x.json", rows)], [f]
    )
    eng = Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)
    out = _json(tools.get_series(reg, eng, condition="Tularemia", locations=["Brooklyn, New York",
                                 "Wichita, Kansas"], source_id="cdc_nndss_weekly", fmt="json"))  # fmt: skip
    assert out["refused"] == "filter_matched_nothing"
    assert out["nearest_served"] == ["Kansas: NNDSS Weekly Data reports by state, not by county or city"]


class _Hub:
    url = "https://example.invalid/hub"

    def snapshot(self, ds: Any) -> Any:  # pragma: no cover - the test fails if describe_dataset downloads
        raise AssertionError(f"describe_dataset downloaded {ds.id}")


def test_describe_never_downloads_and_says_when_values_are_unavailable(tmp_path: Path) -> None:
    reg = load_registry(ROOT)
    eng = Engine(registry=reg, lake=tmp_path / "empty", hub=_Hub(), fetch_at_run_time=True)
    d = _json(tools.describe_dataset(reg, "who_gho_tb_incidence", eng, fmt="json"))
    assert all(v["from"] != "the loaded copy of the data" for v in d["dimension_values"].values())
    assert eng.loaded_values("cdc_nndss_weekly", "states") is None
    assert "cdc_nndss_weekly" not in eng.manifests and not eng._live


def test_loaded_values_reads_a_local_copy_and_gives_up_on_errors(vocab: tuple[Registry, Engine]) -> None:
    import duckdb

    reg, eng = vocab
    assert eng.loaded_values("toy_cases", "reporting_area") == ["MICHIGAN", "OHIO", "US RESIDENTS"]
    assert eng.loaded_values("toy_cases", "nope") is None

    class Slow:
        interrupted = False

        def execute(self, *_: Any) -> Any:
            raise duckdb.InterruptException("timed out")

        def interrupt(self) -> None:  # pragma: no cover - only called when the timer fires
            self.interrupted = True

    eng._con = Slow()  # type: ignore[assignment]
    assert eng.loaded_values("toy_cases", "reporting_area", timeout_s=0.01) is None


def test_respnet_names_say_which_virus() -> None:
    reg = load_registry(ROOT)
    src = reg.get("cdc_respnet").entry.conditions.source
    assert "COVID-NET is COVID-19" in src and "FluSurv-NET is influenza" in src and "RSV-NET is RSV" in src
    for ds in reg:  # clinicians see this text: no contract jargon
        assert "idDimensions" not in ds.entry.conditions.source
        assert "structural fact" not in ds.entry.conditions.source


def test_not_a_human_case_count_is_declared_and_checked(tmp_path: Path) -> None:
    reg = load_registry(ROOT)
    nndss = reg.get("cdc_nndss_weekly")
    assert nndss.not_a_human_case_count("Rabies, Animal") == "rabid animals, not people"
    assert nndss.not_a_human_case_count("Rabies, Human") is None
    bad = 'conditions: {dimension: condition, source: s, labels: [A], not_a_human_case_count: {B: "x"}}\n'
    with pytest.raises(ValueError, match="not in conditions.labels"):
        load_registry(write_registry(tmp_path / "r1", catalog=vocab_catalog(bad), semantic=SEMANTIC))
    blank = 'conditions: {dimension: condition, source: s, labels: [A], not_a_human_case_count: {A: " "}}\n'
    with pytest.raises(ValueError, match="needs a reason"):
        load_registry(write_registry(tmp_path / "r2", catalog=vocab_catalog(blank), semantic=SEMANTIC))


def test_real_rates_refuse_animal_counts(tmp_path: Path) -> None:
    from health_data_registry import analysis

    reg = load_registry(ROOT)
    eng = Engine(registry=reg, lake=tmp_path / "empty", hub=None, fetch_at_run_time=False)
    rate = _json(analysis.get_rate(reg, eng, condition="Rabies, Animal", year=2024, fmt="json"))
    assert rate["refused"] == "not_a_human_count"
    assert rate["message"] == (
        "No rate per 100,000 people is given for 'Rabies, Animal': 'Rabies, Animal' counts rabid animals, not people."
    )
    assert rate["next_step"] == next_step_for("not_a_human_count")
