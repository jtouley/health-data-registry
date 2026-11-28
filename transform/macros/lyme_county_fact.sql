{#
  lyme_county_fact: the one select behind the three Lyme disease county facts (fct_cdc_lyme_county,
  fct_cdc_lyme_county_2008_2021, fct_cdc_lyme_county_1992_2007), one fact per source. Every row of the
  intermediate model is kept with every value as published; the era label and the three status columns qualify a
  row and decide nothing. The status wording is the one the fact contracts describe
  (contracts/models/fct_cdc_lyme_county*.odcs.yaml); transform/tests/fct_cdc_lyme_county_keeps_every_row_and_
  qualifies_each_place.sql checks each fact against it. The markers are those of each source contract's
  state_is_a_postal_code_or_a_marker and fips_is_five_digits_or_a_marker rules; anything else reads as a label to
  review, never as a state or a county.
#}
{% macro lyme_county_status(column, shape, shape_status) -%}
case
    when {{ column }} = 'Suppressed' then 'suppressed by CDC (fewer than 5 records share the group)'
    when {{ column }} = 'Unknown' then 'not reported (Unknown)'
    when regexp_full_match({{ column }}, '{{ shape }}') then '{{ shape_status }}'
    else 'not a {{ shape_status }} or a CDC marker: a label to review'
end
{%- endmacro %}

{% macro lyme_county_fact(intermediate, era) -%}
select
    '{{ era }}' as case_definition_era,
    year,
    state,
    fips,
    case_status,
    sex,
    age_cat_yrs,
    frequency,
    {{ value_status('frequency') }} as value_status,
    {{ lyme_county_status('state', '[A-Z]{2}', 'two-letter state postal code') }} as state_status,
    {{ lyme_county_status('fips', '[0-9]{5}', 'five-digit county FIPS code') }} as county_status,
    is_total_row,
    _snapshot_id,
    _raw_sha256
from {{ ref(intermediate) }}
{%- endmacro %}
