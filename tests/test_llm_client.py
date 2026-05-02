"""``MlxLLMClient`` 단위/통합 테스트(OpenAI 백엔드).

- ``healthcheck``: 200 정상, 5xx, 4xx, 401, 빈 data, 연결 실패
- ``chat``: Authorization 헤더, model, messages 포함, 정상 응답 파싱
- 재시도 백오프(5xx → retry, 429 → retry, 4xx → 즉시 ConfigError, 401 → 즉시 ConfigError)
- 빈 content → ``EmptyResponseError`` 거쳐 retry
- API 키 누락 → ``ConfigError``로 즉시 차단(외부 호출 발생 안 함)
- ``async with`` 미진입 시 RuntimeError

LLM 호출은 100% ``pytest-httpx``로 모킹한다. v1.x부터 백엔드는 OpenAI Chat
Completions API다.
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


_API_BASE = "https://api.openai.com/v1"


def _make_llm_config(
    *,
    base_url: str = _API_BASE,
    api_key: str | None = "test-key",
    retry_max_attempts: int = 3,
) -> LlmConfig:
    return LlmConfig(
        base_url=base_url,
        model="test-model",
        max_tokens=128,
        temperature=0.5,
        timeout=5.0,
        context_budget=32000,
        retry_max_attempts=retry_max_attempts,
        # 백오프 0초로 두면 테스트가 빠르게 끝난다.
        retry_backoff_seconds=(0.0, 0.0, 0.0),
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# healthcheck
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthcheck_200_정상_모델리스트_반환(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_API_BASE}/models",
        json={"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]},
        status_code=200,
    )

    async with MlxLLMClient(_make_llm_config()) as client:
        models = await client.healthcheck()

    assert models == ["gpt-4o-mini", "gpt-4o"]


@pytest.mark.asyncio
async def test_healthcheck_data_비어있음_ServerNotReachableError(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_API_BASE}/models",
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
        url=f"{_API_BASE}/models",
        status_code=503,
    )

    async with MlxLLMClient(_make_llm_config()) as client:
        with pytest.raises(ServerNotReachableError):
            await client.healthcheck()


@pytest.mark.asyncio
async def test_healthcheck_401_키_무효_ConfigError(httpx_mock) -> None:
    """401 응답은 키 무효 안내 메시지로 즉시 ConfigError가 된다."""

    httpx_mock.add_response(
        method="GET",
        url=f"{_API_BASE}/models",
        status_code=401,
        text="invalid api key",
    )

    async with MlxLLMClient(_make_llm_config()) as client:
        with pytest.raises(ConfigError) as exc_info:
            await client.healthcheck()
    assert "API 키" in str(exc_info.value)


@pytest.mark.asyncio
async def test_healthcheck_4xx_ConfigError(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_API_BASE}/models",
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
        url=f"{_API_BASE}/models",
    )

    async with MlxLLMClient(_make_llm_config()) as client:
        with pytest.raises(ServerNotReachableError):
            await client.healthcheck()


@pytest.mark.asyncio
async def test_healthcheck_API키_누락_ConfigError_외부호출_없음(httpx_mock) -> None:
    """API 키 누락 시 외부 호출이 발생하지 않고 즉시 ConfigError가 raise된다."""

    async with MlxLLMClient(_make_llm_config(api_key=None)) as client:
        with pytest.raises(ConfigError) as exc_info:
            await client.healthcheck()
    assert "OPENAI_API_KEY" in str(exc_info.value)
    assert len(httpx_mock.get_requests()) == 0


# ---------------------------------------------------------------------------
# chat: 정상 응답 + 인증/요청 본문
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_정상_응답_content_반환(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_API_BASE}/chat/completions",
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
async def test_chat_request_body_표준_OpenAI_형식(httpx_mock) -> None:
    """body는 ``model``/``messages``/``max_tokens``/``temperature`` 4키만 포함한다."""

    httpx_mock.add_response(
        method="POST",
        url=f"{_API_BASE}/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        status_code=200,
    )

    async with MlxLLMClient(_make_llm_config()) as client:
        await client.chat([{"role": "user", "content": "안녕"}])

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    import json as _json

    body = _json.loads(requests[0].content)
    assert body["model"] == "test-model"
    assert body["messages"] == [{"role": "user", "content": "안녕"}]
    assert "chat_template_kwargs" not in body  # MLX 시절 필드 제거됨


@pytest.mark.asyncio
async def test_chat_Authorization_Bearer_헤더_포함(httpx_mock) -> None:
    """모든 호출에 ``Authorization: Bearer ${api_key}``가 첨부된다."""

    httpx_mock.add_response(
        method="POST",
        url=f"{_API_BASE}/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        status_code=200,
    )

    async with MlxLLMClient(_make_llm_config(api_key="sk-deadbeef")) as client:
        await client.chat([{"role": "user", "content": "안녕"}])

    requests = httpx_mock.get_requests()
    assert requests[0].headers.get("Authorization") == "Bearer sk-deadbeef"


@pytest.mark.asyncio
async def test_chat_API키_누락_ConfigError_외부호출_없음(httpx_mock) -> None:
    """API 키 누락 시 chat은 외부 호출 없이 ConfigError를 raise한다(시크릿 누락 방어)."""

    async with MlxLLMClient(_make_llm_config(api_key=None)) as client:
        with pytest.raises(ConfigError):
            await client.chat([{"role": "user", "content": "사업 아이템 비밀"}])

    assert len(httpx_mock.get_requests()) == 0


# ---------------------------------------------------------------------------
# chat: 재시도 정책
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_5xx_3회_retry_후_RetryExhausted(httpx_mock) -> None:
    for _ in range(3):
        httpx_mock.add_response(
            method="POST",
            url=f"{_API_BASE}/chat/completions",
            status_code=500,
        )

    async with MlxLLMClient(_make_llm_config(retry_max_attempts=3)) as client:
        with pytest.raises(RetryExhaustedError):
            await client.chat([{"role": "user", "content": "x"}])

    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_chat_429_RateLimit_retry_대상(httpx_mock) -> None:
    """OpenAI rate limit(429)은 5xx와 동일하게 재시도 대상이다."""

    httpx_mock.add_response(
        method="POST",
        url=f"{_API_BASE}/chat/completions",
        status_code=429,
        text="rate limit exceeded",
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_API_BASE}/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        status_code=200,
    )

    async with MlxLLMClient(_make_llm_config(retry_max_attempts=2)) as client:
        response = await client.chat([{"role": "user", "content": "x"}])

    assert response.content == "ok"
    assert response.retry_count == 1
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_chat_401_즉시_ConfigError_no_retry(httpx_mock) -> None:
    """401(키 무효)은 retry해도 같은 결과라 즉시 실패시킨다."""

    httpx_mock.add_response(
        method="POST",
        url=f"{_API_BASE}/chat/completions",
        status_code=401,
        text="invalid api key",
    )

    async with MlxLLMClient(_make_llm_config(retry_max_attempts=3)) as client:
        with pytest.raises(ConfigError) as exc_info:
            await client.chat([{"role": "user", "content": "x"}])

    assert "API 키" in str(exc_info.value)
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_chat_4xx_즉시_ConfigError_no_retry(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_API_BASE}/chat/completions",
        status_code=400,
        text="bad request",
    )

    async with MlxLLMClient(_make_llm_config(retry_max_attempts=3)) as client:
        with pytest.raises(ConfigError):
            await client.chat([{"role": "user", "content": "x"}])

    # 4xx(429 제외)는 retry 대상 아님 → 1회만 호출
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_chat_빈_content_EmptyResponseError_거쳐_retry(httpx_mock) -> None:
    """빈 content → 첫 시도 EmptyResponseError → retry → 두 번째 시도에서 정상."""

    httpx_mock.add_response(
        method="POST",
        url=f"{_API_BASE}/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": ""}}]},
        status_code=200,
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_API_BASE}/chat/completions",
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
            url=f"{_API_BASE}/chat/completions",
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
            url=f"{_API_BASE}/chat/completions",
        )

    async with MlxLLMClient(_make_llm_config(retry_max_attempts=3)) as client:
        with pytest.raises(RetryExhaustedError):
            await client.chat([{"role": "user", "content": "x"}])


# ---------------------------------------------------------------------------
# 응답 파싱: OpenAI 응답에는 reasoning 필드가 없다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_응답에_reasoning_있어도_무시_None_반환(httpx_mock) -> None:
    """OpenAI 응답에는 ``reasoning`` 필드가 없다. 호환 서버가 보내도 도메인
    모델은 항상 None으로 채운다(직렬화 backward compat 유지).
    """

    httpx_mock.add_response(
        method="POST",
        url=f"{_API_BASE}/chat/completions",
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "본문",
                        "reasoning": "ignored",
                    }
                }
            ]
        },
        status_code=200,
    )

    async with MlxLLMClient(_make_llm_config()) as client:
        response = await client.chat([{"role": "user", "content": "x"}])

    assert response.content == "본문"
    assert response.reasoning_trace is None


# ---------------------------------------------------------------------------
# 컨텍스트 매니저 미진입
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_with_미진입_RuntimeError() -> None:
    client = MlxLLMClient(_make_llm_config())
    with pytest.raises(RuntimeError):
        await client.healthcheck()
