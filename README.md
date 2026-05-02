# korea-persona-interview

A CLI tool that runs synthetic Korean persona interviews against business ideas using a local MLX LLM server on Apple Silicon. No external LLM API, no telemetry, all inference and data flow stay on the device.

## Features

- Multi-turn interviews with 1M+ Korean synthetic personas (NVIDIA Nemotron-Personas-Korea, CC BY 4.0)
- Local-only inference via MLX OpenAI-compatible server (no external LLM API, no telemetry)
- Structured JSON output plus markdown report (quantitative metrics with LLM qualitative insights)
- Filter DSL with `age`, `gender`, `region`, `subregion`, `occupation_keyword` keys and AND/OR combination
- Automatic follow-up on short answers and persona drift detection (English ratio, persona contradiction)
- Four CLI subcommands: `healthcheck`, `list-personas`, `interview`, `report`
- Reproducible sampling via `--seed` (same seed, same filter, same dataset version returns the same personas)

## Modules

- `src/llm_client.py` - async MLX HTTP client with retry, timeout, and localhost guard
- `src/load_personas.py` - dataset loader, filter DSL parser, seeded sampler
- `src/interview.py` - multi-turn session, system prompt builder, drift/refusal detector, structured summary
- `src/batch.py` - concurrent runner with semaphore, tqdm progress, SIGINT partial save
- `src/report.py` - quantitative aggregation and LLM-driven qualitative report generator
- `src/config.py`, `src/logging_setup.py`, `src/models.py` - cross-cutting config, structured logging, dataclasses
- `main.py` - click entry point that wires the four subcommands

## Requirements

