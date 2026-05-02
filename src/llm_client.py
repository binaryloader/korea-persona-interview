"""OpenAI Chat Completions API 비동기 클라이언트.

``httpx.AsyncClient`` 위에 헬스체크, chat, 재시도, 타임아웃, 응답 후처리, JSON
Lines 로깅을 얹는다. ``openai``/``anthropic`` SDK와 ``tenacity`` 의존을 회피한다
(dependency.md §1, leftpad 회피). 백오프는 6줄 직접 구현이다.

본 모듈은 외부 HTTP를 다루는 infrastructure 계층이다(architecture.md §1).
도메인 예외와의 매핑은 본 모듈에서 일원화하며, 호출자(InterviewSession 등)는
도메인 예외만 다룬다.

v1.x부터 OpenAI Chat Completions API로 호출한다(이전 v1.0의 로컬 MLX 서버는
완전 제거). API 키는 ``LlmConfig.api_key``에서 받아 ``Authorization: Bearer``
헤더로 전송한다. 키 누락 시 ``ConfigError``로 친절한 한국어 안내가 나온다.
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
)


logger = logging.getLogger(__name__)


# 백오프 시퀀스의 jitter 폭. thundering herd 방지(TDD §3.3 비고).
_JITTER_MAX_SECONDS = 0.5

# OpenAI API 키 누락 시 사용자에게 보여줄 한국어 안내. main.py의
# MESSAGES 사전과 별개이며 서버/키 둘을 분리해 안내한다(error-handling.md §1).
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
    """OpenAI Chat Completions 호환 비동기 클라이언트.

    클래스명은 v1.0 시절(로컬 MLX 서버) 호환을 위해 보존한다. v1.x부터 OpenAI
    공식 엔드포인트로 호출한다.

    사용 예시는 아래와 같다.

    ::

        async with MlxLLMClient(cfg.llm) as client:
            models = await client.healthcheck()
            response = await client.chat(messages, max_tokens=500)

    재시도 정책은 HTTP 5xx, 429, 타임아웃, 연결 실패에 대해 지수 백오프(기본
    1s, 2s, 4s)를 최대 ``retry_max_attempts``회 적용한다. 401/4xx(429 제외)는
    즉시 실패한다. 모든 retry 소진 시 ``RetryExhaustedError``.

    API 키가 누락된 상태로 ``healthcheck``/``chat``을 호출하면 즉시
    ``ConfigError``로 차단해 외부 호출이 발생하지 않게 한다(security.md §1).
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

        OpenAI 엔드포인트는 본 호출에 인증을 요구한다. 키 누락 시
        ``ConfigError``로 친절한 한국어 안내를 띄운다.

        Returns:
            응답의 ``data`` 배열에서 추출한 모델 ID 목록.

        Raises:
            ServerNotReachableError: 네트워크 실패, 5xx, ``data`` 비어있음.
            ConfigError: 401(키 무효), 4xx(요청 거부), 키 누락.
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
        """``POST {base_url}/chat/completions``으로 응답을 받는다.

        키 누락 시 ``ConfigError``로 차단한다. 재시도/타임아웃 정책은
        ``LlmConfig``의 값을 따른다. OpenAI 응답에는 ``message.reasoning``
        필드가 없으므로 ``ChatResponse.reasoning_trace``는 항상 ``None``이다
        (도메인 모델 backward compat 유지).

        Args:
            messages: OpenAI Chat Completions 형식의 messages 배열.
            max_tokens: 명시 시 config 기본값을 덮는다.
            temperature: 명시 시 config 기본값을 덮는다.

        Returns:
            ``ChatResponse(content, latency_ms, retry_count, ...)``.

        Raises:
            ConfigError: API 키 누락/무효, 4xx 응답.
            ServerNotReachableError: 단일 호출 단계 네트워크 실패.
            RetryExhaustedError: 5xx/429/타임아웃/빈 content가 retry 한도를
                넘어선 경우.
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
                # 401은 키 무효라 즉시 실패. retry해도 같은 결과.
                if response.status_code == 401:
                    raise ConfigError(_INVALID_API_KEY_MESSAGE)
                # 429는 OpenAI rate limit. 재시도 대상으로 본다.
                if response.status_code == 429:
                    last_exc = ServerNotReachableError(
                        f"429 rate limit: {response.text[:200]}"
                    )
                # 그 외 4xx는 즉시 실패.
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
                        # OpenAI에선 거의 발생하지 않지만 안전망으로 retry 대상.
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
                            },
                        )
                        return ChatResponse(
                            content=content,
                            latency_ms=latency_ms,
                            retry_count=attempt,
                            reasoning_trace=None,
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

    def _require_api_key(self) -> None:
        """API 키 누락 시 즉시 ConfigError로 차단한다.

        외부 호출이 발생하기 전 단계에서 차단해 시크릿 누락 상태로 네트워크
        호출이 일어나는 것을 막는다(security.md §1).
        """

        if not self._config.api_key:
            raise ConfigError(_MISSING_API_KEY_MESSAGE)

    def _auth_headers(self) -> dict:
        """``Authorization: Bearer ${api_key}`` 헤더를 만든다.

        키 존재 여부는 ``_require_api_key``에서 사전 검증되므로 본 함수는
        헤더 dict 조립만 담당한다.
        """

        return {"Authorization": f"Bearer {self._config.api_key}"}

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
    def _extract_message_content(response: httpx.Response) -> str:
        """첫 choice의 ``content``를 안전하게 꺼낸다.

        OpenAI 표준 스키마에서는 ``choices[0].message.content``만 사용한다.
        v1.0 시절 Qwen3 ``message.reasoning`` 확장 필드는 OpenAI 응답에
        존재하지 않는다.

        Returns:
            content 문자열. 누락/비정상 응답은 빈 문자열을 반환한다.
        """

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
