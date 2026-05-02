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
    InterviewSession,
    _build_summary_messages,
    _parse_single_turn_response,
    _parse_summary_payload,
    build_system_prompt,
    clear_system_prompt_cache,
    detect_persona_drift,
    detect_refusal,
    estimate_messages_tokens,
    estimate_tokens,
    run_interview,
    should_auto_follow_up,
    summarize_interview,
    truncate_history,
)
from src.llm_client import LLMClient
from src.models import (
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


def test_build_system_prompt_family_type_주입(fake_persona_meta) -> None:
    """family_type이 시스템 프롬프트의 페르소나 JSON에 포함된다(박태민 사례 회귀)."""

    prompt = build_system_prompt(
        fake_persona_meta,
        product="반찬",
        persona_fields=("summary",),
        field_map=_FIELD_MAP,
    )
    assert fake_persona_meta.family_type in prompt


def test_build_system_prompt_housing_type_주입(fake_persona_meta) -> None:
    """housing_type이 시스템 프롬프트의 페르소나 JSON에 포함된다."""

    prompt = build_system_prompt(
        fake_persona_meta,
        product="반찬",
        persona_fields=("summary",),
        field_map=_FIELD_MAP,
    )
    assert fake_persona_meta.housing_type in prompt


def test_build_system_prompt_거주_형태_지침_포함(fake_persona_meta) -> None:
    """[지침] 섹션에 family_type 추측 금지 한 줄이 포함된다."""

    prompt = build_system_prompt(
        fake_persona_meta,
        product="반찬",
        persona_fields=("summary",),
        field_map=_FIELD_MAP,
    )
    assert "family_type" in prompt
    assert "추측하지" in prompt


def test_build_system_prompt_페르소나_톤_지침_5종_포함(fake_persona_meta) -> None:
    """[지침] 섹션에 페르소나 1인칭 톤 강화 지침 5종이 모두 포함된다.

    gpt-4o-mini의 ``혼자 사시는 분들에겐 좋은 서비스`` 류 일반화 응답 회귀
    방지를 위한 톤 가드 지침이다. 본 지침이 누락되면 인터뷰 응답이 페르소나의
    family_type/housing_type 입장에서 벗어나 3인칭 일반화로 흐를 수 있다.
    """

    prompt = build_system_prompt(
        fake_persona_meta,
        product="반찬",
        persona_fields=("summary",),
        field_map=_FIELD_MAP,
    )
    # 1) 2-4문장 간결 지침(기존)
    assert "2-4문장" in prompt
    # 2) 본인 입장 고정 지침
    assert "본인의 경험과 입장에서만" in prompt
    # 3) 3인칭 일반화 회피 지침
    assert "3인칭 일반화" in prompt
    # 4) family_type/housing_type 그대로 따르기 지침
    assert "family_type/housing_type을 그대로" in prompt
    # 5) 거주 형태 추측 금지 지침(기존, family_type 단독)
    assert "거주 형태에 대해" in prompt and "추측하지" in prompt


def test_build_system_prompt_정적_prefix가_가변_부분보다_앞에_위치(
    fake_persona_meta,
) -> None:
    """OpenAI prompt caching 적합 구조 회귀.

    정적 prefix(인트로 + [지침]/[말투와 1인칭 일관성 지침]/[답변 내용 지침]/
    [출력 형식])가 가변 부분(``[페르소나 정보]`` JSON + ``[인터뷰 주제]``)보다
    앞에 와야 prompt caching이 자동 적용된다. prefix가 1024 토큰 이상이고 동일
    prefix가 반복되면 OpenAI가 입력 토큰의 50%를 캐시 환급으로 청구한다.
    """

    prompt = build_system_prompt(
        fake_persona_meta,
        product="반찬",
        persona_fields=("summary",),
        field_map=_FIELD_MAP,
    )
    instruction_idx = prompt.index("[말투와 1인칭 일관성 지침]")
    output_format_idx = prompt.index("[출력 형식]")
    persona_info_idx = prompt.index("[페르소나 정보]")
    interview_topic_idx = prompt.index("[인터뷰 주제]")

    assert instruction_idx < persona_info_idx
    assert output_format_idx < persona_info_idx
    assert persona_info_idx < interview_topic_idx


def test_build_system_prompt_prefix_길이_1024_토큰_이상(fake_persona_meta) -> None:
    """정적 prefix 길이가 OpenAI prompt caching 임계값(1024 토큰) 이상이다.

    estimate_tokens는 한국어 1자=1토큰 휴리스틱이라 OpenAI tokenizer의 실제
    토큰 수보다 보수적으로(작게) 추정된다. 본 테스트는 휴리스틱 기준
    1024 이상을 요구해 OpenAI 실제 토큰으로는 더 충분히 임계를 넘도록 보장한다.
    prefix가 1024 미만으로 줄면 prompt caching이 동작하지 않아 비용 절감 효과가
    사라진다.
    """

    prompt = build_system_prompt(
        fake_persona_meta,
        product="반찬",
        persona_fields=("summary",),
        field_map=_FIELD_MAP,
    )
    prefix_end = prompt.index("[페르소나 정보]")
    prefix = prompt[:prefix_end]
    assert estimate_tokens(prefix) >= 1024


def test_build_system_prompt_family_type_None이면_JSON에_미주입() -> None:
    """family_type이 None이면 페르소나 JSON 객체에 키가 등장하지 않는다.

    [지침] 섹션에는 family_type 단어가 항상 등장하므로 JSON 부분만 검사한다.
    JSON은 ``[페르소나 정보]`` 라벨과 다음 빈 줄 사이에 위치한다.
    """

    persona = PersonaMeta(
        persona_id="p",
        name=None,
        gender="여자",
        age=30,
        region="서울",
        subregion="서울-X",
        occupation="x",
        marital="x",
        education="x",
        raw={},
        family_type=None,
        housing_type=None,
    )
    prompt = build_system_prompt(
        persona,
        product="반찬",
        persona_fields=("summary",),
        field_map=_FIELD_MAP,
    )
    persona_json_block = prompt.split("[페르소나 정보]\n", 1)[1].split("\n\n", 1)[0]
    assert '"family_type"' not in persona_json_block


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


def _persona(
    age: int = 70,
    gender: str = "여자",
    region: str = "서울",
    family_type: str | None = None,
    housing_type: str | None = None,
) -> PersonaMeta:
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
        family_type=family_type,
        housing_type=housing_type,
    )


