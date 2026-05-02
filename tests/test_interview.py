"""``src.interview`` 단위/통합 테스트.

순수 함수와 InterviewSession.run을 모두 검증한다.

- ``build_system_prompt``: 페르소나 JSON 주입, product 포함, 토글 페르소나 추가
- ``estimate_tokens``: 한글/영문/혼합 휴리스틱
- ``truncate_history``: 토큰 초과 시 페어 단위 제거, system 보존, truncated 플래그
- ``should_auto_follow_up``: 짧은 답변/모호 키워드/정상 답변 분기
- ``detect_persona_drift``: 영어 비율, 연령/성별/지역 모순
- ``detect_refusal``: 거부 키워드 부분 매칭
- ``_parse_summary_payload``: JSON, 코드 펜스, 자유 서술 + JSON 혼합, 잘못된 intent
- ``InterviewSession.run``: mock LLM으로 멀티턴 흐름, follow-up, drift, refusal
- ``run_interview`` 함수형 진입점
"""

from __future__ import annotations

import json

import pytest

from src.interview import (
    AUTO_FOLLOW_UP_PROMPT,
    InterviewSession,
    _build_summary_messages,
    _parse_summary_payload,
    build_system_prompt,
    detect_persona_drift,
    detect_refusal,
    estimate_messages_tokens,
    estimate_tokens,
    run_interview,
    should_auto_follow_up,
    summarize_interview,
    truncate_history,
)
from src.llm_client import MlxLLMClient
from src.models import (
    Flags,
    InterviewRecord,
    MessageEntry,
    PersonaMeta,
    StructuredSummary,
    StructuredSummaryParseError,
)


_FIELD_MAP = {
    "name": None,
    "summary": "persona",
    "professional": "professional_persona",
    "sports": "sports_persona",
    "arts": "arts_persona",
    "travel": "travel_persona",
    "culinary": "culinary_persona",
    "family": "family_persona",
}


# ---------------------------------------------------------------------------
# build_system_prompt
# ---------------------------------------------------------------------------


def test_build_system_prompt_페르소나_JSON_주입(fake_persona_meta) -> None:
    prompt = build_system_prompt(
        fake_persona_meta,
        product="반찬 정기배송",
        persona_fields=("summary",),
        field_map=_FIELD_MAP,
    )
    assert "당신은 다음 한국인 인물" in prompt
    assert "반찬 정기배송" in prompt
    # 인구 통계 필드가 JSON 안에 들어 있다
    assert fake_persona_meta.gender in prompt
    assert str(fake_persona_meta.age) in prompt
    assert fake_persona_meta.region in prompt


def test_build_system_prompt_summary_기본_주입(fake_persona_meta) -> None:
    prompt = build_system_prompt(
        fake_persona_meta,
        product="반찬",
        persona_fields=("summary",),
        field_map=_FIELD_MAP,
    )
    # raw["persona"]가 시스템 프롬프트 본문에 포함된다
    assert fake_persona_meta.raw["persona"] in prompt


def test_build_system_prompt_토글_페르소나_추가(fake_persona_meta) -> None:
    prompt = build_system_prompt(
        fake_persona_meta,
        product="반찬",
        persona_fields=("summary", "professional", "family"),
        field_map=_FIELD_MAP,
    )
    assert fake_persona_meta.raw["professional_persona"] in prompt
    assert fake_persona_meta.raw["family_persona"] in prompt


def test_build_system_prompt_토글_OFF_미주입(fake_persona_meta) -> None:
    prompt = build_system_prompt(
        fake_persona_meta,
        product="반찬",
        persona_fields=("summary",),
        field_map=_FIELD_MAP,
    )
    # 토글 OFF면 sports_persona는 prompt에 등장하지 않는다
    assert fake_persona_meta.raw["sports_persona"] not in prompt


# ---------------------------------------------------------------------------
# estimate_tokens / estimate_messages_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_빈_문자열_0() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_한글_1자_1토큰() -> None:
    """한글 1글자는 1토큰으로 카운트된다."""

    assert estimate_tokens("가") == 1
    assert estimate_tokens("안녕하세요") == 5


def test_estimate_tokens_영문_0_25_토큰() -> None:
    # "abcd" 4자 = 1토큰 + 1(올림 남는 분수)
    assert estimate_tokens("abcd") == 1


