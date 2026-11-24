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
  rank ideas, fellows or projects, and the decision rubric never treats it as a score.

## Open questions

- Which meetings publish acceptance rates, and where (stage 2)?
- A verified CROI base rate (in progress elsewhere), then the other conferences in order.
- Whether Johnson's sample was stratified by year, and how many 2018 abstracts were published after the 2-year
  window.
- Where the labelled abstracts for the gate come from, and who adjudicates ambiguous matches.
- Whether a fellow's design maps cleanly onto the design categories the model would use.
