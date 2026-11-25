-- Review queue (contracts/models/flag_near_zero_running_totals.odcs.yaml): year-to-date counts at or below
-- near_zero_ytd_max at the year's latest week after a year that ended at or above near_zero_prior_year_min.
-- The thresholds choose what a maintainer looks at; nothing is changed.
with latest as (
    select states, label, year, max(week) as latest_week
    from {{ ref('int_cdc_nndss_weekly') }}
    group by states, label, year
),

year_end as (
    select n.states, n.label, n.year, l.latest_week, n.m3, n.m3_flag
    from {{ ref('int_cdc_nndss_weekly') }} as n
    inner join latest as l
        on n.states = l.states and n.label = l.label and n.year = l.year and n.week = l.latest_week
)

select
    cur.states,
    cur.label,
    cur.year,
    cur.latest_week,
    cur.m3 as ytd,
    cur.m3_flag as ytd_flag,
    prev.m3 as prior_year_ytd,
    'Year-to-date is ' || coalesce(cast(cur.m3 as varchar), 'empty') || ' at the latest week, after '
    || prev.m3 || ' the year before: withdrawn, reset or not yet reported? Check with the publisher.' as reason
from year_end as cur
inner join year_end as prev
    on cur.states = prev.states and cur.label = prev.label and prev.year = cur.year - 1
where coalesce(cur.m3, 0) <= {{ var('near_zero_ytd_max') }}
    and prev.m3 >= {{ var('near_zero_prior_year_min') }}
