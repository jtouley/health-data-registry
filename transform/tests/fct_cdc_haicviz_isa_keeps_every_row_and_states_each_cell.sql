-- The fact for cdc_haicviz_isa keeps every row of the intermediate model, and each row's value_status is one of
-- the two labels its contract describes and agrees with its value. Returns a row for each break: a row count that
-- differs from int_cdc_haicviz_isa (a filter or join added later), or a cell whose status is unknown or wrong.
with counts as (
    select
        (select count(*) from {{ ref('fct_cdc_haicviz_isa') }}) as fact_rows,
        (select count(*) from {{ ref('int_cdc_haicviz_isa') }}) as intermediate_rows
)
select 'row count' as problem, cast(fact_rows as varchar) as detail
from counts
where fact_rows <> intermediate_rows
union all
select 'value_status' as problem, coalesce(value_status, 'null') as detail
from {{ ref('fct_cdc_haicviz_isa') }}
where value_status is null
    or value_status not in ('published', 'not published: the publisher''s cell is empty')
    or (value is null) <> (value_status <> 'published')
