# 0017. Immutable raw, reproducible builds

- **Status:** Proposed
- **Date:** 2026-11-25
- **Spec:** none; the maintainer's requirements for the data model, 25 Nov 2026
- **Related:** [ADR 0003](0003-provenance-on-every-result.md) (provenance on every result),
  [ADR 0005](0005-raw-fetches-hash-addressed.md) (raw fetches kept, hash-addressed),
  [ADR 0007](0007-odcs-contracts-and-semantic-models.md) (contracts and the contract facts rule),
  [ADR 0009](0009-licences-decide-mirroring.md) (licences decide mirroring), ADR 0015 (ELT: raw data stored as
  sent; proposed on PR #108), ADR 0016 (crawler factory; proposed on PR #118), PR #86 (transform stage b),
  the audit in [docs/dev/reproducibility-audit.md](../dev/reproducibility-audit.md)

## Context

The maintainer's requirements for the data model (25 Nov 2026):

1. Raw stays immutable.
2. Every downstream dataset (staging, intermediate, marts, metrics, exports) is idempotent and versioned.
3. Any published number can be recreated exactly from raw plus versioned code and contracts.
4. The data is trustworthy, backed by data quality.

The [audit](../dev/reproducibility-audit.md) read main, PR #86 (stage b) and stage c against them. In short:

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
with its `.meta.json`, `private/<id>/raw/<sha256>`, and each control-plane export file (stage c).

- **One writer.** Every raw write goes through one function in `store.py`, `put_raw(path, bytes, sha256)`. It
  writes to a temporary file, flushes, links it into place with `os.link` (which fails if the name exists, unlike
  `os.replace`), then marks the file read-only (0o444; 0o400 in the private area). A name that already exists is
  accepted only if its bytes hash the same; otherwise the write is refused in plain words and nothing changes.
- **Content-addressed records.** `.meta.json` records the sha256 of its `.ndjson`, and the snapshot manifest records
  both. Readers (`dbt_raw`, the replay getter below) check the hash before reading and refuse a mismatch.
- **No delete in code.** No module deletes under `raw/`, `raw_records/` or a control-plane folder. The one deletion
  the project allows is the licence purge of a private source (`private.purge`), which removes the whole source and
  records that it did so; it is never a step of a build.
- **Private partitions become loads.** `private.write_rows` rewrites `<partition>.jsonl` in place today. It becomes
  an append-only series of loads, `<partition>/<load_id>.jsonl`, each immutable, with the current view built from
  them.
- **Tests.** A rewrite with different bytes is refused; the same bytes are a no-op with the mtime unchanged; a
  tampered file is refused on read; and a static test fails if any module outside `private.purge` calls `unlink`,
  `rmtree`, `os.replace` or `write_*` on a raw path.

### 2. A build manifest pins every input

A build is one run of the transform (dbt, plus the Python staging step while both exist). Before it starts, the
runner writes `builds/<build_id>/build.json`:

| Field | What it pins |
|---|---|
| `inputs` | for each dataset: snapshot id, the sha256 of each raw file and of its `raw_records` file, and the adapter config digest (section 7) |
| `control_plane` | each export file read, with its sha256 |
| `contracts` | id, version and sha256 of each ODCS contract file, `transform/` generated files included |
| `semantic` | id, version and sha256 of each semantic model |
| `code` | the git commit, whether the tree was clean, and the sha256 of `uv.lock` |
| `engines` | Python, dbt-core, dbt-duckdb and DuckDB versions, and dbt's thread count |
| `vars` | every dbt var passed (`mmwr_first_year`, `mmwr_last_year`, and any later ones) |

`build_id` is `build-` plus the sha256 of `build.json` with keys sorted. The same inputs give the same id, whenever
and wherever the build runs. A dirty tree is refused for a build meant for publishing.

dbt never resolves `LATEST` or "the newest file" itself. The runner resolves those pointers once, writes the
resolved names into `build.json`, and the `dbt_raw` plugin reads only the files the manifest names. Stage c's
`max(filename)` and `glob` reads are replaced by the same plugin.

### 3. Outputs are keyed by build id; LATEST is only a pointer

- Every output of a build is written under `builds/<build_id>/`: the DuckDB file, one Parquet file per model, dbt's
  `run_results.json` and `manifest.json`, and `outputs.json` (each model's row count and content hash, section 5).
  A build directory is written once, in a temporary folder renamed into place, like a snapshot today.
- Every row of staging and later layers carries `_build_id` beside `_snapshot_id` and `_raw_sha256`, so any number
  names the build that made it.
- `datasets/<id>/LATEST` and a new `builds/LATEST` are pointers, swapped in one step. They are never read inside a
  build, only to choose what a build or the server reads. Moving a pointer never changes a file it points at.
- The release manifest records the `build_id` (and the build manifest's sha256) for every source, and the release
  id hashes them too. A code change with the same snapshots is a different build, so a correction gets a new tag
  instead of being taken for an identical release.
- An old build is reproduced with `health-data-registry rebuild <build_id>`: check out `code.commit`, `uv sync
  --locked`, read raw by the hashes in `build.json`, build into a fresh folder, and compare (section 5).

### 4. Determinism rules

These apply to every generated or hand-written model, and to the Python staging step:

- No `now()`, `current_date`, `current_timestamp`, `random()`, `uuid()` or `gen_random_uuid()` in a model. A time a
  model needs (an as-of date) is a dbt var recorded in `build.json`. Times we record (`exported_at`, `recorded_at`)
  stay in run logs, not in output tables or in what the content hash covers.
- No `any_value`, `first`, `last`, `arg_max` or `arg_min` unless the tie-break is total and written in the SQL
  (`arg_max(x, (year, week, x))`). Every `row_number()`, `rank()` and `qualify` orders by a unique key; a
  `string_agg` or `list` aggregate has an `order by`.
- Staging never deduplicates (ADR 0015, #81's fix round); a duplicate key fails the model's test.
- Sums of `DOUBLE` columns are not order-stable under parallel threads. Counts are integers (`BIGINT`, or
  `DECIMAL` for a published decimal), and a model that must add floats casts to `DECIMAL(38, s)` first, `s` from
  the contract. Rates and estimates are never summed (hard rule 2).
- Output Parquet is written with an explicit `ORDER BY` on the model's key, so the same rows give the same file
  for one DuckDB version. Hashes compare content, not file bytes (section 5), so a DuckDB upgrade does not count as
  a change in the data.
- A generated-SQL lint (`transform_gen.py`, and a test over `transform/models/**/*.sql`) refuses the functions
  above and a window without a total order.

### 5. The reproducibility test

The content hash of a model is the sha256 of its rows in canonical form: columns in contract order, rows sorted by
every column, each value written by a fixed text rule (integers as digits, decimals at their declared scale, doubles
as their shortest round-trip text, NULL as `\N`, timestamps in UTC ISO-8601). `outputs.json` holds one hash per
model and one for the whole build.

- **CI, on fixtures (every PR).** On the synthetic fixture lake in `tests/fixtures/transform`: build, record the
  hashes, delete `builds/` and every derived snapshot file (everything but raw), rebuild from raw alone, and require
  every hash to match. A second check builds twice with `threads: 1` and `threads: 8` and requires the same hashes.
  A model whose hash changes in a PR is listed in the job summary; a change the PR didn't mean to make is a failure
  for review to catch, and the column-level impact check from using-dbt-for-analytics-engineering applies.
- **Nightly, on the real lake.** A scheduled job takes the last served build, runs `rebuild <build_id>` from the
  private raw archive (`HDR_RAW_DATASET`), and compares every hash. A mismatch opens one issue, like the refresh's
  failure issue, and the next release is held until it is closed.
- **Replay instead of refetch.** Rebuilding a snapshot from raw needs no network: a replay getter serves each
  `source_url` in the manifest from `raw/<sha256>`, in the recorded order, and the adapter parses it as it did when
  fetched. A raw file that is missing or fails its hash stops the rebuild in plain words.

### 6. Data-quality gates

A build becomes `LATEST`, and a release is published, only when its gates pass. Each gate's result is written into
`outputs.json` and the release manifest's `quality` section, and every warning travels with the answers.

| Gate | Blocks LATEST | Blocks publish | Source of the rule |
|---|---|---|---|
| Contract quality rules, severity error, or a rule that cannot run | yes | yes | the contract (hard rule 1); stage b's `QualityReport.blocking` |
| `has_rows` and row count equal to the snapshot manifest | yes | yes | structural fact |
| Fill rate (warn) | no; caveat | no; caveat | generated per column; an all-empty required column is an error |
| Freshness: newest period older than the catalog's refresh cadence allows | no; caveat | yes, unless a maintainer marks the source paused | catalog cadence (SodaCL-style freshness check) |
| Reconciliation: parts sum to their published total, where the contract declares the total row and its parts | yes | yes | the publisher's own totals, declared in the contract; never inferred |
| Non-additive guard: no model sums a rate, an estimate, or a dimension the semantic model marks non-additive | yes (test on generated SQL) | yes | semantic model (hard rule 2) |
| Reproducibility (section 5) | yes on fixtures; nightly holds the next release | yes | this ADR |
| Partial snapshot | yes | yes | `--max-rows` |
| Draft contract | no; caveat | no; caveat on the card | hard rule 3 |

A blocked build stays on disk under its build id with its results, so a maintainer can see why. `LATEST` keeps
pointing at the last build that passed. Review queues (the `flag_*` models) never block: they list rows for a
maintainer, and change nothing.

### 7. How this fits ELT (ADR 0015) and the crawler factory (ADR 0016)

- **ELT.** ADR 0015 says what raw holds: the publisher's values as sent, metadata added only as declared. This ADR
  says how raw is kept: written once, by one writer, checked by hash, never rewritten. A change to what raw holds is
  a contract version bump and a new snapshot under both.
- **Crawler factory.** Under ADR 0016 a source's adapter type, its config block and its extensions decide how
  bytes become records. They are part of the snapshot's identity: the snapshot id's digest covers the raw sha256s,
  the contract and semantic versions, `partial`, the adapter type and version, and the sha256 of the source's
  canonical config (extensions and their arguments included). A config change gives a new snapshot, never a
  rewritten one. The build manifest records the same digest per input. PR #118's text does not say this yet; its
  fix round should add it, or this ADR's acceptance carries it.
- **Extensions.** ADR 0016's extensions are pure (no clock, network, file system or environment), which is what
  section 4 needs; a `staging` extension compiles into dbt and falls under the same determinism lint.

## Consequences

- Any published number names its build, and its build names every byte, file and version it read. "Rebuild build
  X" is one command, and CI shows it still works on every PR.
- `LATEST` changes what is served, never what was built.
- Raw writes are slower by one hash check and a `chmod`; raw files can't be edited by hand, which is the point. A
  maintainer who must remove raw data for licence reasons uses `purge`, which is logged.
- Builds take disk space per build id. Old builds may be pruned only when no release names them; that pruning is a
  later decision and never touches raw.
- Stage b and stage c change before merge where the [audit](../dev/reproducibility-audit.md) marks a gap
  BLOCKING; the rest are follow-up PRs on the usual cycle.

## Open questions

1. Is the Python staging step (`store.build_snapshot`) retired once dbt builds the published tables, or kept and
   checked against dbt staging by hash? Two paths to one table is one more thing to keep identical.
2. Which sources declare reconciliation pairs first? Only the contract can say a row is the total of others.
3. How long are build directories kept on the maintainer's lake and in the private archive?
