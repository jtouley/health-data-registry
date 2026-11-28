-- Fact: NCHS percent of deaths involving COVID-19, influenza and RSV (contracts/models/fct_nchs_resp_deaths_pct.odcs.yaml).
-- Every row of the intermediate model, none removed and no value changed; NCHS's provisional and suppression notes
-- and its data-as-of date are carried as published, and value_status qualifies an empty percent without deciding
-- anything.
select
    weekending_date as week_end,
    start_date as week_start,
    pathogen as cause,
    demographic_type as group_kind,
    demographic_values as demographic_group,
    deaths as deaths_involving,
    total_deaths as all_cause_deaths,
    percent_deaths as percent_of_deaths,
    provisional,
    suppressed as suppression_note,
    data_as_of,
    case
        when percent_deaths is not null then 'published'
        when suppressed is not null then 'suppressed by NCHS (a count from 1 to 9)'
        when total_deaths = 0 then 'no death recorded in the group'
        else 'not published'
    end as value_status,
    _snapshot_id,
    _raw_sha256
from {{ ref('int_nchs_resp_deaths_pct') }}
