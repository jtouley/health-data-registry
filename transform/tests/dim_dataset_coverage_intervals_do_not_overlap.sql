-- No two versions of one source's declaration of one place hold the same year, and each place has exactly one
-- current version (SCD2 on the publisher's timeline; the registry refuses overlaps first).
with versions as (
    select
        dataset_id,
        place_key,
        valid_from,
        valid_to,
        is_current,
        lead(valid_from) over (partition by dataset_id, place_key order by valid_from) as next_from
    from {{ ref('dim_dataset_coverage') }}
),

overlapping as (
    select dataset_id, place_key, 'overlapping intervals' as problem
    from versions
    where next_from is not null and (valid_to is null or valid_to >= next_from)
),

currents as (
    select dataset_id, place_key, 'not exactly one current version' as problem
    from versions
    group by dataset_id, place_key
    having count(*) filter (where is_current) <> 1
)

select * from overlapping
union all
select * from currents
