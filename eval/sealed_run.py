"""The sealed TEST set in CI: the checks around decrypting it, scoring a run, and the use count.

`.github/workflows/sealed-test.yml` calls this module; it is the only code that sees the TEST plaintext, and it
holds it in memory or in the runner's temporary directory only. Nothing here prints a question, a label or an
answer: every message is one this module wrote, and any other failure is reported by its type alone, because
a parser's message can quote the record it failed on (hard rule 14).

The steps, in the order the workflow runs them:

- `source`: read the ciphertext source, `release:<tag>/<asset>` (an asset of this repository's release) or a
  path in the private ops dataset `<owner>/health-data-registry-ops-eval` (`naming.ops_dataset("eval")`).
- `fetch-hf`: download that path with the eval environment's `EVAL_HF_TOKEN`.
- `expect`: the plaintext sha256 the labels must have. With `reaudited_labels` blank, the frozen TEST v1
  (`scorer.TEST_SHA256`). Otherwise the input must be the sha256 of the ciphertext just fetched and be listed in
  `scorer.REAUDITED_TEST`, which only a reviewed pull request changes: a dispatch input alone can't point the run
  at labels nobody reviewed.
- `decrypt`: `age --decrypt` with the identity on standard input (never on disk or a command line). The
  plaintext is checked against the expected sha256 in memory and written, mode 0600, only when it matches.
- `questions`: write the questions alone (`id` and `question`, mode 0600) for the driver, so no label reaches the
  model under test or the API it calls; the labels file is kept for `score` only.
- `score`: score the driver's transcript and answers into per-question rows, kept in the temporary directory;
  `eval.report --split test` turns them into the aggregate tables, which are all the run prints.
- `prior-uses`: the uses of a label set before this workflow existed, added to the count the use log keeps.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from . import scorer
from .scorer import TEST_SHA256, load_expected, score_question, transcript_by_question

# Runs of a label set before the workflow kept a use log, by plaintext sha256 (TEST v1 was run once).
PRIOR_USES: Mapping[str, int] = MappingProxyType({TEST_SHA256: 1})

SHA256_RE = re.compile(r"[0-9a-f]{64}")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
RELEASE_PREFIX = "release:"
PLAINTEXT_MODE = 0o600


class SealedRunError(Exception):
    """A step that can't go on. Its message is written here, never quotes a record, and is safe to print."""


@dataclass(frozen=True)
class Source:
    """Where the ciphertext is: a release asset of this repository, or a path in the private ops dataset."""

    kind: Literal["release", "hf"]
    name: str  # the asset's file name, or the path in the dataset
    tag: str = ""  # release only


def parse_source(spec: str) -> Source:
    """Read `release:<tag>/<asset>` or `<path>` (in the ops dataset). Every part is a plain file name: letters,
    digits, '.', '_' and '-', never starting with '.' (so no '..'), and nothing a shell or glob would read."""
    spec = spec.strip()
    if spec.startswith(RELEASE_PREFIX):
        tag, _, asset = spec[len(RELEASE_PREFIX) :].partition("/")
        if not (NAME_RE.fullmatch(tag) and NAME_RE.fullmatch(asset)):
            raise SealedRunError(
                "The ciphertext source must be release:<tag>/<asset>, each a plain name "
                "(letters, digits, '.', '_' and '-')."
            )
        return Source("release", asset, tag)
    parts = spec.split("/")
    if not spec or not all(NAME_RE.fullmatch(p) for p in parts):
        raise SealedRunError(
            "The ciphertext source must be a path in the ops dataset made of plain names (letters, digits, "
            "'.', '_' and '-', no '..'), or release:<tag>/<asset>."
        )
    return Source("hf", spec)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expected_plaintext_sha(ciphertext_sha: str, reaudited: str) -> str:
    """The plaintext sha256 the decrypted labels must have (see the module docstring, `expect`)."""
    if not SHA256_RE.fullmatch(ciphertext_sha):
        raise SealedRunError("The ciphertext's sha256 is not a 64-character hex digest.")
    reaudited = reaudited.strip().lower()
    if not reaudited:
        return TEST_SHA256
    if not SHA256_RE.fullmatch(reaudited):
        raise SealedRunError(
            "reaudited_labels must be blank (TEST v1) or the 64-character sha256 of the re-audited ciphertext."
        )
    if reaudited != ciphertext_sha:
        raise SealedRunError(
            f"The ciphertext fetched has sha256 {ciphertext_sha}, not the reaudited_labels hash {reaudited}. "
            "Check the source and the hash."
        )
    if reaudited not in scorer.REAUDITED_TEST:
        raise SealedRunError(
            f"The re-audited ciphertext {reaudited} is not listed in REAUDITED_TEST in eval/scorer.py. "
            "Add it there in a reviewed pull request, then run again from main."
        )
    return scorer.REAUDITED_TEST[reaudited]


