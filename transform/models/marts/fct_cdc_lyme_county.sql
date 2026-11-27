-- Hand-written fact over CDC's three Lyme disease county tables (contracts/models/fct_cdc_lyme_county.odcs.yaml).
-- Every published row is kept as published; each is labelled with its source table and case-definition era, and
-- flagged where CDC withheld or did not know its state or county. Nothing is dropped, moved, added or filled in:
-- the flags qualify a row, and the reader decides (counts are added within one era only).
select
  'cdc_lyme_county_1992_2007' as source_id,
  '1992-2007' as case_definition_era,
  year,
  state,
  fips,
  case_status,
  sex,
  age_cat_yrs,
  frequency,
  state in ('Suppressed', 'Unknown') as state_withheld,
  not regexp_full_match(fips, '[0-9]{5}') as county_withheld,
  _snapshot_id,
  _raw_sha256
from {{ ref('int_cdc_lyme_county_1992_2007') }}
union all
select
  'cdc_lyme_county_2008_2021' as source_id,
  '2008-2021' as case_definition_era,
  year,
  state,
  fips,
  case_status,
  sex,
  age_cat_yrs,
  frequency,
  state in ('Suppressed', 'Unknown') as state_withheld,
  not regexp_full_match(fips, '[0-9]{5}') as county_withheld,
  _snapshot_id,
  _raw_sha256
from {{ ref('int_cdc_lyme_county_2008_2021') }}
union all
select
  'cdc_lyme_county' as source_id,
  '2022 onward' as case_definition_era,
  year,
  state,
  fips,
  case_status,
  sex,
  age_cat_yrs,
  frequency,
  state in ('Suppressed', 'Unknown') as state_withheld,
  not regexp_full_match(fips, '[0-9]{5}') as county_withheld,
  _snapshot_id,
  _raw_sha256
from {{ ref('int_cdc_lyme_county') }}
