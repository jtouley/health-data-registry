-- Hand-written county dimension over CDC/ATSDR SVI 2022 (contracts/models/dim_cdc_svi_county.odcs.yaml): a
-- covariate keyed by five-digit county FIPS, to join beside an infectious-disease rate for the same county
-- (hard rule 11, ADR 0019). Every county row is kept with its ranks as published; nothing is added, averaged,
-- bucketed or filled in. The edition and the ranking scope travel with each row, because ranks from different SVI
-- years, or from a state database, are not comparable with these.
select
  county_fips,
  state_fips,
  state_abbr,
  state_name,
  county_name,
  '2022' as svi_edition,
  'US counties (50 states and DC)' as ranking_scope,
  '2018-2022' as acs_period,
  rank_overall,
  rank_socioeconomic,
  rank_household_characteristics,
  rank_racial_ethnic_minority,
  rank_housing_transportation,
  flags_total,
  acs_population,
  acs_population_moe,
  state_fips = '09' as is_connecticut_planning_region,
  _snapshot_id,
  _raw_sha256
from {{ ref('int_cdc_svi_county') }}
