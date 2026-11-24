# Behaviour evaluation

This folder scores how a client using the server behaves on plain clinical questions. For each question, the
scorer checks whether the reply does the right kind of thing (answers, declines, hands off to the IHME GBD
connector, asks which series is meant), uses the right dataset and place, carries provenance on every number,
and invents no numbers. The scoring rules are those of the baseline run at commit `6059a85`, so a later run
can be compared with it.

| File | What it holds |
|---|---|
| `scorer.py` | Scores one question from its expected record, the tool calls, their responses and the final answer |
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

The rules in `scorer.py` are numbered as in the baseline (R1 to R9) plus S1 to S6, the ones this port had to
make explicit. Each function's docstring states its rule.

### Where the port differs from the baseline

The baseline was scored by a model reading each transcript. The scorer replaces that reading with fixed
rules. These parts of the baseline needed judgement that the rules can't reproduce exactly:

- **Class.** Declines, hand-offs, verdicts and clarifying questions are recognised by fixed phrases (S2,
  S4), so wording outside those phrases is missed. The rules don't check that a `DISAMBIGUATE` reply lists
  the catalogued candidates.
- **Target.** A dataset counts only when its id appears in the reply, not its title. A wrong series or
  filter inside the right dataset (a total row added to states, screening added to clinical, New York State
  including New York City) and a publisher's successor series (R9) are not detected.
- **Provenance.** Of the caveats, only "provisional" is checked (S6). Year-to-date reading, '-' flags and
  the series caveat in each question's notes are not.
- **Fabrication.** The number check is the baseline's `fabcheck.py`, ported line for line. It flags every
  derived number, including sums the rubric allows. The baseline set its `fabricated` field by judgement,
  informed by that check.
- **Protocol B.** The baseline set `sql_needed` by hand for each question. Here it is a rule (S5).

For these, pass a `JudgementGrader` to `score_question`. `LLMGrader` wraps any model call with one fixed
prompt. The grader is off by default and can only lower a score: where two scorers disagree, the stricter
one stands.

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
