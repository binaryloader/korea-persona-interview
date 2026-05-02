"""Unit tests for the MCP server tool dispatch.

The application-layer logic (``run_batch``, ``generate_report``, the LLM
backends) is exercised by other test modules; this module focuses on dispatch,
the ``mcp.mode`` toggle (server default vs sampling opt-in), and the response
``backend`` label invariant.

For sampling-mode tests the host is mocked by registering a fake session under
``_current_sampling_session`` and overriding the working directory with a
``config.yaml`` that pins ``mcp.mode: "sampling"``. For server-mode tests the
cwd is overridden with a yaml that pins ``mcp.mode: "server"`` and provider
backends are stubbed via monkeypatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from src import mcp_server as _mcp_server
from src.llm_backend import McpSamplingBackend
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


class _FakeSamplingSession:
    """Minimal stand-in for ``mcp.server.session.ServerSession``."""

    def __init__(
        self,
        *,
        supports_sampling: bool = True,
        responses: Optional[list] = None,
    ) -> None:
        self.supports_sampling = supports_sampling
        self._responses = list(responses or [])
        self._call_index = 0
        self.last_call_kwargs: Optional[dict] = None

    def check_client_capability(self, capability: Any) -> bool:
        return self.supports_sampling

    async def create_message(self, **kwargs: Any) -> Any:
        self.last_call_kwargs = kwargs
        from mcp import types

        if not self._responses:
            text = "안녕하세요"
        else:
            idx = min(self._call_index, len(self._responses) - 1)
            text = self._responses[idx]
            self._call_index += 1

        return types.CreateMessageResult(
            role="assistant",
            content=types.TextContent(type="text", text=text),
            model="claude-test",
            stopReason="endTurn",
        )


def _install_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    supports_sampling: bool = True,
    responses: Optional[list] = None,
) -> _FakeSamplingSession:
    """Register a fake sampling session for the in-flight tool call."""

    session = _FakeSamplingSession(
        supports_sampling=supports_sampling, responses=responses
    )
    monkeypatch.setattr(
        _mcp_server, "_current_sampling_session", lambda: session
    )
    return session


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


def test_list_tools_metadata_네_개_Tool_생성() -> None:
    tools = _list_tools_metadata()
    names = [t.name for t in tools]
    assert names == ["healthcheck", "list_personas", "interview", "report"]
    for tool in tools:
        assert tool.description
        assert tool.inputSchema


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
async def test_dispatch_핸들러_예외_안전망(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _broken_handler(arguments: dict) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setitem(_TOOL_HANDLERS, "healthcheck", _broken_handler)
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
async def test_handle_healthcheck_sampling_정상(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "sampling")
    _install_session(monkeypatch)

    result = await dispatch_tool("healthcheck", {})

    assert result["ok"] is True
    assert result["backend"] == "mcp_sampling"


@pytest.mark.asyncio
async def test_handle_healthcheck_sampling_세션없음_안내_메시지(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No MCP host attached and sampling mode: tool routes back to the CLI."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "sampling")
    monkeypatch.setattr(_mcp_server, "_current_sampling_session", lambda: None)

    result = await dispatch_tool("healthcheck", {})

    assert "error" in result
    assert result["error"]["code"] == "config_error"
    assert "CLI" in result["error"]["message"] or "main.py" in result["error"]["message"]
    assert result["backend"] == "mcp_sampling"


@pytest.mark.asyncio
async def test_handle_healthcheck_sampling_미지원_안내(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host attached but sampling capability missing: clear hint to the CLI."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "sampling")
    _install_session(monkeypatch, supports_sampling=False)

    result = await dispatch_tool("healthcheck", {})

    assert "error" in result
    assert result["error"]["code"] == "config_error"
    assert "Claude Code" in result["error"]["message"] or "CLI" in result["error"]["message"]
    assert result["backend"] == "mcp_sampling"


@pytest.mark.asyncio
async def test_handle_healthcheck_server_mode_정상(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock
) -> None:
    """server mode healthcheck: OpenAI ``/models`` 엔드포인트에 ping을 보내고
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
    """server mode인데 OPENAI_API_KEY 미설정: 친절한 ConfigError로 차단된다."""

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
async def test_handle_list_personas_sampling_라벨(
    fake_load_dataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sampling mode에서도 list_personas 응답에 mcp_sampling 라벨이 박힌다."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "sampling")

    result = await dispatch_tool(
        "list_personas",
        {"filter": "age:20-29", "limit": 2, "seed": 42},
    )

    assert result["ok"] is True
    assert result["backend"] == "mcp_sampling"


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
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))

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


