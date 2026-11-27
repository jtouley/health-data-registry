# Publication outlook

`get_publication_outlook` (CLI: `health-data-registry publication-outlook`) tells a fellow how often abstracts
presented at a conference became full papers. It is a **publication outlook**, never a probability that their idea
will be published. Today it is **base rate only**: model version none, calibration not yet assessed.

The rule it is built around: never show a number that looks more precise than its evidence.

## Method today

**Frame: per conference.** The base-rate file (`src/health_data_registry/publication_base_rates.yaml`, shipped
in the wheel) is keyed by conference, in this order: CROI, IDWeek, ESCMID Global, SHEA Spring, IAS/AIDS,
STI & HIV World Congress, ASM Microbe, AASLD, PIDS/ESPID, APIC, ICID, Michigan ID Society. A conference uses only
a base rate measured at that conference. It never borrows another meeting's rate or a pooled review's; the
loader refuses a file that tries.

**Stages, one row each.** The path from idea to paper has three stages:

| Stage | Step | Today |
|---|---|---|
| 1 | idea → finished study | unknown: no published rate |
| 2 | finished study → accepted abstract | unknown unless the meeting publishes its acceptance rate; IDWeek, SHEA, CROI and the others are unverified |
| 3 | presented abstract → full paper | the conference's verified base rate; only IDWeek has one (Johnson 2022). Every other conference: "unknown: no verified base rate yet" |

A stage with no data is never multiplied in, and no overall product is computed. With no `conference`, the tool
shows stage 3 for every conference side by side, not a pooled number.

**What a fellow sees** (stage 3, IDWeek):

> Of 887 sampled IDWeek 2017–2018 abstracts, 236 were full papers within the source's follow-up (3 years from
> the meeting for 2017 abstracts, 2 years for 2018 abstracts): about 1 in 4 (95% interval 24–30%). Source:
> Johnson 2022; n = 887; calibration: base rate only. This is one meeting and one field (infectious diseases).
> It is the average for all abstracts at that meeting, not a prediction for your idea: your mentor, your team,
> protected time and finishing the study matter most, and none are in the data. The chance of finishing the
> study and of the abstract being accepted is unknown.

- The interval is a Wilson score interval from n and k, rounded to whole percents. The natural frequency
  ("about 1 in 4") is the simplest fraction with a denominator up to 10 that lies inside that interval; if none
  does, only the interval is shown. There is no bare point estimate in any output.
- The event definition follows the source: a full peer-reviewed article found by a
  standardised search of PubMed, International Pharmaceutical Abstracts and Google Scholar.
- The feature arguments (`design`, `data_sources`, `condition`, `geography`) are checked and echoed back as
  "features recorded but not used until the model passes its calibration gate". An unknown design or conference
  is refused in plain words with the accepted values.

## Sources

Every number was checked on 24 Nov 2026 against the primary source (PubMed through NCBI E-utilities and, for
Johnson, the PMC full text). Status: draft, pending maintainer review.

| Source | Role | What it says | Notes |
|---|---|---|---|
| Johnson AJ et al. Open Forum Infect Dis 2022;9:ofac415. PMID 36147600, doi 10.1093/ofid/ofac415 | IDWeek stage 3 | 236 of 887 published (26.6%); oral 132/382 vs poster 104/505 (34.6% vs 20.6%; 35% vs 21% in the abstract); median 10.9 months (IQR 5.0–17.7) among the published | Random sample (Excel) of the 4355 accepted abstracts listed in the IDWeek portal, 433 from 2017 and 454 from 2018; whether the draw was stratified by year is not stated. Follow-up 3 years (2017) vs 2 years (2018): 154/433 vs 82/454 |
| Scherer RW et al. Cochrane MR000005.pub4, 2018. PMID 30480762 | Context only | 37.3% (95% CI 35.3–39.3%), 307,028 abstracts in 425 reports, varying follow-up | Shown only beside a conference's own rate, never as any conference's rate |
| IDWeek, SHEA, CROI and the other meetings' acceptance rates | Stage 2 | unknown | No number until the meeting's own published rate is verified |
| CROI abstract-to-paper rate | Stage 3 | unknown | Being researched separately; the slot is marked in the YAML |

**Discrepancies with the brief.** The brief described the event as a "full PubMed-indexed paper"; Johnson
searched PubMed, IPA and Google Scholar for any peer-reviewed article, so the event follows the source. The brief
gave one frame for 2017–2018, but follow-up differed by year and 2018's overlapped the COVID-19 pandemic, so the
rate has no single horizon. The other reported numbers match.

## The planned model (not built)

Stage 3 later becomes a Kaplan–Meier cumulative incidence of full publication at 12, 24 and 36 months, fitted
from labelled abstracts with censoring, per conference.

- **Records are abstract-level only.** No person-level data beyond what an abstract and a paper publish.
- **Matching rule** (abstract to paper): first or last author surname and initial, plus at least half the title
  words or 3 rare terms, and the paper appears no earlier than 3 months before the meeting. Ambiguous matches go
  to a maintainer; nothing is matched by a model alone.
- **"Similar" strata** need at least 30 abstracts; otherwise the pooled estimate for that conference is used and
  the output says so.
- **Holdout by follow-up time**, not by latest year: freeze the data at time T, then score the 12- and 24-month
  incidence on abstracts whose follow-up reaches each horizon, with a censoring-adjusted (IPCW) Brier score
  against the base rate and reliability bins.
- **Strata ship only if they beat the pooled curve.**
- **Pinned** in `models/publication-v<n>.yaml`; the method is fixed per release and never tuned on blind sets.

## The gate

Features change the output only after the model passes this gate:

