# Data conventions

How to read the numbers this tool gives you: what kind of number each one is, how final it is, what an empty cell
means, how places and years are named, and how to set them beside GBD. Every rule here comes from a source's
contract (see [What a contract is](#where-these-rules-come-from)), not from Claude's judgement.

**Contents:** [Measures](#measures-never-mixed) · [Final and provisional](#final-and-provisional) ·
[Empty cells](#empty-cells-suppression-and-missing-codes-never-zero) · [Uncertainty](#uncertainty) ·
[Places](#places) · [Years and weeks](#years-and-weeks) · [Age standardisation](#age-standardisation) ·
[Case definitions](#case-definitions) · [Using it with GBD](#using-it-with-gbd) ·
[Where these rules come from](#where-these-rules-come-from)

## Measures, never mixed

Every row says which kind of number it is, in its `measure` column. There are four kinds:

| Measure | What it is | Example source | Can it be added up? |
|---|---|---|---|
| `cases` (a count) | Cases reported to a health department, as the publisher counted them | NNDSS weekly tables | Yes, across places, with the publisher's total rows left out |
| `rate` | Cases per 100,000 population, as the publisher worked it out | RESP-NET hospitalisation rates; HAICViz C. diff rates | Never |
| `percent` | A share, such as percent of isolates resistant | NHSN antimicrobial resistance | Never |
| `estimate` | A modelled value, not a count of reported cases | WHO tuberculosis incidence | Never |

- **Counts are added; nothing else is.** When a place or year needs a count added up, the tool adds the count
  column and leaves out the publisher's own total rows, so nothing is counted twice. Rates, percents and estimates
  are shown exactly as published. If more than one published rate matches what you asked, the tool refuses rather
  than average or add them, and says which breakdown to choose.
- **Measures never mix in one answer.** If a question matches series of different kinds (for example, a count from
  one source and a rate from another), the answer opens with a warning and the `measure` column tells them apart.
  Never compare or add numbers across measures.
- **A number the tool works out is labelled as such.** `get_rate` divides cases by Census population and returns
  `rate_per_100k` with `lower_95` and `upper_95`; every row of `get_rate`, `compare_rates`, `get_trend`,
  `age_adjust` and `check_signal` ends with a `computed` column saying it was computed here and how, so it can't
  be mistaken for a published rate.
- **Population is an estimate, too.** The denominator is the Census Bureau's estimate of the resident population on
  1 July (Vintage 2025, years 2020–2025). Each new vintage revises every earlier year, so a rate's denominator can
  change between releases. Census publishes no uncertainty for it.
- **Different rate types are different series.** RESP-NET publishes observed, age-adjusted and estimated rates;
  they are kept apart and never combined.

## Final and provisional

Recent surveillance counts change as late reports arrive. Every row from a running-total source carries a
`period_status`:

- **`provisional: as published at year end (through week N)`.** The year's own running total at its last week
  with a number (`observation_type` `year_end_ytd`). Late reports can still change it. Say "provisional" whenever
  you quote it.
- **`provisional: running total so far (through week N)`.** The year is still open: the total so far, not the
  year's count.
- **`incomplete: …`** The running total stops before the year's last week, so it is not the year's total and is
  never used as one.
- **CDC's revised provisional figure, from the next year's table** (`observation_type`
  `subsequent_year_restatement`). The following year's table gives its own figure for the same year, with the
  week it reaches. CDC doesn't say the two count the same thing, and for some states they differ a lot, so both
  are kept, as separate rows: this one never replaces or merges with the year-end figure, and neither is final
  until a domain reviewer approves them as the same count (`resolution_policy: require_domain_review`).

How NNDSS annual counts are read: the tool takes CDC's running year-to-date column at the last week published for
that year. Weekly counts are **never** added up to make a year, because late reports land in the running total,
not in the week they happened: for Michigan in 2023, adding the weekly column gives 62 cases, the year-end running
total 103. A year-to-date column is added across places only at one week; the tool refuses it otherwise.
`query_sql` follows the same rules: it refuses a sum of weekly counts or of year-to-date counts across weeks, and
a sum across places leaves CDC's summary rows out (the US totals, US territories combined and the nine census
divisions); Non-U.S. Residents stays in, as its own bucket. These `query_sql` rules apply to sums only.

An empty NNDSS count flagged `-` is CDC's "no reported cases": the jurisdiction did not submit any cases (dataset
notes). It is read as **not reported**, the maintainer's declaration of 25 Nov 2026, which reverses the earlier
reading as a reported zero. It comes back empty, with the flag and its meaning beside it, and is **never counted
as 0**: no total, rate or trend point is worked out from it. In `get_series` and `query_sql` the flag and its
meaning come back beside the empty value; `get_rate` and `get_trend` name the flag when they leave the year out.
Every other flag (`U` unavailable, `N` not reportable, `NN`, `NP`, `NC`) leaves the cell empty too.

Other sources:

- **NHSN antimicrobial resistance** and **HAICViz C. diff** are annual tables with no provisional flag.
- **RESP-NET** does not say in its metadata how far back recent weeks are revised; this is an open question in
  its contract, and `list_caveats` reports it.
- **WHO** re-estimates its whole tuberculosis series each year, so earlier years can change between releases.
  Cite the fetch date that comes with each row.

## Empty cells: suppression and missing codes

An empty value means the publisher marked it missing, not reportable or suppressed. **It is never a zero**, and
the tool never fills it in or back-calculates it. That includes a code the publisher's codebook defines as "no
reported cases" (NNDSS `-`): the cell stays empty, with its flag and that meaning beside it.

| Source | What an empty cell can mean |
|---|---|
| NNDSS weekly | A flag explains it: `U` the jurisdiction could not send data; `N` not reportable there; `NN` not nationally notifiable; `NP` notifiable but not published; `NC` not calculated. A flag of `-` with an empty count is CDC's "no reported cases", read as not reported; it stays empty with the flag, never 0. |
| NHSN resistance | Resistant count and percent resistant are shown only when at least 20 isolates were tested; smaller cells are empty. |
| RESP-NET | Some estimated cumulative rates for the current season are published empty. |
| WHO tuberculosis | No value published for that country and year. |

NNDSS's `-` is stored and returned as published (empty, with the flag). The maintainer declared on 25 Nov 2026
that it means not reported, never a zero; the declaration is in the contract.

## Uncertainty

- **Observed counts have no uncertainty interval.** A reported case count is what was reported, not an
  estimate. The tool says so with every count.
- **Published bounds are always kept.** Where a publisher gives lower and upper bounds, they come back in the
  `lower` and `upper` columns, next to the value. They are never dropped.
  - **NHSN:** 95% mid-P exact confidence intervals around percent resistant. They describe precision only.
  - **WHO tuberculosis:** WHO's low and high bounds around its estimate. Whether they are 95% intervals is not
    stated in WHO's data feed; this is an open question in the contract.
- **GBD values** (from the IHME GBD connector, not this tool) are modelled and come with 95% uncertainty
  intervals.
- **The analysis tools** (`get_rate`, `compare_rates`, `get_trend`, `age_adjust`, `check_signal`) return each
  number with a 95% interval and its method in one sentence, labelled as computed here.

## Places

One place is one place, whatever the publisher calls it. "Michigan", "MI", "MICHIGAN" and FIPS code 26 all mean
Michigan.

| Kind of place | How it is named | Used by |
|---|---|---|
| US nation | "United States" | NNDSS, NHSN, RESP-NET, GBD |
| US states, DC and Puerto Rico | Full name, two-letter postal code or two-digit state FIPS code | NNDSS, NHSN, RESP-NET |
| GBD location | The plain state name ("Michigan"), or "United States" for the nation: the names the IHME GBD connector accepts | `get_aligned_with_gbd` |
| Countries | ISO 3166-1 three-letter code (for example `NGA`, `IND`) | WHO |
| WHO regions, World Bank income groups, `GLOBAL` | As WHO labels them | WHO |

Place traps the tool reports with each answer:

- **NNDSS counts by state of residence.** Some CDC tracking pages count by the state of the facility, so their
  numbers can differ.
- **New York in NNDSS excludes New York City,** which is reported as its own area.
- **US RESIDENTS in NNDSS excludes territories;** the TOTAL row includes territories and non-US residents.
- **RESP-NET rates cover each site's catchment area,** not necessarily the whole state. "Overall" means all
  participating sites of that network combined, and the site list differs by network and season.
- **HAICViz C. diff covers the Emerging Infections Program catchment areas,** pooled across sites. It has no
  state-level values.
- **NHSN:** small territories are in the national figures but not shown as states; Puerto Rico is shown as a state
  but left out of regions.
- **WHO regions are not GBD regions.** Rows for regions, income groups and the globe are aggregates, not countries.
- **Out-Wayne County reports exclude the City of Detroit,** which has its own health department.

Population (Census): states, DC and counties, 2020–2025. The national figure is the 50 states plus DC; Puerto
Rico and the other territories are not in the file, so no rate is worked out for them. Connecticut is shown by its
nine planning regions, not its former counties. No case source publishes county values yet.

## Years and weeks

- **MMWR weeks and years (NNDSS).** CDC's epidemiological calendar. Weeks run 1 to 52, or 53 in a 53-week year
  such as 2025. An MMWR year can start a few days before or after 1 January, so it is close to, but not exactly,
  the calendar year.
- **Calendar years:** NHSN, HAICViz and WHO.
- **Respiratory seasons and week-ending dates (RESP-NET).** Weekly rows are dated by the week's end
  (`YYYY-MM-DD`); clinical outcome percents (ICU, ventilation, in-hospital death) are per season, not per week.
  A year asked of a date column means that whole calendar year.
- **GBD 2023 ends in 2023.** Observed years after 2023 have no GBD match.
- **Rates mix two calendars.** Case counts are by MMWR year; Census population is for 1 July of the calendar year.
  The two line up closely but not exactly.
- **Freshness.** Every row carries `retrieved_at`, the time the data was fetched from the publisher.

## Age standardisation

- **Crude by default.** Rates the tool works out are crude: cases divided by population, with no age adjustment.
- **Never compared with GBD's age-standardised rates.** GBD standardises to its own world standard population; CDC
  usually standardises to the 2000 US standard population. Rates standardised to different populations cannot be
  compared, and a crude rate cannot be compared with either.
- **RESP-NET's age-adjusted rates** are a separate series. Which standard population they use is not yet
  confirmed, so they must not be set beside GBD.
- `age_adjust` standardises directly to the 2000 US standard population, with gamma (Fay–Feuer) intervals as NCHS
  uses, and only where age-specific counts and populations exist (no loaded source has them yet).

## Case definitions

A case is whatever the publisher's case definition says it is, and a change in definition can make a trend look
like it rose or fell.

- **NNDSS** counts follow the national surveillance case definitions (CSTE position statements, listed at
  [ndc.services.cdc.gov](https://ndc.services.cdc.gov/)). NNDSS does not publish confirmed and probable cases
  separately here, so each count may combine them as the condition's definition allows, and they cannot be split.
- **C. auris:** clinical cases and screening (colonisation) cases are separate series. Never add them.
  Screening counts people found to carry *C. auris*, not new cases of disease, so the tools give no rate per
  100,000 people for it (the NNDSS contract's `notIncidence`); its counts are still returned.
- **RESP-NET** counts only laboratory-confirmed COVID-19, influenza and RSV hospitalisations.
- **NHSN** covers pathogens from healthcare-associated infections reported to NHSN (CLABSI, CAUTI and SSI). It is
  not all clinical isolates in a state. Some phenotypes count non-susceptible rather than strictly resistant
  isolates.
- **HAICViz** splits C. diff into healthcare-associated and community-associated by the EIP's epidemiologic
  classification; its exact definitions are not on data.cdc.gov and are an open question.
- **WHO** tuberculosis incidence covers new and relapse tuberculosis of all forms, including people with HIV.
- `describe_dataset` and `get_provenance` show the case definition where the contract records one. Where the
  contract records the year a definition took effect (`caseDefinitions`), `get_trend` splits the trend there and
  `compare_rates` says so; no real contract records one yet.

## Using it with GBD

This tool never calls GBD, never copies it, and never says which number is "right". It labels observed rows with
GBD's place and cause names so you can set them beside what the IHME GBD connector returns. Follow these rules:

1. **Never compare age-standardised rates across standards.** GBD uses its world standard; CDC uses US 2000.
   Rates from `get_rate` are crude.
2. **Don't mix GBD rounds.** All GBD values in a project come from GBD 2023.
3. **Counts are not estimates.** Observed rows have no uncertainty interval; GBD rows do. Don't call a gap a real
   difference unless the observed value falls outside GBD's 95% uncertainty interval.
4. **Pathogen shares don't add up to the cause total.** The crosswalk maps a condition to a GBD cause and pathogen
   pair, not to a fraction of a total.
5. **Sepsis isn't a GBD cause.** Map sepsis questions to the underlying infectious causes, and use the IHME
   connector's etiology tool for pathogens.
6. **Antimicrobial resistance:** keep deaths *attributable to* resistance and deaths *associated with* resistance
   separate. They are different GBD measures.
7. **Watch the place traps** listed under [Places](#places).

The crosswalk from each source's conditions to GBD names, as recorded in [`catalog/`](../catalog/):

| Condition (source) | GBD cause | GBD pathogen |
|---|---|---|
| C. auris, clinical and screening (NNDSS) | Other infectious diseases | Candida spp. |
| Legionellosis (NNDSS) | Lower respiratory infections | Legionella spp |
| Syphilis, primary and secondary; congenital (NNDSS) | Syphilis | |
| Tuberculosis (NNDSS) | Tuberculosis | |
| MRSA (NHSN) | Other infectious diseases | Staphylococcus aureus |
| Carbapenem-resistant Klebsiella (NHSN) | Other infectious diseases | Klebsiella pneumoniae |
| Carbapenem-resistant E. coli (NHSN) | Other infectious diseases | Escherichia coli |
| Carbapenem-resistant Enterobacter (NHSN) | Lower respiratory infections | Enterobacter spp |
| Carbapenem-resistant Acinetobacter (NHSN) | Other infectious diseases | Acinetobacter baumannii |
| Carbapenem-resistant Pseudomonas aeruginosa (NHSN) | Other infectious diseases | Pseudomonas aeruginosa |
| Influenza hospitalisations (RESP-NET) | Lower respiratory infections | Influenza |
| RSV hospitalisations (RESP-NET) | Lower respiratory infections | Respiratory syncytial virus |
| COVID-19 hospitalisations (RESP-NET) | COVID-19 | |
| C. difficile infection (HAICViz) | Diarrheal diseases | Clostridium difficile |
| Tuberculosis incidence (WHO) | Tuberculosis | |

`get_aligned_with_gbd` returns these names with each row, and the exact IHME GBD connector call to run next:
`get_multi_location_trend` for a cause, or `get_etiology` when the condition maps to a pathogen.

## Where these rules come from

Every fact that changes a number comes from the source's **contract**, never from a guess:

- **A data contract** is a reviewed file, one per source, that says what one row is, what each column means and
  its unit, which codes mean "missing", which rows are totals and what the publisher's own documentation says.
  Contracts follow the [Open Data Contract Standard (ODCS)](https://github.com/bitol-io/open-data-contract-standard),
  version 3.2.0, an open standard from the Bitol project at the Linux Foundation. They live in
  [`contracts/`](../contracts/).
- **A data product file** describes the open health data hub as a whole: which datasets it offers, under which
  licences, and who maintains it. It follows the
  [Open Data Product Standard (ODPS)](https://github.com/bitol-io/open-data-product-standard), from the same
  project: [`contracts/health-data-registry.odps.yaml`](../contracts/health-data-registry.odps.yaml), a draft.
  Each dataset on the hub is one of its "output ports", marked hosted or not, and points to that dataset's
  contract.
- A fact in a contract comes only from the publisher's own documentation, an exact fact about the file's
  structure, or a maintainer's reviewed note. Anything else is written down as an open question, and
  `list_caveats` shows it to you.

**Every contract is still a draft** that no maintainer has reviewed. Every answer says so.
