-- Review queue (contracts/models/flag_cauris_clinical_screening_swap.odcs.yaml, issue #16): area-years whose
-- C. auris clinical year-to-date exceeds screening at the year's latest week. Lists candidates; changes nothing.
-- Reporting areas only: a summary row (TOTAL, a census division) would repeat one state's swap as another area.
-- The two labels are literals; the NNDSS contract's warn rule cauris_labels_present notices if CDC renames one.
with cauris as (
    select states, states_key, year, week, label, m3
    from {{ ref('int_cdc_nndss_weekly') }}
    where label in ('Candida auris, clinical', 'Candida auris, screening') and not is_total_row
),

latest as (
    select states_key, year, max(week) as latest_week
    from cauris
    group by states_key, year
),

ytd as (
    select
        c.states_key,
        c.year,
        l.latest_week,
        -- Two spellings of one area in the latest week: the alphabetically last (year is fixed by the grouping),
        -- so every build shows the same. Each count is the larger of the spellings' counts.
        max(c.states) as states,
        max(c.m3) filter (where c.label = 'Candida auris, clinical') as clinical_ytd,
        max(c.m3) filter (where c.label = 'Candida auris, screening') as screening_ytd
    from cauris as c
    inner join latest as l on c.states_key = l.states_key and c.year = l.year and c.week = l.latest_week
    group by c.states_key, c.year, l.latest_week
)

select
    states,
    year,
    latest_week,
    clinical_ytd,
    screening_ytd,
    'Clinical year-to-date (' || clinical_ytd || ') is larger than screening ('
    || coalesce(cast(screening_ytd as varchar), 'empty')
    || ') at the latest week; issue #16 saw this where the two looked swapped. Check against CDC before any change.'
    as reason
from ytd
where clinical_ytd > coalesce(screening_ytd, 0)
