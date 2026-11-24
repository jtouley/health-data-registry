"""Command line: serve, refresh, check-contracts, validate."""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from health_data_registry.registry import Registry, RegistryError, load_registry
from health_data_registry.store import RawFetch, build_snapshot, lake_root, write_ndjson, write_raw

RETRIES = 4
FETCH_TIMEOUT_S = 180.0


def _fetcher(registry: Registry):  # type: ignore[no-untyped-def]
    from health_data_registry.fetch import FetchRefused, fetch

    hosts = registry.hosts

    def get(url: str) -> RawFetch:
        r = None
        for attempt in range(RETRIES):
            try:
                r = fetch(
                    url, allowed_hosts=hosts, headers={"Accept": "application/json"}, timeout=FETCH_TIMEOUT_S
                )
                break
            except FetchRefused as e:
                # A slow or briefly unreachable publisher is retried; a refused URL or host never is.
                if e.code not in ("timeout", "connect_failed") or attempt == RETRIES - 1:
                    raise
                time.sleep(2 ** (attempt + 1))
        if r is None:  # pragma: no cover - the loop either breaks with a result or raises
            raise RuntimeError(f"{url}: no response")
        if r.status != 200:
            raise RuntimeError(f"{url}: HTTP {r.status}")
        return RawFetch(url=r.final_url, content=r.content, sha256=r.sha256, retrieved_at=r.retrieved_at)

    return get


def refresh(registry: Registry, ids: Sequence[str], *, max_rows: int | None, lake: Path) -> int:
    from health_data_registry.adapters import pages

    get = _fetcher(registry)
    failed = 0
    for dataset_id in ids:
        ds = registry.get(dataset_id)
        with tempfile.TemporaryDirectory() as tmp:
            fetches, files = [], []
            try:
                fields = sorted({c.source_field for c in ds.contract.columns})
                for i, page in enumerate(pages(ds.entry, get, max_rows=max_rows, fields=fields)):
                    write_raw(lake, [page.fetch])  # keep the bytes on disk now, not in memory
                    f = page.fetch
                    fetches.append(
                        RawFetch(url=f.url, content=b"", sha256=f.sha256, retrieved_at=f.retrieved_at)
                    )
                    nd = Path(tmp) / f"page-{i:05d}.ndjson"
                    write_ndjson(nd, page.records)
                    files.append(nd)
                m = build_snapshot(lake, ds, fetches, files, partial=max_rows is not None)
            except Exception as e:  # report and carry on with the next dataset
                failed += 1
                print(f"FAIL {dataset_id}: {e}", file=sys.stderr)
                continue
        print(f"ok   {dataset_id}: {m.row_count} rows, snapshot {m.snapshot_id}")
    return 1 if failed else 0


def check_contracts(registry: Registry) -> int:
    """Does each publisher still serve every field its contract reads? (The nightly drift check.)"""
    from health_data_registry.adapters import socrata, who_gho

    get = _fetcher(registry)
    bad = 0
    for ds in registry:
        mod = socrata if ds.entry.source.portal == "socrata" else who_gho
        try:
            served = mod.publisher_fields(ds.entry, get)
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
    ids = sorted(registry.datasets) if a.all else list(a.datasets)
    if not ids:
        p.error("name datasets to refresh, or --all")
    return refresh(registry, ids, max_rows=a.max_rows, lake=lake_root())


if __name__ == "__main__":
    raise SystemExit(main())