def test_estimate_tokens_혼합_텍스트() -> None:
    text = "안녕 hello"  # 한글 2 + 공백 0.5 + 영문 5 * 0.25 = 2.5 + 1.25 = 3.75 → 4
    assert estimate_tokens(text) == 4


def test_estimate_messages_tokens_role_오버헤드_가산() -> None:
    msgs = [MessageEntry(role="system", content="안녕")]
    # "안녕" 2 + role 오버헤드 4 = 6
    assert estimate_messages_tokens(msgs) == 6


def test_estimate_messages_tokens_dict_지원() -> None:
    msgs = [{"role": "user", "content": "test"}]
    # "test" 4 * 0.25 = 1 + 4 = 5
    assert estimate_messages_tokens(msgs) == 5


# ---------------------------------------------------------------------------
# truncate_history
# ---------------------------------------------------------------------------


def test_truncate_history_빈_messages_그대로() -> None:
    out, was = truncate_history([], max_tokens=100)
    assert out == []
    assert was is False


def test_truncate_history_예산_안_그대로() -> None:
    msgs = [
        MessageEntry(role="system", content="시스템"),
        MessageEntry(role="user", content="질문"),
        MessageEntry(role="assistant", content="답변"),
    ]
    out, was = truncate_history(msgs, max_tokens=10000)
    assert was is False
    assert len(out) == 3


def test_truncate_history_예산_초과시_가장_오래된_페어_제거() -> None:
    long_text = "가" * 5000  # 5000 토큰. 한 페어로 약 10,008 토큰
    msgs = [
        MessageEntry(role="system", content="짧은 시스템"),
        MessageEntry(role="user", content=long_text),
        MessageEntry(role="assistant", content=long_text),
        MessageEntry(role="user", content="새 질문"),
        MessageEntry(role="assistant", content="새 답변"),
    ]
    out, was = truncate_history(msgs, max_tokens=8000)
    assert was is True
    # system은 보존
    assert out[0].role == "system"
    # 가장 오래된 페어가 제거되어 새 질문/답변만 남아야 함
    contents = [m.content for m in out]
    assert "새 질문" in contents
    assert "새 답변" in contents
    assert long_text not in contents


def test_truncate_history_system_보존_절대조건() -> None:
    """system messages[0]은 어떤 경우에도 truncate되지 않는다."""

    msgs = [
        MessageEntry(role="system", content="필수 시스템"),
        MessageEntry(role="user", content="가" * 100000),
        MessageEntry(role="assistant", content="가" * 100000),
    ]
    out, was = truncate_history(msgs, max_tokens=100)
    # system은 1개 남아 있어야 한다
    system_messages = [m for m in out if m.role == "system"]
    assert len(system_messages) == 1
    assert system_messages[0].content == "필수 시스템"


# ---------------------------------------------------------------------------
# should_auto_follow_up
# ---------------------------------------------------------------------------


def test_should_auto_follow_up_빈_답변_True() -> None:
    assert should_auto_follow_up("") is True


def test_should_auto_follow_up_짧은_답변_True() -> None:
    # 공백 제거 후 20자 미만
    assert should_auto_follow_up("그래요", threshold=20) is True


def test_should_auto_follow_up_긴_답변_False() -> None:
    long = "가격이 합리적이라 한번 시도해 볼 만한 것 같아요. 최근에 비슷한 서비스를 찾고 있었거든요."
    assert should_auto_follow_up(long, threshold=20) is False


def test_should_auto_follow_up_모호_키워드_True() -> None:
    long_with_kw = "글쎄요. 좀 더 생각해 봐야 할 것 같아요. 솔직히 잘 와닿지 않네요. 가격이 좀 부담입니다."
    assert (
        should_auto_follow_up(
            long_with_kw,
            threshold=20,
            ambiguous_keywords=("글쎄요",),
        )
        is True
    )


def test_should_auto_follow_up_딱히_키워드() -> None:
    text = "딱히 매력적인지 모르겠어요. 지금은 안 사겠습니다. 더 봐야 알 듯하네요."
    assert (
        should_auto_follow_up(text, threshold=20, ambiguous_keywords=("딱히",)) is True
    )


