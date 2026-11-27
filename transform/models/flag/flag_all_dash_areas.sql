-- Review queue (contracts/models/flag_all_dash_areas.odcs.yaml, issue #14): area and condition pairs whose every
-- published week has an empty count flagged '-'. Each '-' stays as published (no number, with its flag): by the
-- maintainer's declaration (NNDSS contract, noCasesCode) it means not reported, so no answer counts it as 0 (#173).
-- Reporting areas only (summary rows such as TOTAL and the census divisions are left out), matched across years
-- on states_key, so 'MINNESOTA' (through 2024) and 'Minnesota' (from 2025) are one area. A week with neither a
-- count nor a flag is not '-' (coalesce), so it keeps the pair out of the queue.
-- The area is shown as spelled in its latest year; two spellings in that year: the alphabetically last. A total
-- order (arg_max over year, then spelling), so two builds from the same snapshot give the same rows.
-- weeks counts distinct MMWR weeks, so a week published twice (two spellings) is one week.
select
    arg_max(states, (year, states)) as states,
    label,
    min(year) as first_year,
    max(year) as last_year,
    count(distinct (year, week)) as weeks,
    'Every one of the ' || count(distinct (year, week)) || ' published weeks is ''-'' (no reported cases). Check whether the area reports '
    || 'this condition: ''-'' means not reported, so no answer counts these weeks as 0 (issues #14, #173).' as reason
from {{ ref('int_cdc_nndss_weekly') }}
where not is_total_row
group by states_key, label
having bool_and(m1 is null and coalesce(trim(m1_flag), '') = '-')
