"""MCP 서버 dispatch 단위 테스트.

도구 로직 자체(``run_batch``, ``generate_report``, ``MlxLLMClient``)는 다른
테스트 모듈에서 이미 커버하므로 본 모듈은 dispatch만 집중적으로 검증한다.

- 도구 메타(이름, 입력 스키마)
- 알려지지 않은 도구 / 잘못된 인자 dispatch 동작
- 4개 도구의 입력 검증과 정상/에러 응답 형태
- 출력은 ``{"ok": true, ...}`` 또는 ``{"error": {...}}`` 둘 중 하나
- 백엔드 선택(`_build_backend`)이 sampling 세션 가용 시 sampling 백엔드를 고르는지
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src import mcp_server as _mcp_server
from src.llm_backend import McpSamplingBackend, OpenAIBackend
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


# ---------------------------------------------------------------------------
# 도구 메타 / 등록 테이블
# ---------------------------------------------------------------------------


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
    # 각 도구에 description, inputSchema가 채워져 있어야 한다.
    for tool in tools:
        assert tool.description
        assert tool.inputSchema


def test_input_schema_타입과_required_필드() -> None:
    # interview만 product/questions가 required다.
    assert _HEALTHCHECK_SCHEMA["type"] == "object"
    assert _LIST_PERSONAS_SCHEMA["type"] == "object"
    assert _INTERVIEW_SCHEMA["type"] == "object"
    assert _REPORT_SCHEMA["type"] == "object"
    assert set(_INTERVIEW_SCHEMA["required"]) == {"product", "questions"}
    assert set(_REPORT_SCHEMA["required"]) == {"json_path"}


# ---------------------------------------------------------------------------
# dispatch 자체 동작
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_에러_응답() -> None:
    result = await dispatch_tool("does_not_exist", {})
    assert "error" in result
    assert result["error"]["code"] == "unknown_tool"


@pytest.mark.asyncio
async def test_dispatch_arguments_dict가_아니면_에러() -> None:
    # type ignore: arguments 인자에 일부러 잘못된 타입을 넣는다.
    result = await dispatch_tool("healthcheck", "not-a-dict")  # type: ignore[arg-type]
    assert "error" in result
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_dispatch_핸들러_예외_안전망(monkeypatch: pytest.MonkeyPatch) -> None:
    """핸들러가 예상치 못한 예외를 던져도 dict 응답으로 흡수해야 한다."""

    async def _broken_handler(arguments: dict) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setitem(_TOOL_HANDLERS, "healthcheck", _broken_handler)
    result = await dispatch_tool("healthcheck", {})
    assert "error" in result
    assert result["error"]["code"] == "unhandled_exception"
    assert "boom" in result["error"]["message"]


def test_to_json_text_한국어_보존() -> None:
    text = _to_json_text({"msg": "안녕하세요"})
    # ensure_ascii=False가 적용되어 한국어가 그대로 들어가야 한다.
    assert "안녕하세요" in text
    # 다시 파싱 가능한 유효 JSON.
    payload = json.loads(text)
    assert payload["msg"] == "안녕하세요"


# ---------------------------------------------------------------------------
# healthcheck 도구
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_healthcheck_정상(httpx_mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]},
        status_code=200,
    )
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    result = await dispatch_tool("healthcheck", {})

    assert result["ok"] is True
    assert result["base_url"] == "https://api.openai.com/v1"
    assert result["model"] == "gpt-4o-mini"
    assert "gpt-4o-mini" in result["models"]


@pytest.mark.asyncio
async def test_handle_healthcheck_API_키_누락_에러(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    # OPENAI_API_KEY 미설정.

    result = await dispatch_tool("healthcheck", {})

    assert "error" in result
    # 키 누락은 ConfigError 메시지에 키워드가 포함되어 api_key_invalid_or_missing 매핑.
    assert result["error"]["code"] == "api_key_invalid_or_missing"


@pytest.mark.asyncio
async def test_handle_healthcheck_model_override_적용(
    httpx_mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "gpt-4o"}]},
        status_code=200,
    )
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    result = await dispatch_tool("healthcheck", {"model": "gpt-4o"})

    assert result["ok"] is True
    assert result["model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# list_personas 도구
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_list_personas_정상(
    fake_load_dataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))

    result = await dispatch_tool(
        "list_personas",
        {"filter": "age:20-29", "limit": 2, "seed": 42},
    )

    assert result["ok"] is True
    assert result["count"] == 2
    assert result["filter"] == "age:20-29"
    assert result["seed"] == 42
    # 페르소나 dict에 raw가 포함되지 않아야 한다(토큰 절약).
    persona = result["personas"][0]
    assert "persona_id" in persona
    assert "raw" not in persona


@pytest.mark.asyncio
async def test_handle_list_personas_limit_검증(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))

    result = await dispatch_tool("list_personas", {"limit": 0})

    assert "error" in result
    assert result["error"]["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_handle_list_personas_필터_DSL_파싱_실패(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))

    result = await dispatch_tool(
        "list_personas",
        {"filter": "unsupported_key:foo"},
    )

    assert "error" in result
    assert result["error"]["code"] == "config_error"


@pytest.mark.asyncio
async def test_handle_list_personas_필터_결과_0건(
    fake_load_dataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))

    # 가짜 데이터셋은 65세 이상이 1명뿐. 90세 이상 필터로 0건을 만든다.
    result = await dispatch_tool(
        "list_personas",
        {"filter": "age:90-100", "limit": 1},
    )

    assert "error" in result
    assert result["error"]["code"] == "filter_matched_zero"
    assert result["error"]["exit_code"] == 2


# ---------------------------------------------------------------------------
# interview 도구
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_interview_product_누락_에러(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

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
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

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
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

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
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

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
async def test_handle_interview_API_키_누락_에러(
    fake_load_dataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 키 미설정 상태에서는 인터뷰 단계의 healthcheck에서 ConfigError로 차단된다."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    # OPENAI_API_KEY 미설정.

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
    assert result["error"]["code"] == "api_key_invalid_or_missing"


@pytest.mark.asyncio
async def test_handle_interview_정상_실행(
    fake_load_dataset,
    httpx_mock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_batch가 정상 종료되는 시나리오. healthcheck + chat 호출을 모두 모킹한다."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    # healthcheck (run_batch가 시작 직전 호출).
    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "gpt-4o-mini"}]},
        status_code=200,
    )
    # 단일턴 모드: 페르소나 1명당 메인 chat 호출 1회 + 구조화 요약 1회 = 2회.
    # pytest-httpx의 add_response는 1회용이라 두 번 등록한다.
    # 첫 번째는 인터뷰 본체 응답.
    httpx_mock.add_response(
        method="POST",
        url="https://api.openai.com/v1/chat/completions",
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "1) 가격이 합리적이라 한번 써보고 싶어요.",
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 30,
                "total_tokens": 130,
            },
        },
        status_code=200,
    )
    # 두 번째는 구조화 요약 응답(JSON).
    httpx_mock.add_response(
        method="POST",
        url="https://api.openai.com/v1/chat/completions",
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "intent": "positive",
                                "willingness_to_pay": 30000,
                                "willingness_to_pay_currency": "KRW",
                                "rejection_reasons": [],
                                "one_line": "좋아 보입니다.",
                            },
                            ensure_ascii=False,
                        ),
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 80,
                "completion_tokens": 40,
                "total_tokens": 120,
            },
        },
        status_code=200,
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
    assert result["model"] == "gpt-4o-mini"
    # output_path가 tmp_path 안에 떨어져야 한다.
    assert result["output_path"].startswith(str(output_dir))


# ---------------------------------------------------------------------------
# report 도구
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

    # 임시 입력 파일 하나 만들어 둔다(없으면 이전 분기에서 떨어진다).
    fake_json = tmp_path / "fake.json"
    fake_json.write_text("{}", encoding="utf-8")

    result = await dispatch_tool(
        "report",
        {"json_path": str(fake_json), "top_n": 0},
    )

    assert "error" in result
    assert result["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------------
# 백엔드 선택 정책
# ---------------------------------------------------------------------------


def _make_app_config(backend_choice: str = "auto"):
    """간단한 ``AppConfig`` 빌더(make_app_config fixture와 별개로 본 테스트 한정)."""

    from src.config import (
        AppConfig,
        BatchConfig,
        DatasetConfig,
        InterviewConfig,
        LlmConfig,
        ReportConfig,
    )

    return AppConfig(
        llm=LlmConfig(
            base_url="https://api.openai.com/v1",
            model="test-model",
            max_tokens=100,
            temperature=0.5,
            timeout=5.0,
            context_budget=32000,
            retry_max_attempts=3,
            retry_backoff_seconds=(0.0,),
            api_key="test-key",
            backend=backend_choice,
        ),
        batch=BatchConfig(concurrency=1, persona_fields=("summary",)),
        dataset=DatasetConfig(
            name="x",
            split="train",
            field_map={},
            gender_aliases={},
            province_aliases={},
        ),
        interview=InterviewConfig(
            short_answer_threshold=20,
            english_ratio_threshold=0.30,
            ambiguous_keywords=(),
            refusal_keywords=(),
        ),
        report=ReportConfig(),
        output_dir=Path("/tmp"),
        log_level="INFO",
        no_color=True,
    )


def test_build_backend_auto_세션없음은_OpenAIBackend(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_ACTIVE_SERVER``가 None이면 sampling 세션도 None → OpenAIBackend."""

    monkeypatch.setattr(_mcp_server, "_ACTIVE_SERVER", None)
    backend = _build_backend(_make_app_config("auto"))
    assert isinstance(backend, OpenAIBackend)


