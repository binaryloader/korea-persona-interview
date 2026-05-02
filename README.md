# korea-persona-interview

[![CI](https://github.com/binaryloader/korea-persona-interview/actions/workflows/test.yml/badge.svg)](https://github.com/binaryloader/korea-persona-interview/actions/workflows/test.yml)

A field-ready CLI for running synthetic Korean persona interviews on top of OpenAI, Anthropic Claude, or any OpenAI-compatible local LLM (mlx_lm.server, vLLM, llama.cpp). Pair the NVIDIA Nemotron-Personas-Korea dataset (CC BY 4.0, about 1M Korean synthetic personas) with the model of your choice to pressure-test product ideas, interview guides, and persona hypotheses before recruiting real participants.

The tool ships four CLI subcommands (`healthcheck`, `list-personas`, `interview`, `report`), a JSON output mode for machine-to-machine use, and a Model Context Protocol (MCP) entry point that runs in either MCP server mode (server-side OpenAI/Anthropic calls) or MCP orchestrator mode (the host agent's sub-agent does the LLM work).

## Features

- Multi-turn interviews with 1M+ Korean synthetic personas (NVIDIA Nemotron-Personas-Korea, CC BY 4.0)
- Three inference targets: OpenAI Chat Completions API, Anthropic Messages API, and any OpenAI-compatible local server (mlx_lm.server, vLLM, llama.cpp)
- `--provider openai|anthropic`, `--base-url URL`, `--model MODEL_ID` CLI flags for one-off backend overrides
- `--persona-id` to pin a specific persona (or list of personas) by uuid for A/B comparisons; `--resume PATH` to re-run only the failed records of a previous batch and merge them with the existing completed/refused/drift entries
- `--insight-model` (and `common.report.insight_model` yaml key) to run the per-question interview on a small model and the qualitative-insight call on a larger one
- OpenAI streaming response support (`llm.streaming: true`, opt-in default off) for faster time-to-first-token
- Anthropic prompt caching via `cache_control: ephemeral` markers on the system prompt (`llm.anthropic_cache_control: true`, default on); `cache_creation_input_tokens` and `cache_read_input_tokens` both feed into the same `cached_tokens` counter
- `llm.extra_chat_kwargs` free-form dict forwarded into the OpenAI request body for backend-specific extensions (e.g. `chat_template_kwargs.enable_thinking` on mlx_lm.server / vLLM Qwen3)
- LLM-as-judge drift refinement (`heuristics.llm_drift_review: true`, opt-in default off) that revisits heuristic drift detections with a single 1-token verdict call to clear false positives
- `acceptable_price_signal` (`cheap`/`fair`/`expensive`/`null`) on every structured summary so price sentiment lands on every record even when the persona never names a number; `common.report.estimate_wtp_from_signal` opts into a recommendation prompt that uses the signal distribution alongside explicit numbers
- MCP entry point (`python -m src.mcp_server`) that exposes interview-pipeline tools to Claude Code, Cursor, and Codex. The `mcp.mode` toggle picks between two inference paths: `server` (default) calls OpenAI/Anthropic directly server-side using the same `LlmConfig` the CLI uses, and `orchestrator` exposes data and prompt helpers that the host agent's sub-agent uses to run interviews with its own LLM (no server-side API key required)
- Automatic markdown report after every interview run (toggle off with `--no-report` for JSON-only pipelines)
- `--json` root mode that emits a single JSON document on stdout for shell scripts and external agents; every JSON envelope (CLI and MCP) carries an explicit `ok: true|false` field for one-key branching
- Single-turn mode (`--single-turn`) that bundles every question into one chat call to cut tokens at scale
- Async batch runner with concurrency 1-10 (default 4), tqdm progress, SIGINT partial save, and exit-code 3 partial-failure detection
- Token usage (prompt / completion / cached) printed at the end of every run, also written into the result JSON and report header
- Prompt-caching-friendly system prompt structure. OpenAI cached input tokens and Anthropic `cache_read_input_tokens` are tracked separately
- Per-process persona-pool cache so `list-personas` -> `interview` -> `interview --dry-run` on the same filter/seed reuses the sampled list
- External system-prompt template (`prompts/system_prompt.txt`) for domain tone customization without code changes; pip-installed users get a packaged fallback at `src/_prompts/system_prompt.txt`
- Externalized heuristic thresholds (English ratio, ambiguous keywords, refusal keywords, follow-up text, cohort masking, partial failure ratio, occupation English whitelist) in `config.yaml`
- Filter DSL with `age`, `gender`, `region`, `subregion`, `occupation_keyword` keys plus AND/OR combination and 17-province aliases
- Persona drift detection that uses sentence-bounded first-person assertions for the gender/age/region/family-type axes (with negation guards and third-person exclusion) and an English-ratio safety net that excludes the persona's own occupation tokens
- Reproducible sampling via `--seed`. Same seed plus same filter plus same dataset version returns the same personas
- Operational hardening: persona ids are sha256-masked in logs (demographic fields drop to DEBUG), the `outputs/` directory is created with mode 0700 and result files with 0600, and `--product` plus per-question text are length-capped at 2000 characters with system-prompt section markers escaped to block prompt injection
- No external telemetry. Outbound calls go only to the configured LLM endpoint and (on first run) Hugging Face Hub for the dataset

## Requirements

- Python 3.12 (pinned in `.python-version`)
- [uv](https://docs.astral.sh/uv/) package manager
- An API key for the provider you plan to use:
  - `OPENAI_API_KEY` for `provider=openai` (default). Get one at https://platform.openai.com/api-keys
  - `ANTHROPIC_API_KEY` for `provider=anthropic`. Get one at https://console.anthropic.com/
  - For local LLMs (mlx_lm.server, vLLM, llama.cpp) keep `provider=openai` and use any non-empty value (the local server ignores it)
- Internet access for the LLM API call and the first dataset download (about 1M records, cached afterwards under `~/.cache/huggingface`)
- macOS, Linux, and Windows are all supported. There is no Apple Silicon, GPU, or local-runtime requirement (the local-LLM path is opt-in)

## Dependencies

Direct runtime dependencies live in `pyproject.toml` (`[project.dependencies]`). The `requirements.txt` shim simply forwards to it via `-e .` for compatibility with `pip install -r` workflows.

- `httpx` - async HTTP client for the OpenAI API
- `datasets` - Hugging Face loader for `nvidia/Nemotron-Personas-Korea`
- `pyyaml` - `config.yaml` loader
- `tqdm` - batch progress bar
- `click` - CLI framework
- `mcp` - Python SDK for the Model Context Protocol server
- `aiohttp` (transitive via `datasets`) - explicitly bounded to address GHSA-9548-qrrj-x5pj. Kept under 3.14 because the upstream patch is only available in 3.14+, which has not shipped a stable release yet. Refresh the bound and recompile the lockfile when 3.14 lands

The official `openai` SDK is intentionally not used. Calls go to `https://api.openai.com/v1/chat/completions` directly via httpx so the project keeps its dependency tree small and owns the retry, timeout, and logging policy. See [docs/adr/2026-05-02-openai-backend-migration.md](docs/adr/2026-05-02-openai-backend-migration.md) for the rationale.

Test-only dependencies live in `requirements-dev.txt` and the `dev` extra of `pyproject.toml` (`pytest`, `pytest-asyncio`, `pytest-httpx`).

The transitive dependency tree is fully resolved in `requirements.lock` and `requirements-dev.lock`, generated by `uv pip compile`. Production deploys must install from the lockfiles to keep the resolved graph identical across environments (`dependency.md` section 2).

## Installation

Create the virtual environment with uv and install dependencies. The `.python-version` file pins Python 3.12, so `uv venv` picks the right interpreter automatically.

Reproducible installs use the lockfiles (frozen graph). Recompile the lockfiles only after editing `requirements*.txt`.

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip sync requirements.lock requirements-dev.lock
```

Editing direct dependencies follows a two-step flow.

```bash
uv pip compile requirements.txt -o requirements.lock
uv pip compile requirements-dev.txt -o requirements-dev.lock
```

### Editable install with console scripts (optional)

To run the CLI as `kpi` and the MCP server as `kpi-mcp-server` from anywhere instead of `python main.py` and `python -m src.mcp_server`, install the project in editable mode after the dependency sync above.

```bash
uv pip install -e .
```

After this you can call `kpi healthcheck`, `kpi interview ...`, `kpi-mcp-server`, and so on. The editable install reuses the same `[project.dependencies]` graph that `requirements.txt` forwards to, so no duplicate dependency tree is created. Skip this step if you only need to run via `python main.py`.

Plain pip works too if you cannot use uv.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Quick Start

Five commands take you from a fresh checkout to a finished report. Each step is independently verifiable. The first interview run downloads the dataset, which takes 5-10 minutes. Subsequent runs start in under 30 seconds because `datasets` caches the parquet files under `~/.cache/huggingface`.

```bash
export OPENAI_API_KEY=sk-...
python main.py healthcheck
python main.py list-personas --filter "age:25-39,region:서울특별시" --limit 20
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원, 주 2회 배송" --filter "age:25-39,region:서울특별시" --n 10 --questions "이 서비스 쓰실 의향 있나요?" "월 얼마면 적당한가요?" "거절한다면 왜요?"
python main.py report outputs/interview_korea-persona-interview_20260502_120000.json
```

To use Claude instead, set `ANTHROPIC_API_KEY` and pass `--provider anthropic`.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python main.py healthcheck --provider anthropic --model claude-haiku-4-5
python main.py interview --provider anthropic --model claude-haiku-4-5 --product "..." --questions "..." --n 10
```

To use a local OpenAI-compatible server (mlx_lm.server, vLLM, llama.cpp), keep `provider=openai` and override `--base-url`. Any non-empty `OPENAI_API_KEY` works; local servers ignore the value.

```bash
export OPENAI_API_KEY=local
python main.py healthcheck --base-url http://localhost:8080/v1 --model llama-3-8b
python main.py interview --base-url http://localhost:8080/v1 --model llama-3-8b --product "..." --questions "..." --n 10
```

The `interview` command auto-generates the markdown report after the JSON is saved (default `--report`). The standalone `python main.py report ...` step in the Quick Start is shown for completeness; you only need it if you used `--no-report`, edited the JSON, or want to regenerate the report with different `--top-n` or `--include-drift` settings.

A `.env` file at the project root with `OPENAI_API_KEY=sk-...` (or `ANTHROPIC_API_KEY=sk-ant-...` for `provider=anthropic`) is picked up automatically. Existing shell environment variables take precedence over `.env`.

### Tip: ask explicit value-pricing questions

`willingness_to_pay` is filled in only when the persona names a specific number. If you want to maximize the explicit-number rate, ask a direct value-pricing question that anchors the answer to a number, for example:

- "본인은 월 얼마면 가입하시겠어요?" (anchored to a monthly subscription)
- "월 39,900원이면 가입할 의향이 있으세요? 아니면 얼마면 적당할까요?" (counter-offer prompt)
- "비슷한 서비스에 한 달에 얼마까지 쓸 수 있어요?" (ceiling probe)

Open-ended price questions ("이 서비스 어떻게 보세요?") often only return a qualitative signal (`acceptable_price_signal`), which is filled for every record but does not produce a `willingness_to_pay` integer. Use the qualitative signal distribution and the explicit numbers together rather than relying only on the median price.

## Usage Examples

The five scenarios below cover the most common research goals. Each scenario lists the commands and the expected outcome.

### Scenario A: validate a product idea

You are validating a meal-kit subscription targeted at single-person households in Seoul.

```bash
export OPENAI_API_KEY=sk-...
python main.py healthcheck
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원, 주 2회 배송" --filter "age:25-39,region:서울특별시" --n 10 --seed 42 --questions "이 서비스 쓰실 의향 있나요?" "월 얼마면 적당한가요?" "거절한다면 왜요?"
```

Expected outcome: a markdown report with intent share (positive/neutral/negative), willingness-to-pay median plus IQR, top rejection reasons, and 5-10 actionable insights for the next round.

### Scenario B: A/B test product copy

You want to compare two product descriptions on the same persona sample. Reuse the same `--seed` and `--filter` so only the `--product` text changes.

```bash
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬, 월 39,900원" --filter "age:25-39,region:서울특별시" --n 10 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/copy-a/
python main.py interview --product "주말에 받는 1주일치 한식 반찬 박스, 월 39,900원" --filter "age:25-39,region:서울특별시" --n 10 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/copy-b/
```

Expected outcome: two reports with the same persona sample but different copy. Compare the intent share and the rejection reasons to see which message lands better. The seed pin removes persona-sampling noise so the delta you see is mostly about copy.

### Scenario C: cohort comparison

You want to see how the 20s and 30s cohorts respond to the same idea.

```bash
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬 정기배송" --filter "age:20-29" --n 15 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/cohort-20s/
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬 정기배송" --filter "age:30-39" --n 15 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/cohort-30s/
```

Expected outcome: two reports per cohort. The cohort intent table inside each report further splits by region and gender, so you can see whether the 20s/30s gap holds across all regions or comes from one segment.

### Scenario D: drive the tool from an external agent (Claude Code MCP)

You want Claude Code to run an interview from a chat prompt instead of a shell. Register the MCP server as shown in [Integration with External Agents](#integration-with-external-agents) and ask the agent in plain Korean.

```text
1인 가구 대상 반찬 정기배송 (월 39,900원)을 25-39세 서울 30명에게 인터뷰 돌리고 리포트까지 만들어 줘. seed는 42로 고정.
```

Expected outcome: the agent calls the `interview` tool, then `report`, and returns the path to the generated markdown plus a summary that includes intent share and token usage.

### Scenario E: large-scale screen with single-turn mode

You want to screen 100 personas in a single sweep before drilling deeper. Single-turn mode bundles every question into one chat call, which roughly halves the prompt tokens versus multi-turn.

```bash
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원" --filter "age:20-49" --n 100 --seed 42 --concurrency 8 --single-turn --questions "이 서비스 쓸 의향?" "월 얼마면 적당?" "거절 사유?"
```

Expected outcome: a single 100-persona JSON plus markdown report. The auto follow-up is disabled in single-turn mode, so plan your questions to be self-contained.

### Scenario F: A/B copy comparison on the exact same personas

You want to remove sampling noise entirely. Pin the same persona ids across the two runs by extracting them from the first batch and replaying them on the second.

```bash
# Run A
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬, 월 39,900원" --filter "age:25-39,region:서울특별시" --n 10 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/copy-a/

# Pull the persona ids out
python -c "import json,sys; d=json.load(open(sys.argv[1])); print('\n'.join(r['persona_id'] for r in d['records']))" outputs/copy-a/interview_*.json > /tmp/persona_ids.txt

# Run B with --persona-id replayed
xargs -I {} echo --persona-id {} < /tmp/persona_ids.txt | xargs python main.py interview --product "주말에 받는 1주일치 한식 반찬 박스, 월 39,900원" --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/copy-b/
```

Expected outcome: both runs interview the exact same persona ids, so the only variable is the product copy. The qualitative-insight diff is more trustworthy than seed-only A/B.

### Scenario G: resume after a partial-failure exit

A 30-person batch hit rate-limit storms and the run exited with code 3. Re-run only the failed records on top of the previous JSON.

```bash
python main.py interview --product "..." --filter "..." --n 30 --seed 42 --questions "..." --resume outputs/interview_korea-persona-interview_20260502_120000.json
```

Expected outcome: a fresh result JSON whose completed/refused/drift records come from the original run and whose `failed` records are replaced by retried records. `meta_extra.previous_run_id` is set to the original `interview_id` so the two runs can be linked.

## CLI Reference

### Subcommands

| Command | Description | Exit codes |
| --- | --- | --- |
| `healthcheck` | Verify OpenAI API reachability and model availability | 0 ok, 1 missing key / 401 / 429 / unreachable |
| `list-personas` | Preview personas matching a filter | 0 ok, 2 no match |
| `interview` | Run a batch interview, save JSON, auto-generate report | 0 ok, 1 server error, 2 sample shortfall, 3 partial failure |
| `report` | Generate a markdown report from an interview JSON | 0 ok, 1 input error, 2 no valid records |

Exit code 130 is reserved for `SIGINT` (Ctrl-C). The first interrupt saves a partial JSON to `outputs/`. The second interrupt terminates immediately.

### Root options

These apply to every subcommand and must be placed before the subcommand name.

| Option | Default | Description |
| --- | --- | --- |
| `--config PATH` | `config.yaml` in cwd | Override the config file path |
| `--no-color` | off | Disable ANSI color output (also honors `NO_COLOR` env) |
| `--log-level LEVEL` | `INFO` (from yaml) | Set log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `--json` | off | Emit a single JSON document on stdout. Disables tqdm, color, and Korean labels. Errors land as `{"error": {...}}` with non-zero exit |

### `healthcheck` options

| Option | Default | Description |
| --- | --- | --- |
| `--provider {openai,anthropic}` | from `llm.provider` | LLM provider. Local LLMs use `openai` plus `--base-url` |
| `--base-url URL` | from `llm.base_url` | LLM server base URL (use `http://localhost:PORT/v1` for local LLMs) |
| `--model MODEL_ID` | from `llm.model` | One-shot model override |

### `list-personas` options

| Option | Default | Description |
| --- | --- | --- |
| `--filter SPEC` | none | Filter DSL (see Filter DSL below) |
| `--persona-id UUID` | none, repeatable | Pin specific persona ids by uuid. Disables `--limit` and `--seed` randomization. Combine with `--filter` for an intersection |
| `--limit N` | `20` | Number of personas to print |
| `--seed N` | `42` | Sampling seed |

### `interview` options

| Option | Default | Description |
| --- | --- | --- |
| `--product TEXT` | required | One-line product description (max 2000 chars) |
| `--questions TEXT` | required, repeatable | Each question is one `--questions` flag (max 2000 chars each) |
| `--filter SPEC` | none | Filter DSL |
| `--persona-id UUID` | none, repeatable | Pin specific persona ids by uuid. Disables `--n` and `--seed` randomization. Combine with `--filter` for an intersection |
| `--n N` | `10` | Number of personas |
| `--seed N` | `42` | Sampling seed |
| `--concurrency N` | `4` | Async concurrency, range 1-10 |
| `--persona-fields LIST` | `summary` | Comma-separated toggles: `summary`, `professional`, `sports`, `arts`, `travel`, `culinary`, `family` |
| `--follow-up TEXT` | none, repeatable | Common follow-up question for every persona |
| `--single-turn` | off | Bundle every question into one chat call. Auto follow-up disabled |
| `--dry-run` | off | Run one persona, print to console, write neither JSON nor report |
| `--output DIR` | `outputs/` | Result JSON directory |
| `--report / --no-report` | `--report` | Auto-generate the markdown report after the interview. `--no-report` keeps JSON only |
| `--resume PATH` | none | Re-run only the `failed` records of a previous result JSON. The merged JSON gets a fresh timestamp and `meta_extra.previous_run_id` |
| `--provider {openai,anthropic}` | from `llm.provider` | LLM provider |
| `--base-url URL` | from `llm.base_url` | LLM server base URL |
| `--model MODEL_ID` | from `llm.model` | One-shot model override |

### `report` options

| Option | Default | Description |
| --- | --- | --- |
| `RESULT_PATH` | required (positional) | Path to an interview JSON |
| `--top-n N` | `10` | Number of top rejection reasons |
| `--include-drift` | off | Include `status: drift` records in quantitative aggregation |
| `--output-dir DIR` | next to input JSON | Where to save the markdown report |
| `--provider {openai,anthropic}` | from `llm.provider` | LLM provider |
| `--base-url URL` | from `llm.base_url` | LLM server base URL |
| `--model MODEL_ID` | from `llm.model` | One-shot model override for the qualitative-insight call |
| `--insight-model MODEL_ID` | from `common.report.insight_model` or `--model` | Use a different model for the qualitative-insight call only. Useful when interviews run on a small model and you want a larger model for the synthesis step |

### Filter DSL

Filters use `key:value` pairs separated by commas. Different keys combine with AND, repeated keys combine with OR.

- `age:25-39` (range), `age:30` (exact)
- `gender:F`, `gender:M`, `gender:여자`, `gender:남자`, `gender:여성`, `gender:남성` (all map to `여자`/`남자`)
- `region:서울특별시`, `region:서울` (17 provinces, with full-name aliases)
- `subregion:강남구` (suffix match against the `district` column)
- `occupation_keyword:개발자` (substring match)

Examples.

```text
--filter "age:25-39,region:서울특별시"                    # 25-39 AND Seoul
--filter "age:25-39,region:서울특별시,region:경기도"      # 25-39 AND (Seoul OR Gyeonggi)
--filter "gender:F,occupation_keyword:디자이너"          # female AND occupation contains 디자이너
```

## Output Format

### Result JSON

Interview results are written to `outputs/interview_{slug}_{YYYYMMDD_HHMMSS}.json`. The envelope contains the run metadata (`interview_id`, `slug`, `product`, `model`, `seed`, `config_snapshot`) plus a `records` array. Each record holds `persona_meta`, the multi-turn `messages`, per-question `raw_responses`, a `structured_summary`, and `flags`.

| Field | Type | Notes |
| --- | --- | --- |
| `interview_id` | string (uuid) | One per run |
| `slug` | string | Always `korea-persona-interview` |
| `schema_version` | int | `2` since v1.1.0 (was `1` in v1.0.x). Readers can branch on this to handle the `acceptable_price_signal` field |
| `model` | string | Resolved model id (e.g. `gpt-4o-mini`) |
| `seed` | int | Sampling seed |
| `meta_extra.usage` | object | Aggregated `prompt_tokens`, `completion_tokens`, `total_tokens`, `cached_tokens` |
| `meta_extra.previous_run_id` | string or absent | Set when the run came from `--resume`. Holds the source run's `interview_id` |
| `records[].status` | enum | `completed` / `refused` / `failed` / `drift` |
| `records[].structured_summary` | object or null | `intent`, `acceptable_price_signal`, `willingness_to_pay`, `willingness_to_pay_currency`, `rejection_reasons`, `one_line`. In schema v2 `willingness_to_pay` is filled only for explicit numbers; qualitative price sentiment lives on `acceptable_price_signal` (`cheap`/`fair`/`expensive`/`null`) |
| `records[].flags` | object | `persona_drift`, `auto_follow_up_used`, `refusal_detected`, `truncated`, `parse_failed` |

See `docs/prd/korea-persona-interview.md` section 5.4 for the full schema. v1 JSON files (with `schema_version: 1`) load fine on v1.1.0+: the loader fills `acceptable_price_signal=null` for those records.

### Markdown report

The report subcommand emits `outputs/report_{slug}_{YYYYMMDD_HHMMSS}.md` next to the input JSON by default. The section tree is below.

```text
# 가상 인터뷰 리포트: {product}
| meta table | model, seed, persona counts, dataset, usage |

## 1. 정량 지표
### 1.1. 의향률          # intent share table + bar chart
### 1.2. 가격 수용가     # WTP median, IQR, histogram
### 1.3. 거절 사유 빈도  # top-N rejection reasons table
### 1.4. 코호트별 의향률 # age x region x gender, masked under min cell size

## 2. 정성 인사이트
### 2.1. 공통 반응       # up to 5 shared reactions
### 2.2. 인사이트        # 5-10 actionable insights
### 2.3. 코호트 차이     # cohort-level qualitative differences

## 3. 제외 record 요약   # excluded record counts and reasons

## 4. 한계와 출처        # synthetic-data caveat, dataset citation, model id
```

## Configuration

Settings policy is `secrets via env, defaults via yaml, one-off overrides via CLI`. Configuration is layered with the following precedence (later overrides earlier).

- Built-in defaults
- `config.yaml`
- CLI options (`--model`, `--concurrency`, etc.)

The only environment variables this tool reads are secrets and the output directory.

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI API key (used when `provider=openai`) |
| `ANTHROPIC_API_KEY` | Anthropic API key (used when `provider=anthropic`) |
| `KPI_OUTPUT_DIR` | Output directory override (kept for test/CI isolation) |

Change the model or provider with `--model gpt-4o`, `--provider anthropic --model claude-sonnet-4-5`, or by editing `llm.*` in `config.yaml`.

Notable yaml keys.

- `llm.provider` - `openai` or `anthropic` (default `openai`)
- `llm.base_url` - LLM endpoint. Default flips to `https://api.anthropic.com/v1` when `provider=anthropic`. Override to `http://localhost:PORT/v1` for local OpenAI-compatible servers
- `llm.model` - model id sent to the API. Default flips to `claude-haiku-4-5` when `provider=anthropic`, otherwise `gpt-4o-mini`. See "Choosing a model" below for trade-offs
- `llm.context_budget` - 32000 token budget for multi-turn history (oldest user/assistant pairs are dropped first, system prompt is preserved)
- `batch.concurrency` - 1-10 allowed (default 4). Anything outside this range is rejected to keep OpenAI rate-limit pressure predictable
- `batch.partial_failure_threshold` - completion ratio under which the batch is flagged partial-failure (default 0.5, higher is stricter)
- `common.dataset.field_map`, `common.dataset.gender_aliases`, `common.dataset.province_aliases` - column and value aliases. Update the YAML if NVIDIA changes the dataset schema, no code change needed
- `common.persona.fields` - persona-field toggle list (default `["summary"]`); add `professional`, `sports`, `arts`, `travel`, `culinary`, or `family` to inject the matching free-form column
- `common.persona.system_prompt_path` - path to the system prompt template (default `prompts/system_prompt.txt`). The template must include the `{persona_json}` and `{product}` placeholders
- `common.report.cohort_min_cell` - cohort cell sample-size mask threshold (default 3, raise to 5 for more conservative reporting)
- `common.report.histogram_bins` - price histogram bin count (default 10)
- `common.report.bar_width` - text bar chart width (default 30, lower for narrow terminals)
- `heuristics.short_answer_threshold` - 20 character trigger for the auto follow-up
- `heuristics.english_ratio_threshold` - 0.30 trigger for persona drift detection
- `heuristics.ambiguous_keywords` - tokens that trigger an auto follow-up when present in a response (default `글쎄요`, `잘 모르겠습니다`, etc.)
- `heuristics.refusal_keywords` - tokens that mark a response as refused (default `답변할 수 없습니다`, `I cannot`, `As an AI`, etc.)
- `heuristics.auto_follow_up_text` - the user message sent when the auto follow-up fires. Edit it to fit your domain tone
- `heuristics.auto_follow_up_max` - per-persona cap on auto follow-ups (default 1, set to 0 to disable)
- `mcp.mode` - MCP entry point inference path. `server` (default) calls OpenAI/Anthropic server-side using the same `LlmConfig` the CLI uses. `orchestrator` exposes data and prompt helpers and lets the host agent's sub-agent do the LLM work; no server-side API key required. No automatic fallback. See ADR-005 for the rationale

Knobs added in v1.1.

| Key | Default | Purpose |
| --- | --- | --- |
| `llm.streaming` | `false` | Opt into OpenAI Server-Sent Events for faster time-to-first-token. The chunked stream is reassembled into a single `ChatResponse` and the final `usage` block is mapped via `stream_options.include_usage`. CLI + `provider=openai` only |
| `llm.anthropic_cache_control` | `true` | Mark the system prompt with `cache_control: ephemeral` so Anthropic prompt caching applies. Set to `false` if you target an older Messages API revision that rejects the marker. CLI + `provider=anthropic` only |
| `llm.extra_chat_kwargs` | `{}` | Free-form dict merged into the OpenAI Chat Completions request body. Reserved keys (`model`, `messages`, `max_tokens`, `temperature`) are skipped. Common use: `chat_template_kwargs.enable_thinking: false` to disable Qwen3 thinking on mlx_lm.server / vLLM. CLI + `provider=openai` only |
| `heuristics.occupation_english_whitelist` | `true` | Exclude English tokens that appear in the persona's occupation (`IT 컨설턴트`, `UX 디자이너`) from the English-ratio drift denominator |
| `heuristics.llm_drift_review` | `false` | Send heuristic drift detections to a single 1-token LLM verdict call. An `ok` clears the drift flag, `drift` keeps it. Adds one extra LLM call per drift candidate |
| `common.report.insight_model` | `null` | Model id used only for the qualitative-insight call. When unset, the per-question interview model is reused. CLI `--insight-model` overrides this for a single run |
| `common.report.estimate_wtp_from_signal` | `false` | Add a recommendation block that combines `acceptable_price_signal` distribution with the explicit number distribution when willingness-to-pay numbers are sparse |

The full annotated yaml lives in [config.yaml](config.yaml).

### Choosing a model

`gpt-4o-mini` is the default because it gives a strong quality baseline for this workload. If you measure persona-drift rates above 5% on your own runs, try the alternatives below by changing `llm.model` in `config.yaml` or by passing `--model` on the command line for a one-off run.

- `gpt-4o-mini` (OpenAI) - default. Good Korean fluency and persona adherence
- `gpt-4o` (OpenAI) - higher quality. Use only if `gpt-4o-mini` does not meet your drift target
- `claude-haiku-4-5` (Anthropic) - default model when `--provider anthropic`. Fluent Korean output
- `claude-sonnet-4-5` / `claude-opus-4-5` (Anthropic) - higher quality
- Local LLMs via `mlx_lm.server`, `vLLM`, or `llama.cpp` work as long as they expose the OpenAI Chat Completions API surface. Korean fluency depends heavily on the underlying weights; validate persona drift on a small sample first

Persona-drift behavior has been validated end-to-end with `gpt-4o-mini`. Other models may need tuned thresholds (`heuristics.english_ratio_threshold`, `heuristics.short_answer_threshold`) for similar quality.

## Customization

Most behavior changes do not require touching code. The three knobs below cover the common cases.

### System prompt

The system prompt is read from `prompts/system_prompt.txt`. Edit the file to change tone, persona instructions, or output formatting. The file must contain the `{persona_json}` and `{product}` placeholders or the tool refuses to start with a `ConfigError`. To use a different file, point `common.persona.system_prompt_path` in `config.yaml` at the new path (absolute or repo-relative).

The template is loaded lazily and cached per-process by mtime, so editing the file between runs picks up the change without a restart of the MCP server.

### Heuristic thresholds

The drift detector and the auto follow-up trigger are tuned via `config.yaml` `heuristics.*`. Common adjustments.

- Loosen the auto follow-up: lower `short_answer_threshold` to 10 or set `auto_follow_up_max: 0` to disable entirely
- Tighten drift detection on technical domains: raise `english_ratio_threshold` to 0.5 if many personas have English-heavy occupations
- Custom domain refusal: append your domain-specific refusal phrases to `heuristics.refusal_keywords`
- Custom ambiguous-answer phrases: add or replace tokens in `heuristics.ambiguous_keywords`

### Report output

- Conservative cohort comparison: raise `common.report.cohort_min_cell` to 5 or 7 for tighter masking on small batches
- Narrow terminal: lower `common.report.bar_width` to 20-25
- Different histogram resolution: tune `common.report.histogram_bins`

## Integration with External Agents

There are three entry points for driving this tool: CLI, MCP server, and MCP orchestrator. They are not interchangeable - the choice depends on whether you want server-side LLM calls (CLI, MCP server) or whether you want the host agent's sub-agent to do the LLM work (MCP orchestrator).

### Entry point matrix

| Entry point | mode (yaml) | Server-side LLM call | Host LLM call | API key required |
| --- | --- | --- | --- | --- |
| CLI (`kpi`) | n/a | yes | no | provider-dependent |
| MCP server | `mcp.mode: "server"` (default) | yes | no | provider-dependent |
| MCP orchestrator | `mcp.mode: "orchestrator"` | no | yes (host sub-agent) | none |

Choose by these rules.

- CLI: shell scripts, automation, reproducible defaults. Validated persona quality on `gpt-4o-mini`. Token usage is tracked
- MCP server: external agents (Claude Code, Cursor, Codex) call the tool with their own LLM-free MCP client. The tool itself calls OpenAI or Anthropic server-side. Recommended for one-tool registration that works in any MCP client today; mcp.json `env` block must carry `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`
- MCP orchestrator: the host agent already has its own LLM (Claude Code's task tool, Cursor's sub-agent) and you want the interviews to be run by that LLM rather than paying for a second backend. The tool exposes data and prompt helpers; the host sub-agent does the LLM calls. No server-side API key is needed

There is no automatic fallback between modes. The chosen path is reflected on every response as `"backend": "mcp_server"` or `"backend": "mcp_orchestrator"`. ADR-005 captures the rationale (sampling mode was removed in v1.2.0 because mainstream MCP clients did not advertise the capability).

If you run `python -m src.mcp_server` outside an MCP host with `mcp.mode: "orchestrator"`, the helper tools still work but `interview` is blocked with a clear hint to use `build_batch_prompts` + sub-agent + `aggregate_results` instead.

### Which yaml fields apply on each entry point

The yaml is split into category sections. Each section lists which entry points read it.

- `common` (dataset, persona, report) is read by every entry point
- `llm` (provider, base_url, model, max_tokens, temperature, timeout, context_budget, retry_max_attempts, retry_backoff_seconds, anthropic_cache_control, extra_chat_kwargs, streaming) is read only by CLI and MCP server. MCP orchestrator never calls a server-side LLM, so this section is ignored
- `batch` (concurrency, partial_failure_threshold) is read only by CLI and MCP server. MCP orchestrator follows the host's sub-agent policy
- `heuristics` (short_answer_threshold, english_ratio_threshold, ambiguous_keywords, refusal_keywords, auto_follow_up_text, auto_follow_up_max, occupation_english_whitelist, llm_drift_review) is auto-applied on CLI and MCP server. MCP orchestrator surfaces the same thresholds via the helper tools (`detect_persona_drift`, `should_auto_follow_up`) and only takes effect when the host explicitly calls them
- `mcp` (mode) is read only by the MCP entry point. CLI ignores it
- `output` (output_dir, log_level, no_color) is read by every entry point

### Tool exposure by mode

The MCP entry point exposes a different tool set in each mode.

| Tool | MCP server | MCP orchestrator | Notes |
| --- | --- | --- | --- |
| `healthcheck` | yes | yes | server mode pings the provider; orchestrator mode returns ok + cwd |
| `list_personas` | yes | yes | preview personas matching a filter |
| `interview` | yes | no (blocked) | server-side batch interview. orchestrator mode points the caller at `build_batch_prompts` instead |
| `report` | yes | yes | server mode runs the qualitative-insight LLM call; orchestrator mode skips it (fallback message) |
| `build_persona_prompt` | no | yes | returns system prompt + persona dict for one persona |
| `build_batch_prompts` | no | yes | returns system prompts for N personas (host sub-agent fan-out) |
| `aggregate_results` | no | yes | takes records from the host and emits the markdown report |
| `detect_persona_drift` | yes | yes | helper. CLI and MCP server auto-apply; MCP orchestrator must invoke explicitly |
| `should_auto_follow_up` | yes | yes | helper. same applies |
| `parse_structured_summary` | yes | yes | helper. parses the LLM's structured-summary JSON |
| `interview_record_schema` | yes | yes | helper. returns the record schema and an example for the host |

Every response (success and error) carries `"backend": "mcp_server"` or `"backend": "mcp_orchestrator"`. On failure the envelope has the shape `{"ok": false, "backend": "...", "error": {"code": "...", "message": "...", "exit_code": N}}`.

### Where to keep the API key

In MCP server mode, the recommended pattern is a `.env` file at the project root. The tool's stdlib `.env` loader uses `setdefault` semantics, so a key already exported in the shell wins, and otherwise `.env` populates the process environment when the MCP server boots. `.env` is gitignored, isolated per project, and stays out of the agent's mcp.json file. Adding an `env` block to mcp.json still works (the secret resolver reads OS environment variables first) but the key ends up in plaintext inside the agent's config and is more likely to leak through git, dotfile sync, or screenshots. The example mcp.json files below ship without an `env` block for this reason. MCP orchestrator mode does not need a key in either location.

### Registering the MCP entry point

Run the server manually to verify it starts.

```bash
python -m src.mcp_server
```

Register it in Claude Code by adding the snippet below to `~/.claude/mcp.json` (create the file if it does not exist). The `cwd` must point at the project root so that `config.yaml`, `prompts/system_prompt.txt`, `.env`, and `outputs/` resolve correctly. The same shape covers both `mcp.mode` values - the mode is decided at startup from `config.yaml`.

```json
{
  "mcpServers": {
    "korea-persona-interview": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/absolute/path/to/korea-persona-interview"
    }
  }
}
```

Register it in Cursor by adding the snippet below to `.cursor/mcp.json` at the project root or to the global Cursor MCP settings.

```json
{
  "mcpServers": {
    "korea-persona-interview": {
      "command": "uv",
      "args": ["run", "--", "python", "-m", "src.mcp_server"],
      "cwd": "/absolute/path/to/korea-persona-interview"
    }
  }
}
```

In MCP server mode, drop your `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) into the project's `.env` before the first run; in MCP orchestrator mode no key is needed. Drop-in copies live under [examples/mcp/](examples/mcp/).

### MCP server mode (default) usage

A natural-language example: ask the agent "1인 가구 대상 반찬 정기배송 (월 39,900원)을 25-39세 서울 30명에게 인터뷰 돌리고 리포트까지 만들어 줘" and it will call `interview` then `report` back-to-back, returning the markdown path. The interview tool runs the batch on the server side using your `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.

### MCP orchestrator mode usage

The host agent owns the LLM. The flow is below.

1. Call `build_batch_prompts` with `product`, `questions`, `n` (and optionally `filter`, `seed`, `persona_ids`). The tool returns N system prompts plus persona dicts
2. The host fan-outs N sub-agents (one per persona). Each sub-agent uses its own LLM with the returned system prompt as the system message and the questions as user turns. The host can also call `should_auto_follow_up` and `detect_persona_drift` between turns to keep behavior parity with the CLI heuristic
3. After the LLM call the host calls `parse_structured_summary` on the LLM's structured-summary text to get a normalized dict, then assembles a record per `interview_record_schema`
4. The host calls `aggregate_results` with the assembled `records`. The tool runs the quantitative aggregation and writes the markdown report to disk. Qualitative insights default to a fallback message; if the host already produced its own qualitative summary it can pass it as `insights` to be embedded in the report

Pseudocode for the host workflow.

```text
prompts = mcp.call("build_batch_prompts", {
  "product": "...", "questions": [...], "n": 30, "seed": 42,
})
records = []
for p in prompts.prompts:
  llm_responses = host_sub_agent.run(p.system_prompt, p.questions)
  for response in llm_responses:
    if mcp.call("should_auto_follow_up", {"text": response}).should_follow_up:
      llm_responses += host_sub_agent.follow_up(...)
    drift = mcp.call("detect_persona_drift", {"text": response, "persona_meta": p.persona_meta}).is_drift
    summary = mcp.call("parse_structured_summary", {"raw_response": last_summary_call}).structured_summary
    records.append({...})  # see interview_record_schema for shape
mcp.call("aggregate_results", {"records": records, "product": "...", "output_dir": "outputs/"})
```

### --json mode for shell scripts

For agents that drive a CLI directly (or for shell scripts), pass `--json` at the root group. The mode disables tqdm progress, ANSI color, and the Korean `[OK]/[INFO]/[ERR]` labels, and emits a single JSON document on stdout. Logs continue to flow to stderr and `outputs/logs/run_*.jsonl`.

```bash
python main.py --json healthcheck
# {"ok": true, "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "models": [...]}

python main.py --json list-personas --filter "age:25-39,region:서울특별시" --limit 5
# {"personas": [...], "count": 5, "filter": "age:25-39,region:서울특별시", "seed": 42}

python main.py --json interview --product "..." --questions "..." --n 10
# {"ok": true, "output_path": "outputs/interview_*.json", "report_path": "outputs/report_*.md",
#  "summary": {"requested": 10, "completed": 10, ...}, "usage": {...}, "model": "gpt-4o-mini"}

python main.py --json report outputs/interview_*.json
# {"ok": true, "output_path": "outputs/report_*.md", "input_path": "outputs/interview_*.json", ...}
```

Errors are emitted as `{"error": {"code": "...", "message": "...", "exit_code": N}}` with a non-zero exit code. Interview record bodies stay in the saved JSON file so the stdout payload remains compact.

## Project Structure

```text
korea-persona-interview/
├── README.md                  # This file
├── LICENSE                    # MIT
├── pyproject.toml             # PEP 621 metadata, console scripts (kpi, kpi-mcp-server)
├── requirements.txt           # Runtime dependencies (direct)
├── requirements.lock          # Frozen runtime graph
├── requirements-dev.txt       # Test dependencies (direct)
├── requirements-dev.lock      # Frozen dev graph
├── config.yaml                # Annotated default config
├── .python-version            # 3.12
├── .env.example               # Template for the OpenAI API key
├── main.py                    # click CLI entry point
├── prompts/
│   └── system_prompt.txt      # System prompt template (editable)
├── src/
│   ├── __init__.py
│   ├── batch.py               # Concurrent batch runner, partial save, usage aggregation
│   ├── cli_views.py           # Persona table, --json dict adapters
│   ├── config.py              # AppConfig dataclass, yaml + env layered loader
│   ├── console.py             # Korean message bank, ANSI color helper
│   ├── interview.py           # Multi-turn session, drift/refusal detection, structured summary
│   ├── llm_backend.py         # LLMBackend protocol, OpenAIBackend, AnthropicBackend
│   ├── mcp_handlers/          # Per-mode MCP tool handlers (common, server, orchestrator, helpers)
│   ├── llm_client.py          # Async OpenAI Chat Completions client (httpx)
│   ├── load_personas.py       # Dataset loader, filter DSL, seeded sampler, persona-pool cache
│   ├── logging_setup.py       # JSON Lines logger, request_id, masking
│   ├── mcp_server.py          # stdio MCP server entry point; tool set depends on mcp.mode
│   ├── models.py              # Domain dataclasses and exceptions
│   └── report.py              # Quantitative aggregation, qualitative insight via LLM
├── tests/
│   ├── conftest.py            # Shared fixtures, env isolation, dataset mock
│   ├── test_*.py              # regression suite (rounds A-G + v1.1.1 mcp.mode toggle + v1.2.0 orchestrator/sampling-removal: backends, persona id pinning, resume, streaming, drift judge, mcp dispatch in server/orchestrator modes)
│   └── manual/smoke_e2e.py    # Live OpenAI smoke test (excluded from default run)
├── examples/
│   └── mcp/                   # Drop-in mcp.json snippets for Claude Code and Cursor
├── docs/
│   ├── INDEX.md               # Doc tree and decision log
│   ├── prd/                   # Product requirements
│   ├── tdd/                   # Technical design
│   ├── adr/                   # Architecture decision records
│   ├── ui/                    # CLI flow, console output, message dictionary
│   ├── tasks/                 # Task breakdown
│   └── backlog/               # rolling backlog (next minor)
└── outputs/                   # Generated JSON/markdown/logs (.gitignored)
    └── logs/
```

## Development

### Setup

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip sync requirements.lock requirements-dev.lock
```

### Tests

Run the full test suite with pytest. The suite mocks the OpenAI API with `pytest-httpx` and the dataset with monkeypatch fixtures, so it does not require a live API key or network access.

```bash
pytest tests/ -v
```

The current regression covers OpenAI, Anthropic Claude, MCP server, and MCP orchestrator coverage (config, filter DSL, persona loader, LLM client, LLM backend selection, interview session, persona drift, batch runner, report quant, MCP dispatch in both modes, MCP orchestrator helper tools, error messages, logging, and CLI integration). See `tests/test_mcp_orchestrator.py` for the v1.2.0 orchestrator dispatch coverage.

Manual smoke tests that exercise a real LLM API call live under `tests/manual/` and are excluded from the default run. They expect `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) in the environment.

### Lint and format

A lint/format toolchain is intentionally not pinned in v1.x. The codebase reads cleanly with default formatting rules; if you want to add `ruff` or `black` locally, run them in your editor only and skip committing config files. A formal pre-commit setup is tracked in the rolling backlog (see [docs/backlog/v1.3.0.md](docs/backlog/v1.3.0.md)).

### Commit messages

Use Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`). Do not put `Co-Authored-By` trailers on commits.

## Limitations and Disclaimer

Synthetic personas are not a replacement for real user interviews. The dataset is generated, not sampled from real respondents, so the demographic distribution may diverge from the actual Korean population. The output of this tool is best treated as a quick gut check before recruiting real participants, and as a way to pressure-test interview questions and product copy before spending recruitment budget.

Every report and JSON file produced by this tool also carries the synthetic-data disclaimer in its footer.

The `--product` text and the persona metadata used for each interview are sent to whichever LLM endpoint you configure (OpenAI, Anthropic, a local server, or the MCP host agent's LLM). Do not put unreleased IP, trade secrets, or personally identifiable information into `--product`. Abstract or paraphrase sensitive parts before running the tool. The tool itself ships no external telemetry beyond the LLM call and the initial dataset download from Hugging Face.

API billing is the user's responsibility. Token usage (prompt / completion / cached) is printed at the end of each run, written into the result JSON `meta_extra.usage`, and surfaced in the report header so you can correlate it against your provider's invoice. The actual invoice from your provider is the authoritative number; this tool does not estimate USD cost. Persona-drift quality is validated against `gpt-4o-mini`; other models may need tuned thresholds.

Legal and ethical review of the output is the user's responsibility. The tool does not run any compliance or PII filter beyond the input-secret policy.

Specific limitations to note for v1.1.

- Streaming responses (`llm.streaming: true`) cover the OpenAI provider only. The Anthropic provider does not stream (MCP orchestrator mode does not use this setting because the host agent owns the LLM call)
- LLM-as-judge drift refinement (`heuristics.llm_drift_review: true`) is opt-in because it adds a small extra LLM call per drift candidate. The default heuristic-only path keeps the cost predictable
- Schema bumped from v1 to v2 in v1.1.0 with the new `acceptable_price_signal` field. v1 result JSONs still load on v1.1.0+ (the loader fills `acceptable_price_signal=null`), but v1.1.0+ result JSONs cannot be read by older v1.0.x consumers
- The `--persona-id` flag re-fetches the dataset to filter by uuid, so the persona-pool cache does not help on that path. Repeated calls with the same id list will still re-read the parquet files

## Roadmap

A short list of v1.3.0 candidates, full details in [docs/backlog/v1.3.0.md](docs/backlog/v1.3.0.md).

- FastAPI REST API on top of the same application layer
- OpenAI Batch API path for offline runs
- Multi-model A/B routing (run the same persona sample on two different models and diff the outputs)
- Provider quality validation report (golden-dataset drift measurement for OpenAI, Anthropic, local LLM)
- macOS Keychain / Linux libsecret / Windows Credential Manager integration for API keys
- Per-record streaming write to disk so OOM/crash mid-batch loses fewer records than the SIGINT partial save

## Dataset and Credits

This project uses the [nvidia/Nemotron-Personas-Korea](https://huggingface.co/datasets/nvidia/Nemotron-Personas-Korea) dataset.

- Title: Nemotron-Personas-Korea
- Author: NVIDIA Corporation (2025)
- Source: https://huggingface.co/datasets/nvidia/Nemotron-Personas-Korea
- License: [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/)
- Modifications: none. The dataset is downloaded from Hugging Face Hub at runtime and sampled in-memory. No derivative dataset is redistributed by this repository

About 1M records and 7M synthetic Korean personas covering name, gender, age, marital status, education, occupation, residence (province and district), and seven persona facets (professional, sports, arts, travel, culinary, family, summary).

CC BY 4.0 permits commercial use with attribution. Credit goes to NVIDIA Corporation. Every markdown report and JSON record produced by this tool also carries the dataset citation and license in its footer so attribution travels with downstream artifacts.

The OpenAI Chat Completions API does not require attribution. The default model `gpt-4o-mini` and any model id you set in `config.yaml` are recorded inside each interview JSON so reports stay reproducible.

## Contributing

Pull requests are welcome. Before opening one.

- Run `pytest tests/ -v` and confirm all 568 tests pass
- Use Conventional Commits
- For substantive changes, open an issue first to discuss the approach

## Acknowledgments

This project was developed with [Claude Code](https://claude.com/claude-code).

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
