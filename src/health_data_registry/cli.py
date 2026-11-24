"""Command line: serve, refresh, fetch, export-public, check-contracts, validate, eval."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from health_data_registry.registry import Registry, RegistryError, load_registry
from health_data_registry.store import FetchLog, lake_root

def _fetcher(registry: Registry, log: FetchLog | None = None):  # type: ignore[no-untyped-def]
    from health_data_registry.crawl import fetcher

    return fetcher(registry, log)


def refresh(registry: Registry, ids: Sequence[str], *, max_rows: int | None, lake: Path) -> int:
    from health_data_registry.crawl import refresh_dataset

    failed = 0
    for dataset_id in ids:
        try:
            ds = registry.get(dataset_id)
            m = refresh_dataset(ds, lake, _fetcher(registry, FetchLog(lake, ds.id)), max_rows=max_rows)
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


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="health-data-registry", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the MCP server on stdio")
    r = sub.add_parser("refresh", help="fetch datasets from their publishers into the local lake")
    r.add_argument("datasets", nargs="*")
    r.add_argument("--all", action="store_true")
    r.add_argument(
        "--max-rows", type=int, default=None, help="development sample; marks the snapshot partial"
    )
    fe = sub.add_parser("fetch", help="refresh one source from its publisher (same as refresh <source>)")
    fe.add_argument("source")
    fe.add_argument("--max-rows", type=int, default=None, help="development sample; marks the snapshot partial")
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
    a = p.parse_args(argv)
    try:
        registry = load_registry()
    except RegistryError as e:
        print(f"registry error: {e}", file=sys.stderr)
        return 2
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
