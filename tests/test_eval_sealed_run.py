"""eval/sealed_run.py: the checks around the sealed TEST set in CI, on synthetic labels only.

The round trip encrypts a tiny made-up label set with a throwaway age key generated here; it needs the age and
age-keygen commands and is skipped without them. Every other test uses a stand-in age script."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from eval import report, sealed_run
from eval.scorer import TEST_SHA256
from eval.sealed_run import (
    SealedRunError,
    Source,
    decrypt,
    expected_plaintext_sha,
    fetch_hf,
    parse_source,
    prior_uses,
    score_run,
)

MARKER = "SYNTHETIC-QUESTION-TEXT"  # must never reach any output
LABELS = [
    {"id": "syn-a", "question": MARKER, "expected_class": "OUT_OF_SCOPE", "expected_datasets": []},
    {"id": "syn-b", "question": MARKER, "expected_class": "HANDOFF_GBD", "expected_nearest": "Toyland"},
    {"id": "syn-c", "question": MARKER, "expected_class": "HANDOFF_CONNECTOR", "expected_nearest": "pubmed"},
]
FAKE_KEY = "AGE-SECRET-KEY-1SYNTHETIC"


def labels_bytes() -> bytes:
    return "".join(json.dumps(r) + "\n" for r in LABELS).encode()


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


@pytest.fixture
def fake_age(tmp_path: Path) -> Path:
    """An age stand-in: checks its arguments and the identity on stdin, then 'decrypts' by copying the file."""
    script = tmp_path / "fake-age"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "assert args[:3] == ['--decrypt', '--identity', '-'] and len(args) == 4, args\n"
        f"if sys.stdin.read().strip() != {FAKE_KEY!r}:\n"
        "    sys.stderr.write('no identity matched\\n'); sys.exit(1)\n"
        "sys.stdout.buffer.write(open(args[3], 'rb').read())\n"
    )
    script.chmod(0o755)
    return script


# ------------------------------------------------------------------------------------------ the source


@pytest.mark.parametrize(
    ("spec", "want"),
    [
        ("release:eval-test-v1/test.jsonl.age", Source("release", "test.jsonl.age", "eval-test-v1")),
        (" test/v1/test.jsonl.age ", Source("hf", "test/v1/test.jsonl.age")),
        ("test.age", Source("hf", "test.age")),
    ],
)
def test_parse_source_reads_both_forms(spec: str, want: Source) -> None:
    assert parse_source(spec) == want


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "release:",
        "release:tag",
        "release:tag/",
        "release:/asset",
        "release:tag/a/b",
        "release:../x/y",
        "../test.age",
        "test/../../x",
        "/abs/test.age",
        "test//x",
        "test/.hidden",
        "test*.age",
        "te st.age",
        "test.age;rm",
        "a/" + "x" * 201,
    ],
)
def test_parse_source_refuses_anything_but_plain_names(spec: str) -> None:
    with pytest.raises(SealedRunError):
        parse_source(spec)


# ------------------------------------------------------------------------------------------ the expected labels

CIPHER = "ab" * 32


def test_blank_reaudited_means_the_frozen_test_v1() -> None:
    assert expected_plaintext_sha(CIPHER, "") == TEST_SHA256
    assert expected_plaintext_sha(CIPHER, "  ") == TEST_SHA256


def test_a_reaudited_hash_must_be_the_ciphertext_and_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SealedRunError, match="not the reaudited_labels hash"):
        expected_plaintext_sha(CIPHER, "cd" * 32)
    with pytest.raises(SealedRunError, match="not listed in REAUDITED_TEST"):
        expected_plaintext_sha(CIPHER, CIPHER)
    monkeypatch.setattr(sealed_run, "REAUDITED_TEST", {CIPHER: "ef" * 32})
    assert expected_plaintext_sha(CIPHER, CIPHER.upper()) == "ef" * 32


@pytest.mark.parametrize("bad", ["abc", "g" * 64, "ab" * 33])
def test_hashes_must_be_hex_digests(bad: str) -> None:
    with pytest.raises(SealedRunError, match="reaudited_labels must be blank"):
        expected_plaintext_sha(CIPHER, bad)
    with pytest.raises(SealedRunError, match="not a 64-character hex digest"):
        expected_plaintext_sha(bad, "")


def test_test_v1_was_run_once_before_the_log() -> None:
    assert prior_uses(TEST_SHA256) == 1
    assert prior_uses("ef" * 32) == 0


# ------------------------------------------------------------------------------------------ decrypt


def test_decrypt_writes_matching_plaintext_readable_by_owner_only(tmp_path: Path, fake_age: Path) -> None:
    cipher = tmp_path / "test.age"
    cipher.write_bytes(labels_bytes())
    out = tmp_path / "test.jsonl"
    decrypt(
        cipher,
        out,
        f"  {FAKE_KEY}\n",
        expect_sha=hashlib.sha256(labels_bytes()).hexdigest(),
        age=str(fake_age),
    )
    assert out.read_bytes() == labels_bytes()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_decrypt_refuses_to_replace_a_file(tmp_path: Path, fake_age: Path) -> None:
    cipher = tmp_path / "test.age"
    cipher.write_bytes(labels_bytes())
    out = tmp_path / "test.jsonl"
    out.write_text("x")
    with pytest.raises(FileExistsError):
        decrypt(
            cipher, out, FAKE_KEY, expect_sha=hashlib.sha256(labels_bytes()).hexdigest(), age=str(fake_age)
        )


def test_decrypt_writes_nothing_when_the_hash_differs(tmp_path: Path, fake_age: Path) -> None:
    cipher = tmp_path / "test.age"
    cipher.write_bytes(labels_bytes())
    out = tmp_path / "test.jsonl"
    with pytest.raises(SealedRunError, match="the run is void") as exc:
        decrypt(cipher, out, FAKE_KEY, expect_sha="0" * 64, age=str(fake_age))
    assert not out.exists()
    assert MARKER not in str(exc.value)


@pytest.mark.parametrize(
    ("key", "message"), [("", "EVAL_TEST_KEY is empty"), ("AGE-SECRET-KEY-1WRONG", "could not decrypt")]
)
def test_decrypt_refuses_a_missing_or_wrong_key(
    tmp_path: Path, fake_age: Path, key: str, message: str
) -> None:
    cipher = tmp_path / "test.age"
    cipher.write_bytes(labels_bytes())
    with pytest.raises(SealedRunError, match=message) as exc:
        decrypt(cipher, tmp_path / "out.jsonl", key, expect_sha="0" * 64, age=str(fake_age))
    assert "no identity matched" not in str(exc.value)  # age's own message is not passed on
    assert not (tmp_path / "out.jsonl").exists()


def test_decrypt_says_when_age_is_missing(tmp_path: Path) -> None:
    with pytest.raises(SealedRunError, match="not installed"):
        decrypt(tmp_path / "c", tmp_path / "o", FAKE_KEY, expect_sha="0" * 64, age=str(tmp_path / "no-age"))


# ------------------------------------------------------------------------------------------ score


def scored_inputs(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    labels = tmp_path / "labels.jsonl"
    labels.write_bytes(labels_bytes())
    transcript = write_jsonl(tmp_path / "transcript.jsonl", [])
    answers = write_jsonl(
        tmp_path / "answers.jsonl",
        [
            {"id": "syn-a", "answer": "No catalogued source covers that."},
            {"id": "syn-b", "answer": "Ask the IHME GBD connector for Toyland."},
        ],
    )
    return labels, transcript, answers, hashlib.sha256(labels_bytes()).hexdigest()


def test_score_run_scores_every_label_and_keeps_rows_private(tmp_path: Path) -> None:
    labels, transcript, answers, digest = scored_inputs(tmp_path)
    results = tmp_path / "results.jsonl"
    assert score_run(labels, transcript, answers, results, expect_sha=digest) == 3
    rows = [json.loads(x) for x in results.read_text().splitlines()]
    assert [(r["id"], r["split"], r["pass"]) for r in rows] == [
        ("syn-a", "TEST", True),
        ("syn-b", "TEST", True),
        ("syn-c", "TEST", False),  # no answer: scored as an empty answer
    ]
    assert stat.S_IMODE(results.stat().st_mode) == 0o600
    assert MARKER not in results.read_text()


def test_score_run_refuses_ids_that_are_not_labels(tmp_path: Path) -> None:
    labels, transcript, answers, digest = scored_inputs(tmp_path)
    write_jsonl(answers, [{"id": "stray-1", "answer": "x"}])
    with pytest.raises(SealedRunError, match="1 question id"):
        score_run(labels, transcript, answers, tmp_path / "r.jsonl", expect_sha=digest)
    write_jsonl(transcript, [{"id": "stray-2", "tool": "list_sources", "args": {}, "result": ""}])
    write_jsonl(answers, [])
    with pytest.raises(SealedRunError, match="1 question id"):
        score_run(labels, transcript, answers, tmp_path / "r.jsonl", expect_sha=digest)


def test_score_run_refuses_a_question_answered_twice(tmp_path: Path) -> None:
    labels, transcript, answers, digest = scored_inputs(tmp_path)
    write_jsonl(answers, [{"id": "syn-a", "answer": "x"}, {"id": "syn-a", "answer": "y"}])
    with pytest.raises(SealedRunError, match="more than once"):
        score_run(labels, transcript, answers, tmp_path / "r.jsonl", expect_sha=digest)


def test_score_run_refuses_lines_that_are_not_objects(tmp_path: Path) -> None:
    labels, transcript, answers, digest = scored_inputs(tmp_path)
    answers.write_text("[1, 2]\n")
    with pytest.raises(SealedRunError, match="one JSON object per line"):
        score_run(labels, transcript, answers, tmp_path / "r.jsonl", expect_sha=digest)


# ------------------------------------------------------------------------------------------ fetch-hf


def test_fetch_hf_downloads_from_the_eval_ops_dataset(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def download(**kw: Any) -> str:
        seen.update(kw)
        return str(tmp_path / kw["filename"])

    got = fetch_hf("v1/test.age", tmp_path, "hf_x", owner="someone", download=download)
    assert got == tmp_path / "v1/test.age"
    assert seen == {
        "repo_id": "someone/health-data-registry-ops-eval",
        "filename": "v1/test.age",
        "repo_type": "dataset",
        "token": "hf_x",
        "local_dir": str(tmp_path),
    }


def test_fetch_hf_needs_a_token_and_a_plain_path(tmp_path: Path) -> None:
    def download(**kw: Any) -> str:
        raise AssertionError("must not download")

    with pytest.raises(SealedRunError, match="no EVAL_HF_TOKEN"):
        fetch_hf("test.age", tmp_path, " ", owner="o", download=download)
    with pytest.raises(SealedRunError, match="not a release asset"):
        fetch_hf("release:t/a", tmp_path, "hf_x", owner="o", download=download)
    with pytest.raises(SealedRunError, match="plain names"):
        fetch_hf("../x", tmp_path, "hf_x", owner="o", download=download)


def test_fetch_hf_reports_a_failed_download_plainly(tmp_path: Path) -> None:
    def download(**kw: Any) -> str:
        raise OSError("401 for https://example.invalid with token hf_x")

    with pytest.raises(
        SealedRunError, match=r"Could not download test.age from o/health-data-registry-ops-eval \(OSError\)"
    ) as exc:
        fetch_hf("test.age", tmp_path, "hf_x", owner="o", download=download)
    assert "hf_x" not in str(exc.value)


# ------------------------------------------------------------------------------------------ the command line


def test_main_prints_key_value_lines(capsys: pytest.CaptureFixture[str]) -> None:
    assert sealed_run.main(["source", "release:t1/a.age"]) == 0
    assert capsys.readouterr().out == "kind=release\ntag=t1\nname=a.age\n"
    assert sealed_run.main(["expect", "--ciphertext-sha", CIPHER]) == 0
    assert capsys.readouterr().out == f"plaintext_sha256={TEST_SHA256}\n"
    assert sealed_run.main(["prior-uses", TEST_SHA256]) == 0
    assert capsys.readouterr().out == "prior_uses=1\n"


def test_main_reports_its_own_errors_as_annotations(capsys: pytest.CaptureFixture[str]) -> None:
    assert sealed_run.main(["source", "../x"]) == 1
    assert capsys.readouterr().out.startswith("::error::The ciphertext source must be")


def test_main_withholds_any_other_error_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A label line that fails validation: pydantic's message would quote it, question text included.
    bad = (json.dumps({"id": "syn-x", "question": MARKER, "expected_class": "NOT_A_CLASS"}) + "\n").encode()
    labels = tmp_path / "labels.jsonl"
    labels.write_bytes(bad)
    empty = write_jsonl(tmp_path / "empty.jsonl", [])
    argv = ["score", "--labels", str(labels), "--transcript", str(empty), "--answers", str(empty)]
    argv += ["--results", str(tmp_path / "r.jsonl"), "--expect-sha", hashlib.sha256(bad).hexdigest()]
    assert sealed_run.main(argv) == 1
    captured = capsys.readouterr()
    assert (
        captured.out
        == "::error::score failed (ValidationError). Details are withheld: they could quote TEST.\n"
    )
    assert MARKER not in captured.out + captured.err


def test_main_decrypts_with_the_key_from_the_environment(
    tmp_path: Path, fake_age: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cipher = tmp_path / "test.age"
    cipher.write_bytes(labels_bytes())
    monkeypatch.setenv("EVAL_TEST_KEY", FAKE_KEY)
    argv = [
        "decrypt",
        "--ciphertext",
        str(cipher),
        "--out",
        str(tmp_path / "p.jsonl"),
        "--age",
        str(fake_age),
    ]
    assert sealed_run.main([*argv, "--expect-sha", hashlib.sha256(labels_bytes()).hexdigest()]) == 0
    assert capsys.readouterr().out == "decrypted=true\n"


def test_main_fetch_hf_reads_the_token_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("EVAL_HF_TOKEN", raising=False)
    assert sealed_run.main(["fetch-hf", "--path", "test.age", "--out-dir", str(tmp_path)]) == 1
    assert "no EVAL_HF_TOKEN" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------ the round trip

AGE, AGE_KEYGEN = shutil.which("age"), shutil.which("age-keygen")


@pytest.mark.skipif(not (AGE and AGE_KEYGEN), reason="needs the age and age-keygen commands")
def test_round_trip_with_a_throwaway_key_prints_aggregates_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert AGE and AGE_KEYGEN
    # A throwaway key and a synthetic label set, encrypted the way the maintainer encrypts TEST.
    keyfile = tmp_path / "key.txt"
    subprocess.run([AGE_KEYGEN, "-o", str(keyfile)], check=True, capture_output=True)  # noqa: S603 - fixed arguments, test-only
    identity = next(x for x in keyfile.read_text().splitlines() if x.startswith("AGE-SECRET-KEY-1"))
    recipient = subprocess.run(  # noqa: S603
        [AGE_KEYGEN, "-y", str(keyfile)], check=True, capture_output=True, text=True
    ).stdout
    keyfile.unlink()
    cipher = tmp_path / "test.age"
    subprocess.run(  # noqa: S603
        [AGE, "--encrypt", "--recipient", recipient.strip(), "--output", str(cipher)],
        input=labels_bytes(),
        check=True,
    )
    assert MARKER.encode() not in cipher.read_bytes()
    digest = hashlib.sha256(labels_bytes()).hexdigest()
    monkeypatch.setattr(sealed_run, "REAUDITED_TEST", {sealed_run.sha256_file(cipher): digest})
    monkeypatch.setenv("EVAL_TEST_KEY", identity)
    cipher_sha = sealed_run.sha256_file(cipher)
    plain = tmp_path / "test.jsonl"
    labels, transcript, answers, _ = scored_inputs(tmp_path)
    labels.unlink()  # only the decrypted copy is scored

    assert sealed_run.main(["expect", "--ciphertext-sha", cipher_sha, "--reaudited", cipher_sha]) == 0
    assert (
        sealed_run.main(["decrypt", "--ciphertext", str(cipher), "--out", str(plain), "--expect-sha", digest])
        == 0
    )
    results = tmp_path / "results.jsonl"
    argv = ["score", "--labels", str(plain), "--transcript", str(transcript), "--answers", str(answers)]
    assert sealed_run.main([*argv, "--results", str(results), "--expect-sha", digest]) == 0
    assert report.main(["--split", "test", "--results", str(results), "--tag", "c0ffee"]) == 0
    out = capsys.readouterr().out
    assert f"plaintext_sha256={digest}" in out and "decrypted=true" in out and "scored=3" in out
    assert "| **Overall** | 3 | 2 |" in out
    for text in (MARKER, "syn-a", "syn-b", "syn-c", "Toyland", "pubmed"):
        assert text not in out  # no question, id or label value in what the workflow prints

    # A wrong key decrypts nothing.
    monkeypatch.setenv("EVAL_TEST_KEY", "AGE-SECRET-KEY-1" + "Q" * 58)
    assert (
        sealed_run.main(
            ["decrypt", "--ciphertext", str(cipher), "--out", str(tmp_path / "x"), "--expect-sha", digest]
        )
        == 1
    )
    assert "could not decrypt" in capsys.readouterr().out
    assert not (tmp_path / "x").exists()
