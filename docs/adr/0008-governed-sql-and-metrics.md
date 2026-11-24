# 0008. Named tools over reviewed metrics, with governed read-only SQL as the escape hatch

- **Status:** Accepted (revised 2026-11-24: `query_metric` dropped from the tool list, spec rev 62)
- **Date:** 2026-11-24
- **Spec:** "MCP tools"; "Spec changes" (consistency pass); "Starting point: what we reuse" (governed read-only SQL)

## Context

Free-form SQL written by a model is where sums of rates, double-counted total rows and bad joins come from. A
generic metric tool still asks the model to pick metric names and dimensions, which a clinician's question rarely
names. But no fixed set of tools covers every question.

## Decision

- Clinician-shaped tools, named in the GBD connector's style, are the main path: `get_series`,
  `get_aligned_with_gbd`, `get_rate`, plus `list_sources`, `search_datasets`, `describe_dataset`,
  `suggest_datasets`, `get_provenance`, `list_caveats` and `find_prior_work`. Behind them, `query.py` compiles the
  semantic model's metrics to SQL with each metric's aggregation rule; the model never writes that SQL.
- `query_sql` is the escape hatch for questions the tools don't cover. `sql_guard.py` parses the statement and
  accepts exactly one read-only `SELECT` over catalogued tables, with allowlisted functions only; it refuses
  writes, file reads, attachments, unbounded scans and any join the contract doesn't declare. Results are capped
  and DuckDB connections are read-only.
- Every result returns the SQL that ran, the source, its version and a method line. Results are CSV by default.
- GBD-style questions (top causes, trends) become named metrics behind these tools, not new tools.

## Consequences

- Most answers follow reviewed aggregation rules without the model choosing them.
- Some legitimate questions are refused by `query_sql`; the refusal says why in plain words, and the fix is a new
  metric or a declared relationship in the contract.
- The SQL guard is security-sensitive and is reviewed with the sharp-edges skill.
