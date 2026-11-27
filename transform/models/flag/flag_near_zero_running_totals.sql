-- Review queue (contracts/models/flag_near_zero_running_totals.odcs.yaml): year-to-date counts at or below
-- near_zero_ytd_max at the year's latest week, after the year before had reached near_zero_prior_year_min by the
-- same MMWR week. Comparing week for week keeps a year in progress (week 1 in January) from being set against a
-- whole prior year. Reporting areas only (no summary rows), matched across years on states_key, so 'MINNESOTA'
-- (through 2024) and 'Minnesota' (from 2025) are one area. The thresholds choose what a maintainer looks at;
-- nothing is changed.
with nndss as (
    select states, states_key, label, year, week, m3, m3_flag
    from {{ ref('int_cdc_nndss_weekly') }}
    where not is_total_row
),

latest as (
    select states_key, label, year, max(week) as latest_week
    from nndss
    group by states_key, label, year
),

-- One row per area, condition and year, whatever the spellings: an area published under two spellings in its latest
-- week is shown once, as the alphabetically last spelling with that spelling's count (a total order, so every build
-- gives the same rows).
cur as (
    select n.states, n.states_key, n.label, n.year, l.latest_week, n.m3, n.m3_flag
    from nndss as n
    inner join latest as l
        on n.states_key = l.states_key and n.label = l.label and n.year = l.year and n.week = l.latest_week
    qualify row_number() over (
        partition by n.states_key, n.label, n.year
        order by n.states desc, n.m3 desc nulls last, n.m3_flag desc nulls last
    ) = 1
),

-- The prior year's week to compare with: the same week, or its last published week before it (a 53-week year
-- set against a 52-week one).
prior_week as (
    select c.states_key, c.label, c.year, max(p.week) as week
    from cur as c
    inner join nndss as p
        on p.states_key = c.states_key and p.label = c.label and p.year = c.year - 1 and p.week <= c.latest_week
    group by c.states_key, c.label, c.year
),

prior as (
    select w.states_key, w.label, w.year, w.week, p.m3
    from prior_week as w
    inner join nndss as p
        on p.states_key = w.states_key and p.label = w.label and p.year = w.year - 1 and p.week = w.week
    -- Two spellings in that week: the same rule as cur, so one prior count per area.
    qualify row_number() over (
        partition by w.states_key, w.label, w.year order by p.states desc, p.m3 desc nulls last
    ) = 1
)

select
    cur.states,
    cur.label,
    cur.year,
    cur.latest_week,
    cur.m3 as ytd,
    cur.m3_flag as ytd_flag,
    prior.m3 as prior_year_ytd,
    'Year-to-date is ' || coalesce(cast(cur.m3 as varchar), 'empty') || ' at week ' || cur.latest_week
    || ', after ' || prior.m3 || ' by week ' || prior.week
    || ' the year before: withdrawn, reset or not yet reported? Check with the publisher.' as reason
from cur
inner join prior on cur.states_key = prior.states_key and cur.label = prior.label and cur.year = prior.year
where coalesce(cur.m3, 0) <= {{ var('near_zero_ytd_max') }}
    and prior.m3 >= {{ var('near_zero_prior_year_min') }}
