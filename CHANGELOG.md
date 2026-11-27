# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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

- New source, draft: `nchs_resp_deaths_pct`, NCHS's provisional weekly percent of all deaths among US residents
  involving COVID-19, influenza, RSV or the three combined, by age group, sex and race and Hispanic origin
  (data.cdc.gov 53g5-jf7x). Read as published and never added; a suppressed count (1 to 9) is no value, never zero,
  and keeps NCHS's note. It answers only a question about deaths: a plain "COVID-19" question doesn't get it.
- Semantic series can list `asked_with` words: a series that names a disease but isn't a measure of it (a percent
  of deaths, vaccination coverage) answers only a question that uses one of them.
- The first marts: `fct_cdc_nssp_lab_positivity` and `fct_nchs_resp_deaths_pct`, one fact per source with every
  row kept, the publisher's own notes as published, and a `value_status` that says why a value is empty.
- New source, draft (contract not yet reviewed): `cdc_nssp_lab_positivity`, CDC's weekly NSSP commercial
  laboratory NAAT tests, positives and percent positive for COVID-19, influenza and RSV by state (data.cdc.gov
  jqxy-cyu2), on the existing Socrata adapter. Read as published and never added; a count or percent CDC
  suppressed (under 10 tests or 10 positives) is no value, never zero. A national or county question is told the
  table is by state only.
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
