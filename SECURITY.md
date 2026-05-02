# Security Policy

## Supported versions

Only the latest minor release line receives security fixes. The current supported line is below. The table is updated when a new minor or major release ships.

| Version | Supported |
| --- | --- |
| 1.2.x | Yes |
| 1.1.x | 30-day EOL grace window for security-only fixes (until 2026-06-01) |
| 1.0.x | No (superseded by 1.1.x and 1.2.x) |
| < 1.0.0 | No (no prior public release) |

v1.2.0 shipped on 2026-05-02. v1.1.x is now in a 30-day EOL grace window for security-only fixes; new feature work targets v1.2.x.

## Reporting a vulnerability

Do not open a public GitHub issue for a security vulnerability. Use one of the private channels below instead.

- Email binaryloader.official@gmail.com with `[security][korea-persona-interview]` in the subject line
- Or use GitHub Private Vulnerability Reporting on the repository (Security tab -> Report a vulnerability) once the maintainer enables it

Please include the following.

- A description of the issue and the impact you can demonstrate
- A minimal reproduction (command line, input, environment) with API keys redacted
- The commit SHA or release tag you tested against
- Whether you believe the issue is exploitable in default configuration or only in a custom setup

### Response SLA

- Initial acknowledgement within 7 days of receipt
- Triage and severity assignment within 14 days
- Fix or mitigation timeline communicated after triage. Critical issues are targeted for a same-week patch release, others land in the next scheduled release

We will credit you in the release notes unless you ask to remain anonymous.

## Out of scope

The items below are not vulnerabilities of this project and should be reported to the upstream owner instead.

- Bias, hallucination, or sensitive content produced by the OpenAI model itself. Report to OpenAI at https://openai.com/security
- Bias or coverage gaps inside the NVIDIA Nemotron-Personas-Korea synthetic dataset. Report to NVIDIA via the dataset card on Hugging Face
- Issues in `httpx`, `datasets`, `aiohttp`, `mcp`, `click`, `pyyaml`, `tqdm`, or other transitive dependencies. Report upstream and mention the issue here only if a workaround on this project's side is needed
- Bugs that require an attacker to already have shell access on the user's machine (this project trusts the local filesystem and the local environment as documented in the security summary below)

## Security summary

The points below are the load-bearing security properties of this project. Any deviation from this list is in scope for the report channel above.

- API keys are read from the environment (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY` depending on `provider`) or a project-root `.env` file only. The tool never writes the key to logs, result JSON, or the markdown report. The structured logger masks anything matching the key shape before emitting
- The `.env` parser uses `setdefault` semantics so a key already set in the shell is never overridden. `.env` is gitignored. A project-root `.env` is the recommended single source for API keys; storing the key inside an agent's mcp.json `env` block still works but is discouraged because mcp.json is plaintext and more likely to leak through git, dotfile sync, or screenshots
- The `--product` text and persona metadata used for each interview are sent to whichever LLM backend you configure. For CLI and MCP server mode the destination is one of the OpenAI Chat Completions API, the Anthropic Messages API, or an OpenAI-compatible local server. For MCP orchestrator mode the destination is whichever LLM the host agent's sub-agent calls. The exact destination is determined by `provider`, `base_url`, and `mcp.mode`. This is documented in the README `Limitations and Disclaimer` section and ADR-002 / ADR-003 / ADR-005. Do not put unreleased intellectual property, trade secrets, or personally identifiable information into `--product`
- No external telemetry. The only outbound calls are to the configured LLM backend and (on first run) the Hugging Face Hub for the dataset download. The MCP orchestrator mode performs no direct outbound LLM call from this process; the host agent's sub-agent issues the call instead
- All result JSON files and markdown reports are written to the local `outputs/` directory, which is gitignored. The MCP server returns paths to these local files, not their contents over the network

## Dependency hygiene

- `requirements.lock` and `requirements-dev.lock` pin the full transitive graph and are committed to the repository
- `aiohttp` is bound to `>=3.13.5,<3.14` to address GHSA-9548-qrrj-x5pj. The bound is held under 3.14 because the upstream patch is only available in 3.14+, which has not shipped a stable release yet
- We follow the SLA in `dependency.md` for upstream CVEs (Critical 24 hours, High 7 days, Medium 30 days, Low next release)
