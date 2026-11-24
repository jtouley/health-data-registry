# Automation: GitHub Actions and Hugging Face

Every crawl, refresh, check and publish runs on GitHub Actions; the data and the hosted server live on Hugging
Face. The repository is public, so standard GitHub-hosted runners cost nothing. Nothing here needs a server of
our own.

| Workflow | What it does | Schedule (UTC) | Writes to |
|---|---|---|---|
| [`refresh.yml`](../../.github/workflows/refresh.yml) | Fetches each public source in its own job; when every source passes, publishes and tags one immutable `release-<date>` | Tuesday 06:17 | Public dataset `jtouley/health-data-registry`; optional private raw archive; an issue on failure |
| [`crawl-croi.yml`](../../.github/workflows/crawl-croi.yml) | Indexes CROI abstracts at CROI's robots.txt crawl delay, in slices of ~4.7 h | 00:23, 06:23, 12:23, 18:23 daily | Three private datasets (state, metadata, text) |
| [`crawl-doi.yml`](../../.github/workflows/crawl-doi.yml) | Harvests IDWeek, SHEA, APIC and ICID abstracts by DOI, one job per conference | Wednesday 03:41 | One private dataset per conference |
| [`sitemaps.yml`](../../.github/workflows/sitemaps.yml) | Takes a weekly copy of the michigan.gov and kentcountymi.gov seed sitemaps, with the week's diff | Monday 04:43 | Private dataset `jtouley/health-data-registry-ops-sitemaps`; an issue when refused |
| [`deploy-space.yml`](../../.github/workflows/deploy-space.yml) | Syncs `space/` to the Space that hosts the MCP server | Only on a release tag `vX.Y.Z` on main that passed CI | Space `jtouley/health-data-registry-mcp` |
| [`drift-check.yml`](../../.github/workflows/drift-check.yml) | `check-contracts` against every publisher, opening or updating an issue on failure; re-enables the scheduled workflows | Friday 05:37 | Issues; workflow enabled state |

CI, CodeQL, Scorecard, release and npm-publish are unchanged and described in their own files.

Every action is pinned to a full commit SHA. Every workflow's top-level token is `contents: read`; `issues:
write` is granted only to the jobs that open an issue, `actions: write` only to the keep-alive job and
`actions: read` only to the deploy's verify job. Every checkout drops its credentials, and no workflow pushes to
git. Each source has its own concurrency group, and no group cancels a running job.

## Guarded until another pull request merges

Four workflows are on main before the code they run. Each checks for that code first and, until it is there,
ends green with a notice and does nothing. Each starts working on its next run after the other PR merges.

| Workflow | Waits for | Check |
|---|---|---|
| `crawl-croi.yml` | `index-croi` (CROI crawler, `wip/croi-index`) | `health-data-registry index-croi --help` |
| `crawl-doi.yml` | `scripts/crawl_doi.py` (DOI harvester, `feat/doi-harvest`) | the file exists |
| `sitemaps.yml` | `browser_fetch.py` (browser fetcher, `feat/michigan-tableau`) | the module imports; until then it uses plain HTTPS, which michigan.gov refuses from cloud servers, so expect the "Sitemap refresh blocked" issue |
| `deploy-space.yml` | `space/` (HTTP server, `wip/http-server`) | `space/README.md` exists at the tag |

## What each workflow does

### Weekly refresh (`refresh.yml`)

1. **plan** lists the sources whose catalog entry says `hosting: public`. That field defaults to `private`,
   and `hosting: public` is refused for a licence that doesn't allow redistribution. WHO GHO and other
   restricted sources are never fetched here; the server reads them live.
2. **fetch** runs one job per source (a matrix with `fail-fast: false`, so one broken publisher doesn't stop the
   others). Each job starts from an empty lake, fetches fresh from the publisher and uploads its lake as an
   artifact kept 7 days. Nothing is restored from the Actions cache, and `setup-uv` caching is off, so a
   release never depends on, or is poisoned by, another run's cache.
3. **raw** (optional) uploads the raw publisher bytes (`raw/<sha256>`) and fetch logs to a maintainer-only
   PRIVATE dataset named by the `HDR_RAW_DATASET` variable, which must be
   `<owner>/health-data-registry-ops-raw-archive` ([naming.md](naming.md)). They never go in the public release.
   Skipped with a notice if the variable or token is missing; fails if the variable names any other dataset.
4. **publish** (main only, full runs only) runs only when every fetch job passed. It checks that each planned
   source has a snapshot, merges the per-source lakes, runs `validate` and `check-contracts` (publishers still
   serve every contract field), stages the release locally (all pre-publish checks, including that every
   snapshot has rows and each data file has exactly the rows its snapshot's manifest records), then runs `export-public --to hf://datasets/<repo>`, which tags the hub
   commit `release-<date>`. `export-public` stops before uploading if a check fails, never moves a tag, and
   publishes a correction for the same date as `release-<date>.2`. A release is never partial: if any source
   fails, nothing is published or tagged. Without a token, the release is staged and checked and the job ends
   with a notice instead of publishing.
