-- value_status is a label we write ourselves (macros/flag_value_status.sql), an exact structural fact, so a row with any
-- other label means the CASE was edited without its list: an error. Returns the offending rows.
{% set labels = flag_value_status_labels() %}
{% for model in ['fct_cdc_covid_adult_coverage', 'fct_cdc_rsv_adult_coverage'] %}
select '{{ model }}' as model, value_status
from {{ ref(model) }}
where value_status is null or value_status not in ({% for l in labels %}'{{ l }}'{% if not loop.last %}, {% endif %}{% endfor %})
{% if not loop.last %}union all{% endif %}
{% endfor %}
