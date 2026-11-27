-- Coverage of each place in each year, per source (contracts/models/fct_dataset_place_year_coverage.odcs.yaml).
-- The rule coverage.Coverage.holds applies in the server:
--   * a declared place, and every place inside it at a level the source reports (its catalog geography), but a
--     city only when the city itself is declared;
--   * for each year of the source, the most specific declaration whose interval holds the year decides;
--   * none holds the year: not_covered.
-- Years run from the source's first year to its last, or to var('coverage_through_year') while it is published.
with recursive ancestry as (
    select place_key, place_key as ancestor_key, place_level, 0 as depth
    from {{ ref('dim_place') }}
    union all
    select a.place_key, p.parent_key as ancestor_key, a.place_level, a.depth + 1 as depth
    from ancestry as a
    inner join {{ ref('dim_place') }} as p on a.ancestor_key = p.place_key
    where p.parent_key is not null
),

declared as (
    select * from {{ ref('dim_dataset_coverage') }}
),

-- Every (source, place) a declaration reaches, with how far below the declared place it sits.
reached as (
    select
        d.dataset_id,
        a.place_key,
        d.dim_dataset_coverage_sk,
        a.depth
    from declared as d
    inner join ancestry as a on d.place_key = a.ancestor_key
    where
        a.depth = 0
        or (a.place_level <> 'city' and list_contains(string_split(d.reported_levels, ' '), a.place_level))
),

years as (
    select distinct
        dataset_id,
        unnest(generate_series(
            dataset_first_year, coalesce(dataset_last_year, {{ var('coverage_through_year') }})
        )) as year
    from declared
),

candidates as (
    select
        r.dataset_id,
        r.place_key,
        y.year,
        r.depth,
        d.*
            exclude (dataset_id, place_key),
        d.place_key as declared_place_key
    from reached as r
    inner join declared as d on r.dim_dataset_coverage_sk = d.dim_dataset_coverage_sk
    inner join years as y on r.dataset_id = y.dataset_id
),

decided as (
    select
        dataset_id,
        place_key,
        year,
        arg_min(dim_dataset_coverage_sk, depth) filter (
            where year >= valid_from and (valid_to is null or year <= valid_to)
        ) as dim_dataset_coverage_sk,
        arg_min(denominator, depth) as denominator
    from candidates
    group by dataset_id, place_key, year
)

select
    x.dataset_id,
    x.place_key,
    x.year,
    case
        when c.dim_dataset_coverage_sk is null then 'not_covered'
        when c.depth = 0 and c.coverage_kind = 'full' and c.part = 'whole' then 'covered'
        when c.depth = 0 then 'partial_catchment'
        when c.part = 'whole' and c.coverage_kind = 'full' then 'covered'
        when c.part = 'whole' then 'partial_catchment'
        when c.counties is null then 'unknown'
        when list_contains(string_split(c.counties, ' '), x.place_key) then 'covered'
        else 'not_covered'
    end as coverage_status,
    x.denominator,
    x.dim_dataset_coverage_sk
from decided as x
left join candidates as c
    on
        x.dim_dataset_coverage_sk = c.dim_dataset_coverage_sk
        and x.place_key = c.place_key
        and x.year = c.year
