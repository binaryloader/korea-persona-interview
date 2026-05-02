# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.2] - 2026-05-02

Documentation patch release. After the multi-provider rollout (v1.1.0) and the `mcp.mode` toggle (v1.1.1) the report footer, PRD, TDD, UI spec, task spec, and SECURITY summary all still claimed `--product` was sent specifically to OpenAI servers. The actual destination is whichever LLM backend the user configures (OpenAI Chat Completions API, Anthropic Messages API, an OpenAI-compatible local server, or the MCP host agent's LLM), governed by `provider`, `base_url`, and `mcp.mode`. No code behavior changes; the regression suite stays at 571 passing tests.

### Fixed

- Report markdown footer no longer claims `--product` is sent to OpenAI servers. The disclaimer paragraph now enumerates the four possible destinations (OpenAI / Anthropic / local LLM / MCP host agent) and the inference-model row drops the hard-coded "(OpenAI Chat Completions API)" suffix so it reflects whatever model id was actually used (`src/report.py` `_render_footer`)

### Documentation

- External transmission disclaimer rephrased to multi-provider phrasing across PRD sections 1, 6.3, and 10.6, TDD section 13, UI spec section 4.5, the task spec T10 row, the SECURITY summary, and the `examples/sample-interview/sample-report.md` artifact. ADR-002 and the v1.0 INDEX revision-log entry are left as-is because they are point-in-time records of when the OpenAI-only assumption was actually true
- v1.0.0 changelog Security entry stating `--product` was "sent to OpenAI servers as part of the Chat Completions request" is left unchanged. That statement was correct as of v1.0.0 and rewriting historical changelog rows would erase the audit trail. This v1.1.2 entry is the canonical correction for current versions

## [1.1.1] - 2026-05-02

Patch release that adds the MCP `mcp.mode` toggle. The MCP server entry point now picks between server-side OpenAI/Anthropic calls (default, immediate usability with mainstream MCP clients) and host-LLM delegation through `sampling/createMessage` (opt-in, no server-side API key). There is no automatic fallback. Test count climbs from 555 to 571.

### Added

- `mcp.mode` config option in `config.yaml`. Two values are accepted and validated against a whitelist: `server` (default) and `sampling`. ADR-004 captures the rationale and supersedes the sampling-only clause of ADR-003. Wraps `_build_backend(config)` in mode dispatch and exposes the new `_backend_label` helper
- `McpConfig` dataclass on `AppConfig`. Frozen, range-checked in `__post_init__`, exposed at `config.mcp.mode`. Conftest `make_app_config` grows an `mcp_mode` parameter for tests
- `backend` field on every MCP tool response envelope. Successful responses carry `"backend": "mcp_server"` or `"backend": "mcp_sampling"`; error envelopes carry the same field whenever the mode is known by the time the error is raised. `load_config` failures (which precede mode resolution) still emit error envelopes without the field
- ADR-004 (`docs/adr/2026-05-02-mcp-mode-toggle.md`) records the decision, the rejected alternatives (automatic fallback, sampling default, deferring MCP entirely), and the follow-up trigger for revisiting the default once sampling-capable clients hit majority share

### Changed

- The MCP server now invokes server-side OpenAI/Anthropic backends by default. Existing mcp.json snippets that include `OPENAI_API_KEY` in their `env` block continue to work without changes. Users who relied on sampling delegation must set `mcp.mode: "sampling"` in `config.yaml` explicitly
- ADR-003 is annotated as superseded for the sampling-only clause only; the multi-provider backend decision (provider toggle, AnthropicBackend, `build_cli_backend` factory) remains in force
- README Integration section rewritten around the toggle. Adds the trade-off matrix split for the two MCP rows, the why-default paragraph, and a sampling-mode mcp.json variant alongside the server-mode example. Configuration table grows the `mcp.mode` row

### Tests

- 571 regression tests (up from 555). New coverage: `mcp.mode` whitelist validation in `McpConfig.__post_init__`, default `server` value, sampling override, server-mode `_build_backend` dispatch (OpenAI and Anthropic), sampling-mode `_build_backend` dispatch (with and without an active session), `_backend_label` helper for both modes, and the response `backend` label invariant on healthcheck, list_personas, interview, and report tool calls in both modes

### Documentation

- README, `docs/INDEX.md`, `docs/prd/korea-persona-interview.md`, `docs/tdd/korea-persona-interview.md` updated for the mode toggle. INDEX revision log gains the v1.1.1 entry
- `examples/mcp/README.md` rewritten with two-mode guidance, a sampling-mode mcp.json snippet, and the backend label invariant

## [1.1.0] - 2026-05-02

Feature release that consumes the entire v1.1.0 backlog (27 items across UX, security, and observability) plus the four LLM-as-judge / streaming / insight-model / structured-summary refactors that were carried over from earlier rounds. Test count climbs from 509 to 555.

### Added

- `--persona-id` selection on `interview` and `list-personas`. Accepts the dataset uuid (`PersonaMeta.persona_id`) directly so the same persona sample can be reused across A/B comparisons. The MCP server exposes the same field as `persona_ids`. When combined with `--filter`, the intersection of filter and ID match is taken; missing IDs raise a `ConfigError` with the missing list
- `--resume` option on `interview`. Reads a previous result JSON, retries only `status=failed` records, and merges them with the existing completed/refused/drift records. The merged JSON gets a fresh timestamp and `meta_extra.previous_run_id` linking back to the source run. If every record is already completed, the LLM call path is skipped entirely
- `--insight-model` CLI flag and `report.insight_model` yaml key. Lets the qualitative-insight LLM call use a different model than the per-question interview calls (for example, run the interview on a small model and the insight on a larger one)
- `ok` field on every `--json` envelope (success: `ok: true`, failure: `ok: false`). The MCP server uses the same shape so external agents can branch on a single key. The error envelope is now `{"ok": false, "error": {"code", "message", "exit_code"}}`
- Anthropic prompt caching via `cache_control: ephemeral` markers on the system prompt. Enabled by default and gated behind `llm.anthropic_cache_control: false` for older API revisions. `cache_creation_input_tokens` and `cache_read_input_tokens` both feed into `TokenUsage.cached_tokens`
- `llm.extra_chat_kwargs` free-form dict that is merged into the OpenAI-compatible chat request body. Lets users forward `chat_template_kwargs` (mlx_lm.server / vLLM Qwen3 thinking toggles) and other backend-specific fields without code changes. Reserved keys (`model`/`messages`/`max_tokens`/`temperature`) are skipped
- Streaming response support (`llm.streaming: true`, opt-in default off). Uses OpenAI Server-Sent Events; the chunked content is reassembled into a single `ChatResponse` and the final `usage` block is mapped via `stream_options.include_usage`
- LLM-as-judge drift refinement (`interview.llm_drift_review: true`, opt-in default off). Heuristic drift detections are reviewed by a single 1-token LLM call; an `ok` verdict clears the drift flag, drift verdict keeps it. Failures fall back conservatively to keeping the drift label
- `acceptable_price_signal` (`cheap`/`fair`/`expensive`/`null`) field on `StructuredSummary`. Filled for every record from qualitative price language so that interviews without a numeric answer still surface price sentiment. `report.estimate_wtp_from_signal: true` opts into LLM-side recommendation prompts that use the signal distribution alongside the explicit numbers
- `mask_persona_id(persona_id)` returning a sha256 hex prefix. Logger calls in `interview.py` and `batch.py` now emit `persona_id_hash` instead of the raw uuid so log forwarders cannot cross-link runs by ID. Demographic fields (age/gender/region) drop from INFO to DEBUG
- Length cap (2000 chars) on `--product` and per-question text plus prompt-injection sanitization. System prompt section markers (`[페르소나 정보]` / `[인터뷰 주제]` / ...) found in user input get a zero-width-space prefix so the model cannot interpret them as new instructions. Over-length input raises `ConfigError` immediately
- Output-path safety guard. `--output` and `--output-dir` outside the current working directory now print a one-line warning. The `outputs/` directory is created with mode `0700` and result JSON / markdown reports are written with mode `0600` so they cannot be read by other users on the host
- Packaged prompt template fallback. `prompts/system_prompt.txt` ships in both the sdist (via `MANIFEST.in`) and the wheel (`src/_prompts/system_prompt.txt`). When the configured path does not exist and matches the default, the loader falls back to `importlib.resources` so `pip install` users do not need to download the template manually
- Tightened drift heuristics for the region, age, and gender axes. All three axes now use the same sentence-level precision as the family-type axis: first-person subject + assertion verb, with explicit negation and third-person guards (`다른 사람들은`, `보통 사람`). The 30-character self-intro window heuristic is gone
- Occupation English whitelist (`interview.occupation_english_whitelist: true`, default on). English tokens that appear in the persona's occupation (`IT 컨설턴트`, `UX 디자이너`) are excluded from the `_english_ratio` denominator so personas can naturally use their own job title without tripping drift detection
- `gender_aliases` reverse normalization in `_build_persona_meta`. The dataset can drift to `남성`/`여성`/`M`/`F` and PersonaMeta still constructs without raising. Reverse alias is hardcoded for the most common variants

### Changed

- `MlxLLMClient` is renamed to `LLMClient` (BREAKING). The legacy alias is gone. New code should still depend on the `LLMBackend` protocol from `llm_backend` so the underlying transport stays swappable
- `StructuredSummary` schema (BREAKING). `willingness_to_pay` now only carries explicit numbers; qualitative price signals live on the new `acceptable_price_signal` field. `SCHEMA_VERSION` bumped 1 -> 2. `_records_from_payload` loads v1 JSON by filling `acceptable_price_signal=None`
- `_run_dry_run` moved out of `main.py` into `src/dry_run.py`. The CLI entry point is now click routing only
- Direct dependencies live in `pyproject.toml` (`[project.dependencies]`) as the single source of truth. `requirements.txt` becomes a one-line `-e .` shim for compatibility with `pip install -r` workflows

### Tests

- 555 regression tests (up from 509). New coverage: `--persona-id` and `--resume` flows, `ok` field on JSON envelopes, Anthropic cache markers and `cache_creation_input_tokens` aggregation, OpenAI streaming SSE parsing, drift heuristic precision (negation guard, third-person exclusion, occupation whitelist), persona ID hashing, prompt-injection sanitization, length-cap enforcement, packaged prompt fallback, and v1/v2 schema backward compatibility

### Documentation

- `config.yaml`: scope header on the `llm:` section listing which fields apply on the CLI entry point only versus which reach the MCP sampling host (only `max_tokens` and `temperature` are forwarded to `sampling/createMessage`; `context_budget` is honored on both paths via the message-history truncator). Scope per-knob comments rewritten so each setting explains the why and the unit
- README: Features list expanded to cover every v1.1.0 addition, Configuration adds a v1.1.0 knob table, Usage Examples adds Scenario F (`--persona-id` A/B with identical persona ids) and Scenario G (`--resume`), Output Format documents the v2 schema (`schema_version: 2`, `acceptable_price_signal`, `meta_extra.previous_run_id`), Limitations spells out the streaming and judge opt-in plus v1/v2 migration, Roadmap rewritten for v1.2.0 candidates
- PRD: section 5.1 documents `--resume` semantics, section 5.2 documents `--persona-id` + filter intersection, section 5.4 covers the v2 schema with `acceptable_price_signal`, section 5.8 covers the precision-tuned drift heuristic and the LLM-as-judge opt-in, section 5.9 CLI table covers every v1.1.0 flag, section 6.1 mentions streaming, section 6.6 covers `persona_id` hashing and demographic-field DEBUG demotion
- TDD: section 1 module responsibilities reflect `src/dry_run.py`, `src/llm_backend.py`, the `LLMClient` rename, and `src/_prompts/` packaged fallback. Section 8 drift heuristics replace the old age/gender/region 30-character window with the sentence-bounded precision regex shared with the cohabitation axis; the LLM-as-judge opt-in is documented. Section 12 LLM HTTP contract covers streaming, `extra_chat_kwargs`, and Anthropic `cache_control` markers; section 13 covers length caps, prompt-injection escapes, persona id hashing, output mode 0700/0600, and `gender_aliases` reverse normalization. Section 16 records the 555-test regression
- UI spec: console samples updated to the v1.1.0 format (token usage one-liner, no cost line)
- Source comments and docstrings tightened across `src/batch.py`, `src/dry_run.py`, `src/interview.py`, `src/llm_client.py`. User-facing Korean strings are preserved; only internal why-comments and developer-facing docstrings are normalized to a consistent SDK voice. `src/dry_run.py` now includes `acceptable_price_signal` in the structured-summary dump so the dry-run output matches the v2 schema

## [1.0.0] - 2026-05-02

First stable release. The previous `0.1.0` line is folded into `1.0.0` because the public surface, multi-provider backend, MCP sampling-only entry point, and 509-test regression are all considered stable for production use.

### Added

- `AnthropicBackend` calling the Anthropic Messages API directly over httpx (no SDK dependency). Token usage maps `input_tokens`, `output_tokens`, and `cache_read_input_tokens` into the existing `TokenUsage` shape
- `llm.provider` config field (`openai` or `anthropic`) plus matching CLI flags `--provider`, `--base-url`, and `--model` on `healthcheck`, `interview`, and `report`. The provider switch also picks up `ANTHROPIC_API_KEY` from the environment or `.env`
- `build_cli_backend(LlmConfig)` factory that returns the correct backend (OpenAI or Anthropic) based on `provider`. The CLI uses this for every entry point so swapping provider only changes one call site

### Changed

- The MCP server is now sampling-only. Every tool call delegates inference to the host agent via `sampling/createMessage`. Running the MCP entry point without an MCP host returns a config error pointing at the CLI (`python main.py interview ...`)
- Source comments and docstrings rewritten at SDK-publication level. Internal change-history notes were removed; public surface keeps API-doc style docstrings only
- `LlmConfig.backend` (the legacy `auto`/`openai`/`mcp_sampling` toggle) is gone. Existing yaml files that still set `llm.backend` are silently dropped during config load to keep upgrades graceful
- Console messages reference "LLM 서버" instead of "OpenAI 서버" so the same copy works for every provider

### Removed

- Cost estimation module and `estimated_cost_usd` field. Token usage display remains. The `src/_pricing.py` per-model price table, the `BatchResultEnvelope.estimated_cost_usd` field, the `meta_extra.estimated_cost_usd` JSON key, the "비용 추정: $X.XXXX" console line, and the report header cost row were removed because per-token list prices change frequently and the tool's estimate diverges from the provider's invoice. The authoritative number is the user's own provider invoice
- `LlmConfig.backend` field and the `select_backend` / `normalize_backend_choice` policy helpers in `llm_backend.py`. Use `build_cli_backend(config.llm)` from the CLI and the explicit `McpSamplingBackend(session)` constructor in the MCP server instead

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
- Token usage tracking. `usage.prompt_tokens_details.cached_tokens` is tracked per response and aggregated into `BatchResultEnvelope.usage`. The same numbers are surfaced in console output, result JSON `meta_extra.usage`, and the report header
- Prompt-caching-friendly system prompt structure. Static prefix is held at the front of the system prompt and the variable parts (persona JSON, product) are placed at the back so OpenAI auto-applies prompt cache on prefixes over 1024 tokens
- Per-process persona-pool cache keyed by (filter, n, seed, field map, gender aliases, province aliases, dataset name, split) so `list-personas` -> `interview` -> `interview --dry-run` on the same parameters reuses the sampled list. Invalidation helper `clear_persona_pool_cache()` is provided for tests
- External system prompt template at `prompts/system_prompt.txt` with `{persona_json}` and `{product}` placeholders. The path is configurable via `interview.system_prompt_path`. The template is loaded lazily and cached per-process by mtime
- Externalized heuristic thresholds and keywords in `config.yaml` `interview.*` (English ratio, CJK ideograph ratio, short-answer trigger, ambiguous keywords, refusal keywords, auto follow-up text, auto follow-up cap) plus `report.*` (cohort minimum cell, top-N default, histogram bins, bar width) and `batch.partial_failure_threshold`. All thresholds are range-validated in `__post_init__`
- Structured JSON Lines logging (`src/logging_setup.py`) with `request_id`, secret masking, and `outputs/logs/run_*.jsonl` output. Logs flow to stderr so they do not pollute the stdio JSON-RPC channel
- Layered configuration loader (`src/config.py`). Precedence is built-in defaults, then `config.yaml`, then CLI options. Secrets come from the environment (`OPENAI_API_KEY`, `KPI_OPENAI_API_KEY`) and `KPI_OUTPUT_DIR` is honored for test/CI isolation. `.env` files at the project root are auto-loaded with stdlib parsing and `setdefault` semantics so existing environment variables are never overridden
- OpenAI Chat Completions backend via async httpx (`src/llm_client.py`). Default model `gpt-4o-mini`, configurable via `llm.model` or one-off `--model`. Default base URL `https://api.openai.com/v1`. The official `openai` SDK is intentionally not used, see [docs/adr/2026-05-02-openai-backend-migration.md](docs/adr/2026-05-02-openai-backend-migration.md)
- 509 regression tests (`tests/`) covering config, filter DSL, persona loader, LLM client, interview session, persona drift, batch runner, report quant, MCP dispatch, error messages, logging, and CLI integration. The OpenAI API is mocked with `pytest-httpx` and the dataset is mocked with monkeypatch fixtures so the suite runs offline
- Drop-in MCP configuration examples for Claude Code and Cursor under [examples/mcp/](examples/mcp/)
- Reproducible install via `requirements.lock` and `requirements-dev.lock` generated by `uv pip compile`. `pyproject.toml` carries PEP 621 metadata and console-script registrations and is kept in sync with `requirements.txt`
- Documentation tree under `docs/` with PRD, TDD, two ADRs (multi-turn strategy and OpenAI backend migration), UI flow, task breakdown, and v1.1.0 backlog

### Security

- API keys are read from the environment or a project-root `.env` file only. Keys are never written to logs, result JSON, or the markdown report
- The `--product` text and persona metadata used for each interview are sent to OpenAI servers as part of the Chat Completions request. The README and ADR-002 document this explicitly
- No external telemetry beyond the OpenAI API call and the initial Hugging Face dataset download
- `aiohttp` is bound to `>=3.13.5,<3.14` to address GHSA-9548-qrrj-x5pj. The bound is held under 3.14 because the upstream patch is only available in 3.14+, which has not shipped a stable release yet. The bound and lockfile are scheduled to be refreshed when 3.14 lands

### Dataset and License

- Dataset: [nvidia/Nemotron-Personas-Korea](https://huggingface.co/datasets/nvidia/Nemotron-Personas-Korea), CC BY 4.0
- Default model: `gpt-4o-mini` (configurable)
- License: MIT (see [LICENSE](LICENSE))

[Unreleased]: https://github.com/binaryloader/korea-persona-interview/compare/v1.1.2...HEAD
[1.1.2]: https://github.com/binaryloader/korea-persona-interview/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/binaryloader/korea-persona-interview/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/binaryloader/korea-persona-interview/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/binaryloader/korea-persona-interview/releases/tag/v1.0.0
