-- SCD Type 2 on the publisher's timeline (contracts/models/dim_dataset_coverage.odcs.yaml): one version per declared
-- place and interval. valid_from and valid_to say when the coverage was true, from the publisher; when we learned
-- it is declared_in_contract_version and declared_at. Nothing comes from load times, so this is not a dbt snapshot.
-- A source that declares no place has no row here (its coverage of any place is unknown).
with declared as (
    select * from {{ ref('coverage_declarations') }}
    where place_key is not null
)

select
    md5(dataset_id || '|' || place_key || '|' || cast(valid_from as varchar)) as dim_dataset_coverage_sk,
    dataset_id,
    place_key,
    valid_from,
    valid_to,
    row_number() over (partition by dataset_id, place_key order by valid_from desc) = 1 as is_current,
    coverage_kind,
    part,
    counties,
    denominator,
    time_unit,
    basis_url,
    basis_quote,
    contract_version,
    declared_in_contract_version,
    declared_at,
    dataset_first_year,
    dataset_last_year,
    reported_levels,
    national_scope
from declared
