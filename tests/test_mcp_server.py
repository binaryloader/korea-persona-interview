"""Unit tests for the MCP server tool dispatch.

The application-layer logic (``run_batch``, ``generate_report``, the LLM
backends) is exercised by other test modules; this module focuses on dispatch,
the ``mcp.mode`` toggle (server / orchestrator), and the response ``backend``
label invariant.

For each test the cwd is overridden with a yaml that pins ``mcp.mode`` and
provider backends are stubbed via ``pytest_httpx``.

McpSamplingBackend는 v1.2.0(ADR-005)에서 제거됐고, MCP orchestrator 모드가
호스트 sub-agent를 통해 같은 가치를 제공한다. orchestrator 모드 dispatch
테스트는 별도 모듈 분리 후 추가된다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from src import mcp_server as _mcp_server
from src.mcp_server import (
    _HEALTHCHECK_SCHEMA,
    _INTERVIEW_SCHEMA,
    _LIST_PERSONAS_SCHEMA,
    _REPORT_SCHEMA,
    _TOOL_HANDLERS,
    _build_backend,
    _list_tools_metadata,
    _to_json_text,
    dispatch_tool,
)


def _pin_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Pin ``mcp.mode`` for the in-flight tool call.

    The MCP server handlers all call ``load_config(yaml_path=None, ...)`` which
    resolves to ``Path("config.yaml")`` relative to the current working
    directory. We move into ``tmp_path`` and write a yaml that only sets the
    mode toggle so the rest of the defaults stay intact.
    """

    monkeypatch.chdir(tmp_path)
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(f"mcp:\n  mode: '{mode}'\n", encoding="utf-8")


def test_tool_handlers_네_개_도구_등록() -> None:
    assert set(_TOOL_HANDLERS.keys()) == {
        "healthcheck",
        "list_personas",
        "interview",
        "report",
    }


def test_list_tools_metadata_server_mode_도구_8개() -> None:
    """v1.2.0(ADR-005): MCP server 모드는 healthcheck/list_personas/interview/
    report에 더해 helper 4개(detect_persona_drift, should_auto_follow_up,
    parse_structured_summary, interview_record_schema)도 노출한다."""

    from src.mcp_server import _list_tools_metadata_for_mode

    tools = _list_tools_metadata_for_mode("server")
    names = [t.name for t in tools]
    assert "healthcheck" in names
    assert "list_personas" in names
    assert "interview" in names
    assert "report" in names
    assert "detect_persona_drift" in names
    assert "should_auto_follow_up" in names
    assert "parse_structured_summary" in names
    assert "interview_record_schema" in names
    for tool in tools:
        assert tool.description
        assert tool.inputSchema


def test_list_tools_metadata_orchestrator_mode_도구() -> None:
    """v1.2.0(ADR-005): MCP orchestrator 모드는 interview 도구는 빠지고
    build_persona_prompt/build_batch_prompts/aggregate_results가 추가된다."""

    from src.mcp_server import _list_tools_metadata_for_mode

    tools = _list_tools_metadata_for_mode("orchestrator")
    names = [t.name for t in tools]
    assert "interview" not in names
    assert "build_persona_prompt" in names
    assert "build_batch_prompts" in names
    assert "aggregate_results" in names
    assert "healthcheck" in names
    assert "list_personas" in names
    assert "report" in names


def test_input_schema_타입과_required_필드() -> None:
    assert _HEALTHCHECK_SCHEMA["type"] == "object"
    assert _LIST_PERSONAS_SCHEMA["type"] == "object"
    assert _INTERVIEW_SCHEMA["type"] == "object"
    assert _REPORT_SCHEMA["type"] == "object"
    assert set(_INTERVIEW_SCHEMA["required"]) == {"product", "questions"}
    assert set(_REPORT_SCHEMA["required"]) == {"json_path"}


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_에러_응답() -> None:
    result = await dispatch_tool("does_not_exist", {})
    assert "error" in result
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_tool"


