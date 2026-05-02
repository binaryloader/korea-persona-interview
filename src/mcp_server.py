"""MCP 서버 엔트리 포인트.

stdio JSON-RPC 위에 네 개의 도구(``healthcheck``, ``list_personas``,
``interview``, ``report``)를 노출해서 외부 에이전트(Claude Code, Cursor,
Codex 등)가 자연어로 인터뷰 파이프라인을 구동할 수 있게 한다.

도구가 LLM 호출이 필요할 때 추론은 항상 ``sampling/createMessage``를 통해
호스트 에이전트에 위임한다. 서버 자체는 OpenAI/Anthropic API 키를 보유하지
않으며 비용은 호스트 LLM에 청구된다. 호스트가 sampling capability를
노출하지 않으면 CLI 엔트리 포인트로 안내하는 메시지와 함께 도구 호출이
실패한다.

애플리케이션 계층 함수(``run_batch``, ``generate_report``)는 그대로
재사용한다. MCP 서버는 비대화형으로 실행되므로 tqdm 프로그레스, ANSI
색상, ``[OK]`` 라벨은 비활성화하고 결과는 ``TextContent`` 봉투에 JSON으로
실어 보낸다. 로그는 stderr와 jsonl로 흘려보낸다.

``mcp`` SDK는 ``main()`` 안에서 lazy import 한다. 덕분에 SDK가 없어도 이
모듈 자체는 문제없이 import되고 사용자는 stack trace 대신 안내 메시지와
종료 코드 1을 본다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .batch import run_batch
from .config import AppConfig, load_config
from .llm_backend import McpSamplingBackend
from .load_personas import load_and_sample, parse_filter
from .logging_setup import bind_request_id, configure_logging
from .models import (
    ConfigError,
    DatasetUnavailableError,
    EmptyValidRecordsError,
    FilterMatchedZeroError,
    PersonaMeta,
    ServerNotReachableError,
)
from .report import ReportOptions, generate_report


logger = logging.getLogger(__name__)


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


def _error_payload(code: str, message: str, *, exit_code: int = 1) -> dict:
    """모든 도구 핸들러에서 공통으로 쓰는 에러 응답 dict를 만든다.

    ``ok: false`` 필드는 CLI ``--json`` 모드 봉투와 같은 형태이므로 MCP
    클라이언트는 도구 출력을 읽을 때 단일 키 하나로 분기할 수 있다.
    """

    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "exit_code": int(exit_code),
        },
    }


def _to_json_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _persona_to_dict(persona: PersonaMeta) -> dict:
    """``PersonaMeta``를 JSON 친화적인 dict로 변환한다(``raw`` 필드 제외)."""

    return {
        "persona_id": persona.persona_id,
        "name": persona.name,
        "gender": persona.gender,
        "age": persona.age,
        "region": persona.region,
        "subregion": persona.subregion,
        "occupation": persona.occupation,
        "marital": persona.marital,
        "education": persona.education,
        "family_type": persona.family_type,
        "housing_type": persona.housing_type,
    }


def _setup_logging_for_run(config: AppConfig) -> None:
    """도구 호출마다 새로운 request id로 구조화 로깅을 구성한다."""

    log_dir = config.output_dir / "logs"
    log_path = log_dir / f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"
    configure_logging(level=config.log_level, json_path=log_path)
    bind_request_id(uuid.uuid4().hex)


def _load_config_with_overrides(overrides: Optional[dict]) -> AppConfig:
    return load_config(yaml_path=None, cli_overrides=overrides)


def _current_sampling_session() -> Optional[Any]:
    """현재 처리 중인 도구 호출의 활성 MCP ``ServerSession``을 돌려준다.

    ``mcp`` SDK가 도구 핸들러 콜백에 서버 인스턴스를 전달하지 않기 때문에
    모듈 레벨 변수에 보관한다. 프로세스당 stdio 서버는 하나만 돌아가므로
    레이스가 발생하지 않는다.
    """

    server = _ACTIVE_SERVER
    if server is None:
        return None
    try:
        ctx = server.request_context
    except (LookupError, AttributeError):
        return None
    return getattr(ctx, "session", None)


def _build_backend(config: AppConfig) -> McpSamplingBackend:
    """현재 처리 중인 도구 호출을 위한 sampling 백엔드를 구성한다.

    MCP 세션이 없을 때(예: 사용자가 MCP 호스트 밖에서 모듈을 직접 실행)는
    CLI 폴백 안내 메시지를 담은 ``ConfigError``를 던진다.
    """

    session = _current_sampling_session()
    if session is None:
        raise ConfigError(
            "MCP sampling 세션이 없습니다. "
            "이 모듈은 Claude Code/Cursor 같은 MCP 호스트가 stdio로 연결된 상태에서만 동작합니다. "
            "독립 실행이 필요하면 `python main.py interview ...` 또는 `kpi interview ...`를 사용해 주세요"
        )
    logger.info(
        "MCP sampling 백엔드 사용(클라이언트 LLM 위임)",
        extra={"llm_backend": "mcp_sampling"},
    )
    return McpSamplingBackend(session)


_ACTIVE_SERVER: Optional[Any] = None


async def _handle_healthcheck(arguments: dict) -> dict:
    """sampling capability로 호스트 LLM 가용성을 확인한다."""

    try:
        config = _load_config_with_overrides(None)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    _setup_logging_for_run(config)

    try:
        backend = _build_backend(config)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    try:
        async with backend as client:
            await client.healthcheck()
    except ServerNotReachableError as exc:
        return _error_payload(
            "server_not_reachable",
            f"MCP sampling capability 확인에 실패했습니다: {exc}",
            exit_code=1,
        )
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    return {
        "ok": True,
        "backend": "mcp_sampling",
    }


async def _handle_list_personas(arguments: dict) -> dict:
    filter_spec: Optional[str] = arguments.get("filter")
    limit = int(arguments.get("limit", 20))
    seed = int(arguments.get("seed", 42))
    persona_ids_raw = arguments.get("persona_ids") or []
    persona_ids_tuple = tuple(str(pid) for pid in persona_ids_raw if str(pid).strip())

    if limit < 1 and not persona_ids_tuple:
        return _error_payload(
            "invalid_argument",
            f"limit은 1 이상이어야 합니다. 입력값: {limit}",
            exit_code=1,
        )

    try:
        config = _load_config_with_overrides(None)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    _setup_logging_for_run(config)

    try:
        parse_filter(
            filter_spec,
            config.dataset.gender_aliases,
            config.dataset.province_aliases,
        )
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    try:
        personas = load_and_sample(
            filter_str=filter_spec,
            n=len(persona_ids_tuple) if persona_ids_tuple else limit,
            seed=seed,
            field_map=config.dataset.field_map,
            gender_aliases=config.dataset.gender_aliases,
            province_aliases=config.dataset.province_aliases,
            dataset_name=config.dataset.name,
            split=config.dataset.split,
            persona_ids=persona_ids_tuple or None,
        )
    except FilterMatchedZeroError as exc:
        return _error_payload("filter_matched_zero", str(exc), exit_code=2)
    except DatasetUnavailableError as exc:
        return _error_payload("dataset_unavailable", str(exc), exit_code=1)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    if not personas:
        return _error_payload(
            "filter_matched_zero",
            "필터 결과가 비어 있습니다. 조건을 완화해 주세요",
            exit_code=2,
        )

    return {
        "ok": True,
        "personas": [_persona_to_dict(p) for p in personas],
        "count": len(personas),
        "filter": filter_spec,
        "seed": seed,
    }


async def _handle_interview(arguments: dict) -> dict:
    product = arguments.get("product")
    questions = arguments.get("questions")
    if not isinstance(product, str) or not product.strip():
        return _error_payload(
            "missing_argument",
            "product(사업 아이템 설명)는 필수입니다",
            exit_code=1,
        )
    if not isinstance(questions, list) or not questions:
        return _error_payload(
            "missing_argument",
            "questions(질문 리스트)는 1개 이상 필요합니다",
            exit_code=1,
        )

    filter_spec: Optional[str] = arguments.get("filter")
    persona_ids_raw = arguments.get("persona_ids") or []
    persona_ids_tuple = tuple(str(pid) for pid in persona_ids_raw if str(pid).strip())
    n = int(arguments.get("n", 10))
    seed = int(arguments.get("seed", 42))
    concurrency = int(arguments.get("concurrency", 5))
    persona_fields = arguments.get("persona_fields") or ["summary"]
    follow_ups = arguments.get("follow_ups") or []
    single_turn = bool(arguments.get("single_turn", False))
    output_dir_raw = arguments.get("output_dir") or "outputs/"

    if not (1 <= concurrency <= 10):
        return _error_payload(
            "invalid_argument",
            f"concurrency는 1-10 범위만 허용합니다. 입력값: {concurrency}",
            exit_code=1,
        )
    if n < 1:
        return _error_payload(
            "invalid_argument",
            f"n은 1 이상이어야 합니다. 입력값: {n}",
            exit_code=1,
        )

    output_dir = Path(str(output_dir_raw))

    overrides: dict = {
        "batch": {
            "concurrency": concurrency,
            "persona_fields": [str(f) for f in persona_fields],
            "single_turn": single_turn,
        },
        "output": {"output_dir": str(output_dir)},
    }

    try:
        config = _load_config_with_overrides(overrides)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    _setup_logging_for_run(config)

    try:
        parse_filter(
            filter_spec,
            config.dataset.gender_aliases,
            config.dataset.province_aliases,
        )
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    questions_list = [str(q) for q in questions]
    follow_ups_list = [str(f) for f in follow_ups]

    try:
        personas = load_and_sample(
            filter_str=filter_spec,
            n=len(persona_ids_tuple) if persona_ids_tuple else n,
            seed=seed,
            field_map=config.dataset.field_map,
            gender_aliases=config.dataset.gender_aliases,
            province_aliases=config.dataset.province_aliases,
            dataset_name=config.dataset.name,
            split=config.dataset.split,
            persona_ids=persona_ids_tuple or None,
        )
    except FilterMatchedZeroError as exc:
        return _error_payload("filter_matched_zero", str(exc), exit_code=2)
    except DatasetUnavailableError as exc:
        return _error_payload("dataset_unavailable", str(exc), exit_code=1)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    try:
        backend = _build_backend(config)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    try:
        async with backend as client:
            envelope = await run_batch(
                personas=personas,
                product=product,
                questions=questions_list,
                follow_ups=follow_ups_list,
                llm=client,
                config=config,
                output_dir=output_dir,
                slug="korea-persona-interview",
                seed=seed,
                progress_disable=True,
            )
    except ServerNotReachableError as exc:
        return _error_payload(
            "server_not_reachable",
            f"MCP sampling 호출에 실패했습니다: {exc}",
            exit_code=1,
        )
    except DatasetUnavailableError as exc:
        return _error_payload("dataset_unavailable", str(exc), exit_code=1)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    summary = envelope.summary
    usage = envelope.usage
    payload: dict = {
        "ok": not envelope.partial_failure,
        "partial_failure": envelope.partial_failure,
        "output_path": str(envelope.output_path) if envelope.output_path else None,
        "summary": {
            "requested": summary.requested,
            "completed": summary.completed,
            "refused": summary.refused,
            "failed": summary.failed,
            "drift": summary.drift,
            "cancelled": summary.cancelled,
        },
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cached_tokens": usage.cached_tokens,
        },
        "model": config.llm.model,
        "backend": "mcp_sampling",
        "failure_reason_counts": dict(envelope.failure_reason_counts),
    }
    return payload


async def _handle_report(arguments: dict) -> dict:
    json_path_raw = arguments.get("json_path")
    if not isinstance(json_path_raw, str) or not json_path_raw.strip():
        return _error_payload(
            "missing_argument",
            "json_path는 필수입니다",
            exit_code=1,
        )
    json_path = Path(json_path_raw)
    if not json_path.exists():
        return _error_payload(
            "input_file_not_found",
            f"입력 JSON 파일을 찾을 수 없습니다: {json_path}",
            exit_code=1,
        )

    top_n = int(arguments.get("top_n", 10))
    include_drift = bool(arguments.get("include_drift", False))
    output_dir_raw = arguments.get("output_dir")
    output_dir = Path(str(output_dir_raw)) if output_dir_raw else None

    if top_n < 1:
        return _error_payload(
            "invalid_argument",
            f"top_n은 1 이상이어야 합니다. 입력값: {top_n}",
            exit_code=1,
        )

    try:
        config = _load_config_with_overrides(None)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    _setup_logging_for_run(config)

    options = ReportOptions(
        top_n=top_n,
        include_drift=include_drift,
        output_dir=output_dir,
    )

    try:
        backend = _build_backend(config)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    try:
        async with backend as client:
            report_path = await generate_report(
                json_path=json_path,
                options=options,
                llm=client,
                config=config,
            )
    except FileNotFoundError:
        return _error_payload(
            "input_file_not_found",
            f"입력 JSON 파일을 찾을 수 없습니다: {json_path}",
            exit_code=1,
        )
    except EmptyValidRecordsError as exc:
        return _error_payload("empty_valid_records", str(exc), exit_code=2)
    except ConfigError as exc:
        return _error_payload("input_file_schema", str(exc), exit_code=1)
    except ServerNotReachableError as exc:
        return _error_payload(
            "server_not_reachable",
            f"MCP sampling 호출에 실패했습니다: {exc}",
            exit_code=1,
        )

    return {
        "ok": True,
        "output_path": str(report_path),
        "input_path": str(json_path),
        "top_n": top_n,
        "include_drift": include_drift,
    }


_TOOL_HANDLERS: dict = {
    "healthcheck": _handle_healthcheck,
    "list_personas": _handle_list_personas,
    "interview": _handle_interview,
    "report": _handle_report,
}


async def dispatch_tool(name: str, arguments: Optional[dict]) -> dict:
    """이름으로 도구 호출을 dispatch하고 공통 응답 dict를 돌려준다.

    핸들러 내부에서 발생한 에러는 ``_error_payload`` dict로 변환해 MCP
    TextContent 봉투가 항상 JSON 객체를 실어 보내도록 한다.
    """

    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return _error_payload(
            "unknown_tool",
            f"알 수 없는 도구 이름입니다: {name!r}. "
            f"사용 가능: {sorted(_TOOL_HANDLERS)}",
            exit_code=1,
        )

    args = arguments or {}
    if not isinstance(args, dict):
        return _error_payload(
            "invalid_arguments",
            f"도구 인자는 JSON object여야 합니다: {type(args).__name__}",
            exit_code=1,
        )

    try:
        return await handler(args)
    except KeyboardInterrupt:
        return _error_payload(
            "user_interrupted",
            "사용자 중단으로 도구 실행을 중지했습니다",
            exit_code=130,
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
        )


def _list_tools_metadata() -> list:
    """``mcp.types.Tool`` 메타데이터 객체 리스트를 만든다.

    SDK가 없을 때도 ``import src.mcp_server``가 동작하도록 lazy import 한다.
    """

    from mcp import types

    return [
        types.Tool(
            name="healthcheck",
            description=(
                "MCP 호스트가 sampling capability를 노출하는지 확인합니다. "
                "최초 인터뷰 호출 전 한 번 실행해 호스트 LLM 가용성을 검증할 때 사용합니다."
            ),
            inputSchema=_HEALTHCHECK_SCHEMA,
        ),
        types.Tool(
            name="list_personas",
            description=(
                "필터 결과에 해당하는 한국인 합성 페르소나(NVIDIA Nemotron-Personas-Korea, "
                "CC BY 4.0)를 미리 보여줍니다. 인터뷰 표본이 의도한 인구 통계 분포에 "
                "부합하는지 사전 점검할 때 사용합니다."
            ),
            inputSchema=_LIST_PERSONAS_SCHEMA,
        ),
        types.Tool(
            name="interview",
            description=(
                "사업 아이템과 질문 리스트로 N명의 합성 페르소나에게 배치 인터뷰를 "
                "실행합니다. 결과 JSON 경로와 요약 통계, 토큰 사용량을 돌려줍니다. "
                "결과 JSON은 ``report`` 도구에 그대로 입력할 수 있습니다."
            ),
            inputSchema=_INTERVIEW_SCHEMA,
        ),
        types.Tool(
            name="report",
            description=(
                "interview 도구가 생성한 결과 JSON에서 마크다운 리포트를 생성합니다. "
                "정량 지표(의향률, 가격 수용가, 거절 사유, 코호트별 의향률)와 LLM 정성 "
                "인사이트를 결합한 마크다운 파일 경로를 돌려줍니다."
            ),
            inputSchema=_REPORT_SCHEMA,
        ),
    ]


_MISSING_SDK_MESSAGE = (
    "mcp Python SDK를 찾을 수 없습니다. "
    "`uv pip sync requirements.lock requirements-dev.lock`로 의존성을 설치한 뒤 다시 실행해 주세요. "
    "(직접 설치는 `pip install mcp==1.27.0`)"
)


async def _serve_stdio() -> None:
    """네 개의 도구를 등록한 MCP stdio 서버를 실행한다."""

    global _ACTIVE_SERVER

    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types

    server: Server = Server("korea-persona-interview")
    _ACTIVE_SERVER = server

    @server.list_tools()
    async def _on_list_tools() -> list:
        return _list_tools_metadata()

    @server.call_tool()
    async def _on_call_tool(name: str, arguments: Optional[dict[str, Any]]) -> list:
        result = await dispatch_tool(name, arguments)
        return [types.TextContent(type="text", text=_to_json_text(result))]

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        _ACTIVE_SERVER = None


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
