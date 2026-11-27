{{ config(severity='warn') }}
-- Every declared place and listed county is a key dim_place has. The registry refuses an unknown fixed key or a
-- malformed county key when it loads; whether a county exists depends on the Census load dim_place reads (CI's
-- synthetic lake has a few counties only), so a missing county is a warning to check against the latest load.
with declared as (
    select dataset_id, place_key from {{ ref('dim_dataset_coverage') }}
    union all
    select dataset_id, unnest(string_split(counties, ' ')) as place_key
    from {{ ref('dim_dataset_coverage') }}
    where counties is not null
)

select d.dataset_id, d.place_key
from declared as d
left join {{ ref('dim_place') }} as p on d.place_key = p.place_key
where p.place_key is null
