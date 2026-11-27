-- Fact: Vaccination coverage among adults, BRFSS (contracts/models/fct_cdc_adult_vax_coverage.odcs.yaml).
-- Every row of the intermediate model, none removed and no value changed; the estimate's published text and the
-- interval are carried as sent, and value_status names the symbol CDC published instead of a number, without
-- interpreting it. The symbol is named trimmed, and an empty text reads 'nothing', so a status never ends in a blank.
-- A business rule in the mart because intermediate models are generated (open question in the fact's contract).
select
    vaccine,
    dose,
    fips as place_code,
    geography as place,
    geography_type as place_kind,
    year_season as survey_year,
    dimension_type as age_group,
    dimension as demographic_group,
    estimate_as_published,
    ci_95,
    coverage_estimate as coverage_percent,
    population_sample_size as sample_size,
    case
        when coverage_estimate is not null then 'published'
        else 'not a number: CDC published ' || coalesce(nullif(trim(estimate_as_published), ''), 'nothing')
    end as value_status,
    _snapshot_id,
    _raw_sha256
from {{ ref('int_cdc_adult_vax_coverage') }}
