"""Command line: serve [--http], refresh, fetch, export-public, check-contracts, validate, eval,
publication-outlook, choose-output-format, sitemaps snapshot, harvest-doi, and the analysis tools."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from health_data_registry.registry import Registry, RegistryError, load_registry
from health_data_registry.store import FetchLog, lake_root


def _fetcher(registry: Registry, log: FetchLog | None = None):  # type: ignore[no-untyped-def]
    from health_data_registry.crawl import fetcher, shared_robots

    # One access policy for the whole command: the pace and any block hold across every dataset it reads.
    return fetcher(registry, log, robots=shared_robots(registry))


def refresh(registry: Registry, ids: Sequence[str], *, max_rows: int | None, lake: Path) -> int:
    from health_data_registry.crawl import LiveOnly, refresh_dataset
    from health_data_registry.quality import expected_ids, sample_blocked_message

    failed = 0
    for dataset_id in ids:
        try:
            ds = registry.get(dataset_id)
            m = refresh_dataset(ds, lake, _fetcher(registry, FetchLog(lake, ds.id)), max_rows=max_rows)
        except LiveOnly as e:  # restricted terms: never stored, read live by the server
            print(f"skip {dataset_id}: {e}")
            continue
        except Exception as e:  # report and carry on with the next dataset
            failed += 1
            print(f"FAIL {dataset_id}: {e}", file=sys.stderr)
            continue
        # A development sample is kept even when a blocking check fails on it, but the command still fails.
        if m.partial and (
            why := sample_blocked_message(dataset_id, m.quality, expected=expected_ids(ds.contract))
        ):
            failed += 1
            print(f"FAIL {dataset_id}: {why}", file=sys.stderr)
            continue
        print(f"ok   {dataset_id}: {m.row_count} rows, snapshot {m.snapshot_id}")
    return 1 if failed else 0


def validate(registry: Registry, lake: Path) -> int:
    """Every entry loaded and cross-checked (load_registry did that); then each local snapshot's condition labels
    against the catalog's list. A label the catalog doesn't list fails: add it from the publisher's codebook."""
    from health_data_registry.query import Engine
    from health_data_registry.tools import unlisted_condition_labels
    from health_data_registry.transform_gen import model_contracts

    for ds in registry:
        print(
            f"ok   {ds.id}: contract {ds.contract.version} ({ds.entry.status}), {len(ds.semantic.metrics)} metrics"
        )
        if ds.entry.source.portal is not None:
            print(
                f"warn {ds.id}: source.portal is the older name of source.type and is read for one more release; "
                f"write type: {ds.entry.source.kind}"
            )
    try:
        models = model_contracts(registry.root)
    except RegistryError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    for path, contract in models:
        print(f"ok   {contract.id}: model contract {contract.version} ({path})")
    from health_data_registry.format_choice import RubricError, load_rubric

    failed = 0
    try:
        rubric = load_rubric()
        print(f"ok   format rubric {rubric.version} ({rubric.status}), sha256 {rubric.sha256[:12]}")
    except RubricError as e:
        failed += 1
        print(f"FAIL format rubric: {e}", file=sys.stderr)
    engine = Engine(registry=registry, lake=lake, hub=None, fetch_at_run_time=False)
    for ds_id, extra in unlisted_condition_labels(registry, engine).items():
        if extra is None:
            print(f"skip {ds_id}: no local snapshot, so its condition labels weren't checked")
        elif isinstance(extra, str):
            failed += 1
            print(f"FAIL {ds_id}: {extra}", file=sys.stderr)
        elif extra:
            failed += 1
            print(
                f"FAIL {ds_id}: the snapshot has condition labels the catalog doesn't list: "
                + "; ".join(repr(x) for x in extra),
                file=sys.stderr,
            )
        else:
            print(f"ok   {ds_id}: every condition label in the snapshot is catalogued")
    return 1 if failed else 0