def test_build_backend_auto_세션있음은_McpSamplingBackend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_current_sampling_session``이 더미 세션을 반환하면 McpSamplingBackend."""

    fake_session = object()

    def _fake_session_getter() -> Any:
        return fake_session

    monkeypatch.setattr(_mcp_server, "_current_sampling_session", _fake_session_getter)
    backend = _build_backend(_make_app_config("auto"))
    assert isinstance(backend, McpSamplingBackend)


def test_build_backend_openai_명시는_세션있어도_OpenAIBackend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``backend=openai``는 sampling 세션이 있어도 OpenAI를 강제한다."""

    fake_session = object()

    def _fake_session_getter() -> Any:
        return fake_session

    monkeypatch.setattr(_mcp_server, "_current_sampling_session", _fake_session_getter)
    backend = _build_backend(_make_app_config("openai"))
    assert isinstance(backend, OpenAIBackend)


@pytest.mark.asyncio
async def test_handle_healthcheck_backend_라벨_노출(
    httpx_mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """healthcheck 응답에 ``backend`` 라벨이 들어 있어야 한다(openai/mcp_sampling)."""

    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "gpt-4o-mini"}]},
        status_code=200,
    )
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # _ACTIVE_SERVER가 None이라 sampling 세션 없음 → openai 라벨
    monkeypatch.setattr(_mcp_server, "_ACTIVE_SERVER", None)

    result = await dispatch_tool("healthcheck", {})

    assert result["ok"] is True
    assert result["backend"] == "openai"
