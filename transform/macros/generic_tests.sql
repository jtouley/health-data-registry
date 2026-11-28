{#
  Generic data tests the generated schemas use (transform_gen.py). Each returns the rows that break it.

  one_row_per_key: the contract's primary key holds once per load. A duplicate is a data finding that fails the
  build, never something staging resolves by dropping a row (hard rule 2).
  not_all_empty: a column the contract expects to carry values is not empty in every row of a non-empty load.
  An all-empty column passes every value check, and a renamed publisher field would load that way.
#}
{% test one_row_per_key(model, columns) %}
select
  {% for c in columns %}{{ adapter.quote(c) }}, {% endfor %}
  count(*) as rows_with_this_key
from {{ model }}
group by all
having count(*) > 1
{% endtest %}

{% test not_all_empty(model, column_name) %}
select count(*) as rows_in_load
from {{ model }}
having count(*) > 0 and count({{ adapter.quote(column_name) }}) = 0
{% endtest %}
