"""모든 MCP mode 공통 helper 도구.

CLI와 MCP server 모드는 휴리스틱(`detect_persona_drift`, `should_auto_follow_up`, `_parse_summary_payload`)이 자동 적용되지만 MCP orchestrator 모드는 호스트 sub-agent가 인터뷰를 수행하므로 호스트가 동일 임계값/키워드로 판정 가능하도록 본 휴리스틱을 도구로 노출한다.

본 모듈의 도구는 server-side LLM을 호출하지 않는다(순수 Python 함수). 따라서 MCP server / MCP orchestrator 어느 모드에서나 동일한 응답을 돌려준다.
"""

from __future__ import annotations

import logging
from typing import Optional

from .._json_utils import extract_json_object
from ..config import load_config
from ..interview import detect_persona_drift, should_auto_follow_up
from ..models import (
    ALLOWED_GENDER,
    ALLOWED_INTENT,
    ALLOWED_PRICE_SIGNAL,
    ALLOWED_ROLE,
    ALLOWED_STATUS,
    ConfigError,
    PersonaMeta,
)
from ._payloads import error_payload
from ._setup import backend_label


logger = logging.getLogger(__name__)


def _persona_from_payload(persona_meta: dict) -> PersonaMeta:
    """``persona_meta`` dict를 ``PersonaMeta`` dataclass로 복원한다.

    detect_persona_drift 도구가 호스트로부터 받은 페르소나 dict를 dataclass로 되돌릴 때 사용한다. 키가 모자라면 ConfigError로 차단한다.
    """

    required = {"persona_id", "gender", "age", "region", "subregion", "occupation", "marital", "education"}
    missing = required - set(persona_meta.keys())
    if missing:
        raise ConfigError(
            f"persona_meta dict에 필수 키가 누락되었습니다: {sorted(missing)}"
        )

    return PersonaMeta(
        persona_id=str(persona_meta["persona_id"]),
        name=persona_meta.get("name"),
        gender=str(persona_meta["gender"]),
        age=int(persona_meta["age"]),
        region=str(persona_meta["region"]),
        subregion=str(persona_meta["subregion"]),
        occupation=str(persona_meta["occupation"]),
        marital=str(persona_meta["marital"]),
        education=str(persona_meta["education"]),
        raw=dict(persona_meta.get("raw") or {}),
        family_type=persona_meta.get("family_type"),
        housing_type=persona_meta.get("housing_type"),
    )


async def detect_persona_drift_tool(arguments: dict) -> dict:
    """페르소나 깨짐 휴리스틱을 호스트가 명시 호출할 수 있도록 노출한다.

    입력으로 ``text``(인터뷰 응답)와 ``persona_meta``(dict)를 받아 4축 정밀 정규식 + 영어 비율 임계값 기반의 drift 여부를 boolean으로 돌려준다. 임계값과 화이트리스트는 ``heuristics.*`` yaml 값을 따른다.
    """

    text = arguments.get("text")
    persona_raw = arguments.get("persona_meta")
    if not isinstance(text, str):
        return error_payload(
            "missing_argument",
            "text(인터뷰 응답 본문)는 str이어야 합니다",
            exit_code=1,
        )
    if not isinstance(persona_raw, dict):
        return error_payload(
            "missing_argument",
            "persona_meta(페르소나 dict)는 필수입니다",
            exit_code=1,
        )

    try:
        config = load_config(yaml_path=None, cli_overrides=None)
    except ConfigError as exc:
        return error_payload("config_error", str(exc), exit_code=1)

    label = backend_label(config)

    try:
        persona = _persona_from_payload(persona_raw)
    except (ConfigError, ValueError) as exc:
        return error_payload(
            "invalid_argument", str(exc), exit_code=1, backend=label
        )

    is_drift = detect_persona_drift(
        text,
        persona,
        english_ratio_threshold=config.heuristics.english_ratio_threshold,
        occupation_english_whitelist=config.heuristics.occupation_english_whitelist,
    )

    return {
        "ok": True,
        "backend": label,
        "is_drift": bool(is_drift),
        "thresholds": {
            "english_ratio_threshold": config.heuristics.english_ratio_threshold,
            "occupation_english_whitelist": config.heuristics.occupation_english_whitelist,
        },
    }


