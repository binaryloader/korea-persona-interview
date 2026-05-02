"""MCP orchestrator 모드 전용 핸들러.

본 모드는 server-side LLM을 호출하지 않는다. 호스트 sub-agent(Claude Code의 Task tool 같은 sub-agent 기능)가 자기 LLM으로 인터뷰를 수행하며 본 도구는 데이터/프롬프트 helper만 노출한다.

도구 흐름은 아래와 같다.

1. ``build_persona_prompt`` 또는 ``build_batch_prompts``로 시스템 프롬프트와 페르소나 dict를 받는다
2. 호스트 sub-agent가 받은 프롬프트로 자기 LLM을 호출해 인터뷰를 수행한다
3. 호스트가 record를 모아 ``aggregate_results``로 정량 집계 + 마크다운 리포트 파일을 생성한다

본 모드의 helper 도구(detect_persona_drift, should_auto_follow_up, parse_structured_summary, interview_record_schema)는 ``helpers`` 모듈에 있으며 호스트가 명시 호출 시 동일 임계값/키워드를 적용한다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import load_config
from ..interview import build_system_prompt
from ..load_personas import load_and_sample, parse_filter
from ..models import (
    ConfigError,
    DatasetUnavailableError,
    EmptyValidRecordsError,
    Flags,
    FilterMatchedZeroError,
    InterviewRecord,
    MessageEntry,
    PersonaMeta,
    RawResponse,
    StructuredSummary,
    TokenUsage,
)
from ..report import (
    QualitativeInsights,
    ReportOptions,
    _records_summary,
    _resolve_output_path,
    _validate_records_for_report,
    compute_quant,
    generate_qualitative_insights,
    render_markdown,
)
from ._payloads import error_payload, persona_to_payload
from ._setup import backend_label, setup_logging_for_run


logger = logging.getLogger(__name__)


_SCHEMA_HINT = (
    "{\n"
    '  "intent": "positive | neutral | negative",\n'
    '  "acceptable_price_signal": "cheap | fair | expensive | null",\n'
    '  "willingness_to_pay": int|null,\n'
    '  "willingness_to_pay_currency": "KRW",\n'
    '  "rejection_reasons": [str, ...],\n'
    '  "one_line": "한 줄 요약(80자 이내)"\n'
    "}"
)


async def healthcheck(arguments: dict) -> dict:
    """MCP orchestrator 모드 healthcheck.

    server-side LLM 호출이 없으므로 도구 부팅 자체와 cwd, dataset 가용성을 돌려준다. dataset 가용성은 list_personas 도구로 별도 검증할 수 있다.
    """

    try:
        config = load_config(yaml_path=None, cli_overrides=None)
    except ConfigError as exc:
        return error_payload("config_error", str(exc), exit_code=1)

    setup_logging_for_run(config)
    label = backend_label(config)

    return {
        "ok": True,
        "backend": label,
        "cwd": str(Path.cwd()),
        "dataset_name": config.common.dataset.name,
    }


def _resolve_personas_for_prompt(
    arguments: dict,
    config,
) -> tuple:
    """build_persona_prompt / build_batch_prompts 공용 페르소나 해석 로직.

    Returns:
        ``(personas, error_dict_or_None)``. 에러가 있으면 두 번째 원소가 봉투,
        성공 시 None.
    """

    persona_id_raw = arguments.get("persona_id")
    persona_ids_raw = arguments.get("persona_ids") or []
    filter_spec: Optional[str] = arguments.get("filter")
    n = int(arguments.get("n", 0))
    seed = int(arguments.get("seed", 42))

    persona_ids: tuple = ()
    if persona_id_raw and isinstance(persona_id_raw, str):
        persona_ids = (persona_id_raw,)
    elif persona_ids_raw:
        persona_ids = tuple(str(pid) for pid in persona_ids_raw if str(pid).strip())

    label = backend_label(config)

    try:
        parse_filter(
            filter_spec,
            config.common.dataset.gender_aliases,
            config.common.dataset.province_aliases,
        )
    except ConfigError as exc:
        return [], error_payload(
            "config_error", str(exc), exit_code=1, backend=label
        )

    try:
        personas = load_and_sample(
            filter_str=filter_spec,
            n=len(persona_ids) if persona_ids else max(n, 1),
            seed=seed,
            field_map=config.common.dataset.field_map,
            gender_aliases=config.common.dataset.gender_aliases,
            province_aliases=config.common.dataset.province_aliases,
            dataset_name=config.common.dataset.name,
            split=config.common.dataset.split,
            persona_ids=persona_ids or None,
        )
    except FilterMatchedZeroError as exc:
        return [], error_payload(
            "filter_matched_zero", str(exc), exit_code=2, backend=label
        )
    except DatasetUnavailableError as exc:
        return [], error_payload(
            "dataset_unavailable", str(exc), exit_code=1, backend=label
        )
    except ConfigError as exc:
        return [], error_payload(
            "config_error", str(exc), exit_code=1, backend=label
        )

    return personas, None


def _build_prompt_for_persona(
    persona: PersonaMeta,
    product: str,
    questions: list,
    follow_ups: list,
    persona_fields: tuple,
    config,
) -> dict:
    """단일 페르소나용 시스템 프롬프트 + 메타 dict를 만든다."""

    system_prompt = build_system_prompt(
        persona,
        product,
        persona_fields,
        config.common.dataset.field_map,
        config.common.persona.system_prompt_path,
    )
    return {
        "persona_id": persona.persona_id,
        "system_prompt": system_prompt,
        "persona_meta": persona_to_payload(persona),
        "questions": list(questions),
        "follow_ups": list(follow_ups),
    }


async def build_persona_prompt(arguments: dict) -> dict:
    """단일 페르소나에 대한 시스템 프롬프트와 페르소나 dict를 돌려준다.

    호스트 sub-agent가 받은 ``system_prompt``를 자기 LLM의 system 메시지로
    그대로 사용해 인터뷰를 수행한다. 응답은 호스트가 record로 모아
    aggregate_results 도구에 전달한다.
    """

    product = arguments.get("product")
    questions = arguments.get("questions")
    if not isinstance(product, str) or not product.strip():
        return error_payload(
            "missing_argument",
            "product(사업 아이템 설명)는 필수입니다",
            exit_code=1,
        )
    if not isinstance(questions, list) or not questions:
        return error_payload(
            "missing_argument",
            "questions(질문 리스트)는 1개 이상 필요합니다",
            exit_code=1,
        )

    follow_ups = arguments.get("follow_ups") or []

    try:
        config = load_config(yaml_path=None, cli_overrides=None)
    except ConfigError as exc:
        return error_payload("config_error", str(exc), exit_code=1)

    setup_logging_for_run(config)
    label = backend_label(config)

    persona_fields_raw = arguments.get("persona_fields")
    persona_fields = (
        tuple(str(f) for f in persona_fields_raw)
        if isinstance(persona_fields_raw, list) and persona_fields_raw
        else config.common.persona.fields
    )

    personas, err = _resolve_personas_for_prompt(arguments, config)
    if err is not None:
        return err
    if not personas:
        return error_payload(
            "filter_matched_zero",
            "페르소나를 찾지 못했습니다. persona_id 또는 filter를 확인해 주세요",
            exit_code=2,
            backend=label,
        )

    questions_list = [str(q) for q in questions]
    follow_ups_list = [str(f) for f in follow_ups]

    try:
        prompt_dict = _build_prompt_for_persona(
            personas[0],
            product,
            questions_list,
            follow_ups_list,
            persona_fields,
            config,
        )
    except ConfigError as exc:
        return error_payload(
            "config_error", str(exc), exit_code=1, backend=label
        )

    return {
        "ok": True,
        "backend": label,
        "system_prompt": prompt_dict["system_prompt"],
        "persona_meta": prompt_dict["persona_meta"],
        "questions": prompt_dict["questions"],
        "follow_ups": prompt_dict["follow_ups"],
        "schema_hint": _SCHEMA_HINT,
    }


async def build_batch_prompts(arguments: dict) -> dict:
    """N명 분의 시스템 프롬프트 + 페르소나 dict를 한 번에 돌려준다.

    호스트 sub-agent가 본 응답을 받아 sub-agent fan-out으로 N개의 인터뷰를
    병렬 실행한다. 결과 record는 호스트가 모아 aggregate_results에 전달한다.
    """

    product = arguments.get("product")
    questions = arguments.get("questions")
    if not isinstance(product, str) or not product.strip():
        return error_payload(
            "missing_argument",
            "product(사업 아이템 설명)는 필수입니다",
            exit_code=1,
        )
    if not isinstance(questions, list) or not questions:
        return error_payload(
            "missing_argument",
            "questions(질문 리스트)는 1개 이상 필요합니다",
            exit_code=1,
        )

    follow_ups = arguments.get("follow_ups") or []
    n = int(arguments.get("n", 0))
    persona_ids_raw = arguments.get("persona_ids") or []
    if not n and not persona_ids_raw:
        return error_payload(
            "missing_argument",
            "n(인원) 또는 persona_ids(uuid 리스트) 중 하나는 필수입니다",
            exit_code=1,
        )
    if n and n < 1:
        return error_payload(
            "invalid_argument",
            f"n은 1 이상이어야 합니다. 입력값: {n}",
            exit_code=1,
        )

    try:
        config = load_config(yaml_path=None, cli_overrides=None)
    except ConfigError as exc:
        return error_payload("config_error", str(exc), exit_code=1)

    setup_logging_for_run(config)
    label = backend_label(config)

    persona_fields_raw = arguments.get("persona_fields")
    persona_fields = (
        tuple(str(f) for f in persona_fields_raw)
        if isinstance(persona_fields_raw, list) and persona_fields_raw
        else config.common.persona.fields
    )

    personas, err = _resolve_personas_for_prompt(arguments, config)
    if err is not None:
        return err
    if not personas:
        return error_payload(
            "filter_matched_zero",
            "페르소나를 찾지 못했습니다. n/filter/persona_ids를 확인해 주세요",
            exit_code=2,
            backend=label,
        )

    questions_list = [str(q) for q in questions]
    follow_ups_list = [str(f) for f in follow_ups]

    try:
        prompts = [
            _build_prompt_for_persona(
                p, product, questions_list, follow_ups_list, persona_fields, config
            )
            for p in personas
        ]
    except ConfigError as exc:
        return error_payload(
            "config_error", str(exc), exit_code=1, backend=label
        )

    return {
        "ok": True,
        "backend": label,
        "count": len(prompts),
        "prompts": prompts,
        "schema_hint": _SCHEMA_HINT,
    }


def _record_from_payload(raw: dict) -> InterviewRecord:
    """호스트가 보낸 record dict를 ``InterviewRecord`` dataclass로 복원한다.

    스키마 검증은 dataclass의 ``__post_init__``이 수행한다. 키 누락이나 타입
    불일치는 그대로 ConfigError/ValueError로 throw해 호출자가 ``aggregate_results``
    의 에러 봉투로 변환하도록 한다.
    """

    persona_raw = raw.get("persona_meta") or {}
    persona = PersonaMeta(
        persona_id=str(persona_raw.get("persona_id", raw.get("persona_id", ""))),
        name=persona_raw.get("name"),
        gender=str(persona_raw.get("gender", "여자")),
        age=int(persona_raw.get("age", 0)),
        region=str(persona_raw.get("region", "")),
        subregion=str(persona_raw.get("subregion", "")),
        occupation=str(persona_raw.get("occupation", "")),
        marital=str(persona_raw.get("marital", "")),
        education=str(persona_raw.get("education", "")),
        raw=dict(persona_raw.get("raw", {})),
        family_type=persona_raw.get("family_type"),
        housing_type=persona_raw.get("housing_type"),
    )

    summary_raw = raw.get("structured_summary")
    summary: Optional[StructuredSummary] = None
    if isinstance(summary_raw, dict):
        price_signal_raw = summary_raw.get("acceptable_price_signal")
        price_signal: Optional[str] = None
        if isinstance(price_signal_raw, str):
            candidate = price_signal_raw.strip().lower()
            if candidate in ("cheap", "fair", "expensive"):
                price_signal = candidate
        summary = StructuredSummary(
            intent=str(summary_raw.get("intent", "neutral")),
            willingness_to_pay=summary_raw.get("willingness_to_pay"),
            willingness_to_pay_currency=str(
                summary_raw.get("willingness_to_pay_currency", "KRW")
            ),
            rejection_reasons=list(summary_raw.get("rejection_reasons", [])),
            one_line=str(summary_raw.get("one_line", "")),
            acceptable_price_signal=price_signal,
        )

    flags_raw = raw.get("flags") or {}
    flags = Flags(
        persona_drift=bool(flags_raw.get("persona_drift", False)),
        auto_follow_up_used=bool(flags_raw.get("auto_follow_up_used", False)),
        refusal_detected=bool(flags_raw.get("refusal_detected", False)),
        truncated=bool(flags_raw.get("truncated", False)),
        parse_failed=bool(flags_raw.get("parse_failed", False)),
    )

    raw_responses = []
    for r in raw.get("raw_responses") or []:
        if not isinstance(r, dict):
            continue
        raw_responses.append(
            RawResponse(
                question_index=int(r.get("question_index", 0)),
                response=str(r.get("response", "")),
                latency_ms=int(r.get("latency_ms", 0)),
                retry_count=int(r.get("retry_count", 0)),
                reasoning_trace=r.get("reasoning_trace"),
            )
        )

    messages = []
    for m in raw.get("messages") or []:
        if not isinstance(m, dict):
            continue
        messages.append(
            MessageEntry(
                role=str(m.get("role", "user")),
                content=str(m.get("content", "")),
            )
        )

    return InterviewRecord(
        persona_id=str(raw.get("persona_id", persona.persona_id)),
        persona_meta=persona,
        started_at=str(raw.get("started_at", "")),
        finished_at=str(raw.get("finished_at", "")),
        status=str(raw.get("status", "completed")),
        messages=messages,
        raw_responses=raw_responses,
        structured_summary=summary,
        flags=flags,
        error=raw.get("error"),
    )


async def aggregate_results(arguments: dict) -> dict:
    """호스트가 모은 record 리스트로 정량 집계 + 마크다운 리포트를 생성한다.

    정성 인사이트는 기본 fallback으로 둔다(server-side LLM 호출 없음). 호스트가
    인사이트까지 받으려면 호스트 sub-agent가 직접 정성 분석을 추가로 수행한다.
    """

    records_raw = arguments.get("records")
    if not isinstance(records_raw, list) or not records_raw:
        return error_payload(
            "missing_argument",
            "records(인터뷰 record 리스트)는 1개 이상 필요합니다",
            exit_code=1,
        )

    product = str(arguments.get("product") or "")
    questions = arguments.get("questions") or []
    questions_list = [str(q) for q in questions if isinstance(q, str)]
    slug = str(arguments.get("slug") or "korea-persona-interview")
    output_dir_raw = arguments.get("output_dir")
    top_n = int(arguments.get("top_n", 10))
    include_drift = bool(arguments.get("include_drift", False))

    if top_n < 1:
        return error_payload(
            "invalid_argument",
            f"top_n은 1 이상이어야 합니다. 입력값: {top_n}",
            exit_code=1,
        )

    try:
        config = load_config(yaml_path=None, cli_overrides=None)
    except ConfigError as exc:
        return error_payload("config_error", str(exc), exit_code=1)

    setup_logging_for_run(config)
    label = backend_label(config)

    # record dict → InterviewRecord 복원. 스키마 위반은 dataclass __post_init__이
    # ValueError로 차단한다.
    try:
        records = [_record_from_payload(r) for r in records_raw if isinstance(r, dict)]
    except (ValueError, ConfigError, TypeError) as exc:
        return error_payload(
            "invalid_argument",
            f"record 스키마 검증 실패: {exc}",
            exit_code=1,
            backend=label,
        )

    if not records:
        return error_payload(
            "empty_valid_records",
            "유효한 record가 없습니다. interview_record_schema 도구로 형식을 확인해 주세요",
            exit_code=2,
            backend=label,
        )

    try:
        _validate_records_for_report(records, include_drift=include_drift)
    except EmptyValidRecordsError as exc:
        return error_payload(
            "empty_valid_records", str(exc), exit_code=2, backend=label
        )

    quant = compute_quant(
        records,
        top_n=top_n,
        include_drift=include_drift,
        cohort_min_cell=config.common.report.cohort_min_cell,
        histogram_bins=config.common.report.histogram_bins,
    )

    # 정성 인사이트는 기본 fallback. server-side LLM 호출이 없는 모드라 호스트가
    # 정성 분석을 따로 만들어 옵션으로 넘기는 흐름을 안내한다.
    insights_override = arguments.get("insights")
    if isinstance(insights_override, dict):
        insights = QualitativeInsights(
            common_reactions=[
                str(s).strip()
                for s in (insights_override.get("common_reactions") or [])
                if str(s).strip()
            ][:5],
            insights=[
                str(s).strip()
                for s in (insights_override.get("insights") or [])
                if str(s).strip()
            ],
            cohort_differences=str(insights_override.get("cohort_differences") or "").strip(),
            fallback_message=str(insights_override.get("fallback_message") or "").strip(),
        )
    else:
        insights = QualitativeInsights(
            fallback_message=(
                "MCP orchestrator 모드에서는 server-side LLM 호출 없이 정량 지표만 채웁니다. "
                "정성 인사이트는 호스트 sub-agent가 별도 호출로 생성해 aggregate_results 호출 시 "
                "insights 인자로 전달해 주세요"
            )
        )

    # output_path 결정. orchestrator 모드는 record 기반이라 별도 입력 JSON이 없을
    # 수 있어, json_path로 timestamp 기반 가짜 경로를 만들어 _resolve_output_path
    # 가 report_*.md 명을 생성하도록 한다.
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    pseudo_input = Path(str(output_dir_raw or "outputs/")) / f"interview_{slug}_{ts}.json"
    output_path = _resolve_output_path(
        pseudo_input,
        Path(str(output_dir_raw)) if output_dir_raw else None,
    )

    meta_for_render = {
        "product": product,
        "model": "(host sub-agent)",
        "seed": "-",
        "started_at": "",
    }
    summary_dict = _records_summary({}, records)

    markdown_text = render_markdown(
        quant=quant,
        insights=insights,
        meta=meta_for_render,
        records_summary=summary_dict,
        json_path=pseudo_input,
        include_drift=include_drift,
        top_n=top_n,
        usage_summary=None,
        bar_width=config.common.report.bar_width,
        cohort_min_cell=config.common.report.cohort_min_cell,
    )

    output_path.write_text(markdown_text, encoding="utf-8")
    try:
        import os as _os

        _os.chmod(output_path, 0o600)
    except (PermissionError, OSError):
        pass

    logger.info(
        "MCP orchestrator aggregate_results 리포트 저장",
        extra={
            "output_md": str(output_path),
            "valid_records": quant.valid_records,
            "include_drift": include_drift,
        },
    )

    # 토큰 사용량은 호스트가 자기 LLM에서 수집해야 한다. server-side에서는 알 수
    # 없으므로 0으로 채워 돌려준다(필요 시 호스트가 응답에 추가 메타로 박는다).
    usage_total = TokenUsage()

    return {
        "ok": True,
        "backend": label,
        "output_path": str(output_path),
        "summary": summary_dict,
        "valid_records": quant.valid_records,
        "excluded_total": quant.excluded_total,
        "usage_total": {
            "prompt_tokens": usage_total.prompt_tokens,
            "completion_tokens": usage_total.completion_tokens,
            "total_tokens": usage_total.total_tokens,
            "cached_tokens": usage_total.cached_tokens,
        },
    }
