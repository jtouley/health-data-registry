-- Every declared place cites the publisher's own basis (hard rule 1): a page or file and what it says. Only a
-- source of unknown kind may go without, and it declares no place.
select dataset_id, place_key
from {{ ref('dim_dataset_coverage') }}
where coverage_kind <> 'unknown' and (nullif(trim(basis_url), '') is null or nullif(trim(basis_quote), '') is null)
