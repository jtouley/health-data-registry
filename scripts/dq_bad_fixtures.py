"""Prove each contract-declared dbt-expectations check fails when it should (`make transform`, after the build).

The synthetic fixture build is clean, so it shows only that the checks pass. For each scenario below this copies
the fixture build's DuckDB file, breaks the copy in known ways, runs just the named checks with `dbt test` against
it, and compares each status with the one expected. A check expected to pass is there to show that each check
catches its own break and nothing else. Nothing it writes is kept: the copies and their run records live under
transform/target/ (ignored), and each run record is removed first, so a dbt run that writes none can never be
read as an earlier run's result.

    row_count_matches   a staging row deleted (raw to staging fails); an intermediate row duplicated (staging to
                        intermediate fails); an intermediate count changed with no row added (the row check passes)
    total_matches       that changed count fails its own total alone; the duplicated row fails its total too
    values_between      an MMWR week of 54 and a percent of -1 (each bound) fail; a week of 0 fails; an empty
                        percent passes (empty values are not judged)
    load_within_sla     respnet's load stamped now passes; NNDSS's January load, past its weekly cadence, warns

Exit 1, naming each check, when a status differs from the one expected or dbt could not run the checks.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

TRANSFORM = Path(__file__).resolve().parent.parent / "transform"
GOOD_DB = TRANSFORM / "target" / "ci.duckdb"
TARGET = TRANSFORM / "target"
LAKE = TRANSFORM.parent / "tests" / "fixtures" / "transform"


@dataclass(frozen=True)
class Scenario:
    name: str
    breaks: list[str]
    expected: dict[str, str]


# Each change is literal SQL on the copy: the intermediate views are replaced by tables of their own rows first,
# where a scenario changes them (no build can do that).
SCENARIOS = [
    Scenario(
        "breaks",
        [
            # values_between, upper bound: one week out of the codebook's 1-53.
            "update staging.stg_cdc_nndss_weekly set week = 54"
            " where rowid = (select min(rowid) from staging.stg_cdc_nndss_weekly)",
            # values_between, lower bound: one percent below 0.
            "update staging.stg_cdc_nhsn_ar_hai set percent_resistant = -1 where rowid ="
            " (select min(rowid) from staging.stg_cdc_nhsn_ar_hai where percent_resistant is not null)",
            # row_count_matches, raw to staging: one staging row gone.
            "delete from staging.stg_cdc_nndss_weekly"
            " where rowid = (select max(rowid) from staging.stg_cdc_nndss_weekly)",
            # total_matches alone: one intermediate count changed, no row added or dropped.
            "create table main.nndss_copy as select * from intermediate.int_cdc_nndss_weekly",
            "drop view intermediate.int_cdc_nndss_weekly",
            "create table intermediate.int_cdc_nndss_weekly as select * from main.nndss_copy",
            "update intermediate.int_cdc_nndss_weekly set m1 = m1 + 1"
            " where rowid = (select min(rowid) from intermediate.int_cdc_nndss_weekly where m1 > 0)",
            # row_count_matches and total_matches, staging to intermediate: one intermediate row twice.
            "create table main.nhsn_copy as select * from intermediate.int_cdc_nhsn_ar_hai",
            "drop view intermediate.int_cdc_nhsn_ar_hai",
            "create table intermediate.int_cdc_nhsn_ar_hai as select * from main.nhsn_copy"
            " union all (select * from main.nhsn_copy where isolates_tested > 0 limit 1)",
            # load_within_sla: a load stamped now is within any cadence.
            "update staging.stg_cdc_respnet set _snapshot_id ="
            " strftime(now() at time zone 'UTC', '%Y%m%dT%H%M%SZ') || '-0'",
        ],
        {
            "cdc_nndss_weekly_rows_raw_to_staging": "fail",
            "cdc_nndss_weekly_rows_staging_to_intermediate": "pass",
            "cdc_nndss_weekly_total_m1_staging_to_intermediate": "fail",
            "cdc_nhsn_ar_hai_rows_staging_to_intermediate": "fail",
            "cdc_nhsn_ar_hai_total_isolates_tested_staging_to_intermediate": "fail",
            "cdc_nndss_weekly_week_in_range": "fail",
            "cdc_nhsn_ar_hai_percent_resistant_in_range": "fail",
            "cdc_nndss_weekly_load_within_refresh": "warn",
            "cdc_respnet_load_within_refresh": "pass",
        },
    ),
    Scenario(
        "edges",
        [
            # values_between, lower bound of the week: 0 is out of range.
            "update staging.stg_cdc_nndss_weekly set week = 0"
            " where rowid = (select min(rowid) from staging.stg_cdc_nndss_weekly)",
            # values_between skips an empty value: it is not judged, so the check passes.
            "update staging.stg_cdc_nhsn_ar_hai set percent_resistant = null where rowid ="
            " (select min(rowid) from staging.stg_cdc_nhsn_ar_hai where percent_resistant is not null)",
        ],
        {
            "cdc_nndss_weekly_week_in_range": "fail",
            "cdc_nhsn_ar_hai_percent_resistant_in_range": "pass",
        },
    ),
]


def run(scenario: Scenario) -> list[str]:
    """Break a copy of the fixture build, run the scenario's checks, and return what came out wrong."""
    bad_db = TARGET / f"dq_{scenario.name}.duckdb"
    run_dir = TARGET / f"dq_{scenario.name}"
    shutil.copyfile(GOOD_DB, bad_db)
    shutil.rmtree(run_dir, ignore_errors=True)  # never read an earlier run's record
    con = duckdb.connect(str(bad_db))
    for sql in scenario.breaks:
        con.execute(sql)
    con.close()
    env = {
        **os.environ,
        "HDR_LAKE": str(LAKE),
        "HDR_TRANSFORM_DB": str(bad_db),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    cmd = ["dbt", "test", "--profiles-dir", ".", "--project-dir", ".", "--target-path", str(run_dir)]
    cmd += ["--select", *scenario.expected]
    done = subprocess.run(cmd, cwd=TRANSFORM, env=env, check=False, capture_output=True, text=True)
    record = run_dir / "run_results.json"
    # dbt test exits 1 when a test fails, as some must here; anything else, or no run record, means it never ran.
    if done.returncode not in (0, 1) or not record.is_file():
        print(f"dbt could not run the {scenario.name} checks (exit {done.returncode}):", file=sys.stderr)
        print(done.stdout + done.stderr, file=sys.stderr)
        return [f"{scenario.name}: dbt test did not run"]
    results = json.loads(record.read_text(encoding="utf-8"))["results"]
    got = {r["unique_id"].split(".")[2]: r["status"] for r in results}
    wrong = []
    for name, want in scenario.expected.items():
        ok = got.get(name) == want
        print(
            f"{'ok ' if ok else 'BAD'} {scenario.name} {name}: {got.get(name, 'no result')} (expected {want})"
        )
        if not ok:
            wrong.append(f"{scenario.name} {name}: expected {want}, got {got.get(name, 'no result')}")
    if wrong:
        print(done.stdout, file=sys.stderr)
    return wrong


def main() -> int:
    if not GOOD_DB.is_file():
        print(f"{GOOD_DB} is missing: run the fixture build first (make transform)", file=sys.stderr)
        return 1
    wrong = [w for scenario in SCENARIOS for w in run(scenario)]
    if wrong:
        print("A data-quality check did not catch its deliberately bad fixture:\n  " + "\n  ".join(wrong))
        return 1
    print(f"every check caught its bad fixture ({datetime.now(UTC):%Y-%m-%d})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