def _write_private(path: Path, data: bytes) -> None:
    """Write a new file only the runner's user can read; refuse to replace one."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PLAINTEXT_MODE)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def decrypt(ciphertext: Path, out: Path, identity: str, *, expect_sha: str, age: str = "age") -> None:
    """Decrypt with age, the identity on standard input; write the plaintext (0600) only when its sha256 is
    `expect_sha`. age's own messages are not passed on: they are about the key, and none is needed to act."""
    identity = identity.strip()
    if not identity:
        raise SealedRunError("EVAL_TEST_KEY is empty. Add the age identity to the eval environment.")
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [age, "--decrypt", "--identity", "-", str(ciphertext)],
            input=identity.encode("utf-8") + b"\n",
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SealedRunError("The age command is not installed on this runner.") from exc
    if proc.returncode != 0:
        raise SealedRunError(
            "age could not decrypt the ciphertext with EVAL_TEST_KEY: the key does not match the file, or the "
            "file is not an age file. Nothing was written."
        )
    if hashlib.sha256(proc.stdout).hexdigest() != expect_sha:
        raise SealedRunError(
            f"The decrypted labels do not have the expected sha256 {expect_sha}, so the run is void. Nothing was "
            "written. Check the source, and reaudited_labels for a re-audited set."
        )
    _write_private(out, proc.stdout)