def _persona_with_occupation(occupation: str) -> PersonaMeta:
    """직업명만 다른 PersonaMeta. G11 영문 화이트리스트 회귀용."""

    return PersonaMeta(
        persona_id="p",
        name=None,
        gender="여자",
        age=30,
        region="서울",
        subregion="서울-X",
        occupation=occupation,
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


def test_detect_persona_drift_가족동거_1인_가구_부정_False_정합() -> None:
    """가족 동거 페르소나의 ``1인 가구가 아니`` 부정 단언은 정합이라 drift False다.

    record 1-4 사례 회귀 방지 테스트. 가족 동거 페르소나가 본인이 1인 가구가
    아님을 명시하는 답변은 페르소나와 정합하므로 drift 트리거 대상이 아니다.
    """

    text = "성수동에서 살고 있는데, 딱히 저는 1인 가구가 아니라서 필요성을 못 느끼겠네요."
    persona = _persona(age=34, gender="남자", region="서울", family_type="배우자와 거주")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_단독거주_가족과_살아_긍정_True() -> None:
    """family_type=1인 가구 페르소나가 ``저는 가족과 함께 살아``를 긍정 단언하면 drift다."""

    text = "저는 가족과 함께 살아서 그런 서비스가 필요 없어요."
    persona = _persona(age=25, family_type="1인 가구")
    assert detect_persona_drift(text, persona) is True


def test_detect_persona_drift_가족동거_혼자_산다_단언_True() -> None:
    """family_type=배우자와 거주 페르소나가 ``저는 혼자 산다``를 단언하면 drift다."""

    text = "저는 혼자 사는 입장이라 그런 거 잘 모르겠어요."
    persona = _persona(age=34, gender="남자", family_type="배우자와 거주")
    assert detect_persona_drift(text, persona) is True


def test_detect_persona_drift_가족동거_1인_가구_단언_True() -> None:
    """family_type=배우자·자녀와 거주 페르소나가 ``저는 1인 가구``를 단언하면 drift다."""

    text = "저는 1인 가구라서 식비가 늘 부담이에요."
    persona = _persona(age=41, family_type="배우자·자녀와 거주")
    assert detect_persona_drift(text, persona) is True


def test_detect_persona_drift_가족동거_혼자_살지_않_부정_False_정합() -> None:
    """가족 동거 페르소나의 ``혼자 살지 않`` 부정 단언도 정합이라 drift False다."""

    text = "저는 혼자 살지 않아서 큰 단위로 장을 봅니다."
    persona = _persona(age=34, gender="남자", family_type="배우자와 거주")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_단독거주_정합_답변_False() -> None:
    """family_type=혼자 거주 페르소나의 정합 답변(예: ``저는 1인 가구라``)은 drift가 아니다."""

    text = "저는 1인 가구라서 작은 단위로 사는 게 편해요."
    persona = _persona(age=25, family_type="혼자 거주")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_단독거주_가족과_살아_부정_False_정합() -> None:
    """단독 거주 페르소나의 ``가족과 살지 않`` 부정 단언은 정합이라 drift False다."""

    text = "저는 가족과 살지 않아서 식재료 보관이 늘 문제예요."
    persona = _persona(age=25, family_type="1인 가구")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_가족동거_정합_답변_False() -> None:
    """family_type=배우자와 거주 페르소나의 정합 답변(``저는 가족과 함께``)은 drift가 아니다.

    가족 동거 페르소나의 ``가족과 함께 살아`` 긍정 단언은 정합이라 drift 트리거
    대상이 아니다(가족 동거 → solo_assertion만 검사함).
    """

    text = "저는 가족과 함께 살아서 한 번에 큰 단위로 장을 봐요."
    persona = _persona(age=34, gender="남자", family_type="배우자와 거주")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_family_type_None이면_검사_건너뜀() -> None:
    """family_type이 None이면 거주 형태 휴리스틱은 동작하지 않는다(과도한 false positive 회피)."""

    text = "저는 1인 가구가 아니라서 필요성을 못 느끼겠어요."
    persona = _persona(age=25, family_type=None)
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_가족동거_3인칭_혼자_사시는_분들_False_정합() -> None:
    """가족 동거 페르소나가 ``혼자 사시는 분들에겐``으로 3인칭 표현을 써도 drift False다.

    실제 인터뷰 사례 회귀(record 3, 구나라 어머니와 동거): 응답에 ``혼자 사시는
    분들에겐 좋은 서비스``라는 3인칭 일반화가 들어와도 본인이 단독 거주임을
    단언하는 1인칭 동사가 아니므로 drift 트리거 대상이 아니다.
    """

    text = "또, 개인적으로 요리에 조금 관심이 있어서 저 스스로 반찬을 만들거나 준비하는 걸 더 선호하기도 하고요. 혼자 사시는 분들에겐 좋은 서비스일 것 같아요!"
    persona = _persona(age=28, gender="여자", family_type="어머니와 동거")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_가족동거_혼자서_행동표현_False_정합() -> None:
    """가족 동거 페르소나가 ``혼자서 끼니를 해결``로 행동 표현을 써도 drift False다.

    실제 인터뷰 사례 회귀(record 4, 박승환 부모와 동거): ``혼자서`` 다음에 거주
    동사가 아닌 행동 동사(``끼니를 해결``)가 오면 거주 형태 단언이 아니므로
    drift 트리거 대상이 아니다.
    """

    text = "그리고 가끔은 혼자서 간단히 끼니를 해결할 수 있기 때문에 필요성을 느끼지 못할 것 같네요."
    persona = _persona(age=37, gender="남자", family_type="부모와 동거")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_연령_부정문_단언_False_정합() -> None:
    """``저는 20대가 아니라 30대입니다`` 부정문 단언은 drift False다.

    라운드 G10 정밀화 회귀: 부정문 가드는 family_type 외에도 연령/성별/지역
    축에 동등하게 적용된다.
    """

    text = "저는 20대가 아니라 30대입니다. 가격 부담은 좀 있어요."
    persona = _persona(age=35)
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_연령_3인칭_언급_False() -> None:
    """``20대 친구는`` 같은 3인칭 언급은 drift False다."""

    text = "다른 사람들은 20대일 수도 있는데, 저는 30대라 좀 다른 관점입니다."
    persona = _persona(age=35)
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_성별_부정문_단언_False_정합() -> None:
    """여자 페르소나의 ``남자가 아니라`` 부정 단언은 drift False다."""

    text = "저는 남자가 아니라서 그런 액션 게임에는 관심이 적어요."
    persona = _persona(gender="여자")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_성별_단순_언급_False() -> None:
    """1인칭 주어 없이 ``남자``라는 단어가 등장만 해도 drift는 False."""

    text = "남자가 좋아할 만한 디자인이라 친구에게 추천해 줄 수 있을 것 같아요."
    persona = _persona(gender="여자")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_지역_부정문_단언_False_정합() -> None:
    """``부산 사람이 아니라``는 부정 단언은 drift False다."""

    text = "저는 부산 사람이 아니라서 거기 사정은 잘 모릅니다."
    persona = _persona(region="서울")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_지역_3인칭_언급_False() -> None:
    """``부산 친구``류 3인칭 언급은 drift False다."""

    text = "다른 사람들은 부산에 살기도 하지만 저는 서울 사람이라서 잘 모릅니다."
    persona = _persona(region="서울")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_지역_3인칭_관광_언급_False() -> None:
    """``부산 다녀왔다``류 잠깐 거론은 1인칭 거주 단언이 아니라 drift False다.

    G10 이전 30자 윈도우 휴리스틱은 ``저는 부산`` + ``살``로 매칭해 false
    positive를 일으켰다. 정밀화 후에는 거주 단언 동사 list에 매칭되어야 한다.
    """

    text = "저는 부산에 한 번 다녀왔어요. 거기 음식이 좋았어요."
    persona = _persona(region="서울")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_직업명_영문_화이트리스트_False() -> None:
    """``IT 컨설턴트`` 페르소나가 응답에 ``IT``를 포함해도 영어 비율 false positive가 없다.

    G11 occupation_english_whitelist 회귀 방지. 직업명 토큰은 분모에서 제외해
    drift 트리거를 일으키지 않는다.
    """

    persona = _persona_with_occupation("IT 컨설턴트")
    text = "IT 업계 동향을 보면 이 서비스도 좋아 보입니다. 그 외 인지도가 어느 정도 있을지 궁금하네요."
    assert detect_persona_drift(text, persona) is False


@pytest.mark.asyncio
async def test_review_drift_with_llm_judge_drift_True() -> None:
    """judge가 'drift'로 판정하면 True를 반환한다."""

    from src.interview import review_drift_with_llm
    from src.models import ChatResponse, TokenUsage

    class _Stub:
        async def chat(self, messages, max_tokens=None, temperature=None):
            return ChatResponse(
                content="drift",
                latency_ms=10,
                retry_count=0,
                reasoning_trace=None,
                usage=TokenUsage(),
            )

    persona = _persona(age=25)
    out = await review_drift_with_llm(
        "응답 본문",
        persona,
        _Stub(),  # type: ignore[arg-type]
        config=_persona_with_occupation("x").raw and None or _make_dummy_llm_config(),
    )
    assert out is True


@pytest.mark.asyncio
async def test_review_drift_with_llm_judge_ok_False() -> None:
    """judge가 'ok'로 판정하면 False를 반환해 drift를 되돌린다."""

    from src.interview import review_drift_with_llm
    from src.models import ChatResponse, TokenUsage

    class _Stub:
        async def chat(self, messages, max_tokens=None, temperature=None):
            return ChatResponse(
                content="ok",
                latency_ms=10,
                retry_count=0,
                reasoning_trace=None,
                usage=TokenUsage(),
            )

    persona = _persona(age=25)
    out = await review_drift_with_llm(
        "응답",
        persona,
        _Stub(),  # type: ignore[arg-type]
        config=_make_dummy_llm_config(),
    )
    assert out is False


@pytest.mark.asyncio
async def test_review_drift_with_llm_호출실패_보수적_True() -> None:
    """judge LLM 호출 자체가 실패하면 보수적으로 drift 라벨을 유지한다."""

    from src.interview import review_drift_with_llm

    class _BoomStub:
        async def chat(self, *args, **kwargs):
            raise RuntimeError("network down")

    persona = _persona(age=25)
    out = await review_drift_with_llm(
        "응답",
        persona,
        _BoomStub(),  # type: ignore[arg-type]
        config=_make_dummy_llm_config(),
    )
    assert out is True


def test_sanitize_user_text_길이_상한_ConfigError() -> None:
    """``--product`` 본문이 2000자를 초과하면 ConfigError로 차단된다."""

    from src.interview import _MAX_PRODUCT_LENGTH, _sanitize_user_text
    from src.models import ConfigError

    big = "가" * (_MAX_PRODUCT_LENGTH + 1)
    with pytest.raises(ConfigError, match="상한"):
        _sanitize_user_text(big, max_length=_MAX_PRODUCT_LENGTH, label="--product")


def test_sanitize_user_text_프롬프트_인젝션_마커_escape() -> None:
    """``[페르소나 정보]`` 같은 시스템 프롬프트 마커가 본문에 들어오면 escape된다."""

    from src.interview import _sanitize_user_text

    raw = "이 서비스는 [페르소나 정보] 같은 키워드를 보여요"
    cleaned = _sanitize_user_text(raw, max_length=2000, label="--product")
    assert "[페르소나 정보]" not in cleaned
    # 형태는 보존(zero-width space로 첫 글자만 갈아 끼움).
    assert "페르소나 정보]" in cleaned


def test_sanitize_user_text_정상_본문_그대로_통과() -> None:
    """일반 본문은 그대로 통과한다."""

    from src.interview import _sanitize_user_text

    raw = "1인 가구용 반찬 정기배송, 월 39,900원"
    cleaned = _sanitize_user_text(raw, max_length=2000, label="--product")
    assert cleaned == raw


def _make_dummy_llm_config():
    """judge 테스트용 최소 LlmConfig."""

    from src.config import LlmConfig

    return LlmConfig(
        base_url="https://api.openai.com/v1",
        model="test-model",
        max_tokens=128,
        temperature=0.5,
        timeout=5.0,
        context_budget=32000,
        retry_max_attempts=1,
        retry_backoff_seconds=(0.0,),
        api_key="test",
    )


def test_detect_persona_drift_직업명_영문_화이트리스트_OFF_다시_True() -> None:
    """occupation_english_whitelist=False면 직업명 토큰도 영어 비율 분자/분모에 포함된다."""

    persona = _persona_with_occupation("UX 디자이너")
    text = "UX UI design feel 한 부분이 어색해서 with 좀 어렵게 느껴졌어요."
    # 영어 단어 비율이 임계값을 넘기는 케이스. 화이트리스트 OFF면 직업명 토큰을
    # 분모에서 제외하지 않으므로 그대로 trigger된다.
    assert (
        detect_persona_drift(
            text, persona, occupation_english_whitelist=False
        )
        is True
    )


def test_detect_persona_drift_연령_정확한_단언_True() -> None:
    """1인칭 + 다른 연령 버킷 단언은 G10 후에도 drift True를 유지한다."""

    text = "저는 30대라서 가격이 좀 부담이긴 해요."
    persona = _persona(age=70)
    assert detect_persona_drift(text, persona) is True


def test_detect_persona_drift_성별_정확한_단언_True() -> None:
    """1인칭 + 반대 성별 + 단언 어미는 G10 후에도 drift True를 유지한다."""

    text = "저는 여자라서 화장품에 더 신경을 씁니다."
    persona = _persona(gender="남자")
    assert detect_persona_drift(text, persona) is True


def test_detect_persona_drift_지역_정확한_단언_True() -> None:
    """1인칭 + 다른 시도 + 거주 동사는 G10 후에도 drift True를 유지한다.

    자기 시도(서울)가 같은 문장에 들어가면 보수적으로 매칭에서 제외하므로
    다른 시도만 등장하는 문장이어야 한다.
    """

    text = "저는 부산에 살고 있어서 가까운 동네 식당을 자주 이용해요."
    persona = _persona(region="서울")
    assert detect_persona_drift(text, persona) is True


def test_detect_persona_drift_가족동거_product_키워드_누설_False_정합() -> None:
    """가족 동거 페르소나가 product 키워드 ``1인 가구용``을 응답에 포함해도 drift False다.

    실제 인터뷰 사례 회귀(record 3/4, 첫 응답): ``저는 어머니와 함께 살고 있어서
    1인 가구용 반찬 정기배송 서비스는 필요하지 않을 것 같아요`` 처럼 자기소개
    뒤에 product 키워드가 따라와도 ``1인 가구``를 본인 거주 형태로 단언하는
    1인칭 단언 동사(``라``/``입``/``예요``)가 없으면 drift 트리거 대상이 아니다.
    """

    text = "저는 어머니와 함께 살고 있어서 1인 가구용 반찬 정기배송 서비스는 필요하지 않을 것 같아요. 하지만 혼자 사시는 분들한테는 정말 유용할 것 같아요!"
    persona = _persona(age=28, gender="여자", family_type="어머니와 동거")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_가족동거_혼자_사는_분들_3인칭_False_정합() -> None:
    """가족 동거 페르소나가 ``혼자 사는 분들에게``로 3인칭 일반화를 써도 drift False다.

    record 4 r1 사례 회귀: ``주 2회 배송이면 혼자 사는 분들에게 충분히 도움이
    될 것 같고요`` 처럼 ``혼자 사는`` 뒤에 ``분들``/``사람들`` 같은 3인칭 명사가
    오면 본인 거주 형태 단언이 아니다.
    """

    text = "주 2회 배송이면 혼자 사는 분들에게 충분히 도움이 될 것 같고, 가격도 부담스럽지 않은 수준인 것 같아요."
    persona = _persona(age=37, gender="남자", family_type="부모와 동거")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_단독거주_3인칭_가족과_사시는_분들_False_정합() -> None:
    """단독 거주 페르소나가 ``가족과 사시는 분들``로 3인칭 표현을 써도 drift False다.

    대칭 회귀 가드: cohabit 정규식이 1인칭 주어를 강제하므로 3인칭 일반화는
    매칭되지 않는다.
    """

    text = "가족과 사시는 분들에게는 별로 도움이 안 될 것 같아요."
    persona = _persona(age=25, family_type="1인 가구")
    assert detect_persona_drift(text, persona) is False


def test_detect_persona_drift_지역_자기_시도_동시_등장_False() -> None:
    """자기 단언 컨텍스트에 자기 시도와 다른 시도가 동시에 등장하면 false positive를 내지 않는다.

    예: 서울 거주자가 ``저는 서울 사람이지만 부산에도 자주 갑니다``라고 하면 drift가 아니다.
    """

    text = "저는 서울 사람이지만 부산에도 자주 갑니다."
    persona = _persona(region="서울")
    assert detect_persona_drift(text, persona) is False


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
        url="https://api.openai.com/v1/chat/completions",
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
    async with LLMClient(config.llm) as client:
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
    async with LLMClient(config.llm) as client:
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
    async with LLMClient(config.llm) as client:
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
    async with LLMClient(config.llm) as client:
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
    async with LLMClient(config.llm) as client:
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
            url="https://api.openai.com/v1/chat/completions",
            status_code=500,
        )

    config = make_app_config(retry_max_attempts=3)
    async with LLMClient(config.llm) as client:
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
    async with LLMClient(config.llm) as client:
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
    async with LLMClient(config.llm) as client:
        result = await summarize_interview(msgs, client, config.llm)

    assert result is None


