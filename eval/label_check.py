"""Which sealed labels a catalog change may have made stale, without running the server.

Labels are pinned to the catalog they were audited against. When catalog entries are added, changed or removed
between two commits, this lists the ids of the items whose labels depend on one of them, each with the reasons,
so a maintainer knows which items to re-audit. It reads the labels and the two commits' catalog files only:
nothing from either commit is run.

Hard rule 14: the output names item ids and fixed reasons only. A reason never quotes a question, a label value,
a condition or a dataset id, so it does not say which entry an item depends on. The changed entries themselves
come from the public diff and are listed apart. Run it from the sealed TEST workflow (`mode: label-check`), or
locally on DEV.

Rules, each a reason:

- L1 an expected dataset's catalog entry changed (in the diff and in the item's `expected_datasets`);
- L2 an expected dataset's catalog entry was removed (a rename counts as a removal and an addition, whether the
  file is renamed or its `id:` changes in place);
- L3 the item's condition is named in an added or changed entry it does not expect: the entry may now serve,
  or stop serving, a question labelled as a decline, a hand-off, out of scope or another dataset. A condition is
  matched by its words (three letters or more, generic words such as "disease" left out) against the fields that
  say what an entry covers (`COVERAGE_KEYS`: title, grain, conditions, GBD causes and so on; the whole text when
  the YAML can't be read), so it errs towards listing an item; a reviewer decides.

Not covered: changes in `contracts/` or `semantic/` (they are not catalog entries), and a new place in an entry
whose text doesn't name the item's condition.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from .scorer import ExpectedRecord, Split, load_expected

CATALOG_DIR = "catalog"
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
WORD_RE = re.compile(r"[a-z0-9]+")
GENERIC_WORDS = frozenset(
    {
        "and", "the", "for", "with", "all", "any", "other", "disease", "diseases", "infection", "infections",
        "invasive", "associated", "cases", "case", "virus", "illness", "syndrome", "total", "type",
    }
)  # fmt: skip

# L3: the catalog fields that say what an entry covers; caveats, citations and publisher names are left out.
COVERAGE_KEYS = ("title", "grain", "conditions", "gbd", "id_areas", "geography", "description")

Status = Literal["added", "modified", "removed"]
STATUS: dict[str, Status] = {"A": "added", "M": "modified", "D": "removed"}

L1 = "an expected dataset's catalog entry changed"
L2 = "an expected dataset's catalog entry was removed"
L3 = "its condition is named in an added or changed catalog entry it does not expect"


class LabelCheckError(Exception):
    """Inputs the check can't use; the message never quotes a label."""


@dataclass(frozen=True)
class CatalogChange:
    """One catalog entry in the diff: its dataset id, what happened to it, and its YAML text (the newer commit's
    for an addition or change, the older commit's for a removal)."""

    dataset_id: str
    status: Status
    text: str


