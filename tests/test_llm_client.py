"""``MlxLLMClient`` 단위/통합 테스트.

- ``healthcheck``: 200 정상, 5xx, 4xx, 빈 data, 연결 실패
- ``chat``: chat_template_kwargs 포함, enable_thinking on/off에 따른 reasoning 처리
- 재시도 백오프(5xx → retry, 4xx → 즉시 ConfigError)
- 빈 content → ``EmptyResponseError`` 거쳐 retry
- localhost 외 base_url에서 chat 차단
- ``async with`` 미진입 시 RuntimeError

LLM 호출은 100% ``pytest-httpx``로 모킹한다.
"""

from __future__ import annotations

import httpx
import pytest

from src.config import LlmConfig
from src.llm_client import MlxLLMClient
from src.models import (
    ChatResponse,
    ConfigError,
    EmptyResponseError,
    RetryExhaustedError,
    ServerNotReachableError,
)


def _make_llm_config(
    *,
    base_url: str = "http://localhost:8080/v1",
    enable_thinking: bool = False,
    retry_max_attempts: int = 3,
) -> LlmConfig:
    return LlmConfig(
        base_url=base_url,
        model="test-model",
        max_tokens=128,
        temperature=0.5,
        timeout=5.0,
        context_budget=8000,
        retry_max_attempts=retry_max_attempts,
        # 백오프 0초로 두면 테스트가 빠르게 끝난다.
        retry_backoff_seconds=(0.0, 0.0, 0.0),
        enable_thinking=enable_thinking,
    )


# ---------------------------------------------------------------------------
# healthcheck
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthcheck_200_정상_모델리스트_반환(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8080/v1/models",
        json={"data": [{"id": "model-A"}, {"id": "model-B"}]},
        status_code=200,
    )

    async with MlxLLMClient(_make_llm_config()) as client:
        models = await client.healthcheck()

    assert models == ["model-A", "model-B"]


@pytest.mark.asyncio
async def test_healthcheck_data_비어있음_ServerNotReachableError(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8080/v1/models",
        json={"data": []},
        status_code=200,
    )

    async with MlxLLMClient(_make_llm_config()) as client:
        with pytest.raises(ServerNotReachableError):
            await client.healthcheck()


@pytest.mark.asyncio
async def test_healthcheck_5xx_ServerNotReachableError(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8080/v1/models",
        status_code=503,
    )

    async with MlxLLMClient(_make_llm_config()) as client:
        with pytest.raises(ServerNotReachableError):
            await client.healthcheck()


@pytest.mark.asyncio
async def test_healthcheck_4xx_ConfigError(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8080/v1/models",
        status_code=404,
        text="not found",
    )

    async with MlxLLMClient(_make_llm_config()) as client:
        with pytest.raises(ConfigError):
            await client.healthcheck()


@pytest.mark.asyncio
async def test_healthcheck_연결실패_ServerNotReachableError(httpx_mock) -> None:
    httpx_mock.add_exception(
        httpx.ConnectError("연결 거부"),
        url="http://localhost:8080/v1/models",
    )

    async with MlxLLMClient(_make_llm_config()) as client:
        with pytest.raises(ServerNotReachableError):
            await client.healthcheck()


# ---------------------------------------------------------------------------
# chat: 정상 응답 + chat_template_kwargs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_정상_응답_content_반환(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": "안녕하세요"},
                    "finish_reason": "stop",
                }
            ]
        },
        status_code=200,
    )

    async with MlxLLMClient(_make_llm_config()) as client:
        response = await client.chat([{"role": "user", "content": "안녕"}])

    assert isinstance(response, ChatResponse)
    assert response.content == "안녕하세요"
    assert response.retry_count == 0
    assert response.reasoning_trace is None


@pytest.mark.asyncio
async def test_chat_request_body_chat_template_kwargs_포함(httpx_mock) -> None:
    """body에 ``chat_template_kwargs.enable_thinking``이 항상 명시된다(GATE-1)."""

    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        status_code=200,
    )

    async with MlxLLMClient(_make_llm_config(enable_thinking=False)) as client:
        await client.chat([{"role": "user", "content": "안녕"}])

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    import json as _json

    body = _json.loads(requests[0].content)
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["model"] == "test-model"


