# Automation: GitHub Actions and Hugging Face

Every crawl, refresh, check and publish runs on GitHub Actions; the data and the hosted server live on Hugging
Face. The repository is public, so standard GitHub-hosted runners cost nothing. Nothing here needs a server of
our own.

| Workflow | What it does | Schedule (UTC) | Writes to |
|---|---|---|---|
| [`refresh.yml`](../../.github/workflows/refresh.yml) | Fetches each public source in its own job, then publishes one immutable `release-<date>` | Tuesday 06:17 | Public dataset `jtouley/health-data-registry`; optional private raw archive |
| [`crawl-croi.yml`](../../.github/workflows/crawl-croi.yml) | Indexes CROI abstracts at CROI's robots.txt crawl delay, in slices of ~4.7 h | 00:23, 06:23, 12:23, 18:23 daily | Three private datasets (state, metadata, text) |
| [`crawl-doi.yml`](../../.github/workflows/crawl-doi.yml) | Stub for IDWeek, SHEA, APIC and ICID via Crossref, OpenAlex or Europe PMC; fails with "not built yet" | Manual only until ledger task 16a lands | Nothing |
| [`deploy-space.yml`](../../.github/workflows/deploy-space.yml) | Syncs `space/` to the Space that hosts the MCP server | On push to main touching `space/`; manual | Space `jtouley/health-data-registry-mcp` |
| [`drift-check.yml`](../../.github/workflows/drift-check.yml) | `check-contracts` against every publisher, opening or updating an issue on failure; re-enables the scheduled workflows | Friday 05:37 | Issues; workflow enabled state |

CI, CodeQL, Scorecard, release and npm-publish are unchanged and described in their own files.

## What each workflow does

### Weekly refresh (`refresh.yml`)

1. **plan** lists the public sources from the catalog: licence allows redistribution and not a private-tier
   source. WHO GHO and other restricted sources are never fetched here; the server reads them live.
2. **fetch** runs one job per source (a matrix with `fail-fast: false`, so one broken publisher doesn't stop the
   others). Each job starts from an empty lake, fetches fresh from the publisher and uploads its lake as an
   artifact kept 7 days. Nothing is restored from the Actions cache, and `setup-uv` caching is off, so a
   release never depends on, or is poisoned by, another run's cache.
3. **raw** (optional) uploads the raw publisher bytes (`raw/<sha256>`) and fetch logs to a maintainer-only
   PRIVATE dataset named by the `HDR_RAW_DATASET` variable, which must be
   `<owner>/health-data-registry-ops-raw-archive` ([naming.md](naming.md)). They never go in the public release.
   Skipped with a notice if the variable or token is missing; fails if the variable names any other dataset.
4. **publish** (main only, full runs only) merges the per-source lakes, runs `validate` and `check-contracts`,
   stages the release locally (all pre-publish checks), then runs `export-public --to hf://datasets/<repo>`.
   `export-public` stops before uploading if a check fails, never moves a tag, and publishes a correction for the
   same date as `release-<date>.2`. If a source failed to fetch, publish doesn't run: a release is never partial.
   Without a token, the release is staged and checked and the job ends with a notice instead of publishing.
5. **report-failure** opens the issue "Weekly refresh failed", or comments on the open one.

A manual run can name one source (`source` input) to test a fetch; such a run never publishes.

### CROI crawl (`crawl-croi.yml`)

CROI's robots.txt asks for 120 s between requests, so a conference year (about 1,400 requests) takes about
47 hours. The crawl runs in slices: each run is one job that sends at most `max_requests` requests (default
140, about 4.7 h), under a 330-minute step timeout and a 360-minute job timeout. Four slices a day finish a year
in about two days.

Each run:

1. Checks that `index-croi` exists. It is on the `wip/croi-index` branch and **not merged yet**; until it is,
   every run skips with a notice. It also skips if no token is set, since the state can't be kept.