def _jsonl(path: Path) -> list[dict[str, object]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not all(isinstance(r, dict) for r in rows):
        raise SealedRunError(f"{path.name} must hold one JSON object per line.")
    return rows


def write_questions(labels: Path, out: Path, *, expect_sha: str) -> int:
    """Write each record's `id` and `question` only (0600, one JSON object per line) for the driver; return how
    many. The labels' hash is checked first, and a record with no question text stops the run."""
    rows = []
    for e in load_expected(labels, "test", sha256=expect_sha):
        question = (e.model_extra or {}).get("question")
        if not isinstance(question, str) or not question.strip():
            raise SealedRunError("A TEST record has no question text, so the driver can't ask it.")
        rows.append(json.dumps({"id": e.id, "question": question}))
    _write_private(out, ("\n".join(rows) + "\n").encode("utf-8"))
    return len(rows)


def score_run(labels: Path, transcript: Path, answers: Path, results: Path, *, expect_sha: str) -> int:
    """Score every TEST question into `results` (0600, one row per question); return how many were scored.

    A question the driver gave no answer for scores as an empty answer. Calls or answers for ids that are not in
    the labels stop the run (the driver read the wrong file); their ids are not printed.
    """
    expected = load_expected(labels, "test", sha256=expect_sha)
    ids = {e.id for e in expected}
    calls = transcript_by_question(_jsonl(transcript))
    answer_rows = _jsonl(answers)
    given = {str(r["id"]): str(r.get("answer") or "") for r in answer_rows}
    if len(given) != len(answer_rows):
        raise SealedRunError(f"{answers.name} answers a question more than once.")
    strays = (calls.keys() | given.keys()) - ids
    if strays:
        raise SealedRunError(
            f"The transcript or the answers name {len(strays)} question id(s) that are not in the labels."
        )
    lines = []
    for e in expected:
        c, r = calls.get(e.id, ([], []))
        lines.append(json.dumps(score_question(e, c, r, given.get(e.id, "")).to_row("test")))
    _write_private(results, ("\n".join(lines) + "\n").encode("utf-8"))
    return len(lines)


def prior_uses(plaintext_sha: str) -> int:
    return PRIOR_USES.get(plaintext_sha, 0)


Downloader = Callable[..., str]


def fetch_hf(path: str, out_dir: Path, token: str, *, owner: str, download: Downloader | None = None) -> Path:
    """Download `path` from the private ops dataset `<owner>/health-data-registry-ops-eval` into `out_dir`."""
    from health_data_registry import naming

    if not token.strip():
        raise SealedRunError(
            "The eval environment has no EVAL_HF_TOKEN secret, or it is empty. Add a read token for the ops "
            "dataset under Settings > Environments > eval, or use a release:<tag>/<asset> source."
        )
    source = parse_source(path)
    if source.kind != "hf":
        raise SealedRunError("fetch-hf takes a path in the ops dataset, not a release asset.")
    if download is None:  # pragma: no cover - the network path; tests pass their own
        from huggingface_hub import hf_hub_download

        download = hf_hub_download
    repo = naming.ops_dataset("eval", owner)
    try:
        got = download(
            repo_id=repo, filename=source.name, repo_type="dataset", token=token, local_dir=str(out_dir)
        )
    except Exception as exc:
        raise SealedRunError(
            f"Could not download {source.name} from {repo} ({type(exc).__name__}). Check the path, and that "
            "EVAL_HF_TOKEN can read the dataset."
        ) from exc
    return Path(got)


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _run(a: argparse.Namespace) -> int:
    match a.cmd:
        case "source":
            s = parse_source(a.spec)
            _out(f"kind={s.kind}\ntag={s.tag}\nname={s.name}")
        case "fetch-hf":
            from health_data_registry import naming

            owner = naming.private_owner()
            got = fetch_hf(a.path, a.out_dir, os.environ.get("EVAL_HF_TOKEN", ""), owner=owner)
            _out(f"path={got}")
        case "expect":
            _out(f"plaintext_sha256={expected_plaintext_sha(a.ciphertext_sha, a.reaudited)}")
        case "decrypt":
            identity = os.environ.get("EVAL_TEST_KEY", "")
            decrypt(a.ciphertext, a.out, identity, expect_sha=a.expect_sha, age=a.age)
            _out("decrypted=true")
        case "questions":
            _out(f"questions={write_questions(a.labels, a.out, expect_sha=a.expect_sha)}")
        case "score":
            n = score_run(a.labels, a.transcript, a.answers, a.results, expect_sha=a.expect_sha)
            _out(f"scored={n}")
        case "prior-uses":
            _out(f"prior_uses={prior_uses(a.plaintext_sha)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.sealed_run", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("source", help="read a ciphertext source").add_argument("spec")
    fh = sub.add_parser("fetch-hf", help="download a path from the private ops dataset (EVAL_HF_TOKEN)")
    fh.add_argument("--path", required=True)
    fh.add_argument("--out-dir", required=True, type=Path)
    ex = sub.add_parser("expect", help="the plaintext sha256 the labels must have")
    ex.add_argument("--ciphertext-sha", required=True)
    ex.add_argument("--reaudited", default="")
    de = sub.add_parser("decrypt", help="decrypt with EVAL_TEST_KEY and check the plaintext sha256")
    de.add_argument("--ciphertext", required=True, type=Path)
    de.add_argument("--out", required=True, type=Path)
    de.add_argument("--expect-sha", required=True)
    de.add_argument("--age", default="age")
    qu = sub.add_parser("questions", help="write the questions alone (id and question) for the driver")
    qu.add_argument("--labels", required=True, type=Path)
    qu.add_argument("--out", required=True, type=Path)
    qu.add_argument("--expect-sha", required=True)
    sc = sub.add_parser("score", help="score a driver run into per-question rows (kept off the log)")
    for flag in ("--labels", "--transcript", "--answers", "--results"):
        sc.add_argument(flag, required=True, type=Path)
    sc.add_argument("--expect-sha", required=True)
    sub.add_parser("prior-uses", help="uses of a label set before the use log").add_argument("plaintext_sha")
    a = ap.parse_args(argv)
    try:
        return _run(a)
    except SealedRunError as exc:
        _out(f"::error::{exc}")
    except Exception as exc:  # the message could quote a sealed record, so only its type is shown
        _out(f"::error::{a.cmd} failed ({type(exc).__name__}). Details are withheld: they could quote TEST.")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
