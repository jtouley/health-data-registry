-- Every MMWR week of the calendar and every year (contracts/models/dim_time.odcs.yaml). A year row carries no
-- dates: which year it is (MMWR, calendar, the year of a July 1 estimate) is the fact's date_basis.
with weeks as (
    select
        cast(mmwr_year as varchar) || '-W' || lpad(cast(mmwr_week as varchar), 2, '0') as time_key,
        'mmwr_week' as time_grain,
        mmwr_year as year,
        mmwr_week,
        week_start as period_start,
        week_end as period_end
    from {{ ref('mmwr_calendar') }}
),

years as (
    select unnest(generate_series({{ var('time_first_year') }}, {{ var('mmwr_last_year') }})) as year
)

select time_key, time_grain, year, mmwr_week, period_start, period_end from weeks
union all
select
    'Y' || cast(year as varchar) as time_key,
    'year' as time_grain,
    year,
    cast(null as bigint) as mmwr_week,
    cast(null as date) as period_start,
    cast(null as date) as period_end
from years
