# 0017. Immutable raw, reproducible builds

- **Status:** Proposed
- **Date:** 2026-11-25
- **Spec:** none; the maintainer's requirements for the data model, 25 Nov 2026
- **Related:** [ADR 0003](0003-provenance-on-every-result.md) (provenance on every result),
  [ADR 0005](0005-raw-fetches-hash-addressed.md) (raw fetches kept, hash-addressed),
  [ADR 0007](0007-odcs-contracts-and-semantic-models.md) (contracts and the contract facts rule),
  [ADR 0009](0009-licences-decide-mirroring.md) (licences decide mirroring),
  [ADR 0015](0015-elt-raw-data-stored-as-sent.md) (ELT: raw data stored as sent), [ADR 0016](0016-crawler-factory.md)
  (crawler factory), stage b and stage c of the transform (named in the audit's ref table),
  the audit in [docs/dev/reproducibility-audit.md](../dev/reproducibility-audit.md)

"Hard rule N" below means rule N in [CLAUDE.md, Hard rules](../../CLAUDE.md#hard-rules).

## Context

The maintainer's requirements for the data model (25 Nov 2026):

1. Raw stays immutable.
2. Every downstream dataset (staging, intermediate, marts, metrics, exports) is idempotent and versioned.
3. Any published number can be recreated exactly from raw plus versioned code and contracts.
4. The data is trustworthy, backed by data quality.

The [audit](../dev/reproducibility-audit.md) read main, stage b (`pre-pr/contract-silver-b` at `99e014f`, the
pre-PR branch for PR #86) and stage c against them. In short:

- Raw is written once by convention (`store.write_raw`, `keep_raw_records`), but nothing refuses a rewrite or a
  delete, nothing checks stored bytes against their name on read, and no test covers either.
- dbt reads whatever `LATEST` names when it runs (`dbt_raw.load_sql`); stage c reads the newest file in a folder
  instead. No record pins what a build read, so two builds at different times can differ with nothing to show why.
- The gate's build id is a hash of dbt's invocation id and clock time (stage b `release_gate.read_build`): it names
  a run, not its inputs. dbt's tables live in a temporary DuckDB file that is thrown away. No old build can be
  rebuilt.
- Nothing rebuilds from raw and compares. The snapshot id leaves out the code that made the snapshot.
- Contract quality rules run on every snapshot (stage b), but a failed error-severity rule blocks neither `LATEST`
  nor publishing. The dbt gate blocks a weekly upload, but it is not tied to the files uploaded.

## Decision

### 1. Raw is immutable, enforced in code and tests

Raw means everything a build reads that we did not compute: `raw/<sha256>`, `raw_records/<id>/<snapshot_id>.ndjson`
with its `.meta.json`, `private/<id>/raw/<sha256>`, and each control-plane export file (stage c). The control plane
is the set of records only a maintainer decides, such as the condition crosswalk: `crosswalk.py` proposes records,
a maintainer accepts or rejects them, and each change is exported as a new complete file under
`$HDR_CONTROL_PLANE`.

- **One writer.** Every raw write goes through one function in `store.py`, `put_raw(path, bytes, sha256)`. It
  writes to a temporary file in the same folder, flushes and fsyncs it, marks it read-only (0o444; 0o400 in the
  private area), links it into place with `os.link` (which fails if the name exists, unlike `os.replace`), removes
  the temporary name and fsyncs the folder. The final name therefore never exists writable, and a crash never
  leaves a name without its bytes. A name that already exists is accepted only if its bytes hash the same.
  Otherwise nothing is written and the refusal says what failed and what to do, for example: "Raw file
  `raw/<sha256>` already holds different bytes; nothing was written. Check the fetch log for this source; if the
  stored file was edited, restore it from the private raw archive."
- **Streamed fetches use the same writer.** `crawl.refresh_dataset` writes each page as it arrives and later hands
  `build_snapshot` a fetch with empty content. Both steps go through `put_raw`; when the content is empty because
  the bytes were already streamed, `put_raw` re-hashes the stored file and refuses a mismatch, instead of skipping
  the check.
- **Content-addressed records.** `.meta.json` records the sha256 of its `.ndjson`, and the snapshot manifest records
  both. Readers (`dbt_raw`, the replay getter below) check the hash before reading and refuse a mismatch, naming
  the file and the snapshot and pointing to the private raw archive.
- **No delete in code.** No module deletes under `raw/`, `raw_records/` or a control-plane folder. The one deletion
  the project allows is the licence purge of a private source (`private.purge`), which removes the whole source and
  records that it did so; it is never a step of a build.
- **Private partitions become loads.** `private.write_rows` rewrites `<partition>.jsonl` in place today. It becomes
  an append-only series of loads, `<partition>/<load_id>.jsonl`, each immutable. A per-partition manifest gives each
  load a monotonic sequence number, its sha256 and its mode (upsert or replace). The current view applies the loads
  in sequence order, with a total tie-break on the key, never by file name or mtime. `build.json` pins the load ids
  a build read.
- **Tests.** A rewrite with different bytes is refused; the same bytes are a no-op with the mtime unchanged; a
  tampered file is refused on read; a streamed file whose stored bytes no longer match is refused. A static test
  fails if any module other than `store.put_raw` and `private.purge` calls `unlink`, `rmtree`, `os.replace` or
  `write_*` on a final raw name (`put_raw`'s own temporary file is exempt).

### 2. A build manifest pins every input

A build is one run of the transform (dbt, plus the Python staging step while both exist). Before it starts, the
runner writes `builds/<build_id>/build.json`. Its `pinned` part decides the build id:

| Field | What it pins |
|---|---|
| `inputs` | for each dataset: snapshot id, the sha256 of each raw file and of its `raw_records` file, and the adapter config digest (section 7); for a private source, the load ids read |
| `control_plane` | each export file read, with its sha256 |
| `contracts` | id, version and sha256 of each ODCS contract file, `transform/` generated files included |
| `semantic` | id, version and sha256 of each semantic model |
| `code` | the git commit, the sha256 of `uv.lock`, and for a dirty tree the sha256 of the uncommitted changes (the diff of tracked files plus the bytes of untracked, not-ignored files) |
| `engines` | Python, dbt-core, dbt-duckdb and DuckDB versions |
| `vars` | every dbt var passed (`mmwr_first_year`, `mmwr_last_year`, and any later ones) |

`build.json` also has a `run` part that the build id does not cover: dbt's thread count, the start time and the
host. Section 5's thread test proves the output doesn't depend on them, so they are run metadata, not inputs.

`build_id` is `build-` plus the sha256 of the `pinned` part with keys sorted. The same inputs give the same id,
whenever and wherever the build runs. Two dirty-tree builds with different uncommitted changes get different ids.
A dirty tree is refused for a build meant for publishing.

A snapshot id starts with its newest `retrieved_at`, by design: retrieval time is provenance (hard rule 8).
Refetching identical bytes later therefore gives a new snapshot id and a new build id, while the content hashes
(section 5) stay equal. Replaying stored raw (section 5) keeps the original id.

dbt never resolves `LATEST` or "the newest file" itself. The runner resolves those pointers once, writes the
resolved names into `build.json`, and the `dbt_raw` plugin reads only the files the manifest names. Stage c's
`max(filename)` and `glob` reads are replaced by the same plugin.

### 3. Outputs are keyed by build id; `LATEST` is only a pointer

- Every output of a build is written under `builds/<build_id>/`: the DuckDB file, one Parquet file per model, dbt's
  `run_results.json` and `manifest.json`, and `outputs.json` (each model's row count and content hash, and each
  gate result, sections 5 and 6). A build directory is written once, in a temporary folder renamed into place, like
  a snapshot today.
- **Reruns are no-ops.** If `builds/<build_id>/outputs.json` already exists, the build does nothing and returns
  that result, a blocked one included. A run that must check the stored outputs builds into a temporary folder and
  compares content hashes; it never replaces the directory.
- The build id is not a column. Every row of staging and later layers keeps `_snapshot_id` and `_raw_sha256`, which
  vary row by row once marts union sources. The build id, which is the same for every row of a build, is recorded
  once: in `outputs.json`, in each Parquet file's key-value metadata, and in the release manifest. Provenance
  (hard rule 8) reads it from the manifest, so there is one place it can be wrong.
- `datasets/<id>/LATEST` and a new `builds/LATEST` are pointers, swapped in one step. They are never read inside a
  build, only to choose what a build or the server reads. Moving a pointer never changes a file it points at.
- **What is served.** `export-public` publishes the Parquet files from `builds/<build_id>/`, so the build the
  release names is the build that produced the uploaded bytes. Until that lands (stage b still publishes the
  snapshot's `data.parquet`), the release manifest records the snapshot files and the build id separately, and
  `export-public` refuses when the build did not read exactly those snapshots.
- The release manifest records the `build_id` and the build manifest's sha256 for every source, as provenance. The
  release id hashes the build's content hashes from `outputs.json`, not the build id. A change to code or to a
  dependency that changes no row makes no new release; a correction that changes rows gets a new tag.
- An old build is reproduced with `health-data-registry rebuild <build_id>`: check out `code.commit`, `uv sync
  --locked`, read raw by the hashes in `build.json`, build into a fresh folder, and compare (section 5).

### 4. Determinism rules

These apply to every generated or hand-written model, and to the Python staging step:

- No `now()`, `current_date`, `current_timestamp`, `random()`, `uuid()` or `gen_random_uuid()` in a model. No dbt
  run-time context used as a value: `run_started_at`, `invocation_id`, `modules.datetime`, or `this` in a value.
  No `env_var()` in model or source SQL; what a build reads comes from `build.json` through the plugin. A time a
  model needs (an as-of date) is a dbt var recorded in `build.json`. Times we record (`exported_at`, `recorded_at`)
  stay in run logs, not in output tables or in what the content hash covers.
- No `any_value`, `first`, `last`, `arg_max` or `arg_min` unless the tie-break is total and written in the SQL
  (`arg_max(x, (year, week, x))`). A `string_agg` or `list` aggregate has an `order by`.
- Every `row_number()`, `rank()` and `qualify` orders by a unique key. Because the SQL text can't show that a key is
  unique, a window passes only when its `partition by` and `order by` columns match a uniqueness test declared on
  the model it reads: dbt's built-in `unique` for one column, or for a composite key an `odcs_check` rule (the
  generic test in `transform/macros/odcs_check.sql`) that the generator emits from the contract's primary key,
  counting duplicate keys (the project has no `dbt_utils`).
- Staging never deduplicates: a row the publisher sent twice is kept twice, and a duplicate key fails the model's
  test (the rule #81's fix round set for staging).
- Any aggregate or window aggregate over a `DOUBLE` column (`sum`, `avg`, `var_*`, `stddev_*`, `corr`, `regr_*`,
  running totals, moving averages) depends on order under parallel threads, so it casts to `DECIMAL(38, s)` first,
  `s` from the contract. Counts are integers (`BIGINT`, or `DECIMAL` for a published decimal). Rates and estimates
  are never summed (hard rule 2).
- Output Parquet is written with an explicit `ORDER BY` on the model's key, so the same rows give the same file
  for one DuckDB version. That order holds only while DuckDB's `preserve_insertion_order` is on (its default): the
  writer never turns it off, and a test checks the setting. Hashes compare content, not file bytes (section 5), so a
  DuckDB upgrade does not count as a change in the data.
- A lint (`transform_gen.py`, and a test over `transform/models/**/*.sql`) refuses all of the above. It reads the
  raw SQL, not only the compiled SQL, because the dbt context and `env_var()` are Jinja and disappear on compile.

### 5. The reproducibility test

The content hash of a model is the sha256 of its rows in canonical form: columns sorted by name (so a model without
a contract has a defined order too), rows sorted by every column, each value written by a fixed text rule (integers
as digits, decimals at their declared scale, doubles as their shortest round-trip text, NULL as `\N`, timestamps in
UTC ISO-8601). It covers the data columns and `_snapshot_id` and `_raw_sha256`. It leaves out anything that names a
run rather than describes the data. `outputs.json` holds one hash per model and one for the whole build.

- **CI, on fixtures (every PR).** On the synthetic fixture lake in `tests/fixtures/transform`: build, record the
  hashes, delete `builds/` and every derived snapshot file (everything but raw), rebuild from raw alone, and require
  every hash to match. A second check builds with `threads: 1` and with `threads: 8`; thread count is in the `run`
  part of `build.json`, so both get the same build id, and the check requires the same hashes.
- **What changed, and what it affects.** The job summary lists each model whose hash changed, and each model the
  change can affect: CI runs `dbt ls --select state:modified+ --state builds/<last_served_build_id>/` against the
  kept `manifest.json`, then searches the downstream SQL for any column the PR changed or removed (the column-level
  impact check from using-dbt-for-analytics-engineering). A model whose hash didn't move on the fixtures can still
  break on real data, so review reads that list, not only the changed hashes.
- **Nightly, on the real lake.** A scheduled job takes the last served build, runs `rebuild <build_id>` from the
  private raw archive (`HDR_RAW_DATASET`), and compares every hash. A mismatch opens one issue, like the refresh's
  failure issue, and the next release is held until it is closed.
- **Replay instead of refetch.** Rebuilding a snapshot from raw needs no network. A replay getter serves each
  `source_url` in the manifest from `raw/<sha256>`, in the manifest's `raw_sha256` order, and restores each fetch's
  own `retrieved_at` from the fetch log (the manifest keeps only the newest). The adapter parses the bytes as it did
  when fetched, and the replayed snapshot gets the same snapshot id; a test checks that. A raw file that is missing
  or fails its hash stops the rebuild, naming the file and the snapshot that needs it, and pointing to the private
  raw archive to restore it from.

### 6. Data-quality gates

A build becomes `LATEST`, and a release is published, only when its gates pass. `outputs.json` and the release
manifest's `quality` section record each gate result under its rule id, never by position: the Python checks and the
generated dbt tests can return results in a different order.

Two kinds of result are kept apart everywhere they are reported, as stage b's `quality.py` already does: a **data
failure** ("data failed check X: observed v, bound b") and a **rule error** ("check X could not run: the contract
no longer matches the data; a maintainer's job"). Both block. Only data warnings travel with the answers as
caveats; a rule error never becomes a caveat about the numbers.

| Gate | Blocks `LATEST` | Blocks publish | Source of the rule |
|---|---|---|---|
| Contract quality rules, severity error: data failure | yes | yes | the contract (hard rule 1) |
| Contract quality rules: a rule that cannot run (rule error) | yes | yes | stage b's `QualityReport.blocking` |
| An error-severity value rule over a column whose fill-rate check reports it all empty | no; reported as not evaluated, never as a pass | no; the card says not evaluated | the same rule plus the fill-rate check |
| `has_rows`; for staging models (1:1 with the snapshot), row count equal to the snapshot manifest's | yes | yes | structural fact |
| Declared key unique and not null, per model | yes | yes | the contract's key columns, generated for every model |
| Fill rate (warn) | no; caveat | no; caveat | generated for columns that are not required, not flag columns and not `mayBeEmpty`; required columns are enforced at load |
| Freshness: newest period older than the source's declared maximum lag | no; caveat | yes, unless a maintainer marks the source paused | a reviewed catalog field, `freshness: {max_lag_days, basis}` with a codebook link; a source without it has no freshness gate |
| Reconciliation: parts against their published total | if staging differs from raw: yes. If the publisher's own figures don't reconcile: no; caveat and a maintainer queue | the same | the contract declares the total row, its parts, the comparison (`=` or `<=`) and the tolerance, each from the codebook; never inferred |
| Non-additive guard: no model sums a rate, an estimate, or a dimension the semantic model marks non-additive | yes (test on generated SQL) | yes | semantic model (hard rule 2) |
| Reproducibility (section 5) | no (CI fails the PR on fixtures; a nightly mismatch holds the next release) | yes | this ADR |
| Partial snapshot | no; caveat | yes | `--max-rows` |
| Draft contract | no; caveat | no; caveat on the card | hard rule 3 |

Reconciliation reports **not evaluated**, with the reason, when any part is suppressed or flagged (for NNDSS, U, N,
NP or NC) or the contract declares no tolerance. A publisher's own release that doesn't add up is a fact about the
source: it gets a caveat and a maintainer's look, and it is not withheld. A difference our staging introduced is
our bug, and it blocks.

A partial snapshot can still become `LATEST` on a development lake, as today: `refresh <id> --max-rows N` then
`serve` keeps working. The server adds a caveat to every answer from it saying the snapshot is a sample (it has no
such caveat today). A partial snapshot can never be published.

A blocked build stays on disk under its build id with its results, so a maintainer can see why. `LATEST` keeps
pointing at the last build that passed. Review queues (`flag_*` models) never block: they list rows for a maintainer,
and change nothing.

### 7. How this fits ELT (ADR 0015) and the crawler factory (ADR 0016)

- **ELT.** [ADR 0015](0015-elt-raw-data-stored-as-sent.md) says what raw holds: the publisher's values as sent,
  metadata added only as declared. This ADR says how raw is kept: written once, by one writer, checked by hash,
  never rewritten. A change to what raw holds is a contract version bump and a new snapshot under both.
- **Crawler factory.** Under [ADR 0016](0016-crawler-factory.md) a source's adapter type, its config block and its extensions decide
  how bytes become records. They are part of the snapshot's identity: the snapshot id's digest covers the raw
  sha256s, the contract and semantic versions, `partial`, the adapter type and version, and the sha256 of the
  source's canonical config (extensions and their arguments included). A config change gives a new snapshot, never
  a rewritten one. The build manifest records the same digest per input. ADR 0016 already adds the config and
  extension digest to the snapshot inputs; this ADR adds the adapter type and version.
- **Extensions.** ADR 0016's extensions are pure (no clock, network, file system or environment), which is what
  section 4 needs; a `staging` extension compiles into dbt and falls under the same determinism lint.

### 8. One staging path

The Python staging step (`store.build_snapshot`) stays only until dbt staging matches it by content hash on the
fixtures and on one real refresh. Then it is retired and dbt is the one definition of each staging table. Two
definitions of one table are two places every fix has to land. The published files then come from the build
(section 3), which also closes the audit's gap 2.

## Consequences

- Any published number names its build, and its build names every byte, file and version it read. "Rebuild build
  X" is one command. CI shows it still works on the fixture lake on every PR, and the nightly job checks the real
  lake.
- `LATEST` changes what is served, never what was built.
- Raw writes are slower by one hash check and a `chmod`; raw files can't be edited by hand, which is the point. A
  maintainer who must remove raw data for licence reasons uses `purge`, which is logged.
- Builds take disk space per build id. Old builds may be pruned only when no release names them; that pruning is a
  later decision and never touches raw.
- Contracts don't change for this ADR: the build id is metadata, not a column.
- The development workflow stays: a partial snapshot still becomes `LATEST` locally and is never published.
- Stage b and stage c change before merge where the [audit](../dev/reproducibility-audit.md) marks a gap as
  blocking them; the rest are follow-up PRs on the usual cycle.
- The PR that implements this ADR also updates the developer docs it changes: `docs/architecture.md` (the
  `store.py` row and the step where dbt reads the load `LATEST` names), and CLAUDE.md's architecture map (the lake
  layout gains `builds/`) and command table (`rebuild`).

## Open questions

1. Which sources declare reconciliation pairs and freshness bounds first? Only the contract can say a row is the
   total of others, and only a reviewed catalog field can say how late a source may be.
2. How long are build directories kept on the maintainer's lake and in the private archive?