5. **report-failure** opens the issue "Weekly refresh failed", or comments on the open one, whenever any job
   fails, so a week with no release always has an issue.

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

**The public gate stays with the maintainer.** Every catalog entry has `hosting: public` or `hosting: private`
(the default). Only `hosting: public` sources are planned by the refresh and given data files by
`export-public`; its pre-publish check refuses data files staged for any other source. When
`croi_abstracts_index` is catalogued it stays `hosting: private` until a maintainer changes that in a reviewed
pull request. Abstract text is never public.

When a year is complete, the run says so; set `HDR_CROI_YEAR` to the next year or disable the workflow.

### DOI conference crawl (`crawl-doi.yml`)

IDWeek (Open Forum Infectious Diseases supplements), SHEA, APIC and ICID publish abstracts with DOIs, so their
metadata can come from Crossref, OpenAlex or Europe PMC without crawling the conference sites. One job per
conference (`fail-fast: false`, 30-minute timeout, its own concurrency group) runs
`uv run python scripts/crawl_doi.py <conference>`, which harvests into `$HDR_LAKE/private/<source>/` and names
the source's private dataset. The upload step accepts only the name `naming.py` gives for that source, then
uploads the `*.jsonl` files there. Nothing reaches the public release.

`scripts/crawl_doi.py` comes with the DOI harvester (`feat/doi-harvest`) and **isn't on main yet**; until it is,
each job skips with a notice.

### Sitemap snapshots (`sitemaps.yml`)

`health-data-registry sitemaps snapshot` takes a copy of each seed sitemap (`www.michigan.gov/mdhhs/sitemap.xml`,
`www.michigan.gov/emergingdiseases/sitemap.xml`, `www.kentcountymi.gov/sitemap.xml`), follows a sitemap index to
its children on the same host, and uploads in one commit, per seed, the gzipped sitemap, `latest.json` (every
page and its lastmod, with sha256 and `retrieved_at`) and the diff against last week (new, changed and removed
pages). The crawlers read that diff as their queue.

- **Where:** the private dataset `<owner>/health-data-registry-ops-sitemaps` (`naming.py`). The command creates
  it private on its first run if it is missing, and refuses to write if an existing one is public. Sitemap files
  are never written to git.
- **How:** headless Chromium (`browser_fetch.py`, Playwright) with the repo's honest user agent
  (`health-data-registry/<version> (+repo URL)`). michigan.gov's edge network (Akamai) refuses plain HTTPS from
  cloud IPs and serves the same public sitemap to a browser. robots.txt is read first, by the same browser, and
  its Disallow and Crawl-delay rules are obeyed. `--fetcher http` uses the guarded plain fetcher instead.
- **Refused:** a 401, 403 or 429, an edge challenge page, or a robots.txt that disallows the path is final. The
  seed is not taken, the others are still uploaded, the job fails, and **report-blocked** opens the issue
  "Sitemap refresh blocked" (or comments on the open one) with the reasons. The job never retries another way.
  A maintainer can download the sitemap in their own browser and run
  `health-data-registry sitemaps snapshot --from-file NAME=PATH` instead.

Until `browser_fetch.py` is on main, the job uses `--fetcher http` with a notice.

### Deploy the Space (`deploy-space.yml`)

Deploys only from a release tag (`vX.Y.Z`), never on a push to a branch and never from a pull request. The
**verify** job checks, through the GitHub API, that the tagged commit is on main and that CI passed on main for
that commit; only then does the **deploy** job, which alone gets the token, upload `space/` to the Space as one
commit. To redeploy a tag, run the workflow by hand with "Use workflow from" set to that tag; any other ref is
refused. Files in the Space that aren't in `space/` are deleted, so `space/README.md` must hold the Space's
config header. `space/` comes from the HTTP-server work (`wip/http-server`) and **isn't on main yet**; until it
is, the job skips with a notice.

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
| Job time, GitHub-hosted runner | 6 h per job | CROI slices stop at 140 requests (~4.7 h), step timeout 330 min, job timeout 360 min; DOI and sitemap jobs 30 min |
| Workflow run time | 35 days per run (the older 72 h limit no longer applies) | No run is near it |
| Concurrent jobs, free plan | 20 (5 macOS) | The refresh matrix is 5 jobs; the DOI matrix 4 |
| Matrix size | 256 jobs per run | 5 |
| Cache | 10 GB per repo; entries not accessed in 7 days are evicted; oldest first when full; branch-scoped | Not used for correctness: every run fetches fresh and crawl state is on Hugging Face |
| Artifacts and logs | Kept 90 days by default; 1 to 90 days in a public repo | Per-source lakes kept 7 days; publish checklist 30 days |
| Schedule | Delayed under high load, especially at the top of the hour; runs only on the default branch; shortest interval 5 min | Every cron is off the hour; nothing relies on exact timing |
| Inactive repos | Scheduled workflows are disabled after 60 days with no repository activity (public repos) | Weekly keep-alive job |
| Concurrency groups | One run running and one pending per group; a newer pending run cancels the older pending one | `cancel-in-progress: false` everywhere, so a running crawl or refresh is never cancelled; a skipped pending slice is picked up by the next schedule |
| `GITHUB_TOKEN` API rate | 1,000 requests per hour per repo | The issue, verify and keep-alive jobs make a handful |

