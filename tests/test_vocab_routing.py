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
    assert ds.label_for("Tularemia") == "Tularemia"
    assert ds.label_for("q  FEVER acute") == "Q fever, Acute"
    assert ds.label_for("Rabbit fever") == "Tularemia"  # an alias from the (synthetic) codebook
    assert ds.label_for("rabbit") is None
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
        missing = {s.condition for s in ds.semantic.series} - set(ds.condition_labels)
        assert not missing, (ds.id, missing)
        assert ds.semantic.generic_series, ds.id


def test_every_real_catalogued_label_compiles_with_its_aggregation_rule() -> None:
    """Regression: any catalogued label reads through get_series' series lookup and compiles; a running total is
    read at the last week of each year (never weeks added), a rate or percent is never summed."""
    reg = load_registry(ROOT)
    checked = 0
    for ds in reg:
        for label in ds.condition_labels:
            found = tools.series_for(reg, label, source_id=ds.id)
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
        "nearest_served": ["Ohio from toy_cases (the state that contains Lake County, Ohio; toy_cases has no "
                           "smaller area)"],
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
    near = _json(tools.get_series(reg, eng, condition="chronic hepatitis", fmt="json"))
    assert near["nearest_served"] == ["Q fever, Chronic"] and "did_you_mean" not in near
    gbd = _json(tools.get_series(reg, eng, condition="Lower respiratory infections", fmt="json"))
    assert gbd["outside_registry"] == [
        "GBD cause 'Lower respiratory infections': modelled burden for it is in the IHME GBD connector (this "
        "server never fetches GBD)"
    ]
    text = tools.get_aligned_with_gbd(reg, eng, condition="brucelosis")
    assert isinstance(text, str) and text.splitlines()[-1] == (
        "Did you mean (these are different things from what you asked; check before using one): Brucellosis"
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
                               "from": "the catalog's condition labels (the publisher's own)"}  # fmt: skip
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
    assert bare["dimension_values"]["state"]["from"] == "not listed (no copy loaded yet)"
    assert bare["retrieved_at"] == "not fetched yet" and bare["raw_sha256"] == "not fetched yet"


def test_dimension_values_from_contract_and_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = load_registry(ROOT)
    resp = tools.dimension_values(reg.get("cdc_respnet"), None)
    assert resp["virus_network"]["values"] == ["COVID-NET", "FluSurv-NET", "RSV-NET", "Combined"]
    assert resp["sex"]["from"] in ("the contract's documented values", "examples from the contract, not the full list",
                                   "not listed (no copy loaded yet)")  # fmt: skip
    hai = tools.dimension_values(reg.get("cdc_nhsn_ar_hai"), None)
    assert "Methicillin-resistant Staphylococcus aureus" in hai["phenotype"]["values"]
    monkeypatch.setattr(tools, "MAX_DIMENSION_VALUES", 2)
    nndss = tools.dimension_values(reg.get("cdc_nndss_weekly"), None)["condition"]
    assert len(nndss["values"]) == 2 and nndss["from"].endswith(
        f"; first 2 of {len(reg.get('cdc_nndss_weekly').condition_labels)}"
    )


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
    eng.manifests["toy_cases"] = replace(m, raw_sha256=[])
    assert tools.provenance_fields(reg.get("toy_cases"), eng)["raw_sha256"] == "read live, not stored"


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