@pytest.mark.asyncio
async def test_dispatch_arguments_dict가_아니면_에러() -> None:
    result = await dispatch_tool("healthcheck", "not-a-dict")  # type: ignore[arg-type]
    assert "error" in result
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_dispatch_핸들러_예외_안전망(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """핸들러가 예외를 던지면 dispatch가 ``unhandled_exception`` 봉투로 감싼다."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "server")

    async def _broken_handler(arguments: dict) -> dict:
        raise RuntimeError("boom")

    from src.mcp_handlers import HANDLERS

    monkeypatch.setitem(HANDLERS, ("server", "healthcheck"), _broken_handler)
    result = await dispatch_tool("healthcheck", {})
    assert "error" in result
    assert result["ok"] is False
    assert result["error"]["code"] == "unhandled_exception"
    assert "boom" in result["error"]["message"]


def test_to_json_text_한국어_보존() -> None:
    text = _to_json_text({"msg": "안녕하세요"})
    assert "안녕하세요" in text
    payload = json.loads(text)
    assert payload["msg"] == "안녕하세요"


# ---------------------------------------------------------------------------
# healthcheck
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_healthcheck_server_mode_정상(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock
) -> None:
    """MCP server 모드 healthcheck: OpenAI ``/models`` 엔드포인트에 ping을 보내고
    응답 라벨에 ``mcp_server``가 박힌다."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-server-mode")
    _pin_mode(tmp_path, monkeypatch, "server")

    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "gpt-4o-mini"}]},
        status_code=200,
    )

    result = await dispatch_tool("healthcheck", {})

    assert result["ok"] is True
    assert result["backend"] == "mcp_server"


@pytest.mark.asyncio
async def test_handle_healthcheck_server_mode_키없음_ConfigError(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP server 모드인데 OPENAI_API_KEY 미설정: 친절한 ConfigError로 차단된다."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _pin_mode(tmp_path, monkeypatch, "server")

    result = await dispatch_tool("healthcheck", {})

    assert "error" in result
    assert result["error"]["code"] == "config_error"
    assert result["backend"] == "mcp_server"


# ---------------------------------------------------------------------------
# list_personas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_list_personas_정상(
    fake_load_dataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "server")

    result = await dispatch_tool(
        "list_personas",
        {"filter": "age:20-29", "limit": 2, "seed": 42},
    )

    assert result["ok"] is True
    assert result["count"] == 2
    assert result["filter"] == "age:20-29"
    assert result["seed"] == 42
    assert result["backend"] == "mcp_server"
    persona = result["personas"][0]
    assert "persona_id" in persona
    assert "raw" not in persona


@pytest.mark.asyncio
async def test_handle_list_personas_limit_검증(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))

    result = await dispatch_tool("list_personas", {"limit": 0})

    assert "error" in result
    assert result["error"]["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_handle_list_personas_필터_DSL_파싱_실패(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "server")

    result = await dispatch_tool(
        "list_personas",
        {"filter": "unsupported_key:foo"},
    )

    assert "error" in result
    assert result["error"]["code"] == "config_error"
    assert result["backend"] == "mcp_server"


@pytest.mark.asyncio
async def test_handle_list_personas_필터_결과_0건(
    fake_load_dataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "server")

    result = await dispatch_tool(
        "list_personas",
        {"filter": "age:90-100", "limit": 1},
    )

    assert "error" in result
    assert result["error"]["code"] == "filter_matched_zero"
    assert result["error"]["exit_code"] == 2
    assert result["backend"] == "mcp_server"


# ---------------------------------------------------------------------------
# interview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_interview_product_누락_에러(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # interview 도구는 server 모드에만 노출되므로 default(orchestrator)에서 차단되지 않도록 명시 pin한다.
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "server")

    result = await dispatch_tool(
        "interview",
        {"questions": ["쓸 의향?"]},
    )

    assert "error" in result
    assert result["error"]["code"] == "missing_argument"
    assert "product" in result["error"]["message"]


@pytest.mark.asyncio
async def test_handle_interview_questions_빈_리스트_에러(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "server")

    result = await dispatch_tool(
        "interview",
        {"product": "테스트 상품", "questions": []},
    )

    assert "error" in result
    assert result["error"]["code"] == "missing_argument"
    assert "questions" in result["error"]["message"]


