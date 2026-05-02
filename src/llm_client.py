"""MLX 서버용 OpenAI 호환 비동기 클라이언트.

``httpx.AsyncClient`` 위에 헬스체크, chat, 재시도, 타임아웃, 로컬 가드, JSON
Lines 로깅을 얹는다. ``openai``/``anthropic`` SDK와 ``tenacity`` 의존을 회피한다
(dependency.md §1, leftpad 회피). 백오프는 6줄 직접 구현이다.

본 모듈은 외부 HTTP를 다루는 infrastructure 계층이다(architecture.md §1).
도메인 예외와의 매핑은 본 모듈에서 일원화하며, 호출자(InterviewSession 등)는
도메인 예외만 다룬다.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional, Tuple

import httpx

from .config import LlmConfig, is_local_base_url
from .models import (
    ChatResponse,
    ConfigError,
    EmptyResponseError,
    RetryExhaustedError,
    ServerNotReachableError,
)


logger = logging.getLogger(__name__)


# 백오프 시퀀스의 jitter 폭. thundering herd 방지(TDD §3.3 비고).
_JITTER_MAX_SECONDS = 0.5


class MlxLLMClient:
    """OpenAI Chat Completions 호환 MLX 서버 비동기 클라이언트.

    사용 예시는 아래와 같다.

    ::

        async with MlxLLMClient(cfg.llm) as client:
            models = await client.healthcheck()
            response, latency_ms = await client.chat(messages, max_tokens=500)

    재시도 정책은 HTTP 5xx, 타임아웃, 연결 실패에 대해 지수 백오프(기본 1s,
    2s, 4s)를 최대 ``retry_max_attempts``회 적용한다. 4xx는 즉시 실패한다
    (``ConfigError`` 또는 그대로 raise). 모든 retry 소진 시 ``RetryExhaustedError``.

    base_url이 localhost가 아니면 ``chat()`` 호출을 차단한다(security.md §1).
    ``healthcheck()``는 외부 URL이어도 경고 로그만 남기고 진행한다.
    """

    def __init__(self, config: LlmConfig) -> None:
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "MlxLLMClient":
        # 단일 AsyncClient를 생성해 keep-alive를 활용한다. base_url은
        # __init__에서 박지 않고 매 호출 절대 경로로 다룬다(테스트 용이성).
        self._client = httpx.AsyncClient(timeout=self._config.timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    async def healthcheck(self) -> list:
        """``GET {base_url}/models``로 모델 가용성을 검증한다.

        Returns:
            응답의 ``data`` 배열에서 추출한 모델 ID 목록.

        Raises:
            ServerNotReachableError: 네트워크 실패, 5xx, ``data`` 비어있음.
            ConfigError: 4xx 응답.
        """

        url = f"{self._config.base_url.rstrip('/')}/models"
        if not is_local_base_url(self._config.base_url):
            # 외부 URL이어도 healthcheck는 진행하되 경고를 남긴다.
            logger.warning(
                "base_url이 localhost가 아니다. chat 호출은 차단된다",
                extra={"base_url": self._config.base_url},
            )
        client = self._require_client()

        try:
            response = await client.get(url)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ServerNotReachableError(
                f"MLX 서버 연결 실패: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ServerNotReachableError(
                f"MLX 서버 응답 타임아웃: {exc}"
            ) from exc

        if 500 <= response.status_code < 600:
            raise ServerNotReachableError(
                f"MLX 서버 5xx 응답: {response.status_code}"
            )
        if 400 <= response.status_code < 500:
            raise ConfigError(
                f"MLX 서버 4xx 응답: {response.status_code} {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ServerNotReachableError(
                f"MLX 서버 응답 JSON 파싱 실패: {exc}"
            ) from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            raise ServerNotReachableError(
                "MLX 서버 /models 응답의 data가 비어 있다"
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
        """``POST {base_url}/chat/completions``으로 응답을 받는다.

        외부 URL일 때 호출을 차단한다(security.md §1). 재시도/타임아웃 정책은
        ``LlmConfig``의 값을 따른다.

        Qwen3 계열 호환을 위해 요청 body에 ``chat_template_kwargs``로
        ``enable_thinking`` 값을 항상 명시한다. ``enable_thinking=true``면
        ``message.reasoning``을 ``ChatResponse.reasoning_trace``로 보존하고,
        False면 reasoning 필드는 무시한다(GATE-1 검증).

        Args:
            messages: OpenAI Chat Completions 형식의 messages 배열.
            max_tokens: 명시 시 config 기본값을 덮는다.
            temperature: 명시 시 config 기본값을 덮는다.

        Returns:
            ``ChatResponse(content, latency_ms, retry_count, reasoning_trace)``.

        Raises:
            ConfigError: 외부 URL 차단, 4xx 응답.
            ServerNotReachableError: 단일 호출 단계 네트워크 실패.
            RetryExhaustedError: 5xx/타임아웃/빈 content가 retry 한도를 넘어선 경우.
        """

        if not is_local_base_url(self._config.base_url):
            raise ConfigError(
                "base_url이 localhost가 아니라 chat 호출을 차단한다. "
                "사업 아이템 본문이 외부로 송신되는 것을 방지한다"
            )

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
            # Qwen3 계열 thinking 토글. GATE-1 검증 결과 default(true)로 두면
            # reasoning이 토큰 예산을 소진해 content가 비어 온다. 본 도구는
            # default가 False라 빈 content 사례를 회피한다.
            "chat_template_kwargs": {
                "enable_thinking": self._config.enable_thinking,
            },
        }

        # 디버그 레벨에서도 messages 본문은 출력하지 않는다(security.md §1, PRD §6.6).
        # 길이 메타만 남긴다.
        logger.debug(
            "chat 요청 시작",
            extra={
                "messages_count": len(messages),
                "messages_total_chars": sum(
                    len(m.get("content", "")) if isinstance(m, dict) else 0
                    for m in messages
                ),
                "model": self._config.model,
                "enable_thinking": self._config.enable_thinking,
            },
        )

        last_exc: Optional[Exception] = None
        for attempt in range(self._config.retry_max_attempts):
            start = asyncio.get_event_loop().time()
            try:
                response = await client.post(url, json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_exc = ServerNotReachableError(f"연결 실패: {exc}")
            except httpx.TimeoutException as exc:
                last_exc = ServerNotReachableError(f"타임아웃: {exc}")
            else:
                latency_ms = int((asyncio.get_event_loop().time() - start) * 1000)
                # 4xx는 즉시 실패. retry하지 않는다.
                if 400 <= response.status_code < 500:
                    raise ConfigError(
                        f"chat 4xx 응답: {response.status_code} "
                        f"{response.text[:200]}"
                    )
                if 500 <= response.status_code < 600:
                    last_exc = ServerNotReachableError(
                        f"5xx 응답: {response.status_code}"
                    )
                else:
                    content, reasoning = self._extract_message_parts(response)
                    if not content:
                        # choices/content 비었음. Qwen3 thinking on에서 흔한
                        # 패턴이라 retry 대상으로 본다(EmptyResponseError로 마킹).
                        last_exc = EmptyResponseError(
                            "응답 message.content가 비어 있다"
                            + (
                                " (thinking on, reasoning 토큰 폭증 가능성)"
                                if self._config.enable_thinking
                                else ""
                            )
                        )
                    else:
                        logger.info(
                            "chat 응답 정상",
                            extra={
                                "model": self._config.model,
                                "response_chars": len(content),
                                "reasoning_chars": (
                                    len(reasoning) if reasoning else 0
                                ),
                                "latency_ms": latency_ms,
                                "retry_count": attempt,
                            },
                        )
                        # enable_thinking=False면 reasoning 필드를 보존하지 않는다.
                        kept_reasoning = (
                            reasoning if self._config.enable_thinking else None
                        )
                        return ChatResponse(
                            content=content,
                            latency_ms=latency_ms,
                            retry_count=attempt,
                            reasoning_trace=kept_reasoning,
                        )

            # retry 가능한 실패. 마지막 시도면 백오프를 건너뛴다.
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

        # 모든 retry 소진. 도메인 예외로 변환한다.
        raise RetryExhaustedError(
            f"chat 재시도 {self._config.retry_max_attempts}회 모두 실패: {last_exc}"
        )

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _require_client(self) -> httpx.AsyncClient:
        """``async with`` 컨텍스트 안에서만 호출 가능하도록 강제한다."""

        if self._client is None:
            raise RuntimeError(
                "MlxLLMClient는 async with 블록 안에서만 사용할 수 있다"
            )
        return self._client

    def _compute_backoff(self, attempt: int) -> float:
        """attempt 인덱스를 백오프 시퀀스에 매핑하고 jitter를 더한다.

        ``retry_backoff_seconds``의 마지막 값을 넘는 attempt는 마지막 값을 사용한다.
        """

        seq = self._config.retry_backoff_seconds
        if not seq:
            base = 1.0
        else:
            base = float(seq[min(attempt, len(seq) - 1)])
        return base + random.uniform(0, _JITTER_MAX_SECONDS)

    @staticmethod
    def _extract_message_parts(
        response: httpx.Response,
    ) -> Tuple[str, Optional[str]]:
        """첫 choice의 ``content``와 ``reasoning``을 안전하게 꺼낸다.

        Qwen3 계열은 ``message.reasoning``에 영문 추론 트레이스를 동봉한다.
        OpenAI 표준 스키마에 없는 확장 필드라 ``None``일 수도 있다.

        Returns:
            ``(content, reasoning_trace)``. content는 항상 str(빈 문자열 가능),
            reasoning_trace는 존재할 때만 str, 그 외는 None.
        """

        try:
            payload = response.json()
        except ValueError:
            return "", None
        if not isinstance(payload, dict):
            return "", None
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return "", None
        first = choices[0]
        if not isinstance(first, dict):
            return "", None
        message = first.get("message")
        if not isinstance(message, dict):
            return "", None
        content = message.get("content")
        content_text = content if isinstance(content, str) else ""
        reasoning = message.get("reasoning")
        reasoning_text = (
            reasoning if isinstance(reasoning, str) and reasoning else None
        )
        return content_text, reasoning_text
