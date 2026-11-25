# Behaviour evaluation

This folder scores how a client using the server behaves on plain clinical questions. For each question, the
scorer checks whether the reply does the right kind of thing (answers, declines, hands off to the IHME GBD
connector, asks which series is meant), uses the right dataset and place, carries provenance on every number,
and invents no numbers. The rules are numbered as in the baseline run at commit `6059a85`, and the
differences are listed below, so a later run can be compared with it.

| File | What it holds |
|---|---|
| `scorer.py` | Scores one question from its expected record, the tool calls, their responses and the final answer |
| `numbers.py` | Reads numbers as tokens (value, precision, year or count) and finds the sums the rubric allows |
| `stats.py` | Wilson 95% interval, McNemar's test for before/after runs, the DEV−TEST gap, per-class tables |
| `report.py` | Writes aggregate tables; prints per-question rows for DEV only |
| `drive.py` | Asks each question through the MCP server with the Claude API and writes the transcript and answers |
| `drive_prompt.md` | The driver's fixed system prompt (versioned; its sha256 is recorded in every run) |
| `dev/dev.jsonl` | The DEV questions: 29 expected records, no answers |

Quick start, on DEV: drive the questions through the server ([Driving a run](#driving-a-run)), score the run
into `run.jsonl` ([Running a report](#running-a-report)), then

```sh
uv run python -m eval.report --split dev --results run.jsonl --questions
```

## The split

80 questions were split by expected class (largest-remainder allocation, seed `20261124`): 25 to DEV and 55
to TEST. Four trial questions were added to DEV afterwards, so DEV has 29 and the total is 84. Labels are
pinned to the catalog at the baseline commit.

- **DEV**: 29 questions. Read them, tune against them and print their results freely. sha256
  `7591b7f5294b25ccf582be2a24ae07584d1b6fa6c5dbaf96a2806a64c3f8ab7a`.
- **TEST**: 55 questions, held out.

### TEST stays sealed

- TEST is stored encrypted with [age](https://age-encryption.org) in the private Hugging Face dataset
  `jtouley/health-data-registry-ops-eval`. It is never committed to this repo.
- Its frozen plaintext sha256 is `6d689deb8179559a7d676587c6c3d5b91980af7a21ca64f72488121778646c53`.
  `scorer.load_expected` checks it before scoring. A mismatch voids the run.
- `report.py` raises `SealedSplitError` for per-question rows and tag breakdowns on any split other than DEV.
  For TEST it gives the number of regressions and withholds their ids, so TEST is reported as aggregate
  tables only. The rule stops at the report: `scorer.score_question` and `to_row` have no guard, so a TEST
  results file holds one row per TEST question. Keep it outside the repo.
- Run TEST rarely: once before a fix and once after.
- Hard rule 14 in [CLAUDE.md](../CLAUDE.md) applies: sealed sets stay sealed. Don't read TEST while you
  develop, and never change prompts, contracts, synonyms, place lists or metrics because of a TEST result.

This is the second held-out set in the repo. The first, in `eval/sealed/` and checked by `make eval`
(`health_data_registry.evaluation`), scores the numbers a client returns against hand-computed answers and
gates a release on accuracy. This one scores behaviour (class, target, provenance, fabrication) from whole
transcripts. `make eval` does not cover it: it is scored with `scorer.py` and `report.py` as below.

### Rule 14 disclosure

The scorer was touched once after a TEST answer was inspected. An earlier build flagged a TEST answer as
fabricated; looking at the flagged token showed two general tokeniser faults: the second year of a range such
as "2022-2023" was skipped, and a year followed by "the publisher reports" was read as a count. Both fixes are
general, and the server under test was not changed. They are kept, and each is proved on its own by synthetic
tests (`test_second_year_of_a_range_is_checked_as_a_year`, `test_year_followed_by_reports_is_a_year_not_a_count`,
`test_rule_14_tokeniser_rules_each_on_their_own`). DEV holds expected records only, no answers, so it gives no
evidence either way. The TEST baseline is marked "scorer touched once (rule 14 disclosure)".

## The protocol

Each question carries one of six expected classes:

| Class | The reply must |
|---|---|
| `ANSWER` | Give a number or series from the named dataset |
| `DECLINE_NEAREST` | Say the place isn't served, and name the nearest served place or places |
| `DECLINE_ELSEWHERE` | Decline the measure or source asked for, and point to the catalogued dataset that fits |
| `HANDOFF_GBD` | Hand modelled burden to the IHME GBD connector, with no "GBD vs local" verdict |
| `DISAMBIGUATE` | Ask which condition or series is meant before giving any number |
| `OUT_OF_SCOPE` | Say no catalogued source covers it, with no number |

Each reply gets three scores from 0 to 2 (Class, Target, Provenance) and a fabrication gate. A question
passes only when the gate passes and all three scores are 2. A reply that shows no number has provenance
N/A, which counts as 2. A failed gate sets the total to 0.

There are two protocols:

- **A** uses every tool.
- **B** takes `query_sql` away. An answer that depended on `query_sql` fails under B, except for
  `OUT_OF_SCOPE`, where the reply comes out the same without it. Use B for before/after comparisons: A sat
  at 97.6% in the baseline, too close to the ceiling to show a fix.

The rules in `scorer.py` are numbered as in the baseline (R1 to R9), plus S1 to S8, rules the baseline
applied implicitly and this scorer states outright, T1 to T3 for the target flags and H1 to H5 for the hosted
run. The number rules are N1 to N6 in `numbers.py`. Each function's docstring states its rule.

### Where the scorer differs from the baseline

The baseline was scored by a model reading each transcript. The scorer replaces that reading with fixed
rules:

- **Fabrication** compares numbers, not digits. Each number in the reply's prose must equal a number a tool
  returned, rounded to the decimals the reply shows. A percentage also matches a fraction (12.5% and 0.125).
  A sign is read ("-35%"), and both ends of a range ("120-150"). A year counts only as a year: "in 2024"
  must match a year somewhere in the transcript, and "2024 cases" must match a returned count, never a year
  in the call arguments or a year column. Sums the rubric allows pass and are listed apart as derived
  numbers: weekly counts added across neighbouring weeks, and counts added across places without total rows,
  over the whole table, a run of rows, or places the call's filter or the reply names. Each row records how
  many candidate sums were tried (`sums_tried`). Sums it forbids (year-to-date across weeks, a total row
  added, two series added, rates or maxima added) fail, and the result says which.
  `score_question(..., legacy=True)` runs the baseline's substring check instead, for comparison only.
- **Class** reads the structured decline fields in `DECLINE_FIELDS` (`next_step`, `did_you_mean`,
  `nearest_served`, `served_elsewhere`, `outside_registry`, `filter_matched_nothing`) when a tool response
  carries them, and falls back to fixed phrases when it doesn't. A caveat ("'-' means not reported") is not a
  decline, and a follow-up offer ("Would you like the weekly breakdown?") is not a question. For `ANSWER`,
  phrases lower Class only when no data tool returned rows. Wording outside the phrases is still missed when
  the fields are absent.
- **Target** also reads the tool calls. With no number shown, the dataset a call resolved to counts as used.
  A SUM over a total row and places, over two labels, or over New York and New York City caps Target at 1,
  and so does a shown number reached the same way. A publisher's successor series (R9) is not detected.
- **Provenance**: of the caveats, only "provisional" is checked (S6); phrases that deny it, such as "not
  provisional", don't count. Every provenance line the reply gives for a dataset must be complete.
  Year-to-date reading, '-' flags and the series caveat in each question's notes are not checked.
- **Hosted run**: with `hosted=True`, rules H1 to H5 score a run whose data tools say the data hub has no
  release (the hub's own wording, not a `query_sql` refusal). They come from the baseline's
  `scoring/hosted_answers.py`, kept with the baseline run outside this repo, rewritten to read the transcript
  instead of naming questions.
- **Protocol B.** The baseline set `sql_needed` by hand for each question. Here it is a rule (S5).

For what the rules can't judge, pass a `JudgementGrader` to `score_question`. `LLMGrader` wraps any model call
with one fixed prompt. The grader is off by default and can only lower a score: where two scorers disagree,
the stricter one stands.

## Driving a run

`drive.py` plays the clinician's Claude. It starts the server once over stdio (`uv run --no-sync
health-data-registry serve` in `--server-dir`). Then, for each question in file order and one at a time, it
offers the model the server's tools, runs every tool call the model makes, and stops at the final answer.

It needs three environment variables:

| Variable | What it is |
|---|---|
| `ANTHROPIC_API_KEY` | The Claude API key |
| `EVAL_MODEL` | The model id that answers. There is no default: the driver stops with a message when it's unset |
| `HDR_LAKE` | The lake the server reads, as for `make serve` |

`--server-dir` must be a git checkout that has been `uv sync`'d. On DEV, from the repo root:

```sh
export EVAL_MODEL=<model id> HDR_LAKE=$PWD/lake
uv run python -m eval.drive --questions eval/dev/dev.jsonl --server-dir . --out ../run-dev
uv run python -m eval.drive --questions eval/dev/dev.jsonl --server-dir . --out ../run-dev-b --protocol B
```

- **Inputs.** Only each line's `id` and `question` are read; the expected fields are never sent or read.
- **Prompt.** The system prompt is `drive_prompt.md` followed by the server's own `instructions`, as an MCP
  client hands them to the model. Changing the prompt changes its sha256: bump `PROMPT_VERSION` in `drive.py`
  and pin the new hash in `tests/test_eval_drive.py`.
- **Protocol B** (`--protocol B`) takes `query_sql` out of the tools offered and refuses it if the model calls
  it anyway. A refused call never reaches the server, so it is listed in the answer's `refused_calls`, not in
  the transcript.
- **Limits.** 12 API requests and 25 tool calls per question, 120 seconds per tool call, 300 per API request
  and 900 for the whole question (`--max-turns`, `--max-calls`, `--tool-timeout`, `--api-timeout`,
  `--question-timeout`). A question that hits one ends with `status` `turn_cap` or `deadline`; a tool call over
  its timeout returns an error to the model and the question goes on. Overload, rate limits and server errors
  are retried 6 times with backoff (2, 4, 8 ... seconds, or the API's `retry-after`); any other API error, or
  retries running out, stops the run.
- **Temperature is not set.** The API rejects sampling parameters, so two runs of the same questions can
  differ. Compare runs with McNemar's test, not by eye.
- **Privacy.** Nothing is printed but counts (`12/55 done`) and plain error messages: no question, answer,
  argument or tool result. Everything goes to `--out`, which must not already hold a run: `transcript.jsonl`,
  `answers.jsonl`, the server's stderr in `server.log` and library warnings in `drive.log`. The sealed
  workflow relies on this to run TEST.

Both files start with one header record, `{"drive_header": {...}}`: the model, the prompt's version and
sha256, the server instructions' sha256, the server commit (and whether its tree was dirty), the snapshot id
each dataset's `LATEST` names in the lake, the protocol, the tools offered and the limits. Then:

- `transcript.jsonl`: one object per tool call that reached the server: `id`, `tool`, `args`, `result` (the
  text the tool returned), `turn`, `is_error` and `seconds`.
- `answers.jsonl`: one object per question: `id`, `answer` (the final text), `status` (`answered`,
  `turn_cap`, `deadline`, `refusal`, `max_tokens` or `stopped`), `turns`, `tool_calls`, `refused_calls`,
  `stop_reason` and `usage` (tokens, for cost).

### A new baseline

Scores from this driver start a new baseline. The earlier runs (DEV 28/29 and TEST 46/55 at `f8f3625`) were
produced by an agent acting as the client by hand, not by this driver, with a different prompt and no caps, so
they are not directly comparable with a driver run. Compare driver runs with driver runs, at the same
`EVAL_MODEL` and prompt sha256.

## Running a report

To score a driver run on DEV into `run.jsonl` (`transcript_by_question` and `answers_by_question` skip the
header record):

```python
import json
from pathlib import Path

from eval.scorer import answers_by_question, load_expected, score_question, transcript_by_question


def jsonl(path: str) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x]


expected = load_expected(Path("eval/dev/dev.jsonl"), "dev")
calls = transcript_by_question(jsonl("../run-dev/transcript.jsonl"))
answers = answers_by_question(jsonl("../run-dev/answers.jsonl"))
with Path("run.jsonl").open("w") as out:
    for e in expected:
        c, r = calls.get(e.id, ([], []))
        out.write(json.dumps(score_question(e, c, r, answers.get(e.id, "")).to_row("dev")) + "\n")
```

Then:

```sh
uv run python -m eval.report --split dev --results run.jsonl --questions
```

The output starts with the DEV table:

```text
#### DEV

| class | n | passes | pass rate | 95% Wilson CI | mean total | fabrication fails |
```

`--before` adds McNemar's test against an earlier run on the same split and labels. `--questions` adds
per-question scores and works for DEV only.

To score TEST, the plaintext must be decrypted first. The maintainer holds the age key (the eval workflow
reads it from the `EVAL_TEST_KEY` secret). Decrypt into a folder outside the repo, score with
`load_expected(path, "test")` and `to_row("test")` as above (a file whose hash differs voids the run), and
keep the results outside the repo too. Then:

```sh
uv run python -m eval.report --split test --results ../test_run.jsonl --before ../test_before.jsonl \
  --dev-results run.jsonl --tag <commit>
```

`--dev-results` adds the DEV−TEST gap. `--split test --questions` stops with a message and exit code 2
before writing anything.

## Statistics

- **Wilson 95% interval** (z = 1.96), the same function as `health_data_registry.evaluation.wilson`.
- **McNemar's test** on paired PASS/FAIL for the same question ids. With fewer than 25 discordant pairs it
  uses the exact two-sided binomial test, p = min(1, 2·P[X ≤ min(b, c)]) with X ~ Binomial(b + c, ½).
  Otherwise it uses χ² = (|b − c| − 1)² / (b + c) with 1 df. Make the call from the overall test and the
  list of regressions; per-class tests are descriptive only. A regression on `ANSWER` or a new fabrication
  blocks merging. That is the reviewer's decision from the McNemar table: the report doesn't make it.
- **DEV−TEST gap**: the DEV pass rate minus the TEST pass rate, with Newcombe's hybrid score interval. An
  interval that excludes 0 suggests tuning has leaked into DEV.

Any fabrication on TEST blocks a release, whatever the pass rates. The TEST report prints "Release is
blocked." when it finds one; the DEV report only counts them.
