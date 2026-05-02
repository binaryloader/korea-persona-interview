"""OpenAI Chat Completions HTTP client.

Async client built on ``httpx.AsyncClient`` with retry, timeout, and content
extraction. The official ``openai`` SDK is intentionally not used to keep
dependencies minimal; backoff is a six-line in-house implementation.

The same client is used unchanged for OpenAI-compatible local servers
(``mlx_lm.server``, ``vLLM``, ``llama.cpp``). Configure ``base_url`` and any
non-empty ``api_key`` to talk to those.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

import httpx

from .config import LlmConfig
from .models import (
    ChatResponse,
    ConfigError,
    EmptyResponseError,
    RetryExhaustedError,
    ServerNotReachableError,
    TokenUsage,
)


logger = logging.getLogger(__name__)


_JITTER_MAX_SECONDS = 0.5

_MISSING_API_KEY_MESSAGE = (
    "OpenAI API 키가 설정되지 않았습니다. https://platform.openai.com/api-keys "
    "에서 발급 후 환경변수 OPENAI_API_KEY로 셸에 적용하거나"
    "(`export OPENAI_API_KEY=sk-...`) 프로젝트 루트의 .env 파일에 "
    "`OPENAI_API_KEY=...` 형식으로 저장해 주세요"
)

_INVALID_API_KEY_MESSAGE = (
    "OpenAI API 키가 유효하지 않거나 권한이 없습니다. "
    "환경변수 OPENAI_API_KEY를 다시 확인해 주세요"
)


class MlxLLMClient:
    """Async client for the OpenAI Chat Completions API and compatible servers.

    Class name is preserved for import compatibility with earlier releases of
    this package. New code should depend on the ``LLMBackend`` protocol from
    ``llm_backend`` rather than this concrete class.

    Example::

        async with MlxLLMClient(cfg.llm) as client:
            models = await client.healthcheck()
            response = await client.chat(messages, max_tokens=500)

    Retry policy: HTTP 5xx, 429, timeouts, and connect failures are retried
    with exponential backoff up to ``retry_max_attempts`` times. 401 and other
    4xx responses fail fast. ``RetryExhaustedError`` is raised when retries
    are exhausted.

    A missing API key is rejected with ``ConfigError`` before any HTTP call.
    """

    def __init__(self, config: LlmConfig) -> None:
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "MlxLLMClient":
        self._client = httpx.AsyncClient(timeout=self._config.timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def healthcheck(self) -> list:
        """Verify connectivity by listing models.

        Returns:
            Model ids extracted from ``data[*].id``.

        Raises:
            ServerNotReachableError: Network failure, 5xx, or empty payload.
            ConfigError: 401 or other 4xx response, or missing API key.
        """

        self._require_api_key()
        url = f"{self._config.base_url.rstrip('/')}/models"
        client = self._require_client()

        try:
            response = await client.get(url, headers=self._auth_headers())
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ServerNotReachableError(
                f"OpenAI 서버 연결 실패: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ServerNotReachableError(
                f"OpenAI 서버 응답 타임아웃: {exc}"
            ) from exc

        if 500 <= response.status_code < 600:
            raise ServerNotReachableError(
                f"OpenAI 서버 5xx 응답: {response.status_code}"
            )
        if response.status_code == 401:
            raise ConfigError(_INVALID_API_KEY_MESSAGE)
        if 400 <= response.status_code < 500:
            raise ConfigError(
                f"OpenAI 서버 4xx 응답: {response.status_code} {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ServerNotReachableError(
                f"OpenAI 서버 응답 JSON 파싱 실패: {exc}"
            ) from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            raise ServerNotReachableError(
                "OpenAI 서버 /models 응답의 data가 비어 있다"
            )

        model_ids = [
            item.get("id", "")
            for item in data
            if isinstance(item, dict)
        ]
        logger.info(
            "헬스체크 응답 정상",
            extra={
                "base_url": self._config.base_url,
                "model_count": len(model_ids),
            },
        )
        return model_ids

    async def chat(
        self,
        messages: list,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> ChatResponse:
        """Send a chat completion request and return the response.

        Args:
            messages: OpenAI-shaped messages array.
            max_tokens: Per-call override of ``LlmConfig.max_tokens``.
            temperature: Per-call override of ``LlmConfig.temperature``.

        Raises:
            ConfigError: Missing or invalid API key, or 4xx response.
            ServerNotReachableError: Single-call network failure.
            RetryExhaustedError: 5xx, 429, timeout, or empty content beyond
                the configured retry limit.
        """

        self._require_api_key()

        client = self._require_client()
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        body = {
            "model": self._config.model,
            "messages": messages,
            "max_tokens": max_tokens or self._config.max_tokens,
            "temperature": (
                temperature
                if temperature is not None
                else self._config.temperature
            ),
        }
        # ``extra_chat_kwargs`` lets users forward backend-specific request
        # fields that fall outside the OpenAI Chat Completions spec, such as
        # ``chat_template_kwargs`` for mlx_lm.server / vLLM thinking toggles
        # on Qwen3 models. Reserved keys (``model``/``messages``/
        # ``max_tokens``/``temperature``) are skipped to keep the canonical
        # request body shape intact.
        extras = self._config.extra_chat_kwargs_dict()
        for key, value in extras.items():
            if key in body:
                continue
            body[key] = value

        # Message bodies stay out of the structured log per security policy.
        # Only counts and char totals are recorded.
        logger.debug(
            "chat 요청 시작",
            extra={
                "messages_count": len(messages),
                "messages_total_chars": sum(
                    len(m.get("content", "")) if isinstance(m, dict) else 0
                    for m in messages
                ),
                "model": self._config.model,
            },
        )

        last_exc: Optional[Exception] = None
        for attempt in range(self._config.retry_max_attempts):
            start = asyncio.get_event_loop().time()
            try:
                response = await client.post(
                    url, json=body, headers=self._auth_headers()
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_exc = ServerNotReachableError(f"연결 실패: {exc}")
            except httpx.TimeoutException as exc:
                last_exc = ServerNotReachableError(f"타임아웃: {exc}")
            else:
                latency_ms = int((asyncio.get_event_loop().time() - start) * 1000)
                if response.status_code == 401:
                    raise ConfigError(_INVALID_API_KEY_MESSAGE)
                if response.status_code == 429:
                    last_exc = ServerNotReachableError(
                        f"429 rate limit: {response.text[:200]}"
                    )
                elif 400 <= response.status_code < 500:
                    raise ConfigError(
                        f"chat 4xx 응답: {response.status_code} "
                        f"{response.text[:200]}"
                    )
                elif 500 <= response.status_code < 600:
                    last_exc = ServerNotReachableError(
                        f"5xx 응답: {response.status_code}"
                    )
                else:
                    content = self._extract_message_content(response)
                    if not content:
                        last_exc = EmptyResponseError(
                            "응답 message.content가 비어 있다"
                        )
                    else:
                        usage = self._extract_usage(response)
                        logger.info(
                            "chat 응답 정상",
                            extra={
                                "model": self._config.model,
                                "response_chars": len(content),
                                "latency_ms": latency_ms,
                                "retry_count": attempt,
                                "prompt_tokens": usage.prompt_tokens,
                                "completion_tokens": usage.completion_tokens,
                                "cached_tokens": usage.cached_tokens,
                            },
                        )
                        return ChatResponse(
                            content=content,
                            latency_ms=latency_ms,
                            retry_count=attempt,
                            reasoning_trace=None,
                            usage=usage,
                        )

            if attempt < self._config.retry_max_attempts - 1:
                backoff = self._compute_backoff(attempt)
                logger.warning(
                    "chat 재시도",
                    extra={
                        "attempt": attempt + 1,
                        "backoff_seconds": round(backoff, 3),
                        "reason": str(last_exc),
                    },
                )
                await asyncio.sleep(backoff)

        raise RetryExhaustedError(
            f"chat 재시도 {self._config.retry_max_attempts}회 모두 실패: {last_exc}"
        )

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "MlxLLMClient는 async with 블록 안에서만 사용할 수 있다"
            )
        return self._client

    def _require_api_key(self) -> None:
        if not self._config.api_key:
            raise ConfigError(_MISSING_API_KEY_MESSAGE)

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._config.api_key}"}

    def _compute_backoff(self, attempt: int) -> float:
        seq = self._config.retry_backoff_seconds
        if not seq:
            base = 1.0
        else:
            base = float(seq[min(attempt, len(seq) - 1)])
        return base + random.uniform(0, _JITTER_MAX_SECONDS)

    @staticmethod
    def _extract_message_content(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        return content if isinstance(content, str) else ""

    @staticmethod
    def _extract_usage(response: httpx.Response) -> TokenUsage:
        """Extract ``TokenUsage`` from an OpenAI ``usage`` block.

        Returns zeros for mocked responses or compatible servers that omit
        the field. ``cached_tokens`` lives under
        ``usage.prompt_tokens_details.cached_tokens`` per the OpenAI schema.
        """

        try:
            payload = response.json()
        except ValueError:
            return TokenUsage()
        if not isinstance(payload, dict):
            return TokenUsage()
        usage_raw = payload.get("usage")
        if not isinstance(usage_raw, dict):
            return TokenUsage()

        def _as_int(value) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        prompt = _as_int(usage_raw.get("prompt_tokens"))
        completion = _as_int(usage_raw.get("completion_tokens"))
        total = _as_int(usage_raw.get("total_tokens"))
        details = usage_raw.get("prompt_tokens_details")
        cached = 0
        if isinstance(details, dict):
            cached = _as_int(details.get("cached_tokens"))
        return TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cached_tokens=cached,
        )