# ---------------------------------------------------------------------------
# 단일턴 파서 단위 테스트
# ---------------------------------------------------------------------------


def test_parse_single_turn_response_정상_3개_분리() -> None:
    """3개 질문에 대한 정상 응답을 번호별로 분리한다."""

    text = (
        "1. 가격이 합리적이라 한번 시도해 볼 만합니다.\n"
        "2. 월 3만 원 정도면 좋겠어요.\n"
        "3. 너무 비싸면 안 쓸 것 같아요."
    )
    answers, parse_failed = _parse_single_turn_response(text, 3)

    assert parse_failed is False
    assert len(answers) == 3
    assert "합리적" in answers[0]
    assert "3만 원" in answers[1]
    assert "비싸면" in answers[2]


def test_parse_single_turn_response_괄호_번호_지원() -> None:
    """``1)`` 형식 번호도 인식한다."""

    text = "1) 첫 답변\n2) 두 번째 답변"
    answers, parse_failed = _parse_single_turn_response(text, 2)

    assert parse_failed is False
    assert answers[0] == "첫 답변"
    assert answers[1] == "두 번째 답변"


def test_parse_single_turn_response_부분_파싱_실패_fallback() -> None:
    """기대 3개인데 2개만 파싱되면 parse_failed=True + fallback."""

    text = "1. 첫 답변\n2. 두 번째 답변"  # 3번이 없음
    answers, parse_failed = _parse_single_turn_response(text, 3)

    assert parse_failed is True
    assert len(answers) == 3
    # fallback: 마지막 인덱스에 통째 텍스트 보존
    assert text in answers[-1]


