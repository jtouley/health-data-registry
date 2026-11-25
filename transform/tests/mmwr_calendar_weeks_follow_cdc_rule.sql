-- The MMWR calendar as dbt builds it follows CDC's rule. Returns every week that breaks it:
-- a week that doesn't start on a Sunday or doesn't end six days later, a year with other than 52 or 53 weeks,
-- or a week that doesn't start the day after the previous one ends.
-- tests/test_transform_gen.py checks the same model against CDC's published weeks for 2019 to 2026.
with weeks as (
    select
        mmwr_year,
        mmwr_week,
        week_start,
        week_end,
        lag(week_end) over (order by mmwr_year, mmwr_week) as previous_end,
        count(*) over (partition by mmwr_year) as weeks_in_year
    from {{ ref('mmwr_calendar') }}
)

select mmwr_year, mmwr_week, 'week_start is not a Sunday' as broken
from weeks where dayofweek(week_start) <> 0
union all
select mmwr_year, mmwr_week, 'week_end is not week_start + 6 days'
from weeks where week_end <> week_start + 6
union all
select mmwr_year, mmwr_week, 'the year has neither 52 nor 53 weeks'
from weeks where weeks_in_year not in (52, 53)
union all
select mmwr_year, mmwr_week, 'the week does not start the day after the previous week ends'
from weeks where previous_end is not null and week_start <> previous_end + 1