# ---------------------------------------------------------------------------
# detect_persona_drift
# ---------------------------------------------------------------------------


def _persona(age: int = 70, gender: str = "여자", region: str = "서울") -> PersonaMeta:
    return PersonaMeta(
        persona_id="p",
        name=None,
        gender=gender,
        age=age,
        region=region,
        subregion=f"{region}-X",
        occupation="x",
        marital="x",
        education="x",
        raw={},
    )


def test_detect_persona_drift_빈_답변_False() -> None:
    assert detect_persona_drift("", _persona()) is False


def test_detect_persona_drift_정상_답변_False() -> None:
    text = "가격이 합리적이라면 한 번 시도해 볼 만하다고 생각합니다."
    assert detect_persona_drift(text, _persona(age=70)) is False


def test_detect_persona_drift_영어_비율_초과_True() -> None:
    text = "I really like this product because it solves my daily inconvenience completely."
    assert detect_persona_drift(text, _persona()) is True


def test_detect_persona_drift_연령_모순_True() -> None:
    text = "저는 20대 학생이라서 구매할 여유가 없어요."
    assert detect_persona_drift(text, _persona(age=70)) is True


def test_detect_persona_drift_성별_모순_여자_True() -> None:
    text = "저는 남자라서 그런 거 별로 안 좋아해요."
    assert detect_persona_drift(text, _persona(gender="여자")) is True


def test_detect_persona_drift_성별_모순_남자_True() -> None:
    text = "저는 여자라서 보통 화장품에 관심이 많아요."
    assert detect_persona_drift(text, _persona(gender="남자")) is True


def test_detect_persona_drift_지역_모순_True() -> None:
    text = "저는 부산 사람이라 잘 모릅니다."
    assert detect_persona_drift(text, _persona(region="서울")) is True


def test_detect_persona_drift_자기_시도_단언_False() -> None:
    text = "저는 서울 사람이라 자주 이용합니다."
    assert detect_persona_drift(text, _persona(region="서울")) is False


def test_detect_persona_drift_영어_단어_비율_단어단위_False() -> None:
    """단어 단위 비율 회귀: 한 단어만 영어이면 비율은 1/N(임계 30% 미만)."""

    text = "가격이 합리적이고 brand 인지도도 어느 정도 있어 시도해 볼 만하다고 생각합니다."
    assert detect_persona_drift(text, _persona()) is False


def test_detect_persona_drift_영어_단어_비율_단어단위_True() -> None:
    """단어 단위 비율 회귀: 영어 단어가 다수면 임계값을 넘어 True."""

    text = "I think this product is good because it really helps me daily and so on."
    assert detect_persona_drift(text, _persona()) is True


def test_truncate_history_O_n_재계산_없이_동일결과() -> None:
    """O(n²) → O(n) 회귀: 페어 제거 시 전체 토큰을 재계산하지 않아도 동일 결과를 낸다.

    동일한 입력에 대해 함수가 결정적이라는 사실을 100회 반복 호출로 단언한다.
    토큰 합 누적 차감 로직이 깨지면 truncate가 멈추거나 과하게 잘릴 수 있다.
    """

    long_text = "가" * 5000
    msgs = [
        MessageEntry(role="system", content="시스템"),
        MessageEntry(role="user", content=long_text),
        MessageEntry(role="assistant", content=long_text),
        MessageEntry(role="user", content=long_text),
        MessageEntry(role="assistant", content=long_text),
        MessageEntry(role="user", content="새 질문"),
        MessageEntry(role="assistant", content="새 답변"),
    ]
    first_out, first_was = truncate_history(msgs, max_tokens=8000)
    for _ in range(100):
        out, was = truncate_history(msgs, max_tokens=8000)
        assert was is first_was
        assert [m.content for m in out] == [m.content for m in first_out]


# ---------------------------------------------------------------------------
# detect_refusal
# ---------------------------------------------------------------------------


_REFUSAL_KEYWORDS = (
    "답변할 수 없습니다",
    "I cannot",
    "I'm sorry, but",
    "저는 인공지능",
    "AI 모델",
)


def test_detect_refusal_빈_답변_False() -> None:
    assert detect_refusal("", _REFUSAL_KEYWORDS) is False