- a held-out Brier score (IPCW, by follow-up time as above) that beats the base rate;
- about 200 or more labelled abstracts, including at least 50 published.

Until then the basis stays "base rate only" and calibration "not yet assessed". Once it passes, the calibration
line shows the Brier score against the base rate.

## Risks

- **Labels miss papers.** Searches miss papers in journals the databases don't index, and matches fail when an
  author's name or the title changes. The base rate is probably an undercount.
- **Only accepted abstracts are seen.** Rejected and never-submitted work is invisible, so stages 1 and 2 stay
  unknown and the stage 3 rate says nothing about them.
- **Small numbers.** One meeting, two years, n = 887; strata will be smaller still, hence the 30-abstract floor
  and the interval on every number.
- **Designs differ.** Ecological and surveillance-data designs, which this server serves, may publish at
  different rates from trials or cohorts; nothing here measures that yet.
- **Fellows may chase the number.** A higher-looking outlook is not a better project. The outlook is never used to
  rank ideas, fellows or projects. Its base rate may inform how output formats are compared for one project
  (`choose_output_format`, below), and nothing else.

## Choosing an output format

`choose_output_format` (CLI: `health-data-registry choose-output-format`) answers "which format should I aim for?"
for one project: conference abstract or poster, research letter or brief report, full original manuscript, or
viewpoint. It answers the same way every time for the same answers and the same rubric. It is a decision under
the clinician's stated priorities, not a prediction that the work will be published, and it never ranks ideas,
fellows or projects.

**Inputs are structured answers only**, never read from a description (hard rule 9): `contribution` (empirical,
methods_standard, argument, case), `data_maturity` (idea, preliminary_counts, frozen_dataset, adjudicated),
`worked_examples` (a whole number), `needs_methods_section` (true or false), and optionally `conference`,
`weeks_to_deadline` (the deadline of that conference; with no conference it is not used) and `priorities` (from
speed, citable_record, reach, effort, most important first). Anything else is refused in plain words.

**The rubric** is `src/health_data_registry/format_rubric.yaml` (status draft until the maintainer reviews it).
Each criterion gives each format a level from 1 to 5. Each level describes a situation, and each rule names its
basis: a reporting guideline (STROBE, RECORD), a venue's own author instructions (OFID, EID, checked 25 Nov
2026), a published method, or a maintainer's declaration marked "pending review". `validate` refuses a rubric
whose weight, rule, limit or threshold has no source.

**How the answer is made:**

1. **Gate.** A format whose contribution fit is level 1 (for example a viewpoint for new data) can't rank
   first. As [ADR 0018](adr/0018-show-everything-qualify-dont-decide.md) asks, it is still scored and shown,
   flagged "left out of the ranking", with the reason.
2. **Weights.** With no priorities, every criterion in use counts the same (unit weights, Dawes 1979). With
   priorities, they get rank-order centroid weights in the order given (Barron and Barrett 1996) and the other
   criteria share the remaining ranks.
3. **Draws.** 10,000 draws with a fixed seed. Each draw takes the weights from a Dirichlet centred on the
   declared weights (concentration 20, a proposed declaration). Each sourced rate comes from its Beta(k + 1,
   n − k + 1) posterior. `probabilities` are the share of draws in which each format ranks first, so they say how
   often a format wins across reasonable weightings. They sum to 1, and a format left out by the gate gets 0.
4. **Confidence** is (p_max − 1/n) / (1 − 1/n) over the n formats left after the gate, as TypeSafe defines it
   for a Choice. It says how settled the answer is, not whether it is right. The **review** advice is `act` at
   p_max 0.9 or more, `discuss with your mentor` from 0.67, and `too close to call: decide with your mentor`
   below that; that last one shows the flip condition first.
5. **Flip condition.** The answer names the change to one declared weight, between 0.25× and 4×, nearest 1×,
   that would put another format first. It is computed exactly on the declared weights, without simulation.

**Unknowns are never imputed.** A criterion with no sourced rule (reach) or no verified rate (acceptance chance,
for every venue) is left out for every format and named under "not used". The citable-record criterion is defined
by what the format itself yields once accepted: a published abstract, or an indexed article. When the named
conference has a verified abstract-to-paper rate (today only IDWeek, 236 of 887), the abstract can also reach
level 5 with that chance. Without one, the abstract keeps only the record it certainly gives, and the missing
rate is named. Ample time before a deadline earns no advantage. So leaving a criterion out never removes one
format's advantage only.

**Each criterion per format is a Score:** its level (the probability-weighted mean), the probabilities over
levels, the legend from level to situation, the weight, and the source. A rule-based level puts all its
probability on one level; only a sourced rate spreads it. Every answer carries the rubric's version, status and
sha256, the draws and the seed, so it can be reproduced later. The answer follows the shape of TypeSafe's Choice
and Score outputs. Nothing calls their service, and no model's judgement enters a level, a weight or a
probability.

With the defaults, the decision record's case (methods_standard, frozen_dataset, 7 worked examples, a methods
section needed) picks the full original manuscript, with a probability below 1.

## Open questions

- Which meetings publish acceptance rates, and where (stage 2)?
- A verified CROI base rate (in progress elsewhere), then the other conferences in order.
- Whether Johnson's sample was stratified by year, and how many 2018 abstracts were published after the 2-year
  window.
- Where the labelled abstracts for the gate come from, and who adjudicates ambiguous matches.
- Whether a fellow's design maps cleanly onto the design categories the model would use.
- The format rubric's own open questions, listed at the end of `format_rubric.yaml`: the Dirichlet
  concentration, sourced criterion weights, verified acceptance rates, IDWeek's abstract limits and a source on
  readership.