2. Restores the state from private Hugging Face datasets (never the Actions cache, which evicts entries unused
   for 7 days). If a dataset is missing or unreadable, the run fails and crawls nothing, so a fresh checkpoint
   can never overwrite the real one.
3. Runs `health-data-registry index-croi --year <year> --max-requests <n>`. It reads robots.txt first on every
   run and obeys its Crawl-delay and Disallow rules; it writes a checkpoint after every request.
4. Uploads the state, even if the slice failed or hit its step timeout.

The names come from `naming.py` ([naming.md](naming.md)); `<owner>` is `HDR_PRIVATE_HF_OWNER` (default `jtouley`).

| Private dataset | Holds |
|---|---|
| `<owner>/health-data-registry-ops-croi-crawl` | `crawl_state/` (checkpoint and last-request time), `fetch_log/`, metadata rows so far |
| `<owner>/health-data-registry-private-croi-abstracts-index` | Metadata rows, read by the hosted server (`private.py`) |
| `<owner>/health-data-registry-private-croi-abstracts-text` | Abstract text. Private tier only; never in a public release |

**The public gate stays with the maintainer.** `catalog/croi_abstracts_index.yaml` says `hosting: private`.
Nothing from the crawl reaches the public release until a maintainer changes that in a reviewed pull request;
`export-public` refuses private-tier data and checks the staged release for private text. Abstract text is
never public.

When a year is complete, the run says so; set `HDR_CROI_YEAR` to the next year or disable the workflow.

### DOI conference crawl (`crawl-doi.yml`)

IDWeek (Open Forum Infectious Diseases supplements), SHEA, APIC and ICID publish abstracts with DOIs, so their
metadata can come from Crossref, OpenAlex or Europe PMC without crawling the conference sites. The harvester
isn't built: the workflow is a stub that fails with "not built yet". TODO: ledger task 16a. When it lands,
replace the stub step, keep the outputs in the private tier as the CROI crawl does, and turn on the commented
schedule.

### Deploy the Space (`deploy-space.yml`)

On a push to main that changes `space/`, or a manual run, uploads `space/` to the Space as one commit. Files in
the Space that aren't in `space/` are deleted, so `space/README.md` must hold the Space's config header.
`space/` comes from the HTTP-server work (`wip/http-server`) and **isn't on main yet**; until it is, the job skips
with a notice. It also skips with a notice when no token is set.

### Drift check and keep-alive (`drift-check.yml`)

`check-contracts` asks each publisher whether it still serves every field its contract reads. On failure on
main, the job opens the issue "Contract drift: a publisher changed", or comments on the open one. Only that job
has `issues: write`.

The keep-alive job re-enables each scheduled workflow through the API every week (`actions: write`, that job
only). GitHub disables scheduled workflows in a public repository after 60 days with no repository activity.
A workflow the maintainer disabled by hand is left alone. If the drift-check workflow itself has been disabled,
re-enable it once in the Actions tab.

## Free-tier limits (checked against GitHub's docs, 24 Nov 2026)

| Limit | Value | How the workflows fit |
|---|---|---|
| Job time, GitHub-hosted runner | 6 h per job | CROI slices stop at 140 requests (~4.7 h), step timeout 330 min, job timeout 360 min |
| Workflow run time | 35 days per run (the older 72 h limit no longer applies) | No run is near it |
| Concurrent jobs, free plan | 20 (5 macOS) | The refresh matrix is 5 jobs |
| Matrix size | 256 jobs per run | 5 |
| Cache | 10 GB per repo; entries not accessed in 7 days are evicted; oldest first when full; branch-scoped | Not used for correctness: every run fetches fresh and crawl state is on Hugging Face |
| Artifacts and logs | Kept 90 days by default; 1 to 90 days in a public repo | Per-source lakes kept 7 days; publish checklist 30 days |
| Schedule | Delayed under high load, especially at the top of the hour; runs only on the default branch; shortest interval 5 min | Every cron is off the hour; nothing relies on exact timing |
| Inactive repos | Scheduled workflows are disabled after 60 days with no repository activity (public repos) | Weekly keep-alive job |
| Concurrency groups | One run running and one pending per group; a newer pending run cancels the older pending one | `cancel-in-progress: false` everywhere, so a running crawl or refresh is never cancelled; a skipped pending slice is picked up by the next schedule |
| `GITHUB_TOKEN` API rate | 1,000 requests per hour per repo | The issue and keep-alive jobs make a handful |

