# health-data-registry

Ask Claude about observed public health data from CDC, WHO and other publishers, in plain English, next to the
IHME GBD connector.

[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/jtouley/health-data-registry/badge)](https://scorecard.dev/viewer/?uri=github.com/jtouley/health-data-registry)
<!-- Swap for the real badge once the project has a Best Practices id: https://www.bestpractices.dev/projects/<id>/badge -->
[![OpenSSF Best Practices](https://img.shields.io/badge/OpenSSF_Best_Practices-applying-lightgrey)](https://www.bestpractices.dev/en)
[![CI](https://github.com/jtouley/health-data-registry/actions/workflows/ci.yml/badge.svg)](https://github.com/jtouley/health-data-registry/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/health-data-registry)](https://pypi.org/project/health-data-registry/)

A free, open-source tool for clinician-researchers, starting with infectious disease fellows. You ask Claude a
question in plain words. Claude uses this tool to find the public data that fits and gives you a table: every
number with its source, the date it was fetched, how it was read and what the data can't tell you. Every number
comes straight from the publisher's data. This tool doesn't model, adjust or fill in any number; WHO's
estimates come back exactly as WHO published them, with their uncertainty range.

It works next to the IHME GBD connector (modelled burden of disease) and the PubMed connector (papers): GBD tells
you what is estimated, this tool tells you what was reported.

**Status:** pre-alpha. Infectious disease only, five starter datasets, and every dataset's contract is still a
draft awaiting review. "health-data-registry" is a working name.

**Contents:** [What you can ask](#what-you-can-ask) · [How it works](#how-it-works) · [Quick start](#quick-start) ·
[Example](#example) · [Tools](#tools) · [Data](#data) · [With GBD and PubMed](#using-it-with-gbd-and-pubmed) ·
[Questions](#questions) · [Limitations](#limitations) · [How to cite](#how-to-cite) · [Terms](#terms-and-privacy) ·
[How answers stay correct](#how-answers-stay-correct) · [Contributing](#contributing) ·
[For developers](#for-developers)

## What you can ask

- **Reported cases** of notifiable conditions by US state and year: C. auris (clinical and screening, kept
  apart), legionellosis, syphilis (primary and secondary, and congenital) and tuberculosis.
- **Antimicrobial resistance** in hospital-reported infections: percent of isolates resistant for MRSA and
  carbapenem-resistant Klebsiella, E. coli, Enterobacter, Acinetobacter and Pseudomonas, nationally, by region and
  by state, with 95% confidence intervals.
- **Hospitalisation rates** for laboratory-confirmed COVID-19, influenza and RSV, by week, at RESP-NET sites.
- **C. difficile infection** counts and rates in CDC's Emerging Infections Program sites.
- **Tuberculosis incidence** by country, as WHO estimates it, with its uncertainty range.
- **Which data could answer your question,** and which can't, before you start a project.
- **Where every number came from:** the exact files, when they were fetched, and how to cite them.

Never put patient information in the chat. This tool reads only public, population-level data, and you can't
upload anything to it.

## How it works

Three layers, each useful on its own:

1. **A crawler and a data lake.** A scheduled job fetches each source from its publisher, checks it against the
   source's contract, and keeps every original file, fingerprinted, so any number can be traced back.
2. **An open health data hub on Hugging Face.** Like Kaggle, but for public health data: anyone can browse, preview,
   download or query each dataset, with a plain-language card saying what one row is, the case definition, the
   caveats, the licence and how to cite it. Datasets are fully hosted on the hub wherever the licence allows.
3. **This MCP server, with analysis tools.** It is what Claude talks to. It does the heavy lifting: it reads the
   hub's tables, applies each source's rules, and returns a sourced table. You never download or crawl anything
   yourself.

Some sources' terms don't allow anyone to republish their data, for example WHO's Global Health Observatory. These
are **listed, not hosted**: the hub has a page for them, with where the data lives, its terms and its citation, but
no data files. The server fetches them from the publisher when you ask, and shows their terms with every result.

Two open standards keep this honest:

- Every fact that changes a number (what one row is, which code means "missing", which rows are totals, what can
  be added up) comes from the source's **data contract**, written in the
  [Open Data Contract Standard (ODCS)](https://github.com/bitol-io/open-data-contract-standard).
- The hub as a whole is described by a **data product file** in the
  [Open Data Product Standard (ODPS)](https://github.com/bitol-io/open-data-product-standard): which datasets it
  offers, under which licences, and who maintains it.

Coming: the hub, its ODPS file and the hosted server are being built now. Today the server runs on your own
computer from data it loads once (see [the setup guide](docs/setup.md#one-time-install-on-your-computer-for-now)).

## Quick start

A **connector** is an add-on that lets Claude use an outside tool. You add it once. No install, no account and no
password are needed for this one.

> [!IMPORTANT]
> Coming: the web address in step 1 is not published yet. Until it is, follow the
> [one-time install](docs/setup.md#one-time-install-on-your-computer-for-now) for Claude Desktop (about 15 minutes),
> or let Claude walk you through it with the [Walk me through an install](docs/helpers.md#walk-me-through-an-install)
> helper. Then continue from step 2.

1. **Add the connector.** In Claude, open Settings → Connectors → **Add custom connector**, paste the tool's web
   address and click Add. [The setup guide](docs/setup.md) has every click, including Claude Code and
   Team or Enterprise plans.
   You should see health-data-registry in your list of connectors.
   <!-- SCREENSHOT: Claude, Settings → Connectors, with health-data-registry listed -->
2. **Start a new chat.**
   A connector only appears in chats started after you add it.
   <!-- SCREENSHOT: the new-chat button -->
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

Treat it like a conversation with a careful colleague. If an answer isn't what you wanted, ask a follow-up: narrow
it, broaden it, ask for the caveats, or ask where a number came from.

## Example

<!-- EXAMPLE: from a real run on 24 November 2026 against CDC's NNDSS weekly tables, fetched that day. -->

You ask: *"How many clinical C. auris cases did Michigan and Ohio report each year from 2022 to 2025?"*

Claude calls `get_series` and gets back this table (trimmed to the columns that matter here):

| Place | Year | Cases | Status |
|---|---|---|---|
| Michigan | 2022 | 33 | final |
| Michigan | 2023 | 110 | final |
| Michigan | 2024 | 184 | final |
| Michigan | 2025 | 178 | provisional (as of MMWR week 53) |
| Ohio | 2022 | 83 | final |
| Ohio | 2023 | 151 | final |
| Ohio | 2024 | 322 | final |
| Ohio | 2025 | 404 | provisional (as of MMWR week 53) |

Every row also carries its source (`cdc_nndss_weekly`, [data.cdc.gov/d/x9gk-5huc](https://data.cdc.gov/d/x9gk-5huc)),
when it was fetched, and a plain note on how the number was read:

- **Final** means CDC's next yearly table restated the whole year, so late reports are included.
- **Provisional** means the year is still open to late reports, so the number can still go up.
- Counts are read from CDC's running year-to-date column. Adding up the weekly column would under-count badly,
  because late reports land in the running total, not in the week: for Michigan in 2023 that gives 62, not 110.

The same answer lists what the data can't tell you: counts are by state of residence; clinical and screening
cases are separate series; this source's contract is still a draft no reviewer has checked.

Good follow-ups: "Line these up with GBD names and run the GBD call you suggest" (`get_aligned_with_gbd`), "What
are the caveats?" (`list_caveats`), and "Give me the citation" (`get_provenance`).

## Tools

You don't call these yourself; Claude picks them. All of them only read data. [docs/tools.md](docs/tools.md) has
each tool's parameters, what comes back, an example prompt and what it says when it can't answer.

| Tool | What it gives you |
|---|---|
| `list_sources` | What data is there: publisher, years, places, how fresh it is, its licence and whether it has been reviewed |
| `search_datasets` | Datasets that fit a topic, ranked, with one line on why each fits |
| `describe_dataset` | One dataset explained in clinical words: what one row is, case definitions, counts vs rates, caveats, how to cite it |
| `suggest_datasets` | For a research question: which datasets can answer it, which can't, and why |
| `get_series` | Reported numbers over time for a condition and places, each row with its source, fetch date and case definition |
| `get_aligned_with_gbd` | The same numbers labelled with GBD's place and cause names, plus the exact GBD connector call to make next |
| `get_rate` | A crude rate per 100,000 from case counts and Census population, marked as computed here and never age-standardised |
| `get_provenance` | Where the numbers came from: web addresses, fetch dates and a fingerprint of each downloaded file |
| `list_caveats` | What the data can't tell you: place traps, hidden small numbers, provisional years, unreviewed contracts |
| `find_prior_work` | Conference abstracts, case reports and health department reports (coming; today it says so rather than guess) |
| `query_sql` | A last resort for questions the others don't cover; it refuses anything but a careful, read-only lookup |

Coming: analysis tools that return a number with its 95% interval and method: `compare_rates`, `get_trend`,
`age_adjust` and `check_signal`, and an exact interval on `get_rate`. Census population for `get_rate` is not
loaded yet either.

Results come back as CSV (a plain table you can paste into a spreadsheet), followed by lines on where the numbers
came from.

## Data

Infectious disease only, for now. The five starter datasets, each with an entry in [`catalog/`](catalog/):

| Dataset | Publisher | What it covers | On the hub (coming) |
|---|---|---|---|
| `cdc_nndss_weekly` | CDC | Notifiable disease cases, weekly, by state and region, 2022 on | Hosted (US federal, public domain) |
| `cdc_nhsn_ar_hai` | CDC (NHSN) | Percent resistant among pathogens from hospital-reported infections (CLABSI, CAUTI, SSI), nation, region and state, 2011 on | Hosted (US federal, public domain) |
| `cdc_respnet` | CDC (RESP-NET) | Hospitalisation rates for COVID-19, flu and RSV, at surveillance sites, 2016 on | Hosted (US federal, public domain) |
| `cdc_haicviz_cdi` | CDC (Emerging Infections Program) | C. difficile infection counts and rates in the program's surveillance areas (not by state), 2011–2022 | Hosted (US federal, public domain) |
| `who_gho_tb_incidence` | WHO | Tuberculosis incidence per 100,000, by country, 2000 on | Listed, not hosted (non-commercial terms) |

[Data conventions](docs/data-conventions.md) explains how to read them: counts, rates, percents and estimates
(never mixed); final and provisional years; why an empty cell is never a zero; uncertainty; places (states, FIPS
codes, GBD names, WHO country codes); MMWR weeks; age standardisation; case definitions; and the rules for setting
them beside GBD.

## Using it with GBD and PubMed

Each tool has its own lane: GBD for modelled burden, this tool for reported and local numbers, PubMed for papers.
This tool never calls GBD, never copies it, and never says which number is "right". It labels observed numbers
with GBD's place and cause names and tells Claude the exact GBD call to make next, so you can set them side by
side. [The setup guide](docs/setup.md) covers adding the IHME GBD and PubMed connectors too.

Put these rules in your Claude Project's instructions, or run the `set_up_my_project` helper to write them for you:

```text
- Use the IHME GBD connector for modelled burden, health-data-registry for observed and local data and
  datasets, and PubMed for papers. Never fill a number from memory or the news.
- Every number cites its tool output: "GBD 2023 (IHME)", or the source web address, fetch date and dataset
  version that health-data-registry returns.
- Report GBD's 95% uncertainty intervals. Observed counts have none; say so.
- Don't call a gap between GBD and observed data real unless it falls outside GBD's 95% interval.
- Flag provisional, suppressed or partial-geography rows.
- Don't compare age-standardised rates across different standard populations.
- Population-level patterns are hypotheses, not causes. No patient information in any chat.
```

## Questions

**How do I know a number came from the data and not from Claude's memory?**
Every number from this tool comes with its `source_id`, the date it was fetched and the publisher's web address.
Look for the tool-call line in Claude's answer; click it to see the raw table and notes. If a number has no
source next to it, ask Claude where it came from. For anything you plan to cite, check that the tool was used.

**What if a number looks wrong?**
Ask Claude to check its work, show the provenance (`get_provenance`) or list the caveats. If the publisher's own
page disagrees with what you see here, trust the publisher, and please
[open an issue](https://github.com/jtouley/health-data-registry/issues) so we can fix the contract.

**Should I use this instead of CDC WONDER, the NNDSS tables or WHO's site?**
No. It is a fast, conversational way to find and scope data and to draft a first table. For the definitive
figures, and for anything this tool doesn't cover, go to the publisher; every answer links to it.

**Can anyone see my questions?**
The server only receives what Claude sends to its tools, and it sends no telemetry anywhere. The hosted server is
planned to keep no logs of questions. Your chat itself is governed by your Claude account's settings.

**Why does Claude ask permission before using the tool?**
That is Claude's default for any connector. You can allow it for a chat or always in the connector's settings;
every tool here only reads public data. See [the setup guide](docs/setup.md#why-claude-asks-before-using-a-tool).

**What is an MCP server?**
The Model Context Protocol (MCP) is a standard way to connect Claude to an outside tool. "MCP server" is the
technical name for the program behind a connector.

## Limitations

- **Infectious disease only,** five starter datasets. Counties, HIV, hepatitis, STI surveillance reports and
  others are on the roadmap, not loaded.
- **Every contract is still a draft** that no maintainer has reviewed. Each answer says so, and `list_caveats`
  lists the facts not yet confirmed with the publisher.
- **No causes.** Population-level data shows what and where. Your hospital's own data, kept in approved systems,
  answers why.
- **Recent years are provisional,** and WHO re-estimates its whole series each year.
- **No sex or age breakdowns** in the current series, and no age-standardised rates computed here.
- **Not built yet:** the hosted server, the hub, the analysis tools and the prior-work index (all marked "coming"
  above).

## How to cite

- **The data:** cite each publisher, using the citation each result gives (also in `describe_dataset` and
  `get_provenance`). For example, NNDSS: *Centers for Disease Control and Prevention. National Notifiable Diseases
  Surveillance System (NNDSS). NNDSS Weekly Data. https://data.cdc.gov/d/x9gk-5huc (accessed &lt;fetch date&gt;).*
- **GBD values:** cite GBD 2023 as IHME asks.
- **This software:** cite it with [CITATION.cff](CITATION.cff), with the version you used.
- Check your target journal's rules on AI tools before you submit.

## Terms and privacy

- **Code:** [Apache-2.0](LICENSE).
- **Data:** each dataset keeps its publisher's terms, shown with every result. CDC data are US federal works in the
  public domain. WHO data are recorded as CC BY-NC-SA 3.0 IGO (non-commercial, share-alike; a maintainer is still
  confirming this), so they are fetched from WHO at run time and never republished. No data is stored in this
  repository.
- **Privacy:** no patient data, ever. No upload or local-file tool exists. Requests go only to catalogued publisher
  sites over HTTPS, and no telemetry is sent.
- **Security:** report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## How answers stay correct

- **Reviewed contracts.** A fact that changes a number comes only from the publisher's own documentation, an exact
  fact about the file's structure, or a maintainer's reviewed note. Anything else stays an open question, and the
  tool says so.
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

To suggest a dataset or report a wrong number, [open an issue](https://github.com/jtouley/health-data-registry/issues).
To add a dataset yourself, see [CONTRIBUTING.md](CONTRIBUTING.md) and the [developer docs](docs/dev/README.md): one
pull request with a catalog entry, an ODCS contract, a semantic model and sealed blind questions. A dataset's
contract merges only after a maintainer reviews it against the publisher's documentation.

## For developers

You need [uv](https://docs.astral.sh/uv/) (it installs a supported Python, 3.12 or 3.13, if needed) and Git.

```bash
git clone https://github.com/jtouley/health-data-registry.git
cd health-data-registry
make install   # uv sync
make check     # lint, type check, tests, mutation tests on changed modules
```

Commands, architecture, tests and how to add a dataset: [docs/dev/README.md](docs/dev/README.md). Design
decisions: [docs/adr/](docs/adr/README.md).
