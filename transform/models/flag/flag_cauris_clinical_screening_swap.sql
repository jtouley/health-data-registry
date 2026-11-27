-- Review queue (contracts/models/flag_cauris_clinical_screening_swap.odcs.yaml, issue #16): area-years whose
-- C. auris clinical year-to-date exceeds screening at the year's latest week. Lists candidates; changes nothing.
with cauris as (
    select states, year, week, label, m3
    from {{ ref('int_cdc_nndss_weekly') }}
    where label in ('Candida auris, clinical', 'Candida auris, screening')
),

latest as (
    select states, year, max(week) as latest_week
    from cauris
    group by states, year
),

ytd as (
    select
        c.states,
        c.year,
        l.latest_week,
        max(c.m3) filter (where c.label = 'Candida auris, clinical') as clinical_ytd,
        max(c.m3) filter (where c.label = 'Candida auris, screening') as screening_ytd
    from cauris as c
    inner join latest as l on c.states = l.states and c.year = l.year and c.week = l.latest_week
    group by c.states, c.year, l.latest_week
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
