# 0018. Show everything; qualify, don't decide

- **Status:** Accepted
- **Date:** 2026-11-27
- **Decision:** the maintainer's, 27 Nov 2026
- **Supersedes:** the aggregation rule in [ADR 0007](0007-odcs-contracts-and-semantic-models.md) (rule 2 of the
  "rules carried over from the earlier contract work (ADR017)": "Frequency tables sum their count column and
  exclude total rows. Estimate and rate tables are never summed."), which became hard rule 2 in
  [CLAUDE.md](../../CLAUDE.md#hard-rules). The rest of ADR 0007 stands, including the contract facts rule.
- **Related:** [ADR 0003](0003-provenance-on-every-result.md) (provenance on every result),
  [ADR 0004](0004-aligned-data-no-gbd-verdicts.md) (no GBD verdicts), [ADR 0012](0012-built-for-clinicians.md)
  (built for clinicians), [ADR 0015](0015-elt-raw-data-stored-as-sent.md) (raw data stored as sent); issues
  [#186](https://github.com/jtouley/health-data-registry/pull/186) (`-` never read as 0),
  [#222](https://github.com/jtouley/health-data-registry/issues/222) (a check that never ran),
  [#223](https://github.com/jtouley/health-data-registry/issues/223) (coverage and gaps as data),
  [#225](https://github.com/jtouley/health-data-registry/issues/225) (quality records cited in answers)

## Context

The old hard rule 2 said: frequency tables sum their count column and leave out total rows; rates and estimates
are never summed; published uncertainty ranges are always returned.

That rule protects the numbers by deciding for the reader. The server leaves rows out, refuses some sums, and
turns some publisher codes into values. Each of those choices is reasonable on its own, but the physician never
sees it being made:

- A total row left out of a sum is a row the physician can't check.
- A refusal hides the gap it refused over. "Rates can't be added up" says nothing about what the data do hold.
- NNDSS's `-` was read as 0 (#173, fixed by #186). New Jersey 2024 came back as 0 cases and a rate of 0.0: a gap
  presented as a fact.

Physicians are the people who judge these numbers. They need to trust the data and make their own judgements, and
they can do neither with data that was quietly trimmed or filled in. Gaps also have value of their own: a missing
state, a suppressed cell or a catchment area that covers part of a state can be the research question, for example
a paper on gaps in surveillance data.

## Decision

Hard rule 2 becomes **show everything; qualify, don't decide**:

1. **Every row the publisher sent is returned,** each with its flag and what the flag means: `-` (not reported,
   never turned into 0), suppressed cells, total rows, catchment areas and screening counts.
2. **Any figure we compute (a sum, a rate, a trend) is computed when asked.**
3. **Where our contracts or data-quality findings say the figure may mislead,** the answer says so in plain words
   and puts the safer figure beside it. For example:
   - "this adds the US total row to the states; without it: N";
   - "weekly counts undercount the year; CDC's year-to-date figure: N";
   - "catchment area only; the state population overstates the denominator";
   - "screening detections, not incidence".
4. **Every qualification cites its source:** a contract rule, a quality-rule result or a recorded finding (#225).
5. **Gaps are first-class data** that a researcher can ask for (#223).
6. **Published uncertainty ranges are still always returned.**
7. **One thing still blocks: a quality check that never ran** (#222). Telling physicians the data was checked when
   it wasn't would break their trust. That block is about our own honesty, not a decision made for them.

Hard rule 1 still governs every fact that changes a number. A qualification is such a fact: it comes from a
contract rule, a quality-rule result or a recorded finding, never from a guess, and names which one.

Qualifying is not judging. As in ADR 0004, the server never says which figure is "right"; it says what each figure
is and what it leaves out, and the physician decides.

## What changes from the old rule

| Case | Old rule 2 | This rule |
|---|---|---|
| Total rows | Left out of every sum | Returned as rows, flagged. A sum that includes one says so and gives the figure without it |
| Rates, percents, estimates | Never summed; the tool refuses | Computed when asked, with what the figure can't mean and the safer figure beside it |
| Weekly counts into a year | Not added; the year-to-date column is read instead | Added when asked, qualified: "weekly counts undercount the year", with CDC's year-to-date figure |
| NNDSS `-` | Read as 0 | Returned as sent, with its flag and meaning; never 0 |
| Clinical and screening C. auris | Kept apart; never added | Added when asked, qualified: "screening detections, not incidence" |
| Published uncertainty ranges | Always returned | Always returned |
| A quality check that never ran | Not covered | Blocks the answer |

## Consequences

- Answers get longer. Each qualification must stay short, plain and clinical (ADR 0012), with one next step.
- Contracts carry more. A qualification needs a reason a machine can read: which rows are totals, what a catchment
  covers and over which years (#223), which rules ran and what they found (#225). Prose alone in a contract is
  not enough.
- Quality results move from gatekeeping to evidence. A check that ran and failed becomes a qualification citing
  its rule, not a silent drop. Today a failed error-severity rule still holds a copy back (`quality.py`); changing
  that is code work for a later pull request. A check that never ran still blocks (#222).
- Code catches up separately. This record changes no code. #186 already returns `-` empty with its flag, never
  as 0. It also added refusals (a sum of weekly counts, a sum over a count with no number, no rate for counts that
  are not new cases of disease, such as *C. auris* screening); under this rule each becomes a figure with a
  qualification, in follow-up pull requests, as do the total rows that sums still leave out. Until they land, [docs/data-conventions.md](../data-conventions.md)
  and [docs/tools.md](../tools.md) say where today's tools still refuse or leave rows out.
- Tests and code comments that cite "hard rule 2" for a refusal pin the old rule; they change with the code that
  implements this one.
