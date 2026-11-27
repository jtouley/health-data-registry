"""Prove each contract-declared dbt-expectations check fails when it should (`make transform`, after the build).

The synthetic fixture build is clean, so it shows only that the checks pass. This copies the fixture build's
DuckDB file, breaks it in one known way per check type (dq_checks.CHECK_TYPES), runs just those checks with
`dbt test` against the copy, and compares each status with the one expected. Nothing it writes is kept: the copy
and its run record live under transform/target/ (ignored).

    row_count_matches   a staging row deleted (raw to staging) and an intermediate row duplicated (staging to
                        intermediate)
    total_matches       the duplicated intermediate row carries a count, so the layer totals differ
    values_between      an MMWR week of 54
    load_within_sla     respnet's load stamped now passes; NNDSS's January load, past its weekly cadence, warns

Exit 1, naming each check, when a status differs from the one expected.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import duckdb

TRANSFORM = Path(__file__).resolve().parent.parent / "transform"
GOOD_DB = TRANSFORM / "target" / "ci.duckdb"
BAD_DB = TRANSFORM / "target" / "dq_bad.duckdb"
RUN_DIR = TRANSFORM / "target" / "dq_bad"
LAKE = TRANSFORM.parent / "tests" / "fixtures" / "transform"

EXPECTED = {
    "cdc_nndss_weekly_rows_raw_to_staging": "fail",
    "cdc_nhsn_ar_hai_rows_staging_to_intermediate": "fail",
    "cdc_nhsn_ar_hai_total_isolates_tested_staging_to_intermediate": "fail",
    "cdc_nndss_weekly_week_in_range": "fail",
    "cdc_nndss_weekly_load_within_refresh": "warn",
    "cdc_respnet_load_within_refresh": "pass",
}

BREAK = [
    # values_between: one week out of the codebook's 1-53 (the intermediate view reads staging).
    "update staging.stg_cdc_nndss_weekly set week = 54 where rowid = (select min(rowid) from staging.stg_cdc_nndss_weekly)",
    # row_count_matches, raw to staging: one staging row gone.
    "delete from staging.stg_cdc_nndss_weekly where rowid = (select max(rowid) from staging.stg_cdc_nndss_weekly)",
    # row_count_matches and total_matches, staging to intermediate: one intermediate row twice.
    "create table main.int_copy as select * from intermediate.int_cdc_nhsn_ar_hai",
    "drop view intermediate.int_cdc_nhsn_ar_hai",
    "create table intermediate.int_cdc_nhsn_ar_hai as select * from main.int_copy "
    "union all (select * from main.int_copy where isolates_tested > 0 limit 1)",
    # load_within_sla: a load stamped now is within any cadence.
    "update staging.stg_cdc_respnet set _snapshot_id = strftime(now() at time zone 'UTC', '%Y%m%dT%H%M%SZ') || '-0'",
]


def main() -> int:
    if not GOOD_DB.is_file():
        print(f"{GOOD_DB} is missing: run the fixture build first (make transform)", file=sys.stderr)
        return 1
    shutil.copyfile(GOOD_DB, BAD_DB)
    con = duckdb.connect(str(BAD_DB))
    for sql in BREAK:
        con.execute(sql)
    con.close()
    env = {
        **os.environ,
        "HDR_LAKE": str(LAKE),
        "HDR_TRANSFORM_DB": str(BAD_DB),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    cmd = ["dbt", "test", "--profiles-dir", ".", "--project-dir", ".", "--target-path", str(RUN_DIR)]
    cmd += ["--select", *EXPECTED]
    subprocess.run(cmd, cwd=TRANSFORM, env=env, check=False, capture_output=True)
    results = json.loads((RUN_DIR / "run_results.json").read_text(encoding="utf-8"))["results"]
    got = {r["unique_id"].split(".")[2]: r["status"] for r in results}
    wrong = [
        f"{name}: expected {want}, got {got.get(name, 'no result')}"
        for name, want in EXPECTED.items()
        if got.get(name) != want
    ]
    for name, want in EXPECTED.items():
        print(
            f"{'ok ' if got.get(name) == want else 'BAD'} {name}: {got.get(name, 'no result')} (expected {want})"
        )
    if wrong:
        print("A data-quality check did not catch its deliberately bad fixture:\n  " + "\n  ".join(wrong))
        return 1
    print(f"every check caught its bad fixture ({datetime.now(UTC):%Y-%m-%d})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
