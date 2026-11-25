# 0016. Crawler factory: generic adapter types, per-source config, declared extensions

- **Status:** Proposed
- **Date:** 2026-11-25
- **Spec:** "Crawling"; the maintainer's direction of 25 Nov 2026
- **Related:** [ADR 0002](0002-primary-sources-only.md) (primary sources only),
  [ADR 0003](0003-provenance-on-every-result.md) (provenance on every result),
  [ADR 0005](0005-raw-fetches-hash-addressed.md) (raw fetches kept, hash-addressed),
  [ADR 0007](0007-odcs-contracts-and-semantic-models.md) (contracts and the contract facts rule),
  [ADR 0013](0013-browser-fetcher-on-government-hosts.md) (browser fetcher on government hosts),
  [ADR 0014](0014-bibliographic-registries-as-primary-routes.md) (bibliographic registries), ADR 0015 (ELT: raw
  data stored as sent; proposed on PR #108), issue [#104](https://github.com/jtouley/health-data-registry/issues/104)
  (ELT audit), issue [#65](https://github.com/jtouley/health-data-registry/issues/65) (pacing and locks across
  machines), the audit in [docs/dev/crawler-audit.md](../dev/crawler-audit.md), PRs #27 and #30

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
  `registry.py` and `doi_harvest.py` (18 tests on main). `publisher_fields` falls through to the CSV reader for any
  portal it doesn't know.

The P1 queue needs two more kinds of source soon: spreadsheets and zipped files over HTTPS, and tables on HTML pages.
Built the way #27 and #30 were built, each would be another single-source module.

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

- `config_model`: the pydantic model its catalog block must satisfy;
- `pages(entry, get, *, max_rows)`: the raw responses and the records parsed from them;
- `publisher_fields(entry, get)`: what the publisher serves today, for the drift check;
- its capabilities, declared rather than tested by name: `accept` (media types it asks for), `browser_capture`
  (response paths a browser load keeps), `guards_own_tables` (runs the row guard per table, as Tableau does),
  `checks_layout_on_read` (no separate drift check, as the DOI harvest does), `runner` (`refresh` into the lake, or
  `harvest` into a private store), and `live_limits` (how much one question may read).

`adapters.pages` and `adapters.publisher_fields` look the type up in `TYPES`; an unknown type is refused when the
catalog is loaded, never read as a CSV file.

### 2. Config schema per type

The catalog's `source:` block keeps what every type shares (`host`, `landing_url`, `api_url`, `government_host`,
`browser`, `dataset`) and gains `type:` (renamed from `portal`; the old name is read for one release) and one
nested block named after the type, validated by that type's `config_model`. `registry.Source` stops carrying every
type's fields and its portal-testing validators. Extensions are listed under `extensions:` (section 3).

