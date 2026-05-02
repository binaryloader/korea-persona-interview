# Contributing to korea-persona-interview

Thanks for considering a contribution. This document covers how to set up a development environment, run the test suite, and open a pull request that lands cleanly.

## Development setup

### Prerequisites

- Python 3.12 (pinned in `.python-version`)
- [uv](https://docs.astral.sh/uv/) package manager
- An OpenAI API key with access to `gpt-4o-mini` (or whichever model id you set in `config.yaml`). Get one at https://platform.openai.com/api-keys. The full test suite mocks the OpenAI API with `pytest-httpx` and does not require a live key, so contributors can run the regression locally without a billable account. A live key is only needed for the manual smoke test under `tests/manual/`

### Clone and install

```bash
git clone https://github.com/binaryloader/korea-persona-interview.git
cd korea-persona-interview
uv venv --python 3.12
source .venv/bin/activate
uv pip sync requirements.lock requirements-dev.lock
```

### Configure secrets

Copy the template and fill in your key.

```bash
cp .env.example .env
```

Edit `.env` and set `OPENAI_API_KEY=sk-...`. The project root `.env` file is auto-loaded with `setdefault` semantics, so an `OPENAI_API_KEY` already exported in your shell wins. `.env` is gitignored.

### Run the test suite

```bash
pytest tests/ -v
```

The current regression covers 571 tests across rounds A through G plus the v1.1.1 mcp.mode toggle (config, filter DSL, persona loader, LLM client, interview session, persona drift, batch runner, report quant, MCP dispatch in both server and sampling modes, error messages, logging, CLI integration, --persona-id, --resume, streaming, LLM-as-judge drift, structured-summary v2 backward compatibility, and the McpConfig whitelist). All 571 tests must pass before opening a pull request.

Manual smoke tests that exercise a real OpenAI API call live under `tests/manual/` and are excluded from the default run.

## Project structure

The full directory tree is documented in the README under `Project Structure`. Quick map below for orientation.

- `main.py` - click CLI entry point
- `src/` - application code (config loader, LLM client, persona loader, interview engine, batch runner, report, MCP server)
- `tests/` - 571-test regression
- `docs/` - PRD, TDD, ADR, UI, tasks, v1.2.0 backlog
- `prompts/system_prompt.txt` - editable system prompt template
- `config.yaml` - annotated default config

For substantive design changes, read [docs/INDEX.md](docs/INDEX.md) first. It carries the canonical decisions for exit codes, CLI surface, heuristic thresholds, MCP tooling, and backend choice.

## Commit guidelines

- Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) prefixes
  - `feat:` for user-facing new features
  - `fix:` for bug fixes
  - `docs:` for documentation only
  - `chore:` for build, dependency, or tooling changes that do not affect runtime behavior
  - `refactor:` for code restructuring without behavior change
  - `test:` for test-only changes
  - `perf:` for performance improvements
- Do not put `Co-Authored-By` trailers on commits
- Keep one logical change per commit

## Branch strategy

- `main` is the release branch. Tagged releases (`v1.0.0`, ...) live here
- Feature branches use Conventional Commits prefixes (`feat/`, `fix/`, `chore/`, `docs/`, `refactor/`, `test/`)
- Open pull requests against `main`. The repository does not currently use a `develop` branch, so feature work targets `main` directly until that changes
- Delete the feature branch after the pull request is merged

## Pull request checklist

Before opening a pull request, run through the list below.

- All 571 regression tests pass (`pytest tests/ -v`)
- Lint and format are not pinned in v1.x (see `Lint` note below). Editor-side `ruff` or `black` is fine, but do not commit lint config files
- Documentation is updated for any user-visible change
  - User-facing CLI or output change: update README and the relevant `docs/prd/` or `docs/tdd/` section
  - Architecture or backend change: add an ADR under `docs/adr/{YYYY-MM-DD}-{title}.md` and add a row to `docs/INDEX.md` section 5 (revision history)
  - New environment variable or config key: update README `Configuration` and `config.yaml`
- Commit messages use Conventional Commits and have no `Co-Authored-By` trailers

### Lint

A formal lint and format toolchain is intentionally not pinned in v1.x. The codebase reads cleanly with default formatting rules. A `ruff` and pre-commit setup is on the v1.2.0 backlog (see [docs/backlog/v1.2.0.md](docs/backlog/v1.2.0.md)). Until then, run any formatter you like locally and discard the config diff before committing.

## Reporting bugs and proposing features

Open an issue at https://github.com/binaryloader/korea-persona-interview/issues. There is no fixed template yet. A useful issue includes the following.

- For bugs: command line that reproduces the problem, expected vs. actual output, Python version, OS, and the relevant `outputs/logs/run_*.jsonl` excerpt with API keys redacted
- For features: the user goal, the smallest change that would unblock the goal, and a pointer to any related v1.2.0 backlog item

For substantive feature work (anything that touches `src/interview.py`, `src/batch.py`, `src/report.py`, or the MCP tool surface), open an issue first so we can agree on the approach before code is written.

## Reporting security issues

Do not file public issues for vulnerabilities. The disclosure process is documented in [SECURITY.md](SECURITY.md).

## Code of conduct

This project has no formal code of conduct yet. Be respectful, keep technical discussions in the open issue or pull request, and assume good faith on the other side. Behavior that targets a person rather than the work is grounds for removal from the discussion.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE) of this project.
