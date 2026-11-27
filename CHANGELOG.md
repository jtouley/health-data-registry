# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Release blockers from #173, where the served path returned wrong numbers:
  - NNDSS's `-` (no reported cases) is read as not reported (the maintainer's declaration of 25 Nov 2026). It
    comes back empty with its flag, never as 0 and never as a rate: `get_rate` gives no rate, `compare_rates`
    refuses, `get_trend` leaves the year out and names the flag, and `check_signal` refuses any 4-week window
    with a `-` week, which is common for rare conditions. The measles trial row "Imported 2024" is now empty
    with flag `-`, not 0.
  - Weekly counts, or year-to-date counts, are never added across weeks into a year (late reports reach only
    the year-to-date count), in the metric compiler and in `query_sql`.
  - A `query_sql` sum across places leaves the publisher's total rows out of that sum and says so; a sum that
    meets an empty value stops, and so does one that filters out the empty values first. These rules cover
    sums only.
  - *C. auris* screening (colonisation) gets no rate per 100,000 people, from the NNDSS contract's new
    `notIncidence` declaration (the maintainer's declaration); its counts are still returned.
  - `refresh` exits non-zero when a development sample (`--max-rows`) fails a blocking data check, and every
    answer from that sample says which check failed.
- A summed count never adds up labels that overlap or count different things (hard rule 2). Semantic-model
  dimensions say `non_additive: true` or `false`, and each `true` says why in plain words
  (`non_additive_reason`), which the refusal and `describe_dataset` show. A metric or series that neither picks
  one value of such a dimension nor shows each value on its own row is declined in plain words with a next step.
  `query_sql` applies the same rule to a typed `sum()`: each such column is filtered to one value in WHERE or
  grouped by, and a column a model leaves undeclared counts as such. Before, a weekly NNDSS count asked for
  without a condition added every label together, including Confirmed, Probable and Total and animal rabies.
  The tools always picked one label, so their answers don't change.
- `validate` refuses a summed metric that keeps the publisher's total rows while adding up a place or group
  declared additive (NNDSS areas add up only once TOTAL, US RESIDENTS and the divisions are excluded).
- Every percent, estimate or rate names the publisher's uncertainty bounds, in the same units, or says
  `uncertainty: none` and why in plain words; a bound measure no metric returns is a load error. A row whose
  interval is empty says so.
- The CDI total case count is read as the one published Total cell (grouping 'All cases'), not a sum. An empty
  published cell, there or in any other figure read as published, comes back as "suppressed or not published",
  never a blank.
- Semantic-model versions: CDI, NNDSS and RESP-NET 0.2.0; Detroit annual and ED-visit models 0.1.1.
- NHSN contract: two quality rules check that the confidence interval is filled wherever the percent resistant
  is, and only there, as the publisher's column descriptions state.
- The Trusted gate's allowlist can take entries again. A test required it to stay empty, and the gate forbids
  changing that test in the pull request that adds an entry, so no allowlist pull request could pass. The test
  now checks that every entry is well-formed instead: it parses, its path is a plain path under an existing
  top-level directory of this repository (a file a later pull request adds counts), and it has a comment above it
  saying why.
### Changed

- The README is rewritten for clinicians: five setup steps that each say what you'll see (the Start here
  helper is the check step), a C. auris in Michigan example checked against CDC with every year provisional and
  CDC's later count beside each, a short GBD and PubMed section, and light and dark images from
  `tools/readme_svg/gen.py`. The setup guide no longer says the hosted preview can't answer CDC questions (the hub
  has published releases since `release-2026-11-24`).
