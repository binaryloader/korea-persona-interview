"""Unit tests for the LLM backend abstractions and implementations.

Covered surface:

- ``OpenAIBackend`` delegates to ``MlxLLMClient`` and satisfies the
  ``LLMBackend`` runtime-checkable protocol.
- ``AnthropicBackend`` issues ``POST /v1/messages`` with ``x-api-key``,
  ``anthropic-version``, the ``system`` field separated from messages,
  retry/backoff policy parity with OpenAI, 401 -> ConfigError, and usage
  extraction from ``input_tokens``/``output_tokens``/``cache_read_input_tokens``.
- ``McpSamplingBackend`` sampling capability check, message conversion, empty
  response handling, and async with semantics with the MCP SDK fully mocked.
- ``build_cli_backend`` selects backend by ``provider`` value.

The OpenAI client's HTTP semantics are exercised in ``test_llm_client.py``;
this module focuses on the backend abstraction layer.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from src.config import LlmConfig
from src.llm_backend import (
    AnthropicBackend,
    LLMBackend,
    McpSamplingBackend,
    OpenAIBackend,
    _convert_to_sampling_messages,
    _extract_sampling_text,
    _split_system_prompt,
    build_cli_backend,
)
from src.models import (
    ChatResponse,
    ConfigError,
    RetryExhaustedError,
    ServerNotReachableError,
    TokenUsage,
)


_OPENAI_BASE = "https://api.openai.com/v1"
_ANTHROPIC_BASE = "https://api.anthropic.com/v1"


def _make_llm_config(
    *,
    provider: str = "openai",
    base_url: Optional[str] = None,
    model: str = "test-model",
    api_key: Optional[str] = "test-key",
) -> LlmConfig:
    if base_url is None:
        base_url = _ANTHROPIC_BASE if provider == "anthropic" else _OPENAI_BASE
    return LlmConfig(
        base_url=base_url,
        model=model,
        max_tokens=128,
        temperature=0.5,
        timeout=5.0,
        context_budget=32000,
        retry_max_attempts=2,
        retry_backoff_seconds=(0.0, 0.0),
        api_key=api_key,
        provider=provider,
    )


# ---------------------------------------------------------------------------
# build_cli_backend
# ---------------------------------------------------------------------------


def test_build_cli_backend_openai는_OpenAIBackend() -> None:
    backend = build_cli_backend(_make_llm_config(provider="openai"))
    assert isinstance(backend, OpenAIBackend)


def test_build_cli_backend_anthropic는_AnthropicBackend() -> None:
    backend = build_cli_backend(_make_llm_config(provider="anthropic"))
    assert isinstance(backend, AnthropicBackend)


def test_LlmConfig_허용외_provider_ConfigError() -> None:
    with pytest.raises(ConfigError):
        _make_llm_config(provider="cohere")


# ---------------------------------------------------------------------------
# OpenAIBackend (delegation wrapper)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_backend_healthcheck_위임(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_OPENAI_BASE}/models",
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
        url=f"{_OPENAI_BASE}/chat/completions",
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
    backend = OpenAIBackend(_make_llm_config())
    assert isinstance(backend, LLMBackend)


# ---------------------------------------------------------------------------
# AnthropicBackend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_backend_healthcheck_정상(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_ANTHROPIC_BASE}/messages",
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "pong"}],
            "model": "claude-haiku-4-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        status_code=200,
    )

    async with AnthropicBackend(
        _make_llm_config(provider="anthropic", model="claude-haiku-4-5")
    ) as backend:
        models = await backend.healthcheck()

    assert models == ["claude-haiku-4-5"]


@pytest.mark.asyncio
async def test_anthropic_backend_healthcheck_401_ConfigError(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_ANTHROPIC_BASE}/messages",
        json={"error": {"type": "authentication_error"}},
        status_code=401,
    )

    async with AnthropicBackend(
        _make_llm_config(provider="anthropic")
    ) as backend:
        with pytest.raises(ConfigError):
            await backend.healthcheck()


@pytest.mark.asyncio
async def test_anthropic_backend_healthcheck_500_ServerNotReachable(
    httpx_mock,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_ANTHROPIC_BASE}/messages",
        text="server error",
        status_code=503,
    )

    async with AnthropicBackend(
        _make_llm_config(provider="anthropic")
    ) as backend:
        with pytest.raises(ServerNotReachableError):
            await backend.healthcheck()


@pytest.mark.asyncio
async def test_anthropic_backend_chat_정상_응답_및_usage_매핑(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_ANTHROPIC_BASE}/messages",
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "네 좋아요"}],
            "model": "claude-haiku-4-5",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 4,
            },
        },
        status_code=200,
    )

    async with AnthropicBackend(
        _make_llm_config(provider="anthropic", model="claude-haiku-4-5")
    ) as backend:
        response = await backend.chat(
            [
                {"role": "system", "content": "당신은 30대 여성입니다"},
                {"role": "user", "content": "이 서비스 쓸 의향이 있나요?"},
            ],
            max_tokens=200,
            temperature=0.5,
        )

    assert response.content == "네 좋아요"
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 20
    assert response.usage.cached_tokens == 4
    assert response.usage.total_tokens == 30


@pytest.mark.asyncio
async def test_anthropic_backend_chat_request_body_system_분리(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_ANTHROPIC_BASE}/messages",
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-haiku-4-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        status_code=200,
    )

    async with AnthropicBackend(
        _make_llm_config(provider="anthropic")
    ) as backend:
        await backend.chat(
            [
                {"role": "system", "content": "프롬프트A"},
                {"role": "system", "content": "프롬프트B"},
                {"role": "user", "content": "안녕"},
            ]
        )

    request = httpx_mock.get_requests()[0]
    import json as _json
    body = _json.loads(request.content)
    assert "프롬프트A" in body["system"]
    assert "프롬프트B" in body["system"]
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "안녕"
    assert request.headers["x-api-key"] == "test-key"
    assert request.headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_anthropic_backend_chat_4xx_ConfigError(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_ANTHROPIC_BASE}/messages",
        text="bad request",
        status_code=400,
    )

    async with AnthropicBackend(
        _make_llm_config(provider="anthropic")
    ) as backend:
        with pytest.raises(ConfigError):
            await backend.chat([{"role": "user", "content": "x"}])


@pytest.mark.asyncio
async def test_anthropic_backend_chat_429_재시도_RetryExhausted(
    httpx_mock,
) -> None:
    for _ in range(2):
        httpx_mock.add_response(
            method="POST",
            url=f"{_ANTHROPIC_BASE}/messages",
            text="rate limit",
            status_code=429,
        )

    async with AnthropicBackend(
        _make_llm_config(provider="anthropic")
    ) as backend:
        with pytest.raises(RetryExhaustedError):
            await backend.chat([{"role": "user", "content": "x"}])


@pytest.mark.asyncio
async def test_anthropic_backend_chat_user_없으면_ConfigError() -> None:
    async with AnthropicBackend(
        _make_llm_config(provider="anthropic")
    ) as backend:
        with pytest.raises(ConfigError):
            await backend.chat([{"role": "system", "content": "프롬프트"}])


@pytest.mark.asyncio
async def test_anthropic_backend_api_key_누락_ConfigError() -> None:
    async with AnthropicBackend(
        _make_llm_config(provider="anthropic", api_key=None)
    ) as backend:
        with pytest.raises(ConfigError):
            await backend.chat([{"role": "user", "content": "x"}])


@pytest.mark.asyncio
async def test_anthropic_backend_프로토콜_만족() -> None:
    backend = AnthropicBackend(_make_llm_config(provider="anthropic"))
    assert isinstance(backend, LLMBackend)


def test_split_system_prompt_system_없으면_None() -> None:
    msgs, system = _split_system_prompt([{"role": "user", "content": "안녕"}])
    assert system is None
    assert msgs == [{"role": "user", "content": "안녕"}]


def test_split_system_prompt_알려지지_않은_role은_user로() -> None:
    msgs, _ = _split_system_prompt([{"role": "tool", "content": "x"}])
    assert msgs[0]["role"] == "user"


# ---------------------------------------------------------------------------
# McpSamplingBackend
# ---------------------------------------------------------------------------


class _FakeSamplingSession:
    """Test double mimicking the relevant ``ServerSession`` surface.

    - ``check_client_capability(cap)`` returns the boolean configured at init.
    - ``create_message(...)`` returns a ``CreateMessageResult`` carrying the
      configured response text, or raises ``raise_exc`` if set.
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

    assert models == []


