from datetime import timedelta
from functools import lru_cache
import logging
import os
import re
from copy import deepcopy
from threading import Lock
from time import monotonic

from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient
from fastmcp import FastMCP

WORKSPACE_ID = os.environ.get("AZURE_LOG_ANALYTICS_WORKSPACE_ID", None)
CACHE_TTL_SECONDS = 300
MAX_DURATION_DAYS = 30

mcp = FastMCP("Azure KQL MCP Server")
logger = logging.getLogger(__name__)

_table_cache: tuple[float, str, list[dict]] | None = None
_schema_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_cache_lock = Lock()


@lru_cache(maxsize=1)
def get_logs_client() -> LogsQueryClient:
    """Create one reusable Azure credential and Log Analytics client."""
    credential = DefaultAzureCredential()
    return LogsQueryClient(credential)


def _get_cached_query(
    cache_key: tuple[str, ...],
    query: str,
    duration: str,
    cache: dict[tuple[str, ...], tuple[float, list[dict]]],
) -> list[dict]:
    now = monotonic()
    with _cache_lock:
        cached = cache.get(cache_key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return deepcopy(cached[1])

    results = run_kql_query(query, duration)
    with _cache_lock:
        cache[cache_key] = (monotonic(), deepcopy(results))
    return results


@mcp.tool()
def list_log_tables(duration: str = "24h") -> list[dict]:
    """List workspace tables that contain data in the selected time window."""
    global _table_cache
    now = monotonic()
    with _cache_lock:
        if _table_cache and now - _table_cache[0] < CACHE_TTL_SECONDS and _table_cache[1] == duration:
            return deepcopy(_table_cache[2])

    results = run_kql_query(
        "union withsource=__SourceTable * "
        "| summarize RowCount=count() by __SourceTable "
        "| order by __SourceTable asc",
        duration,
    )
    with _cache_lock:
        _table_cache = (monotonic(), duration, deepcopy(results))
    return results


@mcp.tool()
def get_table_schema(table_name: str, duration: str = "24h") -> list[dict]:
    """Return the columns and types for a Log Analytics table."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name.strip()):
        raise ValueError("table_name must be a valid Log Analytics table name")

    normalized_name = table_name.strip()
    return _get_cached_query(
        (normalized_name, duration),
        f"{normalized_name} | getschema",
        duration,
        _schema_cache,
    )


@mcp.tool()
def run_kql_query(query: str, duration: str = "24h") -> list[dict]:

    match = re.fullmatch(r"(\d+(?:\.\d+)?)(m|h|d|w)", duration.strip().lower())
    if not match:
        raise ValueError("duration must use a number followed by m, h, d, or w")

    amount = float(match.group(1))
    unit = match.group(2)
    duration_days = {
        "m": amount / (24 * 60),
        "h": amount / 24,
        "d": amount,
        "w": amount * 7,
    }[unit]
    if duration_days <= 0 or duration_days > MAX_DURATION_DAYS:
        raise ValueError(f"duration must be greater than 0 and no more than {MAX_DURATION_DAYS} days")

    client = get_logs_client()

    try:
        response = client.query_workspace(
            workspace_id=WORKSPACE_ID,
            query=query,
            timespan=timedelta(days=duration_days),
        )
    except Exception:
        logger.exception(
            "Azure Log Analytics query failed (duration=%s, query_prefix=%r)",
            duration,
            query[:200],
        )
        raise

    results = []

    if response.tables:
        for table in response.tables:

            columns = [
                col.name if hasattr(col, "name") else str(col)
                for col in table.columns
            ]

            for row in table.rows:
                results.append(dict(zip(columns, row)))

    return results

if __name__ == "__main__":
    print("Starting MCP Server...")
    try:
        mcp.run()
    except Exception as error:
        print(f"MCP Server failed to start: {error}")
        raise
