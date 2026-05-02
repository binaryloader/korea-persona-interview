"""MCP(Model Context Protocol) 서버 진입점.

외부 에이전트(Claude Code, Cursor, Codex 등)가 본 도구를 stdio 기반 도구로
등록해 자연어로 호출할 수 있도록 4개 도구를 노출한다(라운드 C1).

도구 목록은 아래와 같다.

- ``healthcheck``: OpenAI 서버 응답과 모델 가용성 확인
- ``list_personas``: 필터 결과 페르소나 미리 보기
- ``interview``: 배치 인터뷰 실행 후 결과 JSON 경로와 summary 반환
- ``report``: 결과 JSON에서 마크다운 리포트 생성

application 계층(`run_batch`, `generate_report`, `MlxLLMClient` 등)을 그대로
재사용한다. MCP는 비대화식이라 tqdm/ANSI 컬러/[OK] 라벨 출력 없이 stdout 대신
JSON 결과를 ``TextContent``로 돌려준다. 로그는 stderr/jsonl 채널에 그대로 흐른다
(logging.md §3 구조화 + 격리).

본 모듈은 ``mcp`` SDK가 부재한 환경(예: lock 미동기화)에서도 import 자체가 깨지지
않도록 ``mcp`` import는 ``main()`` 진입 시점에 lazy하게 수행한다. import 실패 시
사용자에게 친절한 한국어 안내를 stderr로 출력하고 exit 1로 종료한다
(error-handling.md §1).

진입점은 두 가지다.

- ``python -m src.mcp_server``: 모듈 단위 실행
- ``kpi-mcp-server`` (pyproject.toml console script, 라운드 C4)
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
from .interview import InterviewSession  # noqa: F401 - dry-run/single 호출 가능 여지
from .llm_backend import (
    LLMBackend,
    McpSamplingBackend,
    OpenAIBackend,
    select_backend,
)
from .llm_client import MlxLLMClient  # noqa: F401 - 외부 호환 import 보존
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


# ---------------------------------------------------------------------------
# MCP 도구 입력 스키마(JSON Schema draft-07)
# ---------------------------------------------------------------------------


_HEALTHCHECK_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "base_url": {
            "type": "string",
            "description": (
                "OpenAI 호환 서버 base URL. 미지정 시 config.yaml의 llm.base_url을 사용한다."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "이 호출에 한해 사용할 모델 ID(예: gpt-4o, gpt-4o-mini). "
                "config.yaml의 llm.model을 일회성으로 덮어쓴다."
            ),
        },
    },
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
        "model": {
            "type": "string",
            "description": (
                "이 인터뷰에 한해 사용할 모델 ID. 미지정 시 config.yaml의 llm.model."
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
        "model": {
            "type": "string",
            "description": (
                "정성 인사이트 호출에 한해 사용할 모델 ID. 미지정 시 config.yaml의 llm.model."
            ),
        },
    },
    "required": ["json_path"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# 에러 응답 헬퍼
# ---------------------------------------------------------------------------


def _error_payload(code: str, message: str, *, exit_code: int = 1) -> dict:
    """모든 도구가 공통으로 사용하는 에러 응답 dict.

    MCP에서는 도구 실행 결과를 ``TextContent``로 돌려주므로 본 dict를 JSON
    문자열로 직렬화해 반환한다. 호출자(에이전트)가 ``error`` 필드 존재로 실패를
    판정한다(api-design.md §3과 동일한 형태로 통일).
    """

    return {
        "error": {
            "code": code,
            "message": message,
            "exit_code": int(exit_code),
        }
    }


def _to_json_text(payload: dict) -> str:
    """도구 응답을 사람이 읽기 좋게 들여쓴 JSON 문자열로 변환한다.

    ``ensure_ascii=False``로 한국어를 그대로 보존한다.
    """

    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _persona_to_dict(persona: PersonaMeta) -> dict:
    """``PersonaMeta`` dataclass를 MCP 응답용 dict로 변환한다.

    ``raw`` dict는 토큰 비용을 줄이기 위해 응답에서 제외한다(분석 시 필요한
    원본 raw는 인터뷰 결과 JSON 안에 그대로 보존된다).
    """

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


# ---------------------------------------------------------------------------
# 공통 셋업
# ---------------------------------------------------------------------------


def _setup_logging_for_run(config: AppConfig) -> None:
    """MCP 도구 호출 직전 매번 새 request_id를 박은 로거 세팅.

    여러 도구가 한 프로세스 안에서 호출되어도 jsonl 로그에서 request 단위로
    분리된다. 출력은 stderr/jsonl이라 MCP stdout(JSON-RPC)을 오염시키지 않는다.
    """

    log_dir = config.output_dir / "logs"
    log_path = log_dir / f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"
    configure_logging(level=config.log_level, json_path=log_path)
    bind_request_id(uuid.uuid4().hex)


def _load_config_with_overrides(overrides: Optional[dict]) -> AppConfig:
    """config.yaml 로드 + CLI에서 받은 dict override 적용.

    ConfigError는 호출자가 도구 응답으로 변환한다.
    """

    return load_config(yaml_path=None, cli_overrides=overrides)


# 모듈 단위 hook. 테스트에서 실제 ``Server.request_context`` 의존을 우회하기 위해
# 모킹할 수 있도록 함수 단위로 분리한다(architecture.md §10 테스트 가능성).
def _current_sampling_session() -> Optional[Any]:
    """현재 도구 호출 컨텍스트에서 MCP ServerSession을 꺼낸다.

    호출자가 ``_serve_stdio`` 안의 ``call_tool`` 핸들러일 때 ``mcp_server`` 모듈 변수
    ``_active_server``의 ``request_context.session``을 통해 접근할 수 있다. 컨텍스트가
    없거나 sampling capability가 없으면 ``None``을 반환한다.

    클라이언트 sampling capability 여부는 ``McpSamplingBackend.healthcheck``가
    재확인한다. 본 함수는 단순히 세션 객체 존재 여부만 본다.
    """

    server = _ACTIVE_SERVER
    if server is None:
        return None
    try:
        ctx = server.request_context
    except (LookupError, AttributeError):
        return None
    return getattr(ctx, "session", None)


def _build_backend(config: AppConfig) -> LLMBackend:
    """현재 컨텍스트와 config 정책으로 백엔드 인스턴스를 만든다.

    선택 정책은 ``select_backend``가 담당한다. 본 함수는 sampling 세션을 본 모듈에서
    수집해 정책에 넘기는 thin wrapper다.
    """

    session = _current_sampling_session()
    backend = select_backend(
        config=config.llm,
        backend_choice=config.llm.backend,
        sampling_session=session,
    )
    if isinstance(backend, McpSamplingBackend):
        logger.info(
            "MCP sampling 백엔드 사용(클라이언트 LLM 위임, OpenAI 키 불필요)",
            extra={"llm_backend": "mcp_sampling"},
        )
    else:
        logger.info(
            "OpenAI 백엔드 사용(OPENAI_API_KEY 필요)",
            extra={"llm_backend": "openai", "model": config.llm.model},
        )
    return backend


# ``_serve_stdio``가 실행 중일 때 활성 ``Server`` 인스턴스를 보관한다. ``call_tool``
# 핸들러가 호출될 때 ``server.request_context``를 통해 sampling 세션에 접근하기
# 위함이다. 핸들러 함수에 인자로 전달할 수 없는 mcp SDK 구조 때문에 모듈 변수로 둔다
# (단일 프로세스 안에서 한 번에 하나의 stdio 서버만 동작하므로 race condition 없음).
_ACTIVE_SERVER: Optional[Any] = None


# ---------------------------------------------------------------------------
# 도구 핸들러
# ---------------------------------------------------------------------------


async def _handle_healthcheck(arguments: dict) -> dict:
    """``healthcheck`` 도구. OpenAI 서버 응답과 모델 가용성 확인."""

    base_url = arguments.get("base_url")
    model = arguments.get("model")

    overrides: dict = {}
    if base_url or model:
        llm_overrides: dict = {}
        if base_url:
            llm_overrides["base_url"] = str(base_url)
        if model:
            llm_overrides["model"] = str(model)
        overrides["llm"] = llm_overrides

    try:
        config = _load_config_with_overrides(overrides or None)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    _setup_logging_for_run(config)

    try:
        backend = _build_backend(config)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    try:
        async with backend as client:
            models = await client.healthcheck()
    except ServerNotReachableError as exc:
        return _error_payload(
            "server_not_reachable",
            f"OpenAI 서버에 연결할 수 없습니다: {exc}",
            exit_code=1,
        )
    except ConfigError as exc:
        message = str(exc)
        code = (
            "api_key_invalid_or_missing"
            if ("API 키" in message or "OPENAI_API_KEY" in message)
            else "config_error"
        )
        return _error_payload(code, message, exit_code=1)

    return {
        "ok": True,
        "base_url": config.llm.base_url,
        "model": config.llm.model,
        "backend": "mcp_sampling" if isinstance(backend, McpSamplingBackend) else "openai",
        "models": list(models),
    }


async def _handle_list_personas(arguments: dict) -> dict:
    """``list_personas`` 도구. 필터 결과 페르소나 미리 보기."""

    filter_spec: Optional[str] = arguments.get("filter")
    limit = int(arguments.get("limit", 20))
    seed = int(arguments.get("seed", 42))

    if limit < 1:
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

    # 필터 DSL 사전 검증.
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
            n=limit,
            seed=seed,
            field_map=config.dataset.field_map,
            gender_aliases=config.dataset.gender_aliases,
            province_aliases=config.dataset.province_aliases,
            dataset_name=config.dataset.name,
            split=config.dataset.split,
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
    """``interview`` 도구. 배치 인터뷰 실행 후 결과 JSON 경로와 summary 반환."""

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
    n = int(arguments.get("n", 10))
    seed = int(arguments.get("seed", 42))
    concurrency = int(arguments.get("concurrency", 5))
    persona_fields = arguments.get("persona_fields") or ["summary"]
    follow_ups = arguments.get("follow_ups") or []
    single_turn = bool(arguments.get("single_turn", False))
    model = arguments.get("model")
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
    if model:
        overrides["llm"] = {"model": str(model)}

    try:
        config = _load_config_with_overrides(overrides)
    except ConfigError as exc:
        return _error_payload("config_error", str(exc), exit_code=1)

    _setup_logging_for_run(config)

    # 필터 DSL 사전 검증.
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
            n=n,
            seed=seed,
            field_map=config.dataset.field_map,
            gender_aliases=config.dataset.gender_aliases,
            province_aliases=config.dataset.province_aliases,
            dataset_name=config.dataset.name,
            split=config.dataset.split,
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
            f"OpenAI 서버에 연결할 수 없습니다: {exc}",
            exit_code=1,
        )
    except DatasetUnavailableError as exc:
        return _error_payload("dataset_unavailable", str(exc), exit_code=1)
    except ConfigError as exc:
        message = str(exc)
        code = (
            "api_key_invalid_or_missing"
            if ("API 키" in message or "OPENAI_API_KEY" in message)
            else "config_error"
        )
        return _error_payload(code, message, exit_code=1)

    summary = envelope.summary
    usage = envelope.usage
    backend_label = (
        "mcp_sampling" if isinstance(backend, McpSamplingBackend) else "openai"
    )
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
        "estimated_cost_usd": envelope.estimated_cost_usd,
        "model": config.llm.model,
        "backend": backend_label,
        "failure_reason_counts": dict(envelope.failure_reason_counts),
    }
    return payload


async def _handle_report(arguments: dict) -> dict:
    """``report`` 도구. 결과 JSON에서 마크다운 리포트 생성."""

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
    model = arguments.get("model")

    if top_n < 1:
        return _error_payload(
            "invalid_argument",
            f"top_n은 1 이상이어야 합니다. 입력값: {top_n}",
            exit_code=1,
        )

    overrides: dict = {}
    if model:
        overrides["llm"] = {"model": str(model)}

    try:
        config = _load_config_with_overrides(overrides or None)
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
            f"OpenAI 서버에 연결할 수 없습니다: {exc}",
            exit_code=1,
        )

    return {
        "ok": True,
        "output_path": str(report_path),
        "input_path": str(json_path),
        "top_n": top_n,
        "include_drift": include_drift,
    }


# ---------------------------------------------------------------------------
# dispatch 테이블 + 메인 dispatch 함수
# ---------------------------------------------------------------------------


_TOOL_HANDLERS: dict = {
    "healthcheck": _handle_healthcheck,
    "list_personas": _handle_list_personas,
    "interview": _handle_interview,
    "report": _handle_report,
}


async def dispatch_tool(name: str, arguments: Optional[dict]) -> dict:
    """도구 이름과 인자 dict를 받아 응답 dict를 돌려준다.

    예외는 모두 본 함수 안에서 ``_error_payload``로 감싼다. MCP 핸들러는 본
    함수가 항상 dict를 반환한다고 가정하고 ``TextContent``로 직렬화한다.
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
        # MCP는 보통 SIGINT가 호스트(Claude Code 등)에서 전달되지 않지만 수동
        # 실행 중 ctrl+c를 흡수해 서버 자체가 죽는 일을 막는다.
        return _error_payload(
            "user_interrupted",
            "사용자 중단으로 도구 실행을 중지했습니다",
            exit_code=130,
        )
    except Exception as exc:  # noqa: BLE001 - 안전망
        # 도메인 예외는 각 핸들러가 이미 처리한다. 본 분기는 알려지지 않은
        # 예외가 도구 응답으로 누출되지 않도록 마지막 안전망이다.
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