def test_parse_single_turn_response_번호_없는_응답_fallback() -> None:
    """번호 형식이 전혀 없으면 통째 텍스트를 마지막 question에 담는다."""

    text = "그냥 자유 서술 답변입니다. 번호를 안 붙였네요."
    answers, parse_failed = _parse_single_turn_response(text, 2)

    assert parse_failed is True
    assert answers[0] == ""
    assert text in answers[-1]


def test_parse_single_turn_response_여러_단락_본문_보존() -> None:
    """다음 번호 마커 전까지의 본문(여러 단락)을 한 응답으로 합친다."""

    text = (
        "1. 첫 번째 답변입니다.\n"
        "이어지는 두 번째 줄도 같은 답변에 속합니다.\n"
        "2. 두 번째 답변."
    )
    answers, parse_failed = _parse_single_turn_response(text, 2)

    assert parse_failed is False
    assert "이어지는 두 번째 줄" in answers[0]
    assert answers[1] == "두 번째 답변."


# ---------------------------------------------------------------------------
# 단일턴 인터뷰 흐름 통합 테스트(InterviewSession._run_single_turn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_interview_single_turn_정상_파싱(
    httpx_mock,
    fake_persona_meta,
    make_app_config,
) -> None:
    """단일턴: 질문 3개 → 한 번의 chat 호출로 응답 → 번호별 분리."""

    multi_answer = (
        "1. 가격이 적당하면 한번 써볼 의향이 있어요.\n"
        "2. 월 3만 원 정도면 좋겠습니다.\n"
        "3. 너무 비싸면 거절할 것 같아요."
    )
    _add_chat_response(httpx_mock, multi_answer)
    _add_chat_response(
        httpx_mock,
        json.dumps(
            {
                "intent": "positive",
                "willingness_to_pay": 30000,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": ["가격"],
                "one_line": "긍정",
            },
            ensure_ascii=False,
        ),
    )

    config = make_app_config(single_turn=True)
    async with LLMClient(config.llm) as client:
        record = await run_interview(
            persona=fake_persona_meta,
            product="반찬 정기배송",
            questions=["쓸 의향?", "월 얼마?", "거절 이유?"],
            follow_ups=[],
            llm=client,
            config=config,
        )

    assert record.status == "completed"
    assert record.flags.parse_failed is False
    # raw_responses 3개 = 질문 3개. messages는 system + user + assistant 3개.
    assert len(record.raw_responses) == 3
    assert len(record.messages) == 3
    assert record.messages[0].role == "system"
    assert record.messages[1].role == "user"
    assert record.messages[2].role == "assistant"
    # 첫 응답에만 latency_ms/usage가 박혀 있고 나머지는 0/빈 usage.
    assert record.raw_responses[0].latency_ms > 0 or record.raw_responses[0].latency_ms == 0
    assert record.raw_responses[1].latency_ms == 0
    assert record.raw_responses[2].latency_ms == 0
    assert "적당" in record.raw_responses[0].response
    assert "3만 원" in record.raw_responses[1].response
    assert "비싸" in record.raw_responses[2].response


