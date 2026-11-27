"""One DOI conference harvest for the crawl-doi.yml workflow (one matrix job per conference).

Usage: python scripts/crawl_doi.py <conference> [--year YYYY] [--max-records N] [--no-enrich]

Harvests the conference's year (default: the latest catalogued) into $HDR_LAKE/private/<source>/ with
`health-data-registry harvest-doi`, prints the summary, and, when $GITHUB_OUTPUT is set, writes the step outputs the
workflow's upload step reads:

    source=<source id>                    the catalog source, e.g. idweek_abstracts
    folder=<lake>/private/<source id>     what to upload (the year partitions, *.jsonl)
    private_repo=<owner>/<name>           the source's private Hugging Face dataset (naming.private_dataset)
    complete=true|false                   whether the whole supplement was read

It never uploads anything itself and never runs inside the MCP server. Exit 0 on a harvest, 1 when it stopped.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from health_data_registry import doi_harvest, naming, private
from health_data_registry.adapters.doi_abstracts import ApiLayoutError
from health_data_registry.fetch import FetchRefused
from health_data_registry.registry import load_registry
from health_data_registry.store import lake_root


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("conference")
    p.add_argument("--year", type=int, default=None)
    p.add_argument("--max-records", type=int, default=None)
    p.add_argument("--no-enrich", action="store_true")
    a = p.parse_args(argv)
    registry, lake = load_registry(), lake_root()
    try:
        ds = doi_harvest.conference_dataset(registry, a.conference)
        summary = doi_harvest.harvest(
            registry, lake, a.conference, year=a.year, max_records=a.max_records, enrich=not a.no_enrich
        )
    except (doi_harvest.HarvestRefused, private.PrivateRefused, ApiLayoutError) as e:
        print(f"::error::harvest stopped: {e}", file=sys.stderr)
        return 1
    except FetchRefused as e:
        print(f"::error::harvest stopped: {e.message}", file=sys.stderr)
        return 1
    for line in summary.lines():
        print(line)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        owner = naming.owner() or naming.DEFAULT_OWNER
        with Path(out).open("a", encoding="utf-8") as fh:
            fh.write(f"source={ds.id}\n")
            fh.write(f"folder={private.area(lake, ds.id)}\n")
            fh.write(f"private_repo={naming.private_dataset(ds.id, owner)}\n")
            fh.write(f"complete={'true' if summary.complete else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