# ---------------------------------------------------------------------------
# MCP 도구 메타(서버 시작 시 등록용)
# ---------------------------------------------------------------------------


def _list_tools_metadata() -> list:
    """``mcp.types.Tool`` 인스턴스 리스트를 만든다.

    main()에서 import할 때만 호출되어 mcp 의존이 없는 환경에서도 본 모듈 import는
    가능하다(테스트 격리).
    """

    from mcp import types  # 지역 import - mcp SDK 부재 시 main()에서만 실패.

    return [
        types.Tool(
            name="healthcheck",
            description=(
                "OpenAI Chat Completions API 서버 응답과 모델 가용성을 확인합니다. "
                "최초 인터뷰 호출 전 한 번 실행해 키와 네트워크를 검증할 때 사용합니다."
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
                "실행합니다. 결과 JSON 경로와 요약 통계, 토큰 사용량과 비용 추정을 "
                "돌려줍니다. 결과 JSON은 ``report`` 도구에 그대로 입력할 수 있습니다."
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


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


_MISSING_SDK_MESSAGE = (
    "mcp Python SDK를 찾을 수 없습니다. "
    "`uv pip sync requirements.lock requirements-dev.lock`로 의존성을 설치한 뒤 다시 실행해 주세요. "
    "(직접 설치는 `pip install mcp==1.27.0`)"
)


async def _serve_stdio() -> None:
    """MCP stdio 서버 본체.

    ``mcp.server.Server``에 ``list_tools``/``call_tool`` 핸들러를 등록하고
    ``stdio_server``로 stdin/stdout JSON-RPC 채널을 연결한다.

    활성 ``Server`` 인스턴스를 모듈 변수 ``_ACTIVE_SERVER``에 등록해 도구 핸들러가
    ``request_context.session``으로 sampling을 호출할 수 있게 한다(``_build_backend``
    참고).
    """

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
    """``python -m src.mcp_server`` 와 ``kpi-mcp-server`` 진입점.

    mcp SDK import 실패 시 친절한 한국어 안내를 stderr에 출력하고 exit 1로
    종료한다(error-handling.md §1).
    """

    try:
        asyncio.run(_serve_stdio())
    except ImportError as exc:
        # mcp SDK 부재. 사용자가 lock 동기화를 잊은 케이스의 첫 안내.
        print(
            f"{_MISSING_SDK_MESSAGE}\n\n원인: {exc}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    except KeyboardInterrupt:
        # MCP 호스트(Claude Code 등) 종료 신호. 정상 종료로 처리.
        sys.exit(0)


if __name__ == "__main__":
    main()