@pytest.mark.asyncio
async def test_run_interview_single_turn_부분_파싱_fallback(
    httpx_mock,
    fake_persona_meta,
    make_app_config,
) -> None:
    """단일턴: 응답에 번호가 빠지면 parse_failed=True + 마지막에 통째 본문."""

    bad_answer = "1. 첫 답변만 적었네요."  # 2번이 없음
    _add_chat_response(httpx_mock, bad_answer)
    _add_chat_response(
        httpx_mock,
        json.dumps(
            {
                "intent": "neutral",
                "willingness_to_pay": None,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": [],
                "one_line": "fallback",
            },
            ensure_ascii=False,
        ),
    )

    config = make_app_config(single_turn=True)
    async with LLMClient(config.llm) as client:
        record = await run_interview(
            persona=fake_persona_meta,
            product="반찬",
            questions=["Q1", "Q2"],
            follow_ups=[],
            llm=client,
            config=config,
        )

    assert record.status == "completed"
    assert record.flags.parse_failed is True
    assert len(record.raw_responses) == 2
    assert bad_answer in record.raw_responses[-1].response


@pytest.mark.asyncio
async def test_run_interview_single_turn_drift_감지(
    httpx_mock,
    fake_persona_meta,
    make_app_config,
) -> None:
    """단일턴: 응답에 drift(영어 비율)가 섞이면 status=drift."""

    drift_answer = (
        "1. I think this product is good but I will pass for now.\n"
        "2. Maybe thirty thousand won is fine for me."
    )
    _add_chat_response(httpx_mock, drift_answer)
    _add_chat_response(
        httpx_mock,
        json.dumps(
            {
                "intent": "neutral",
                "willingness_to_pay": 30000,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": [],
                "one_line": "drift",
            },
            ensure_ascii=False,
        ),
    )

    config = make_app_config(single_turn=True)
    async with LLMClient(config.llm) as client:
        record = await run_interview(
            persona=fake_persona_meta,
            product="반찬",
            questions=["Q1", "Q2"],
            follow_ups=[],
            llm=client,
            config=config,
        )

    assert record.status == "drift"
    assert record.flags.persona_drift is True


