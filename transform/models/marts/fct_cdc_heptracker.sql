-- Fact for cdc_heptracker (contracts/models/fct_cdc_heptracker.odcs.yaml): one row per published cell, every row
-- of the latest load kept. Nothing is removed, added up or filled: each count and rate is shown as published, with
-- CDC's code and what it means wherever CDC published no number. A code is never a zero.
select
  condition,
  year,
  grouping,
  stratum,
  case_or_death,
  reported_count,
  count_flag,
  case when reported_count is not null then 'published' when count_flag is not null then 'not published: ' || coalesce(count_flag_meaning, 'CDC wrote ''' || count_flag || ''', a code its column description does not list') else 'not published: the publisher''s cell is empty' end as count_status,
  rate,
  rate_flag,
  case when rate is not null then 'published' when rate_flag is not null then 'not published: ' || coalesce(rate_flag_meaning, 'CDC wrote ''' || rate_flag || ''', a code its column description does not list') else 'not published: CDC gives no rate for this row' end as rate_status,
  is_total_row,
  _snapshot_id as snapshot_id,
  _raw_sha256 as raw_sha256
from {{ ref('int_cdc_heptracker') }}
