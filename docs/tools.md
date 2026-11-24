# Tools

What each tool does, what you can ask it, and what comes back. You never call these yourself: you ask Claude in
plain words, and Claude picks the tool. This page is here so you know what Claude can do, and so you can check an
answer.

Every tool only reads data. None of them can change anything, upload anything or see patient information.

**Contents:** [How results look](#how-results-look) · [Finding data](#finding-data) · [Getting numbers](#getting-numbers) ·
[Checking numbers](#checking-numbers) · [Analysis tools (coming)](#analysis-tools-coming) ·
[Last resort](#last-resort-query_sql) · [When a tool says no](#when-a-tool-says-no) · [Helpers](#helpers)

| Tool | What it answers |
|---|---|
| [`list_sources`](#list_sources) | What data is there? |
| [`search_datasets`](#search_datasets) | Which datasets cover this topic? |
| [`describe_dataset`](#describe_dataset) | What exactly is in this dataset, and what can't it tell me? |
| [`suggest_datasets`](#suggest_datasets) | Can public data answer my research question? |
| [`get_series`](#get_series) | How many, where, and when? |
| [`get_aligned_with_gbd`](#get_aligned_with_gbd) | The same numbers with GBD's names, and what to ask GBD next |
| [`get_rate`](#get_rate) | How common is it here, per 100,000? |
| [`get_provenance`](#get_provenance) | Where exactly did these numbers come from? |
| [`list_caveats`](#list_caveats) | What can't this data tell me? |
| [`find_prior_work`](#find_prior_work) | Has this been looked at before? (coming) |
| [`compare_rates`](#analysis-tools-coming), [`get_trend`](#analysis-tools-coming), [`age_adjust`](#analysis-tools-coming), [`check_signal`](#analysis-tools-coming) | Analysis tools (coming) |
| [`query_sql`](#last-resort-query_sql) | Anything the others can't do |

## How results look

- **A table, then notes.** Results come back as CSV, a plain table you can paste into a spreadsheet, followed by
  lines starting with `#` that say where the numbers came from, how they were read, and what the data can't tell
  you. Claude should repeat those notes in its answer. If it doesn't, ask it to.
- **Every number is traceable.** Rows carry their `source_id` and `retrieved_at` (when the data was fetched). The
  notes add the publisher's web address and a one-line method. [`get_provenance`](#get_provenance) gives the exact
  files.
- **Every number is labelled.** The `measure` column says whether it is a count, rate, percent or estimate, and
  `period_status` says whether a year is final or provisional. See [Data conventions](data-conventions.md).
- **First use of a restricted source.** Sources whose terms don't allow anyone to host a copy (today, WHO) are
  downloaded from the publisher the first time you ask. That answer starts with a one-line note saying so; later
  questions use the copy already fetched.
- **Structured output.** Every tool also takes `fmt="json"`, which returns the same columns, rows and notes as
  structured data. Claude uses this when it needs to; you don't need to ask.

## Finding data

### `list_sources`

**What's loaded.** One row per source: who publishes it, its years and places, how often it updates, when it was
last fetched, its licence, and whether its contract has been reviewed.

| Parameter | Required | What it does |
|---|---|---|
| `condition` | no | Only sources that report this condition, e.g. "legionellosis" |
| `geography_level` | no | Only sources that go down to `nation`, `region`, `state`, `city`, `country` or `who_region` |

**Returns:** `source_id`, `title`, `publisher`, `years`, `geography`, `updated`, `last_fetched`, `licence`,
`review`. A note explains that `review = draft` means no maintainer has checked the contract yet.

**Try:** "Use health-data-registry: what data do you have on tuberculosis?"

### `search_datasets`

**Datasets that fit a topic,** ranked, each with one line on why it fits.

| Parameter | Required | What it does |
|---|---|---|
| `topic` | no | Words such as "legionella" or "syphilis in states" |
| `id_area` | no | Research area: `infectious_disease`, or `denominator` for population |
| `geography` | no | Geography level, as for `list_sources` |
| `year_start`, `year_end` | no | Only datasets that overlap these years |

**Returns:** up to five rows of `source_id`, `title`, `agency`, `years`, `geography`, `why_it_fits`. The note
suggests `describe_dataset` next.

**Try:** "Which datasets cover carbapenem resistance by state?"

### `describe_dataset`

**One dataset explained in clinical words:** what one row is, which conditions you can ask for and their case
definitions, what each column means, which rows are totals, which numbers can be added up and which can't, the
caveats, the open questions, how to cite it, its licence and review status.

| Parameter | Required | What it does |
|---|---|---|
| `source_id` | yes | The dataset, e.g. `cdc_nndss_weekly` |

**Returns:** a short plain-text description (or the same as structured data with `fmt="json"`).

**Try:** "Before we use it, describe the NNDSS weekly dataset: what can't it tell me?"

### `suggest_datasets`

**For a research question in your own words:** which sources can answer it, which can't, and why.

| Parameter | Required | What it does |
|---|---|---|
| `question` | yes | Your question, e.g. "Is congenital syphilis rising in Michigan since 2022?" |

**Returns:** one row per source with `source_id`, `can_answer` (yes or no), `why` (for example "goes down to
nation only, not state", or "covers 2011-2022 only") and `what_it_supports`. The notes say how your question was
read (condition words, places, years), and repeat that public data shows what and where, while your hospital's own
data, kept in approved systems, answers why.

**Try:** "Which public datasets could support a project on C. diff in older adults?"

## Getting numbers

### `get_series`

**Observed numbers over time** for a condition and places: one row per place and year, each with its source,
fetch time and case definition.

| Parameter | Required | What it does |
|---|---|---|
| `condition` | yes | The condition, in the source's words or a plain label, e.g. "legionellosis", "Candida auris, clinical" |
| `locations` | no | Places, e.g. `["Michigan", "Ohio", "United States"]`. Names, postal codes and FIPS codes all work. Leave empty for every place. |
| `year_start`, `year_end` | no | The years to return |
| `measure` | no | Only one kind of number: `cases`, `rate`, `percent`, `estimate` (or `population` for Census) |
| `sex` | no | Today only both sexes; no loaded source splits these series by sex |
| `source_id` | no | Only this source, when more than one reports the condition |

**Returns:** `location`, `year`, `value`, `lower`, `upper`, `period_status`, `measure`, `condition`,
`case_status`, `case_definition`, `source_id`, `retrieved_at`. Then notes: the method, the publisher's web
address, the main caveats, whether the contract is still a draft, and whether the local copy is a partial sample.
`lower` and `upper` are filled only where the publisher gives bounds; empty values are missing or suppressed, not
zeros.

**Try:** "How many clinical C. auris cases did Michigan and Ohio report each year from 2022 to 2025?" The
[README example](../README.md#example) shows the real answer.

### `get_aligned_with_gbd`

**The same observed rows, labelled with GBD's place and cause names,** plus the exact IHME GBD connector call to
run next. This tool never calls GBD and never judges the two against each other.

| Parameter | Required | What it does |
|---|---|---|
| `condition` | yes | As for `get_series` |
| `locations` | no | As for `get_series` |
| `year_start`, `year_end` | no | As for `get_series` |

**Returns:** `gbd_location_name`, `fips`, `year`, `condition`, `observed_value`, `period_status`, `measure`,
`case_status`, `source_id`, `retrieved_at`, `gbd_cause`, `gbd_pathogen`, `suggested_gbd_call`. The suggested call
is `get_multi_location_trend` (for a cause) or `get_etiology` (for a pathogen), with years capped at 2023, the last
GBD 2023 year. Notes repeat the [GBD rules](data-conventions.md#using-it-with-gbd).

**Try:** "Line up reported legionellosis in the Great Lakes states with GBD names, then run the GBD call it
suggests."

### `get_rate`

**A crude rate per 100,000** from case counts and Census population, labelled as computed here and never
age-standardised.

| Parameter | Required | What it does |
|---|---|---|
| `condition` | yes | A condition reported as case counts |
| `year` | yes | One year |
| `locations` | no | As for `get_series` |

**Returns:** `location`, `year`, `condition`, `cases`, `population`, `crude_rate_per_100k_computed`,
`case_source`, `population_source`, and a note that the rate is crude and must not be compared with
age-standardised GBD or CDC rates.

The population is the Census Bureau's 1 July estimate (Vintage 2025) for states and the nation, 2020–2025.
Coming: an exact Poisson 95% interval on the rate.

**Try:** "What was the crude rate of reported legionellosis per 100,000 in Michigan in 2024?"

## Checking numbers

### `get_provenance`

**Where a source's numbers came from:** the exact web addresses fetched, when, a fingerprint of each file, the
dataset version and contract version, the case definitions, and how to cite it.

| Parameter | Required | What it does |
|---|---|---|
| `source_id` | yes | The dataset |
| `year` | no | Adds a note that every row for that year comes from the fetches listed |

**Returns:** `source_id`, `url`, `retrieved_at`, `content_sha256`, `snapshot_id`, `contract_version`, then the
case definitions and the citation. The same `content_sha256` means the same bytes, so a mentor can confirm you used
the same data.

**Try:** "Give me the provenance and citation for the NNDSS numbers you just used."

### `list_caveats`

**What the data can't tell you:** each source's caveats, the facts not yet confirmed with the publisher, whether
the contract is still a draft, and the place traps that apply everywhere.

| Parameter | Required | What it does |
|---|---|---|
| `source_id` | no | Only this source |
| `condition` | no | Only sources that report this condition |

**Returns:** `source_id`, `kind` (`caveat`, `not yet confirmed`, `review` or `geography`), `caveat`.

**Try:** "What are the caveats for C. auris data?"

### `find_prior_work`

**Conference abstracts, case reports and health department reports** on a condition, with the datasets each used.

| Parameter | Required | What it does |
|---|---|---|
| `condition` | yes | The condition |
| `geography`, `years`, `dataset` | no | Narrow the search |

Coming: the index isn't built yet. Today the tool says so, returns nothing rather than a partial list, and suggests
searching PubMed (and OpenEvidence if you use it) and the IDWeek abstracts in the Open Forum Infectious Diseases
supplements.

## Analysis tools (coming)

Coming: these are being built now and are not available yet. Each will return the number, its 95% interval, the
method in one plain sentence, and what the data can't say, and will keep the source's suppression and
unreliable-rate flags.

| Tool | What it will answer | Method |
|---|---|---|
| `get_rate` (with interval) | How common is it here? | Crude rate per 100,000 with an exact Poisson 95% interval |
| `compare_rates` | Is it higher here than there, or now than then? | Rate ratio and rate difference with 95% intervals |
| `get_trend` | Is it rising? | Annual percent change with a 95% interval, split where the case definition changed; provisional years marked |
| `age_adjust` | Does age explain the difference? | Direct standardisation to the 2000 US standard population, with gamma (Fay–Feuer) intervals as NCHS uses; only where age-specific counts and populations exist; never compared with GBD's age-standardised rates |
| `check_signal` | Is this week or quarter unusual? | Compared with the same period in the past five years, in the style of CDC's historical limits method for NNDSS |

## Last resort: `query_sql`

**For questions the other tools don't cover.** Claude writes one read-only database query over the loaded
sources; each table is named by its `source_id`.

| Parameter | Required | What it does |
|---|---|---|
| `sql` | yes | One `SELECT` query |

**Returns:** the table, then the query that ran, the method, the source and its fetch date, and the caveats.
Results are capped at 5,000 rows.

It refuses anything but a careful read: more than one statement, anything that writes or reads files, functions
not on its allowlist, joins between tables that aren't declared, and queries that run too long. Prefer the named
tools: they apply the contract's rules for you (which rows are totals, what can be added up), and `query_sql`
leaves that to whoever writes the query.

## When a tool says no

A tool never shows a stack trace or an error code to you. It says, in plain words, what happened and what to try.
The common ones:

| What you see | What it means | What to do |
|---|---|---|
| "No loaded source reports '…'. Conditions available now: …" | The condition isn't in any loaded source, or is spelt differently | Use one of the listed names, or ask `suggest_datasets` |
| "… has no rows for: …" | That place isn't in the source (for example, a state in a source that covers EIP sites only) | Check `describe_dataset` for the places it covers |
| "None of the loaded sources split this by sex yet" | No sex breakdown in these series | Ask again without sex |
| "Rates and estimates can't be added up …" | More than one published rate matches, and rates are never combined | Choose the breakdown it names |
| "… is a running total, so it is read once per …" | A running total asked for the wrong way | Ask by year |
| "Population counts (Census) aren't loaded yet …" | `get_rate` has no denominator on this setup | Use `get_series`, or a published rate |
| "… could not be reached to download …" | A restricted source (WHO) is fetched from the publisher, and the publisher's site didn't answer | Try again in a few minutes |
| "The hub release … has no file …" or "… does not list …" | The hub release the server reads from is incomplete | Try again later, or [open an issue](https://github.com/jtouley/health-data-registry/issues) |
| "The index … isn't built yet" | `find_prior_work` is coming | Search PubMed |
| "The data for … hasn't been downloaded on this computer yet" | Local install only: the one-time load step hasn't run | Repeat step 4 of the [local install](setup.md#one-time-install-on-your-computer-for-now) |
| "That query can't run: …" | `query_sql` refused the query | Use `get_series` or `describe_dataset` instead |
| "Something went wrong while reading the data, so no numbers are shown" | Anything unexpected | Try again; if it keeps happening, run the [Something's wrong](helpers.md#somethings-wrong) helper |

## Helpers

Helpers are ready-made starters for each step from setup to pitch. They appear in Claude's menu once the connector
is added; if your Claude app doesn't show them, copy them from the [helpers page](helpers.md).

| Helper | What it does for you |
|---|---|
| `start_here` | Tests each connected tool (this one, GBD, PubMed and others) and says which work and how to add any that are missing |
| `set_up_my_project` | Writes the Project instructions to paste, with the guardrails, and says where to paste them |
| `spot_a_pattern` | For a condition and a place, checks GBD, then the reported numbers, and shows what changed and what the data can't say |
| `check_its_new` | Searches PubMed, OpenEvidence if connected, and the prior-work index, then says what has been done and where the gap is |
| `find_data_for_my_question` | Takes your question in your own words and returns datasets that fit, with what each can't answer |
| `draft_my_pitch` | Builds a one-page summary with every number's source, case definition and citation, ready for a mentor |
| `help_me_fix_it` | Asks what you see and gives the fix one step at a time |
| `walk_me_through_install` | Step-by-step help installing an optional tool in Claude Desktop; never asks for a password |
