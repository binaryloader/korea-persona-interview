"""LLM 백엔드 추상화와 두 구현(``OpenAIBackend``, ``McpSamplingBackend``)의 단위 테스트.

검증 범위는 아래와 같다.

- 백엔드 선택 정책(``select_backend``의 auto/openai/mcp_sampling 분기)
- ``normalize_backend_choice``의 입력 검증
- ``OpenAIBackend``가 ``MlxLLMClient``로 위임하여 LLMBackend 프로토콜을 만족하는지
- ``McpSamplingBackend``의 sampling capability 확인, chat 변환, 응답 추출
- mcp 세션을 모킹해 실제 SDK가 없어도 단위 테스트가 동작하는지

OpenAI 호출 자체의 정상/에러 경로는 ``test_llm_client.py``가 이미 커버하므로 본 모듈은
백엔드 추상화 계층의 동작에만 집중한다.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
import pytest

from src.config import LlmConfig
from src.llm_backend import (
    LLMBackend,
    McpSamplingBackend,
    OpenAIBackend,
    _convert_to_sampling_messages,
    _extract_sampling_text,
    normalize_backend_choice,
    select_backend,
)
from src.models import (
    ChatResponse,
    ConfigError,
    RetryExhaustedError,
    ServerNotReachableError,
    TokenUsage,
)


_API_BASE = "https://api.openai.com/v1"


def _make_llm_config(
    *,
    backend: str = "auto",
    api_key: Optional[str] = "test-key",
) -> LlmConfig:
    return LlmConfig(
        base_url=_API_BASE,
        model="test-model",
        max_tokens=128,
        temperature=0.5,
        timeout=5.0,
        context_budget=32000,
        retry_max_attempts=2,
        retry_backoff_seconds=(0.0, 0.0),
        api_key=api_key,
        backend=backend,
    )


# ---------------------------------------------------------------------------
# normalize_backend_choice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "auto"),
        ("", "auto"),
        ("auto", "auto"),
        ("openai", "openai"),
        ("OPENAI", "openai"),
        ("mcp_sampling", "mcp_sampling"),
        ("  Auto  ", "auto"),
    ],
)
def test_normalize_backend_choice_허용값(value: Optional[str], expected: str) -> None:
    assert normalize_backend_choice(value) == expected


def test_normalize_backend_choice_허용외_값_ConfigError() -> None:
    with pytest.raises(ConfigError) as exc_info:
        normalize_backend_choice("anthropic")
    assert "anthropic" in str(exc_info.value)


# ---------------------------------------------------------------------------
# select_backend
# ---------------------------------------------------------------------------


def test_select_backend_openai_명시는_OpenAIBackend() -> None:
    backend = select_backend(
        config=_make_llm_config(),
        backend_choice="openai",
        sampling_session=None,
    )
    assert isinstance(backend, OpenAIBackend)


def test_select_backend_mcp_sampling_명시_세션없음_ConfigError() -> None:
    with pytest.raises(ConfigError) as exc_info:
        select_backend(
            config=_make_llm_config(),
            backend_choice="mcp_sampling",
            sampling_session=None,
        )
    assert "mcp_sampling" in str(exc_info.value)


def test_select_backend_mcp_sampling_명시_세션있음_McpSamplingBackend() -> None:
    fake_session = object()
    backend = select_backend(
        config=_make_llm_config(),
        backend_choice="mcp_sampling",
        sampling_session=fake_session,
    )
    assert isinstance(backend, McpSamplingBackend)


def test_select_backend_auto_세션없음은_OpenAIBackend() -> None:
    backend = select_backend(
        config=_make_llm_config(),
        backend_choice="auto",
        sampling_session=None,
    )
    assert isinstance(backend, OpenAIBackend)


def test_select_backend_auto_세션있음은_McpSamplingBackend() -> None:
    fake_session = object()
    backend = select_backend(
        config=_make_llm_config(),
        backend_choice="auto",
        sampling_session=fake_session,
    )
    assert isinstance(backend, McpSamplingBackend)


# ---------------------------------------------------------------------------
# OpenAIBackend (위임 wrapper)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_backend_healthcheck_위임(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_API_BASE}/models",
        json={"data": [{"id": "gpt-4o-mini"}]},
        status_code=200,
    )

    async with OpenAIBackend(_make_llm_config()) as backend:
        models = await backend.healthcheck()

    assert models == ["gpt-4o-mini"]


@pytest.mark.asyncio
async def test_openai_backend_chat_위임(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_API_BASE}/chat/completions",
        json={
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}}
            ]
        },
        status_code=200,
    )

    async with OpenAIBackend(_make_llm_config()) as backend:
        response = await backend.chat([{"role": "user", "content": "안녕"}])

    assert isinstance(response, ChatResponse)
    assert response.content == "ok"


@pytest.mark.asyncio
async def test_openai_backend_프로토콜_만족() -> None:
    """``OpenAIBackend``는 ``LLMBackend`` runtime_checkable 프로토콜을 만족해야 한다."""

    backend = OpenAIBackend(_make_llm_config())
    assert isinstance(backend, LLMBackend)


# ---------------------------------------------------------------------------
# McpSamplingBackend
# ---------------------------------------------------------------------------


class _FakeSamplingSession:
    """``ServerSession``의 일부 메서드만 흉내 내는 테스트 더블.

    - ``check_client_capability(cap)`` → 생성 시 받은 ``supports_sampling``
    - ``create_message(...)`` → 생성 시 받은 ``response_text``를 ``CreateMessageResult``
      형태로 돌려준다(또는 ``raise_exc``를 raise)
    """

    def __init__(
        self,
        *,
        supports_sampling: bool = True,
        response_text: str = "안녕",
        raise_exc: Optional[Exception] = None,
        capability_exc: Optional[Exception] = None,
    ) -> None:
        self.supports_sampling = supports_sampling
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.capability_exc = capability_exc
        self.last_call_kwargs: Optional[dict] = None

    def check_client_capability(self, capability: Any) -> bool:
        if self.capability_exc is not None:
            raise self.capability_exc
        return self.supports_sampling

    async def create_message(self, **kwargs: Any) -> Any:
        self.last_call_kwargs = kwargs
        if self.raise_exc is not None:
            raise self.raise_exc

        # ``CreateMessageResult``를 직접 만들어 반환한다(실제 SDK 형식).
        from mcp import types

        return types.CreateMessageResult(
            role="assistant",
            content=types.TextContent(type="text", text=self.response_text),
            model="claude-test",
            stopReason="endTurn",
        )


@pytest.mark.asyncio
async def test_mcp_sampling_healthcheck_capability_있음() -> None:
    session = _FakeSamplingSession(supports_sampling=True)
    backend = McpSamplingBackend(session)

    models = await backend.healthcheck()

    assert models == []  # sampling 표준은 모델 가용성 조회 API가 없다


@pytest.mark.asyncio
async def test_mcp_sampling_healthcheck_capability_없음_ServerNotReachable() -> None:
    session = _FakeSamplingSession(supports_sampling=False)
    backend = McpSamplingBackend(session)

    with pytest.raises(ServerNotReachableError) as exc_info:
        await backend.healthcheck()
    assert "sampling" in str(exc_info.value)


@pytest.mark.asyncio
async def test_mcp_sampling_healthcheck_capability_확인_실패도_ServerNotReachable() -> None:
    session = _FakeSamplingSession(
        capability_exc=RuntimeError("session not initialized")
    )
    backend = McpSamplingBackend(session)

    with pytest.raises(ServerNotReachableError):
        await backend.healthcheck()


@pytest.mark.asyncio
async def test_mcp_sampling_chat_정상_응답() -> None:
    session = _FakeSamplingSession(response_text="네 좋아요")
    backend = McpSamplingBackend(session)

    response = await backend.chat(
        [
            {"role": "system", "content": "당신은 30대 여성입니다"},
            {"role": "user", "content": "이 서비스 쓸 의향이 있나요?"},
        ],
        max_tokens=200,
        temperature=0.5,
    )

    assert isinstance(response, ChatResponse)
    assert response.content == "네 좋아요"
    assert response.usage == TokenUsage()  # sampling 표준에 usage 없음
    assert response.retry_count == 0
    # system_prompt가 분리되어 전달되어야 한다
    assert session.last_call_kwargs is not None
    assert "30대 여성" in session.last_call_kwargs["system_prompt"]
    # max_tokens/temperature가 전달되어야 한다
    assert session.last_call_kwargs["max_tokens"] == 200
    assert session.last_call_kwargs["temperature"] == 0.5
    # sampling_messages는 user 1개만 (system은 system_prompt로 분리)
    msgs = session.last_call_kwargs["messages"]
    assert len(msgs) == 1
    assert msgs[0].role == "user"


@pytest.mark.asyncio
async def test_mcp_sampling_chat_user_없으면_ConfigError() -> None:
    """messages가 system뿐이면 sampling 호출은 무의미하므로 ConfigError로 차단."""

    session = _FakeSamplingSession()
    backend = McpSamplingBackend(session)

    with pytest.raises(ConfigError):
        await backend.chat([{"role": "system", "content": "프롬프트"}])


@pytest.mark.asyncio
async def test_mcp_sampling_chat_클라이언트_거부_ServerNotReachable() -> None:
    session = _FakeSamplingSession(raise_exc=RuntimeError("user denied sampling"))
    backend = McpSamplingBackend(session)

    with pytest.raises(ServerNotReachableError) as exc_info:
        await backend.chat([{"role": "user", "content": "x"}])
    assert "sampling" in str(exc_info.value)


@pytest.mark.asyncio
async def test_mcp_sampling_chat_빈_응답_RetryExhausted() -> None:
    """클라이언트가 빈 텍스트를 반환하면 재시도 정책 없이 RetryExhausted로 본다."""

    session = _FakeSamplingSession(response_text="")
    backend = McpSamplingBackend(session)

    with pytest.raises(RetryExhaustedError):
        await backend.chat([{"role": "user", "content": "x"}])


@pytest.mark.asyncio
async def test_mcp_sampling_chat_default_max_tokens() -> None:
    """``max_tokens`` 인자를 생략하면 backend default가 적용된다."""

    session = _FakeSamplingSession()
    backend = McpSamplingBackend(session, max_tokens_default=999)

    await backend.chat([{"role": "user", "content": "x"}])

    assert session.last_call_kwargs["max_tokens"] == 999


@pytest.mark.asyncio
async def test_mcp_sampling_async_with_지원() -> None:
    """McpSamplingBackend는 LLMBackend 프로토콜의 async with도 만족해야 한다."""

    session = _FakeSamplingSession()
    async with McpSamplingBackend(session) as backend:
        response = await backend.chat([{"role": "user", "content": "x"}])
    assert response.content == "안녕"


# ---------------------------------------------------------------------------
# 변환 헬퍼
# ---------------------------------------------------------------------------


def test_convert_to_sampling_messages_system_분리() -> None:
    from mcp import types

    sampling_msgs, system_prompt = _convert_to_sampling_messages(
        [
            {"role": "system", "content": "프롬프트A"},
            {"role": "system", "content": "프롬프트B"},
            {"role": "user", "content": "안녕"},
            {"role": "assistant", "content": "네"},
        ],
        types,
    )

    assert "프롬프트A" in system_prompt
    assert "프롬프트B" in system_prompt
    assert len(sampling_msgs) == 2
    assert sampling_msgs[0].role == "user"
    assert sampling_msgs[1].role == "assistant"


def test_convert_to_sampling_messages_알려지지_않은_role은_user로() -> None:
    from mcp import types

    sampling_msgs, _ = _convert_to_sampling_messages(
        [
            {"role": "tool", "content": "result"},
        ],
        types,
    )

    assert len(sampling_msgs) == 1
    assert sampling_msgs[0].role == "user"


def test_convert_to_sampling_messages_system_없으면_None() -> None:
    from mcp import types

    _, system_prompt = _convert_to_sampling_messages(
        [{"role": "user", "content": "안녕"}],
        types,
    )
    assert system_prompt is None


def test_extract_sampling_text_TextContent_정상() -> None:
    from mcp import types

    result = types.CreateMessageResult(
        role="assistant",
        content=types.TextContent(type="text", text="응답 본문"),
        model="m",
    )
    assert _extract_sampling_text(result) == "응답 본문"


def test_extract_sampling_text_None_안전() -> None:
    class _Empty:
        content = None

    assert _extract_sampling_text(_Empty()) == ""
