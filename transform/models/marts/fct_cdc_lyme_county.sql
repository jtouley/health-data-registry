-- Hand-written fact over CDC's three Lyme disease county tables (contracts/models/fct_cdc_lyme_county.odcs.yaml).
-- Every published row is kept as published; each is labelled with its source table and case-definition era, and
-- flagged where its state or county can't be placed. Nothing is dropped, moved, added or filled in: the flags
-- qualify a row, and the reader decides (counts are added within one era only, never on a total row).
with published as (
    select 'cdc_lyme_county_1992_2007' as source_id, '1992-2007' as case_definition_era, *
    from {{ ref('int_cdc_lyme_county_1992_2007') }}
    union all by name
    select 'cdc_lyme_county_2008_2021' as source_id, '2008-2021' as case_definition_era, *
    from {{ ref('int_cdc_lyme_county_2008_2021') }}
    union all by name
    select 'cdc_lyme_county' as source_id, '2022 onward' as case_definition_era, *
    from {{ ref('int_cdc_lyme_county') }}
),

placed as (
    -- Defined by shape, not by a list of markers: anything that isn't a two-letter postal code ('Suppressed',
    -- 'Unknown', a new or differently cased marker) can't be placed in a state. The source contracts' warn rules
    -- name any label outside their lists for review.
    select
        *,
        not regexp_full_match(state, '[A-Z]{2}') as state_unplaced
    from published
)

select
    source_id,
    case_definition_era,
    year,
    state,
    fips,
    case_status,
    sex,
    age_cat_yrs,
    frequency,
    is_total_row,
    state_unplaced,
    -- Not a five-digit code ('Suppressed', 'Unknown', the 1992-1993 '42  .' values), or a known county under an
    -- unplaced state: those cases count in no state's figure, so they count in no county's either, and a
    -- county's sum is always part of its state's.
    state_unplaced or not regexp_full_match(fips, '[0-9]{5}') as county_unplaced,
    _snapshot_id,
    _raw_sha256
from placed
