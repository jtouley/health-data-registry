{#
  One ODCS contract quality rule as a dbt test. `measure_sql` is the read path's own measurement
  (quality.measure_sql), one SELECT returning one number over {object}; `condition` is true when that number,
  named `measured`, meets the contract's bound (quality.condition_sql). The test returns a row when it does not.
  Both strings are generated from the contract by `make contracts`; nothing here is per source.
#}
{% test odcs_check(model, measure_sql, condition) %}
select measured
from ({{ measure_sql | replace('{object}', model) }}) as m(measured)
where not coalesce({{ condition }}, false)
{% endtest %}
