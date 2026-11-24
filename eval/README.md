# Behaviour evaluation

This folder scores how a client using the server behaves on plain clinical questions. For each question, the
scorer checks whether the reply does the right kind of thing (answers, declines, hands off to the IHME GBD
connector, asks which series is meant), uses the right dataset and place, carries provenance on every number,
and invents no numbers. The scoring rules are those of the baseline run at commit `6059a85`, so a later run
can be compared with it.

| File | What it holds |
|---|---|
| `scorer.py` | Scores one question from its expected record, the tool calls, their responses and the final answer |
| `numbers.py` | Reads numbers as tokens (value, precision, year or count) and finds the sums the rubric allows |
| `stats.py` | Wilson 95% interval, McNemar's test for before/after runs, the DEV−TEST gap, per-class tables |
| `report.py` | Writes aggregate tables; prints per-question rows for DEV only |
| `dev/dev.jsonl` | The DEV questions: 29 expected records, no answers |

## The split

There are 84 questions in two splits, with labels pinned to the catalog at the baseline commit.

- **DEV**: 29 questions (25 drawn in the split, plus 4 trial questions added after it). Read them, tune
  against them and print their results freely. sha256
  `7591b7f5294b25ccf582be2a24ae07584d1b6fa6c5dbaf96a2806a64c3f8ab7a`.
- **TEST**: 55 questions, held out. The 80 new questions were split by expected class with largest-remainder
  allocation and seed `20261124`.

### TEST stays sealed

- TEST is stored encrypted with [age](https://age-encryption.org) in the private Hugging Face dataset
  `jtouley/health-data-registry-ops-eval`. It is never committed to this repo.
- Its frozen plaintext sha256 is `6d689deb8179559a7d676587c6c3d5b91980af7a21ca64f72488121778646c53`.
  `scorer.load_expected` checks it before scoring. A mismatch voids the run.
- Its per-question output is never printed. `report.py` refuses per-question rows, regressed ids and tag
  breakdowns for any split other than DEV, and reports TEST as aggregate tables only.
- Run TEST rarely: once before a fix and once after.
- Hard rule 14 in [CLAUDE.md](../CLAUDE.md) applies: sealed sets stay sealed. Don't read TEST while you
  develop, and never change prompts, contracts, synonyms, place lists or metrics because of a TEST result.

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

The rules in `scorer.py` are numbered as in the baseline (R1 to R9), plus S1 to S8 that this port had to make
explicit, T1 to T3 for the target flags and H1 to H5 for the hosted run. The number rules are N1 to N6 in
`numbers.py`. Each function's docstring states its rule.

### Where the scorer differs from the baseline

The baseline was scored by a model reading each transcript. The scorer replaces that reading with fixed
rules:

- **Fabrication** compares numbers, not digits. Each number in the reply's prose must equal a number a tool
  returned, rounded to the decimals the reply shows. A percentage also matches a fraction (12.5% and 0.125).
  A year counts only as a year: "in 2024" must match a year somewhere in the transcript, and "2024 cases" must
  match a returned count, never a year in the call arguments or a year column. Sums the rubric allows (weekly
  counts added across weeks, counts added across places without total rows) pass and are listed apart as
  derived numbers. Sums it forbids (year-to-date across weeks, a total row added, two series added, rates or
  maxima added) fail, and the result says which. `score_question(..., legacy=True)` runs the baseline's
  substring check instead, for comparison only.
- **Class** reads the structured decline fields in `DECLINE_FIELDS` (`next_step`, `did_you_mean`,
  `nearest_served`, `served_elsewhere`, `outside_registry`, `filter_matched_nothing`) when a tool response
  carries them, and falls back to fixed phrases when it doesn't. Wording outside the phrases is still missed
  when the fields are absent.
- **Target** also reads the tool calls. With no number shown, the dataset a call resolved to counts as used.
  A SUM over a total row and places, over two labels, or over New York and New York City caps Target at 1,
  and so does a shown number reached the same way. A publisher's successor series (R9) is not detected.
- **Provenance**: of the caveats, only "provisional" is checked (S6), ignoring "no provisional status".
  Year-to-date reading, '-' flags and the series caveat in each question's notes are not.
- **Hosted run**: with `hosted=True`, rules H1 to H5 score a run whose data tools say the data hub has no
  release. They come from `scoring/hosted_answers.py`, rewritten to read the transcript instead of naming
  questions.
- **Protocol B.** The baseline set `sql_needed` by hand for each question. Here it is a rule (S5).

For what the rules can't judge, pass a `JudgementGrader` to `score_question`. `LLMGrader` wraps any model call
with one fixed prompt. The grader is off by default and can only lower a score: where two scorers disagree,
the stricter one stands.

## Running a report

Score each question with `scorer.score_question`, write one `to_row(split)` per line, then run:

```sh
uv run python -m eval.report --split dev --results run.jsonl --questions
uv run python -m eval.report --split test --results test_run.jsonl --before test_before.jsonl --tag <commit>
```

`--before` adds McNemar's test against an earlier run on the same split and labels. `--questions` adds
per-question scores and works for DEV only. Keep result files for TEST outside the repo.

## Statistics

- **Wilson 95% interval** (z = 1.96), the same function as `health_data_registry.evaluation.wilson`.
- **McNemar's test** on paired PASS/FAIL for the same question ids. With fewer than 25 discordant pairs it
  uses the exact two-sided binomial test, p = min(1, 2·P[X ≤ min(b, c)]) with X ~ Binomial(b + c, ½).
  Otherwise it uses χ² = (|b − c| − 1)² / (b + c) with 1 df. Make the call from the overall test and the
  list of regressions; per-class tests are descriptive only. A regression on `ANSWER` or a new fabrication
  blocks merging.
- **DEV−TEST gap**: the DEV pass rate minus the TEST pass rate, with Newcombe's hybrid score interval. An
  interval that excludes 0 suggests tuning has leaked into DEV.

Any fabrication on TEST blocks a release, whatever the pass rates.
