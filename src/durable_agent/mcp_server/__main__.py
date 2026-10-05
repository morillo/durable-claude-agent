"""Run the MCP server over Streamable HTTP: ``python -m durable_agent.mcp_server``."""

from __future__ import annotations

import logging
import sys
from urllib.parse import urlparse

from durable_agent.config import get_settings
from durable_agent.mcp_server.server import AppState, create_server
from durable_agent.observability import setup_tracing


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    setup_tracing(settings, service_name="governed-lakehouse-mcp")
    if settings.run_sql_capability_token is None:
        logging.warning("RUN_SQL_CAPABILITY_TOKEN is not set: run_sql will refuse every caller")
    url = urlparse(settings.mcp_server_url)
    state = AppState.from_settings(settings)
    server = create_server(state)
    logging.info(
        "governed-lakehouse MCP server on %s (tables: %s)",
        settings.mcp_server_url,
        ", ".join(state.lakehouse.tables),
    )
    server.run(
        transport="streamable-http",
        host=url.hostname or "127.0.0.1",
        port=url.port or 8765,
        streamable_http_path=url.path or "/mcp",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
