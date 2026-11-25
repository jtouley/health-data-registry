{#
  A source that may not exist yet (a control-plane export, meta.optional: true): the source itself when a file
  matches its meta.glob, else an empty relation with the same text columns and a filename column, so the
  staging model reads "nothing exported yet" instead of failing. Nothing here is per source: the glob and the
  columns come from the source's generated entry in _raw_sources.yml (`make contracts`).
#}
{% macro optional_source(source_name, table_name) -%}
  {%- set rel = source(source_name, table_name) -%}
  {%- if not execute -%}
    {{ rel }}
  {%- else -%}
    {%- set node = graph.sources['source.' ~ project_name ~ '.' ~ source_name ~ '.' ~ table_name] -%}
    {%- set pattern = node.meta.glob | replace("'", "''") -%}
    {%- set found = run_query("select count(*) from glob('" ~ pattern ~ "')").columns[0].values()[0] -%}
    {%- if found | int > 0 -%}
      {{ rel }}
    {%- else -%}
      (select {% for col in node.columns.values() %}cast(null as varchar) as {{ adapter.quote(col.name) }}, {% endfor %}cast(null as varchar) as filename where false)
    {%- endif -%}
  {%- endif -%}
{%- endmacro %}
