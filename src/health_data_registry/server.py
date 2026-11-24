"""MCP server (stdio): five read-only tools over the reviewed registry and the local snapshot lake."""

from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server.mcpserver import MCPServer

from health_data_registry import __version__, tools
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import lake_root

READ_ONLY = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

INSTRUCTIONS = (
    "Public health data for clinician-researchers: observed and local counts from CDC, WHO and other "
    "publishers, with reviewed contracts. Use the IHME GBD connector for modeled burden; this server returns "
    "observed data keyed to GBD cause and location names and never calls GBD. Start with suggest_datasets or "
    "find_datasets, read describe_dataset before querying, prefer query_metric over query_sql. Cite every number "
    "with the source and retrieved_at it comes back with, show caveats, and never fill a number from memory. "
    "No patient information belongs in this chat."
)


def build_server(registry: Registry | None = None, engine: Engine | None = None) -> MCPServer:
    reg = registry or load_registry()
    eng = engine or Engine(registry=reg, lake=lake_root())
    mcp = MCPServer("health-data-registry", version=__version__, instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ_ONLY)
    def find_datasets(
        topic: str = "",
        research_area: str | None = None,
        geography: str | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
        shape: str | None = None,
    ) -> dict[str, Any]:
        """Ranked public datasets for a topic, with agency, years, geography, grain, licence and why each fits.
        geography is a level (nation, state, county, country); shape is e.g. surveillance_series or rate_table."""
        return tools.find_datasets(
            reg,
            topic=topic,
            research_area=research_area,
            geography=geography,
            year_start=year_start,
            year_end=year_end,
            shape=shape,
        )

    @mcp.tool(annotations=READ_ONLY)
    def describe_dataset(dataset_id: str) -> dict[str, Any]:
        """A dataset's contract in clinical words: what one row is, measures and units, missing codes, total rows,
        rates vs counts, metrics and dimensions you can query, caveats, open questions and how to cite it."""
        return tools.describe_dataset(reg, dataset_id, eng)

    @mcp.tool(annotations=READ_ONLY)
    def suggest_datasets(question: str) -> dict[str, Any]:
        """For a research question: which catalogued datasets can answer it, which can't and why, and the
        questions each supports."""
        return tools.suggest_datasets(reg, question)

    @mcp.tool(annotations=READ_ONLY)
    def query_metric(
        dataset_id: str,
        metric: str,
        filters: dict[str, Any] | None = None,
        group_by: list[str] | None = None,
        period: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a defined metric. filters maps dimension name to a value or list of values; group_by lists
        dimension names; period is {"start": .., "end": ..} on the time dimension. Counts are summed with total
        rows excluded; rates and estimates are returned as published, never added up. Returns the table, the SQL
        run, the source and its date, and a method line."""
        return tools.query_metric(
            eng, dataset_id, metric, filters=filters, group_by=group_by or [], period=period
        )

    @mcp.tool(annotations=READ_ONLY)
    def query_sql(sql: str) -> dict[str, Any]:
        """Escape hatch for questions no metric covers: one read-only SELECT over catalogued dataset ids (tables
        are named by dataset id). Writes, file reads and undeclared joins are refused; results are capped."""
        return tools.query_sql(eng, sql)

    return mcp


def main() -> None:
    build_server().run("stdio")
