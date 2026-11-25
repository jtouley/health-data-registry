-- Review queue (contracts/models/flag_all_dash_areas.odcs.yaml, issue #14): area and condition pairs whose every
-- published week has an empty count flagged '-'. Each '-' stays a reported zero for its own week; nothing changes.
-- Reporting areas only (summary rows such as TOTAL and the census divisions are left out), matched across years
-- on states_key, so 'MINNESOTA' (through 2024) and 'Minnesota' (from 2025) are one area. A week with neither a
-- count nor a flag is not '-' (coalesce), so it keeps the pair out of the queue.
select
    arg_max(states, year) as states,
    label,
    min(year) as first_year,
    max(year) as last_year,
    count(*) as weeks,
    'Every one of the ' || count(*) || ' published weeks is ''-'' (no reported cases). Check whether the area reports '
    || 'this condition before any answer reads a whole period as zero (issue #14).' as reason
from {{ ref('int_cdc_nndss_weekly') }}
where not is_total_row
group by states_key, label
having bool_and(m1 is null and coalesce(trim(m1_flag), '') = '-')
