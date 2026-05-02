# MCP integration examples

Drop-in `mcp.json` snippets for registering this project as an MCP tool source.

## 1. Files

- `claude-code.mcp.json` - Claude Code (`~/.claude/mcp.json`)
- `cursor.mcp.json` - Cursor (`.cursor/mcp.json` at repo root or global Cursor MCP settings)

Both examples assume Python 3.12 and a `uv`-managed virtual environment at `.venv/`.

## 2. Pick a mode

The MCP server runs in one of two modes, selected in `config.yaml` under `mcp.mode`. ADR-004 captures the rationale.

- `mcp.mode: "server"` (default). The server invokes OpenAI or Anthropic directly from its own process, using the same `LlmConfig` the CLI uses. mcp.json must include `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY` if you set `llm.provider: "anthropic"`) in the `env` block. The example files in this directory ship with this layout
- `mcp.mode: "sampling"`. The server delegates inference to the host agent through `sampling/createMessage`. No server-side API key needed, but the host must advertise the sampling capability. As of 2026-04 mainstream Claude Code Desktop release builds and cmux do not advertise it; some Cursor builds do. When you opt in, drop the `env` block from the mcp.json snippet and edit `config.yaml` to set `mcp.mode: "sampling"`

There is no automatic fallback between modes. Every tool response carries an explicit `"backend": "mcp_server"` or `"backend": "mcp_sampling"` label so you can confirm which path handled the call.

## 3. Installation

Replace every `/absolute/path/to/korea-persona-interview` with the real project path on your machine, and replace `sk-replace-with-your-key` with your provider API key. Then copy the file content into the agent's MCP config location and restart the agent.

For Claude Code, the merged config lives at `~/.claude/mcp.json`. For Cursor, the project-local config lives at `<repo>/.cursor/mcp.json` and is picked up when you open the workspace.

## 4. Sampling-mode mcp.json example

If you switched `config.yaml` to `mcp.mode: "sampling"`, the snippet drops the API key entirely.

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

The Cursor variant mirrors the same shape (`"command": "uv"`, `"args": ["run", "--", "python", "-m", "src.mcp_server"]`) without the `env` block.

## 5. Available tools

Once registered, the agent can call four tools by name. Each tool returns a single JSON document with an explicit `backend` label and `ok: true|false` field.

- `healthcheck` - in server mode, ping the configured provider; in sampling mode, verify the host's sampling capability
- `list_personas` - preview personas matching a filter (`filter`, `limit`, `seed`)
- `interview` - run a batch interview (`product`, `questions`, `filter`, `n`, `seed`, `concurrency`, `persona_fields`, `follow_ups`, `single_turn`, `output_dir`)
- `report` - generate a markdown report from a result JSON (`json_path`, `top_n`, `include_drift`, `output_dir`)

A natural-language prompt that exercises the full pipeline is below.

```
1인 가구 대상 반찬 정기배송 (월 39,900원)을 25-39세 서울 30명에게 인터뷰 돌리고 리포트까지 만들어 줘. seed는 42로 고정해 주세요.
```

The agent will call `interview` then `report` back-to-back and return the markdown path.

## 6. Logs and output

Logs flow to stderr and `<cwd>/outputs/logs/run_*.jsonl`, separate from the stdio JSON-RPC channel that the agent reads. Result JSON files land in `<cwd>/outputs/interview_*.json`, and reports land next to them at `<cwd>/outputs/report_*.md` unless the tool call sets `output_dir` explicitly.
