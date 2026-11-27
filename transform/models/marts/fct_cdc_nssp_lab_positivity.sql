-- Fact: NSSP commercial laboratory test positivity (contracts/models/fct_cdc_nssp_lab_positivity.odcs.yaml).
-- Every row of the intermediate model, none removed and no value changed; value_status qualifies an empty percent
-- from the published counts and CDC's suppression rule (dataset description), and decides nothing.
select
    pathogen,
    mmwr_week_end as week_end,
    mmwr_week_start as week_start,
    geography_value as state,
    test_count as tests,
    positive_count as positive_tests,
    percent_pos as percent_positive,
    case
        when percent_pos is not null then 'published'
        when test_count = 0 then 'no test reported'
        else 'suppressed by CDC (fewer than 10 tests or 10 positives)'
    end as value_status,
    _snapshot_id,
    _raw_sha256
from {{ ref('int_cdc_nssp_lab_positivity') }}
