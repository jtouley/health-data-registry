-- Fact for cdc_lyme_county_1992_2007 (contracts/models/fct_cdc_lyme_county_1992_2007.odcs.yaml).
-- One row per published count: every row of the latest load, every value as published. The shared select is in
-- macros/lyme_county_fact.sql: it labels the case-definition era and says in words whether CDC published the
-- count, the state and the county. Nothing is removed, moved, added up or filled in, and counts add up within
-- this table's era only.
{{ lyme_county_fact('int_cdc_lyme_county_1992_2007', '1992-2007') }}
