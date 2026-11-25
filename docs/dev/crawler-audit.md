# Crawler audit: generic types, per-source code and per-source branches

Read on 25 Nov 2026 against `main` at 578e5df, plus the open PRs #27 (`feat/wayne-cauris`, Wayne County PDF
reports) and #30 (`feat/croi-index`, the CROI abstract index). No code was changed. This audit is the evidence for
[ADR 0016](../adr/0016-crawler-factory.md) (crawler factory, proposed). Issue
[#104](https://github.com/jtouley/health-data-registry/issues/104) audits the same paths for values changed
before storage (the ELT rule, [ADR 0015](../adr/0015-elt-raw-data-stored-as-sent.md)); this one asks a different
question: what in Python is specific to one source, and so should be configuration or a named extension.

Each entry below gives:

- **Type**: the generic adapter type it is, or should be, under ADR 0016 (`socrata`, `odata`, `https_file`,
  `html_table`, `pdf_table`, `dashboard_export`, `doi_metadata`), or "core" for shared fetch machinery.
- **Hard-coded per source**: a value about one publisher that lives in Python and belongs in the catalog entry.
- **Per-source branches**: an `if` on a portal that stands for one source, a parser written for one publisher, or a
  one-off function.

## Summary

| Path | Type | Per-source code in Python | Verdict |
|---|---|---|---|
| `adapters/socrata.py` | `socrata` | none | Generic already |
| `adapters/who_gho.py` | `odata` | WHO named in the module; paging cap | Generic in practice; rename to `odata` |
| `adapters/https_csv.py` | `https_file` (csv) | none in code; the Census pivot runs in the adapter | Generic; pivot moves to staging (ELT) |
| `adapters/tableau.py` | `dashboard_export` (tableau) | none in code | Generic; the capture path lives in `crawl.py` |
| `adapters/doi_abstracts.py` | `doi_metadata` | IDWeek's title numbering applied to every conference; API constants | Mostly generic; one per-source rule |
| `adapters/row_guard.py` | core (guard) | none | Generic; one portal bypass in the dispatcher |
| `adapters/__init__.py` | core (dispatch) | closed `if portal ==` chains | Replace with a type registry |
| `crawl.py` | core (policy, getter) | Tableau capture path; `harvest-doi` name built from the id; `doi` skip | Move the three into type config |
| `fetch.py` | core (transport) | none | Generic |
| `browser_fetch.py` | core (transport) | none | Generic |
| `live.py` | core (live read) | licence display names; per-portal report cap on #27 | Licence names to catalog |
| `doi_harvest.py` | `doi_metadata` (runner) | enricher list, row layout and CROI columns in code | Generic per conference; enrichers to config |
| `private.py` | core (private store) | none | Generic |
| `sitemaps.py` | core (discovery) | three Michigan seeds and two hosts; its own pacing and robots policy | Seeds to catalog; one policy |
| `registry.py` (`Source`) | core (config schema) | portal-specific fields and validators on one model; reviewed government domains | Split into per-type config models |
| `cli.py` | core | `doi` skip in `check-contracts` | Type capability, not a portal test |
| #27 `adapters/pdf_tables.py` | `pdf_table` | none (a `TableSpec`) | Generic; becomes the `pdf_table` engine |
| #27 `adapters/wayne_county.py` | `pdf_table` | almost all of it | One source's module; split into config and extensions |
| #27 `crawl.py`, `live.py`, `query.py` | core | `Accept` by portal; report cap; refusal keyed to the PDF error class | Type config and one shared layout error |
| #30 `adapters/croi.py` | `html_table` (listing and detail pages) | almost all of it | One source's module; selectors to config |
| #30 `croi_index.py` | runner | dataset ids, years, delay, checkpoint path | One source's runner; to config |
| #30 `polite.py` | core (policy) | CROI's 120 s floor named in docs | A second policy core; merge into one |

`paging.py` was also read: it pages MCP answers, not publisher responses, so it is not an ingestion path.

The main findings:

1. **Two per-source modules on open PRs.** `wayne_county.py` (#27) and `croi.py` plus `croi_index.py` (#30) are each
   written for one publisher: the host, the index URL, file-name patterns, CSS classes, year ranges, the area a
   report covers and the source's dataset ids are Python constants, and each gets its own `portal` value
   (`pdf_report`, `croi`) that names exactly one source.
2. **Three access-policy cores.** `crawl.Robots` (pacing, robots.txt, government-host overrides, 429 back-off),
   `sitemaps.Pace` with `BrowserRobots`, and #30's `polite.PoliteClient` (persisted pacing state, a per-host lock,
   a 24-hour robots.txt cache). They disagree on what a refused robots.txt on a government host means: `crawl.py`
   overrides it under the owner decision of 25 Nov 2026, `sitemaps.py` stops (as ADR 0013 still says).
3. **Closed dispatch on `portal`.** `adapters/__init__.py`, `crawl.py`, `cli.py`, `registry.py` and `doi_harvest.py`
   test `portal == "..."` or `portal != "..."` in 18 places on main, and #27 and #30 add more. Each new source
   type touches all of them.
4. **The existing generic adapters are close to the target.** `socrata`, `who_gho`, `https_csv` and `tableau` read
   everything source-specific from the catalog entry already. What they lack is a declared config schema per type
   and a place for small custom steps other than the adapter's own code.
5. **Transforms run at load time.** The Census wide-to-long pivot (`https_csv`) and Wayne County's period, area and
   total-row inference run before storage. Under ADR 0015 they belong in staging; ADR 0016 keeps them as named
   extensions that run there.

## Main: adapters

### `adapters/socrata.py`

- **Type:** `socrata`.
- **Hard-coded per source:** none. The dataset id, host and API URL come from the catalog entry. `PAGE_SIZE`
  (50,000) and the `:id` ordering are SODA facts, the same for every Socrata portal.
- **Per-source branches:** none.
- **Notes:** `_flat` re-serialises nested location objects (#104 finding 3). Over-delivery past `max_rows` is #73.

### `adapters/who_gho.py`

- **Type:** `odata`. Nothing in the module is WHO-specific except its docstring: it follows `@odata.nextLink`,
  reads the `value` array and asks `$top=1` for the drift check. Any OData v4 feed would read the same way.
- **Hard-coded per source:** `MAX_PAGES = 200` (a safety cap, reasonable as a type default with a per-source
  override).
- **Per-source branches:** none. The licence handling (live only, never stored) is driven by the catalog's
  `licence.redistribution: false`, not by code.
- **Notes:** `_scalars` drops nested fields (#104 finding 4).

### `adapters/https_csv.py`

- **Type:** `https_file`, format `csv`.
- **Hard-coded per source:** none. Encoding comes from `source.encoding`, the pivot from `source.reshape`.
- **Per-source branches:** none, but `https_csv.wide_to_long` (line 62, configured by the catalog key
  `source.reshape`, `registry.py:289`) exists for one source (`census_county_pop`) and runs
  inside the adapter, so `raw_records/` holds our layout, not the file's (#104 finding 5). Under ADR 0016 it
  becomes the `wide_to_long` extension, declared in config and run in staging.
- **Notes:** no xlsx, no zip member, no header-row offset: the next sources need all three (ADR 0016, step 2).

### `adapters/tableau.py`

- **Type:** `dashboard_export`, flavour `tableau`.
- **Hard-coded per source:** none. Views, worksheets, layout hashes and `suppressed_by` come from
  `source.tableau`. The suppression pattern (`SUPPRESSED`), the alias and flag suffixes and `view_url`'s
  `?:embed=y&:showVizHome=no` are Tableau facts.
- **Per-source branches:** none in the module. Two sit outside it: `crawl.py` decides to capture
  `/bootstrapSession/` with `src.portal == "tableau"` (`crawl.py:72`, `:384`), and `adapters/__init__.py:38` skips the
  page-level row guard for Tableau because the adapter guards each worksheet itself.
- **Notes:** suppression blanking (`_flag_suppressed`, `apply_suppressed_by`) changes values at load; #104 item 5
  asks the maintainer to rule on it. ADR 0016 does not decide it.

### `adapters/doi_abstracts.py`

- **Type:** `doi_metadata` (Crossref list, OpenAlex and Europe PMC enrichment).
- **Hard-coded per source:**
  - `_NUMBER` (line 72) reads the abstract number from titles shaped "568. ..." or "P-1589. ...": IDWeek's
    convention, applied to every conference. A conference that numbers abstracts differently gets a wrong or
    missing `abstract_number` silently. This is the one real per-source rule in the module; it belongs in the
    supplement's config as a declared pattern, or as a named extension.
  - `MIN_INTERVAL_S` (line 48), `METADATA_TERMS` (line 53), the three hosts and batch sizes: API facts, the same for
    every conference. They are type constants, not per-source; `METADATA_TERMS` would be better as reviewed
    catalog text so a terms change is a catalog review, not a code change.
- **Per-source branches:** none.

### `adapters/row_guard.py`

- **Type:** core (the hard rule 4 guard). Generic; reads field names and count values only.
- **Per-source branches:** none. Its one bypass is in the dispatcher (`portal != "tableau"`), which ADR 0016
  replaces with a type capability ("guards its own tables").

### `adapters/__init__.py`

- **Type:** core (dispatch).
- **Per-source branches:** `_pages` and `publisher_fields` are closed `if portal == ...` chains (lines 38-81). The
  `else` of `_pages` raises for `doi`; `publisher_fields` falls through to `https_csv` for anything unknown, so a
  new portal without a branch would be read as a CSV file. ADR 0016 replaces both chains with a registry of
  types, each declaring `pages`, `publisher_fields` and its capabilities.

## Main: shared core

### `crawl.py`

- **Type:** core: the access policy (`Robots`), the polite getter (`fetcher`), `refresh_dataset`.
- **Hard-coded per source:**
  - `TABLEAU_DATA = "/bootstrapSession/"` and `tableau = src.portal == "tableau"` (lines 72, 384-389): which
    response a browser load captures is a property of the dashboard flavour, so it belongs to the
    `dashboard_export` type's config.
  - Every non-browser fetch sends `Accept: application/json` (line 381). #27 adds a per-portal `_ACCEPT` map for
    `pdf_report`. The accepted media types belong to the type.
- **Per-source branches:**
  - `refresh_dataset` builds the `harvest-doi` hint from `ds.id.removesuffix('_abstracts')` (line 551): it assumes
    every DOI source's id ends in `_abstracts` and equals its conference key. The key is in config
    (`source.conference`); the message should read it.
  - `_busy` skips `portal != "doi"` (line 518) to name the right publisher for a busy host.
- **Shared rules that stay here:** HTTPS-only, catalogued hosts, robots.txt, the government-host override (owner
  decision, 25 Nov 2026), 10 s pacing on government and browser hosts, 429 back-off with jitter, 503 retry,
  deadline caps, the fetch log and the override notes for the manifest. All generic; ADR 0016 keeps them in one core.

### `fetch.py` and `browser_fetch.py`

- **Type:** core (transport).
- **Hard-coded per source:** none. Hosts in comments and docstrings only (`data.cdc.gov` as an example, michigan.gov
  as the reason the browser exists).
- **Per-source branches:** none.

### `live.py`

- **Type:** core (live, in-memory read).
- **Hard-coded per source:** `_licence_name` (line 69) maps two licence ids (`CC-BY-NC-SA-3.0-IGO`,
  `WHO-DATASET-TERMS`) to display names. A licence's display name is a catalog fact (`licence.name`).
- **Per-source branches:** none on main. The TTL is chosen by `source.browser`, which is config. #27 adds
  `LIVE_MAX_REPORTS = 3` and a `max_reports` argument threaded through `adapters.pages` for one source type;
  ADR 0016 makes it a `pdf_table` config field (`live.max_documents`).

### `doi_harvest.py`

- **Type:** `doi_metadata` runner (CLI only, private store).
- **Hard-coded per source:** none per conference: a conference is a catalog entry (`source.conference`,
  `source.supplements`). Type-level code that could be config:
  - the enrichers are fixed in two places: `ENRICHERS` (line 58), which also builds `PROVENANCE_COLUMNS`
    (lines 60-61), and the local `enrichers` tuple (lines 323-325), which fixes OpenAlex then Europe PMC with their
    batch sizes and parsers. A catalog `enrich: [openalex, europepmc]` list would let a source opt out of one; it
    has to drive both, or a source that opts out still gets provenance columns that are declared but empty;
  - `RESEARCH_INDEX_COLUMNS` (line 69) is described as "the columns every research-index row shares with the CROI
    index": a cross-source layout held in one source type's module.
- **Per-source branches:** `conferences()` selects `portal == "doi"` (line 91). Fine as a type filter.

### `private.py`

- **Type:** core (private-tier store). Generic: one store per source id, contract columns only.
- **Per-source branches:** none.

### `sitemaps.py`

- **Type:** core (discovery queue for crawlers), not a dataset adapter.
- **Hard-coded per source:**
  - `SEEDS` (line 89): three Michigan sitemap URLs (`mdhhs`, `emergingdiseases`, `kentcounty`).
  - `ADR_0013_HOSTS` (line 98): two hosts allowed by ADR exception until their catalog entries land.
  - `PACE_S = 10.0` duplicates `crawl.MIN_INTERVAL_S`.
- **Per-source branches:** none, but it carries **its own access policy**: `Pace` (line 325) and `BrowserRobots`
  (line 375). `BrowserRobots` treats a 401, 403 or 429 on a government host's robots.txt as a block, while
  `crawl.Robots` overrides it under the owner decision of 25 Nov 2026. One host, two rules, depending on which job
  reads it.

### `registry.py` (`Source`)

- **Type:** core (config schema).
- **Hard-coded per source:** `GOVERNMENT_DOMAINS` (line 265) names the Detroit-area county domains. This is a
  reviewed security list kept in code on purpose (a catalog typo must not switch robots.txt off), so it stays; it
  is listed for completeness.
- **Per-source branches:** one flat `Source` model carries every type's fields (`encoding`, `reshape`, `tableau`,
  `supplements`, `enrich_hosts`, `conference`, `conference_name`), with validators that test the portal
  (lines 336-358) to say which fields go with which type. ADR 0016 splits this into one config model per type,
  chosen by `type`.

### `cli.py`

- **Per-source branches:** `check_contracts` skips `portal == "doi"` (line 92). #30 adds a `croi` skip. Both become a
  type capability ("checks its own layout on every run").

## Open PR #27: Wayne County PDF reports

### `adapters/pdf_tables.py`

- **Type:** `pdf_table` (engine). Generic: rebuilds tables from word positions and cell borders, configured by a
  `TableSpec` (group and label headers, suppression marks, a trailing-total pattern, whether blanks are allowed).
  Byte, page and time caps on an untrusted PDF are type defaults.
- **Hard-coded per source:** `_HEADER_VOCAB` lists header words seen in Wayne County's reports ("YTD", "5-Year",
  "Q1-Q4"). It should be the spec's `header_words`, defaulting to a generic set.
- **Per-source branches:** none.

### `adapters/wayne_county.py`

- **Type:** `pdf_table` (one source's module).
- **Hard-coded per source** (nearly the whole module):
  - `INDEX_URL` and `HOST` repeat the catalog's `api_url` and `host`.
  - `AREA` and `AREA_UNSTATED`: what area a row covers. This is a fact that changes how a number is read (hard
    rule 1): it should be a maintainer's reviewed declaration in the catalog or contract, not a Python string.
  - Discovery: `_REPORT_NAME` (file names that look like reports), `_IN_SCOPE_PATH` and `_IN_SCOPE_ANNUAL` (which
    layouts are read), `MAX_REPORTS = 40`, `_NAME_YEAR` and `newest` (ordering by a year in the file name).
  - Report facts read from the text: `_EXCLUDES_DETROIT`, `_RETRIEVED`, `_TITLE_YEAR`.
  - The period header grammar: `_QUARTER`, `_QUARTER_NO_YEAR`, `_TOTALS`, `_YTD`, `_ANNUAL_TOTAL`, `_YEAR`,
    `_SPAN_TOTAL`, `_SPAN_AVERAGE`, with `parse_period` and `resolve_periods`.
  - Reconciliation: `checks`, `reconcile`, `total_mismatches`, `MAX_FAILED_ROWS = 2`, `TOTAL_LABEL`.
  - The record layout in `records()`: derived `area`, `excludes_detroit`, `period_*`, `is_total_row`,
    `failed_checks` (#104 findings 22-25).
- **Per-source branches:** the portal `pdf_report` dispatches straight to `wayne_county.pages` and
  `wayne_county.publisher_fields`, so the portal name means "Wayne County". A second county's PDFs would need a
  second module or a branch inside this one.

### `crawl.py`, `live.py`, `query.py` on #27

- `crawl._ACCEPT = {"pdf_report": ...}` and a `host_portal` map so a report linked from the index page gets the
  right `Accept` header: type config.
- `live.LIVE_MAX_REPORTS`, `LIVE_READ_BUDGET_S` and a `max_reports` argument added to every adapter's signature for
  one type: type config.
- `query.live_refusal` imports `pdf_tables.LayoutError` to pick the "layout unreadable" refusal. Each adapter has its
  own layout error class (`TableauLayoutError`, `ApiLayoutError`, `pdf_tables.LayoutError`, `croi.LayoutError`); a
  shared base in the core would let the refusal cover every type.

## Open PR #30: CROI abstract index

### `adapters/croi.py`

- **Type:** `html_table`, listing-and-detail shape (a paginated search listing, then one HTML page per abstract,
  fields read by CSS class and sidebar headings). It is not a `<table>`; whether this shape stays under
  `html_table` or becomes its own type is an open question in ADR 0016.
- **Hard-coded per source:** `HOST`, `BASE`, `FIRST_YEAR`/`LAST_YEAR` (2014-2027), `PER_PAGE = 10`, the listing
  URL template and query parameters (`aa`, `at`, `an`, `sn`, `kw`, `yr`, `st`), `_ABSTRACT_PATH`,
  `_NUMBER_TITLE`, `_COUNT` ("1277 Results"), the CSS classes and sidebar headings read, and `COPYRIGHT` (a licence
  statement, which belongs in the catalog's licence block).
- **Per-source branches:** the portal `croi` names one source. `CONDITION_RULES`, `DESIGN_RULES`,
  `DATA_SOURCE_RULES` and `GEOGRAPHY_RULES` duplicate main's `research_tags.py`.

### `croi_index.py`

- **Type:** runner for one source (CLI only, private store).
- **Hard-coded per source:** `INDEX_ID = "croi_abstracts_index"` and `TEXT_ID = "croi_abstracts_text"` (dataset ids
  in code), `MIN_YEARS = 5`, `LATEST_HELD_YEAR = 2026`, `CROI_DELAY_S = 120.0`, the checkpoint path
  `crawl_state/croi/`, and the listing-total rule for completeness.
- **Per-source branches:** the whole module, plus `index-croi` in `cli.py` and a `croi` skip in `check-contracts`.
- **Generic ideas worth keeping:** per-year checkpoints, completeness against the listing's own total, and
  same-year redirect rules. They are useful to any listing crawl and belong in the `html_table` listing shape.

### `polite.py`

- **Type:** core (a second access-policy implementation): robots.txt first, the site's crawl delay with a floor,
  pacing state persisted before each request, a per-host inter-process lock, a 24-hour robots.txt cache, one
  request per call with redirects reported.
- **Per-source branches:** none in code; CROI's 120 s floor is set by the caller.
- **Finding:** these are good rules that `crawl.Robots` lacks (persisted pace, locks: issue #65), but as a separate
  class they make a third policy. ADR 0016 merges them into the one core.
