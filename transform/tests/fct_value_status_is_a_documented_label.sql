-- Each fact's value_status is one of the labels its model contract documents (contracts/models/fct_*.odcs.yaml),
-- so a typo in a mart's CASE, or a branch that drifts from the contract's description, fails here. Returns every
-- row whose label isn't documented. When the generator turns a declared list of values into an accepted_values
-- test (#199), this moves into the model contracts.
select 'fct_cdc_nssp_lab_positivity' as model, value_status
from {{ ref('fct_cdc_nssp_lab_positivity') }}
where value_status not in (
    'published',
    '0 tests published (nothing to divide by)',
    'suppressed by CDC (fewer than 10 tests or 10 positives)',
    'not published'
)
union all
select 'fct_nchs_resp_deaths_pct', value_status
from {{ ref('fct_nchs_resp_deaths_pct') }}
where value_status not in (
    'published',
    'suppressed by NCHS (a count from 1 to 9)',
    'no death recorded in the group',
    'not published'
)