Sources: GitHub Docs, "Actions limits", "Events that trigger workflows" (schedule), "Dependency caching",
"Concurrency", "Managing GitHub Actions settings for a repository" (retention).

## Secrets and variables

Set these under Settings → Secrets and variables → Actions.

**`HF_TOKEN` is the one secret** (already set). Every workflow uses it. Three optional secrets can override it
for one use each, if the maintainer later wants narrower tokens; none is needed.

| Secret | Used by | Access it needs |
|---|---|---|
| `HF_TOKEN` | Every workflow below | Write to the public dataset, the Space, and the private and ops datasets in [naming.md](naming.md) |
| `HF_PUBLISH_TOKEN` (optional) | Overrides `HF_TOKEN` in `refresh.yml` publish | Write to `jtouley/health-data-registry` |
| `HF_CRAWL_TOKEN` (optional) | Overrides `HF_TOKEN` in `crawl-croi.yml`, `crawl-doi.yml`, `sitemaps.yml` and the `refresh.yml` raw archive | Read and write to the private and ops datasets only |
| `HF_SPACE_TOKEN` (optional) | Overrides `HF_TOKEN` in `deploy-space.yml` | Write to the Space `jtouley/health-data-registry-mcp` |

| Variable | Default | Meaning |
|---|---|---|
| `HDR_PRIVATE_HF_OWNER` | `jtouley` | Owner of every repository the workflows write (`naming.py`): public dataset, Space, private and ops datasets |
| `HDR_RAW_DATASET` | unset (no archive) | PRIVATE dataset for raw bytes and fetch logs; only `<owner>/health-data-registry-ops-raw-archive` is accepted |
| `HDR_CROI_YEAR` | `2025` | CROI year to crawl |

Repository names are never set by hand: they come from `naming.py` ([naming.md](naming.md)).

Before the first CROI run, the three private CROI datasets (ops, index, text) must exist on Hugging Face; they
were created on 24 Nov 2026 with only a dataset card. That workflow doesn't create them, and it fails rather
than start over if one is missing. The sitemap dataset is the exception: `sitemaps snapshot` creates it private
at run time if it is missing. Create the Space before the first deploy, with its SDK set in `space/README.md`.

## Run a workflow by hand

From the Actions tab (Run workflow), or with the GitHub CLI:

```bash
gh workflow run refresh.yml                                  # full refresh and publish (from main)
gh workflow run refresh.yml -f source=cdc_respnet            # one source, never publishes
gh workflow run crawl-croi.yml -f year=2025 -f max_requests=10   # a short CROI slice
gh workflow run crawl-doi.yml                                # every conference
gh workflow run sitemaps.yml                                 # this week's sitemap snapshots
gh workflow run deploy-space.yml --ref v0.1.0                # redeploy a release tag; any other ref is refused
gh workflow run drift-check.yml
gh run watch                                                 # follow the run you started
```

The same steps run locally from a checkout: `make refresh`, `make check-contracts`,
`uv run health-data-registry export-public --to build/release-preview` (stages and checks without uploading),
`uv run health-data-registry sitemaps snapshot --fetcher http --seed kentcounty`, and, once merged,
`uv run health-data-registry index-croi --year 2025 --max-requests 10` and
`uv run python scripts/crawl_doi.py idweek`.

## Open risks

- `index-croi`, `scripts/crawl_doi.py`, `browser_fetch.py` and `space/` aren't merged; their workflows skip or
  fall back until they are. Check the CLI flags and step outputs still match when each merges.
- The sitemap job installs Playwright through the `browser` extra; if the Michigan PR names the extra
  differently, that step fails and the blocked issue says so.
- The keep-alive relies on the enable API resetting GitHub's inactivity timer; if GitHub changes that, push a
  commit within 60 days or re-enable the workflows by hand.
- A pending run in a concurrency group is replaced by a newer one, so a CROI slice can be skipped when the
  previous one overruns. The next schedule picks it up.
- The refresh's publish job uploads only when every source fetched; one publisher outage delays the week's
  release (the issue says so).
