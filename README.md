**English** | [한국어](docs/i18n/ko/README.md) | [日本語](docs/i18n/ja/README.md)

# korea-persona-interview

[![CI](https://github.com/binaryloader/korea-persona-interview/actions/workflows/test.yml/badge.svg)](https://github.com/binaryloader/korea-persona-interview/actions/workflows/test.yml)

A field-ready CLI for running synthetic Korean persona interviews on top of OpenAI, Anthropic Claude, or any OpenAI-compatible local LLM (mlx_lm.server, vLLM, llama.cpp). Pair the NVIDIA Nemotron-Personas-Korea dataset (CC BY 4.0, about 1M Korean synthetic personas) with the model of your choice to pressure-test product ideas, interview guides, and persona hypotheses before recruiting real participants.

The tool ships four CLI subcommands (`healthcheck`, `list-personas`, `interview`, `report`), a JSON output mode for machine-to-machine use, and a Model Context Protocol (MCP) entry point that runs in either MCP server mode (server-side OpenAI/Anthropic calls) or MCP orchestrator mode (the host agent's sub-agent does the LLM work).

## Features

- Multi-turn interviews with 1M+ Korean synthetic personas (NVIDIA Nemotron-Personas-Korea, CC BY 4.0)
- Three inference targets: OpenAI Chat Completions API, Anthropic Messages API, and any OpenAI-compatible local server
- Async batch runner with concurrency 1-10, tqdm progress, SIGINT partial save, and exit-code 3 partial-failure detection
- Persona drift detection with sentence-bounded first-person assertions for the gender/age/region/family-type axes (negation guards, third-person exclusion) plus an English-ratio safety net
- `--persona-id` to pin specific personas by uuid for A/B comparisons; `--resume PATH` to re-run only the failed records of a previous batch
- `--insight-model` to run interviews on a small model and the qualitative-insight call on a larger one
- OpenAI streaming (`llm.streaming: true`) and Anthropic prompt caching (`llm.anthropic_cache_control: true`, default on)
- LLM-as-judge drift refinement (`heuristics.llm_drift_review`, opt-in) for clearing false positives
- `acceptable_price_signal` (`cheap`/`fair`/`expensive`/`null`) on every structured summary, plus optional WTP recommendation from the signal distribution
- MCP entry point (`python -m src.mcp_server`) for Claude Code, Cursor, and Codex. `mcp.mode` toggles between `orchestrator` (default, no server-side key) and `server` (server-side OpenAI/Anthropic calls)
- Automatic markdown report after every run (toggle with `--no-report`) and `--json` root mode for shell scripts
- Single-turn mode (`--single-turn`) that bundles every question into one chat call to cut tokens
- Token usage (prompt / completion / cached) printed at the end of every run and embedded in the JSON and report header
- Reproducible sampling via `--seed`. Same seed plus same filter plus same dataset version returns the same personas
- Operational hardening: persona ids sha256-masked in logs, `outputs/` created with mode 0700 (result files 0600), `--product` and per-question text length-capped at 2000 chars with prompt-injection guards
- No external telemetry. Outbound calls go only to the configured LLM endpoint and (on first run) Hugging Face Hub for the dataset

## Requirements

- Python 3.12 (pinned in `.python-version`)
- [uv](https://docs.astral.sh/uv/) package manager
- An API key for the provider you plan to use:
  - `OPENAI_API_KEY` for `provider=openai` (default). Get one at https://platform.openai.com/api-keys
  - `ANTHROPIC_API_KEY` for `provider=anthropic`. Get one at https://console.anthropic.com/
  - For local LLMs (mlx_lm.server, vLLM, llama.cpp) keep `provider=openai` and use any non-empty value
- Internet access for the LLM API call and the first dataset download (about 1M records, cached afterwards under `~/.cache/huggingface`)
- macOS, Linux, and Windows are all supported. There is no Apple Silicon, GPU, or local-runtime requirement

## Installation

`.python-version` pins Python 3.12, so `uv venv` picks the right interpreter automatically. Production deploys must install from the lockfiles to keep the resolved graph identical across environments.

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip sync requirements.lock requirements-dev.lock
```

Recompile the lockfiles after editing `requirements*.txt`.

```bash
uv pip compile requirements.txt -o requirements.lock
uv pip compile requirements-dev.txt -o requirements-dev.lock
```

To run the CLI as `kpi` and the MCP server as `kpi-mcp-server` from anywhere, install the project in editable mode after the dependency sync.

```bash
uv pip install -e .
```

Plain pip works too if you cannot use uv.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Direct runtime dependencies live in `pyproject.toml` (`[project.dependencies]`). The official `openai` and `anthropic` SDKs are intentionally not used; calls go through `httpx` so the project keeps its dependency tree small and owns the retry, timeout, and logging policy. See [docs/adr/2026-05-02-openai-backend-migration.md](docs/adr/2026-05-02-openai-backend-migration.md) for the rationale.

## Quick Start

Five commands take you from a fresh checkout to a finished report. The first interview run downloads the dataset (5-10 minutes); subsequent runs start in under 30 seconds.

```bash
export OPENAI_API_KEY=sk-...
python main.py healthcheck
python main.py list-personas --filter "age:25-39,region:서울특별시" --limit 20
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원, 주 2회 배송" --filter "age:25-39,region:서울특별시" --n 10 --questions "이 서비스 쓰실 의향 있나요?" "월 얼마면 적당한가요?" "거절한다면 왜요?"
python main.py report outputs/interview_korea-persona-interview_20260502_120000.json
```

The `interview` command auto-generates the markdown report (default `--report`); the standalone `report` step is only needed if you used `--no-report`, edited the JSON, or want to regenerate with different `--top-n` or `--include-drift` settings.

A `.env` file at the project root with `OPENAI_API_KEY=sk-...` (or `ANTHROPIC_API_KEY=sk-ant-...`) is picked up automatically. Existing shell environment variables take precedence over `.env`.

To use Claude instead, set `ANTHROPIC_API_KEY` and pass `--provider anthropic`.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python main.py interview --provider anthropic --model claude-haiku-4-5 --product "..." --questions "..." --n 10
```

To use a local OpenAI-compatible server, keep `provider=openai` and override `--base-url`. Any non-empty `OPENAI_API_KEY` works; local servers ignore the value.

```bash
export OPENAI_API_KEY=local
python main.py interview --base-url http://localhost:8080/v1 --model llama-3-8b --product "..." --questions "..." --n 10
```

## Usage Examples

### Validate a product idea

```bash
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원, 주 2회 배송" --filter "age:25-39,region:서울특별시" --n 10 --seed 42 --questions "이 서비스 쓰실 의향 있나요?" "월 얼마면 적당한가요?" "거절한다면 왜요?"
```

A markdown report with intent share (positive/neutral/negative), willingness-to-pay median plus IQR, top rejection reasons, and 5-10 actionable insights for the next round.

### A/B test product copy on the same personas

Pin the same persona ids across two runs by extracting them from the first batch and replaying them on the second.

```bash
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬, 월 39,900원" --filter "age:25-39,region:서울특별시" --n 10 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/copy-a/

python -c "import json,sys; d=json.load(open(sys.argv[1])); print('\n'.join(r['persona_id'] for r in d['records']))" outputs/copy-a/interview_*.json > /tmp/persona_ids.txt

xargs -I {} echo --persona-id {} < /tmp/persona_ids.txt | xargs python main.py interview --product "주말에 받는 1주일치 한식 반찬 박스, 월 39,900원" --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/copy-b/
```

Both runs interview the exact same persona ids, so the only variable is the product copy.

### Cohort comparison

```bash
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬 정기배송" --filter "age:20-29" --n 15 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/cohort-20s/
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬 정기배송" --filter "age:30-39" --n 15 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/cohort-30s/
```

The cohort intent table inside each report further splits by region and gender, so you can see whether a 20s/30s gap holds across all regions or comes from one segment.

### Large-scale screen with single-turn mode

Single-turn mode bundles every question into one chat call, which roughly halves the prompt tokens versus multi-turn. The auto follow-up is disabled in this mode.

```bash
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원" --filter "age:20-49" --n 100 --seed 42 --concurrency 8 --single-turn --questions "이 서비스 쓸 의향?" "월 얼마면 적당?" "거절 사유?"
```

### Resume after a partial-failure exit

A 30-person batch hit rate-limit storms and the run exited with code 3. Re-run only the failed records on top of the previous JSON.

```bash
python main.py interview --product "..." --filter "..." --n 30 --seed 42 --questions "..." --resume outputs/interview_korea-persona-interview_20260502_120000.json
```

`meta_extra.previous_run_id` is set to the original `interview_id` so the two runs can be linked.

### Tip: ask explicit value-pricing questions

`willingness_to_pay` is filled in only when the persona names a specific number. To maximize the explicit-number rate, ask a direct value-pricing question.

- "본인은 월 얼마면 가입하시겠어요?" (anchored to a monthly subscription)
- "월 39,900원이면 가입할 의향이 있으세요? 아니면 얼마면 적당할까요?" (counter-offer prompt)
- "비슷한 서비스에 한 달에 얼마까지 쓸 수 있어요?" (ceiling probe)

Open-ended price questions often only return a qualitative signal (`acceptable_price_signal`), which is filled for every record but does not produce a `willingness_to_pay` integer.

## CLI Reference

### Subcommands

| Command | Description | Exit codes |
| --- | --- | --- |
| `healthcheck` | Verify provider reachability and model availability | 0 ok, 1 missing key / 401 / 429 / unreachable |
| `list-personas` | Preview personas matching a filter | 0 ok, 2 no match |
| `interview` | Run a batch interview, save JSON, auto-generate report | 0 ok, 1 server error, 2 sample shortfall, 3 partial failure |
| `report` | Generate a markdown report from an interview JSON | 0 ok, 1 input error, 2 no valid records |

Exit code 130 is reserved for `SIGINT` (Ctrl-C). The first interrupt saves a partial JSON; the second terminates immediately.

### Root options

These apply to every subcommand and must be placed before the subcommand name.

| Option | Default | Description |
| --- | --- | --- |
| `--config PATH` | `config.yaml` in cwd | Override the config file path |
| `--no-color` | off | Disable ANSI color output (also honors `NO_COLOR` env) |
| `--log-level LEVEL` | `INFO` (from yaml) | Set log level: `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `--json` | off | Emit a single JSON document on stdout. Disables tqdm, color, and Korean labels. Errors land as `{"error": {...}}` with non-zero exit |

### `interview` options

| Option | Default | Description |
| --- | --- | --- |
| `--product TEXT` | required | One-line product description (max 2000 chars) |
| `--questions TEXT` | required, repeatable | Each question is one `--questions` flag (max 2000 chars each) |
| `--filter SPEC` | none | Filter DSL (see below) |
| `--persona-id UUID` | none, repeatable | Pin specific persona ids by uuid. Disables `--n` and `--seed` randomization. Combine with `--filter` for an intersection |
| `--n N` | `10` | Number of personas |
| `--seed N` | `42` | Sampling seed |
| `--concurrency N` | `4` | Async concurrency, range 1-10 |
| `--persona-fields LIST` | `summary` | Comma-separated toggles: `summary`, `professional`, `sports`, `arts`, `travel`, `culinary`, `family` |
| `--follow-up TEXT` | none, repeatable | Common follow-up question for every persona |
| `--single-turn` | off | Bundle every question into one chat call. Auto follow-up disabled |
| `--dry-run` | off | Run one persona, print to console, write neither JSON nor report |
| `--output DIR` | `outputs/` | Result JSON directory |
| `--report / --no-report` | `--report` | Auto-generate the markdown report after the interview |
| `--resume PATH` | none | Re-run only the `failed` records of a previous result JSON |
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
| `--insight-model MODEL_ID` | from `common.report.insight_model` or `--model` | Use a different model for the qualitative-insight call only |

`healthcheck` and `list-personas` accept the same provider/base-url/model triple plus filter/limit/seed. See `python main.py {sub} --help` for the full list.

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

| Field | Notes |
| --- | --- |
| `interview_id` | uuid, one per run |
| `schema_version` | `2` since v1.1.0 (was `1` in v1.0.x). Readers can branch on this to handle the `acceptable_price_signal` field |
| `model` | Resolved model id (e.g. `gpt-4o-mini`) |
| `meta_extra.usage` | Aggregated `prompt_tokens`, `completion_tokens`, `total_tokens`, `cached_tokens` |
| `meta_extra.previous_run_id` | Set when the run came from `--resume`. Holds the source run's `interview_id` |
| `records[].status` | `completed` / `refused` / `failed` / `drift` |
| `records[].structured_summary` | `intent`, `acceptable_price_signal`, `willingness_to_pay`, `willingness_to_pay_currency`, `rejection_reasons`, `one_line` |
| `records[].flags` | `persona_drift`, `auto_follow_up_used`, `refusal_detected`, `truncated`, `parse_failed` |

See `docs/prd/korea-persona-interview.md` section 5.4 for the full schema. v1 JSON files load fine on v1.1.0+ (the loader fills `acceptable_price_signal=null`).

### Markdown report

The report subcommand emits `outputs/report_{slug}_{YYYYMMDD_HHMMSS}.md` next to the input JSON by default.

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

Settings policy: `secrets via env, defaults via yaml, one-off overrides via CLI`. Configuration precedence (later overrides earlier): built-in defaults → `config.yaml` → CLI options.

The only environment variables this tool reads are secrets and the output directory.

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI API key (used when `provider=openai`) |
| `ANTHROPIC_API_KEY` | Anthropic API key (used when `provider=anthropic`) |
| `KPI_OUTPUT_DIR` | Output directory override (kept for test/CI isolation) |

The full annotated yaml lives in [config.yaml](config.yaml). Notable keys.

- `llm.provider` / `llm.base_url` / `llm.model` - provider and endpoint. Defaults flip with `--provider anthropic` (`claude-haiku-4-5` on `https://api.anthropic.com/v1`)
- `llm.context_budget` - 32000 token budget for multi-turn history (oldest user/assistant pairs dropped first; system prompt preserved)
- `llm.streaming` / `llm.anthropic_cache_control` / `llm.extra_chat_kwargs` - provider-specific tuning
- `batch.concurrency` (1-10, default 4) and `batch.partial_failure_threshold` (default 0.5)
- `common.dataset.field_map`, `common.dataset.gender_aliases`, `common.dataset.province_aliases` - column and value aliases for dataset schema changes
- `common.persona.fields` and `common.persona.system_prompt_path` - persona toggles and system prompt template path
- `common.report.cohort_min_cell` / `histogram_bins` / `bar_width` / `insight_model` / `estimate_wtp_from_signal`
- `common.output.output_dir` / `log_level` / `no_color`
- `heuristics.short_answer_threshold` / `english_ratio_threshold` / `ambiguous_keywords` / `refusal_keywords` / `auto_follow_up_text` / `auto_follow_up_max` / `occupation_english_whitelist` / `llm_drift_review`
- `mcp.mode` - `orchestrator` (default, no server-side key) or `server` (server-side OpenAI/Anthropic). See ADR-005 for the rationale

### Choosing a model

`gpt-4o-mini` is the default and gives a strong baseline for this workload. If you measure persona-drift rates above 5% on your own runs, try the alternatives below.

- `gpt-4o-mini` (OpenAI) - default. Good Korean fluency and persona adherence
- `gpt-4o` (OpenAI) - higher quality
- `claude-haiku-4-5` (Anthropic) - default for `--provider anthropic`
- `claude-sonnet-4-5` / `claude-opus-4-5` (Anthropic) - higher quality
- Local LLMs via `mlx_lm.server`, `vLLM`, or `llama.cpp` work as long as they expose the OpenAI Chat Completions API surface. Korean fluency depends on the underlying weights; validate persona drift on a small sample first

Persona-drift behavior has been validated end-to-end with `gpt-4o-mini`. Other models may need tuned thresholds (`heuristics.english_ratio_threshold`, `heuristics.short_answer_threshold`).

### Customization

- System prompt: edit `prompts/system_prompt.txt` (must contain `{persona_json}` and `{product}` placeholders). Point `common.persona.system_prompt_path` at a different file to use your own template
- Heuristic thresholds: tune `heuristics.*` in `config.yaml` (lower `short_answer_threshold` for tighter follow-ups, raise `english_ratio_threshold` for technical domains, append domain-specific phrases to `refusal_keywords`/`ambiguous_keywords`)
- Report output: raise `common.report.cohort_min_cell` to 5 or 7 for tighter masking; lower `bar_width` for narrow terminals; tune `histogram_bins` for different price resolution

## Integration with External Agents

There are three entry points: CLI, MCP server, and MCP orchestrator. They are not interchangeable - the choice depends on whether you want server-side LLM calls (CLI, MCP server) or whether the host agent's sub-agent does the LLM work (MCP orchestrator).

### Entry point matrix

| Entry point | mode (yaml) | Server-side LLM call | Host LLM call | API key required |
| --- | --- | --- | --- | --- |
| CLI (`kpi`) | n/a | yes | no | provider-dependent |
| MCP server | `mcp.mode: "server"` | yes | no | provider-dependent |
| MCP orchestrator | `mcp.mode: "orchestrator"` (default) | no | yes (host sub-agent) | none |

There is no automatic fallback between modes. The chosen path is reflected on every response as `"backend": "mcp_server"` or `"backend": "mcp_orchestrator"`. ADR-005 captures the rationale (sampling mode was removed in v1.2.0 because mainstream MCP clients did not advertise the capability).

If you run `python -m src.mcp_server` outside an MCP host with `mcp.mode: "orchestrator"`, the helper tools still work but `interview` is blocked with a hint to use `build_batch_prompts` + sub-agent + `aggregate_results` instead.

### Tool exposure by mode

| Tool | MCP server | MCP orchestrator | Notes |
| --- | --- | --- | --- |
| `healthcheck` | yes | yes | server mode pings the provider; orchestrator mode returns ok + cwd |
| `list_personas` | yes | yes | preview personas matching a filter |
| `interview` | yes | no (blocked) | server-side batch interview |
| `report` | yes | yes | server mode runs the qualitative-insight LLM call; orchestrator mode skips it |
| `build_persona_prompt` | no | yes | system prompt + persona dict for one persona |
| `build_batch_prompts` | no | yes | system prompts for N personas (host sub-agent fan-out) |
| `aggregate_results` | no | yes | takes records from the host and emits the markdown report |
| `detect_persona_drift` / `should_auto_follow_up` / `parse_structured_summary` / `interview_record_schema` | yes | yes | helpers. CLI and MCP server auto-apply; MCP orchestrator must invoke explicitly |

### Registering the MCP entry point

Run the server manually to verify it starts.

```bash
python -m src.mcp_server
```

Register it in Claude Code by adding the snippet below to `~/.claude/mcp.json` (create the file if it does not exist). The `cwd` must point at the project root so that `config.yaml`, `prompts/system_prompt.txt`, `.env`, and `outputs/` resolve correctly.

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

For Cursor, add the snippet to `.cursor/mcp.json` at the project root. Drop-in copies live under [examples/mcp/](examples/mcp/).

In MCP server mode, drop your `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) into the project's `.env` before the first run. The stdlib `.env` loader uses `setdefault` semantics so a key already exported in the shell wins. Putting the key in the agent's mcp.json `env` block also works but the secret ends up in plaintext inside the agent's config and is more likely to leak through git, dotfile sync, or screenshots.

### MCP orchestrator mode usage (default)

The host agent owns the LLM. The flow:

1. Call `build_batch_prompts` with `product`, `questions`, `n` (and optionally `filter`, `seed`, `persona_ids`). Returns N system prompts plus persona dicts
2. The host fans out N sub-agents (one per persona). Each sub-agent uses its own LLM with the returned system prompt as the system message and the questions as user turns. The host can also call `should_auto_follow_up` and `detect_persona_drift` between turns to keep behavior parity with the CLI heuristic
3. After the LLM call the host calls `parse_structured_summary` on the LLM's structured-summary text to get a normalized dict, then assembles a record per `interview_record_schema`
4. The host calls `aggregate_results` with the assembled `records`. The tool runs the quantitative aggregation and writes the markdown report. Qualitative insights default to a fallback message; the host can pass its own as `insights` to be embedded

### MCP server mode usage

Set `mcp.mode: "server"` in `config.yaml` to call OpenAI/Anthropic server-side. Ask the agent in plain Korean: "1인 가구 대상 반찬 정기배송 (월 39,900원)을 25-39세 서울 30명에게 인터뷰 돌리고 리포트까지 만들어 줘" and it will call `interview` then `report` back-to-back, returning the markdown path.

### --json mode for shell scripts

For agents that drive a CLI directly, pass `--json` at the root group. Disables tqdm, color, and Korean labels; emits a single JSON document on stdout. Logs continue to flow to stderr and `outputs/logs/run_*.jsonl`.

```bash
python main.py --json healthcheck
# {"ok": true, "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "models": [...]}

python main.py --json interview --product "..." --questions "..." --n 10
# {"ok": true, "output_path": "outputs/interview_*.json", "report_path": "outputs/report_*.md", "summary": {...}, "usage": {...}, "model": "gpt-4o-mini"}
```

Errors are emitted as `{"error": {"code": "...", "message": "...", "exit_code": N}}` with a non-zero exit code.

## Development

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip sync requirements.lock requirements-dev.lock
pytest tests/ -v
```

The suite mocks the OpenAI/Anthropic APIs with `pytest-httpx` and the dataset with monkeypatch fixtures, so it does not require a live API key or network access. Coverage spans config, filter DSL, persona loader, LLM client/backend, interview session, persona drift, batch runner, report quant, MCP dispatch in both modes, MCP orchestrator helper tools, error messages, logging, and CLI integration.

Manual smoke tests that exercise a real LLM API call live under `tests/manual/` and are excluded from the default run.

Use Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`). Do not put `Co-Authored-By` trailers on commits.

## Limitations and Disclaimer

Synthetic personas are not a replacement for real user interviews. The dataset is generated, not sampled from real respondents, so the demographic distribution may diverge from the actual Korean population. Treat the output as a quick gut check before recruiting real participants and as a way to pressure-test interview questions and product copy before spending recruitment budget.

Every report and JSON file produced by this tool also carries the synthetic-data disclaimer in its footer.

The `--product` text and the persona metadata used for each interview are sent to whichever LLM endpoint you configure (OpenAI, Anthropic, a local server, or the MCP host agent's LLM). Do not put unreleased IP, trade secrets, or personally identifiable information into `--product`. Abstract or paraphrase sensitive parts before running the tool. The tool itself ships no external telemetry beyond the LLM call and the initial dataset download from Hugging Face.

API billing is the user's responsibility. Token usage (prompt / completion / cached) is printed at the end of each run, written into the result JSON `meta_extra.usage`, and surfaced in the report header so you can correlate it against your provider's invoice. The tool does not estimate USD cost. Persona-drift quality is validated against `gpt-4o-mini`; other models may need tuned thresholds.

Legal and ethical review of the output is the user's responsibility. The tool does not run any compliance or PII filter beyond the input-secret policy.

## Roadmap

A short list of v1.3.0 candidates. Full details in [docs/backlog/v1.3.0.md](docs/backlog/v1.3.0.md).

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

## Acknowledgments

This project was developed with [Claude Code](https://claude.com/claude-code).

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
