-- Fact: Fact - COVID-19 vaccination coverage and intent among adults (NIS-FRVM) (contracts/models/fct_cdc_covid_adult_coverage.odcs.yaml).
-- Every row of the intermediate model, none removed and no value changed; CDC's suppression flag is carried as
-- published, and value_status qualifies an empty estimate without deciding anything.
select
    geographic_name as place,
    geographic_level as place_kind,
    demographic_level as group_kind,
    demographic_name as demographic_group,
    indicator_label as measure_kind,
    indicator_category_label as category,
    week_ending as week_end,
    covid_season as season,
    estimate,
    ci_half_width_95pct,
    unweighted_sample_size,
    suppression_flag,
    case
        when estimate is not null then 'published'
        when suppression_flag = '1' then 'suppressed by CDC (small sample)'
        else 'not published'
    end as value_status,
    _snapshot_id,
    _raw_sha256
from {{ ref('int_cdc_covid_adult_coverage') }}