def test_detect_refusal_한국어_키워드_True() -> None:
    assert detect_refusal("죄송합니다, 답변할 수 없습니다.", _REFUSAL_KEYWORDS) is True


def test_detect_refusal_영어_키워드_True() -> None:
    assert detect_refusal("I cannot provide that information.", _REFUSAL_KEYWORDS) is True


def test_detect_refusal_AI_정체성_노출_True() -> None:
    assert detect_refusal("저는 인공지능 모델이라 그런 답변은...", _REFUSAL_KEYWORDS) is True


def test_detect_refusal_정상_답변_False() -> None:
    assert (
        detect_refusal(
            "가격이 적당하면 충분히 시도해 볼 만하다고 생각합니다.",
            _REFUSAL_KEYWORDS,
        )
        is False
    )


def test_detect_refusal_빈_키워드_리스트_False() -> None:
    assert detect_refusal("아무 답변", ()) is False


# ---------------------------------------------------------------------------
# _parse_summary_payload
# ---------------------------------------------------------------------------


def test_parse_summary_payload_정상_JSON() -> None:
    text = json.dumps(
        {
            "intent": "positive",
            "willingness_to_pay": 39900,
            "willingness_to_pay_currency": "KRW",
            "rejection_reasons": ["가격"],
            "one_line": "가격 합리적이면 의향",
        },
        ensure_ascii=False,
    )
    summary = _parse_summary_payload(text)
    assert isinstance(summary, StructuredSummary)
    assert summary.intent == "positive"
    assert summary.willingness_to_pay == 39900


def test_parse_summary_payload_코드_펜스_제거() -> None:
    text = (
        "```json\n"
        + json.dumps(
            {
                "intent": "neutral",
                "willingness_to_pay": None,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": [],
                "one_line": "글쎄요",
            },
            ensure_ascii=False,
        )
        + "\n```"
    )
    summary = _parse_summary_payload(text)
    assert summary.intent == "neutral"
    assert summary.willingness_to_pay is None


def test_parse_summary_payload_자유_서술_혼합_JSON_추출() -> None:
    text = (
        "분석 결과를 아래 JSON으로 제공합니다.\n"
        + json.dumps(
            {
                "intent": "negative",
                "willingness_to_pay": None,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": ["가격 부담"],
                "one_line": "거절",
            },
            ensure_ascii=False,
        )
        + "\n그 외 추가 코멘트는 없습니다."
    )
    summary = _parse_summary_payload(text)
    assert summary.intent == "negative"
    assert "가격 부담" in summary.rejection_reasons


def test_parse_summary_payload_빈_본문_StructuredSummaryParseError() -> None:
    with pytest.raises(StructuredSummaryParseError):
        _parse_summary_payload("")


def test_parse_summary_payload_JSON_없음_StructuredSummaryParseError() -> None:
    with pytest.raises(StructuredSummaryParseError):
        _parse_summary_payload("그냥 자유 서술만 있고 JSON은 없습니다")


def test_parse_summary_payload_JSON_파싱_실패_StructuredSummaryParseError() -> None:
    with pytest.raises(StructuredSummaryParseError):
        _parse_summary_payload('{"intent": "positive", broken')


def test_parse_summary_payload_intent_허용외_StructuredSummaryParseError() -> None:
    text = json.dumps(
        {
            "intent": "strongly-positive",
            "willingness_to_pay": 0,
            "willingness_to_pay_currency": "KRW",
            "rejection_reasons": [],
            "one_line": "x",
        }
    )
    with pytest.raises(StructuredSummaryParseError):
        _parse_summary_payload(text)


def test_parse_summary_payload_wtp_정수_변환_실패_StructuredSummaryParseError() -> None:
    text = json.dumps(
        {
            "intent": "positive",
            "willingness_to_pay": "비싸요",
            "willingness_to_pay_currency": "KRW",
            "rejection_reasons": [],
            "one_line": "x",
        },
        ensure_ascii=False,
    )
    with pytest.raises(StructuredSummaryParseError):
        _parse_summary_payload(text)


