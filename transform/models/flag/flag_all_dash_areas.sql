-- Review queue (contracts/models/flag_all_dash_areas.odcs.yaml, issue #14): area and condition pairs whose every
-- published week has an empty count flagged '-'. Each '-' stays a reported zero for its own week; nothing changes.
select
    states,
    label,
    min(year) as first_year,
    max(year) as last_year,
    count(*) as weeks,
    'Every one of the ' || count(*) || ' published weeks is ''-'' (no reported cases). Check whether the area reports '
    || 'this condition before any answer reads a whole period as zero (issue #14).' as reason
from {{ ref('int_cdc_nndss_weekly') }}
group by states, label
having bool_and(m1 is null and trim(m1_flag) = '-')