@pytest.mark.asyncio
async def test_run_interview_single_turn_chat_호출_1회_멀티턴_대비_절감(
    httpx_mock,
    fake_persona_meta,
    make_app_config,
) -> None:
    """단일턴: 질문 N개라도 chat 호출은 1회(요약 1회 별도). 비용 절감 검증."""

    answer = "1. 좋아요.\n2. 적당해요.\n3. 별로요."
    _add_chat_response(httpx_mock, answer)
    _add_chat_response(
        httpx_mock,
        json.dumps(
            {
                "intent": "neutral",
                "willingness_to_pay": None,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": [],
                "one_line": "단일턴",
            },
            ensure_ascii=False,
        ),
    )

    config = make_app_config(single_turn=True)
    async with LLMClient(config.llm) as client:
        record = await run_interview(
            persona=fake_persona_meta,
            product="제품",
            questions=["Q1", "Q2", "Q3"],
            follow_ups=[],
            llm=client,
            config=config,
        )

    assert record.status == "completed"
    # chat 호출 횟수: 본 인터뷰 1회 + 구조화 요약 1회 = 2회.
    # 멀티턴이라면 질문 3개 + 요약 = 최소 4회였을 것. 절반.
    requests = httpx_mock.get_requests()
    chat_calls = [
        r for r in requests if "chat/completions" in str(r.url)
    ]
    assert len(chat_calls) == 2


