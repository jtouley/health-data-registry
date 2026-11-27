-- Fact for cdc_haicviz_mugsi (contracts/models/fct_cdc_haicviz_mugsi.odcs.yaml): one row per published cell, every
-- row of the latest load kept. Nothing is removed, added up or filled: each row carries the figure as published,
-- whether CDC published it (an empty cell says so, never a zero) and CDC's own total-row flag.
select
  yearname as year,
  organism,
  topic,
  viewby as view,
  series as stratum,
  value,
  {{ value_status('value') }} as value_status,
  is_total_row,
  _snapshot_id as snapshot_id,
  _raw_sha256 as raw_sha256
from {{ ref('int_cdc_haicviz_mugsi') }}