- The Something's wrong helper, the setup guide and `docs/tools.md` now tell "the hub could not be reached" (try
  again) apart from "the hub has no published release yet" (retrying won't help).
- `tools/` is linted and type-checked with the rest of the code, and a test fails if the committed README images
  differ from what the generator draws.

### Added

- New source, draft (contract not yet reviewed): `cdc_svi_county`, the CDC/ATSDR Social Vulnerability Index 2022
  for every US county, the first covariate under the widened hard rule 11 (ADR 0019). Read from svi.cdc.gov as
  config on the `https_file` type, with no new adapter code. It has no conditions and no series, so it never
  answers a question on its own and its ACS population is never a denominator; `suggest_datasets` offers it only
  beside an infectious-disease source that fits, and the registry refuses a covariate with a series, conditions or
  a disease area. Ranks are read as published, -999 becomes no value, and the `dim_cdc_svi_county` mart keys
  them by county FIPS with the edition and ranking scope on every row.
- New source, draft (contract not yet reviewed): `cdc_haicviz_isa`, CDC's Emerging Infections Program case and
  death rates for invasive Staphylococcus aureus since 2005 (HAICViz iSA, data.cdc.gov ssz5-s49e), on the existing
  Socrata adapter. Every rate is for the EIP sites combined, never called a state figure, and nothing is added;
  whether the figures are MRSA only is an open question, so none is called an MRSA rate.
- New source, draft (contract not yet reviewed): `cdc_haicviz_mugsi`, CDC's Emerging Infections Program case and
  death rates for carbapenem-resistant Enterobacterales (CRE) and Acinetobacter baumannii (CRAB) since 2012
  (HAICViz MuGSI, data.cdc.gov v4tm-h8pe), on the existing Socrata adapter. Every rate is for the EIP sites
  combined, never called a state figure, and nothing is added.
- The crawler factory's core (ADR 0016, step 1 and the types step 2 names). Every source is read by one generic
  adapter type chosen in its catalog entry (`type:`; the older `portal:` is read for one more release and `validate`
  warns about it), configured by that type's strict block, plus named extensions it opts in to. New types:
  `https_file` (csv, xlsx, or one member of a zip; xlsx values as stored with their number format, never the displayed
  text), `html_table` (one table by selector, or a resumable listing of item pages) and `pdf_table` (reports linked
  from an index page, plus facts a report prints once, read by a `parse` extension). `socrata`, `odata` (WHO GHO),
  `https_csv`, `tableau` and `doi` read exactly as before. `pdf_table` reads a table wide (one record per row) or long
  (one record per printed cell, for column headers that change from report to report), and can check a table against
  its own printed totals, recording each row's `failed_checks` as metadata and refusing a document with more failing
  rows than the catalog allows. Every row a document type reads (`pdf_table`, `https_file`, `html_table`) carries its
  document's URL with any key stripped, sha256 and fetch time. A listing crawl checks that each item is of the year
  asked for and carries the number the listing gives it, combines several `parse` extensions on one item page in their
  declared order, and reports a listing that runs out before its own total as incomplete, to resume next run. One
  fetch core: a source reaches only its own hosts and declared paths (catalogued patterns matched whole, on the
  linear-time RE2 engine), config can only slow the pace, robots.txt is reread after 24 hours and its Request-rate
  honoured, keys never reach provenance, and a source stops being crawled only after 8 weeks of continuous failure.
  The row-level guard (hard rule 4) runs on every page of every type, with no way to switch it off. A snapshot's
  identity now includes how its records were read (the type, its config and extensions), so a config-only change
  builds a new snapshot; existing snapshots are rebuilt once on their next refresh.
- From the review of the crawler factory (#148): the new types keep every field a document gives in the raw layer,
  not only the contract's, so a year the codebook doesn't list stops the load instead of being dropped; a
  contract for a type that adds per-row metadata must declare it (and `failed_checks` when it reconciles), and a
  staging step's added key must be in the contract's primary key. `failed_checks` is a JSON array of sentences,
  on the cell it concerns in the long layout. A `pdf_table` snapshot keeps its index page as a fetch. A listing
  crawl's checkpoint is tied to how its items were read, a finished year rechecks its listing's total once a run,
  a moving total restarts the listing walk instead of stopping the crawl for good, a gone item is named and asked
  for again without walking the listing, and one year that can't finish no longer holds the others back. A fetch
  sends exactly the URL the scope check approved, and every redirect stays on the source's own hosts. Valid HTML
  that leaves out optional end tags is read as a browser reads it.
- New source, draft (contract not yet reviewed): `cdc_nrevss_positivity`, CDC's weekly national percent of laboratory tests
  positive for COVID-19, influenza and RSV (NREVSS, data.cdc.gov seuz-s2cv), on the existing Socrata adapter. One
  figure for the whole country each week; a question about a state or region is told so.
- SchoolVaxView kindergarten coverage and exemptions (`cdc_schoolvaxview_kindergarten`, CDC data.cdc.gov
  ijqb-a7ye): the percent of kindergartners vaccinated, or with an exemption, by vaccine and dose or exemption
  type, state and school year, as each state reported it, read as published and never added up. Draft contract.
  CDC publishes no confidence intervals here. NA, NReq and the undefined NR and Nreq are no value, never zero, and
  the published text is kept beside the number.
- The Trusted gate (`.github/workflows/trusted-gate.yml`, `python -m health_data_registry.ratchet`, `make
  ratchet`): a check a pull request cannot edit, run from main's copy on `pull_request_target` with a read-only
  token, reading the pull request as data only (a required check once the `main` ruleset lists it, #138). It fails
  a change that lowers a coverage, mutation or other floor, adds a skip, pragma, type-ignore, noqa or lint
  relaxation not in `.github/ratchet-allowlist.txt`, adds a settings file beside `pyproject.toml`, a symbolic link
  or a submodule, drops tests, removes or loosens a required check, touches `eval/sealed/`, adds a way around the
  git hooks or removes a hook, loosens `ci/budgets.yml` (a new key included), or weakens a job that runs mutation
  tests (PR shards, the nightly run, the release gate's mutation check).
- A `pre-push` hook (`scripts/pre_push_gate.py`, installed by `make hooks`): a push to a pull request branch (any
  branch but `pre-pr/**`, `wip/**`, `dependabot/**` and `main`) is refused unless that exact commit already
  passed CI on a `pre-pr/` branch, and a push to `pre-pr/**` first runs ruff, mypy and the changed modules' tests.
  Create a PR branch at the green commit (`git push origin <sha>:refs/heads/<branch>`), and merge main locally
  instead of using GitHub's "Update branch".
- `serve --http`: the hosted, read-only connector over stateless Streamable HTTP (hub releases and live reads
  only), with one deadline per tool call, a result-size cap, rate limits per MCP session and overall, and no
  request content in any log; `space/` for a Hugging Face Docker Space and `docs/dev/hosting.md`.
- Long answers come in pages instead of being refused: every row-returning tool (and the matching CLI commands,
  `get-series`, `get-aligned-with-gbd`, `get-provenance`, `query-sql` and the analysis tools) takes `page` and
  `pin`; each page carries real rows, the same provenance line as every answer, and the exact call for the next
  page, and a pin keeps every page on one version of the data. `query_sql` adds `ORDER BY ALL` when a query has
  no `ORDER BY`. The hosted server refuses to start with a limit of 0, a negative number, inf or nan, or with
  `HDR_PAGE_CHARS` above `HDR_MAX_RESULT_CHARS`.
- Hosted server hardening from review: request bodies over 1 MiB are refused with 413; session ids and pins are
  signed (`HDR_SIGNING_KEY`), calls without a signed session id share one rate-limit bucket, and a refused call
  spends no token; pins carry the answer's row count and row-key hash and may name only release tags; hub
  downloads end by the call deadline and a stuck call never holds the engine past it; `serve --http` binds to
  127.0.0.1 by default with Host and Origin checks; `query_sql` adds tie-breakers to its own `ORDER BY`.
- Hosted query isolation (#64, #74 item 1): every query runs in a child process with an OS memory limit
  (`HDR_QUERY_MEMORY_MB`), a hard kill at the deadline and a capped JSON reply (`HDR_QUERY_RESULT_MB`), so a
  long `lpad` or `repeat` call is stopped and can't exhaust the server's memory. Anything that outlives its call
  poisons the engine until it ends: new calls get a plain BUSY refusal, `/healthz` answers 503 with the cause and
  since when, and after `HDR_POISONED_EXIT_S` (900 s) the server exits so it can be started afresh. Each child
  gets only the tables its query reads and none of the server's environment, and `/healthz` reports whether the
  memory limit was measured to hold. No page of an answer goes over `HDR_PAGE_CHARS`: pages are measured and
  re-cut, and a single row too long for any page, even in a one-row answer, is refused in plain words.
- Condition vocabulary: each catalog entry lists the publisher's own condition labels (`conditions:`, with
  where they come from), and `get_series`, `get_rate` and `get_trend` accept any of them through the semantic
  model's `generic_series`, with the same aggregation rules as the named series. `validate` fails when a local
  snapshot holds a label the catalog doesn't list.
- `describe_dataset` returns `dimension_values` (the values each filter takes) and `presentation_rules`;
  discovery results (`list_sources`, `search_datasets`, `suggest_datasets`, `describe_dataset`) carry each
  source's provenance (source URL, retrieved_at, raw sha256, version, licence, citation, contract status).
- Structured declines: every refusal carries `next_step`, and where they apply `did_you_mean`,
  `nearest_served` (e.g. the state for a county), `served_elsewhere` (the publisher's page),
  `outside_registry` (a GBD cause by name only) or, for `filter_matched_nothing`, the values that exist.
- The server's instructions are an intent-to-tool routing block.
- Repository scaffolding under the Apache License 2.0, with a NOTICE for material adapted from the author's
  earlier MIT-licensed work.
- SECURITY.md (private reporting through GitHub advisories, 14-day first response), CONTRIBUTING.md,
  CODE_OF_CONDUCT.md (Contributor Covenant 2.1), CITATION.cff, CODEOWNERS, pull request and issue templates
  (bug, dataset request, wrong number).
- `licenses/README.md`: what may be mirrored for each class of source.
- CI on every pull request and push to `main`: ruff, mypy, pytest with coverage on Python 3.12 and 3.13.
- CodeQL (Python and Actions), OpenSSF Scorecard, and Dependabot for uv and
  GitHub Actions. Every action is pinned to a commit SHA.
- Release workflow: sdist and wheel, CycloneDX SBOM, Sigstore signatures, SLSA provenance, and PyPI trusted
  publishing.
- Weekly refresh workflow that runs every source's contract check and opens an issue on failure. Publishing
  snapshots to Hugging Face stays off until the maintainer configures it.
- Makefile and pre-commit hooks.
- GitHub Actions pipelines (docs/dev/automation.md): the weekly refresh fetches each public source in its own job
  and publishes and tags `release-<date>` only when every source passes, otherwise it opens an issue; the CROI
  and DOI conference crawls and the Space deploy (release tags on main that passed CI only) wait, with a notice,
  for the code they run; a weekly drift check keeps the scheduled workflows enabled.
- `health-data-registry sitemaps snapshot` and a weekly workflow: copies of the michigan.gov and kentcountymi.gov
  sitemaps, with week-to-week diffs, in a private Hugging Face dataset; a refusal opens a "Sitemap refresh
  blocked" issue and is never worked around.
- `naming.py`: every Hugging Face repository name built from one convention (docs/dev/naming.md), and
  `assert-private`, run before every upload to a private or ops dataset. `HDR_HF_OWNER` owns the public dataset
  and the Space; `HDR_PRIVATE_HF_OWNER` the private and ops datasets.
- ADR 0013: the browser fetcher may read government hosts with an honest user agent, at least 10 s apart and with
  no evasion; a challenge page, CAPTCHA, 401, 403 or 429 stops the crawl and is recorded as blocked.
- Catalog entries carry `hosting: public` or `hosting: private` (the default). Only `hosting: public` sources
  are refreshed for and published to the open data release, and the pre-publish check refuses data files staged
  for any other source. It also refuses a snapshot with no rows, or a data file whose row count differs from
  its snapshot's manifest.
- The MCP server and its read-only tools: `list_sources`, `search_datasets`, `describe_dataset`,
  `suggest_datasets`, `get_series`, `get_aligned_with_gbd`, `get_provenance`, `list_caveats`, `find_prior_work`
  (says the index isn't built yet), `get_publication_outlook` and the guarded `query_sql`.
- Analysis tools with 95% intervals, each row labelled as computed: `get_rate`, `compare_rates`, `get_trend`,
  `age_adjust` and `check_signal`.
- Six catalogued sources: CDC NNDSS weekly, NHSN antimicrobial resistance, RESP-NET, HAICViz C. difficile, Census
  county population and WHO tuberculosis incidence; every contract is a draft.
- WHO data read live from WHO per question, held in memory only, never stored or published.
- The open data hub read path: CDC and Census answers come from the hub's dated releases, checked against their
  sha256; until the first release, a plain message says so.
- The hosted server, as a preview: https://jtouley-health-data-registry-mcp.hf.space/mcp.
- Clinician helpers as MCP prompts, Claude Code skills, a Claude plugin with its marketplace, and a Claude
  Projects kit; an npm launcher and a Claude Desktop extension (`.mcpb`).
- Provenance on every result: source address, fetch time, raw-file sha256, version, contract review status,
  licence and citation.

[Unreleased]: https://github.com/jtouley/health-data-registry/commits/main
