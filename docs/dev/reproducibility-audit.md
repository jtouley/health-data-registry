# Reproducibility audit

An audit of the data model against the maintainer's four requirements (25 Nov 2026): raw stays immutable; every
downstream dataset is idempotent and versioned; any published number can be recreated exactly from raw plus
versioned code and contracts; the data is trustworthy, backed by data quality. The decision that follows from it is
[ADR 0017](../adr/0017-immutable-raw-reproducible-builds.md) (Proposed).

What was read:

| Name | Ref | What it adds |
|---|---|---|
| main | `578e5df` | `store.py`, `staging.py`, `dbt_raw.py`, `transform/`, `publish.py`, `hub.py`, `private.py` |
| stage b | `pre-pr/contract-silver-b` at `99e014f`, the pre-PR branch for PR #86 | quality rules on snapshots, intermediate models and review queues (`flag_*` models), release gate, load metadata |
| stage c | `pre-pr/contract-silver-c` at `8f2dd07` | conformed keys, marts, control-plane crosswalk, Census FIPS sources |

PR #86's head (`feat/contract-silver-b`, `cc32cce`) is older than stage b: it still has `max(filename)` staging.
Stage c branches from PR #86's head, not from stage b, so it does not have #81's `LATEST`-driven staging either.
This audit uses only those two names: "stage b" for `99e014f` and "PR #86's head" for `cc32cce`. In references
below, "b" and "c" are short for stage b and stage c, and each file and line is at the ref named.

## Answers

### Can raw be rewritten or deleted by code? Is that refused and tested?

Raw is written once by convention, but not enforced.

- `raw/<sha256>`: `store.write_raw` (main `store.py:181-193`) skips a name that exists and checks the new bytes'
  hash, but never checks the bytes already stored under that name, never makes the file read-only, and uses
  `tmp.replace(target)` (`store.py:193`), which would overwrite. A raw file edited by hand is read without
  complaint.
- `raw_records/`: `keep_raw_records` (main `store.py:275-298`; b `store.py:306-341`) checks `is_file()` and then
  `os.replace`s, so two writers racing can overwrite each other, and the file's sha256 is recorded nowhere (b's
  `.meta.json` lists the raw fetches' sha256s, not the records file's). On a rerun with the same snapshot id, records
  parsed by newer adapter code are silently not kept (b `store.py:217-224`), so a parser change is invisible.
- Private area: `private.write_raw_private` (main `private.py:186-200`) is write-once and owner-only, but
  `private.write_rows` rewrites `<partition>.jsonl` in place, upsert or replace (`private.py:203-267`), and
  `private.purge` deletes the whole source, raw included (`private.py:411-439`). Purge is a licence action and
  tested (`tests/test_private_store.py:354`); the in-place rewrite is by design today, so these partitions are not
  immutable.
- Control-plane exports (c): the control plane is the set of records only a maintainer decides, such as the
  condition crosswalk (`crosswalk.py` proposes, a maintainer accepts or rejects, each change is exported as a new
  complete file). They are read from `$HDR_CONTROL_PLANE/condition_crosswalk/*.ndjson`; nothing guards them and no
  hash is recorded (c `transform/models/staging/_raw_sources.yml:258-273`).
- Tests: a forged hash is refused (`tests/test_raw_and_hosts.py:30-38`) and snapshots are never overwritten
  (`tests/test_hub.py:351`, `tests/test_compile_golden.py:364`). No test refuses a rewrite of an existing raw file,
  detects a tampered one, or checks that no module deletes raw.

### Does dbt read `LATEST`? Is there a pinned build manifest?

- main and b: yes. `dbt_raw.load_sql` resolves `source('raw', id)` to the load `LATEST` names when dbt runs
  (`dbt_raw.py:47-81`, `latest_snapshot` at `:51`). Two builds at different times read different loads.
- c: worse. Staging reads every file in `raw_records/<id>/*.ndjson` and keeps the newest by file name
  (c `transform_gen.py:104-108`, `:154-156`; for example `transform/models/staging/stg_cdc_nndss_weekly.sql:7-9`),
  then deduplicates on the key (c `transform_gen.py:141-146`). The crosswalk takes the newest export
  (c `stg_control_plane__condition_crosswalk.sql:7-9`). The `_snapshot_id` and `_raw_sha256` provenance columns are
  gone (c staging models have neither).
