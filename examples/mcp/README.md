# MCP integration examples

Drop-in `mcp.json` snippets for registering this project as an MCP tool source.

## 1. Files

- `claude-code.mcp.json` - Claude Code (`~/.claude/mcp.json`)
- `cursor.mcp.json` - Cursor (`.cursor/mcp.json` at repo root or global Cursor MCP settings)

Both examples assume Python 3.12 and a `uv`-managed virtual environment at `.venv/`.

## 2. Installation

Replace every `/absolute/path/to/korea-persona-interview` with the real project path on your machine, and replace `sk-replace-with-your-key` with your OpenAI API key. Then copy the file content into the agent's MCP config location and restart the agent.

For Claude Code, the merged config lives at `~/.claude/mcp.json`. For Cursor, the project-local config lives at `<repo>/.cursor/mcp.json` and is picked up when you open the workspace.

## 3. Available tools

Once registered, the agent can call four tools by name. Each tool returns a single JSON document.

- `healthcheck` - verify OpenAI API reachability and model availability
- `list_personas` - preview personas matching a filter (`filter`, `limit`, `seed`)
- `interview` - run a batch interview (`product`, `questions`, `filter`, `n`, `seed`, `concurrency`, `persona_fields`, `follow_ups`, `single_turn`, `model`, `output_dir`)
- `report` - generate a markdown report from a result JSON (`json_path`, `top_n`, `include_drift`, `output_dir`, `model`)

A natural-language prompt that exercises the full pipeline is below.

```
1인 가구 대상 반찬 정기배송 (월 39,900원)을 25-39세 서울 30명에게 인터뷰 돌리고 리포트까지 만들어 줘. seed는 42로 고정해 주세요.
```

The agent will call `interview` then `report` back-to-back and return the markdown path.

## 4. Logs and output

Logs flow to stderr and `<cwd>/outputs/logs/run_*.jsonl`, separate from the stdio JSON-RPC channel that the agent reads. Result JSON files land in `<cwd>/outputs/interview_*.json`, and reports land next to them at `<cwd>/outputs/report_*.md` unless the tool call sets `output_dir` explicitly.