def gate(a: Any) -> int:
    """The release gate: the expected models and tests come from the contracts, and the build id from its inputs
    (the lake's LATEST snapshots, the contracts and the commit). Anything unreadable holds the release."""
    import os

    from health_data_registry.release_gate import (
        GateInputMissing,
        build_inputs,
        expected_from,
        run,
        run_confirm,
    )

    ops = a.ops or lake_root() / "ops"
    try:
        registry = load_registry()
        expected = expected_from(registry)
        commit = a.commit or os.environ.get("GITHUB_SHA")
        if not commit:  # without the code's commit, builds of different code would share one build id
            raise ValueError("no commit was given (--commit, or GITHUB_SHA in CI)")
        inputs = build_inputs(registry, lake_root(), commit)
    except (RegistryError, GateInputMissing, ValueError) as e:
        print(f"The release is held: the contracts or the lake could not be read ({e}).", file=sys.stderr)
        return 1
    if a.confirm:
        ok, message = run_confirm(a.target, ops, a.confirm, expected=expected, inputs=inputs)
    else:
        ok, message = run(a.target, ops, expected=expected, inputs=inputs, run_date=a.run_date)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def export_public(
    registry: Registry, to: str, *, lake: Path, run_date: str | None, tag_file: Path | None = None
) -> int:
    from health_data_registry.publish import PublishCheckFailed, export_public

    try:
        result = export_public(registry, lake, to, run_date=run_date)
    except PublishCheckFailed as e:
        print(f"export stopped: {e}", file=sys.stderr)
        return 1
    for line in result.summary():
        print(line)
    if tag_file is not None and result.tag:  # only a hub upload is tagged; the gate confirms this exact tag
        tag_file.write_text(result.tag + "\n", encoding="utf-8")
    return 0


def check_contracts(registry: Registry) -> int:
    """Does each publisher still serve every field its contract reads? (The nightly drift check.)"""
    from health_data_registry.adapters import capabilities, publisher_fields

    get = _fetcher(registry)
    bad = 0
    for ds in registry:
        if capabilities(ds.entry).checks_layout_on_read:
            print(f"skip {ds.id}: its adapter type checks the publisher's layout on every read")
            continue
        try:
            served = publisher_fields(ds.entry, get)
        except Exception as e:
            bad += 1
            print(f"FAIL {ds.id}: could not read publisher metadata: {e}", file=sys.stderr)
            continue
        missing = sorted({c.source_field for c in ds.contract.columns} - served)
        if missing:
            bad += 1
            print(f"DRIFT {ds.id}: publisher no longer serves {', '.join(missing)}", file=sys.stderr)
        else:
            print(f"ok   {ds.id}: all {len(ds.contract.columns)} contract fields served")
    return 1 if bad else 0


def harvest_doi(
    registry: Registry,
    conference: str,
    *,
    year: int | None,
    max_records: int | None,
    enrich: bool,
    lake: Path,
) -> int:
    """One conference year's DOI-published abstracts into its private store (doi_harvest.py)."""
    from health_data_registry.adapters.doi_abstracts import ApiLayoutError
    from health_data_registry.doi_harvest import HarvestRefused, harvest
    from health_data_registry.fetch import FetchRefused
    from health_data_registry.private import PrivateRefused

    try:
        summary = harvest(registry, lake, conference, year=year, max_records=max_records, enrich=enrich)
    except (HarvestRefused, PrivateRefused, ApiLayoutError) as e:
        print(f"harvest stopped: {e}", file=sys.stderr)
        return 1
    except FetchRefused as e:
        print(f"harvest stopped: {e.message}", file=sys.stderr)
        return 1
    for line in summary.lines():
        print(line)
    return 0


def run_eval(sealed: Path, run: Path | None) -> int:
    import json

    from health_data_registry.evaluation import SealError, load_questions, read_run, score, verify_seals

    if not sealed.is_dir() or not any(sealed.glob("*.yaml")):
        print(f"no sealed sets in {sealed} yet; a blind author has to write and seal them first")
        return 0
    try:
        files = verify_seals(sealed)
    except SealError as e:
        print(f"seal check failed: {e}", file=sys.stderr)
        return 1
    print(f"ok   {len(files)} sealed sets match SHA256SUMS")
    if run is None:
        return 0
    questions = [q for f in files for q in load_questions(sealed / f)[1]]
    result = score(questions, read_run(run))
    print(json.dumps(result, indent=2))
    return 0 if result["gate"]["passed"] else 1


ANALYSIS_COMMANDS = ("get-rate", "compare-rates", "get-trend", "age-adjust", "check-signal")
ROW_COMMANDS = ("get-series", "get-aligned-with-gbd", "get-provenance", "query-sql")
_YEARS: list[tuple[str, dict[str, Any]]] = [
    ("--location", {"action": "append"}), ("--year-start", {"type": int}), ("--year-end", {"type": int})
]  # fmt: skip


