-- The SVI county dimension (contracts/models/dim_cdc_svi_county.odcs.yaml) keeps one edition and every rank as
-- published. Returns each row that breaks it: more than one edition in the table (ranks from different SVI years
-- are not comparable, publisher's data page), a rank outside 0 to 1 (documentation: "Percentile ranking values
-- range from 0 to 1"), or a rank that differs from the staged value it came from.
with editions as (
    select count(distinct svi_edition) as n from {{ ref('dim_cdc_svi_county') }}
)

select cast(null as varchar) as county_fips, 'more than one SVI edition' as broken
from editions where n > 1
union all
select county_fips, 'a rank is outside 0 to 1'
from {{ ref('dim_cdc_svi_county') }}
where least(rank_overall, rank_socioeconomic, rank_household_characteristics, rank_racial_ethnic_minority,
            rank_housing_transportation) < 0
   or greatest(rank_overall, rank_socioeconomic, rank_household_characteristics, rank_racial_ethnic_minority,
               rank_housing_transportation) > 1
union all
select d.county_fips, 'a rank differs from the staged value'
from {{ ref('dim_cdc_svi_county') }} as d
join {{ ref('stg_cdc_svi_county') }} as s using (county_fips)
where d.rank_overall is distinct from s.rank_overall
   or d.rank_socioeconomic is distinct from s.rank_socioeconomic
   or d.rank_household_characteristics is distinct from s.rank_household_characteristics
   or d.rank_racial_ethnic_minority is distinct from s.rank_racial_ethnic_minority
   or d.rank_housing_transportation is distinct from s.rank_housing_transportation
