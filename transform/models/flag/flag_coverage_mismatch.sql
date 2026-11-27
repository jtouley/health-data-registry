-- Review queue (contracts/models/flag_coverage_mismatch.odcs.yaml): declared coverage against the rows. Only sources
-- whose contract says where their rows name their place (coverage.observed) are reconciled. Removes nothing.
with observed as (
    select * from {{ ref('int_observed_places') }}
),

coverage as (
    select * from {{ ref('fct_dataset_place_year_coverage') }}
),

reconciled as (
    select distinct dataset_id from observed
),

observed_not_declared as (
    select
        o.dataset_id,
        o.place_key,
        o.year,
        'observed_not_declared' as mismatch,
        o.row_count,
        'The source has rows for this place and year, but its contract''s coverage block does not cover them: '
        || 'declare the place (with the publisher''s basis) or find out why the rows are there.' as reason
    from observed as o
    left join coverage as c
        on o.dataset_id = c.dataset_id and o.place_key = c.place_key and o.year = c.year
    where c.coverage_status is null or c.coverage_status = 'not_covered'
),

declared_no_rows as (
    select
        c.dataset_id,
        c.place_key,
        c.year,
        'declared_no_rows' as mismatch,
        cast(0 as bigint) as row_count,
        'The contract declares this place and year covered, but the latest load has no row for it: suppression, a '
        || 'late report, or a declaration to correct.' as reason
    from coverage as c
    inner join reconciled as r on c.dataset_id = r.dataset_id
    left join observed as o
        on c.dataset_id = o.dataset_id and c.place_key = o.place_key and c.year = o.year
    where c.coverage_status in ('covered', 'partial_catchment') and o.place_key is null
)

select * from observed_not_declared
union all
select * from declared_no_rows
