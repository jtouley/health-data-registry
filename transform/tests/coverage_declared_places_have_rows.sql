{{ config(severity='warn') }}
-- A covered place-year with no rows is a warning, not a failure: it may be suppression or a late report (#223).
select dataset_id, place_key, year
from {{ ref('flag_coverage_mismatch') }}
where mismatch = 'declared_no_rows'
