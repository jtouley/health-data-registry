{#
  One ODCS contract quality rule as a dbt test. `measure_sql` is the read path's own measurement
  (quality.measure_sql), one SELECT returning one number over {object}; `condition` is true when that number,
  named `measured`, meets the contract's bound (quality.condition_sql). The test returns a row when it does not.
  A measurement that comes back empty (NULL) raises an error, so dbt records the test as one that could not run
  (a rule error), exactly as quality.run_rules does; it is never a pass and never a data failure. With
  `nothing_to_judge` (a value rule, whose NULL means the column has no values), the error starts "Not evaluated",
  so the release gate reports it apart from a metric error. A metric error always holds the build; not evaluated
  holds it for an error-severity rule and is a warning for a warn-severity one, as on the read path.
  Both strings are generated from the contract by `make contracts`; nothing here is per source.
#}
{% test odcs_check(model, measure_sql, condition, nothing_to_judge=false) %}
select measured
from ({{ measure_sql | replace('{object}', model) }}) as m(measured)
where case
    when measured is null then error(
        {%- if nothing_to_judge %}'Not evaluated: no row has a value in this column, so there was nothing to check.'
        {%- else %}'The check returned no number, so it could not be judged.'{% endif -%}
    )
    else not ({{ condition }})
end
{% endtest %}