@pytest.mark.asyncio
async def test_handle_interview_sampling_세션없음_안내(
    fake_load_dataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sampling mode + no MCP host: the interview tool routes back to the CLI."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "sampling")
    monkeypatch.setattr(_mcp_server, "_current_sampling_session", lambda: None)

    result = await dispatch_tool(
        "interview",
        {
            "product": "테스트 상품",
            "questions": ["쓸 의향?"],
            "n": 1,
            "concurrency": 1,
        },
    )

    assert "error" in result
    assert result["error"]["code"] == "config_error"
    assert result["backend"] == "mcp_sampling"


@pytest.mark.asyncio
async def test_handle_interview_sampling_정상_실행(
    fake_load_dataset,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sampling mode happy path. Two responses cover the interview body and
    structured summary."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "sampling")
    _install_session(
        monkeypatch,
        responses=[
            "1) 가격이 합리적이라 한번 써보고 싶어요.",
            json.dumps(
                {
                    "intent": "positive",
                    "willingness_to_pay": 30000,
                    "willingness_to_pay_currency": "KRW",
                    "rejection_reasons": [],
                    "one_line": "좋아 보입니다.",
                },
                ensure_ascii=False,
            ),
        ],
    )

    output_dir = tmp_path / "outputs"
    result = await dispatch_tool(
        "interview",
        {
            "product": "테스트 상품",
            "questions": ["쓸 의향?"],
            "filter": "age:20-29",
            "n": 1,
            "concurrency": 1,
            "single_turn": True,
            "output_dir": str(output_dir),
        },
    )

    assert "error" not in result, f"unexpected error: {result}"
    assert result["ok"] is True
    assert result["partial_failure"] is False
    assert result["summary"]["requested"] == 1
    assert result["backend"] == "mcp_sampling"
    assert result["output_path"].startswith(str(output_dir))


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


def test_build_backend_sampling_세션있음은_McpSamplingBackend(
    monkeypatch: pytest.MonkeyPatch,
    make_app_config,
) -> None:
    """sampling mode + 세션 존재: McpSamplingBackend 반환."""

    fake_session = _FakeSamplingSession()
    monkeypatch.setattr(
        _mcp_server, "_current_sampling_session", lambda: fake_session
    )
    config = make_app_config(mcp_mode="sampling")

    backend = _build_backend(config)
    assert isinstance(backend, McpSamplingBackend)


def test_build_backend_sampling_세션없음은_ConfigError(
    monkeypatch: pytest.MonkeyPatch,
    make_app_config,
) -> None:
    """sampling mode + 세션 부재: 친절한 ConfigError로 차단."""

    from src.models import ConfigError

    monkeypatch.setattr(_mcp_server, "_current_sampling_session", lambda: None)
    config = make_app_config(mcp_mode="sampling")

    with pytest.raises(ConfigError) as exc_info:
        _build_backend(config)
    msg = str(exc_info.value)
    assert "CLI" in msg or "main.py" in msg or "mcp.mode" in msg


def test_build_backend_server_mode_OpenAIBackend(
    monkeypatch: pytest.MonkeyPatch,
    make_app_config,
) -> None:
    """server mode(provider=openai): OpenAIBackend 인스턴스를 반환한다."""

    from src.llm_backend import OpenAIBackend

    config = make_app_config(mcp_mode="server", provider="openai")
    backend = _build_backend(config)
    assert isinstance(backend, OpenAIBackend)


def test_build_backend_server_mode_AnthropicBackend(
    monkeypatch: pytest.MonkeyPatch,
    make_app_config,
) -> None:
    """server mode(provider=anthropic): AnthropicBackend 인스턴스를 반환한다."""

    from src.llm_backend import AnthropicBackend

    config = make_app_config(
        mcp_mode="server",
        provider="anthropic",
        base_url="https://api.anthropic.com/v1",
    )
    backend = _build_backend(config)
    assert isinstance(backend, AnthropicBackend)


def test_build_backend_server_mode_세션없어도_OK(
    monkeypatch: pytest.MonkeyPatch,
    make_app_config,
) -> None:
    """server mode는 sampling 세션 부재와 무관하게 동작한다."""

    from src.llm_backend import OpenAIBackend

    monkeypatch.setattr(_mcp_server, "_current_sampling_session", lambda: None)
    config = make_app_config(mcp_mode="server", provider="openai")
    backend = _build_backend(config)
    assert isinstance(backend, OpenAIBackend)


# ---------------------------------------------------------------------------
# Default mode = "server" (ADR-004)
# ---------------------------------------------------------------------------


def test_mcp_default_mode_server() -> None:
    """yaml 미존재일 때 ``mcp.mode`` default는 ``server``다(ADR-004)."""

    from src.config import load_config

    config = load_config(yaml_path=Path("/nonexistent/no.yaml"))
    assert config.mcp.mode == "server"


def test_backend_label_helper_server_mode(make_app_config) -> None:
    """``_backend_label`` 헬퍼는 server mode에서 ``mcp_server``를 돌려준다."""

    from src.mcp_server import _backend_label

    config = make_app_config(mcp_mode="server")
    assert _backend_label(config) == "mcp_server"


def test_backend_label_helper_sampling_mode(make_app_config) -> None:
    """``_backend_label`` 헬퍼는 sampling mode에서 ``mcp_sampling``을 돌려준다."""

    from src.mcp_server import _backend_label

    config = make_app_config(mcp_mode="sampling")
    assert _backend_label(config) == "mcp_sampling"
