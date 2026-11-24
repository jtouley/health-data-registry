# health-data-registry

Ask Claude about public health data — GBD, CDC, WHO and more — in plain English.

[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/jtouley/health-data-registry/badge)](https://scorecard.dev/viewer/?uri=github.com/jtouley/health-data-registry)
<!-- Swap for the real badge once the project has a Best Practices id: https://www.bestpractices.dev/projects/<id>/badge -->
[![OpenSSF Best Practices](https://img.shields.io/badge/OpenSSF_Best_Practices-applying-lightgrey)](https://www.bestpractices.dev/en)
[![CI](https://github.com/jtouley/health-data-registry/actions/workflows/ci.yml/badge.svg)](https://github.com/jtouley/health-data-registry/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/health-data-registry)](https://pypi.org/project/health-data-registry/)

A read-only [MCP](https://modelcontextprotocol.io) server for clinician-researchers. You ask a question; it finds
the public datasets that fit, runs a checked query and returns a table with its source, version and method. It
works next to the [IHME GBD connector](https://claude.com/connectors/ihme-gbd): GBD gives modelled burden, this
server gives the observed and local numbers GBD doesn't have.

**Status:** pre-alpha. Tools, datasets and names may change. Not yet on PyPI.

**Contents:** [What it does](#what-it-does-and-doesnt-do) · [Quick start](#quick-start) · [Example](#example) ·
[Datasets](#datasets) · [How answers stay correct](#how-answers-stay-correct) ·
[With the GBD connector](#using-it-with-the-ihme-gbd-connector) · [Contributing a dataset](#contributing-a-dataset) ·
[Security, licence, citation](#security-licence-and-citation)

## What it does and doesn't do

It does:

- **Finds the datasets that fit a question.** Ask "which public data could support a project on hepatitis B
  screening?" and get 3–5 datasets ranked by fit, each with its grain, years, geography and caveats.
- **Answers with a sourced table.** Every result carries the source URL, retrieval date, dataset version, the SQL
  that ran and a one-line method. Published uncertainty ranges are kept, never dropped.
- **Knows each dataset's quirks for you.** Missing-value codes, total rows, and counts versus rates live in
  reviewed contracts, so you never pick a file, a column or a join.

It doesn't:

- **Touch patient data.** It reads only catalogued public, population-level sources. There is no upload or
  local-file tool. Never paste patient information into the chat.
- **Call, copy or second-guess GBD.** GBD comes only through the IHME connector. This server returns observed data
  keyed to GBD names; it never says whether GBD or a local source is "right".
- **Guess.** If a contract can't say what a number means, the tool declines and says why. It never fills a number
  from memory, the news or a secondary source.

## Quick start

You need [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 if you don't have it). The server runs on your
machine over stdio; nothing leaves it except reads from the publishers.

1. **Add the connector.**
   - **Claude Code:**

     ```bash
     claude mcp add health-data-registry -- \
       uvx --from git+https://github.com/jtouley/health-data-registry health-data-registry serve
     ```

   - **Claude Desktop:** add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), then
     restart Claude Desktop:

     ```json
     {
       "mcpServers": {
         "health-data-registry": {
           "command": "uvx",
           "args": ["--from", "git+https://github.com/jtouley/health-data-registry",
                    "health-data-registry", "serve"]
         }
       }
     }
     ```

   - **claude.ai custom connector:** needs a hosted URL, which comes later
     ([ADR 0011](docs/adr/0011-local-stdio-first.md)). Use Claude Desktop or Claude Code for now.
2. **Start a new chat.** Connectors added mid-chat don't load.
3. **Ask one question,** for example: "Which public datasets could support a project on hepatitis B screening?"

Once it is on PyPI, the command shortens to `uvx health-data-registry serve`.

## Example

<!-- EXAMPLE: filled from a real test run by the lead -->

## Datasets

Each dataset has a reviewed catalog entry in [`catalog/`](catalog/): agency, years, geography, grain, licence,
citation and known caveats. `find_datasets` browses them by research area:

| Research area | First sources to cover |
|---|---|
| Infectious disease and AMR | CDC NNDSS, WONDER, AtlasPlus, NHSN and the AR & Patient Safety Portal; WHO GHO |
| Cardiometabolic | CDC PLACES and BRFSS, NHANES, CMS chronic conditions |
| Maternal and child health | CDC WONDER natality, PRAMS, WHO GHO, UNICEF |
| Cancer | CDC USCS, SEER summary tables |
| Behavioural health and substance use | CDC drug overdose tables, SAMHSA NSDUH |
| Health services and quality | CMS Hospital Compare and Medicare data, AHRQ HCUP summaries |
| Context and denominators | Census population, CDC/ATSDR SVI, NOAA weather |
| Global comparisons | WHO GHO, OECD Health, World Bank health indicators, PAHO |

Infectious disease comes first. The first adapters read CDC's Socrata portals and the WHO Global Health
Observatory. The table is the plan; [`catalog/`](catalog/) is what ships today.

GBD itself is never mirrored. Every catalog entry carries its GBD cause and location crosswalk instead.

## How answers stay correct

- **Contracts.** Each dataset has an [ODCS v3.2.0](https://bitol-io.github.io/open-data-contract-standard/latest/)
  contract in [`contracts/`](contracts/): what one row is, units, codes, missing values and total rows. A fact that
  changes a number comes only from the publisher's codebook, an exact structural fact or a maintainer's reviewed
  declaration. Anything else stays an open question in the contract.
- **Semantic models.** Each dataset has a model in [`semantic/`](semantic/) that defines what can be asked and how
  to aggregate it: counts in frequency tables are summed with total rows left out; rates and estimates are never
  summed. `query_metric` compiles these metrics to SQL, so the model doesn't write it.
- **Blind evaluation.** For each dataset, an author who never sees the connector writes 8–10 clinician questions,
  computes each answer twice, and seals the set with a hash. Datasets are split by agency; the held-out agencies
  run once per release and are never used to tune anything.

**The release bar,** measured on held-out agencies with Claude as the client:

- at most **2%** of answered questions wrong, and
- at least **80%** of answerable questions right.

Results, when published, live in [`eval/`](eval/). Design decisions are in [`docs/adr/`](docs/adr/README.md); the
data flow is in [`docs/architecture.md`](docs/architecture.md).

## Using it with the IHME GBD connector

1. In a Claude Project, add the [IHME GBD connector](https://claude.com/connectors/ihme-gbd) and this server.
2. Start a new chat.
3. Paste this into the Project instructions:

```text
- Use the IHME GBD connector for modelled burden and health-data-registry for observed and local data.
  Never fill a number from memory or the news.
- Every number cites its tool output: "GBD 2023 (IHME)", or the source URL, retrieval date and dataset
  version that health-data-registry returns.
- Report GBD's 95% uncertainty intervals. Observed counts have none; say so.
- Don't call a gap between GBD and observed data real unless it falls outside GBD's 95% interval.
- Flag provisional, suppressed or partial-geography rows.
- Don't compare age-standardised rates across different standard populations.
- Population-level patterns are hypotheses, not causes. No patient information in the chat.
```

A later tool, `get_aligned_with_gbd`, will return observed rows keyed to GBD location and cause names, with the
exact GBD call to make next.

## Contributing a dataset

A dataset is three reviewed files plus a blind question set:

1. `catalog/<id>.yaml`: agency, years, geography, shape, licence and terms, citation, source and codebook URLs.
2. `contracts/<id>.odcs.yaml`: the physical truth, in ODCS v3.2.0. Facts come from the publisher's codebook, an
   exact structural fact or a maintainer's declaration; anything else goes in as an open question.
3. `semantic/<id>.yaml`: dimensions, measures and metrics, each with its aggregation rule.
4. Blind questions, written by someone other than the contract's author, sealed under `eval/sealed/`.

Run `make check` before you open the pull request. **The review rule:** a contract or semantic model merges only
after a maintainer reviews it, even when inference or a model drafted it. Data files are never committed, except
tiny test fixtures. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security, licence and citation

- **Security:** report vulnerabilities privately through GitHub security advisories. See [SECURITY.md](SECURITY.md).
- **Licence:** code under [Apache-2.0](LICENSE). Each dataset keeps its publisher's terms, recorded in its catalog
  entry and returned with every result. WHO data is non-commercial and is fetched from WHO at run time, not
  mirrored.
- **Citation:** cite this software with [CITATION.cff](CITATION.cff). Cite the data by the citation each result
  returns.