@pytest.mark.asyncio
async def test_handle_interview_concurrency_범위_검증(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "server")

    result = await dispatch_tool(
        "interview",
        {
            "product": "테스트 상품",
            "questions": ["쓸 의향?"],
            "concurrency": 11,
        },
    )

    assert "error" in result
    assert result["error"]["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_handle_interview_n_범위_검증(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "server")

    result = await dispatch_tool(
        "interview",
        {
            "product": "테스트 상품",
            "questions": ["쓸 의향?"],
            "n": 0,
        },
    )

    assert "error" in result
    assert result["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_report_json_path_누락_에러(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))

    result = await dispatch_tool("report", {})

    assert "error" in result
    assert result["error"]["code"] == "missing_argument"


@pytest.mark.asyncio
async def test_handle_report_파일_미존재_에러(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))

    result = await dispatch_tool(
        "report",
        {"json_path": str(tmp_path / "nonexistent.json")},
    )

    assert "error" in result
    assert result["error"]["code"] == "input_file_not_found"


@pytest.mark.asyncio
async def test_handle_report_top_n_검증(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))

    fake_json = tmp_path / "fake.json"
    fake_json.write_text("{}", encoding="utf-8")

    result = await dispatch_tool(
        "report",
        {"json_path": str(fake_json), "top_n": 0},
    )

    assert "error" in result
    assert result["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------------
# _build_backend (mode 분기)
# ---------------------------------------------------------------------------


def test_build_backend_server_mode_OpenAIBackend(
    monkeypatch: pytest.MonkeyPatch,
    make_app_config,
) -> None:
    """MCP server 모드(provider=openai): OpenAIBackend 인스턴스를 반환한다."""

    from src.llm_backend import OpenAIBackend

    config = make_app_config(mcp_mode="server", provider="openai")
    backend = _build_backend(config)
    assert isinstance(backend, OpenAIBackend)


def test_build_backend_server_mode_AnthropicBackend(
    monkeypatch: pytest.MonkeyPatch,
    make_app_config,
) -> None:
    """MCP server 모드(provider=anthropic): AnthropicBackend 인스턴스를 반환한다."""

    from src.llm_backend import AnthropicBackend

    config = make_app_config(
        mcp_mode="server",
        provider="anthropic",
        base_url="https://api.anthropic.com/v1",
    )
    backend = _build_backend(config)
    assert isinstance(backend, AnthropicBackend)


def test_build_backend_orchestrator_mode_ConfigError(
    monkeypatch: pytest.MonkeyPatch,
    make_app_config,
) -> None:
    """MCP orchestrator 모드는 server-side LLM 호출이 없으므로 _build_backend는
    ConfigError로 차단한다(orchestrator 도구 핸들러가 본 함수를 호출하면 안 된다)."""

    from src.models import ConfigError

    config = make_app_config(mcp_mode="orchestrator", provider="openai")
    with pytest.raises(ConfigError) as exc_info:
        _build_backend(config)
    assert "orchestrator" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Default mode = "orchestrator" (ADR-005 + v1.2.0 후속 정리)
# ---------------------------------------------------------------------------


def test_mcp_default_mode_orchestrator() -> None:
    """yaml 미존재일 때 ``mcp.mode`` default는 ``orchestrator``다.

    v1.2.0 후속 정리에서 default가 ``server``에서 ``orchestrator``로 바뀌었다. orchestrator는 mcp.json env 추가 없이 즉시 동작해 신규 사용자 마찰이 가장 적다.
    """

    from src.config import load_config

    config = load_config(yaml_path=Path("/nonexistent/no.yaml"))
    assert config.mcp.mode == "orchestrator"


def test_backend_label_helper_server_mode(make_app_config) -> None:
    """``_backend_label`` 헬퍼는 MCP server 모드에서 ``mcp_server``를 돌려준다."""

    from src.mcp_server import _backend_label

    config = make_app_config(mcp_mode="server")
    assert _backend_label(config) == "mcp_server"


def test_backend_label_helper_orchestrator_mode(make_app_config) -> None:
    """``_backend_label`` 헬퍼는 MCP orchestrator 모드에서 ``mcp_orchestrator``를 돌려준다."""

    from src.mcp_server import _backend_label

    config = make_app_config(mcp_mode="orchestrator")
    assert _backend_label(config) == "mcp_orchestrator"
