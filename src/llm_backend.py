"""LLM backend abstractions for the interview pipeline.

Three backends are exposed:

- ``OpenAIBackend``: OpenAI Chat Completions API and any OpenAI-compatible
  endpoint (mlx_lm.server, vLLM, llama.cpp). Used by the CLI.
- ``AnthropicBackend``: Anthropic Messages API. Used by the CLI when
  ``provider=anthropic``.
- ``McpSamplingBackend``: MCP ``sampling/createMessage`` request that delegates
  inference to the host agent (Claude Code, Cursor, ...). Used exclusively by
  the MCP server entry point.

All three implementations satisfy the ``LLMBackend`` protocol so the
application layer (``run_batch``, ``run_interview``, ``generate_report``) can
swap them via dependency injection.

The ``mcp`` SDK is imported lazily inside ``McpSamplingBackend`` so this module
imports cleanly when the SDK is absent.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Optional, Protocol, runtime_checkable

import httpx

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


_JITTER_MAX_SECONDS = 0.5

_MISSING_ANTHROPIC_KEY_MESSAGE = (
    "Anthropic API 키가 설정되지 않았습니다. https://console.anthropic.com/ "
    "에서 발급 후 환경변수 ANTHROPIC_API_KEY로 셸에 적용하거나 프로젝트 루트의 "
    ".env 파일에 `ANTHROPIC_API_KEY=...` 형식으로 저장해 주세요"
)

_INVALID_ANTHROPIC_KEY_MESSAGE = (
    "Anthropic API 키가 유효하지 않거나 권한이 없습니다. "
    "환경변수 ANTHROPIC_API_KEY를 다시 확인해 주세요"
)

_MCP_SAMPLING_UNSUPPORTED_MESSAGE = (
    "호스트 에이전트가 MCP sampling을 지원하지 않습니다. "
    "Claude Code 최신 버전으로 업데이트하거나 CLI"
    "(`python main.py interview ...` 또는 `kpi interview ...`)로 호출해 주세요"
)


@runtime_checkable
class LLMBackend(Protocol):
    """Minimal interface used by the interview pipeline.

    Compatible with ``MlxLLMClient`` so any object conforming to this protocol
    is interchangeable in ``run_batch``/``run_interview``/``generate_report``.
    Implementations must support the async context manager protocol.
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
    """Adapter for OpenAI Chat Completions and OpenAI-compatible servers.

    Wraps ``MlxLLMClient`` so the interview pipeline can also target
    self-hosted OpenAI-compatible endpoints (mlx_lm.server, vLLM, llama.cpp)
    by configuring ``base_url`` and ``api_key`` (the local servers usually
    accept any string).
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


class AnthropicBackend:
    """Adapter for the Anthropic Messages API.

    Implements ``POST /v1/messages`` directly over httpx. The official
    ``anthropic`` SDK is intentionally not used to avoid the dependency. Caller
    is responsible for providing ``api_key`` via the ``ANTHROPIC_API_KEY``
    environment variable.

    Notes on the request shape that differ from OpenAI:

    - ``system`` is a top-level field, not a message with ``role=system``.
    - ``max_tokens`` is required.
    - Authentication uses the ``x-api-key`` header plus ``anthropic-version``.

    Token usage maps as follows for parity with OpenAI:

    - ``usage.input_tokens`` -> ``TokenUsage.prompt_tokens``
    - ``usage.output_tokens`` -> ``TokenUsage.completion_tokens``
    - ``usage.cache_read_input_tokens`` -> ``TokenUsage.cached_tokens``

    Anthropic prompt caching ``cache_control`` markers are not emitted by this
    backend. They are tracked in the v1.1 backlog.
    """

    _ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, config: LlmConfig) -> None:
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "AnthropicBackend":
        self._client = httpx.AsyncClient(timeout=self._config.timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def healthcheck(self) -> list:
        """Verify connectivity by issuing a 1-token ping request.

        The Messages API does not expose a list-models endpoint, so the
        healthcheck sends a minimal request and treats any 2xx response as
        success. Returns the configured model id wrapped in a list to match
        the OpenAI backend's contract.
        """

        self._require_api_key()
        client = self._require_client()
        url = f"{self._config.base_url.rstrip('/')}/messages"
        body = {
            "model": self._config.model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }

        try:
            response = await client.post(url, json=body, headers=self._auth_headers())
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ServerNotReachableError(f"Anthropic 서버 연결 실패: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise ServerNotReachableError(f"Anthropic 서버 응답 타임아웃: {exc}") from exc

        if 500 <= response.status_code < 600:
            raise ServerNotReachableError(
                f"Anthropic 서버 5xx 응답: {response.status_code}"
            )
        if response.status_code == 401:
            raise ConfigError(_INVALID_ANTHROPIC_KEY_MESSAGE)
        if 400 <= response.status_code < 500:
            raise ConfigError(
                f"Anthropic 서버 4xx 응답: {response.status_code} {response.text[:200]}"
            )

        logger.info(
            "헬스체크 응답 정상",
            extra={
                "base_url": self._config.base_url,
                "provider": "anthropic",
                "model": self._config.model,
            },
        )
        return [self._config.model]

    async def chat(
        self,
        messages: list,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> ChatResponse:
        """Call ``POST /v1/messages`` with retry/backoff parity with OpenAI."""

        self._require_api_key()
        client = self._require_client()
        url = f"{self._config.base_url.rstrip('/')}/messages"

        anthropic_messages, system_prompt = _split_system_prompt(messages)
        if not anthropic_messages:
            raise ConfigError(
                "Anthropic 호출에 보낼 user/assistant 메시지가 없습니다. "
                "messages 배열에 system 외 1개 이상 포함되어야 합니다"
            )

        body: dict = {
            "model": self._config.model,
            "max_tokens": int(max_tokens or self._config.max_tokens),
            "messages": anthropic_messages,
            "temperature": (
                float(temperature)
                if temperature is not None
                else float(self._config.temperature)
            ),
        }
        if system_prompt:
            body["system"] = system_prompt

        logger.debug(
            "chat 요청 시작",
            extra={
                "messages_count": len(anthropic_messages),
                "model": self._config.model,
                "provider": "anthropic",
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
                    raise ConfigError(_INVALID_ANTHROPIC_KEY_MESSAGE)
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
                            "Anthropic 응답 content가 비어 있다"
                        )
                    else:
                        usage = self._extract_usage(response)
                        logger.info(
                            "chat 응답 정상",
                            extra={
                                "model": self._config.model,
                                "provider": "anthropic",
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
                        "provider": "anthropic",
                    },
                )
                await asyncio.sleep(backoff)

        raise RetryExhaustedError(
            f"chat 재시도 {self._config.retry_max_attempts}회 모두 실패: {last_exc}"
        )

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "AnthropicBackend는 async with 블록 안에서만 사용할 수 있다"
            )
        return self._client

    def _require_api_key(self) -> None:
        if not self._config.api_key:
            raise ConfigError(_MISSING_ANTHROPIC_KEY_MESSAGE)

    def _auth_headers(self) -> dict:
        return {
            "x-api-key": str(self._config.api_key),
            "anthropic-version": self._ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

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
        content = payload.get("content")
        if not isinstance(content, list) or not content:
            return ""
        first = content[0]
        if not isinstance(first, dict):
            return ""
        text = first.get("text")
        return text if isinstance(text, str) else ""

    @staticmethod
    def _extract_usage(response: httpx.Response) -> TokenUsage:
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

        input_tokens = _as_int(usage_raw.get("input_tokens"))
        output_tokens = _as_int(usage_raw.get("output_tokens"))
        cached_read = _as_int(usage_raw.get("cache_read_input_tokens"))
        return TokenUsage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cached_tokens=cached_read,
        )


class McpSamplingBackend:
    """Adapter that delegates inference to the MCP host via ``sampling/createMessage``.

    Used exclusively by the MCP server entry point. The host agent (Claude
    Code, Cursor, ...) generates the response using its own LLM, so no API
    key is required server-side.

    Constraints:

    - ``healthcheck`` only verifies the client's sampling capability. The
      sampling protocol does not expose a list-models endpoint, so it returns
      an empty list on success.
    - The standard sampling response carries no ``usage`` block, so
      ``TokenUsage()`` (all zeros) is returned.
    - Retry and timeout policies are owned by the client. Server-side retries
      are not applied.
    """

    def __init__(
        self,
        session: Any,
        *,
        max_tokens_default: int = 500,
        temperature_default: float = 0.8,
    ) -> None:
        self._session = session
        self._max_tokens_default = int(max_tokens_default)
        self._temperature_default = float(temperature_default)

    async def __aenter__(self) -> "McpSamplingBackend":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def healthcheck(self) -> list:
        """Verify the client exposes the sampling capability.

        Raises:
            ConfigError: The host agent does not advertise sampling support.
            ServerNotReachableError: Capability check itself failed.
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
        except Exception as exc:  # noqa: BLE001 - capability check safety net
            raise ServerNotReachableError(
                f"MCP 클라이언트 capability 확인 실패: {exc}"
            ) from exc

        if not supports:
            raise ConfigError(_MCP_SAMPLING_UNSUPPORTED_MESSAGE)

        logger.info(
            "MCP sampling capability 확인",
            extra={"backend": "mcp_sampling"},
        )
        return []

    async def chat(
        self,
        messages: list,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> ChatResponse:
        """Forward an OpenAI-shaped messages array to the host via sampling.

        ``system`` role messages are concatenated and passed as the
        ``system_prompt`` argument. ``user`` and ``assistant`` messages are
        forwarded as-is. Unknown roles are coerced to ``user``.

        Raises:
            ConfigError: The messages array contains no user/assistant entries.
            ServerNotReachableError: The host rejected the sampling request or
                the SDK is missing.
            RetryExhaustedError: The host returned an empty response.
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
                "MCP sampling 호출에 보낼 user/assistant 메시지가 없습니다. "
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
        except Exception as exc:  # noqa: BLE001 - host response safety net
            raise ServerNotReachableError(
                f"MCP 클라이언트가 sampling 요청을 거부했습니다 (원인: {exc})"
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


def _split_system_prompt(messages: list) -> tuple:
    """Split OpenAI-shaped messages into Anthropic ``messages`` + ``system``.

    Anthropic Messages API takes the system prompt as a top-level field rather
    than a ``role=system`` message. All ``role=system`` entries are joined
    with ``\\n\\n`` and returned as the second element. ``user``/``assistant``
    entries are returned as a list of ``{role, content}`` dicts. Unknown roles
    are coerced to ``user``.
    """

    system_parts: list = []
    out_messages: list = []
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
            role = "user"

        out_messages.append({"role": role, "content": text})

    system_prompt = "\n\n".join(system_parts) if system_parts else None
    return out_messages, system_prompt


def _convert_to_sampling_messages(messages: list, types_mod: Any) -> tuple:
    """Convert OpenAI-shaped messages into ``mcp.types.SamplingMessage`` list.

    The MCP sampling spec only allows ``user``/``assistant`` roles; ``system``
    is a separate ``system_prompt`` argument. All ``role=system`` entries are
    extracted and joined; the rest are wrapped in ``SamplingMessage``.
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
    """Extract ``content.text`` from an MCP ``CreateMessageResult``.

    Returns an empty string for non-text content (image, audio, ...).
    """

    content = getattr(result, "content", None)
    if content is None:
        return ""
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    return ""


def build_cli_backend(config: LlmConfig) -> LLMBackend:
    """Construct the CLI backend for the configured provider.

    Returns ``OpenAIBackend`` for ``provider=openai`` (also covers any
    OpenAI-compatible local server) and ``AnthropicBackend`` for
    ``provider=anthropic``.
    """

    provider = config.provider.strip().lower()
    if provider == "anthropic":
        return AnthropicBackend(config)
    return OpenAIBackend(config)


__all__ = [
    "AnthropicBackend",
    "EmptyResponseError",
    "LLMBackend",
    "McpSamplingBackend",
    "OpenAIBackend",
    "build_cli_backend",
]
