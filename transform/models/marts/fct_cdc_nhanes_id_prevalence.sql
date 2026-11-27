-- Fact for cdc_nhanes_id_prevalence (contracts/models/fct_cdc_nhanes_id_prevalence.odcs.yaml): one row per published
-- cell, every row of the latest load kept. Nothing is removed, added up or filled: each estimate is shown with its
-- interval and NCHS's flag, and the status says in words what the flag means. An unavailable estimate is never a zero.
select
  infection,
  time_period,
  population_group,
  subgroup,
  estimate,
  standard_error,
  estimate_lci,
  estimate_uci,
  flag,
  case
    when estimate is null then 'not published: ' || coalesce(flag_meaning, 'the publisher''s cell is empty')
    when flag is not null then 'published, flagged: ' || coalesce(flag_meaning, 'NCHS wrote ''' || flag || ''', a flag its footnotes do not list')
    else 'published'
  end as estimate_status,
  is_total_row,
  _snapshot_id as snapshot_id,
  _raw_sha256 as raw_sha256
from {{ ref('int_cdc_nhanes_id_prevalence') }}
