"""도메인 모델 dataclass와 예외 9종 단위 테스트.

검증 대상은 아래와 같다.

- ``PersonaMeta``의 ``gender``/``age`` 검증과 ``frozen`` 속성
- ``MessageEntry``의 ``role`` 검증
- ``StructuredSummary``의 ``intent``/``willingness_to_pay`` 검증
- ``InterviewRecord``의 ``status`` 검증
- 사용자 노출 예외 4종과 내부 예외 5종의 instance 생성 가능성
- ``dataclasses.asdict`` 직렬화 가능성
"""

from __future__ import annotations

import dataclasses

import pytest

from src.models import (
    ALLOWED_GENDER,
    ALLOWED_INTENT,
    ALLOWED_ROLE,
    ALLOWED_STATUS,
    SCHEMA_VERSION,
    BatchResult,
    ChatResponse,
    ConfigError,
    DatasetUnavailableError,
    EmptyResponseError,
    FilterMatchedZeroError,
    Flags,
    InterviewRecord,
    MessageEntry,
    ModelRefusedError,
    PersonaBreakError,
    PersonaMeta,
    RawResponse,
    ResponseTooShortError,
    RetryExhaustedError,
    RunMeta,
    ServerNotReachableError,
    StructuredSummary,
    StructuredSummaryParseError,
)


# ---------------------------------------------------------------------------
# PersonaMeta
# ---------------------------------------------------------------------------


def _make_persona(**overrides) -> PersonaMeta:
    base = dict(
        persona_id="p-0001",
        name=None,
        gender="여자",
        age=27,
        region="서울",
        subregion="서울-강남구",
        occupation="개발자",
        marital="미혼",
        education="대학교",
        raw={},
    )
    base.update(overrides)
    return PersonaMeta(**base)


def test_persona_meta_정상값_생성_성공() -> None:
    persona = _make_persona()
    assert persona.persona_id == "p-0001"
    assert persona.gender == "여자"
    assert persona.age == 27


def test_persona_meta_gender_허용외_값_ValueError() -> None:
    with pytest.raises(ValueError):
        _make_persona(gender="X")


def test_persona_meta_gender_None_ValueError() -> None:
    with pytest.raises(ValueError):
        _make_persona(gender=None)


def test_persona_meta_age_음수_ValueError() -> None:
    with pytest.raises(ValueError):
        _make_persona(age=-1)


def test_persona_meta_age_int_아님_ValueError() -> None:
    with pytest.raises(ValueError):
        _make_persona(age="27")


def test_persona_meta_frozen_setattr_불가() -> None:
    persona = _make_persona()
    with pytest.raises(dataclasses.FrozenInstanceError):
        persona.age = 30  # type: ignore[misc]


def test_persona_meta_asdict_직렬화_가능() -> None:
    persona = _make_persona(name="홍길동")
    d = dataclasses.asdict(persona)
    assert d["persona_id"] == "p-0001"
    assert d["name"] == "홍길동"
    assert d["raw"] == {}


# ---------------------------------------------------------------------------
# MessageEntry
# ---------------------------------------------------------------------------


def test_message_entry_role_허용외_ValueError() -> None:
    with pytest.raises(ValueError):
        MessageEntry(role="bot", content="안녕")


def test_message_entry_정상값_생성_성공() -> None:
    m = MessageEntry(role="user", content="안녕")
    assert m.role == "user"
    assert m.content == "안녕"


# ---------------------------------------------------------------------------
# StructuredSummary
# ---------------------------------------------------------------------------


def test_structured_summary_intent_허용외_ValueError() -> None:
    with pytest.raises(ValueError):
        StructuredSummary(
            intent="strong-positive",
            willingness_to_pay=10000,
            willingness_to_pay_currency="KRW",
            rejection_reasons=[],
            one_line="요약",
        )


def test_structured_summary_정상값_생성_성공() -> None:
    s = StructuredSummary(
        intent="positive",
        willingness_to_pay=39900,
        willingness_to_pay_currency="KRW",
        rejection_reasons=["가격"],
        one_line="가격이 합리적이면 의향 있음",
    )
    assert s.intent == "positive"
    assert s.willingness_to_pay == 39900


def test_structured_summary_willingness_to_pay_None_허용() -> None:
    s = StructuredSummary(
        intent="negative",
        willingness_to_pay=None,
        willingness_to_pay_currency="KRW",
        rejection_reasons=["가격 부담"],
        one_line="거절",
    )
    assert s.willingness_to_pay is None


