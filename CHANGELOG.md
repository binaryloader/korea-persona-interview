# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `AnthropicBackend` calling the Anthropic Messages API directly over httpx (no SDK dependency). Token usage maps `input_tokens`, `output_tokens`, and `cache_read_input_tokens` into the existing `TokenUsage` shape for cost-accounting parity with OpenAI
- `llm.provider` config field (`openai` or `anthropic`) plus matching CLI flags `--provider`, `--base-url`, and `--model` on `healthcheck`, `interview`, and `report`. The provider switch also picks up `ANTHROPIC_API_KEY` from the environment or `.env`
- Per-model price entries for `claude-haiku-4-5`, `claude-sonnet-4-5`, and `claude-opus-4-5` in `src/_pricing.py`. Numbers are estimates from the Anthropic pricing page; callers continue to surface "estimated" wording
- `build_cli_backend(LlmConfig)` factory that returns the correct backend (OpenAI or Anthropic) based on `provider`. The CLI uses this for every entry point so swapping provider only changes one call site

### Changed

- The MCP server is now sampling-only. Every tool call delegates inference to the host agent via `sampling/createMessage`. Running the MCP entry point without an MCP host returns a config error pointing at the CLI (`python main.py interview ...`)
- Source comments and docstrings rewritten at SDK-publication level. Internal change-history notes were removed; public surface keeps API-doc style docstrings only
- `LlmConfig.backend` (the legacy `auto`/`openai`/`mcp_sampling` toggle) is gone. Existing yaml files that still set `llm.backend` are silently dropped during config load to keep upgrades graceful
- Console messages reference "LLM 서버" instead of "OpenAI 서버" so the same copy works for every provider

### Removed

- `LlmConfig.backend` field and the `select_backend` / `normalize_backend_choice` policy helpers in `llm_backend.py`. Use `build_cli_backend(config.llm)` from the CLI and the explicit `McpSamplingBackend(session)` constructor in the MCP server instead

## [0.1.0] - 2026-05-02

Initial public release of korea-persona-interview, a CLI for running synthetic Korean persona interviews on top of the OpenAI Chat Completions API and the NVIDIA Nemotron-Personas-Korea dataset (CC BY 4.0, about 1M Korean synthetic personas).

### Added

