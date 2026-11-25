# 0016. Crawler factory: generic adapter types, per-source config, declared extensions

- **Status:** Proposed
- **Date:** 2026-11-25
- **Spec:** "Crawling"; the maintainer's direction of 25 Nov 2026
- **Supersedes in part, if accepted:** [ADR 0013](0013-browser-fetcher-on-government-hosts.md)'s stop rule for a
  refused robots.txt on a government host (section 4, open question 5)
- **Related:**
  - [ADR 0002](0002-primary-sources-only.md): primary sources only
  - [ADR 0003](0003-provenance-on-every-result.md): provenance on every result
  - [ADR 0005](0005-raw-fetches-hash-addressed.md): raw fetches kept, hash-addressed
  - [ADR 0007](0007-odcs-contracts-and-semantic-models.md): contracts and the contract facts rule
  - [ADR 0013](0013-browser-fetcher-on-government-hosts.md): browser fetcher on government hosts
  - [ADR 0014](0014-bibliographic-registries-as-primary-routes.md): bibliographic registries
  - ADR 0015: ELT, raw data stored as sent (proposed on PR #108)
  - issue [#104](https://github.com/jtouley/health-data-registry/issues/104): the ELT audit
  - issue [#65](https://github.com/jtouley/health-data-registry/issues/65): pacing and locks across machines
  - the audit in [docs/dev/crawler-audit.md](../dev/crawler-audit.md), and PRs #27 and #30

## Context

The maintainer's direction (25 Nov 2026): crawlers use a factory approach. Each data source is configuration on a
small set of generic adapter types: its catalog entry and contract, plus the few custom pieces it needs, registered
as named, declared extensions. We don't build a custom tool for every extraction.

The [crawler audit](../dev/crawler-audit.md) read every ingestion path on main and on PRs #27 and #30. What it found:

- The four adapters on main (`socrata`, `who_gho`, `https_csv`, `tableau`) and the DOI harvester already read what is
  specific to a source from its catalog entry. Their remaining per-source code is small: IDWeek's title numbering
  applied to every conference, and the Census pivot run at load time.
- The two open source PRs each add a module written for one publisher: `wayne_county.py` (#27) and `croi.py` with
  `croi_index.py` (#30). Hosts, index URLs, file-name and URL patterns, CSS classes, year ranges, the area a report
  covers, the crawl delay and dataset ids are Python constants, and each adds a `portal` value that names exactly one
  source (`pdf_report`, `croi`).
- The rules every fetch must follow live in three places: `crawl.Robots`, `sitemaps.Pace` with `BrowserRobots`, and
  #30's `polite.PoliteClient`. They already disagree: `crawl.py` overrides a refused robots.txt on a government host
  (owner decision, 25 Nov 2026) and `sitemaps.py` stops.
- Adding a type means editing closed `if portal == ...` chains in `adapters/__init__.py`, `crawl.py`, `cli.py`,
  `registry.py` and `doi_harvest.py` (18 `portal` comparisons on main). `publisher_fields` falls through to the CSV reader for any
  portal it doesn't know.

The next sources to add need two kinds of reader the repo doesn't have: spreadsheets and zipped files over HTTPS,
and tables on HTML pages. Built the way #27 and #30 were built, each would be another single-source module.

## Decision

Every source is read by one of a small, closed set of **adapter types**. A catalog entry chooses its type and gives
that type's **config**. What a type can't express is a **named extension** from one reviewed registry, chosen by name
in config. Adapter modules never contain source-specific values or branches. Fetch rules live in **one shared core**.

### 1. Adapter types

| Type | Reads | Replaces |
|---|---|---|
| `socrata` | SODA 2.x JSON, paged by `$limit`/`$offset` ordered by `:id`; column metadata for the drift check | `adapters/socrata.py` (unchanged in behaviour) |
| `odata` | OData v4 JSON, following `@odata.nextLink`, reading `value` | `adapters/who_gho.py` (WHO GHO becomes config on it) |
| `https_file` | One file over HTTPS: `csv`, `xlsx`, or a `zip` with a named member in either format | `adapters/https_csv.py`; new: xlsx and zip |
| `html_table` | HTML pages: a `<table>` chosen by selector, or a listing of item pages read by declared selectors | new; #30's CROI parser moves onto it |
| `pdf_table` | Text PDFs found on an index page, tables rebuilt from geometry | #27's `pdf_tables.py` engine; `wayne_county.py` becomes config and extensions |
| `dashboard_export` | A dashboard's own data answer: flavour `tableau` (bootstrapSession) or `atlasplus` (CDC AtlasPlus export) | `adapters/tableau.py`; AtlasPlus later |
| `doi_metadata` | Crossref listing of a journal supplement, enriched by OpenAlex and Europe PMC | `adapters/doi_abstracts.py` and `doi_harvest.py` |

The set is closed: a new type is a new ADR. A type is a module that registers itself in `adapters.TYPES` with:

- `config_model`: the pydantic model its catalog block must satisfy. It subclasses `registry._Strict`
  (`extra="forbid"`, `frozen=True`), so a misspelt key such as `expected_sha25` is refused at load instead of
  silently switching its check off;
- `pages(entry, get, *, max_rows)`: the raw responses and the records parsed from them;
- `publisher_fields(entry, get)`: what the publisher serves today, for the drift check;
- its capabilities, declared rather than tested by name: `accept` (media types it asks for), `browser_capture`
  (response paths a browser load keeps), `guards_own_tables` (the row guard runs per table, as Tableau does, through the core's helper: see
  section 4),
  `checks_layout_on_read` (no separate drift check, as the DOI harvest does), `runner` (`refresh` into the lake, or
  `harvest` into a private store), and `live_limits` (how much one question may read).

`adapters.pages` and `adapters.publisher_fields` look the type up in `TYPES`; an unknown type is refused when the
catalog is loaded, never read as a CSV file.

### 2. Config schema per type

The catalog's `source:` block keeps what every type shares (`host`, `landing_url`, `api_url`, `government_host`,
`browser`, `dataset`) and gains `type:` (renamed from `portal`; the old name is read for one release, an entry that carries both is
refused, and `validate` warns on every entry still using `portal`) and one
nested block named after the type, validated by that type's `config_model`. `registry.Source` stops carrying every
type's fields and its portal-testing validators. A field that moves under the nested block (`source.tableau` to
`dashboard_export.tableau`, `source.supplements` to `doi_metadata.supplements`) is read from one place only; an entry
that still has it at the old place is refused. Extensions are listed under `extensions:` (section 3).

```yaml
# socrata
socrata: {page_size: 50000}                      # optional; our default (SODA's own is 1,000)

# odata
odata: {max_pages: 200}                          # optional safety cap

# https_file
https_file:
  format: xlsx                                   # csv | xlsx
  encoding: utf-8                                # csv only
  zip_member: "data/county_rates.xlsx"           # optional: the one member read from a .zip, by exact path
  sheet: "Table 1"                               # xlsx only: by name, never by position
  header_row: 4                                  # 1-based; or an extension (header_row_finder)
  skip_footer_rows: 0
  expected_sha256: null                          # optional: refuse a changed file until reviewed

# html_table
html_table:
  shape: table                                   # table | listing
  table_selector: "table#cases"                  # table: which table, by CSS selector
  caption: "Reported cases by county"            # optional: the table's caption must match exactly
  listing:                                       # listing only
    url_template: "https://{host}/search/?yr={year}&page={page}"
    item_link_selector: "h3.entry-title > a"
    item_path_pattern: "^/abstract/[a-z0-9-]+-(\\d{4})/$"
    total_selector: "p.results-count"            # completeness: rows kept must equal this total
    fields: {title: "h1.entry-title", body: "div.entry-content"}
    years: {first: 2014, last: 2027}

# pdf_table
pdf_table:
  index_link_pattern: ".*(quarter|q[1-4]|5-year|cd-).*\\.pdf"
  in_scope_paths: ["^/files/content/mainsite/.+\\.pdf$"]
  max_documents: 40                              # more links than this: the page changed; refuse
  table: {group_header: Group, label_header: Disease, suppression_marks: ["*"], trailing_total: "^(sub)?totals?\\b"}
  live: {max_documents: 3, order_by: year_in_filename}

# dashboard_export
dashboard_export:
  flavour: tableau                               # tableau | atlasplus
  tableau: {views: [...]}                        # today's source.tableau block, unchanged

# doi_metadata
doi_metadata:
  conference: idweek
  conference_name: IDWeek
  enrich: [openalex, europepmc]                  # which enrichers run; today fixed in code
  supplements: [...]                             # today's source.supplements, unchanged
```

**Caps and pacing are bounded.** Every cap in a type's config (`page_size`, `max_pages`, `max_documents`,
`live.max_documents`) is a pydantic field with `gt=0` and a ceiling the type owns (`le=`); none of them accepts
`null` or `0` as "no cap". `source.min_interval_s` can only slow a crawl down: it must be greater than 0, and the core
waits the longest of its own floor, the Crawl-delay, the API's interval and this value. Live limits are clamped by the
core to the hosted deadline whatever the catalog says.

**Patterns are scope guards.** Link patterns (`index_link_pattern`, `in_scope_paths`, `item_path_pattern`) are
applied with `fullmatch` to the path of the link after the core resolves it against the page's URL and normalises
it (no `..`, percent-decoded once, no userinfo). Text patterns (`trailing_total`, an extension's `key_pattern`) are
anchored as written. Every pattern is compiled when the catalog loads, so a bad one is refused by `validate`, and
runs with a linear-time engine (`google-re2`) on input of bounded length, because it reads untrusted publisher text.

Values that change a number (an area a row covers, a suppression mark, which rows are totals) are reviewed facts
under hard rule 1: they come from the codebook or a maintainer's declaration, and a draft that infers them carries an
open question in the contract. Moving them from Python into YAML moves them into the catalog review, not around it.

### 3. Extensions: named, pure, unit-tested hooks

Some sources need a step no type config can say. That step is an **extension**: a pure function in
`adapters/extensions/`, registered by name in one table (`EXTENSIONS`), with a declared signature for one hook point,
a docstring that says what it does in plain words, and its own table-driven unit tests over fixtures. A source opts
in by name:

```yaml
extensions:
  - {name: header_row_finder, hook: locate, args: {first_cell: "County"}}
  - {name: wide_to_long, hook: staging, args: {keep: [STATE, COUNTY], stubs: [POPESTIMATE], key_field: YEAR,
     key_pattern: "\\d{4}", expected_keys: ["2020", "2021", "2022", "2023", "2024", "2025"]}}
  - {name: metastore_url, hook: resolve, args: {pattern: "...", pick: newest}}
```

Hook points, each with a fixed signature:

| Hook | Runs | May | May not |
|---|---|---|---|
| `resolve` | before fetching | turn the catalog URL plus fetched index bytes into the list of URLs to fetch (a metastore, a "latest file" page) | fetch anything itself; return a URL outside its own entry's hosts and scope paths (section 4) |
| `locate` | after fetching, before parsing | say where the data sit in the raw bytes (a header row, a sheet, a table) | change a value |
| `parse` | in place of the type's record reader for one shape | turn bytes into records keyed by the publisher's own labels, every value as the publisher sent it | rename, compute, fill or drop a value; bypass the row guard (its records go through the core's page-level guard like any type's) |
| `staging` | in staging, never at load | reshape only: pivot wide to long, parse a period header | run before the raw layer is written; flag total rows (a business rule, in the intermediate dbt layer); check or reconcile totals (a contract quality rule or dbt test that fails the load) |

Rules for every extension:

- **Pure.** No network, no file system, no clock, no environment: inputs in, output out. The core does every fetch.
  A guard test checks it (see Test pattern).
- **Declared.** Its name, hook and argument model are registered; the argument model subclasses `registry._Strict`
  like a type's `config_model`. A catalog entry naming an unknown extension, a wrong hook or bad arguments is refused
  at catalog load (`health-data-registry validate`).
- **Generic in name and code.** An extension is named for what it does (`header_row_finder`, `wide_to_long`,
  `period_header_parser`, `metastore_url`), never for a source, and holds no source-specific values: those are its
  arguments, in the catalog.
- **Tested alone.** Each has a table-driven test over small hand-written fixtures (hard rule 5), and mutation tests
  run on it like any changed module (hard rule 13).
- **Reviewed like a contract.** A new extension, or a change to one, is a code review; a source choosing one is a
  catalog review.

A `staging` extension is part of the one shared projection that `staging.py` builds. Today that projection
(`staging.typed_select`) is used three ways: by the lake snapshot (`store.build_snapshot`), by the live in-memory read
and by the generated dbt staging model (`transform_gen.py`), so all three type every column the same way. A
`staging` extension joins that projection, so the lake, a live-only source (which has no dbt model) and dbt get the
same rows. A parity test runs each extension's fixtures through all three and compares the rows. The `staging.py`
docstring's "Nothing per source" becomes "nothing per source except declared extensions".

Each `staging` extension also declares its cardinality: `1:1`, or `1:n` with n taken from its arguments (for
`wide_to_long`, `len(expected_keys)`). From it the core checks row counts at the raw-to-staging boundary (staged rows
equal raw rows times n), beside the one-row-per-key test `transform_gen` already emits, so a reshape that drops or
duplicates rows stops the load.

The `wide_to_long` pivot shows the ELT boundary. Today `https_csv.wide_to_long` (configured by the catalog's
`source.reshape`) pivots the Census file before storage, so the raw layer holds our layout (#104 finding 5). As an
extension it is declared with `hook: staging`: the raw layer keeps the file's own wide columns, and the shared
projection applies the pivot as DuckDB `UNPIVOT` SQL built from the extension's arguments (no new dbt package), with
the same refusals: ragged stubs, keys other than `expected_keys`.

### 4. One shared core for fetch rules

Every adapter type and every runner (refresh, live read, DOI harvest, listing crawl, sitemap snapshot) fetches
through one getter built by `crawl.fetcher`, which owns, in one place:

- **Hosts and transport:** HTTPS on 443 to catalogued hosts only, public addresses only, byte and time caps
  (`fetch.py`); the browser transport for `browser: true` sources, with the same allowlist (`browser_fetch.py`).
- **robots.txt and the government-host policy:** read first; obeyed in full on other hosts; on a reviewed
  government host a disallow, or a 401, 403 or 429 for robots.txt, is overridden and recorded (owner decision,
  25 Nov 2026). `sitemaps.BrowserRobots`, which stops instead, is retired. That changes ADR 0013's stop rule, and
  the ADR index says records are not rewritten: if this ADR is accepted, it supersedes ADR 0013 in that one respect,
  and the index marks 0013 "superseded in part by 0016" before `BrowserRobots` is retired (open question 5). The
  override stays safe only while `government_host: true` is accepted just for hosts on `GOVERNMENT_DOMAINS`
  (`registry.py:342`) and entries on one host agree (`registry.py:1431`). Both validators stay on the shared
  `source:` block when `Source` is split per type, and step 1 tests that an unlisted host with
  `government_host: true` is still refused.
- **Pacing:** at least 10 s between page loads on government and browser hosts, the host's Crawl-delay when longer,
  an API's published rate (`api_intervals`), and a per-source floor from config (`source.min_interval_s`, e.g.
  CROI's 120 s), which can only lengthen the wait (section 2). `sitemaps.Pace` is retired.
- **Persisted pace and locks:** #30's rules (pace written before each request, per-host inter-process lock, a
  robots.txt reused for at most 24 hours) move from `polite.py` into the core so every long crawl gets them; issue
  #65 extends them across machines.
- **Per-source scope:** every URL that a `resolve` extension, a listing's `url_template` or an index page yields is
  checked before it is fetched against that entry's own hosts (`host`, the hosts of `api_url` and `landing_url`,
  declared `enrich_hosts`) and its `in_scope_paths`, not against the whole catalog's hosts, and refused in plain
  words otherwise. One source can't reach another source's host or borrow its `government_host` override. A
  `url_template` may use only declared placeholders, substituted values are URL-encoded, and its host must be
  `{host}` or equal `source.host`.
- **Blocks, back-off and retries:** a 401 or 403 for a page blocks that host for the run, government or not (only a
  refused robots.txt on a reviewed government host is overridden, as above); a 429 is backed off with jitter up to
  five tries; a 503 is retried after its Retry-After; a wait past the cap stops the fetch in plain words; hosted calls
  never wait past their deadline.
- **Provenance:** every attempt in the fetch log; every response's bytes hash-addressed (`lake/raw/` or the private
  area); `source_url`, `retrieved_at` and sha256 for each page in the manifest; override notes in the manifest.
- **Refusals:** one `LayoutError` base in the core that every type's layout error subclasses, so the tools' refusal
  (`tools.refusal` on main; #27 adds `query.live_refusal`) and the CLI give the same plain-words refusal whatever the
  type (ADR 0012).
- **Listing checkpoints:** a resumable listing crawl keeps its checkpoints in the lake (`$HDR_LAKE`, never in git:
  hard rule 5), not in a `crawl_state/` folder. Only a completed crawl whose kept items equal the listing's total
  builds a full snapshot: its raw bytes are every run's pages and its `retrieved_at` is the latest fetch across the
  resumed runs. An interrupted crawl builds no snapshot, unless it was run with `--max-rows`, which marks it partial
  as today.

The row guard (hard rule 4) also stays in the core, and no declaration switches it off. `adapters.pages` runs it on
every page. A type that declares `guards_own_tables` changes only *where* it runs: the type returns each table through
the core's helper (for example `row_guard.guarded(table)`, which returns a `GuardedTable` only after `check_grain`
passes), and `adapters.pages` refuses a page from that type unless every table on it is a `GuardedTable`. A `parse`
extension's records go through the page-level guard like any type's.

### 5. How this fits ELT, the contracts and the dbt stack

- **Raw as sent (ADR 0005, ADR 0015).** The core keeps every response's bytes. A type's `pages` and any `parse`
  extension produce records keyed by the publisher's own labels, with every value as the publisher sent it: no
  rename, no cast, no pivot, no derived column. Metadata we add is declared in the contract or goes in the manifest.
- **Staging does the changes.** Every `staging` extension compiles into the generated staging model
  (`transform_gen.py`), so the change sits in dbt beside the typed projection, with dbt unit tests. Load-time
  changes listed in #104 move there as they are touched.
- **Contracts stay the authority.** The type's config says where the data are; the ODCS contract says what each
  field means, its type, units, missing codes, keys and quality rules; the semantic model says how it may be
  aggregated. A type never decides a meaning. `publisher_fields` feeds the contract drift check for every type that
  does not check its layout on read.
- **The dbt raw contract** (`_raw_sources.yml`, generated) lists the fields the type hands on; changing a type's
  config so that it hands on different fields is a contract version bump and a new snapshot, as ADR 0015 says.
- **Config is part of a snapshot's identity.** `store.build_snapshot` keys a snapshot on the raw sha256s, the
  contract and semantic versions and `partial`, and never rewrites one that exists. Records now also depend on the
  type's config (`sheet`, `header_row`, `zip_member`, `table_selector`, `caption`) and on extensions, so a config
  change that keeps the same field names would be skipped and the old parse served. The snapshot inputs therefore
  add a digest of the type's config block and of each extension's name, arguments and code version, and the
  manifest records it with the method (ADR 0003). A test checks that a config-only change gives a new snapshot.
- **A refused pinned file shows.** When `expected_sha256` refuses a changed file, the freshness check that
  `list_caveats` reads reports it, not only the fetch log, so the data don't go stale unnoticed.
- **Tableau suppression** (#104 item 5) is not decided here: until the maintainer rules, the blanking stays where it
  is, as a documented exception.

## Refactor plan

No refactor code lands with this ADR. Each step is its own PR, on the usual review and fix cycle, with `make check`
passing. Ordered by risk, lowest first.

### Step 1: the scaffolding (no behaviour change)

1. `adapters.TYPES` registry and the `AdapterType` protocol; `adapters.pages` and `publisher_fields` dispatch through
   it; an unknown type is refused at catalog load. Register the four existing adapters as they are.
2. Capabilities replace the 18 portal tests: `accept`, `browser_capture` (Tableau's `/bootstrapSession/` moves out of
   `crawl.py`), `guards_own_tables`, `checks_layout_on_read` (the `doi` skip in `cli.check_contracts`), `runner`.
3. `crawl.refresh_dataset` names the conference from `source.conference`, not `id.removesuffix('_abstracts')`.
4. One `LayoutError` base in the core; existing layout errors subclass it.
5. `EXTENSIONS` registry and the four hook signatures, with no extension yet.
6. The guard tests below that pass on main once items 1 to 5 land: strict models, row guard per type, the
   `government_host` validators and extension purity. The no-source-ids and no-portal-branch tests land later, with
   the steps that remove what they would catch (see Test pattern).

### Step 2: the two new generic readers the next sources need

1. **`https_file` with `xlsx` and `zip`.** Generalise `https_csv` into `https_file` (`format: csv | xlsx`,
   `zip_member`, `sheet` by name, `header_row`, `skip_footer_rows`). The raw store keeps the downloaded file (the zip
   itself, not the extracted member), and the manifest also records `zip_member` and the member's own sha256, so an
   answer traces to the exact file read inside the archive. Zip safety in the core: one named member only, matched
   on its normalised name; no path traversal; an archive with two members of the same name, or with an encrypted,
   symlink or absolute-path entry, is refused, not skipped; uncompressed-size and ratio caps like `fetch.py`'s gzip
   cap; no nested archives.

   **xlsx is read as stored, not as printed.** Display formats change numbers (0.123 shows as 12.3%, rounding hides
   decimals, dates are serial numbers), so this choice changes a number (hard rule 1). A record holds each cell's
   stored value as text, and the cell's number format travels with it as metadata the contract may declare, so
   staging types dates and percentages from the contract, never from the display. A formula cell gives its cached
   value; a file with a formula cell that has no cached value is refused, never recalculated. An xlsx is itself a
   zip of XML, so it is opened through the same capped zip reader, and its XML is parsed entity-safe (`defusedxml`
   as a hard dependency: no DTDs, no external entities). Merged header cells and hidden sheets are a `locate`
   extension's job or a refusal. First extension: `header_row_finder` (`locate`).
2. **`html_table`, shape `table`.** One table by CSS selector, an optional exact caption, header cells as field names
   (spanning headers joined by a declared separator, or refused), every cell as text, footnote markers kept. The
   HTML parser loads nothing over the network.
   Server-rendered HTML only; a page that needs JavaScript sets `browser: true` and goes through the same core.

These come before any migration because new sources should land on them rather than as new modules.

### Step 3: move existing per-source code into config and extensions (by risk)

| Order | Code | Moves to | Risk |
|---|---|---|---|
| 1 | `who_gho.py` | `odata` type; WHO GHO becomes config | Low: no per-source code; rename and register |
| 2 | `live._licence_name` | `licence.name` in the catalog | Low |
| 3 | `doi_abstracts._NUMBER` | `doi_metadata.abstract_number_pattern` per supplement, defaulting to today's pattern; a supplement that declares `abstract_number_pattern: null` gets no number | Low: every current conference (APIC, ICID, IDWeek, SHEA) keeps today's numbers unless a maintainer declares another pattern for it |
| 4 | `doi_harvest` enrichers: `ENRICHERS` (line 58, which also builds `PROVENANCE_COLUMNS`) and the local `enrichers` tuple (lines 323-325); `METADATA_TERMS` | `doi_metadata.enrich` list, driving both; terms text in the catalog | Low |
| 5 | `sitemaps.SEEDS`, `ADR_0013_HOSTS` | `sitemaps:` lists on the catalog entries for those hosts; the ADR 0013 exception ends when they land | Low |
| 6 | `sitemaps.Pace`, `BrowserRobots` | The shared core (after the index marks ADR 0013 superseded in part) | Medium: the sitemap job's stop rule changes on government hosts |
| 7 | `https_csv` and its `wide_to_long` (catalog `source.reshape`) | `https_file` (csv) plus the `wide_to_long` extension at `hook: staging` | Medium: the raw layer of `census_county_pop` changes, so its contract version is bumped and it is re-pulled to a new snapshot (ADR 0015) |
| 8 | `tableau.py` | `dashboard_export` (flavour `tableau`); `source.tableau` moves under it unchanged | Medium: touches the live Detroit dashboards; fixture tests must pass byte for byte |
| 9 | #27 `wayne_county.py` | `pdf_table` config (index pattern, scope paths, caps, `TableSpec`); extension `period_header_parser` at `hook: staging`; total-row flags in the intermediate dbt layer; reconciliation of totals as a contract quality rule that fails the load; `AREA` and `excludes_detroit` as a maintainer's declaration in the contract | High: values that change how a number is read move layer; best done before #27 merges or in its fix round |
| 10 | #30 `croi.py`, `croi_index.py`, `polite.py` | `html_table` shape `listing` (selectors, years, completeness against the listing total, checkpoints in the lake as in section 4); `polite.py`'s rules into the core; dataset ids and the 120 s floor into the catalog | High: a 47-hour resumable crawl with crash-safety rules; move last, after the listing shape has its own tests |

`dashboard_export` flavour `atlasplus` and `pdf_table` for a second publisher are added on demand, as config on the
existing types.

### Test pattern

- **One table-driven test per adapter type.** `tests/adapters/test_<type>.py` holds a table of cases, each a small
  hand-written fixture (bytes as a publisher might send them, never real data: hard rule 5), a config block, and the
  expected records or the expected refusal. Every type's table includes: the happy path; the drift check
  (`publisher_fields`); a changed layout refused in plain words; a row-level table refused by the row guard; values
  kept exactly as printed (thousands separators, suppression marks, footnotes); and `max_rows`.
- **One table-driven test per extension**, over fixtures, with its argument validation cases.
- **A catalog test** loads every real catalog entry through its type's `config_model` and every named extension
  through the registry.
- **A strict-models test** iterates `TYPES` and `EXTENSIONS` and fails on any config or argument model whose
  `model_config` isn't `extra="forbid"`.
- **A row-guard test** iterates `TYPES` and every `parse` extension and requires a "row-level table refused" case for
  each, so no type or extension can leave it out.
- **An extension purity test** fails if a module under `adapters/extensions/` imports `fetch`, `browser_fetch`,
  `crawl`, `socket`, `urllib`, `os`, `pathlib` or `subprocess`, or reads the clock; extension tests run with sockets
  disabled.
- **A no-source-ids test** fails when an adapter or extension module names a specific source: it reads every module
  under `src/health_data_registry/adapters/` (types and extensions) and fails if its code, not its comments or
  docstrings, contains any catalog id, any catalogued host, or a `source_id ==`/`entry.id ==` comparison. Hosts a
  type owns as API facts (Crossref, OpenAlex and Europe PMC for `doi_metadata`) are declared on the type and exempt.
  Hosts and ids belong in `catalog/`. The test reads the ids and hosts from the loaded registry, so a new catalog
  entry is covered without editing the test. It lands with step 3 row 9, the first point at which it passes.
- **A no-portal-branch test** fails when code outside `adapters/__init__.py` compares `type` or `portal` to a string
  literal; behaviour that differs by type is a declared capability. After step 1, main still compares in the
  registry validators (`registry.py:336-358`), `doi_harvest.conferences()` (line 91), `crawl._busy` (line 518) and
  the `harvest-doi` hint (line 552), so this test lands with the per-type config split (section 2), which removes
  them and adds a capability for the doi type filter and `_busy`.

## Consequences

- A new source on an existing type is a catalog entry, a contract and a semantic model, plus at most an extension
  that is generic by construction. Reviews of a new source are mostly catalog and contract reviews.
- Rules every fetch must follow have one implementation, so a policy change (as on 25 Nov 2026) lands once.
- Values that change how a number is read move out of Python into reviewed YAML and contracts, where hard rule 1 and
  hard rule 3 already apply.
- `portal` is renamed `type` in every catalog entry; the loader reads both for one release.
- Some sources will still need code; this keeps that code small, named, pure and tested on its own, and makes its
  use visible in the catalog.
- PRs #27 and #30 would be reshaped before or after merge; that choice is the maintainer's.

## Open questions

1. Is CROI's listing-and-detail crawl a shape of `html_table`, or its own type (`html_pages`)? This ADR proposes a
   shape, to keep the set small.
2. Does `doi_metadata` stay one type with a fixed API trio, or become a `bibliographic` type whose APIs are config?
3. Does the `sitemaps` discovery job become a `resolve` extension feeding the other types, or stay a separate job on
   the shared core?
4. Where does Tableau's suppression blanking sit under ELT (#104 item 5)?
5. Is ADR 0013's stop rule for a refused robots.txt on a government host superseded in part by this ADR (the index
   marks 0013 "superseded in part by 0016"), or by a separate record that lands first? This ADR proposes the former.
