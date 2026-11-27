-- fct_cdc_adult_vax_coverage: value_status takes only the states its contract describes. 'published', or a symbol
-- the source contract declares in missingCodes ('*', 'NR'), or 'nothing' for an empty text. A warning, not an error:
-- the load already refuses an undeclared symbol (#188), so a row here means the mart and the contract disagree.
{{ config(severity='warn') }}
select value_status, count(*) as n_rows
from {{ ref('fct_cdc_adult_vax_coverage') }}
where value_status not in (
    'published',
    'not a number: CDC published *',
    'not a number: CDC published NR',
    'not a number: CDC published nothing'
)
group by value_status
