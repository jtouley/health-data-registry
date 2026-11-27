-- Every row of the Lyme disease mart sits in the era its year belongs to. Returns each row whose hard-coded
-- case_definition_era label disagrees with its year, so the mart's labels and the source contracts' era rules
-- (year_in_this_tables_era) can't drift apart.
select source_id, case_definition_era, year
from {{ ref('fct_cdc_lyme_county') }}
where not (
    (case_definition_era = '1992-2007' and year between 1992 and 2007)
    or (case_definition_era = '2008-2021' and year between 2008 and 2021)
    or (case_definition_era = '2022 onward' and year >= 2022)
)