@dataclass(frozen=True)
class Affected:
    id: str
    reasons: tuple[str, ...]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed git argument list, no shell
        ["git", "-C", str(repo), *args],  # noqa: S607 - git from PATH, as every script here runs it
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise LabelCheckError(f"git {args[0]} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def resolve_commit(repo: Path, commit: str) -> str:
    """The commit's full sha; only a full 40-character sha is accepted, so a branch name can't move under a run."""
    if not COMMIT_RE.fullmatch(commit):
        raise LabelCheckError(f"{commit!r} is not a full 40-character commit sha.")
    try:
        return _git(
            repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{commit}^{{commit}}"
        ).strip()
    except LabelCheckError:
        raise LabelCheckError(
            f"Commit {commit} is not in this checkout's history. Pick base_commit and commit from main."
        ) from None


def _dataset_id(path: str, text: str) -> str:
    """The entry's `id`, else its file name: catalog files are named by dataset id."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("id"), str):
        return str(doc["id"])
    return Path(path).stem


def parse_name_status(out: str) -> list[tuple[Status, str]]:
    """`git diff --name-status -z --no-renames` output -> (status, path) for each catalog/*.yaml file."""
    fields = out.split("\0")
    pairs: list[tuple[Status, str]] = []
    for code, path in zip(fields[::2], fields[1::2], strict=False):
        p = Path(path)
        if code in STATUS and p.parent.as_posix() == CATALOG_DIR and p.suffix in (".yaml", ".yml"):
            pairs.append((STATUS[code], path))
    return pairs


def catalog_changes(repo: Path, base: str, head: str) -> list[CatalogChange]:
    """Every catalog entry added, changed or removed between two commits, with the YAML text to match against."""
    old, new = resolve_commit(repo, base), resolve_commit(repo, head)
    out = _git(repo, "diff", "--name-status", "-z", "--no-renames", old, new, "--", CATALOG_DIR)
    changes = []
    for status, path in parse_name_status(out):
        text = _git(repo, "show", f"{old if status == 'removed' else new}:{path}")
        dataset_id = _dataset_id(path, text)
        if status == "modified":
            old_text = _git(repo, "show", f"{old}:{path}")
            old_id = _dataset_id(path, old_text)
            if old_id != dataset_id:  # `id:` changed in place: the old id is gone and the new one is added
                changes.append(CatalogChange(old_id, "removed", old_text))
                status = "added"
        changes.append(CatalogChange(dataset_id, status, text))
    return sorted(changes, key=lambda c: (c.status, c.dataset_id))


def words(text: str) -> frozenset[str]:
    return frozenset(w for w in WORD_RE.findall(text.lower()) if len(w) >= 3 and w not in GENERIC_WORDS)


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def coverage_words(text: str) -> frozenset[str]:
    """L3: the words of an entry's `COVERAGE_KEYS` fields; the whole text when it isn't a YAML mapping."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        doc = None
    if not isinstance(doc, dict):
        return words(text)
    return words(" ".join(s for k in COVERAGE_KEYS for s in _strings(doc.get(k))))


def _condition(record: ExpectedRecord) -> str:
    tags = (record.model_extra or {}).get("tags")
    value = tags.get("condition") if isinstance(tags, dict) else None
    return value if isinstance(value, str) else ""


def affected(records: Iterable[ExpectedRecord], changes: Sequence[CatalogChange]) -> list[Affected]:
    """L1-L3 for each record; records with no reason are left out."""
    changed = {c.dataset_id for c in changes if c.status == "modified"}
    removed = {c.dataset_id for c in changes if c.status == "removed"}
    present = [(c.dataset_id, coverage_words(c.text)) for c in changes if c.status != "removed"]
    out = []
    for r in records:
        expected = set(r.expected_datasets)
        reasons = []
        if expected & changed:
            reasons.append(L1)
        if expected & removed:
            reasons.append(L2)
        condition = words(_condition(r))
        if condition and any(i not in expected and condition & w for i, w in present):
            reasons.append(L3)
        if reasons:
            out.append(Affected(r.id, tuple(reasons)))
    return out


def report_md(
    changes: Sequence[CatalogChange], found: Sequence[Affected], n_items: int, base: str, head: str
) -> str:
    md = [f"#### Sealed label check: catalog {base[:12]} to {head[:12]}", ""]
    if not changes:
        md.append("No catalog entry changed between these commits, so no label depends on a change.")
        return "\n".join(md) + "\n"
    for status in ("added", "modified", "removed"):
        ids = [c.dataset_id for c in changes if c.status == status]
        if ids:
            md.append(f"- Catalog entries {status}: {', '.join(f'`{i}`' for i in ids)}")
    md += ["", f"Items to re-audit: {len(found)} of {n_items}.", ""]
    if found:
        md += ["| item id | reasons |", "|---|---|"]
        md += [f"| {a.id} | {'; '.join(a.reasons)} |" for a in found]
        md.append("")
    md.append(
        "Reasons never name a dataset, a condition or a class, and no question text or label is shown: "
        "TEST stays sealed (hard rule 14)."
    )
    return "\n".join(md) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.label_check", description=__doc__.splitlines()[0])
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--split", required=True, choices=["dev", "test"])
    ap.add_argument(
        "--expect-sha", default=None, help="a re-audited set's plaintext sha256 (eval.sealed_run)"
    )
    ap.add_argument("--base", required=True, help="full sha of the older catalog")
    ap.add_argument("--head", required=True, help="full sha of the newer catalog")
    ap.add_argument("--repo", type=Path, default=Path("."))
    a = ap.parse_args(argv)
    split: Split = a.split
    try:
        records = load_expected(a.labels, split, sha256=a.expect_sha)
        changes = catalog_changes(a.repo, a.base, a.head)
    except LabelCheckError as exc:
        sys.stdout.write(f"::error::{exc}\n")
        return 1
    except Exception as exc:  # the message could quote a sealed record, so only its type is shown
        sys.stdout.write(f"::error::label check failed ({type(exc).__name__}); details are withheld.\n")
        return 1
    found = affected(records, changes)
    sys.stdout.write(report_md(changes, found, len(records), a.base, a.head))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