```yaml
# socrata
socrata: {page_size: 50000}                      # optional; SODA's default

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
  index_link_pattern: "(quarter|q[1-4]|5-year|cd-).*\\.pdf$"
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
| `resolve` | before fetching | turn the catalog URL plus fetched index bytes into the list of URLs to fetch (a metastore, a "latest file" page) | fetch anything itself; return a host outside the catalog |
| `locate` | after fetching, before parsing | say where the data sit in the raw bytes (a header row, a sheet, a table) | change a value |
| `parse` | in place of the type's record reader for one shape | turn bytes into records keyed by the publisher's own labels, every value as printed | rename, compute, fill or drop a value |
| `staging` | in staging, never at load | reshape or derive (pivot wide to long, parse a period header, flag a total row) | run before the raw layer is written |

Rules for every extension:

- **Pure.** No network, no file system, no clock, no environment: inputs in, output out. The core does every fetch.
- **Declared.** Its name, hook and argument model are registered; a catalog entry naming an unknown extension, a wrong
  hook or bad arguments is refused at catalog load (`health-data-registry validate`).
- **Generic in name and code.** An extension is named for what it does (`header_row_finder`, `wide_to_long`,
  `period_header_parser`, `metastore_url`), never for a source, and holds no source-specific values: those are its
  arguments, in the catalog.
- **Tested alone.** Each has a table-driven test over small hand-written fixtures (hard rule 5), and mutation tests
  run on it like any changed module (hard rule 13).
- **Reviewed like a contract.** A new extension, or a change to one, is a code review; a source choosing one is a
  catalog review.

The `wide_to_long` pivot shows the ELT boundary. Today `https_csv` pivots the Census file before storage, so the
raw layer holds our layout (#104 finding 5). As an extension it is declared with `hook: staging`: the raw layer keeps
the file's own wide columns, and the generated staging model applies the pivot (`transform_gen.py` emits a dbt
`unpivot` from the extension's arguments, with the same refusals: ragged stubs, keys other than `expected_keys`).

### 4. One shared core for fetch rules

Every adapter type and every runner (refresh, live read, DOI harvest, listing crawl, sitemap snapshot) fetches
through one getter built by `crawl.fetcher`, which owns, in one place:

- **Hosts and transport:** HTTPS on 443 to catalogued hosts only, public addresses only, byte and time caps
  (`fetch.py`); the browser transport for `browser: true` sources, with the same allowlist (`browser_fetch.py`).
- **robots.txt and the government-host policy:** read first; obeyed in full on other hosts; on a reviewed
  government host a disallow, or a 401, 403 or 429 for robots.txt, is overridden and recorded (owner decision,
  25 Nov 2026). `sitemaps.BrowserRobots`, which stops instead, is retired; ADR 0013's stop conditions are amended to
  match before that lands.
- **Pacing:** at least 10 s between page loads on government and browser hosts, the host's Crawl-delay when longer,
  an API's published rate (`api_intervals`), and a per-source floor from config (`source.min_interval_s`, e.g.
  CROI's 120 s). `sitemaps.Pace` is retired.
- **Persisted pace and locks:** #30's rules (pace written before each request, per-host inter-process lock, a
  robots.txt reused for at most 24 hours) move from `polite.py` into the core so every long crawl gets them; issue
  #65 extends them across machines.
- **Blocks, back-off and retries:** a 401 or 403 blocks the host for the run; a 429 is backed off with jitter up to
  five tries; a 503 is retried after its Retry-After; a wait past the cap stops the fetch in plain words; hosted calls
  never wait past their deadline.
- **Provenance:** every attempt in the fetch log; every response's bytes hash-addressed (`lake/raw/` or the private
  area); `source_url`, `retrieved_at` and sha256 for each page in the manifest; override notes in the manifest.
- **Refusals:** one `LayoutError` base in the core that every type's layout error subclasses, so `query.live_refusal`
  and the CLI give the same plain-words refusal whatever the type (ADR 0012).

The row guard (hard rule 4) also stays in the core: `adapters.pages` runs it on every page unless the type declares
`guards_own_tables`, in which case the type must run it on each table before projecting anything.

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
6. The guard tests below.

### Step 2: the two new generic types the P1 queue needs first

1. **`https_file` with `xlsx` and `zip`.** Generalise `https_csv` into `https_file` (`format: csv | xlsx`,
   `zip_member`, `sheet` by name, `header_row`, `skip_footer_rows`). The raw store keeps the downloaded file (the zip
   itself, not the extracted member); records are the member's cells as text, as printed. Zip safety in the core: one
   named member only, no path traversal, uncompressed-size and ratio caps like `fetch.py`'s gzip cap, no nested
   archives. xlsx: values as stored text, never formulas evaluated; merged header cells and hidden sheets are a
   `locate` extension's job or a refusal. First extension: `header_row_finder` (`locate`).
2. **`html_table`, shape `table`.** One table by CSS selector, an optional exact caption, header cells as field names
   (spanning headers joined by a declared separator, or refused), every cell as text, footnote markers kept.
   Server-rendered HTML only; a page that needs JavaScript sets `browser: true` and goes through the same core.

These come before any migration because new sources should land on them rather than as new modules.

### Step 3: move existing per-source code into config and extensions (by risk)

| Order | Code | Moves to | Risk |
|---|---|---|---|
| 1 | `who_gho.py` | `odata` type; WHO GHO becomes config | Low: no per-source code; rename and register |
| 2 | `live._licence_name` | `licence.name` in the catalog | Low |
| 3 | `doi_abstracts._NUMBER` | `doi_metadata.abstract_number_pattern` per supplement (IDWeek's pattern as its value); none means no number | Low; changes `abstract_number` only for conferences that don't use IDWeek's form, which is the fix |
| 4 | `doi_harvest` enricher tuple; `METADATA_TERMS` | `doi_metadata.enrich` list; terms text in the catalog | Low |
| 5 | `sitemaps.SEEDS`, `ADR_0013_HOSTS` | `sitemaps:` lists on the catalog entries for those hosts; the ADR 0013 exception ends when they land | Low |
| 6 | `sitemaps.Pace`, `BrowserRobots` | The shared core (after the ADR 0013 amendment) | Medium: the sitemap job's stop rule changes on government hosts |
| 7 | `https_csv` and `reshape.wide_to_long` | `https_file` (csv) plus the `wide_to_long` extension at `hook: staging` | Medium: the raw layer of `census_county_pop` changes, so its contract version is bumped and it is re-pulled to a new snapshot (ADR 0015) |
| 8 | `tableau.py` | `dashboard_export` (flavour `tableau`); `source.tableau` moves under it unchanged | Medium: touches the live Detroit dashboards; fixture tests must pass byte for byte |
| 9 | #27 `wayne_county.py` | `pdf_table` config (index pattern, scope paths, caps, `TableSpec`); extensions `period_header_parser` and `totals_reconciliation` at `hook: staging`; `AREA` and `excludes_detroit` as a maintainer's declaration in the contract | High: values that change how a number is read move layer; best done before #27 merges or in its fix round |
| 10 | #30 `croi.py`, `croi_index.py`, `polite.py` | `html_table` shape `listing` (selectors, years, completeness against the listing total, checkpoints); `polite.py`'s rules into the core; dataset ids and the 120 s floor into the catalog | High: a 47-hour resumable crawl with crash-safety rules; move last, after the listing shape has its own tests |

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
- **A no-source-ids test** fails when an adapter or extension module names a specific source: it reads every module
  under `src/health_data_registry/adapters/` (types and extensions) and fails if one contains any catalog id, any
  catalogued host, or a `source_id ==`/`entry.id ==` comparison. Hosts and ids belong in `catalog/`. The test
  reads the ids and hosts from the loaded registry, so a new catalog entry is covered without editing the test.
- **A no-portal-branch test** fails when code outside `adapters/__init__.py` compares `type` or `portal` to a string
  literal; behaviour that differs by type is a declared capability.

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