async def should_auto_follow_up_tool(arguments: dict) -> dict:
    """짧은 답변/모호 키워드 매칭으로 자동 follow-up 트리거 여부를 돌려준다.

    호스트가 인터뷰 답변을 받은 직후 본 도구로 짧은 답변/모호 답변을 감지해
    follow-up을 호스트 LLM 흐름에서 직접 트리거할 수 있게 한다.
    """

    text = arguments.get("text")
    if not isinstance(text, str):
        return error_payload(
            "missing_argument",
            "text(답변 본문)는 str이어야 합니다",
            exit_code=1,
        )

    threshold_override = arguments.get("threshold")
    keywords_override = arguments.get("ambiguous_keywords")

    try:
        config = load_config(yaml_path=None, cli_overrides=None)
    except ConfigError as exc:
        return error_payload("config_error", str(exc), exit_code=1)

    label = backend_label(config)

    threshold = (
        int(threshold_override)
        if threshold_override is not None
        else config.heuristics.short_answer_threshold
    )
    keywords = (
        tuple(str(k) for k in keywords_override)
        if isinstance(keywords_override, list)
        else config.heuristics.ambiguous_keywords
    )

    triggered = should_auto_follow_up(text, threshold=threshold, ambiguous_keywords=keywords)

    return {
        "ok": True,
        "backend": label,
        "should_follow_up": bool(triggered),
        "threshold": int(threshold),
        "ambiguous_keywords": list(keywords),
        "auto_follow_up_text": config.heuristics.auto_follow_up_text,
    }


async def parse_structured_summary_tool(arguments: dict) -> dict:
    """LLM의 구조화 요약 응답 텍스트를 ``StructuredSummary`` dict로 파싱한다.

    호스트 sub-agent가 single-turn으로 받은 JSON 응답을 본 도구로 파싱해
    같은 정규화 결과(intent/willingness_to_pay/acceptable_price_signal/...)를
    얻는다. 코드 펜스/주변 텍스트가 섞여도 ``extract_json_object``가 가장 바깥
    JSON 객체만 골라낸다.
    """

    raw = arguments.get("raw_response")
    if not isinstance(raw, str):
        return error_payload(
            "missing_argument",
            "raw_response(LLM 응답 본문)는 str이어야 합니다",
            exit_code=1,
        )

    try:
        config = load_config(yaml_path=None, cli_overrides=None)
    except ConfigError as exc:
        return error_payload("config_error", str(exc), exit_code=1)

    label = backend_label(config)
    data = extract_json_object(raw)
    if data is None:
        return {
            "ok": True,
            "backend": label,
            "structured_summary": None,
            "parse_failed": True,
        }

    intent_raw = str(data.get("intent", "neutral")).strip().lower()
    if intent_raw not in ALLOWED_INTENT:
        return {
            "ok": True,
            "backend": label,
            "structured_summary": None,
            "parse_failed": True,
        }

    wtp_raw = data.get("willingness_to_pay")
    wtp: Optional[int]
    try:
        wtp = int(wtp_raw) if wtp_raw is not None else None
    except (TypeError, ValueError):
        wtp = None

    price_signal_raw = data.get("acceptable_price_signal")
    price_signal: Optional[str] = None
    if isinstance(price_signal_raw, str):
        candidate = price_signal_raw.strip().lower()
        if candidate in ALLOWED_PRICE_SIGNAL:
            price_signal = candidate

    rejection_reasons_raw = data.get("rejection_reasons") or []
    rejection_reasons: list = []
    if isinstance(rejection_reasons_raw, list):
        for r in rejection_reasons_raw:
            text = str(r).strip()
            if text:
                rejection_reasons.append(text)

    one_line = str(data.get("one_line", "")).strip()
    currency = str(data.get("willingness_to_pay_currency", "KRW")).strip() or "KRW"

    return {
        "ok": True,
        "backend": label,
        "parse_failed": False,
        "structured_summary": {
            "intent": intent_raw,
            "willingness_to_pay": wtp,
            "willingness_to_pay_currency": currency,
            "rejection_reasons": rejection_reasons,
            "one_line": one_line,
            "acceptable_price_signal": price_signal,
        },
    }