- macOS with Apple Silicon (M1 or newer). External GPUs and Linux are out of scope for v1
- Python 3.12 (pinned in `.python-version`)
- [uv](https://docs.astral.sh/uv/) package manager
- mlx-lm server running locally with a Qwen3 MLX model (recommended: `unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit`)
- 32 GB or more unified memory recommended for the 35B-A3B 4-bit model
- Internet access for the first dataset download (about 1M records, cached afterwards under `~/.cache/huggingface`)

## Dependencies

Direct runtime dependencies are pinned in `requirements.txt`.

- `httpx` - async HTTP client for the MLX server
- `datasets` - Hugging Face loader for `nvidia/Nemotron-Personas-Korea`
- `pyyaml` - `config.yaml` loader
- `tqdm` - batch progress bar
- `click` - CLI framework

Test-only dependencies live in `requirements-dev.txt` (`pytest`, `pytest-asyncio`, `pytest-httpx`).

`mlx-lm` is installed separately because it is the inference server, not a library this project imports.

## Installation

Create the virtual environment with uv and install dependencies. The `.python-version` file pins Python 3.12, so `uv venv` picks the right interpreter automatically.

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt -r requirements-dev.txt
```

Install `mlx-lm` separately. It is the inference server, not a project dependency.

```bash
uv pip install mlx-lm
```

## Quick Start

Start the MLX server in a separate terminal first. The default model below matches `config.yaml` and the documented evaluation target.

```bash
mlx_lm.server --model unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit --port 8080
```

Then run the four subcommands in order. Each step is independently verifiable.

```bash
python main.py healthcheck
python main.py list-personas --filter "age:25-39,region:서울특별시" --limit 20
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원, 주 2회 배송" --filter "age:25-39,region:서울특별시" --n 10 --questions "이 서비스 쓰실 의향 있나요?" "월 얼마면 적당한가요?" "거절한다면 왜요?"
python main.py report outputs/interview_korea-persona-interview_20260502_120000.json
```

The first dataset load takes 5-10 minutes and downloads roughly 1M records. Subsequent runs start in under 30 seconds because `datasets` caches the parquet files under `~/.cache/huggingface`.

## CLI Commands

| Command | Description | Key options | Exit codes |
| --- | --- | --- | --- |
| `healthcheck` | Verify MLX server reachability and model availability | `--base-url` (default `http://localhost:8080/v1`) | 0 ok, 1 server down |
| `list-personas` | Preview personas matching a filter | `--filter`, `--limit` (default 20), `--seed` | 0 ok, 2 no match |
| `interview` | Run a batch interview | `--product` (required), `--questions` (required, multiple), `--filter`, `--n` (default 10), `--seed`, `--concurrency` (default 2, max 3), `--persona-fields`, `--follow-up`, `--single-turn`, `--dry-run`, `--output` | 0 ok, 1 server error, 2 sample shortfall, 3 partial failure |
| `report` | Generate a markdown report from an interview JSON | positional path to JSON, `--top-n` (default 10), `--include-drift`, `--output-dir` | 0 ok, 1 input error, 2 no valid records |

Exit code 130 is reserved for `SIGINT` (Ctrl-C). The first interrupt saves a partial JSON to `outputs/`. The second interrupt terminates immediately.

## Output Format

Interview results are written to `outputs/interview_{slug}_{YYYYMMDD_HHMMSS}.json`. The report subcommand emits `outputs/report_{slug}_{YYYYMMDD_HHMMSS}.md` next to the input JSON by default.

The JSON envelope contains the run metadata (`interview_id`, `slug`, `product`, `model`, `seed`, `config_snapshot`) and a `records` array. Each record holds `persona_meta`, the multi-turn `messages`, per-question `raw_responses`, a `structured_summary` (intent, willingness to pay, rejection reasons, one-line takeaway), and `flags` (`persona_drift`, `auto_follow_up_used`, `refusal_detected`, `truncated`). See `docs/prd/korea-persona-interview.md` section 5.4 for the full schema.

## Project Structure

```
korea-persona-interview/
├── README.md
├── requirements.txt
├── requirements-dev.txt
├── config.yaml
├── .python-version
├── main.py
├── src/
│   ├── batch.py
│   ├── config.py
│   ├── interview.py
│   ├── llm_client.py
│   ├── load_personas.py
│   ├── logging_setup.py
│   ├── models.py
│   └── report.py
├── tests/
│   ├── test_*.py
│   └── manual/smoke_e2e.py
├── docs/
│   ├── INDEX.md
│   ├── prd/korea-persona-interview.md
│   ├── tdd/korea-persona-interview.md
│   ├── adr/2026-05-02-multiturn-strategy.md
│   ├── ui/korea-persona-interview.md
│   └── tasks/korea-persona-interview.md
└── outputs/
    └── logs/
```

## Configuration

Configuration is layered with the following precedence (later overrides earlier).

- Built-in defaults
- `config.yaml`
- Environment variables prefixed with `KPI_` (for example `KPI_LLM_MODEL`, `KPI_BATCH_CONCURRENCY`)
- CLI options

Notable keys.

- `llm.base_url` - MLX server URL. Non-localhost URLs are blocked for chat calls
- `llm.model` - model id served by `mlx_lm.server`
- `llm.context_budget` - 8000 token budget for multi-turn history (oldest user/assistant pairs are dropped first, system prompt is preserved)
- `llm.enable_thinking` - kept `false` to avoid Qwen3 reasoning token blow-up
- `batch.concurrency` - 1-3 allowed, 4 or higher rejected to avoid OOM
- `dataset.field_map`, `dataset.gender_aliases`, `dataset.province_aliases` - column and value aliases. Update the YAML if NVIDIA changes the dataset schema, no code change needed
- `interview.short_answer_threshold` - 20 character trigger for the auto follow-up
- `interview.english_ratio_threshold` - 0.30 trigger for persona drift detection

## Development

Run the full test suite with pytest. The suite mocks the MLX server with `pytest-httpx` and the dataset with monkeypatch fixtures, so it does not require a live server.

```bash
pytest tests/ -v
```

Manual smoke tests that exercise a real MLX server live under `tests/manual/` and are excluded from the default run.

## Limitations and Disclaimer

Synthetic personas are not a replacement for real user interviews. The dataset is generated, not sampled from real respondents, so the demographic distribution may diverge from the actual Korean population. The output of this tool is best treated as a quick gut check before recruiting real participants, and as a way to pressure-test interview questions and product copy before spending recruitment budget.

Every report and JSON file produced by this tool also carries the synthetic-data disclaimer in its footer.

## Dataset and Credits

This project uses the [nvidia/Nemotron-Personas-Korea](https://huggingface.co/datasets/nvidia/Nemotron-Personas-Korea) dataset published by NVIDIA in 2025. About 1M records and 7M synthetic Korean personas covering name, gender, age, marital status, education, occupation, residence (province and district), and seven persona facets (professional, sports, arts, travel, culinary, family, summary).

The dataset is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), which permits commercial use with attribution. Credit goes to NVIDIA.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