def test_build_summary_messages_system_제외_사용자_헬퍼() -> None:
    msgs = [
        MessageEntry(role="system", content="시스템"),
        MessageEntry(role="user", content="질문"),
        MessageEntry(role="assistant", content="답변"),
    ]
    summary_msgs = _build_summary_messages(msgs)
    assert summary_msgs[0]["role"] == "system"
    # 시스템 프롬프트에 출력 JSON 스키마 안내가 있다
    assert "JSON" in summary_msgs[0]["content"]
    # transcript에는 user/assistant만 들어가야 한다(원본 system은 제외)
    assert "[질문] 질문" in summary_msgs[1]["content"]
    assert "[답변] 답변" in summary_msgs[1]["content"]


# ---------------------------------------------------------------------------
# InterviewSession.run / run_interview (mock LLM 통합)
# ---------------------------------------------------------------------------


def _add_chat_response(httpx_mock, content: str) -> None:
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
        status_code=200,
    )


@pytest.mark.asyncio
async def test_run_interview_정상_경로_completed(
    httpx_mock,
    fake_persona_meta,
    make_app_config,
) -> None:
    """1개 질문 → 정상 답변 → 구조화 요약."""

    long_answer = (
        "가격이 합리적이라 한번 시도해 볼 만한 것 같아요. 최근에 비슷한 서비스를 찾고 있었거든요."
    )
    _add_chat_response(httpx_mock, long_answer)
    # 구조화 요약 응답
    summary_json = json.dumps(
        {
            "intent": "positive",
            "willingness_to_pay": 39900,
            "willingness_to_pay_currency": "KRW",
            "rejection_reasons": ["가격"],
            "one_line": "긍정",
        },
        ensure_ascii=False,
    )
    _add_chat_response(httpx_mock, summary_json)

    config = make_app_config()
    async with MlxLLMClient(config.llm) as client:
        record = await run_interview(
            persona=fake_persona_meta,
            product="반찬 정기배송",
            questions=["쓸 의향 있나요?"],
            follow_ups=[],
            llm=client,
            config=config,
        )

    assert isinstance(record, InterviewRecord)
    assert record.status == "completed"
    assert record.persona_id == fake_persona_meta.persona_id
    # system + user + assistant
    assert len(record.messages) == 3
    assert record.messages[0].role == "system"
    assert record.raw_responses[0].response == long_answer
    assert record.structured_summary is not None
    assert record.structured_summary.intent == "positive"


@pytest.mark.asyncio
async def test_run_interview_자동_follow_up_트리거(
    httpx_mock,
    fake_persona_meta,
    make_app_config,
) -> None:
    """짧은 답변 → 자동 follow-up 1회 추가."""

    _add_chat_response(httpx_mock, "그래요")  # 짧은 답변
    _add_chat_response(httpx_mock, "조금 더 자세히 말씀드리면 가격이 너무 비쌉니다.")  # follow-up 응답
    # 요약
    _add_chat_response(
        httpx_mock,
        json.dumps(
            {
                "intent": "negative",
                "willingness_to_pay": None,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": ["가격"],
                "one_line": "가격 부담",
            },
            ensure_ascii=False,
        ),
    )

    config = make_app_config()
    async with MlxLLMClient(config.llm) as client:
        record = await run_interview(
            persona=fake_persona_meta,
            product="반찬",
            questions=["쓰실래요?"],
            follow_ups=[],
            llm=client,
            config=config,
        )

    assert record.flags.auto_follow_up_used is True
    # raw_responses에는 메인 응답 + follow-up 응답 = 2건
    assert len(record.raw_responses) == 2
    # follow-up은 같은 question_index, retry_count 1
    assert record.raw_responses[1].question_index == 0
    assert record.raw_responses[1].retry_count >= 1


@pytest.mark.asyncio
async def test_run_interview_거부_감지_status_refused(
    httpx_mock,
    fake_persona_meta,
    make_app_config,
) -> None:
    """거부 키워드 감지 시 status=refused, 인터뷰 즉시 중단."""

    _add_chat_response(httpx_mock, "죄송합니다. 답변할 수 없습니다.")
    # 요약 호출도 발생하므로 미리 등록(거부도 요약 시도 - PRD §5.4 참고)
    _add_chat_response(
        httpx_mock,
        json.dumps(
            {
                "intent": "neutral",
                "willingness_to_pay": None,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": [],
                "one_line": "거부",
            },
            ensure_ascii=False,
        ),
    )

    config = make_app_config()
    async with MlxLLMClient(config.llm) as client:
        record = await run_interview(
            persona=fake_persona_meta,
            product="반찬",
            questions=["Q1", "Q2"],
            follow_ups=[],
            llm=client,
            config=config,
        )

    assert record.status == "refused"
    assert record.flags.refusal_detected is True
    # 두 번째 질문은 진행되지 않는다(중단)
    assert len(record.raw_responses) == 1