def _add_analysis_commands(sub: Any) -> None:
    """The row-returning tools on the command line: the same functions the MCP tools call, the same output and
    the same pages (--page N, with --pin from the previous page's Next line)."""
    specs: dict[str, list[tuple[str, dict[str, Any]]]] = {
        "get-series": [*_YEARS, ("--measure", {}), ("--source-id", {})],
        "get-aligned-with-gbd": list(_YEARS),
        "get-provenance": [("--year", {"type": int})],
        "query-sql": [],
        "get-rate": [("--year", {"type": int, "required": True}), ("--location", {"action": "append"})],
        "compare-rates": [("--location", {"required": True}), ("--year", {"type": int, "required": True}),
                          ("--other-location", {}), ("--other-year", {"type": int})],
        "get-trend": [("--location", {"required": True}), ("--year-start", {"type": int}),
                      ("--year-end", {"type": int}), ("--include-provisional", {"action": "store_true"}),
                      ("--fit-on", {"choices": ["rates", "counts"], "default": "rates"})],
        "age-adjust": [("--location", {"required": True}), ("--year", {"type": int, "required": True})],
        "check-signal": [("--location", {"required": True}), ("--year", {"type": int, "required": True}),
                         ("--week", {"type": int, "required": True})],
    }  # fmt: skip
    first = {"get-provenance": "source_id", "query-sql": "sql"}
    for name, opts in specs.items():
        sp = sub.add_parser(name, help=f"{name.replace('-', '_')} (same as the MCP tool, same pages)")
        sp.add_argument(first.get(name, "condition"))
        for flag, kw in opts:
            sp.add_argument(flag, **kw)
        sp.add_argument("--page", type=int, default=None, help="page of a long answer (1-based)")
        sp.add_argument("--pin", default=None, help="the pin from the previous page's Next line")
        sp.add_argument("--json", action="store_true", help="structured output instead of CSV")


def run_analysis(registry: Registry, a: argparse.Namespace) -> int:
    import json

    from health_data_registry import analysis, tools
    from health_data_registry.query import Engine

    eng = Engine(registry=registry, lake=lake_root())
    fmt: Literal["csv", "json"] = "json" if a.json else "csv"
    pg: dict[str, Any] = {"page": a.page, "pin": a.pin, "fmt": fmt}
    calls: dict[str, Callable[[], Any]] = {
        "get-rate": lambda: analysis.get_rate(
            registry, eng, condition=a.condition, year=a.year, locations=a.location or [], **pg
        ),
        "compare-rates": lambda: analysis.compare_rates(
            registry, eng, condition=a.condition, location=a.location, year=a.year,
            other_location=a.other_location, other_year=a.other_year, **pg,
        ),
        "get-trend": lambda: analysis.get_trend(
            registry, eng, condition=a.condition, location=a.location, year_start=a.year_start,
            year_end=a.year_end, include_provisional=a.include_provisional, fit_on=a.fit_on, **pg,
        ),
        "age-adjust": lambda: analysis.age_adjust(
            registry, eng, condition=a.condition, location=a.location, year=a.year, **pg
        ),
        "check-signal": lambda: analysis.check_signal(
            registry, eng, condition=a.condition, location=a.location, year=a.year, week=a.week, **pg
        ),
        "get-series": lambda: tools.get_series(
            registry, eng, condition=a.condition, locations=a.location or [], year_start=a.year_start,
            year_end=a.year_end, measure=a.measure, source_id=a.source_id, **pg,
        ),
        "get-aligned-with-gbd": lambda: tools.get_aligned_with_gbd(
            registry, eng, condition=a.condition, locations=a.location or [], year_start=a.year_start,
            year_end=a.year_end, **pg,
        ),
        "get-provenance": lambda: tools.get_provenance(registry, eng, source_id=a.source_id, year=a.year, **pg),
        "query-sql": lambda: tools.query_sql(eng, a.sql, **pg),
    }  # fmt: skip
    out = calls[a.cmd]()
    print(
        out if isinstance(out, str) else json.dumps(out, indent=2, default=str),
        end="" if isinstance(out, str) else "\n",
    )
    return 0