- No build manifest exists. The snapshot manifest pins raw sha256s and contract and semantic versions
  (main `store.py:63-93`), but not the code commit, adapter, `uv.lock`, dbt or DuckDB version. The only versions
  recorded for a build are dbt's own version and invocation id in the gate's log (b `release_gate.py:59-63`).
  `uv.lock` pins versions in CI (`uv sync --locked`), but nothing records which lock a build used.

### Is each output tagged with a build id? Can an old build be reproduced?

- No. The gate's `build_id` is `sha256(invocation_id | generated_at)` (b `release_gate.py:171-175`): new on every
  run even with the same inputs, and it names no input.
- dbt's output is a DuckDB file in the runner's temp folder (b `refresh.yml:220`), discarded at the end of the job.
  Only `run_results.json` and `manifest.json` are kept, for 30 days.
- The published files are the snapshot's `data.parquet` from the Python staging step (main `publish.py:709`), not
  dbt output. The release manifest has no build id; `release_id` hashes the run date and snapshot ids only
  (main `publish.py:126-129`). A code fix with the same snapshots gets the same release id and is taken for the
  release already published (`publish.py:494-496`).
- Raw bytes cannot be replayed into a snapshot offline: `crawl.refresh_dataset` always fetches (b `crawl.py:399-437`).

### Does running the build twice give identical rows? Non-deterministic functions?

Mostly yes. These are the exceptions, in staging, review queues (`flag_*` models), marts and the publish step:

| Where | What | Risk |
|---|---|---|
| b `transform/models/flag/flag_cauris_clinical_screening_swap.sql:22` | `any_value(c.states)` | the shown area name can differ between runs when spellings differ |
| b `transform/models/flag/flag_all_dash_areas.sql:7` | `arg_max(states, year)` | ties within a year pick either spelling |
| c `transform_gen.py:141-146` and every c staging model, e.g. `stg_census_fips_county.sql:20` | `row_number() ... order by filename desc` over a key | ties inside one file pick an arbitrary row |
| c `transform/models/marts/dim_condition.sql:224` | `row_number() ... order by reviewed_at desc, record_id desc` | deterministic only if `record_id` is unique; no test says so |
| c staging, crosswalk | newest file by name | result depends on which files are present |
| main `staging.py:244-248`, `store.py:235` | Parquet written without `ORDER BY` | row order follows DuckDB's insertion order; bytes also carry DuckDB's version |
| main `publish.py:684` | `exported_at` in the release manifest | manifest bytes never repeat |
| b staging (`stg_cdc_respnet.sql:16`, `stg_cdc_nhsn_ar_hai.sql:19-23`, `stg_cdc_haicviz_cdi.sql:10`) | `DOUBLE` columns | safe now (no model sums them); any later `sum` over them is order-sensitive under `threads: 4` (`profiles.yml:10`) |

No model calls `now()`, `random()` or `uuid()` (searched main, b and c `transform/`).

### Is there a rebuild-from-raw test?

No. No test deletes derived data, rebuilds and compares hashes. CI runs `dbt build` once on fixtures
(b `.github/workflows/ci.yml:120-144`). The nearest checks are that committed transform files equal what the
contracts generate (b `tests/test_transform_gen.py:49`) and that a snapshot is not overwritten.

### Which data-quality gates block publishing today?

| Check | Runs | Blocks `LATEST` | Blocks publish |
|---|---|---|---|
| Citation, ID-area tag, licence text, redistribution, hosting | export | n/a | yes (main `publish.py:792-863`) |
| Partial snapshot; no rows; file sha256 and row count match the manifest | export | no | yes (`publish.py:843-862`) |
| Suppressed cells kept as published | export | no | yes (`publish.py:747`, `:860`) |
| Required value empty; count with a fraction; declared field absent | snapshot | yes: the load is refused (main `staging.py:178-200`) | yes |
| Contract quality rules, error severity (b) | every snapshot (b `store.py:243`) | **no**: `LATEST` moves anyway (b `store.py:268-269`); `QualityReport.blocking` (b `quality.py:325-327`) is never called | **no**: results go to the snapshot manifest; `export-public` is called without them (b `cli.py:78`) |
| dbt tests and unit tests (b) | weekly refresh | no | yes, by job order only (b `refresh.yml:320-321`); nothing ties the judged build to the uploaded files |
| Fill rate (b) | every snapshot | no (warn) | no (caveat) |
| Freshness | nowhere | no | no |
| Reconciliation of totals to parts | nowhere; the review queues list candidates only | no | no |
| Non-additive guard | at query time (main `query.py:191`, `sql_guard.py`) | no | no; no build-time test that a model doesn't sum a rate |
| Draft contract | answers carry a caveat | no | no |

