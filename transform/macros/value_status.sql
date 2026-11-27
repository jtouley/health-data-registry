{#
  value_status: whether the publisher published a figure in a fact's cell, in the one wording every fact uses.
  An empty cell is kept and says so, never dropped or filled with a zero. transform/tests/ checks each fact's
  value_status against its value, so the two labels can't drift from what the fact contracts describe.
#}
{% macro value_status(column) -%}
case when {{ column }} is null then 'not published: the publisher''s cell is empty' else 'published' end
{%- endmacro %}
