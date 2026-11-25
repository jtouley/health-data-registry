"""Behaviour evaluation: score a client run question by question, then report aggregates.

`scorer` scores one question from its expected record, the tool calls, their responses and the final answer.
`stats` holds the Wilson interval, McNemar's test and the DEV-TEST gap. `report` writes aggregate tables
and refuses to print per-question rows for the sealed TEST split. See eval/README.md.
"""