@pytest.mark.asyncio
async def test_chat_enable_thinking_true_reasoning_보존(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "최종 답변",
                        "reasoning": "let me think...",
                    }
                }
            ]
        },
        status_code=200,
    )

    async with MlxLLMClient(_make_llm_config(enable_thinking=True)) as client:
        response = await client.chat([{"role": "user", "content": "안녕"}])

    assert response.content == "최종 답변"
    assert response.reasoning_trace == "let me think..."


@pytest.mark.asyncio
async def test_chat_enable_thinking_false_reasoning_무시(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "답",
                        "reasoning": "내부 추론",
                    }
                }
            ]
        },
        status_code=200,
    )

    async with MlxLLMClient(_make_llm_config(enable_thinking=False)) as client:
        response = await client.chat([{"role": "user", "content": "안녕"}])

    assert response.reasoning_trace is None


# ---------------------------------------------------------------------------
# chat: 재시도 정책
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_5xx_3회_retry_후_RetryExhausted(httpx_mock) -> None:
    for _ in range(3):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/v1/chat/completions",
            status_code=500,
        )

    async with MlxLLMClient(_make_llm_config(retry_max_attempts=3)) as client:
        with pytest.raises(RetryExhaustedError):
            await client.chat([{"role": "user", "content": "x"}])

    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_chat_4xx_즉시_ConfigError_no_retry(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        status_code=400,
        text="bad request",
    )

    async with MlxLLMClient(_make_llm_config(retry_max_attempts=3)) as client:
        with pytest.raises(ConfigError):
            await client.chat([{"role": "user", "content": "x"}])

    # 4xx는 retry 대상 아님 → 1회만 호출
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_chat_빈_content_EmptyResponseError_거쳐_retry(httpx_mock) -> None:
    """빈 content → 첫 시도 EmptyResponseError → retry → 두 번째 시도에서 정상."""

    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": ""}}]},
        status_code=200,
    )
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": "정상"}}]},
        status_code=200,
    )

    async with MlxLLMClient(_make_llm_config(retry_max_attempts=2)) as client:
        response = await client.chat([{"role": "user", "content": "x"}])

    assert response.content == "정상"
    assert response.retry_count == 1
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_chat_빈_content_모두_실패_RetryExhausted(httpx_mock) -> None:
    for _ in range(3):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/v1/chat/completions",
            json={"choices": [{"message": {"role": "assistant", "content": ""}}]},
            status_code=200,
        )

    async with MlxLLMClient(_make_llm_config(retry_max_attempts=3)) as client:
        with pytest.raises(RetryExhaustedError):
            await client.chat([{"role": "user", "content": "x"}])


@pytest.mark.asyncio
async def test_chat_타임아웃_retry_후_RetryExhausted(httpx_mock) -> None:
    for _ in range(3):
        httpx_mock.add_exception(
            httpx.ReadTimeout("timeout"),
            url="http://localhost:8080/v1/chat/completions",
        )

    async with MlxLLMClient(_make_llm_config(retry_max_attempts=3)) as client:
        with pytest.raises(RetryExhaustedError):
            await client.chat([{"role": "user", "content": "x"}])


# ---------------------------------------------------------------------------
# 보안: localhost 외 chat 차단
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_외부URL_ConfigError_차단(httpx_mock) -> None:
    """base_url이 localhost가 아니면 chat은 즉시 ConfigError로 차단된다."""

    cfg = _make_llm_config(base_url="https://api.example.com/v1")

    async with MlxLLMClient(cfg) as client:
        with pytest.raises(ConfigError):
            await client.chat([{"role": "user", "content": "사업 아이템 비밀"}])

    # 차단되어 외부 호출이 한 번도 발생하지 않는다.
    assert len(httpx_mock.get_requests()) == 0


# ---------------------------------------------------------------------------
# 컨텍스트 매니저 미진입
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_with_미진입_RuntimeError() -> None:
    client = MlxLLMClient(_make_llm_config())
    with pytest.raises(RuntimeError):
        await client.healthcheck()
