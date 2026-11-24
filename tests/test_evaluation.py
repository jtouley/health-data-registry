from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from health_data_registry.evaluation import (
    Question,
    SealError,
    load_questions,
    outcome,
    read_run,
    score,
    verify_seals,
    wilson,
)

SET = """\
set: toy
pool: held_out
author: blind author
questions:
  - {id: q1, dataset: toy_cases, agency: CDC, shape: surveillance_series, question: "MI 2023?", answerable: true, answer: 310}
  - {id: q2, dataset: toy_cases, agency: CDC, shape: surveillance_series, question: "Why?", answerable: false, answer: null}
  - {id: q3, dataset: who_tb, agency: WHO, shape: estimate_table, question: "TB?", answerable: true, answer: 1.5, tolerance: 0.05}
"""


def _seal(d: Path) -> Path:
    d.mkdir()
    f = d / "toy.yaml"
    f.write_text(SET)
    (d / "SHA256SUMS").write_text(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  toy.yaml\n")
    return f


def test_seal_detects_changes(tmp_path: Path) -> None:
    f = _seal(tmp_path / "sealed")
    assert verify_seals(tmp_path / "sealed") == ["toy.yaml"]
    f.write_text(SET.replace("310", "311"))
    with pytest.raises(SealError, match="changed"):
        verify_seals(tmp_path / "sealed")
    (tmp_path / "sealed" / "new.yaml").write_text("x: 1")
    with pytest.raises(SealError, match="not sealed"):
        verify_seals(tmp_path / "sealed")


def test_score_and_gate(tmp_path: Path) -> None:
    f = _seal(tmp_path / "sealed")
    pool, qs = load_questions(f)
    assert pool == "held_out"
    run = tmp_path / "run.jsonl"
    run.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"id": "q1", "status": "answered", "value": 310},
                {"id": "q2", "status": "declined"},
                {"id": "q3", "status": "answered", "value": 1.53},
            ]
        )
    )
    s = score(qs, read_run(run))
    assert s["overall"]["right_among_answerable"] == 1.0
    assert s["overall"]["declined_correctly"] == 1
    assert s["gate"]["passed"] is True
    assert set(s["by"]["agency"]) == {"CDC", "WHO"}
    s2 = score(qs, {"q1": {"status": "answered", "value": 300}})
    assert s2["gate"]["passed"] is False and s2["overall"]["held"] == 2


def test_outcomes() -> None:
    q = Question("q", "d", "CDC", "s", answerable=False, answer=None, tolerance=0)
    assert outcome(q, {"status": "answered", "value": 1}) == "wrong"
    assert outcome(q, None) == "held"
    lo, hi = wilson(0, 50)
    assert lo == 0.0 and 0.0 < hi < 0.1
    assert wilson(0, 0) == (0.0, 1.0)


def test_answerable_needs_answer(tmp_path: Path) -> None:
    f = tmp_path / "bad.yaml"
    f.write_text(SET.replace("answer: 310", "answer: null"))
    with pytest.raises(SealError, match="needs an answer"):
        load_questions(f)
