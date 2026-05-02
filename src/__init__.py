"""korea-persona-interview: synthetic Korean persona interview pipeline.

Public surface:

- ``InterviewSession`` and ``run_interview`` — single-persona interview engine.
- ``run_batch`` and ``BatchResultEnvelope`` — concurrent batch runner.
- ``generate_report`` and ``ReportOptions`` — markdown report renderer.
- ``OpenAIBackend``, ``AnthropicBackend``, ``McpSamplingBackend`` — LLM
  backends that all satisfy the ``LLMBackend`` protocol.
- ``load_config`` and ``AppConfig`` — layered configuration loader.

CLI entry points are exposed via ``main.py`` (``kpi`` console script) and the
MCP stdio server lives in ``src.mcp_server`` (``kpi-mcp-server``).
"""

__version__ = "1.0.0"