async def interview_record_schema_tool(arguments: dict) -> dict:
    """호스트가 record dict를 만들 때 참조할 schema 가이드를 돌려준다.

    aggregate_results 도구의 입력 형태를 호스트가 정확히 맞출 수 있도록
    필드 이름, 허용 enum, 한 record 예시를 노출한다. ``ALLOWED_*`` 도메인
    상수와 모듈 docstring을 정본으로 박는다.
    """

    try:
        config = load_config(yaml_path=None, cli_overrides=None)
    except ConfigError as exc:
        return error_payload("config_error", str(exc), exit_code=1)

    label = backend_label(config)

    schema = {
        "record": {
            "persona_id": "str (PersonaMeta.persona_id와 동일)",
            "persona_meta": "object (list_personas/build_persona_prompt 응답의 페르소나 dict)",
            "started_at": "str (ISO-8601 UTC, 예: 2026-05-02T12:00:00+00:00)",
            "finished_at": "str (ISO-8601 UTC)",
            "status": f"enum: {sorted(ALLOWED_STATUS)}",
            "messages": "array of {role, content} (role enum: " + str(sorted(ALLOWED_ROLE)) + ")",
            "raw_responses": "array of {question_index:int, response:str, latency_ms:int, retry_count:int}",
            "structured_summary": "object or null (parse_structured_summary 응답의 structured_summary)",
            "flags": "object {persona_drift, auto_follow_up_used, refusal_detected, truncated, parse_failed}",
            "error": "object or null",
        },
        "enums": {
            "status": sorted(ALLOWED_STATUS),
            "intent": sorted(ALLOWED_INTENT),
            "gender": sorted(ALLOWED_GENDER),
            "role": sorted(ALLOWED_ROLE),
            "acceptable_price_signal": sorted(ALLOWED_PRICE_SIGNAL),
        },
    }

    example = {
        "persona_id": "p-0001",
        "persona_meta": {
            "persona_id": "p-0001",
            "name": None,
            "gender": "여자",
            "age": 27,
            "region": "서울",
            "subregion": "서울-강남구",
            "occupation": "소프트웨어 엔지니어",
            "marital": "미혼",
            "education": "대학교",
            "family_type": "1인 가구",
            "housing_type": "원룸",
        },
        "started_at": "2026-05-02T12:00:00+00:00",
        "finished_at": "2026-05-02T12:00:30+00:00",
        "status": "completed",
        "messages": [
            {"role": "system", "content": "(시스템 프롬프트)"},
            {"role": "user", "content": "이 서비스 쓸 의향?"},
            {"role": "assistant", "content": "네, 가격이 합리적이라면 한 번 써보고 싶어요."},
        ],
        "raw_responses": [
            {"question_index": 0, "response": "네, 가격이 합리적이라면 한 번 써보고 싶어요.", "latency_ms": 1200, "retry_count": 0}
        ],
        "structured_summary": {
            "intent": "positive",
            "willingness_to_pay": 30000,
            "willingness_to_pay_currency": "KRW",
            "rejection_reasons": [],
            "one_line": "가격 합리적이면 시도 의향",
            "acceptable_price_signal": "fair",
        },
        "flags": {
            "persona_drift": False,
            "auto_follow_up_used": False,
            "refusal_detected": False,
            "truncated": False,
            "parse_failed": False,
        },
        "error": None,
    }

    return {
        "ok": True,
        "backend": label,
        "schema": schema,
        "example": example,
    }