## Gap list

A gap marked as blocking stage b or stage c should be fixed before that branch merges (stage b merges as PR #86),
because it either regresses something main already does or builds on a base ADR 0017 would have to undo.
Everything else is a follow-up PR on the usual cycle.

- **These block stage b (PR #86):** [1](#gap-1) quality failures don't block `LATEST` or publishing;
  [2](#gap-2) the dbt gate isn't tied to the uploaded files; [3](#gap-3) the gate's build id names a run, not its
  inputs.
- **These block stage c:** [4](#gap-4) stage c's base lacks `LATEST`-driven staging; [5](#gap-5) the crosswalk is
  read as the newest file, unpinned; [6](#gap-6) ties in `row_number()` dedupes.
- **Follow-ups:** gaps 7 to 20.

| # | Gap | Blocks | Smallest fix |
|---|---|---|---|
| <a id="gap-1"></a>1 | Snapshot quality failures don't block: `LATEST` moves onto a snapshot whose error-severity rule failed, and `blocking` is dead code (b `store.py:268-269`, `quality.py:325-327`). A rerun over the same raw also moves `LATEST` without any check: the early return for an existing snapshot calls `_point_latest` unconditionally (b `store.py:222-225`) | **blocks stage b** | In `build_snapshot`, if `staged.quality.blocking`, keep the snapshot directory but don't call `_point_latest`. On the early-return path, read `quality` from the existing manifest and apply the same check. The refusal names the rule and says which kind of problem stopped it: a data failure ("data failed rule X on `<dataset>`: observed v, bound b; `LATEST` stays on `<snapshot>`; see the snapshot's manifest") or a rule error ("rule X could not run: the contract no longer matches the data; a maintainer updates the contract"). In `check_release`, refuse a snapshot whose manifest `quality` is blocking. Three tests: a data failure, a rule error, and a rerun of a blocked snapshot that leaves `LATEST` unmoved. |
| <a id="gap-2"></a>2 | The dbt gate isn't bound to what is uploaded: the gate judges a run, export reads `LATEST` again later, and the release manifest has no build id (b `release_gate.py:171-175`, `refresh.yml:320-337`) | **blocks stage b** | Record each input's snapshot id in the gate's `Build` (read from staging's `_snapshot_id` values, or from a file the transform job writes), and have `export-public` refuse when the snapshots it stages differ from the ones the gate passed. Put `build_id` in the release manifest. |
| <a id="gap-3"></a>3 | `build_id` is not content-derived (b `release_gate.py:173`) | **blocks stage b** | Hash the sorted input snapshot ids, the git commit and `uv.lock`'s sha256 instead of invocation and clock; keep `invocation_id` as a separate field. A smaller version of ADR 0017 section 2 that #86 can carry. |
| <a id="gap-4"></a>4 | Stage c is based on PR #86's head (`cc32cce`), without #81's `LATEST`-driven staging: it reads `*.ndjson` and keeps the newest file, deduplicates in staging, and drops `_snapshot_id` and `_raw_sha256` (c `transform_gen.py:104-108`, `:139-158`) | **blocks stage c** | Rebase stage c onto stage b (`pre-pr/contract-silver-b` at `99e014f`) and regenerate: staging through `dbt_raw`, no dedupe, provenance columns kept. The Census FIPS sources go through the same plugin. |
| <a id="gap-5"></a>5 | Control-plane crosswalk read as "newest file in a folder", unhashed and unpinned; it changes condition keys in the marts (c `stg_control_plane__condition_crosswalk.sql:7-9`, `optional_source.sql:13-19`) | **blocks stage c** | Give the export a pointer and manifest like a snapshot (export id, sha256), resolve it in a `dbt_raw`-style plugin, and carry `_export_id` on each row of `dim_condition`. |
| <a id="gap-6"></a>6 | Ties in c's dedupe and `dim_condition`'s `row_number` (c `dim_condition.sql:224`) | **blocks stage c** | Drop the staging dedupe (gap 4); add a `unique` test on `record_id` in the crosswalk contract. |
| 7 | `any_value` and `arg_max` without a total tie-break in review queues (b `flag_cauris_clinical_screening_swap.sql:22`, `flag_all_dash_areas.sql:7`) | follow-up | In `flag_all_dash_areas`, `arg_max(states, (year, states))`, which keeps the spelling from the latest year and only breaks ties (`min(states)` would usually show the old upper-case spelling). In `flag_cauris_clinical_screening_swap`, where every row shares one year and the latest week, `min(c.states)`. Review queues only; no number depends on them. |
| 8 | Raw immutability is not enforced: no read-only mode, no hash check of an existing file, `os.replace` can overwrite, no test (main `store.py:181-193`, b `store.py:327-334`) | follow-up | One `put_raw` writer (ADR 0017 section 1): chmod the temporary file 0o444, `os.link` it into place, fsync the folder, refuse a differing existing file, and re-hash an already-streamed file; a test per case, and a static test that no module other than `put_raw` and `private.purge` deletes raw. |
| 9 | `raw_records` file hash recorded nowhere; a newer parse on the same snapshot id is silently dropped (b `store.py:217-224`) | follow-up | Write the records file's sha256 into `.meta.json` and the snapshot manifest; on rerun, compare, and when a newer parse differs say so: "Records for `<dataset>` snapshot `<id>` parse differently with this adapter version; the stored records were kept. Bump the contract version to keep the new parse (ADR 0015)." |
| 10 | Snapshot id leaves out code and adapter config (main `store.py:211`) | follow-up | Add the adapter type and version and the config digest to the digest inputs (ADR 0016, on PR #118, should say this too). |
| 11 | Private partitions rewritten in place (main `private.py:259-266`) | follow-up | Write each harvest as a new immutable load file; build the current partition view from the loads. |
| 12 | No build manifest; dbt output discarded (b `refresh.yml:220`) | follow-up | `builds/<build_id>/build.json` and outputs as in ADR 0017 sections 2 and 3; keep them in the private ops dataset. |
| 13 | No offline replay from raw bytes (b `crawl.py:399-437`) | follow-up | A replay getter serving each manifest `source_url` from `raw/<sha256>`, passed to `refresh_dataset` as `get`. |
| 14 | No rebuild-from-raw or thread-count test | follow-up | A CI job on the fixture lake: build, delete derived, rebuild, compare canonical content hashes; repeat with `threads: 1` and `threads: 8`. Then the nightly job on the real lake. |
| 15 | Release id ignores code, so a code-only correction looks already published (main `publish.py:126-129`, `:494-496`) | follow-up | Hash the build's content hashes (ADR 0017 section 3) into `release_id` once gap 12 lands, and record the build id beside it as provenance. |
| 16 | Parquet written unordered; `exported_at` in the manifest (main `staging.py:244-248`, `publish.py:684`) | follow-up | `COPY (SELECT ... ORDER BY key)`; leave `exported_at` out of anything hashed. Compare content hashes, not file bytes. |
| 17 | No freshness gate | follow-up | A reviewed catalog field per source, `freshness: {max_lag_days, basis}` with a codebook link; one check per source that has it, none for a source that doesn't. Caveat on the read path; block on publish unless the source is marked paused. |
| 18 | No reconciliation gate | follow-up | A contract-declared total-to-parts rule compiled to a dbt test, only where the contract names the total row, its parts, the comparison (`=` or `<=`) and the tolerance, each from the codebook. When any part is suppressed or flagged, the check reports not evaluated. A difference staging introduced blocks; a publisher's own total that doesn't reconcile is a caveat for a maintainer (ADR 0017 section 6). |
| 19 | No build-time non-additive guard | follow-up | A test over generated SQL that no `sum()` reads a measure the semantic model marks rate, estimate or non-additive. |
| 20 | Float columns could be summed order-sensitively later | follow-up | Determinism lint (ADR 0017 section 4); cast to `DECIMAL` before any sum of a published decimal. |

Not gaps: the hub reads only immutable release refs and checks each file's sha256 (main `hub.py:229-275`,
`:368-373`); snapshot directories are never overwritten (`store.py:215-221`); `LATEST` is swapped in one step
(`store.py:301-306`); dbt reads only full loads (`dbt_raw.py:64-68`).
