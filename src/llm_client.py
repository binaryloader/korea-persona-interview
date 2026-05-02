"""OpenAI Chat Completions HTTP 클라이언트.

``httpx.AsyncClient``를 기반으로 retry, timeout, 응답 본문 추출을 묶은 async 클라이언트다. 공식 ``openai`` SDK는 의존성을 최소화하기 위해 의도적으로 사용하지 않는다. backoff 로직은 6줄짜리 자체 구현이다.

OpenAI 호환 로컬 서버(``mlx_lm.server``, ``vLLM``, ``llama.cpp``)에도 본 클라이언트를 그대로 사용한다. ``base_url``과 비어 있지 않은 ``api_key``만 설정하면 동일한 인터페이스로 호출된다.
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

def _parse_streaming_body(body_text: str) -> tuple:
    """OpenAI Server-Sent Events 스트림 본문을 ``(content, usage)``로 합산한다.

    스트리밍 응답은 ``data: {...}`` 라인 시퀀스이며 ``data: [DONE]``으로 끝난다.
    각 chunk의 ``choices[0].delta.content``를 이어 붙이고 마지막 chunk의 ``usage`` 블록(요청 시 ``stream_options.include_usage``를 켠 경우)을 응답 usage로 사용한다.
    JSON 파싱이 실패하거나 기대 필드가 없는 라인은 건너뛴다. 부분 chunk가 합산 결과를 오염시키지 못하게 한 안전 장치다.
    """

    import json as _json

    content_parts: list = []
    usage_dict: dict = {}
    for raw_line in body_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = _json.loads(payload)
        except ValueError:
            continue
        if not isinstance(chunk, dict):
            continue
        choices = chunk.get("choices")
        if isinstance(choices, list) and choices:
            delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
            if isinstance(delta, dict):
                segment = delta.get("content")
                if isinstance(segment, str):
                    content_parts.append(segment)
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            usage_dict = usage

    content = "".join(content_parts)

    def _as_int(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    prompt = _as_int(usage_dict.get("prompt_tokens"))
    completion = _as_int(usage_dict.get("completion_tokens"))
    total = _as_int(usage_dict.get("total_tokens"))
    details = usage_dict.get("prompt_tokens_details")
    cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    return content, TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_tokens=cached,
    )


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


class LLMClient:
    """OpenAI Chat Completions API와 호환 서버용 async 클라이언트.

    클래스 이름은 provider-agnostic하다(레거시 alias ``MlxLLMClient``는 v1.1.0에서 제거됐다). 신규 코드는 본 구체 클래스가 아니라 ``llm_backend``의 ``LLMBackend`` 프로토콜에 의존해, 하위 transport(OpenAI / Anthropic / MCP sampling)를 교체 가능하게 유지하는 것이 권장된다.

    사용 예시는 아래와 같다.

    ::

        async with LLMClient(cfg.llm) as client:
            models = await client.healthcheck()
            response = await client.chat(messages, max_tokens=500)

    retry 정책은 다음과 같다. HTTP 5xx, 429, timeout, connect 실패는 ``retry_max_attempts``까지 exponential backoff로 재시도한다. 401과 그 외 4xx 응답은 fast-fail이다. retry가 모두 소진되면 ``RetryExhaustedError``를 raise한다.

    API 키가 없으면 HTTP 호출 전에 ``ConfigError``로 차단한다.
    """

    def __init__(self, config: LlmConfig) -> None:
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "LLMClient":
        self._client = httpx.AsyncClient(timeout=self._config.timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def healthcheck(self) -> list:
        """모델 목록 조회로 연결성을 검증한다.

        Returns:
            ``data[*].id``에서 추출한 모델 ID 리스트.

        Raises:
            ServerNotReachableError: 네트워크 실패, 5xx, 또는 빈 응답.
            ConfigError: 401 등 4xx 응답이거나 API 키 누락.
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
                f"LLM 엔드포인트가 4xx 응답을 보냈습니다: HTTP {response.status_code}. "
                f"base_url과 모델 ID를 확인해 주세요. 응답 본문: {response.text[:200]}"
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
        """chat completion 요청을 보내고 응답을 반환한다.

        Args:
            messages: OpenAI 형식 messages 배열.
            max_tokens: 호출 단위 ``LlmConfig.max_tokens`` override.
            temperature: 호출 단위 ``LlmConfig.temperature`` override.

        Raises:
            ConfigError: API 키가 없거나 유효하지 않거나, 4xx 응답.
            ServerNotReachableError: 단발 네트워크 실패.
            RetryExhaustedError: 5xx/429/timeout/빈 응답이 설정된 retry 한도를 넘은 경우.
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
        if self._config.streaming:
            body["stream"] = True
            # OpenAI에 마지막 stream chunk에서 합산 usage 블록을 함께 보내달라고 요청한다.
            # streaming과 non-streaming 응답이 같은 토큰 카운트를 노출하게 한다.
            # ``stream_options.include_usage`` 옵션 게이트 뒤에 있다.
            body["stream_options"] = {"include_usage": True}
        # ``extra_chat_kwargs``는 OpenAI Chat Completions 스펙 밖에 있는 backend 고유 요청 필드를 그대로 forward한다.
        # 표준 use case는 mlx_lm.server / vLLM의 Qwen3 모델 thinking 토글용 ``chat_template_kwargs``다.
        # 예약 키는 의도적으로 skip해, 사용자가 표준 body 모양을 실수로 override하지 못하게 한다.
        extras = self._config.extra_chat_kwargs_dict()
        for key, value in extras.items():
            if key in body:
                continue
            body[key] = value

        # 보안 정책상 메시지 본문은 구조화 로그에 남기지 않는다. count와 글자 합계만 기록한다.
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
                    if self._config.streaming:
                        content, usage = _parse_streaming_body(response.text)
                    else:
                        content = self._extract_message_content(response)
                        usage = self._extract_usage(response)
                    if not content:
                        last_exc = EmptyResponseError(
                            "응답 message.content가 비어 있다"
                        )
                    else:
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
                                "streaming": self._config.streaming,
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
                "LLMClient는 async with 블록 안에서만 사용할 수 있다"
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
        """OpenAI ``usage`` 블록에서 ``TokenUsage``를 추출한다.

        모킹 응답이나 본 필드를 생략한 호환 서버 응답에는 0으로 채워 반환한다.
        ``cached_tokens``는 OpenAI 스키마에 따라 ``usage.prompt_tokens_details.cached_tokens`` 위치에 있다.
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
