"""Blind evaluation: sealed question sets, a hash check, a scorer and the release gate.

A sealed set is written by an author who never saw the connector. Each answer is computed twice
(independently), and the set is sealed with a SHA-256 over its file. This module never writes answers.
It checks the seal, scores a client run's answers against the set, and applies the release bar from the
spec: among answered questions at most 2% wrong; among answerable questions at least 80% right.

Sealed set file (eval/sealed/<set>.yaml):
    set: <name>
    pool: development | held_out
    author: <who wrote it, not a connector maintainer>
    questions:
      - {id, dataset, agency, shape, question, answerable: true|false,
         answer: <number> | null, tolerance: <abs, default 0>}
Seal: eval/sealed/SHA256SUMS, lines "<sha256>  <file name>".

Run file (JSON lines), one per question: {"id": ..., "status": "answered"|"declined", "value": <number|null>}
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

MAX_WRONG = 0.02
MIN_RIGHT = 0.80

Outcome = Literal["right", "wrong", "declined_correctly", "held"]


class SealError(ValueError):
    pass


@dataclass(frozen=True)
class Question:
    id: str
    dataset: str
    agency: str
    shape: str
    answerable: bool
    answer: float | None
    tolerance: float


def verify_seals(sealed_dir: Path) -> list[str]:
    """Return the sealed files whose bytes match SHA256SUMS; raise if any file is unsealed or altered."""
    sums = sealed_dir / "SHA256SUMS"
    if not sums.is_file():
        raise SealError(f"{sums} is missing")
    expected: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            expected[name.strip()] = digest
    files = sorted(p.name for p in sealed_dir.glob("*.yaml"))
    unsealed = sorted(set(files) - set(expected))
    if unsealed:
        raise SealError(f"not sealed: {', '.join(unsealed)}")
    for name, digest in expected.items():
        p = sealed_dir / name
        if not p.is_file():
            raise SealError(f"sealed file missing: {name}")
        if hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise SealError(f"{name} changed after it was sealed")
    return files


def load_questions(path: Path) -> tuple[str, list[Question]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    pool = str(doc.get("pool", ""))
    if pool not in ("development", "held_out"):
        raise SealError(f"{path.name}: pool must be development or held_out")
    out = []
    for q in doc.get("questions") or []:
        answerable = bool(q["answerable"])
        answer = q.get("answer")
        if answerable and answer is None:
            raise SealError(f"{path.name}:{q['id']}: an answerable question needs an answer")
        out.append(
            Question(
                id=str(q["id"]),
                dataset=str(q["dataset"]),
                agency=str(q["agency"]),
                shape=str(q["shape"]),
                answerable=answerable,
                answer=None if answer is None else float(answer),
                tolerance=float(q.get("tolerance", 0)),
            )
        )
    ids = [q.id for q in out]
    if len(set(ids)) != len(ids):
        raise SealError(f"{path.name}: duplicate question ids")
    return pool, out


def outcome(q: Question, response: dict[str, Any] | None) -> Outcome:
    if response is None:
        return "held"
    if response.get("status") == "declined":
        return "declined_correctly" if not q.answerable else "held"
    if not q.answerable:
        return "wrong"  # answered a question the data can't answer
    value = response.get("value")
    if value is None or q.answer is None:
        return "wrong"
    return "right" if abs(float(value) - q.answer) <= q.tolerance else "wrong"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def score(questions: list[Question], responses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, list[Outcome]]] = {"agency": defaultdict(list), "shape": defaultdict(list)}
    allo: list[tuple[Question, Outcome]] = []
    for q in questions:
        o = outcome(q, responses.get(q.id))
        allo.append((q, o))
        groups["agency"][q.agency].append(o)
        groups["shape"][q.shape].append(o)

    def summarise(items: list[tuple[Question, Outcome]]) -> dict[str, Any]:
        answered = [o for q, o in items if o in ("right", "wrong")]
        answerable = [o for q, o in items if q.answerable]
        wrong = sum(o == "wrong" for o in answered)
        right = sum(o == "right" for o in answerable)
        return {
            "questions": len(items),
            "answered": len(answered),
            "wrong_among_answered": wrong / len(answered) if answered else 0.0,
            "wrong_ci95": wilson(wrong, len(answered)),
            "right_among_answerable": right / len(answerable) if answerable else 0.0,
            "right_ci95": wilson(right, len(answerable)),
            "declined_correctly": sum(o == "declined_correctly" for _, o in items),
            "held": sum(o == "held" for _, o in items),
        }

    overall = summarise(allo)
    by: dict[str, dict[str, Any]] = {}
    for dim in ("agency", "shape"):
        by[dim] = {
            k: summarise([(q, o) for q, o in allo if getattr(q, dim) == k]) for k in sorted(groups[dim])
        }
    passed = overall["wrong_among_answered"] <= MAX_WRONG and overall["right_among_answerable"] >= MIN_RIGHT
    return {
        "overall": overall,
        "by": by,
        "gate": {"max_wrong": MAX_WRONG, "min_right": MIN_RIGHT, "passed": passed},
    }


def read_run(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[str(r["id"])] = r
    return out