@pytest.mark.asyncio
async def test_run_interview_single_turn_자동_follow_up_비활성화(
    httpx_mock,
    fake_persona_meta,
    make_app_config,
) -> None:
    """단일턴은 자동 follow-up 미실행(짧은 답변이어도 1회 호출만)."""

    short_answer = "1. 그래요.\n2. 싫어요."  # 짧지만 단일턴이라 follow-up 안 함
    _add_chat_response(httpx_mock, short_answer)
    _add_chat_response(
        httpx_mock,
        json.dumps(
            {
                "intent": "neutral",
                "willingness_to_pay": None,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": [],
                "one_line": "짧음",
            },
            ensure_ascii=False,
        ),
    )

    config = make_app_config(single_turn=True)
    async with LLMClient(config.llm) as client:
        record = await run_interview(
            persona=fake_persona_meta,
            product="제품",
            questions=["Q1", "Q2"],
            follow_ups=[],
            llm=client,
            config=config,
        )

    assert record.flags.auto_follow_up_used is False
    requests = httpx_mock.get_requests()
    chat_calls = [r for r in requests if "chat/completions" in str(r.url)]
    # 인터뷰 1회 + 요약 1회 = 2. follow-up 호출 없음.
    assert len(chat_calls) == 2


# ---------------------------------------------------------------------------
# 라운드 B2: 외부화된 휴리스틱 임계값/키워드 적용 검증
# ---------------------------------------------------------------------------


def test_detect_persona_drift_영어_비율_임계값_상향_관대(
    fake_persona_meta,
) -> None:
    """english_ratio_threshold를 0.5로 올리면 영어 비율 0.4 응답이 drift False."""

    text = "This is okay 그러나 가격이 좀 비싸요"
    # 기본 0.30 → True
    assert detect_persona_drift(text, fake_persona_meta, 0.30) is True
    # 임계값 0.5 → False(관대해짐)
    assert detect_persona_drift(text, fake_persona_meta, 0.5) is False


def test_detect_persona_drift_영어_비율_임계값_하향_엄격(
    fake_persona_meta,
) -> None:
    """english_ratio_threshold를 0.10으로 내리면 영어 단어 한 개에도 trigger."""

    text = "이 product는 적당히 좋아 보입니다 그렇지만 가격이 좀 부담입니다"
    # 기본 0.30 → False(영어 1개라 비율 낮음)
    assert detect_persona_drift(text, fake_persona_meta, 0.30) is False
    # 임계값 0.10 → True(엄격해짐)
    assert detect_persona_drift(text, fake_persona_meta, 0.10) is True


def test_should_auto_follow_up_threshold_상향_더_자주_발동() -> None:
    """short_answer_threshold를 30으로 올리면 22자 답변도 짧음으로 본다."""

    # 공백 제거 시 22자가 되도록 구성한다(20자 기본 임계 위, 30자 임계 아래).
    text = "가격이 적당해서 한번 써볼 만합니다 아주 좋아 보여요"
    no_ws = "".join(text.split())
    assert len(no_ws) == 22, f"테스트 가정 위반: {len(no_ws)}자"
    # 기본 20 → 통과(False, 22자 >= 20)
    assert should_auto_follow_up(text, threshold=20) is False
    # 임계값 30 → True(더 자주 발동, 22자 < 30)
    assert should_auto_follow_up(text, threshold=30) is True


@pytest.mark.asyncio
async def test_run_interview_auto_follow_up_max_0이면_비활성(
    httpx_mock,
    fake_persona_meta,
    make_app_config,
) -> None:
    """auto_follow_up_max=0이면 짧은 답변에도 follow-up이 발동되지 않는다."""

    _add_chat_response(httpx_mock, "그래요")  # 짧은 답변
    _add_chat_response(
        httpx_mock,
        json.dumps(
            {
                "intent": "neutral",
                "willingness_to_pay": None,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": [],
                "one_line": "짧음",
            },
            ensure_ascii=False,
        ),
    )

    config = make_app_config(auto_follow_up_max=0)
    async with LLMClient(config.llm) as client:
        record = await run_interview(
            persona=fake_persona_meta,
            product="반찬",
            questions=["쓰실래요?"],
            follow_ups=[],
            llm=client,
            config=config,
        )

    assert record.flags.auto_follow_up_used is False
    # 호출은 본 인터뷰 1 + 요약 1 = 2회만
    requests = httpx_mock.get_requests()
    chat_calls = [r for r in requests if "chat/completions" in str(r.url)]
    assert len(chat_calls) == 2


# ---------------------------------------------------------------------------
# 라운드 B4: 시스템 프롬프트 템플릿 파일 분리
# ---------------------------------------------------------------------------


def test_build_system_prompt_default_파일_로드(fake_persona_meta) -> None:
    """기본 prompts/system_prompt.txt 파일을 읽어 템플릿이 적용된다."""

    clear_system_prompt_cache()
    prompt = build_system_prompt(
        fake_persona_meta,
        "반찬 정기배송",
        ("summary",),
        _FIELD_MAP,
    )
    # 페르소나 JSON 주입
    assert "27" in prompt
    assert "여자" in prompt
    # product 주입
    assert "반찬 정기배송" in prompt
    # 1인칭 일관성 지침이 들어 있다
    assert "1인칭" in prompt