@pytest.mark.asyncio
async def test_mcp_sampling_healthcheck_capability_없음_ConfigError() -> None:
    session = _FakeSamplingSession(supports_sampling=False)
    backend = McpSamplingBackend(session)

    with pytest.raises(ConfigError) as exc_info:
        await backend.healthcheck()
    message = str(exc_info.value)
    assert "sampling" in message
    assert "Claude Code" in message or "CLI" in message


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
    assert response.usage == TokenUsage()
    assert response.retry_count == 0
    assert session.last_call_kwargs is not None
    assert "30대 여성" in session.last_call_kwargs["system_prompt"]
    assert session.last_call_kwargs["max_tokens"] == 200
    assert session.last_call_kwargs["temperature"] == 0.5
    msgs = session.last_call_kwargs["messages"]
    assert len(msgs) == 1
    assert msgs[0].role == "user"


@pytest.mark.asyncio
async def test_mcp_sampling_chat_user_없으면_ConfigError() -> None:
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
    session = _FakeSamplingSession(response_text="")
    backend = McpSamplingBackend(session)

    with pytest.raises(RetryExhaustedError):
        await backend.chat([{"role": "user", "content": "x"}])


@pytest.mark.asyncio
async def test_mcp_sampling_chat_default_max_tokens() -> None:
    session = _FakeSamplingSession()
    backend = McpSamplingBackend(session, max_tokens_default=999)

    await backend.chat([{"role": "user", "content": "x"}])

    assert session.last_call_kwargs["max_tokens"] == 999


@pytest.mark.asyncio
async def test_mcp_sampling_async_with_지원() -> None:
    session = _FakeSamplingSession()
    async with McpSamplingBackend(session) as backend:
        response = await backend.chat([{"role": "user", "content": "x"}])
    assert response.content == "안녕"


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
        [{"role": "tool", "content": "result"}],
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
