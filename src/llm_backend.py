"""LLM 백엔드 추상화와 구현 두 종.

본 도구의 인터뷰 호출 경로는 두 가지 백엔드를 선택적으로 사용한다.

- ``OpenAIBackend``: 기존 ``MlxLLMClient``를 그대로 감싼다. CLI/MCP 어느 진입점에서
  쓰든 자체 OpenAI Chat Completions 호출을 수행한다. 비용은 사용자 OpenAI 키에서 빠진다
- ``McpSamplingBackend``: MCP 표준의 ``sampling/createMessage`` request로 클라이언트
  (Claude Code, Cursor 등)에 추론을 위임한다. OpenAI 키 없이도 동작하며 비용은
  클라이언트 측 LLM 사용량으로 청구된다

두 구현은 ``LLMBackend`` 프로토콜을 만족한다. ``run_batch``/``run_interview``/
``generate_report``는 본 프로토콜에 의존해 백엔드를 그대로 swap할 수 있다
(architecture.md §3 의존성 주입).

본 모듈은 ``mcp`` SDK 부재 환경에서도 import 자체가 깨지지 않도록 ``mcp`` import는
``McpSamplingBackend`` 내부에서 lazy하게 수행한다.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, runtime_checkable

from .config import LlmConfig
from .llm_client import MlxLLMClient
from .models import (
    ChatResponse,
    ConfigError,
    EmptyResponseError,
    RetryExhaustedError,
    ServerNotReachableError,
    TokenUsage,
)


logger = logging.getLogger(__name__)


@runtime_checkable
class LLMBackend(Protocol):
    """인터뷰 호출 경로가 의존하는 최소 인터페이스.

    ``MlxLLMClient``의 공개 메서드(``healthcheck``/``chat``)와 호환된다. 본 프로토콜을
    만족하는 객체라면 ``run_batch``/``run_interview``/``generate_report`` 어느 곳에서도
    그대로 swap할 수 있다.

    구현체는 ``async with`` 컨텍스트 매니저 프로토콜도 함께 지원한다(리소스 정리 일관성).
    """

    async def healthcheck(self) -> list:  # pragma: no cover - protocol stub
        ...

    async def chat(
        self,
        messages: list,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> ChatResponse:  # pragma: no cover - protocol stub
        ...

    async def __aenter__(self) -> "LLMBackend":  # pragma: no cover - protocol stub
        ...

    async def __aexit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - protocol stub
        ...


class OpenAIBackend:
    """``MlxLLMClient``를 위임받아 본 프로토콜로 노출한다.

    구현은 단순 위임이라 기존 인터뷰/리포트/배치 회귀 테스트가 그대로 통과한다.
    명시적인 별칭 클래스를 둔 이유는 호출 지점에서 백엔드 종류가 명확히 드러나도록
    하기 위함이다(``isinstance(backend, OpenAIBackend)`` 분기 가능).
    """

    def __init__(self, config: LlmConfig) -> None:
        self._client = MlxLLMClient(config)
        self._config = config

    async def __aenter__(self) -> "OpenAIBackend":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    async def healthcheck(self) -> list:
        return await self._client.healthcheck()

    async def chat(
        self,
        messages: list,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> ChatResponse:
        return await self._client.chat(
            messages, max_tokens=max_tokens, temperature=temperature
        )


# 사용자가 클라이언트가 sampling을 지원한다고 명시했지만 실제 호출에서 거부하는 경우의
# 메시지. ``ServerNotReachableError``로 변환해 호출자가 OpenAI fallback을 시도하거나
# 사용자에게 알릴 수 있게 한다.
_SAMPLING_REJECTED_MESSAGE = (
    "MCP 클라이언트가 sampling 요청을 거부했습니다. "
    "클라이언트의 sampling 권한 설정을 확인하거나 OpenAI 백엔드로 전환해 주세요"
)


class McpSamplingBackend:
    """MCP ``sampling/createMessage`` request로 추론을 클라이언트에 위임하는 백엔드.

    호출 흐름은 아래와 같다.

    - ``chat(messages, ...)`` 호출 시 OpenAI 형식 messages를 MCP ``SamplingMessage``
      리스트로 변환하고, system role은 ``system_prompt`` 인자로 분리해 전달
    - 클라이언트(Claude Code 등)가 자기 LLM(Anthropic Claude, 사용자 설정 모델 등)으로
      응답을 생성해 ``CreateMessageResult``로 반환
    - 응답의 ``content.text``를 ``ChatResponse.content``로 매핑

    제약 사항은 아래와 같다.

    - ``healthcheck``는 클라이언트 sampling capability 존재 확인만 수행한다. 실제
      LLM 가용성 검증은 클라이언트에 맡긴다(서버는 클라이언트 LLM에 직접 접근 불가)
    - ``usage`` 정보는 표준 sampling 응답에 포함되지 않으므로 0으로 채운 ``TokenUsage``
      를 반환한다. 비용 추정은 클라이언트 측에서 수행한다(``estimated_cost_usd``는 본
      백엔드 사용 시 항상 0)
    - retry/timeout 정책은 클라이언트가 결정한다. 서버 측 재시도는 적용하지 않는다
    """

    def __init__(
        self,
        session: Any,
        *,
        max_tokens_default: int = 500,
        temperature_default: float = 0.8,
    ) -> None:
        """MCP ``ServerSession``을 주입받는다.

        Args:
            session: MCP ``ServerSession`` 인스턴스. ``create_message`` 메서드를 가져야 한다
            max_tokens_default: ``chat``에 ``max_tokens`` 인자가 없을 때 사용할 기본값
            temperature_default: ``chat``에 ``temperature`` 인자가 없을 때 사용할 기본값
        """

        self._session = session
        self._max_tokens_default = int(max_tokens_default)
        self._temperature_default = float(temperature_default)

    async def __aenter__(self) -> "McpSamplingBackend":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def healthcheck(self) -> list:
        """클라이언트 sampling capability 존재 확인.

        실제 LLM 호출 없이 capability만 검증한다. 모델 ID 리스트는 sampling 표준에
        존재하지 않으므로 빈 리스트를 반환한다(호출자는 ``len(models)``로 가용성을 판단하지
        않도록 주의).

        Raises:
            ServerNotReachableError: 클라이언트가 sampling을 지원하지 않거나 capability
                확인 자체가 실패한 경우
        """

        try:
            from mcp import types
        except ImportError as exc:
            raise ServerNotReachableError(
                f"mcp Python SDK를 import할 수 없어 sampling capability를 확인할 수 없다: {exc}"
            ) from exc

        try:
            supports = self._session.check_client_capability(
                types.ClientCapabilities(sampling=types.SamplingCapability())
            )
        except Exception as exc:  # noqa: BLE001 - capability 검증 안전망
            raise ServerNotReachableError(
                f"MCP 클라이언트 capability 확인 실패: {exc}"
            ) from exc

        if not supports:
            raise ServerNotReachableError(
                "MCP 클라이언트가 sampling capability를 노출하지 않습니다. "
                "클라이언트 설정을 확인하거나 OpenAI 백엔드를 사용해 주세요"
            )

        logger.info(
            "MCP sampling capability 확인",
            extra={"backend": "mcp_sampling"},
        )
        # sampling 표준은 모델 가용성 조회 API가 없다. 호환 위해 빈 리스트 반환
        return []

    async def chat(
        self,
        messages: list,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> ChatResponse:
        """MCP sampling/createMessage로 클라이언트 LLM에 추론을 위임한다.

        Args:
            messages: OpenAI Chat Completions 형식 messages 배열. ``role`` 키로 system/
                user/assistant를 구분한다
            max_tokens: 명시 시 본 호출 한정 max_tokens. 없으면 default 사용
            temperature: 명시 시 본 호출 한정 temperature. 없으면 default 사용

        Returns:
            ``ChatResponse(content, latency_ms=0, retry_count=0, usage=TokenUsage())``

        Raises:
            ConfigError: messages 형식이 sampling 호환 변환 불가
            ServerNotReachableError: 클라이언트가 sampling을 거부 또는 SDK 미설치
            RetryExhaustedError: 클라이언트 응답 본문이 비어 있는 경우(retry 정책 없음)
        """

        try:
            from mcp import types
        except ImportError as exc:
            raise ServerNotReachableError(
                f"mcp Python SDK를 import할 수 없어 sampling 호출을 수행할 수 없다: {exc}"
            ) from exc

        sampling_messages, system_prompt = _convert_to_sampling_messages(messages, types)

        if not sampling_messages:
            raise ConfigError(
                "MCP sampling 호출에 보낼 user/assistant 메시지가 없다. "
                "messages 배열에 system 외 1개 이상 포함되어야 합니다"
            )

        try:
            result = await self._session.create_message(
                messages=sampling_messages,
                max_tokens=int(max_tokens or self._max_tokens_default),
                system_prompt=system_prompt,
                temperature=(
                    float(temperature)
                    if temperature is not None
                    else self._temperature_default
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 클라이언트 응답 안전망
            # mcp SDK의 ``McpError``는 본 모듈에서 직접 import하지 않는다(SDK 부재
            # 환경 호환). 모든 예외를 ``ServerNotReachableError``로 변환한다
            raise ServerNotReachableError(
                f"{_SAMPLING_REJECTED_MESSAGE} (원인: {exc})"
            ) from exc

        content_text = _extract_sampling_text(result)
        if not content_text:
            raise RetryExhaustedError(
                "MCP sampling 응답이 비어 있습니다. 클라이언트 LLM 동작을 확인해 주세요"
            )

        logger.info(
            "MCP sampling 응답 수신",
            extra={
                "backend": "mcp_sampling",
                "response_chars": len(content_text),
                "model": getattr(result, "model", "unknown"),
            },
        )

        return ChatResponse(
            content=content_text,
            latency_ms=0,
            retry_count=0,
            reasoning_trace=None,
            usage=TokenUsage(),
        )


# ---------------------------------------------------------------------------
# 변환 헬퍼
# ---------------------------------------------------------------------------


def _convert_to_sampling_messages(messages: list, types_mod: Any) -> tuple:
    """OpenAI 형식 messages를 MCP ``SamplingMessage`` 리스트로 변환한다.

    sampling 표준은 ``role``로 ``user``/``assistant``만 허용하고 ``system``은 별도
    ``system_prompt`` 인자로 분리해 전달한다. 본 함수는 system role 메시지를 모두 추출해
    하나의 system_prompt로 결합하고, 나머지를 ``SamplingMessage`` 리스트로 만든다.

    Args:
        messages: OpenAI 형식 messages 배열. dict 또는 ``MessageEntry`` 모두 받는다
        types_mod: ``mcp.types`` 모듈 참조. 호출자에서 lazy import한 결과를 넘긴다

    Returns:
        ``(sampling_messages, system_prompt)``. system_prompt는 추출된 system 메시지가
        없으면 ``None``
    """

    system_parts: list = []
    sampling_messages: list = []

    for m in messages:
        if hasattr(m, "role") and hasattr(m, "content"):
            role = m.role
            content = m.content
        elif isinstance(m, dict):
            role = m.get("role", "")
            content = m.get("content", "")
        else:
            continue

        text = str(content) if content is not None else ""

        if role == "system":
            if text:
                system_parts.append(text)
            continue
        if role not in ("user", "assistant"):
            # tool/function 등 sampling이 모르는 role은 user로 강제 변환
            role = "user"

        sampling_messages.append(
            types_mod.SamplingMessage(
                role=role,
                content=types_mod.TextContent(type="text", text=text),
            )
        )

    system_prompt = "\n\n".join(system_parts) if system_parts else None
    return sampling_messages, system_prompt


def _extract_sampling_text(result: Any) -> str:
    """``CreateMessageResult``에서 텍스트 본문을 안전하게 꺼낸다.

    표준 응답은 ``result.content``가 ``TextContent``(``type="text"``, ``text=...``)다.
    이미지/오디오 등 비-텍스트 응답은 본 도구가 다루지 않으므로 빈 문자열을 반환한다.
    """

    content = getattr(result, "content", None)
    if content is None:
        return ""
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    return ""


# ---------------------------------------------------------------------------
# 백엔드 선택 정책
# ---------------------------------------------------------------------------


_VALID_BACKEND_VALUES = frozenset({"openai", "mcp_sampling", "auto"})


def normalize_backend_choice(value: Optional[str]) -> str:
    """config.yaml의 ``llm.backend`` 값을 정규화하고 검증한다.

    허용 값은 ``openai``/``mcp_sampling``/``auto``다. 미지정(``None`` 또는 빈 문자열)은
    ``auto``로 본다. 그 외 값은 ``ConfigError``로 차단한다(error-handling.md §1).
    """

    if value is None:
        return "auto"
    raw = str(value).strip().lower()
    if not raw:
        return "auto"
    if raw not in _VALID_BACKEND_VALUES:
        raise ConfigError(
            f"llm.backend는 {sorted(_VALID_BACKEND_VALUES)} 중 하나여야 합니다. "
            f"입력값: {value!r}"
        )
    return raw


def select_backend(
    *,
    config: LlmConfig,
    backend_choice: str,
    sampling_session: Optional[Any] = None,
) -> LLMBackend:
    """선택 정책에 따라 백엔드 인스턴스를 만든다.

    정책은 아래와 같다.

    - ``openai``: 항상 ``OpenAIBackend``를 만든다(API 키 누락 시 chat/healthcheck
      호출 시점에 ConfigError로 차단)
    - ``mcp_sampling``: ``sampling_session``이 있어야 한다. 없으면 ``ConfigError``
    - ``auto``: ``sampling_session``이 있으면 ``McpSamplingBackend``, 없으면 ``OpenAIBackend``

    Args:
        config: ``LlmConfig``. OpenAI 백엔드 사용 시 base_url/model/key를 사용한다
        backend_choice: ``normalize_backend_choice``로 정규화된 값
        sampling_session: MCP 서버 진입점에서 ``request_context.session``으로 가져온
            ``ServerSession``. CLI 진입점에서는 ``None``

    Returns:
        ``LLMBackend`` 프로토콜을 만족하는 인스턴스
    """

    choice = normalize_backend_choice(backend_choice)

    if choice == "openai":
        return OpenAIBackend(config)

    if choice == "mcp_sampling":
        if sampling_session is None:
            raise ConfigError(
                "llm.backend=mcp_sampling은 MCP 서버 진입점에서만 사용할 수 있습니다. "
                "CLI 진입점에서는 llm.backend=openai 또는 auto로 두어 주세요"
            )
        return McpSamplingBackend(sampling_session)

    # auto: sampling 세션이 있으면 우선 사용, 없으면 OpenAI fallback
    if sampling_session is not None:
        return McpSamplingBackend(sampling_session)
    return OpenAIBackend(config)


# ``EmptyResponseError``는 본 모듈에서 직접 raise하지 않지만 ``OpenAIBackend``가 위임한
# ``MlxLLMClient.chat``가 빈 content에 대해 retry를 거쳐 ``RetryExhaustedError``로
# 변환하는 흐름과의 호환성을 명시하기 위해 re-export한다(호출자가 ``except`` 블록에서
# 본 모듈에서 한 번에 import할 수 있게 한다).
__all__ = [
    "EmptyResponseError",
    "LLMBackend",
    "McpSamplingBackend",
    "OpenAIBackend",
    "normalize_backend_choice",
    "select_backend",
]
