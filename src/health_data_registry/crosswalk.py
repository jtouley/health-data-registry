"""Proposed records for the control plane's condition crosswalk (contracts/control_plane/condition_crosswalk.odcs.yaml).

The crosswalk maps a publisher's condition label to a concept (MeSH, NCBI Taxonomy, GBD cause). Only a
maintainer accepts or rejects a record; this module may only PROPOSE, from two exact sources:

    string_match       a catalog entry's gbd.causes names one of its own catalogued labels, spelled exactly the
                       same: the GBD cause it names is proposed for that label.
    dev_eval_synonym   a DEV eval question's condition wording spells one of its expected dataset's labels
                       (registry.Dataset.labels_for, spelling only): the wording is proposed as a synonym.
                       Only eval/dev/dev.jsonl is read, every record must be split dev, and nothing from the TEST
                       split or eval/sealed/ is ever read (hard rule 14).

A proposed record changes nothing in an answer (dim_condition reads accepted records only). No model output is
ever written: every field is copied from the catalog or the DEV file, and the notes are fixed text (hard rules
1 and 9). Writing keeps every record already exported, accepted and rejected ones untouched, and adds only new
proposals, as a new complete export (the newest file is the one dbt reads).

    python -m health_data_registry.crosswalk [--dev eval/dev/dev.jsonl] [--to DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from health_data_registry.registry import Registry, load_registry

EXPORT = "condition_crosswalk"
ProposedBy = Literal["string_match", "dev_eval_synonym"]
FIELDS = ("record_id", "dataset", "label", "state", "mesh_id", "ncbi_taxon_id", "gbd_cause", "synonym",
          "proposed_by", "source", "reviewer", "reviewed_at", "note")  # fmt: skip


class NotAllowed(ValueError):
    """A proposal source or record this module may never write (a TEST question, a reviewed state)."""


@dataclass(frozen=True)
class Proposal:
    dataset: str
    label: str
    proposed_by: ProposedBy
    gbd_cause: str | None = None
    synonym: str | None = None
    note: str = ""

    @property
    def record_id(self) -> str:
        key = json.dumps([self.dataset, self.label, self.proposed_by, self.gbd_cause, self.synonym])
        return "p-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def record(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "dataset": self.dataset,
            "label": self.label,
            "state": "proposed",
            "mesh_id": None,
            "ncbi_taxon_id": None,
            "gbd_cause": self.gbd_cause,
            "synonym": self.synonym,
            "proposed_by": self.proposed_by,
            "source": None,
            "reviewer": None,
            "reviewed_at": None,
            "note": self.note,
        }


def from_catalog(registry: Registry) -> list[Proposal]:
    """GBD causes the catalog names for a label spelled exactly as one of the dataset's catalogued labels."""
    out = []
    for ds in registry:
        labels = set(ds.entry.conditions.labels)
        for cause in ds.entry.gbd.causes:
            if cause.condition in labels:
                out.append(
                    Proposal(
                        ds.id,
                        cause.condition,
                        "string_match",
                        gbd_cause=cause.cause,
                        note="Proposed by exact spelling from the catalog's gbd.causes; a maintainer checks it "
                        "against the GBD cause list before accepting it.",
                    )
                )
    return out


def _dev_records(path: Path) -> list[dict[str, Any]]:
    resolved = path.resolve()
    if "sealed" in resolved.parts or resolved.name != "dev.jsonl" or resolved.parent.name != "dev":
        raise NotAllowed(f"{path}: only the DEV eval file (eval/dev/dev.jsonl) may propose synonyms")
    records = []
    for n, line in enumerate(resolved.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("split") != "dev":
            raise NotAllowed(f"{path}: line {n} is not a DEV question; nothing outside DEV is ever read")
        records.append(rec)
    return records


def from_dev_set(path: Path, registry: Registry) -> list[Proposal]:
    """A DEV question's condition wording, proposed as a synonym of each expected dataset's label it spells."""
    out = []
    for rec in _dev_records(path):
        wording = str((rec.get("tags") or {}).get("condition") or "").strip()
        if not wording:
            continue
        for dataset in rec.get("expected_datasets") or []:
            if dataset not in registry.datasets:
                continue
            for label in registry.get(dataset).labels_for(wording):
                if label == wording:
                    continue
                out.append(
                    Proposal(
                        dataset,
                        label,
                        "dev_eval_synonym",
                        synonym=wording,
                        note="Proposed from a DEV eval question's wording (spelling match only); a maintainer "
                        "decides whether clinicians ask for the label this way.",
                    )
                )
    return out


def latest_export(directory: Path) -> list[dict[str, Any]]:
    """Every record of the newest export (by file name), or none when nothing has been exported."""
    files = sorted((directory / EXPORT).glob("*.ndjson"))
    if not files:
        return []
    return [json.loads(x) for x in files[-1].read_text(encoding="utf-8").splitlines() if x.strip()]


def merge(existing: Sequence[dict[str, Any]], proposals: Iterable[Proposal]) -> list[dict[str, Any]]:
    """The existing records unchanged, then each new proposal not already recorded (by record id)."""
    seen = {str(r.get("record_id")) for r in existing}
    out = [dict(r) for r in existing]
    for p in proposals:
        rec = p.record()
        if rec["state"] != "proposed" or rec["reviewer"] is not None or rec["reviewed_at"] is not None:
            raise NotAllowed(f"{p.dataset}/{p.label}: only proposed records are written here")
        if rec["record_id"] in seen:
            continue
        seen.add(rec["record_id"])
        out.append(rec)
    return out


def write_export(directory: Path, records: Sequence[dict[str, Any]], *, now: datetime | None = None) -> Path:
    """A new complete export file; earlier exports are kept (append-only)."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    target = directory / EXPORT / f"{stamp}-proposals.ndjson"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"{target} already exists; exports are never overwritten")
    lines = [json.dumps({k: r.get(k) for k in FIELDS}, ensure_ascii=False, sort_keys=False) for r in records]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write proposed crosswalk records (never accepted ones).")
    ap.add_argument(
        "--dev", type=Path, help="the DEV eval file (eval/dev/dev.jsonl) to propose synonyms from"
    )
    ap.add_argument(
        "--to",
        type=Path,
        default=Path(os.environ.get("HDR_CONTROL_PLANE", "lake/control_plane")),
        help="the control-plane export directory (default $HDR_CONTROL_PLANE or lake/control_plane)",
    )
    ap.add_argument("--root", type=Path, default=None)
    args = ap.parse_args(argv)
    registry = load_registry(args.root)
    proposals = from_catalog(registry)
    if args.dev is not None:
        proposals += from_dev_set(args.dev, registry)
    existing = latest_export(args.to)
    records = merge(existing, proposals)
    added = len(records) - len(existing)
    if not added:
        print("No new proposals; nothing written.")
        return 0
    path = write_export(args.to, records)
    print(f"Wrote {added} new proposed record{'s' if added != 1 else ''} to {path}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
