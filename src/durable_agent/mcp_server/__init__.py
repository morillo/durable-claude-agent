"""MCP tool server for the analyst agent.

Four tools over one Streamable HTTP endpoint:

* ``get_schema``        read-only, model-facing
* ``search_governance`` read-only, model-facing
* ``validate_sql``      read-only dry run (EXPLAIN + deterministic risk), model-facing
* ``run_sql``           executes; requires a capability token the model never holds

``MODEL_TOOLS`` is the allow-list the ``generate_sql`` activity exposes to Claude. ``run_sql``
is excluded there *and* refuses calls without the bearer token, so two independent controls
have to fail before the model can execute anything.
"""

from durable_agent.mcp_server.client import ToolClient, tool_client
from durable_agent.mcp_server.server import MODEL_TOOLS, create_server

__all__ = ["MODEL_TOOLS", "ToolClient", "create_server", "tool_client"]