@pytest.mark.asyncio
async def test_run_interview_drift_감지_status_drift(
    httpx_mock,
    fake_persona_meta,
    make_app_config,
) -> None:
    """페르소나 깨짐 감지 시 status=drift 플래그 설정. 인터뷰는 계속 진행."""

    # 27세 여자 페르소나에 대해 "저는 남자" 단언으로 drift 트리거
    _add_chat_response(
        httpx_mock,
        "저는 남자라서 잘 모르겠어요. 그냥 보통 사람의 의견을 말씀드립니다. 가격은 적당하다고 봅니다.",
    )
    # 요약
    _add_chat_response(
        httpx_mock,
        json.dumps(
            {
                "intent": "neutral",
                "willingness_to_pay": None,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": [],
                "one_line": "drift",
            },
            ensure_ascii=False,
        ),
    )

    config = make_app_config()
    async with MlxLLMClient(config.llm) as client:
        record = await run_interview(
            persona=fake_persona_meta,
            product="반찬",
            questions=["Q1"],
            follow_ups=[],
            llm=client,
            config=config,
        )

    assert record.status == "drift"
    assert record.flags.persona_drift is True


@pytest.mark.asyncio
async def test_run_interview_questions_비어있음_ConfigError(
    fake_persona_meta,
    make_app_config,
) -> None:
    from src.models import ConfigError

    config = make_app_config()
    async with MlxLLMClient(config.llm) as client:
        with pytest.raises(ConfigError):
            session = InterviewSession(
                persona=fake_persona_meta,
                product="반찬",
                questions=[],
                follow_up_questions=[],
                client=client,
                config=config,
            )


@pytest.mark.asyncio
async def test_run_interview_LLM_실패_status_failed(
    httpx_mock,
    fake_persona_meta,
    make_app_config,
) -> None:
    """LLM 호출이 retry 모두 실패하면 status=failed로 기록한다."""

    for _ in range(3):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/v1/chat/completions",
            status_code=500,
        )

    config = make_app_config(retry_max_attempts=3)
    async with MlxLLMClient(config.llm) as client:
        record = await run_interview(
            persona=fake_persona_meta,
            product="반찬",
            questions=["Q1"],
            follow_ups=[],
            llm=client,
            config=config,
        )

    assert record.status == "failed"
    assert record.error is not None
    assert record.error["type"] == "retry_exhausted"


# ---------------------------------------------------------------------------
# summarize_interview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_interview_정상(httpx_mock, make_app_config) -> None:
    summary_json = json.dumps(
        {
            "intent": "positive",
            "willingness_to_pay": 32000,
            "willingness_to_pay_currency": "KRW",
            "rejection_reasons": [],
            "one_line": "긍정",
        },
        ensure_ascii=False,
    )
    _add_chat_response(httpx_mock, summary_json)

    config = make_app_config()
    msgs = [
        MessageEntry(role="system", content="시스템"),
        MessageEntry(role="user", content="질문"),
        MessageEntry(role="assistant", content="답변"),
    ]
    async with MlxLLMClient(config.llm) as client:
        result = await summarize_interview(msgs, client, config.llm)

    assert result is not None
    assert result.intent == "positive"


@pytest.mark.asyncio
async def test_summarize_interview_파싱_실패_2회후_None(
    httpx_mock, make_app_config
) -> None:
    """2회 retry 후에도 파싱 실패하면 None 반환."""

    _add_chat_response(httpx_mock, "JSON이 아닌 자유 서술")
    _add_chat_response(httpx_mock, "{invalid json")

    config = make_app_config()
    msgs = [
        MessageEntry(role="user", content="질문"),
        MessageEntry(role="assistant", content="답변"),
    ]
    async with MlxLLMClient(config.llm) as client:
        result = await summarize_interview(msgs, client, config.llm)

    assert result is None
