"""durable_agent: a Claude text-to-SQL analyst run as a Temporal workflow.

Package layout (filled in milestone by milestone):

- ``config``      typed settings loaded from environment / ``.env``
- ``lakehouse``   DuckDB connection over Delta Lake tables + schema introspection   (M1)
- ``seed``        synthetic retail dataset generator                                 (M1)
- ``risk``        deterministic SQL risk classification with sqlglot                 (M1)
- ``mcp_server``  MCP tools: get_schema, search_governance, validate_sql, run_sql     (M2)
- ``retrieval``   LanceDB index over governance/*.md                                  (M2)
- ``llm``         Anthropic client wrappers, model routing, response cache            (M3)
- ``activities``  Temporal activities (one per agent step)                            (M3)
- ``workflows``   AnalystWorkflow with approval signal and state queries              (M3)
- ``worker``      Temporal worker entrypoint                                          (M3)
- ``cli``         start / approve / status / demo-crash                               (M3)
"""

__version__ = "0.1.0"
