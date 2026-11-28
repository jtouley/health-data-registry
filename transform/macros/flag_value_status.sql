{#
  flag_value_status: why a published estimate is or isn't there, from the estimate and the publisher's flag, as one
  of five fixed labels. It qualifies the row and removes nothing (ADR 0018: show every row with its flag; qualify,
  don't decide). The flag's codes are named, never given a meaning: a code's meaning is a fact for the codebook or
  a maintainer's reviewed declaration (hard rule 1), so 'CDC flag 1' is not read as 'suppressed' here.
  The labels are listed once, in flag_value_status_labels(), which the marts' singular test checks against.
  It sits beside value_status(column) (macros/value_status.sql), the plain published / not published label other
  facts use, until #261 settles one convention for every fact.
#}
{% macro flag_value_status(estimate, flag) %}
case
    when {{ estimate }} is not null and {{ flag }} = '1' then 'published; CDC flag 1'
    when {{ estimate }} is not null then 'published'
    when {{ flag }} = '1' then 'empty; CDC flag 1'
    when {{ flag }} = '0' then 'empty; CDC flag 0'
    else 'empty; no CDC flag 0 or 1'
end
{% endmacro %}

{% macro flag_value_status_labels() %}
{{ return(['published', 'published; CDC flag 1', 'empty; CDC flag 1', 'empty; CDC flag 0', 'empty; no CDC flag 0 or 1']) }}
{% endmacro %}
