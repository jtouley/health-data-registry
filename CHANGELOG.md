# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- `query_sql` refuses queries that could build values too large to answer safely (#74): `lpad`/`rpad` lengths
  over 1,000, `string_agg`/`list`/`array_agg` and the other ways to gather a whole column into one value
  (`ARRAY(SELECT ...)`, `max`/`min`/`arg_max`/`arg_min` with a count), `levenshtein`, recursive `WITH`,
  non-literal `replace` replacements and `strftime` formats, and any one value that `||`, `concat`, `replace`,
  splitting, lists or structs could grow more than 256-fold. Growth is counted per value, through CTEs and
  subqueries, so separate columns and conditions don't add up against each other. Each refusal says what to ask
  instead, and `query_sql` passes that next step on.
- `query_sql` refuses any query that reaches two sources with no declared join in any part of it, including each
  branch of a `UNION`, `INTERSECT` or `EXCEPT`, every subquery and every CTE (#74).
- The lake is never read from the working folder: it is `$HDR_LAKE` (a full path) or a per-user data folder.
  Each time the server connects, it checks each lake snapshot's dataset id, snapshot id, contract version and
  review status, that its folder and files belong to the current user and can't be changed by others (on Unix),
  and that `data.duckdb` is a regular file with the sha256 in its manifest. A snapshot that fails, or can't be
  read, is refused in plain words; the others still answer. DuckDB then reads the file by its path, so a change
  made after that check is read until the next reconnect. The manifest sits beside the data, so these hashes
  catch damage, not tampering. A file is hashed once per server process and again only when its size, times or
  inode change. `export-public` checks `data.duckdb` and `data.parquet` the same way before publishing, and
  compares the copy it publishes. Cached hub files are checked again each time the server reconnects (#74).
  Snapshots built before this change have no file fingerprints and must be rebuilt with `refresh`.

### Added

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