def test_build_system_prompt_커스텀_파일_경로(
    fake_persona_meta, tmp_path
) -> None:
    """system_prompt_path를 임시 파일로 바꾸면 그 내용이 system 메시지가 된다."""

    clear_system_prompt_cache()
    custom_path = tmp_path / "custom_prompt.txt"
    custom_path.write_text(
        "[CUSTOM] persona={persona_json} product={product}",
        encoding="utf-8",
    )

    prompt = build_system_prompt(
        fake_persona_meta,
        "테스트 제품",
        ("summary",),
        _FIELD_MAP,
        str(custom_path),
    )
    assert prompt.startswith("[CUSTOM]")
    assert "테스트 제품" in prompt
    # 기본 템플릿의 1인칭 일관성 지침은 들어가지 않는다(다른 파일이라)
    assert "3인칭 일반화" not in prompt


def test_build_system_prompt_파일_없음_ConfigError(
    fake_persona_meta, tmp_path
) -> None:
    """존재하지 않는 경로를 주면 ConfigError + 친절한 안내."""

    clear_system_prompt_cache()
    missing = tmp_path / "missing.txt"
    from src.models import ConfigError

    with pytest.raises(ConfigError, match="시스템 프롬프트"):
        build_system_prompt(
            fake_persona_meta,
            "x",
            ("summary",),
            _FIELD_MAP,
            str(missing),
        )


def test_build_system_prompt_default_경로_부재시_packaged_fallback(
    fake_persona_meta, tmp_path, monkeypatch
) -> None:
    """default 경로 ``prompts/system_prompt.txt``가 부재하면 패키지 내부 리소스로 fallback.

    pip 사용자가 프로젝트 루트 prompts/ 디렉토리를 두지 않은 환경에서도 본
    도구가 동작하게 한다. 명시 경로를 지정한 사용자에게는 fallback이 적용되지
    않는다(default와 다른 경로를 의도적으로 가리킨 것이라 다른 파일이 들어가는
    일을 막아야 한다).
    """

    clear_system_prompt_cache()

    # 프로젝트 루트의 ``prompts/system_prompt.txt`` 위치를 일시적으로 비어 있는
    # tmp_path 기반 경로로 갈아 끼워 fallback 경로를 강제한다.
    import src.interview as _interview_mod

    monkeypatch.setattr(_interview_mod, "_PROJECT_ROOT", tmp_path)

    # default 경로(prompts/system_prompt.txt)는 tmp_path에 존재하지 않으므로
    # 패키지 내부 리소스가 fallback으로 잡혀야 한다.
    prompt = build_system_prompt(
        fake_persona_meta,
        "x",
        ("summary",),
        _FIELD_MAP,
        "prompts/system_prompt.txt",
    )
    assert "{persona_json}" not in prompt
    assert "x" in prompt


def test_build_system_prompt_placeholder_누락_ConfigError(
    fake_persona_meta, tmp_path
) -> None:
    """템플릿에 {persona_json} 또는 {product}이 없으면 ConfigError."""

    clear_system_prompt_cache()
    bad_path = tmp_path / "bad.txt"
    bad_path.write_text("placeholder가 없는 본문", encoding="utf-8")
    from src.models import ConfigError

    with pytest.raises(ConfigError, match="placeholder"):
        build_system_prompt(
            fake_persona_meta,
            "x",
            ("summary",),
            _FIELD_MAP,
            str(bad_path),
        )


def test_build_system_prompt_캐시_재로드_없음(
    fake_persona_meta, tmp_path
) -> None:
    """같은 파일에 대한 두 번째 호출은 캐시 hit으로 디스크를 다시 읽지 않는다."""

    clear_system_prompt_cache()
    p = tmp_path / "p.txt"
    p.write_text("v1 {persona_json} {product}", encoding="utf-8")

    out1 = build_system_prompt(
        fake_persona_meta, "x", ("summary",), _FIELD_MAP, str(p)
    )
    assert "v1" in out1

    # 디스크 내용 변경 후 mtime이 같으면 캐시가 그대로(테스트 환경에서 mtime은
    # 보통 다르지만 적어도 한 번 더 호출했을 때 정상적으로 결과를 받는지 확인).
    out2 = build_system_prompt(
        fake_persona_meta, "x", ("summary",), _FIELD_MAP, str(p)
    )
    assert out1 == out2


@pytest.mark.asyncio
async def test_run_interview_auto_follow_up_text_커스텀_적용(
    httpx_mock,
    fake_persona_meta,
    make_app_config,
) -> None:
    """auto_follow_up_text yaml/CLI 설정이 messages에 그대로 들어간다."""

    custom_text = "한 줄만 더 부탁드릴게요!"
    _add_chat_response(httpx_mock, "그래요")  # 짧은 답변(공백 제거 3자)
    _add_chat_response(
        httpx_mock,
        "조금 더 자세히 말씀드리면 가격이 좀 부담스럽습니다.",
    )  # follow-up 응답
    _add_chat_response(
        httpx_mock,
        json.dumps(
            {
                "intent": "negative",
                "willingness_to_pay": None,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": ["가격"],
                "one_line": "부담",
            },
            ensure_ascii=False,
        ),
    )

    config = make_app_config(auto_follow_up_text=custom_text)
    async with LLMClient(config.llm) as client:
        record = await run_interview(
            persona=fake_persona_meta,
            product="반찬",
            questions=["쓰실래요?"],
            follow_ups=[],
            llm=client,
            config=config,
        )

    assert record.flags.auto_follow_up_used is True
    # messages에서 follow-up user 발화 확인
    follow_up_user_msgs = [
        m for m in record.messages
        if m.role == "user" and m.content == custom_text
    ]
    assert len(follow_up_user_msgs) == 1
