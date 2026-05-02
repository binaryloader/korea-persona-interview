"""MCP 서버 엔트리 포인트.

stdio JSON-RPC 위에 인터뷰 파이프라인 도구를 노출해서 외부 에이전트
(Claude Code, Cursor, Codex 등)가 자연어로 본 도구를 구동할 수 있게 한다.

추론 경로는 ``config.yaml`` ``mcp.mode``로 명시 선택한다(ADR-005).

- ``mode: "server"`` (기본): server-side ``OpenAIBackend``/``AnthropicBackend``
  를 사용한다. CLI와 동일한 ``LlmConfig``를 활용하므로 mcp.json ``env``에
  ``OPENAI_API_KEY``/``ANTHROPIC_API_KEY``를 박아 주어야 한다. 응답에는
  ``backend: "mcp_server"`` 라벨이 박힌다
- ``mode: "orchestrator"``: server-side에서 LLM을 호출하지 않는다. 호스트
  sub-agent가 자기 LLM으로 인터뷰를 수행하고 본 도구는 데이터/프롬프트
  helper만 노출한다. server-side 키 불필요. 응답에는 ``backend:
  "mcp_orchestrator"`` 라벨이 박힌다

자동 fallback은 하지 않는다. yaml의 ``mcp.mode`` 값으로 분기가 결정된다.

도구 핸들러는 ``src.mcp_handlers`` 패키지에 모드별로 분리되어 있다. 본 모듈은
stdio loop, list_tools 메타데이터, dispatch 라우팅만 책임진다.

``mcp`` SDK는 ``main()`` 안에서 lazy import 한다. 덕분에 SDK가 없어도 이
모듈 자체는 문제없이 import되고 사용자는 stack trace 대신 안내 메시지와
종료 코드 1을 본다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Optional

from .config import load_config
from .mcp_handlers import HANDLERS, TOOLS_BY_MODE
from .mcp_handlers._payloads import error_payload as _error_payload
from .models import ConfigError


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 도구 입력 스키마
# ---------------------------------------------------------------------------


_HEALTHCHECK_SCHEMA: dict = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


_LIST_PERSONAS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "filter": {
            "type": "string",
            "description": (
                "필터 DSL(예: 'age:25-39,region:서울특별시,gender:F'). 미지정 시 전체에서 샘플링."
            ),
        },
        "persona_ids": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": (
                "명시 페르소나 uuid 리스트. 지정 시 limit/seed는 무시되며 입력 "
                "ID 순서대로 반환한다."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 20,
            "description": "출력 행 수(1 이상).",
        },
        "seed": {
            "type": "integer",
            "default": 42,
            "description": "샘플링 시드. 같은 시드면 같은 표본을 보장한다.",
        },
    },
    "additionalProperties": False,
}


_INTERVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "product": {
            "type": "string",
            "description": "사업 아이템 한 줄 설명(필수).",
        },
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "질문 리스트(1개 이상).",
        },
        "filter": {
            "type": "string",
            "description": "필터 DSL(선택).",
        },
        "persona_ids": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": (
                "명시 페르소나 uuid 리스트. 지정 시 n/seed는 무시되며 입력 ID "
                "순서대로 인터뷰가 실행된다."
            ),
        },
        "n": {
            "type": "integer",
            "minimum": 1,
            "default": 10,
            "description": "인터뷰 인원(1 이상).",
        },
        "seed": {
            "type": "integer",
            "default": 42,
            "description": "샘플링 시드.",
        },
        "concurrency": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "default": 5,
            "description": "동시성(1-10).",
        },
        "persona_fields": {
            "type": "array",
            "items": {"type": "string"},
            "default": ["summary"],
            "description": (
                "페르소나 토글(예: ['summary', 'professional']). 미지정 시 ['summary']."
            ),
        },
        "follow_ups": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "공통 follow-up 질문 리스트.",
        },
        "single_turn": {
            "type": "boolean",
            "default": False,
            "description": (
                "단일턴 모드(모든 질문을 한 chat 호출에 묶는다). 자동 follow-up은 비활성."
            ),
        },
        "output_dir": {
            "type": "string",
            "default": "outputs/",
            "description": "결과 JSON 저장 디렉토리. 미지정 시 outputs/.",
        },
    },
    "required": ["product", "questions"],
    "additionalProperties": False,
}


_REPORT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "json_path": {
            "type": "string",
            "description": "interview 도구가 생성한 결과 JSON 경로(필수).",
        },
        "top_n": {
            "type": "integer",
            "minimum": 1,
            "default": 10,
            "description": "거절 사유 상위 N개.",
        },
        "include_drift": {
            "type": "boolean",
            "default": False,
            "description": "drift record를 정량 집계에 포함할지 여부.",
        },
        "output_dir": {
            "type": "string",
            "description": "리포트 저장 디렉토리. 미지정 시 입력 JSON과 같은 디렉토리.",
        },
    },
    "required": ["json_path"],
    "additionalProperties": False,
}


_BUILD_PERSONA_PROMPT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "product": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "follow_ups": {"type": "array", "items": {"type": "string"}, "default": []},
        "persona_id": {"type": "string", "description": "단일 페르소나 uuid"},
        "filter": {"type": "string", "description": "필터 DSL(persona_id 미지정 시)"},
        "n": {"type": "integer", "minimum": 1, "default": 1},
        "seed": {"type": "integer", "default": 42},
        "persona_fields": {"type": "array", "items": {"type": "string"}, "default": ["summary"]},
    },
    "required": ["product", "questions"],
    "additionalProperties": False,
}


_BUILD_BATCH_PROMPTS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "product": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "follow_ups": {"type": "array", "items": {"type": "string"}, "default": []},
        "filter": {"type": "string"},
        "persona_ids": {"type": "array", "items": {"type": "string"}, "default": []},
        "n": {"type": "integer", "minimum": 1},
        "seed": {"type": "integer", "default": 42},
        "persona_fields": {"type": "array", "items": {"type": "string"}, "default": ["summary"]},
    },
    "required": ["product", "questions"],
    "additionalProperties": False,
}


_AGGREGATE_RESULTS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "records": {"type": "array", "items": {"type": "object"}, "minItems": 1},
        "product": {"type": "string", "default": ""},
        "questions": {"type": "array", "items": {"type": "string"}, "default": []},
        "slug": {"type": "string", "default": "korea-persona-interview"},
        "output_dir": {"type": "string", "default": "outputs/"},
        "top_n": {"type": "integer", "minimum": 1, "default": 10},
        "include_drift": {"type": "boolean", "default": False},
        "insights": {
            "type": "object",
            "description": (
                "정성 인사이트 옵션(common_reactions, insights, cohort_differences 키)을 "
                "호스트 sub-agent가 직접 채워 넘기면 그대로 리포트에 박힌다."
            ),
        },
    },
    "required": ["records"],
    "additionalProperties": False,
}


_HELPER_DRIFT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "인터뷰 응답 본문"},
        "persona_meta": {"type": "object", "description": "페르소나 dict(list_personas/build_persona_prompt 응답)"},
    },
    "required": ["text", "persona_meta"],
    "additionalProperties": False,
}


_HELPER_FOLLOWUP_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "threshold": {"type": "integer"},
        "ambiguous_keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["text"],
    "additionalProperties": False,
}


_HELPER_PARSE_SUMMARY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "raw_response": {"type": "string", "description": "LLM의 구조화 요약 응답 본문"},
    },
    "required": ["raw_response"],
    "additionalProperties": False,
}


_HELPER_RECORD_SCHEMA_SCHEMA: dict = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


# 도구 이름 → (description, inputSchema) 매핑. list_tools 메타데이터를 mode별로
# 잘라낼 때 사용한다.
_TOOL_METADATA: dict = {
    "healthcheck": (
        "MCP server 모드에서는 provider 엔드포인트 도달성을 검증하고, "
        "MCP orchestrator 모드에서는 도구 부팅 자체와 cwd, dataset 정보를 돌려줍니다.",
        _HEALTHCHECK_SCHEMA,
    ),
    "list_personas": (
        "필터 결과에 해당하는 한국인 합성 페르소나(NVIDIA Nemotron-Personas-Korea, "
        "CC BY 4.0)를 미리 보여줍니다. 인터뷰 표본이 의도한 인구 통계 분포에 "
        "부합하는지 사전 점검할 때 사용합니다.",
        _LIST_PERSONAS_SCHEMA,
    ),
    "interview": (
        "사업 아이템과 질문 리스트로 N명의 합성 페르소나에게 배치 인터뷰를 "
        "실행합니다. server-side LLM을 사용해 결과 JSON 경로와 요약 통계, 토큰 사용량을 "
        "돌려줍니다. 결과 JSON은 ``report`` 도구에 그대로 입력할 수 있습니다. "
        "MCP server 모드 전용 도구입니다.",
        _INTERVIEW_SCHEMA,
    ),
    "report": (
        "결과 JSON에서 마크다운 리포트를 생성합니다. 정량 지표는 모든 모드에서 "
        "동일하게 채워지며, 정성 인사이트는 MCP server 모드에서만 server-side LLM "
        "호출로 채웁니다(MCP orchestrator 모드는 fallback 메시지).",
        _REPORT_SCHEMA,
    ),
    "build_persona_prompt": (
        "단일 페르소나에 대한 시스템 프롬프트와 페르소나 dict를 돌려줍니다. "
        "MCP orchestrator 모드 전용. 호스트 sub-agent가 받은 시스템 프롬프트로 "
        "자기 LLM을 호출해 인터뷰를 수행합니다.",
        _BUILD_PERSONA_PROMPT_SCHEMA,
    ),
    "build_batch_prompts": (
        "N명 분의 시스템 프롬프트 + 페르소나 dict를 한 번에 돌려줍니다. "
        "MCP orchestrator 모드 전용. 호스트 sub-agent가 fan-out으로 N개의 인터뷰를 "
        "병렬 수행하는 흐름을 지원합니다.",
        _BUILD_BATCH_PROMPTS_SCHEMA,
    ),
    "aggregate_results": (
        "호스트가 모은 인터뷰 record 리스트로 정량 집계와 마크다운 리포트를 생성합니다. "
        "MCP orchestrator 모드 전용. 정성 인사이트는 호스트 sub-agent가 직접 만들어 "
        "insights 인자로 전달하면 그대로 리포트에 박힙니다.",
        _AGGREGATE_RESULTS_SCHEMA,
    ),
    "detect_persona_drift": (
        "페르소나 깨짐 휴리스틱(영어 비율 + 4축 정밀 정규식)을 호스트가 명시 호출할 수 "
        "있도록 노출합니다. CLI와 MCP server는 자동 적용하지만, MCP orchestrator는 "
        "호스트가 본 도구를 호출해야 같은 임계값으로 drift를 판정할 수 있습니다.",
        _HELPER_DRIFT_SCHEMA,
    ),
    "should_auto_follow_up": (
        "짧은 답변/모호 키워드 매칭으로 자동 follow-up 트리거 여부를 돌려줍니다. "
        "임계값과 키워드는 heuristics.* yaml 값을 따릅니다.",
        _HELPER_FOLLOWUP_SCHEMA,
    ),
    "parse_structured_summary": (
        "LLM의 구조화 요약 응답 텍스트(JSON)를 정규화된 structured_summary dict로 "
        "파싱합니다. 코드 펜스/주변 텍스트가 섞여도 가장 바깥 JSON 객체만 골라냅니다.",
        _HELPER_PARSE_SUMMARY_SCHEMA,
    ),
    "interview_record_schema": (
        "aggregate_results 도구에 전달할 record dict 형식을 안내합니다. 필드 이름, "
        "허용 enum, 한 record 예시를 돌려줍니다.",
        _HELPER_RECORD_SCHEMA_SCHEMA,
    ),
}


# 호환성: 기존 import 경로를 유지하기 위해 _TOOL_HANDLERS 에는 server 모드 핸들러만
# 노출한다(외부 테스트는 본 매핑을 사용해 dispatch 호환성을 확인했었다).
from .mcp_handlers import common as _common_handlers
from .mcp_handlers import server as _server_handlers


_TOOL_HANDLERS: dict = {
    "healthcheck": _server_handlers.healthcheck,
    "list_personas": _common_handlers.list_personas,
    "interview": _server_handlers.interview,
    "report": _common_handlers.report,
}


def _to_json_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _backend_label(config) -> str:
    """현재 모드에 대응하는 응답 라벨(``mcp_server`` 또는 ``mcp_orchestrator``)."""

    return "mcp_server" if config.mcp.mode == "server" else "mcp_orchestrator"


def _build_backend(config):
    """현재 도구 호출을 위한 LLM 백엔드를 ``mcp.mode``에 따라 구성한다.

    v1.2.0(ADR-005)부터 두 모드만 다룬다.

    - ``mode == "server"``: ``build_cli_backend(config.llm)``으로 CLI와 동일한
      OpenAIBackend/AnthropicBackend를 만든다. server-side에 API 키가 필요하다
    - ``mode == "orchestrator"``: server-side LLM을 호출하지 않으므로 ConfigError
      로 차단한다. orchestrator 도구 핸들러는 본 함수를 호출하지 않는다
    """

    from .llm_backend import build_cli_backend

    mode = config.mcp.mode

    if mode == "server":
        logger.info(
            "MCP server 백엔드 사용(provider=%s, model=%s)",
            config.llm.provider,
            config.llm.model,
            extra={
                "llm_backend": "mcp_server",
                "provider": config.llm.provider,
                "model": config.llm.model,
            },
        )
        return build_cli_backend(config.llm)

    raise ConfigError(
        "MCP orchestrator 모드에서는 server-side LLM 호출이 불가합니다. "
        "본 도구를 호출한 호스트 sub-agent가 자기 LLM으로 인터뷰를 수행해야 합니다. "
        "build_persona_prompt 또는 build_batch_prompts 도구로 시스템 프롬프트를 받고, "
        "호스트가 인터뷰 결과 record를 모아 aggregate_results 도구로 리포트를 생성하는 흐름을 사용해 주세요"
    )


async def dispatch_tool(name: str, arguments: Optional[dict]) -> dict:
    """이름으로 도구 호출을 dispatch하고 공통 응답 dict를 돌려준다.

    핸들러 내부에서 발생한 에러는 ``error_payload`` dict로 변환해 MCP
    TextContent 봉투가 항상 JSON 객체를 실어 보내도록 한다.

    mode별 도구 노출 정책은 ``mcp_handlers.HANDLERS``를 따른다. 본 mode에 노출
    되지 않는 도구는 ``tool_unavailable_in_mode`` 코드로 차단된다.
    """

    args = arguments or {}
    if not isinstance(args, dict):
        return _error_payload(
            "invalid_arguments",
            f"도구 인자는 JSON object여야 합니다: {type(args).__name__}",
            exit_code=1,
        )

    # mode 결정. config 로드 실패는 backend 라벨 없이 에러를 돌려준다(handler 안
    # 에서도 같은 처리를 하지만 unknown_tool 분기 전에 mode를 알아야 한다).
    try:
        config = load_config(yaml_path=None, cli_overrides=None)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    mode = config.mcp.mode
    label = _backend_label(config)

    handler = HANDLERS.get((mode, name))
    if handler is None:
        # mode에 노출되지 않은 도구. 다른 mode에서는 사용 가능한 도구라면 친절한
        # 안내를 단다.
        all_known = {tool for (_, tool) in HANDLERS.keys()}
        if name not in all_known:
            return _error_payload(
                "unknown_tool",
                f"알 수 없는 도구 이름입니다: {name!r}. "
                f"현재 mode({mode})에서 사용 가능: {sorted(TOOLS_BY_MODE.get(mode, []))}",
                exit_code=1,
                backend=label,
            )
        return _error_payload(
            "tool_unavailable_in_mode",
            f"'{name}' 도구는 현재 mode({mode})에서 사용할 수 없습니다. "
            f"현재 mode 사용 가능 도구: {sorted(TOOLS_BY_MODE.get(mode, []))}. "
            "yaml의 mcp.mode를 변경하거나 다른 도구를 사용해 주세요",
            exit_code=1,
            backend=label,
        )

    try:
        return await handler(args)
    except KeyboardInterrupt:
        return _error_payload(
            "user_interrupted",
            "사용자 중단으로 도구 실행을 중지했습니다",
            exit_code=130,
            backend=label,
        )
    except Exception as exc:  # noqa: BLE001 - 최후의 안전망
        logger.error(
            "MCP 도구 실행 안전망 발동",
            extra={"tool": name, "reason": str(exc), "exception_type": type(exc).__name__},
            exc_info=True,
        )
        return _error_payload(
            "unhandled_exception",
            f"도구 실행 중 예상치 못한 예외: {type(exc).__name__}: {exc}",
            exit_code=1,
            backend=label,
        )


def _list_tools_metadata_for_mode(mode: str) -> list:
    """``mcp.types.Tool`` 메타데이터 객체 리스트를 mode에 맞춰 만든다.

    SDK가 없을 때도 ``import src.mcp_server``가 동작하도록 lazy import 한다.
    """

    from mcp import types

    tools = []
    for name in TOOLS_BY_MODE.get(mode, []):
        meta = _TOOL_METADATA.get(name)
        if meta is None:
            continue
        description, schema = meta
        tools.append(
            types.Tool(
                name=name,
                description=description,
                inputSchema=schema,
            )
        )
    return tools


def _list_tools_metadata() -> list:
    """기존 호출자 호환용. 현재 yaml의 mode 기준으로 mode별 tool 리스트를 돌려준다.

    config 로드에 실패하면 server 모드 도구를 돌려준다(가장 일반적인 default).
    """

    try:
        config = load_config(yaml_path=None, cli_overrides=None)
        mode = config.mcp.mode
    except ConfigError:
        mode = "server"
    return _list_tools_metadata_for_mode(mode)


_MISSING_SDK_MESSAGE = (
    "mcp Python SDK를 찾을 수 없습니다. "
    "`uv pip sync requirements.lock requirements-dev.lock`로 의존성을 설치한 뒤 다시 실행해 주세요. "
    "(직접 설치는 `pip install mcp==1.27.0`)"
)


async def _serve_stdio() -> None:
    """현재 모드에 맞는 도구를 등록한 MCP stdio 서버를 실행한다."""

    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types

    server: Server = Server("korea-persona-interview")

    @server.list_tools()
    async def _on_list_tools() -> list:
        return _list_tools_metadata()

    @server.call_tool()
    async def _on_call_tool(name: str, arguments: Optional[dict[str, Any]]) -> list:
        result = await dispatch_tool(name, arguments)
        return [types.TextContent(type="text", text=_to_json_text(result))]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """``python -m src.mcp_server``와 ``kpi-mcp-server``의 엔트리 포인트."""

    try:
        asyncio.run(_serve_stdio())
    except ImportError as exc:
        print(
            f"{_MISSING_SDK_MESSAGE}\n\n원인: {exc}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
