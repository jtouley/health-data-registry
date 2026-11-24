# 0008. Metric-first queries, with governed read-only SQL as the escape hatch

- **Status:** Accepted
- **Date:** 2026-11-24
- **Spec:** "MCP tools"; "Starting point: what we reuse" (governed read-only SQL)

## Context

Free-form SQL written by a model is where sums of rates, double-counted total rows and bad joins come from. But no
metric set covers every question.

## Decision

- `query_metric` is the preferred path. The model picks a defined metric, filters, group-by and period from the
  semantic model; `semantic.py` writes the SQL with the metric's aggregation rule.
- `query_sql` is the escape hatch for questions no metric covers. `sql_guard.py` parses the statement and refuses
  writes, DDL, unbounded scans, files or URLs outside the snapshot views, and any join the contract doesn't declare.
  DuckDB connections are read-only.
- Both return the SQL that ran, the source, its version and a method line.
- GBD-style questions (top causes, trends) become named metrics, not new tools.

## Consequences

- Most answers follow reviewed aggregation rules.
- Some legitimate questions are refused by `query_sql`; the refusal says why, and the fix is a new metric or a
  declared relationship in the contract.
- The SQL guard is security-sensitive and is reviewed with the sharp-edges skill.