def publication_outlook(a: argparse.Namespace) -> int:
    import json

    from health_data_registry.publication import get_publication_outlook

    out = get_publication_outlook(
        conference=a.conference,
        design=a.design,
        data_sources=a.data_sources,
        condition=a.condition,
        geography=a.geography,
        fmt="json" if a.json else "csv",
    )
    if isinstance(out, dict):
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 1 if "refused" in out else 0
    print(out.rstrip("\n"))
    return 0 if out.startswith("conference,") else 1  # a plain-words refusal is not a table


def choose_output_format(a: argparse.Namespace) -> int:
    import json

    from health_data_registry.format_choice import choose_output_format as choose

    out = choose(
        contribution=a.contribution,
        data_maturity=a.data_maturity,
        worked_examples=a.worked_examples,
        needs_methods_section={"yes": True, "no": False}.get(a.needs_methods_section),
        weeks_to_deadline=a.weeks_to_deadline,
        conference=a.conference,
        priorities=a.priorities,
        fmt="json" if a.json else "csv",
    )
    if isinstance(out, dict):
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 1 if "refused" in out else 0
    print(out.rstrip("\n"))
    return 0 if out.startswith("row,") else 1  # a plain-words refusal is not a table


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="health-data-registry", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser(
        "serve", help="run the MCP server on stdio, or with --http the hosted read-only server"
    )
    sv.add_argument(
        "--http", action="store_true", help="Streamable HTTP for the hosted connector (hub releases only)"
    )
    sv.add_argument(
        "--host", default="127.0.0.1", help="with --http: address to listen on (0.0.0.0 serves publicly)"
    )
    sv.add_argument("--port", type=int, default=7860, help="with --http: port (a Space's app_port)")
    r = sub.add_parser("refresh", help="fetch datasets from their publishers into the local lake")
    r.add_argument("datasets", nargs="*")
    r.add_argument("--all", action="store_true")
    r.add_argument(
        "--max-rows", type=int, default=None, help="development sample; marks the snapshot partial"
    )
    fe = sub.add_parser("fetch", help="refresh one source from its publisher (same as refresh <source>)")
    fe.add_argument("source")
    fe.add_argument(
        "--max-rows", type=int, default=None, help="development sample; marks the snapshot partial"
    )
    ex = sub.add_parser(
        "export-public",
        help="publish redistributable sources to the open data hub (hf://datasets/<org>/<name>) or a directory",
    )
    ex.add_argument("--to", required=True, help="hf://datasets/<org>/<name>, or a local directory")
    ex.add_argument("--run-date", default=None, help="YYYY-MM-DD; defaults to today (UTC)")
    ex.add_argument(
        "--tag-file",
        type=Path,
        default=None,
        help="write the release tag to this file once the release is uploaded and tagged (or already published)",
    )
    sub.add_parser("check-contracts", help="check each publisher still serves the fields its contract reads")
    sub.add_parser("validate", help="load and cross-check every catalog entry, contract and semantic model")
    ev = sub.add_parser(
        "eval", help="verify the sealed blind sets; with --run, score a client run against them"
    )
    ev.add_argument("--sealed", type=Path, default=Path("eval/sealed"))
    ev.add_argument("--run", type=Path, default=None, help="JSON lines of a client's answers")
    _add_analysis_commands(sub)
    hd = sub.add_parser(
        "harvest-doi",
        help="harvest one conference year's abstracts by DOI (Crossref, OpenAlex, Europe PMC) into its private store",
    )
    hd.add_argument("conference", help="the catalog's conference key: idweek, shea, apic, icid, ...")
    hd.add_argument("--year", type=int, default=None, help="conference year; default the latest catalogued")
    hd.add_argument("--max-records", type=int, default=None, help="stop after this many DOIs (a sample)")
    hd.add_argument("--no-enrich", action="store_true", help="Crossref only; skip OpenAlex and Europe PMC")
    po = sub.add_parser(
        "publication-outlook",
        help="how often presented abstracts became full papers, per conference and stage (base rate only)",
    )
    po.add_argument("--conference", default=None, help='e.g. "IDWeek" or "CROI"; leave out to list them all')
    po.add_argument("--design", default=None, help="study design (recorded, not used yet)")
    po.add_argument("--data-source", dest="data_sources", action="append", default=None, help="repeatable")
    po.add_argument("--condition", default=None)
    po.add_argument("--geography", default=None)
    po.add_argument("--json", action="store_true", help="JSON instead of CSV")
    co = sub.add_parser(
        "choose-output-format",
        help="which output fits a project now (abstract, letter, manuscript, viewpoint), with probabilities",
    )
    co.add_argument("--contribution", required=True, help="empirical, methods_standard, argument or case")
    co.add_argument(
        "--data-maturity", required=True, help="idea, preliminary_counts, frozen_dataset or adjudicated"
    )
    co.add_argument("--worked-examples", type=int, required=True, help="how many worked examples, 0 or more")
    co.add_argument("--needs-methods-section", required=True, choices=["yes", "no"])
    co.add_argument(
        "--weeks-to-deadline", type=int, default=None, help="weeks before the named conference's deadline"
    )
    co.add_argument("--conference", default=None, help='the target meeting, e.g. "IDWeek"')
    co.add_argument(
        "--priority",
        dest="priorities",
        action="append",
        default=None,
        help="speed, citable_record, reach or effort; repeat, most important first",
    )
    co.add_argument("--json", action="store_true", help="JSON instead of CSV")
    from health_data_registry import sitemaps

    sm = sub.add_parser("sitemaps", help="publisher sitemaps the crawlers start from")
    sm_sub = sm.add_subparsers(dest="sitemaps_cmd", required=True)
    snap = sm_sub.add_parser(
        "snapshot", help="take this week's copy of each seed sitemap and upload it to the private ops dataset"
    )
    sitemaps.add_arguments(snap)
    g = sub.add_parser(
        "gate", help="serve a hub release only if its transform build passed every error-severity test"
    )
    g.add_argument("--target", type=Path, default=Path("transform/target"), help="dbt's target directory")
    g.add_argument("--ops", type=Path, default=None, help="where builds and the serving log live (lake/ops)")
    g.add_argument(
        "--confirm",
        metavar="TAG",
        default=None,
        help="after the upload: record the tag export-public created as served (only a build that passed)",
    )
    g.add_argument(
        "--run-date",
        default=None,
        help="YYYY-MM-DD: the run date the release must carry; recorded with a pass, checked on --confirm",
    )
    g.add_argument(
        "--commit",
        default=None,
        help="the code commit the build ran from (default: $GITHUB_SHA); part of the build's content hash",
    )
    a = p.parse_args(argv)
    if a.cmd == "publication-outlook":  # reads only the packaged base rates, not the registry
        return publication_outlook(a)
    if a.cmd == "choose-output-format":  # reads only the packaged rubric and base rates
        return choose_output_format(a)
    if a.cmd == "gate":
        return gate(a)

    if a.cmd == "sitemaps":  # reads only the seed list, not the registry
        return sitemaps.snapshot(a, error=snap.error)
    try:
        registry = load_registry()
    except RegistryError as e:
        print(f"registry error: {e}", file=sys.stderr)
        return 2
    if a.cmd == "serve" and a.http:
        from health_data_registry.hosted import StartRefused
        from health_data_registry.hosted import main as serve_http

        try:
            serve_http(host=a.host, port=a.port)
        except StartRefused as e:  # a plain refusal with its next step, never a traceback
            print(e, file=sys.stderr)
            return 2
        return 0
    if a.cmd == "serve":
        from health_data_registry.server import main as serve

        serve()
        return 0
    if a.cmd == "validate":
        return validate(registry, lake_root())
    if a.cmd == "eval":
        return run_eval(a.sealed, a.run)
    if a.cmd in ANALYSIS_COMMANDS + ROW_COMMANDS:
        return run_analysis(registry, a)
    if a.cmd == "check-contracts":
        return check_contracts(registry)
    if a.cmd == "harvest-doi":
        return harvest_doi(
            registry, a.conference, year=a.year, max_records=a.max_records, enrich=not a.no_enrich,
            lake=lake_root(),
        )  # fmt: skip
    if a.cmd == "export-public":
        return export_public(registry, a.to, lake=lake_root(), run_date=a.run_date, tag_file=a.tag_file)
    if a.cmd == "fetch":
        return refresh(registry, [a.source], max_rows=a.max_rows, lake=lake_root())
    ids = sorted(registry.datasets) if a.all else list(a.datasets)
    if not ids:
        p.error("name datasets to refresh, or --all")
    return refresh(registry, ids, max_rows=a.max_rows, lake=lake_root())


if __name__ == "__main__":
    raise SystemExit(main())
