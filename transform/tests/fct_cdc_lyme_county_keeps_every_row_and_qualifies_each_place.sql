-- The three Lyme disease county facts (one per source) keep every row of their intermediate model, and each row's
-- era and statuses are the ones its contract describes (contracts/models/fct_cdc_lyme_county*.odcs.yaml) and agree
-- with its values. Returns a row for each break: a row count that differs from the intermediate model (a filter or
-- join added later), a year outside the era label, a status whose wording or meaning drifts from the contract, or
-- a total row (the contracts declare none).
{% set facts = [
    ('fct_cdc_lyme_county_1992_2007', 'int_cdc_lyme_county_1992_2007', '1992-2007', 1992, 2007),
    ('fct_cdc_lyme_county_2008_2021', 'int_cdc_lyme_county_2008_2021', '2008-2021', 2008, 2021),
    ('fct_cdc_lyme_county', 'int_cdc_lyme_county', '2022 onward', 2022, 9999),
] %}
{% for fact, intermediate, era, first, last in facts %}
{% if not loop.first %}union all{% endif %}
select '{{ fact }}' as model, 'row count' as problem, cast(f.n as varchar) as detail
from (select count(*) as n from {{ ref(fact) }}) as f, (select count(*) as n from {{ ref(intermediate) }}) as i
where f.n <> i.n
union all
select '{{ fact }}', 'era or year', case_definition_era || ' ' || cast(year as varchar)
from {{ ref(fact) }}
where case_definition_era <> '{{ era }}' or year < {{ first }} or year > {{ last }}
union all
select '{{ fact }}', 'value_status', coalesce(value_status, 'null')
from {{ ref(fact) }}
where value_status is null
    or value_status not in ('published', 'not published: the publisher''s cell is empty')
    or (frequency is null) <> (value_status <> 'published')
union all
select '{{ fact }}', 'state_status', coalesce(state_status, 'null') || ' for ' || state
from {{ ref(fact) }}
where state_status is null
    or state_status <> case
        when state = 'Suppressed' then 'suppressed by CDC (fewer than 5 records share the group)'
        when state = 'Unknown' then 'not reported (Unknown)'
        when regexp_full_match(state, '[A-Z]{2}') then 'two-letter state postal code'
        else 'not a two-letter state postal code or a CDC marker: a label to review'
    end
union all
select '{{ fact }}', 'county_status', coalesce(county_status, 'null') || ' for ' || fips
from {{ ref(fact) }}
where county_status is null
    or county_status <> case
        when fips = 'Suppressed' then 'suppressed by CDC (fewer than 5 records share the group)'
        when fips = 'Unknown' then 'not reported (Unknown)'
        when regexp_full_match(fips, '[0-9]{5}') then 'five-digit county FIPS code'
        else 'not a five-digit county FIPS code or a CDC marker: a label to review'
    end
union all
select '{{ fact }}', 'total row', cast(year as varchar) || ' ' || state || ' ' || fips
from {{ ref(fact) }}
where is_total_row
{% endfor %}