def test_structured_summary_willingness_to_pay_음수_ValueError() -> None:
    with pytest.raises(ValueError):
        StructuredSummary(
            intent="positive",
            willingness_to_pay=-100,
            willingness_to_pay_currency="KRW",
            rejection_reasons=[],
            one_line="음수",
        )


# ---------------------------------------------------------------------------
# InterviewRecord
# ---------------------------------------------------------------------------


def _make_record(**overrides) -> InterviewRecord:
    persona = _make_persona()
    base = dict(
        persona_id="p-0001",
        persona_meta=persona,
        started_at="2026-05-02T00:00:00+00:00",
        finished_at="2026-05-02T00:00:10+00:00",
        status="completed",
        messages=[],
        raw_responses=[],
        structured_summary=None,
        flags=Flags(),
        error=None,
    )
    base.update(overrides)
    return InterviewRecord(**base)


def test_interview_record_status_허용외_ValueError() -> None:
    with pytest.raises(ValueError):
        _make_record(status="unknown")


def test_interview_record_허용된_status_생성_성공() -> None:
    for s in ("completed", "refused", "failed", "drift"):
        rec = _make_record(status=s)
        assert rec.status == s


# ---------------------------------------------------------------------------
# Flags / ChatResponse / RawResponse / RunMeta / BatchResult
# ---------------------------------------------------------------------------


def test_flags_기본값_모두_False() -> None:
    flags = Flags()
    assert flags.persona_drift is False
    assert flags.auto_follow_up_used is False
    assert flags.refusal_detected is False
    assert flags.truncated is False


def test_chat_response_기본_생성_성공() -> None:
    cr = ChatResponse(content="응답", latency_ms=120, retry_count=0)
    assert cr.content == "응답"
    assert cr.reasoning_trace is None


def test_raw_response_기본_생성_성공() -> None:
    rr = RawResponse(question_index=0, response="네", latency_ms=10, retry_count=0)
    assert rr.question_index == 0


def test_run_meta_schema_version_상수_일치() -> None:
    meta = RunMeta(
        interview_id="x",
        slug="korea-persona-interview",
        schema_version=SCHEMA_VERSION,
        product="제품",
        questions=["Q1"],
        follow_up_questions=[],
        model="m",
        seed=42,
        started_at="t1",
        finished_at="t2",
        config_snapshot={},
    )
    assert meta.schema_version == 1
    assert SCHEMA_VERSION == 1


def test_batch_result_asdict_직렬화_가능() -> None:
    meta = RunMeta(
        interview_id="x",
        slug="korea-persona-interview",
        schema_version=SCHEMA_VERSION,
        product="제품",
        questions=["Q1"],
        follow_up_questions=[],
        model="m",
        seed=42,
        started_at="t1",
        finished_at="t2",
        config_snapshot={},
    )
    record = _make_record()
    result = BatchResult(meta=meta, records=[record])
    payload = dataclasses.asdict(result)
    assert payload["meta"]["slug"] == "korea-persona-interview"
    assert len(payload["records"]) == 1


# ---------------------------------------------------------------------------
# 화이트리스트 상수
# ---------------------------------------------------------------------------


def test_화이트리스트_상수_정합성() -> None:
    assert "completed" in ALLOWED_STATUS
    assert "drift" in ALLOWED_STATUS
    assert "positive" in ALLOWED_INTENT
    assert "남자" in ALLOWED_GENDER and "여자" in ALLOWED_GENDER
    assert "system" in ALLOWED_ROLE


# ---------------------------------------------------------------------------
# 예외 9종 + EmptyResponseError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls",
    [
        ConfigError,
        ServerNotReachableError,
        DatasetUnavailableError,
        FilterMatchedZeroError,
        PersonaBreakError,
        ResponseTooShortError,
        ModelRefusedError,
        RetryExhaustedError,
        StructuredSummaryParseError,
        EmptyResponseError,
    ],
)
def test_도메인_예외_생성과_raise_가능(exc_cls: type) -> None:
    instance = exc_cls("메시지")
    assert isinstance(instance, Exception)
    with pytest.raises(exc_cls):
        raise instance


def test_사용자_노출_예외_분류() -> None:
    """UI §3 기준 사용자 노출 4종과 내부 5종 분리.

    분류 자체는 코드 주석/주석에 의존하므로 본 테스트는 클래스 존재만 확인한다.
    """

    user_facing = (
        ConfigError,
        ServerNotReachableError,
        DatasetUnavailableError,
        FilterMatchedZeroError,
    )
    internal = (
        PersonaBreakError,
        ResponseTooShortError,
        ModelRefusedError,
        RetryExhaustedError,
        StructuredSummaryParseError,
    )
    for cls in user_facing + internal:
        assert issubclass(cls, Exception)
