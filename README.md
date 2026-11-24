# health-data-registry

Ask Claude about public health data — GBD, CDC, WHO and more — in plain English.

[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/jtouley/health-data-registry/badge)](https://scorecard.dev/viewer/?uri=github.com/jtouley/health-data-registry)
<!-- Swap for the real badge once the project has a Best Practices id: https://www.bestpractices.dev/projects/<id>/badge -->
[![OpenSSF Best Practices](https://img.shields.io/badge/OpenSSF_Best_Practices-applying-lightgrey)](https://www.bestpractices.dev/en)
[![CI](https://github.com/jtouley/health-data-registry/actions/workflows/ci.yml/badge.svg)](https://github.com/jtouley/health-data-registry/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/health-data-registry)](https://pypi.org/project/health-data-registry/)

A free, open-source tool for clinician-researchers. You ask Claude a question about infectious disease in plain
words. Claude uses this tool to find the public data that fits, and gives you a table with the source, the date it
was fetched and what the data can't tell you. It works next to the IHME GBD and PubMed connectors you may already
use.

**Status:** pre-alpha. Infectious disease only, five starter datasets, and every dataset's description is still a
draft awaiting review. "health-data-registry" is a working name.

**Contents:** [What it does](#what-it-does-and-doesnt-do) · [Quick start](#quick-start) · [Helpers](#helpers) ·
[Example](#example) · [Tools](#tools) · [Data](#data) ·
[With GBD, PubMed and others](#using-it-with-gbd-pubmed-and-others) ·
[How answers stay correct](#how-answers-stay-correct) · [Contributing](#contributing) ·
[Security, licence, citation](#security-licence-and-citation) · [For developers](#for-developers)

## What it does and doesn't do

It does:

- **Finds the data that fits your question.** Ask "which public datasets could support a project on C. diff in
  older adults?" and get the few that fit, each with its years, places and what it can't answer.
- **Answers with a sourced table.** Every number comes with its source web address, the date it was fetched, the
  dataset version and a one-line method, so you or a mentor can check it.
- **Handles each dataset's quirks for you.** Which code means "missing", which row is a total, and whether a column
  is a count or a rate are written down and reviewed once, so you never pick a file or a column.

It doesn't:

- **Touch patient data.** It reads only public, population-level data. You can't upload anything to it. Never put
  patient information in the chat.
- **Call, copy or second-guess GBD.** GBD comes only through the IHME GBD connector. This tool gives you the
  reported numbers, labelled with GBD's place and cause names, and never says which one is "right".
- **Guess.** If it can't say what a number means, it tells you so in plain words and suggests what to try next.
  It never fills a number from memory or the news.

## Quick start

A **connector** is an add-on that lets Claude use an outside tool. You add it once.

> [!IMPORTANT]
> **For now, this connector needs a one-time install on your computer** and works in the Claude Desktop app, not
> yet in Claude on the web or on your phone. There is no web address to paste yet. The install takes about 15
> minutes: follow [the setup guide](docs/setup.md), or let Claude walk you through it by pasting the
> [Walk me through an install](docs/helpers.md#walk-me-through-an-install) helper into a chat. Once the tool is
> hosted, step 1 becomes pasting one web address into Settings → Connectors.

1. **Add the connector.** Follow [the setup guide](docs/setup.md).
   You should see "health-data-registry" listed in Claude Desktop under Settings → Connectors.
   If you don't, quit Claude Desktop fully and open it again.
   <!-- SCREENSHOT: Claude Desktop, Settings → Connectors, with health-data-registry listed -->
2. **Start a new chat.**
   A connector only appears in chats started after you add it.
   <!-- SCREENSHOT: the new-chat button in Claude Desktop -->
3. **Run the "Start here" helper.** In the message box, open the menu of ready-made starters, choose
   health-data-registry, then **Start here: check my tools work**.
   You should see one line per tool saying whether it works, and steps for any that are missing.
   If the menu doesn't show the helpers, copy [Start here](docs/helpers.md#start-here-check-my-tools-work) from
   the helpers page and paste it as your message.
   <!-- SCREENSHOT: the starters menu in the message box, with "Start here: check my tools work" highlighted -->
4. **Ask one question,** for example: "Use health-data-registry: how many legionellosis cases were reported in
   Michigan each year since 2022?"
   You should see a table, then lines saying where the numbers came from and when.
   If you see a message instead of a table, it says why and what to try. Still stuck? Run the
   [Something's wrong](docs/helpers.md#somethings-wrong) helper.
   <!-- SCREENSHOT: a question and the sourced answer table -->

## Helpers

Helpers are ready-made starters, one for each step from setup to pitch. Once the connector is added, they appear in
Claude's menu of starters. If your Claude app doesn't show them, copy them from the
[helpers page](docs/helpers.md).

| Step | Helper | What it does for you |
|---|---|---|
| Check it works | `start_here` | Tests each connected tool (this one, GBD, PubMed and others) and says which work and how to add any that are missing |
| Set up a Claude Project | `set_up_my_project` | Writes the Project instructions to paste, with the guardrails, and says where to paste them |
| Spot a pattern | `spot_a_pattern` | For a condition and a place, checks GBD, then the reported numbers, and shows what changed and what the data can't say |
| Check it's new | `check_its_new` | Searches PubMed, OpenEvidence if connected, and the prior-work index, then says what has been done and where the gap is |
| Find data for my question | `find_data_for_my_question` | Takes your question in your own words and returns datasets that fit, with what each can't answer |
| Draft the pitch | `draft_my_pitch` | Builds a one-page summary with every number's source, case definition and citation, ready for a mentor |
| Something's wrong | `help_me_fix_it` | Asks what you see and gives the fix one step at a time |
| Optional installs | `walk_me_through_install` | Step-by-step help installing an optional tool in Claude Desktop; never asks for a password |

## Example

<!-- EXAMPLE: from a real run on 24 November 2026 against CDC's NNDSS weekly tables, fetched that day. -->

You ask: *"How many clinical C. auris cases did Michigan and Ohio report each year from 2022 to 2025?"*

Claude calls `get_series`, which returns each figure as its own row with an `observation_type`; here they are
side by side, trimmed to the columns that matter:

| Place | Year | Cases, as published at year end | Same year in next year's table |
|---|---|---|---|
| Michigan | 2022 | 27 (through week 52) | 33 (through week 52) |
| Michigan | 2023 | 103 (through week 52) | 110 (through week 52) |
| Michigan | 2024 | 174 (through week 52) | 184 (through week 53) |
| Michigan | 2025 | 178 (through week 53) | 217 (through week 45 only, not the whole year) |
| Ohio | 2022 | 83 (through week 52) | 83 (through week 52) |
| Ohio | 2023 | 150 (through week 52) | 151 (through week 52) |
| Ohio | 2024 | 320 (through week 52) | 322 (through week 53) |
| Ohio | 2025 | 404 (through week 53) | 335 (through week 45 only, not the whole year) |

Every row also carries its source (`cdc_nndss_weekly`, [data.cdc.gov/d/x9gk-5huc](https://data.cdc.gov/d/x9gk-5huc)),
when it was fetched, and a plain note on how the number was read:

- **Cases, as published at year end** is CDC's running year-to-date count at the last week of that year that has
  a number (`year_end_ytd`). It is always **provisional**; a year whose later weeks have no number says
  **incomplete**.
- **Same year in next year's table** (`subsequent_year_restatement`) is the figure CDC's following yearly table
  gives for that year, with the week it reaches. CDC doesn't say the two count the same thing, and for some
  states they differ a lot, so both are kept and this one is marked an unverified restatement: not a final
  disease count. Neither is final until a domain reviewer confirms they count the same thing.
- Counts are read from CDC's running year-to-date column. Adding up the weekly column would under-count badly,
  because late reports land in the running total, not in the week: for Michigan in 2023 that gives 62, not 103.
- An empty count is never a zero: the flag column carries CDC's reason (`-` no reported cases, `U` unavailable,
  `N` not reportable there).

The same answer lists what the data can't tell you: counts are by state of residence; clinical and screening
cases are separate series; this source's contract is still a draft no reviewer has checked.

## Tools

You don't call these yourself; Claude picks them. They are listed so you know what Claude can do. All of them only
read data.

| Tool | What it gives you |
|---|---|
| `list_sources` | What data is loaded: publisher, years, places, how fresh it is, its licence and whether it has been reviewed |
| `search_datasets` | Datasets that fit a topic, ranked, with one line on why each fits |
| `describe_dataset` | One dataset explained in clinical words: what one row is, case definitions, counts vs rates, caveats, how to cite it |
| `suggest_datasets` | For a research question: which datasets can answer it, which can't, and why |
| `get_series` | Reported numbers over time for a condition and places, each row with its source, fetch date and case definition |
| `get_aligned_with_gbd` | The same numbers labelled with GBD's place and cause names, plus the exact GBD connector question to ask next |
| `get_rate` | A crude rate per 100,000 from case counts and Census population, marked as computed here and never age-standardised |
| `get_provenance` | Where the numbers came from: web addresses, fetch dates and a fingerprint of each downloaded file |
| `list_caveats` | What the data can't tell you: place traps, hidden small numbers, provisional years, unreviewed descriptions |
| `find_prior_work` | Conference abstracts, case reports and health department reports. Not built yet; it says so rather than guess |
| `query_sql` | A last resort for questions the others don't cover; it refuses anything but a careful, read-only lookup |

Results come back as CSV (a plain table you can paste into a spreadsheet), followed by lines on where the numbers
came from.

## Data

Infectious disease only, for now. The five starter datasets are in [`catalog/`](catalog/):

| Dataset | Publisher | What it covers |
|---|---|---|
| `cdc_nndss_weekly` | CDC | Notifiable disease cases, weekly, by state and region, 2022 on |
| `cdc_nhsn_ar_hai` | CDC (NHSN) | Percent resistant among pathogens from hospital-reported infections (CLABSI, CAUTI, SSI), nation, region and state, 2011 on |
| `cdc_respnet` | CDC (RESP-NET) | Hospitalisation rates for COVID-19, flu and RSV, nation and state, 2016 on |
| `cdc_haicviz_cdi` | CDC (Emerging Infections Program) | C. difficile infection counts and rates in the program's surveillance areas (not by state), 2011–2022 |
| `who_gho_tb_incidence` | WHO | Tuberculosis incidence per 100,000, by country, 2000 on |

Every dataset's description (its contract) is still a draft awaiting review; `list_caveats` says so with each
answer. WHO data is for non-commercial use, and that is shown with every WHO result.

## Using it with GBD, PubMed and others

Each tool has its own lane: GBD for modelled burden, this tool for reported and local numbers, PubMed for papers.
[The setup guide](docs/setup.md) covers adding the IHME GBD and PubMed connectors too.

Put these rules in your Claude Project's instructions, or run the `set_up_my_project` helper to write them for you:

```text
- Use the IHME GBD connector for modelled burden, health-data-registry for observed and local data and
  datasets, and PubMed for papers. Never fill a number from memory or the news.
- Every number cites its tool output: "GBD 2023 (IHME)", or the source web address, fetch date and dataset
  version that health-data-registry returns.
- Report GBD's 95% uncertainty intervals. Observed counts have none; say so.
- Before interpreting any difference between GBD and observed data, establish how they differ in case
  definition, ascertainment, geography and uncertainty model; GBD's interval is not a test of observed data.
- Flag provisional, suppressed or partial-geography rows.
- Don't compare age-standardised rates across different standard populations.
- Population-level patterns are hypotheses, not causes. No patient information in any chat.
```

## How answers stay correct

- **Reviewed descriptions.** Each dataset has a contract: a file that says what one row is, the units, the codes,
  which values mean "missing" and which rows are totals. A fact that changes a number comes only from the
  publisher's own documentation, an exact fact about the file's structure, or a maintainer's reviewed note.
  Anything else stays an open question, and the tool says so.
- **Rules for adding up.** Counts are added with total rows left out. Rates and estimates are never added.
  Published uncertainty ranges are kept, never dropped.
- **Blind testing.** For each dataset, someone who has never seen the tool writes 8–10 clinician questions and
  works out each answer twice. Those questions are sealed and run once per release, on datasets from agencies the
  tool was never tuned on.

**The release bar,** with Claude as the client:

- at most **2%** of answered questions wrong, and
- at least **80%** of answerable questions right.

Before each release, two or three clinicians with no technical background also try it from this page, unaided.
Results, when published, live in [`eval/`](eval/).

## Contributing

To suggest a dataset or report a wrong number, open an issue. To add a dataset yourself, see
[CONTRIBUTING.md](CONTRIBUTING.md) and the [developer docs](docs/dev/README.md). A dataset's contract merges only
after a maintainer reviews it.

## Security, licence and citation

- **Security:** report vulnerabilities privately; see [SECURITY.md](SECURITY.md).
- **Licence:** the code is [Apache-2.0](LICENSE). Each dataset keeps its publisher's terms, shown with every
  result. Data is never stored in this repository.
- **Citation:** cite this software with [CITATION.cff](CITATION.cff). Cite the data with the citation each result
  gives.

## For developers

```bash
git clone https://github.com/jtouley/health-data-registry.git
cd health-data-registry
make install   # uv sync
make check     # lint, type check, tests, mutation tests on changed modules
```

Commands, architecture, tests and how to add a dataset: [docs/dev/README.md](docs/dev/README.md). Design
decisions: [docs/adr/](docs/adr/README.md).
