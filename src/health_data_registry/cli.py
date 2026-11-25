"""Command line: serve [--http], refresh, fetch, export-public, check-contracts, validate, eval,
publication-outlook, sitemaps snapshot, and the analysis tools."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from health_data_registry.registry import Registry, RegistryError, load_registry
from health_data_registry.store import FetchLog, lake_root


def _fetcher(registry: Registry, log: FetchLog | None = None):  # type: ignore[no-untyped-def]
    from health_data_registry.crawl import fetcher

    return fetcher(registry, log)


def refresh(registry: Registry, ids: Sequence[str], *, max_rows: int | None, lake: Path) -> int:
    from health_data_registry.crawl import LiveOnly, refresh_dataset

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
        print(f"ok   {dataset_id}: {m.row_count} rows, snapshot {m.snapshot_id}")
    return 1 if failed else 0


def export_public(registry: Registry, to: str, *, lake: Path, run_date: str | None) -> int:
    from health_data_registry.publish import PublishCheckFailed, export_public

    try:
        result = export_public(registry, lake, to, run_date=run_date)
    except PublishCheckFailed as e:
        print(f"export stopped: {e}", file=sys.stderr)
        return 1
    for line in result.summary():
        print(line)
    return 0


def check_contracts(registry: Registry) -> int:
    """Does each publisher still serve every field its contract reads? (The nightly drift check.)"""
    from health_data_registry.adapters import publisher_fields

    get = _fetcher(registry)
    bad = 0
    for ds in registry:
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
    sub.add_parser("check-contracts", help="check each publisher still serves the fields its contract reads")
    sub.add_parser("validate", help="load and cross-check every catalog entry, contract and semantic model")
    ev = sub.add_parser(
        "eval", help="verify the sealed blind sets; with --run, score a client run against them"
    )
    ev.add_argument("--sealed", type=Path, default=Path("eval/sealed"))
    ev.add_argument("--run", type=Path, default=None, help="JSON lines of a client's answers")
    _add_analysis_commands(sub)
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
    from health_data_registry import sitemaps

    sm = sub.add_parser("sitemaps", help="publisher sitemaps the crawlers start from")
    sm_sub = sm.add_subparsers(dest="sitemaps_cmd", required=True)
    snap = sm_sub.add_parser(
        "snapshot", help="take this week's copy of each seed sitemap and upload it to the private ops dataset"
    )
    sitemaps.add_arguments(snap)
    a = p.parse_args(argv)
    if a.cmd == "publication-outlook":  # reads only the packaged base rates, not the registry
        return publication_outlook(a)
    if a.cmd == "sitemaps":  # reads only the seed list, not the registry
        return sitemaps.snapshot(a, error=snap.error)
    try:
        registry = load_registry()
    except RegistryError as e:
        print(f"registry error: {e}", file=sys.stderr)
        return 2
    if a.cmd == "serve" and a.http:
        from health_data_registry.hosted import main as serve_http

        serve_http(host=a.host, port=a.port)
        return 0
    if a.cmd == "serve":
        from health_data_registry.server import main as serve

        serve()
        return 0
    if a.cmd == "validate":
        for ds in registry:
            print(
                f"ok   {ds.id}: contract {ds.contract.version} ({ds.entry.status}), {len(ds.semantic.metrics)} metrics"
            )
        return 0
    if a.cmd == "eval":
        return run_eval(a.sealed, a.run)
    if a.cmd in ANALYSIS_COMMANDS + ROW_COMMANDS:
        return run_analysis(registry, a)
    if a.cmd == "check-contracts":
        return check_contracts(registry)
    if a.cmd == "export-public":
        return export_public(registry, a.to, lake=lake_root(), run_date=a.run_date)
    if a.cmd == "fetch":
        return refresh(registry, [a.source], max_rows=a.max_rows, lake=lake_root())
    ids = sorted(registry.datasets) if a.all else list(a.datasets)
    if not ids:
        p.error("name datasets to refresh, or --all")
    return refresh(registry, ids, max_rows=a.max_rows, lake=lake_root())


if __name__ == "__main__":
    raise SystemExit(main())