- click-based CLI with four subcommands (`healthcheck`, `list-personas`, `interview`, `report`) and exit codes 0/1/2/3 + 130 for SIGINT
- Multi-turn interview engine (`src/interview.py`) with persona drift detection (English ratio, CJK ideograph ratio, age/gender/region/family-type contradiction), short-answer auto follow-up, refusal detection, and token loop guard
- Single-turn mode (`--single-turn`) that bundles every question into one chat call to roughly halve prompt tokens at scale. Auto follow-up is disabled in this mode and parse failures fall back to storing the full text on the last question with `flags.parse_failed=true`
- Async batch runner (`src/batch.py`) with concurrency 1-10 (default 4), tqdm progress bar, SIGINT partial save, and exit code 3 partial-failure detection at `batch.partial_failure_threshold` (default 0.5)
- Filter DSL with `age`, `gender`, `region`, `subregion`, `occupation_keyword` keys plus AND/OR combination and 17-province aliases, exposed in `list-personas` and `interview`
- Reproducible sampling via `--seed` (same seed + same filter + same dataset version returns the same personas)
- Automatic markdown report generation after every interview run (toggle off with `--no-report`). Report includes intent share, willingness-to-pay median + IQR, top-N rejection reasons, cohort table (age x region x gender, masked under `report.cohort_min_cell` of 3), shared reactions, 5-10 actionable insights, cohort qualitative differences, excluded record counts, and a synthetic-data disclaimer
- `--json` root mode that emits a single JSON document on stdout for shell scripts and external agents. Disables tqdm, ANSI color, and Korean labels. Errors emit `{"error": {...}}` with non-zero exit
- MCP (Model Context Protocol) server (`src/mcp_server.py`) exposing the four CLI commands as tools to Claude Code, Cursor, and Codex over stdio JSON-RPC. Console scripts `kpi` and `kpi-mcp-server` are registered via `pyproject.toml`
- Token usage and USD cost estimation. `usage.prompt_tokens_details.cached_tokens` is tracked per response and aggregated into `BatchResultEnvelope.usage`. `src/_pricing.py` carries the per-model price table (OpenAI list prices as of 2026-05) with cached_tokens at 50% discount and a fallback estimate for unknown models. Same numbers are surfaced in console output, result JSON `meta_extra.usage`/`meta_extra.estimated_cost_usd`, and the report header
- Prompt-caching-friendly system prompt structure. Static prefix is held at the front of the system prompt and the variable parts (persona JSON, product) are placed at the back so OpenAI auto-applies prompt cache on prefixes over 1024 tokens
- Per-process persona-pool cache keyed by (filter, n, seed, field map, gender aliases, province aliases, dataset name, split) so `list-personas` -> `interview` -> `interview --dry-run` on the same parameters reuses the sampled list. Invalidation helper `clear_persona_pool_cache()` is provided for tests
- External system prompt template at `prompts/system_prompt.txt` with `{persona_json}` and `{product}` placeholders. The path is configurable via `interview.system_prompt_path`. The template is loaded lazily and cached per-process by mtime
- Externalized heuristic thresholds and keywords in `config.yaml` `interview.*` (English ratio, CJK ideograph ratio, short-answer trigger, ambiguous keywords, refusal keywords, auto follow-up text, auto follow-up cap) plus `report.*` (cohort minimum cell, top-N default, histogram bins, bar width) and `batch.partial_failure_threshold`. All thresholds are range-validated in `__post_init__`
- Structured JSON Lines logging (`src/logging_setup.py`) with `request_id`, secret masking, and `outputs/logs/run_*.jsonl` output. Logs flow to stderr so they do not pollute the stdio JSON-RPC channel
- Layered configuration loader (`src/config.py`). Precedence is built-in defaults, then `config.yaml`, then CLI options. Secrets come from the environment (`OPENAI_API_KEY`, `KPI_OPENAI_API_KEY`) and `KPI_OUTPUT_DIR` is honored for test/CI isolation. `.env` files at the project root are auto-loaded with stdlib parsing and `setdefault` semantics so existing environment variables are never overridden
- OpenAI Chat Completions backend via async httpx (`src/llm_client.py`). Default model `gpt-4o-mini`, configurable via `llm.model` or one-off `--model`. Default base URL `https://api.openai.com/v1`. The official `openai` SDK is intentionally not used, see [docs/adr/2026-05-02-openai-backend-migration.md](docs/adr/2026-05-02-openai-backend-migration.md)
- 470 regression tests (`tests/`) covering config, filter DSL, persona loader, LLM client, interview session, persona drift, batch runner, report quant, MCP dispatch, error messages, logging, and CLI integration. The OpenAI API is mocked with `pytest-httpx` and the dataset is mocked with monkeypatch fixtures so the suite runs offline
- Drop-in MCP configuration examples for Claude Code and Cursor under [examples/mcp/](examples/mcp/)
- Reproducible install via `requirements.lock` and `requirements-dev.lock` generated by `uv pip compile`. `pyproject.toml` carries PEP 621 metadata and console-script registrations and is kept in sync with `requirements.txt`
- Documentation tree under `docs/` with PRD, TDD, two ADRs (multi-turn strategy and OpenAI backend migration), UI flow, task breakdown, and v1.1 backlog

### Security

- API keys are read from the environment or a project-root `.env` file only. Keys are never written to logs, result JSON, or the markdown report
- The `--product` text and persona metadata used for each interview are sent to OpenAI servers as part of the Chat Completions request. The README and ADR-002 document this explicitly
- No external telemetry beyond the OpenAI API call and the initial Hugging Face dataset download
- `aiohttp` is bound to `>=3.13.5,<3.14` to address GHSA-9548-qrrj-x5pj. The bound is held under 3.14 because the upstream patch is only available in 3.14+, which has not shipped a stable release yet. The bound and lockfile are scheduled to be refreshed when 3.14 lands

### Dataset and License

- Dataset: [nvidia/Nemotron-Personas-Korea](https://huggingface.co/datasets/nvidia/Nemotron-Personas-Korea), CC BY 4.0
- Default model: `gpt-4o-mini` (configurable)
- License: MIT (see [LICENSE](LICENSE))

[Unreleased]: https://github.com/binaryloader/korea-persona-interview/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/binaryloader/korea-persona-interview/releases/tag/v0.1.0
