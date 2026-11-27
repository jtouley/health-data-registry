-- Every place-year a stored source's rows name is covered by its contract's declaration (#223). A row here is in
-- the flag_coverage_mismatch review queue too; the build fails so a wrong declaration never passes silently.
select dataset_id, place_key, year, row_count
from {{ ref('flag_coverage_mismatch') }}
where mismatch = 'observed_not_declared'