Sources: GitHub Docs, "Actions limits", "Events that trigger workflows" (schedule), "Dependency caching",
"Concurrency", "Managing GitHub Actions settings for a repository" (retention).

## Secrets and variables

Set these under Settings → Secrets and variables → Actions. Every workflow runs without them and skips its
Hugging Face step with a notice.

| Secret | Used by | Access it needs |
|---|---|---|
| `HF_PUBLISH_TOKEN` | `refresh.yml` publish | Write to `jtouley/health-data-registry` |
| `HF_CRAWL_TOKEN` | `crawl-croi.yml`; `refresh.yml` raw archive | Read and write to the private and ops datasets listed in [naming.md](naming.md), and no others |
| `HF_SPACE_TOKEN` | `deploy-space.yml` | Write to the Space `jtouley/health-data-registry-mcp` |
| `HF_TOKEN` | Any of the above, as a fallback | All of the above |

Recommended: three fine-grained Hugging Face tokens, each limited to its repositories, so a leaked crawl token
can't change the public release or the Space. One `HF_TOKEN` with write access to all of them also works.

| Variable | Default | Meaning |
|---|---|---|
| `HDR_HUB_DATASET` | `jtouley/health-data-registry` | Public dataset that `export-public` publishes to |
| `HDR_RAW_DATASET` | unset (no archive) | PRIVATE dataset for raw bytes and fetch logs; only `jtouley/health-data-registry-ops-raw-archive` is accepted |
| `HDR_PRIVATE_HF_OWNER` | `jtouley` | Owner of the `health-data-registry-private-<source-id>` and `-ops-<job>` datasets (`naming.py`) |
| `HDR_CROI_YEAR` | `2025` | CROI year to crawl |
| `HDR_SPACE` | `jtouley/health-data-registry-mcp` | Space to deploy |

Before the first CROI run, the three private datasets (ops, index, text) must exist on Hugging Face; they were
created on 24 Nov 2026 with only a dataset card. The workflow doesn't create them, and it fails rather than start over if one is missing. Create the Space before the
first deploy, with its SDK set in `space/README.md`.

## Run a workflow by hand

From the Actions tab (Run workflow), or with the GitHub CLI:

```bash
gh workflow run refresh.yml                                  # full refresh and publish (from main)
gh workflow run refresh.yml -f source=cdc_respnet            # one source, never publishes
gh workflow run crawl-croi.yml -f year=2025 -f max_requests=10   # a short CROI slice
gh workflow run crawl-doi.yml                                # fails: not built yet (ledger task 16a)
gh workflow run deploy-space.yml
gh workflow run drift-check.yml
gh run watch                                                 # follow the run you started
```

The same steps run locally from a checkout: `make refresh`, `make check-contracts`,
`uv run health-data-registry export-public --to build/release-preview` (stages and checks without uploading),
and, once merged, `uv run health-data-registry index-croi --year 2025 --max-requests 10`.

## Open risks

- `index-croi` and `space/` aren't merged; their workflows skip until they are. Check the CLI flags still match
  when `wip/croi-index` merges.
- The keep-alive relies on the enable API resetting GitHub's inactivity timer; if GitHub changes that, push a
  commit within 60 days or re-enable the workflows by hand.
- A pending run in a concurrency group is replaced by a newer one, so a CROI slice can be skipped when the
  previous one overruns. The next schedule picks it up.
- The refresh's publish job uploads only when every source fetched; one publisher outage delays the week's
  release (the issue says so).
