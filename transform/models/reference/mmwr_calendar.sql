-- MMWR weeks computed from CDC's rule (contracts/models/mmwr_calendar.odcs.yaml cites it): every MMWR week starts
-- on a Sunday, and week 1 is the first week with at least four days in the calendar year, which is the
-- Sunday-to-Saturday week that holds 4 January. No data file is read. The casts are arithmetic only (DuckDB adds
-- whole days to a DATE as INTEGER); the output types are the contract's and dbt enforces them.
with years as (
    select unnest(generate_series({{ var('mmwr_first_year') }}, {{ var('mmwr_last_year') }})) as mmwr_year
),

bounds as (
    select
        mmwr_year,
        make_date(mmwr_year, 1, 4) - cast(dayofweek(make_date(mmwr_year, 1, 4)) as integer) as week_one,
        make_date(mmwr_year + 1, 1, 4)
            - cast(dayofweek(make_date(mmwr_year + 1, 1, 4)) as integer) as next_week_one
    from years
),

weeks as (
    select
        mmwr_year,
        week_one,
        unnest(generate_series(1, (next_week_one - week_one) // 7)) as mmwr_week
    from bounds
)

select
    mmwr_year,
    mmwr_week,
    week_one + cast(7 * (mmwr_week - 1) as integer) as week_start,
    week_one + cast(7 * (mmwr_week - 1) + 6 as integer) as week_end
from weeks
