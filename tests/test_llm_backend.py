"""Unit tests for the LLM backend abstractions and implementations.

Covered surface:

- ``OpenAIBackend`` delegates to ``LLMClient`` and satisfies the
  ``LLMBackend`` runtime-checkable protocol.
- ``AnthropicBackend`` issues ``POST /v1/messages`` with ``x-api-key``,
  ``anthropic-version``, the ``system`` field separated from messages,
  retry/backoff policy parity with OpenAI, 401 -> ConfigError, and usage
  extraction from ``input_tokens``/``output_tokens``/``cache_read_input_tokens``.
- ``build_cli_backend`` selects backend by ``provider`` value.

The OpenAI client's HTTP semantics are exercised in ``test_llm_client.py``;
this module focuses on the backend abstraction layer.

McpSamplingBackend는 v1.2.0(ADR-005)에서 제거됐다. sampling 호환 클라이언트
보급률 한계로 실 사용 가치가 사라졌고, MCP orchestrator 모드가 호스트
sub-agent를 통해 같은 가치를 제공한다.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from src.config import LlmConfig
from src.llm_backend import (
    AnthropicBackend,
    LLMBackend,
    OpenAIBackend,
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
    anthropic_cache_control: bool = True,
    extra_chat_kwargs: tuple = (),
    streaming: bool = False,
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
        anthropic_cache_control=anthropic_cache_control,
        extra_chat_kwargs=extra_chat_kwargs,
        streaming=streaming,
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
        _make_llm_config(provider="anthropic", anthropic_cache_control=False)
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
    # cache_control 비활성 모드에서는 system이 단순 문자열로 박힌다.
    assert isinstance(body["system"], str)
    assert "프롬프트A" in body["system"]
    assert "프롬프트B" in body["system"]
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "안녕"
    assert request.headers["x-api-key"] == "test-key"
    assert request.headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_anthropic_backend_cache_control_default_on(httpx_mock) -> None:
    """cache_control 기본 ON 상태에서는 system이 ephemeral 마커가 박힌 list로 박힌다.

    Anthropic Messages API는 ``cache_control`` 마커가 박힌 system 블록을
    캐시 경계로 사용한다. 같은 system 텍스트를 반복 호출하면 정적 prefix가
    재사용되어 입력 토큰 단가가 줄어든다.
    """

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

    async with AnthropicBackend(_make_llm_config(provider="anthropic")) as backend:
        await backend.chat(
            [
                {"role": "system", "content": "당신은 30대 여성입니다"},
                {"role": "user", "content": "안녕"},
            ]
        )

    request = httpx_mock.get_requests()[0]
    import json as _json

    body = _json.loads(request.content)
    assert isinstance(body["system"], list)
    assert len(body["system"]) == 1
    block = body["system"][0]
    assert block["type"] == "text"
    assert "30대 여성" in block["text"]
    assert block["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_openai_backend_extra_chat_kwargs_request_body_머지(httpx_mock) -> None:
    """``extra_chat_kwargs``는 OpenAI 호환 request body에 그대로 머지된다.

    로컬 mlx_lm.server/vLLM이 받는 ``chat_template_kwargs`` 같은 필드를 yaml에서
    바로 박을 수 있게 한다. 예약 키(model/messages/max_tokens/temperature)는
    덮어쓰지 않는다.
    """

    httpx_mock.add_response(
        method="POST",
        url=f"{_OPENAI_BASE}/chat/completions",
        json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
        status_code=200,
    )

    cfg = _make_llm_config(
        provider="openai",
        extra_chat_kwargs=(
            ("chat_template_kwargs", {"enable_thinking": False}),
            ("logprobs", True),
        ),
    )
    async with OpenAIBackend(cfg) as backend:
        await backend.chat([{"role": "user", "content": "hi"}])

    request = httpx_mock.get_requests()[0]
    import json as _json

    body = _json.loads(request.content)
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["logprobs"] is True
    # 예약 키는 그대로 보존되어야 한다.
    assert "messages" in body
    assert "model" in body


@pytest.mark.asyncio
async def test_openai_backend_extra_chat_kwargs_예약_키_덮어쓰기_금지(httpx_mock) -> None:
    """``model``/``messages``/``max_tokens``/``temperature``는 ``extra_chat_kwargs``로 덮을 수 없다."""

    httpx_mock.add_response(
        method="POST",
        url=f"{_OPENAI_BASE}/chat/completions",
        json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
        status_code=200,
    )

    cfg = _make_llm_config(
        provider="openai",
        model="test-model",
        extra_chat_kwargs=(
            ("model", "evil-model"),
            ("temperature", 99.9),
        ),
    )
    async with OpenAIBackend(cfg) as backend:
        await backend.chat(
            [{"role": "user", "content": "hi"}], temperature=0.5
        )

    request = httpx_mock.get_requests()[0]
    import json as _json

    body = _json.loads(request.content)
    assert body["model"] == "test-model"
    assert body["temperature"] == 0.5


@pytest.mark.asyncio
async def test_anthropic_backend_cache_creation_tokens_합산_to_cached(httpx_mock) -> None:
    """``cache_creation_input_tokens`` + ``cache_read_input_tokens``가
    ``TokenUsage.cached_tokens`` 한 필드에 합산된다.

    OpenAI는 cached_tokens 한 값만 노출하므로 합산해 노출 일관성을 맞춘다.
    """

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
            "usage": {
                "input_tokens": 50,
                "output_tokens": 5,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 100,
            },
        },
        status_code=200,
    )

    async with AnthropicBackend(_make_llm_config(provider="anthropic")) as backend:
        response = await backend.chat(
            [
                {"role": "system", "content": "프롬프트"},
                {"role": "user", "content": "안녕"},
            ]
        )

    assert response.usage.cached_tokens == 300


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


# McpSamplingBackend, _convert_to_sampling_messages, _extract_sampling_text는
# v1.2.0(ADR-005)에서 모두 제거됐다. sampling 호환 클라이언트 보급률이 낮아 실
# 사용 가치가 사라졌고, MCP orchestrator 모드가 호스트 sub-agent를 통해 같은
# 가치를 제공한다.
