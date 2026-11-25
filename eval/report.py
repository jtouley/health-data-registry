"""Write the eval's aggregate tables. Per-question output exists for DEV only.

TEST is sealed: its per-question rows, its regressed ids and its tag breakdowns are never printed. Every function
that would show a single question (`question_rows_md`, `breakdown_md`, and `mcnemar_md` before it lists
regressed ids) calls `require_dev` first, which allows only the split named "dev" and only rows that all say
DEV. Anything else, including an unknown or empty split name, a row without a split, or a DEV call carrying a
TEST row, raises `SealedSplitError`. For TEST, `mcnemar_md` gives the number of regressions and withholds their
ids. There is no flag to turn the check off. The rule stops at this module: `scorer.ScoredQuestion.to_row`
writes one row per question for either split, so keep TEST result files outside the repo.

Usage:
    python -m eval.report --split dev|test --results run.jsonl [--before before.jsonl] [--tag label]
        [--dev-results dev_run.jsonl] [--questions] [--out file]

`run.jsonl` holds one row per question in the format `ScoredQuestion.to_row` writes (the baseline's format).
`--dev-results` (with `--split test`) adds the DEV minus TEST gap. `breakdown_md` is library-only.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .scorer import ExpectedClass, Protocol_, Split
from .stats import class_table, dev_test_gap, mcnemar

PER_QUESTION_SPLITS = frozenset({"dev"})  # an allowlist: a new split name is refused until added here


class SealedSplitError(PermissionError):
    """A request to print per-question output for a split that is sealed, unknown or mixed."""


class ResultRow(BaseModel):
    """One scored question, validated when read: scores and flags only, never the question, the answer or its
    numbers."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    id: str
    split: Literal["DEV", "TEST"]
    expected_class: ExpectedClass
    class_score: int = Field(ge=0, le=2)
    target_score: int = Field(ge=0, le=2)
    prov_score: int = Field(ge=0, le=2)
    fabricated: bool
    passed: bool = Field(alias="pass")
    pass_strict: bool
    total: int = Field(ge=0, le=6)
    sql_needed: bool = False

    def as_mapping(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


def require_dev(split: str, rows: Iterable[ResultRow] = ()) -> None:
    """Allow per-question output only for DEV. Fails closed.

    Refused: any split name other than "dev" (case and surrounding spaces ignored), and any row whose own split
    is not DEV, so a TEST file passed as "dev" is still refused.
    """
    name = split.strip().lower() if isinstance(split, str) else ""
    if name not in PER_QUESTION_SPLITS:
        raise SealedSplitError(
            f"Per-question output is shown for the DEV split only, not '{split}'. "
            "TEST is sealed: report its aggregate tables instead."
        )
    if any(r.split != "DEV" for r in rows):
        raise SealedSplitError(
            "These results include questions outside DEV, and per-question output is shown for DEV only. "
            "Pass a DEV-only results file, or drop --questions to get the aggregate tables."
        )


def _check_split(split: Split, rows: Sequence[ResultRow]) -> None:
    """Aggregates may cover either split, but the rows must all be from the split named."""
    if split not in ("dev", "test"):
        raise ValueError(f"unknown split '{split}'; use dev or test")
    if any(r.split != split.upper() for r in rows):
        raise ValueError(f"every row must be from {split.upper()}")


def read_results(path: Path) -> list[ResultRow]:
    """Read a results file, one JSON row per question; a malformed row stops the report."""
    rows: list[ResultRow] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(ResultRow.model_validate_json(line))
        except ValidationError as exc:
            raise ValueError(f"{path.name} line {n} is not a valid result row") from exc
    if len({r.id for r in rows}) != len(rows):
        raise ValueError(f"{path.name} scores a question twice")
    return rows


def _pct(x: float) -> str:
    return f"{x:.1%}"


def tables_md(rows: Sequence[ResultRow], split: Split, tag: str = "") -> str:
    """The rubric's table per class and overall for protocol A, then protocol B where any result differs."""
    _check_split(split, rows)
    title = f"#### {split.upper()}" + (f" ({tag})" if tag else "")
    maps = [r.as_mapping() for r in rows]
    md = [
        title,
        "",
        "| class | n | passes | pass rate | 95% Wilson CI | mean total | fabrication fails |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in class_table(maps, "A"):
        label = "**Overall**" if c.label == "Overall" else c.label
        md.append(
            f"| {label} | {c.n} | {c.passes} | {_pct(c.rate)} | [{_pct(c.low)}, {_pct(c.high)}] "
            f"| {c.mean_total:.2f} | {c.fabrication_fails} |"
        )
    md.append("")
    if any(r.pass_strict != r.passed for r in rows):
        md += [
            f"Protocol B (no query_sql), {split.upper()}:",
            "",
            "| class | n | passes | pass rate | 95% Wilson CI |",
            "|---|---|---|---|---|",
        ]
        for c in class_table(maps, "B"):
            label = "**Overall**" if c.label == "Overall" else c.label
            md.append(f"| {label} | {c.n} | {c.passes} | {_pct(c.rate)} | [{_pct(c.low)}, {_pct(c.high)}] |")
        md.append("")
    fab = sum(r.fabricated for r in rows)
    blocked = " Release is blocked." if fab and split == "test" else ""
    md.append(f"Fabrication fails: {fab} of {len(rows)}.{blocked}")
    return "\n".join(md) + "\n"


def mcnemar_md(
    before: Sequence[ResultRow],
    after: Sequence[ResultRow],
    split: Split,
    protocol: Protocol_ = "A",
) -> str:
    """Before/after on the same ids: overall and per class where b + c > 0. Regressed ids are listed for DEV
    only (after `require_dev`); for TEST the count is given and the ids are withheld. Whether a regression
    blocks merging is the reader's call from this table: the function decides nothing."""
    _check_split(split, before)
    _check_split(split, after)

    def passes(rows: Sequence[ResultRow]) -> dict[str, bool]:
        return {r.id: r.passed if protocol == "A" else r.pass_strict for r in rows}

    overall = mcnemar(passes(before), passes(after))
    md = [
        f"#### McNemar, {split.upper()}, protocol {protocol}",
        "",
        "| class | a | b (regressions) | c (fixes) | d | net c-b | p | test |",
        "|---|---|---|---|---|---|---|---|",
    ]
    classes = {r.id: r.expected_class for r in after}
    groups: dict[str, list[str]] = defaultdict(list)
    for qid, label in classes.items():
        groups[label].append(qid)
    b_map, a_map = passes(before), passes(after)
    for cls in sorted(groups):
        ids = groups[cls]
        m = mcnemar({i: b_map[i] for i in ids}, {i: a_map[i] for i in ids})
        if m.b + m.c:
            md.append(
                f"| {cls} | {m.a} | {m.b} | {m.c} | {m.d} | {m.net:+d} | {m.p_value:.3f} | {m.method} |"
            )
    o = overall
    md.append(f"| **Overall** | {o.a} | {o.b} | {o.c} | {o.d} | {o.net:+d} | {o.p_value:.3f} | {o.method} |")
    md.append("")
    if split == "dev":
        require_dev(split, [*before, *after])
        md.append("Regressed: " + (", ".join(o.regressed) if o.regressed else "none") + ".")
    else:
        md.append(f"Regressed questions: {o.b} (ids withheld: TEST is sealed).")
    return "\n".join(md) + "\n"


def gap_md(dev: Sequence[ResultRow], test: Sequence[ResultRow], protocol: Protocol_) -> str:
    """The DEV minus TEST pass-rate gap with its 95% interval."""
    _check_split("dev", dev)
    _check_split("test", test)

    def k(rows: Sequence[ResultRow]) -> int:
        return sum(r.passed if protocol == "A" else r.pass_strict for r in rows)

    g = dev_test_gap(k(dev), len(dev), k(test), len(test))
    return (
        f"DEV - TEST gap, protocol {protocol}: {g.difference:+.1%} (DEV {_pct(g.dev_rate)}, TEST {_pct(g.test_rate)}), "
        f"95% CI [{g.low:+.1%}, {g.high:+.1%}].\n"
    )


def question_rows_md(rows: Sequence[ResultRow], split: str) -> str:
    """One line per question: scores only, never the question or answer. DEV only (`require_dev`)."""
    require_dev(split, rows)
    md = [
        "| id | class | Class | Target | Provenance | fabricated | pass | pass B |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(
            f"| {r.id} | {r.expected_class} | {r.class_score} | {r.target_score} | {r.prov_score} "
            f"| {'yes' if r.fabricated else 'no'} | {'PASS' if r.passed else 'FAIL'} "
            f"| {'PASS' if r.pass_strict else 'FAIL'} |"
        )
    return "\n".join(md) + "\n"


def breakdown_md(
    rows: Sequence[ResultRow],
    tags: Mapping[str, Mapping[str, object]],
    split: str,
    protocol: Protocol_ = "A",
) -> str:
    """DEV-only secondary breakdowns by tag (place_level, phrasing, intent, condition_group), as passes/n under
    the protocol named in the output's first line. Library-only: the command line does not print it."""
    require_dev(split, rows)
    lines = [f"Passes by tag, protocol {protocol}:"]
    for tag in ("place_level", "phrasing", "intent", "condition_group"):
        counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in rows:
            value = str(tags.get(r.id, {}).get(tag))
            counts[value][0] += r.passed if protocol == "A" else r.pass_strict
            counts[value][1] += 1
        lines.append(f"- {tag}: " + ", ".join(f"{v} {a}/{n}" for v, (a, n) in sorted(counts.items())))
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.report", description=__doc__.splitlines()[0])
    ap.add_argument("--split", required=True, choices=["dev", "test"])
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--before", type=Path, help="an earlier run on the same split and labels, for McNemar")
    ap.add_argument("--tag", default="", help="a label for the run, such as the commit")
    ap.add_argument("--dev-results", type=Path, help="with --split test: a DEV run, for the DEV - TEST gap")
    ap.add_argument("--questions", action="store_true", help="also print per-question scores (DEV only)")
    ap.add_argument("--out", type=Path, help="write here instead of standard output")
    a = ap.parse_args(argv)
    rows = read_results(a.results)
    if a.questions:
        try:
            require_dev(a.split, rows)  # refuse before writing anything
        except SealedSplitError as e:
            sys.stderr.write(f"{e}\n")
            return 2
    if a.dev_results and a.split != "test":
        sys.stderr.write("--dev-results compares a DEV run with a TEST run: use it with --split test.\n")
        return 2
    parts = [tables_md(rows, a.split, a.tag)]
    if a.before:
        before = read_results(a.before)
        parts += [mcnemar_md(before, rows, a.split, "A"), mcnemar_md(before, rows, a.split, "B")]
    if a.dev_results:
        dev = read_results(a.dev_results)
        parts += [gap_md(dev, rows, "A"), gap_md(dev, rows, "B")]
    if a.questions:
        parts.append(question_rows_md(rows, a.split))
    text = "\n".join(parts)
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
