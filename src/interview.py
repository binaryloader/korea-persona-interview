"""멀티턴 인터뷰 세션과 단일턴 구조화 요약(ADR-001 채택).

본 모듈은 페르소나 1명에 대한 멀티턴 인터뷰 1회를 수행한다. 책임은 아래와 같다.

- 시스템 프롬프트 빌드(HANDOFF.md §시스템 프롬프트 템플릿 + 페르소나 정보 JSON 주입)
- 질문별 user → assistant 페어 누적, 토큰 예산 초과 시 가장 오래된 페어부터 truncate
- 자동 follow-up(짧은 답변 또는 모호 키워드 매칭, 상한 1회)
- 사용자 정의 follow-up(메인 질문 후 순차 진행)
- 페르소나 깨짐 감지(영어 비율 + 정면 모순 휴리스틱), 모델 거부 감지(거부 키워드)
- 인터뷰 종료 후 별도 single-turn 호출로 구조화 요약(JSON) 생성

순수 함수(``build_system_prompt``, ``estimate_tokens``, ``truncate_history``,
``should_auto_follow_up``, ``detect_persona_drift``, ``detect_refusal``)는 모듈
함수로 분리해 단위 테스트 용이성을 확보한다(TDD §16).

application 계층이며, infrastructure(``MlxLLMClient``)와 domain(``PersonaMeta``,
``InterviewRecord`` 등)을 조합한다(architecture.md §1, §2).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from ._json_utils import extract_json_object
from .config import AppConfig, InterviewConfig, LlmConfig
from .llm_client import MlxLLMClient
from .logging_setup import mask_name, mask_product
from .models import (
    ChatResponse,
    ConfigError,
    EmptyResponseError,
    Flags,
    InterviewRecord,
    MessageEntry,
    PersonaMeta,
    RawResponse,
    RetryExhaustedError,
    ServerNotReachableError,
    StructuredSummary,
    StructuredSummaryParseError,
)


logger = logging.getLogger(__name__)


# 자동 follow-up 시 추가하는 사용자 발화. PRD §5.1, §5.8 표준 문구다.
AUTO_FOLLOW_UP_PROMPT = "조금만 더 자세히 말씀해 주실 수 있을까요?"


# 구조화 요약 출력 스키마. 모델이 자유 서술 대신 정해진 JSON만 출력하도록 강제한다.
# 키 순서/타입은 PRD §5.4와 ``StructuredSummary`` dataclass에 맞춘다.
_SUMMARY_SCHEMA_HINT = (
    "{\n"
    '  "intent": "positive | neutral | negative",\n'
    '  "willingness_to_pay": 정수 또는 null(원화 KRW 기준 월/회 1회 지불 의사),\n'
    '  "willingness_to_pay_currency": "KRW",\n'
    '  "rejection_reasons": ["거절 사유 1", "거절 사유 2", ...],\n'
    '  "one_line": "인터뷰 한 줄 요약(한국어, 최대 80자)"\n'
    "}"
)


# 17개 시도 짧은 표기. 페르소나 깨짐 감지 시 자기 시도가 아닌 시도를 거주지로
# 단언하는 응답을 잡기 위한 화이트리스트다(TDD §8.2). 데이터셋 표기는 짧은
# 표기지만 사용자가 영어/한국어 변형을 섞을 수 있어 별칭 일부도 포함한다.
_KOREAN_PROVINCES: tuple = (
    "서울",
    "부산",
    "대구",
    "인천",
    "광주",
    "대전",
    "울산",
    "경기",
    "강원",
    "충청북",
    "충청남",
    "전북",
    "전남",
    "경상북",
    "경상남",
    "제주",
    "세종",
)


# 자기소개 문맥 정규식. ``저는`` 류 자기 단언 표현 뒤에 따라오는 토큰을 검사한다.
# 연령대/성별/지역 모순 휴리스틱에 공통 사용한다.
_SELF_INTRO_PATTERN = re.compile(
    r"(?:저는|나는|제가|내가)\s*([^\.\?!\n,]{0,30})"
)


# ---------------------------------------------------------------------------
# 시스템 프롬프트 빌드
# ---------------------------------------------------------------------------


def build_system_prompt(
    persona: PersonaMeta,
    product: str,
    persona_fields: tuple,
    field_map: dict,
) -> str:
    """HANDOFF.md §시스템 프롬프트 템플릿에 페르소나 정보를 주입한다.

    기본 묶음은 인구 통계 7개 필드와 ``persona``(요약 자유 서술)다(TDD §1.4).
    토글 키워드(``professional``/``sports``/``arts``/``travel``/``culinary``/
    ``family``)가 ``persona_fields``에 있으면 해당 자유 서술 컬럼을 raw에서
    꺼내 추가한다.

    Args:
        persona: 페르소나 메타와 raw dict.
        product: 사업 아이템 한 줄 설명. 시스템 프롬프트의 인터뷰 주제로 사용.
        persona_fields: 토글 키워드 튜플. ``("summary",)``가 기본값.
        field_map: ``DatasetConfig.field_map``. 토글 키워드 → 데이터셋 컬럼 매핑.

    Returns:
        시스템 프롬프트 문자열.
    """

    # 기본 묶음: 인구 통계 + summary 페르소나(TDD §1.4).
    persona_obj: dict = {
        "name": persona.name,
        "gender": persona.gender,
        "age": persona.age,
        "marital": persona.marital,
        "education": persona.education,
        "occupation": persona.occupation,
        "region": persona.region,
        "subregion": persona.subregion,
    }

    # summary는 항상 주입한다. 데이터셋의 ``persona`` 컬럼이 매핑된다.
    summary_col = field_map.get("summary", "persona")
    if summary_col and summary_col in persona.raw:
        summary_text = persona.raw.get(summary_col)
        if summary_text:
            persona_obj["summary"] = summary_text

    # 토글 페르소나 자유 서술. ``summary``는 위에서 처리했으니 건너뛴다.
    toggle_keys = ("professional", "sports", "arts", "travel", "culinary", "family")
    for toggle in toggle_keys:
        if toggle not in persona_fields:
            continue
        column = field_map.get(toggle)
        if not column or column not in persona.raw:
            continue
        text = persona.raw.get(column)
        if text:
            persona_obj[toggle] = text

    persona_json = json.dumps(persona_obj, ensure_ascii=False, indent=2)

    # HANDOFF.md §시스템 프롬프트 템플릿 그대로(주제만 product로 명시).
    return (
        "당신은 다음 한국인 인물입니다. 이 인물의 정체성, 가치관, 말투, 관심사를 "
        "그대로 체화하여 답변하세요.\n"
        "\n"
        "[페르소나 정보]\n"
        f"{persona_json}\n"
        "\n"
        "[인터뷰 주제]\n"
        f"{product}\n"
        "\n"
        "[지침]\n"
        "- 이 인물의 연령, 직업, 거주지역에 어울리는 말투를 사용하세요"
        "(예: 60대는 정중한 평어, 20대는 캐주얼한 어투).\n"
        "- 모르는 것은 솔직히 모른다고 답하세요.\n"
        "- 답변은 2-4문장으로 간결하게.\n"
        "- 솔직한 거절도 좋습니다. 무리해서 긍정하지 마세요.\n"
        "- 페르소나 정보에 없는 사실을 지어내지 마세요."
    )


# ---------------------------------------------------------------------------
# 토큰 추정과 truncation(TDD §7)
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """한국어/영어 혼합 텍스트의 토큰 수를 휴리스틱으로 추정한다.

    한글 1자 = 1, 영문 1자 = 0.25, 그 외 1자 = 0.5(공백/숫자/기호 등)로 합산한다.
    절댓값 정확도보다 truncation 트리거의 일관성이 목적이라 실제 토크나이저
    없이 stdlib만으로 계산한다(TDD §7).
    """

    if not text:
        return 0
    score = 0.0
    for ch in text:
        # 한글 음절(가-힣) + 한글 자모 + 한자 + 가나는 1자 1토큰.
        cp = ord(ch)
        if 0xAC00 <= cp <= 0xD7A3:  # 한글 음절
            score += 1.0
        elif 0x3130 <= cp <= 0x318F:  # 한글 자모
            score += 1.0
        elif 0x4E00 <= cp <= 0x9FFF:  # CJK 통합 한자
            score += 1.0
        elif ("a" <= ch.lower() <= "z"):
            score += 0.25
        else:
            score += 0.5
    return int(score) + (1 if score - int(score) > 0 else 0)


def estimate_messages_tokens(messages: list) -> int:
    """messages 배열 전체의 추정 토큰 합. role당 약간의 오버헤드를 더한다."""

    total = 0
    for m in messages:
        if isinstance(m, MessageEntry):
            content = m.content
        elif isinstance(m, dict):
            content = m.get("content", "")
        else:
            continue
        # 메시지당 role 표기/구분자 오버헤드를 4토큰으로 가정한다(휴리스틱).
        total += estimate_tokens(str(content)) + 4
    return total


def truncate_history(
    messages: list,
    max_tokens: int = 8000,
) -> tuple:
    """system을 보존하고 누적이 한계를 넘으면 가장 오래된 user/assistant 페어부터 제거한다.

    페어 단위로 제거하는 이유는 user 질문만 남고 assistant 답변이 사라지면
    모델 컨텍스트가 비대칭이 되기 때문이다(TDD §7).

    Args:
        messages: ``MessageEntry`` 리스트. messages[0]는 system이어야 한다.
        max_tokens: 토큰 예산. ``LlmConfig.context_budget``(기본 8000).

    Returns:
        ``(truncated_messages, was_truncated)``.
    """

    if not messages:
        return list(messages), False

    if estimate_messages_tokens(messages) <= max_tokens:
        return list(messages), False

    # system 메시지를 분리해 보존한다. messages[0]가 system이 아니면 보존 대상
    # 없이 그대로 진행한다(예외적 호출).
    head: list = []
    body: list = list(messages)
    first = messages[0]
    first_role = first.role if isinstance(first, MessageEntry) else first.get("role")
    if first_role == "system":
        head = [body.pop(0)]

    truncated = False
    # 가장 오래된 user/assistant 페어를 2개씩 제거한다. body[0]은 보통 첫 user.
    while estimate_messages_tokens(head + body) > max_tokens and len(body) >= 2:
        # 페어 단위 제거(앞에서 2개씩).
        body = body[2:]
        truncated = True

    # 페어가 남지 않을 정도로 빠진 경우 마지막 단일 메시지까지 제거한다.
    while estimate_messages_tokens(head + body) > max_tokens and body:
        body = body[1:]
        truncated = True

    return head + body, truncated


# ---------------------------------------------------------------------------
# 휴리스틱: 짧은 답변, 페르소나 깨짐, 거부 감지
# ---------------------------------------------------------------------------


def should_auto_follow_up(
    response: str,
    threshold: int = 20,
    ambiguous_keywords: tuple = (),
) -> bool:
    """답변이 짧거나 모호 키워드를 포함하면 True(PRD §5.1, TDD §8.1).

    - 길이 임계: 공백 제거 후 글자 수가 ``threshold`` 미만이면 True
    - 키워드 매칭: ``ambiguous_keywords`` 중 하나라도 부분 문자열로 포함되면 True
    """

    if not response:
        return True
    stripped = response.strip()
    no_ws = "".join(stripped.split())
    if len(no_ws) < threshold:
        return True
    for kw in ambiguous_keywords:
        if kw and kw in stripped:
            return True
    return False


def _english_ratio(text: str) -> float:
    """전체 글자(공백/구두점 제외) 대비 영문 알파벳 비율."""

    if not text:
        return 0.0
    letters = [c for c in text if not c.isspace() and not c in ".,!?;:'\"()[]{}-"]
    if not letters:
        return 0.0
    english = sum(1 for c in letters if "a" <= c.lower() <= "z")
    return english / len(letters)


def _age_bucket(age: int) -> str:
    """연령을 6개 버킷 중 하나로 매핑한다(TDD §8.2)."""

    if age < 20:
        return "10대"
    if age < 30:
        return "20대"
    if age < 40:
        return "30대"
    if age < 50:
        return "40대"
    if age < 60:
        return "50대"
    return "60대 이상"


def _all_age_buckets() -> tuple:
    return ("10대", "20대", "30대", "40대", "50대", "60대 이상")


def detect_persona_drift(response: str, persona: PersonaMeta) -> bool:
    """페르소나 정면 모순 또는 영어 비율 30% 초과 여부를 판정한다(TDD §8.2).

    감지 축은 아래 셋이다.

    - 영어 비율: ``_english_ratio`` > 0.30이면 True
    - 연령대 모순: ``저는 20대``처럼 자기 연령 버킷이 아닌 버킷을 단언
    - 성별 모순: 여자 페르소나가 ``저는 남자``를 단언, 또는 그 반대
    - 지역 모순: 자기 시도가 아닌 다른 시도를 거주지로 단언

    가짜 양성을 줄이기 위해 ``저는``/``나는``/``제가``/``내가`` 같은 자기 단언
    표현 뒤에 따라오는 30자 이내 토큰만 검사한다.
    """

    if not response:
        return False

    if _english_ratio(response) > 0.30:
        return True

    # 자기 단언 컨텍스트 추출.
    self_intros = [m.group(1) for m in _SELF_INTRO_PATTERN.finditer(response)]
    if not self_intros:
        return False

    own_bucket = _age_bucket(persona.age)
    other_buckets = tuple(b for b in _all_age_buckets() if b != own_bucket)
    own_gender = persona.gender  # "남자" 또는 "여자"
    own_region = persona.region

    # 시도 비교는 prefix 매칭으로 수행한다(데이터셋 표기는 짧은 형태).
    other_provinces = tuple(p for p in _KOREAN_PROVINCES if p != own_region)

    for ctx in self_intros:
        text = ctx.strip()
        # 연령대 모순. ``20대``, ``30대`` 등은 self-intro 컨텍스트에서만 본다.
        for bucket in other_buckets:
            # ``60대 이상``은 공백을 포함하므로 그대로 검사하고, 나머지는 ``대``
            # 까지 정확히 매칭한다.
            if bucket in text:
                return True
        # 학생/미성년자 같은 명백한 연령 모순 키워드(70대 페르소나가 ``학생``,
        # ``미성년`` 단언 시 매칭).
        if persona.age >= 30 and ("학생" in text or "미성년" in text):
            return True

        # 성별 모순.
        if own_gender == "여자":
            if "남자" in text or "아저씨" in text:
                return True
        elif own_gender == "남자":
            if "여자" in text or "아줌마" in text:
                return True

        # 지역 모순. ``저는 부산 사람`` 형태.
        for province in other_provinces:
            # 다른 시도명이 self-intro 컨텍스트에 등장 + ``사람``/``살``/``에서``
            # 같은 거주 단언 키워드 동반.
            if province in text and any(
                k in text for k in ("사람", "살고", "에서 자랐", "살아")
            ):
                return True

    return False


def detect_refusal(response: str, refusal_keywords: tuple) -> bool:
    """거부 키워드 부분 매칭으로 모델 거부를 판정한다(TDD §8.3).

    ``answers`` 영문 거부 패턴(``I cannot``, ``I'm sorry, but``)과 한국어
    패턴(``답변할 수 없습니다``, ``저는 인공지능``)을 모두 포함한다.
    """

    if not response or not refusal_keywords:
        return False
    for kw in refusal_keywords:
        if kw and kw in response:
            return True
    return False


# ---------------------------------------------------------------------------
# 구조화 요약(2단계 흐름)
# ---------------------------------------------------------------------------


def _build_summary_messages(messages: list) -> list:
    """구조화 요약용 single-turn messages 배열을 만든다.

    인터뷰 messages를 본문에 직렬화하고, 출력 JSON 스키마를 강제한다. 시스템
    프롬프트는 인터뷰분석가 역할을 부여한다(ADR-001 §2).
    """

    transcript_lines: list = []
    for m in messages:
        role = m.role if isinstance(m, MessageEntry) else m.get("role", "")
        content = m.content if isinstance(m, MessageEntry) else m.get("content", "")
        if role == "system":
            continue  # 분석가에게 페르소나 자체를 다시 주입할 필요 없음
        label = "질문" if role == "user" else "답변"
        transcript_lines.append(f"[{label}] {content}")
    transcript = "\n".join(transcript_lines)

    system_prompt = (
        "당신은 인터뷰 분석가입니다. 아래 인터뷰 대화를 보고 정해진 JSON으로만 "
        "답변하세요. 추가 설명, 주석, 마크다운 코드 블록 표기를 붙이지 마세요. "
        "JSON 외 텍스트가 포함되면 후처리 단계에서 파싱이 실패합니다.\n"
        "\n"
        "[출력 JSON 스키마]\n"
        f"{_SUMMARY_SCHEMA_HINT}\n"
        "\n"
        "[필드 의미]\n"
        "- intent: 인터뷰 종합 의향(positive/neutral/negative 셋 중 하나)\n"
        "- willingness_to_pay: 정수(원). 인터뷰에서 명시된 지불 의사 금액. "
        "명시되지 않았거나 거절한 경우 null.\n"
        "- willingness_to_pay_currency: 항상 \"KRW\".\n"
        "- rejection_reasons: 거절/유보 사유 리스트(빈 배열 허용).\n"
        "- one_line: 한국어 한 줄 요약(80자 이내)."
    )

    user_prompt = (
        "아래 인터뷰 대화를 분석해 정해진 JSON 스키마로만 답하세요.\n"
        "\n"
        "[인터뷰 대화]\n"
        f"{transcript}"
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _parse_summary_payload(text: str) -> StructuredSummary:
    """LLM 응답 텍스트에서 ``StructuredSummary``를 복원한다.

    JSON 본문이 코드 펜스(```json ... ```)에 감싸인 경우와 가장 바깥 ``{ ... }``
    추출은 ``_json_utils.extract_json_object``가 처리한다. 본 함수는 추출된
    dict를 ``StructuredSummary`` 도메인 검증으로 변환한다. 파싱 실패 시
    ``StructuredSummaryParseError``로 변환해 retry 트리거에 사용한다.
    """

    if not text or not text.strip():
        raise StructuredSummaryParseError("구조화 요약 응답이 비어 있다")

    data = extract_json_object(text)
    if data is None:
        raise StructuredSummaryParseError(
            f"구조화 요약 응답에서 JSON 객체를 찾지 못했다: {text.strip()[:120]!r}"
        )

    intent = data.get("intent")
    wtp = data.get("willingness_to_pay")
    currency = data.get("willingness_to_pay_currency", "KRW")
    reasons = data.get("rejection_reasons", [])
    one_line = data.get("one_line", "")

    # 정수 또는 None 강제.
    wtp_int: Optional[int] = None
    if wtp is not None:
        try:
            wtp_int = int(wtp)
        except (TypeError, ValueError) as exc:
            raise StructuredSummaryParseError(
                f"willingness_to_pay 정수 변환 실패: {wtp!r}"
            ) from exc

    if not isinstance(reasons, list):
        raise StructuredSummaryParseError(
            f"rejection_reasons는 list여야 한다: {type(reasons).__name__}"
        )
    reasons_list = [str(r) for r in reasons if r is not None]

    try:
        return StructuredSummary(
            intent=str(intent) if intent is not None else "",
            willingness_to_pay=wtp_int,
            willingness_to_pay_currency=str(currency) if currency else "KRW",
            rejection_reasons=reasons_list,
            one_line=str(one_line) if one_line else "",
        )
    except ValueError as exc:
        # __post_init__의 enum 검증 실패도 파싱 실패로 본다(retry 대상).
        raise StructuredSummaryParseError(
            f"구조화 요약 검증 실패: {exc}"
        ) from exc


async def summarize_interview(
    messages: list,
    client: MlxLLMClient,
    config: LlmConfig,
) -> Optional[StructuredSummary]:
    """별도 single-turn 호출로 ``StructuredSummary``를 생성한다(ADR-001 §2).

    JSON 파싱 실패 시 1회 retry. 그래도 실패하면 ``None`` 반환.
    LLM 호출 자체가 ``RetryExhaustedError``로 실패하면 ``None`` 반환.
    """

    summary_messages = _build_summary_messages(messages)
    last_error: Optional[Exception] = None

    for attempt in range(2):
        try:
            chat_response: ChatResponse = await client.chat(
                summary_messages,
                max_tokens=min(400, config.max_tokens),
                # 요약은 자유 서술 변동성을 줄이기 위해 살짝 낮춘다.
                temperature=0.3,
            )
        except (RetryExhaustedError, ServerNotReachableError, ConfigError) as exc:
            last_error = exc
            logger.warning(
                "구조화 요약 LLM 호출 실패",
                extra={"attempt": attempt + 1, "reason": str(exc)},
            )
            return None

        try:
            return _parse_summary_payload(chat_response.content)
        except StructuredSummaryParseError as exc:
            last_error = exc
            logger.warning(
                "구조화 요약 JSON 파싱 실패",
                extra={"attempt": attempt + 1, "reason": str(exc)},
            )
            # 1회 retry 후에도 실패하면 None 반환.
            continue

    logger.warning(
        "구조화 요약 None 반환(retry 한도 초과)",
        extra={"last_error": str(last_error) if last_error else None},
    )
    return None


# ---------------------------------------------------------------------------
# 인터뷰 세션
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO 8601 UTC 타임스탬프(초 단위)."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class InterviewSession:
    """페르소나 1명에 대한 멀티턴 인터뷰 1회(TDD §3.5).

    사용 예시는 아래와 같다.

    ::

        async with MlxLLMClient(cfg.llm) as client:
            session = InterviewSession(persona, product, questions, follow_ups, client, cfg)
            record = await session.run()

    상태는 ``messages``, ``raw_responses``, ``flags``, ``status`` 네 가지다.
    호출자는 ``run()`` 결과 ``InterviewRecord`` 하나만 받는다.
    """

    def __init__(
        self,
        persona: PersonaMeta,
        product: str,
        questions: list,
        follow_up_questions: list,
        client: MlxLLMClient,
        config: AppConfig,
    ) -> None:
        if not questions:
            raise ConfigError("questions가 비어 있다. 1개 이상 지정해 주세요")

        self._persona = persona
        self._product = product
        self._questions = list(questions)
        self._follow_ups = list(follow_up_questions or [])
        self._client = client
        self._config = config
        self._llm_cfg: LlmConfig = config.llm
        self._interview_cfg: InterviewConfig = config.interview

    async def run(self) -> InterviewRecord:
        """인터뷰 1회를 끝까지 진행하고 ``InterviewRecord``를 반환한다.

        에러 처리는 TDD §5.2에 따른다. 내부 예외는 ``status``/``flags``/``error``
        로 변환하고 외부로 누출하지 않는다.
        """

        started_at = _now_iso()
        system_prompt = build_system_prompt(
            self._persona,
            self._product,
            self._config.batch.persona_fields,
            self._config.dataset.field_map,
        )
        messages: list = [MessageEntry(role="system", content=system_prompt)]
        raw_responses: list = []
        flags = Flags()
        status = "completed"
        error_payload: Optional[dict] = None

        logger.info(
            "인터뷰 시작",
            extra={
                "persona_id": self._persona.persona_id,
                "persona_name": mask_name(self._persona.name),
                "persona_age": self._persona.age,
                "persona_gender": self._persona.gender,
                "persona_region": self._persona.region,
                "product": mask_product(self._product),
                "questions_count": len(self._questions),
                "follow_ups_count": len(self._follow_ups),
            },
        )

        try:
            # 메인 질문 + 사용자 정의 follow-up을 순차 진행한다. follow-up은
            # 메인 질문 이후 별도 question_index로 누적한다.
            all_questions = list(self._questions) + list(self._follow_ups)

            for q_index, question in enumerate(all_questions):
                # 질문 1턴 진행.
                messages, was_truncated = self._maybe_truncate(messages)
                if was_truncated:
                    flags = dataclasses.replace(flags, truncated=True)

                messages.append(MessageEntry(role="user", content=question))
                response_text, latency_ms, retry_count = await self._call_llm(messages)
                messages.append(
                    MessageEntry(role="assistant", content=response_text)
                )
                raw_responses.append(
                    RawResponse(
                        question_index=q_index,
                        response=response_text,
                        latency_ms=latency_ms,
                        retry_count=retry_count,
                    )
                )

                # 거부 감지(가장 강한 신호. 즉시 중단).
                if detect_refusal(response_text, self._interview_cfg.refusal_keywords):
                    flags = dataclasses.replace(flags, refusal_detected=True)
                    status = "refused"
                    logger.warning(
                        "모델 거부 감지",
                        extra={
                            "persona_id": self._persona.persona_id,
                            "question_index": q_index,
                        },
                    )
                    break

                # 페르소나 깨짐 감지(중단하지 않고 플래그만 기록, PRD §5.8).
                if detect_persona_drift(response_text, self._persona):
                    flags = dataclasses.replace(flags, persona_drift=True)
                    status = "drift"
                    logger.warning(
                        "페르소나 깨짐 감지",
                        extra={
                            "persona_id": self._persona.persona_id,
                            "question_index": q_index,
                        },
                    )

                # 자동 follow-up은 메인 질문 구간(q_index < len(self._questions))
                # 에서만, flag 미사용 시 1회 적용한다.
                if (
                    q_index < len(self._questions)
                    and not flags.auto_follow_up_used
                    and should_auto_follow_up(
                        response_text,
                        threshold=self._interview_cfg.short_answer_threshold,
                        ambiguous_keywords=self._interview_cfg.ambiguous_keywords,
                    )
                ):
                    flags = dataclasses.replace(flags, auto_follow_up_used=True)
                    logger.debug(
                        "자동 follow-up 트리거",
                        extra={
                            "persona_id": self._persona.persona_id,
                            "question_index": q_index,
                        },
                    )

                    messages, was_truncated = self._maybe_truncate(messages)
                    if was_truncated:
                        flags = dataclasses.replace(flags, truncated=True)

                    messages.append(
                        MessageEntry(role="user", content=AUTO_FOLLOW_UP_PROMPT)
                    )
                    fu_text, fu_latency_ms, fu_retry = await self._call_llm(messages)
                    messages.append(MessageEntry(role="assistant", content=fu_text))
                    # 같은 question_index, retry_count는 1 증가로 표기한다.
                    raw_responses.append(
                        RawResponse(
                            question_index=q_index,
                            response=fu_text,
                            latency_ms=fu_latency_ms,
                            retry_count=fu_retry + 1,
                        )
                    )

                    # follow-up 응답에서도 거부/drift는 감지한다.
                    if detect_refusal(
                        fu_text, self._interview_cfg.refusal_keywords
                    ):
                        flags = dataclasses.replace(flags, refusal_detected=True)
                        status = "refused"
                        break
                    if detect_persona_drift(fu_text, self._persona):
                        flags = dataclasses.replace(flags, persona_drift=True)
                        status = "drift"

        except RetryExhaustedError as exc:
            status = "failed"
            error_payload = {
                "type": "retry_exhausted",
                "message": str(exc),
            }
            logger.error(
                "인터뷰 실패(재시도 한도 초과)",
                extra={
                    "persona_id": self._persona.persona_id,
                    "reason": str(exc),
                },
            )
        except ServerNotReachableError as exc:
            status = "failed"
            error_payload = {
                "type": "server_not_reachable",
                "message": str(exc),
            }
            logger.error(
                "인터뷰 실패(서버 응답 없음)",
                extra={
                    "persona_id": self._persona.persona_id,
                    "reason": str(exc),
                },
            )
        except EmptyResponseError as exc:
            # 단일 호출 단위 EmptyResponse는 llm_client에서 retry가 흡수해야
            # 한다. 흡수 실패가 EmptyResponseError로 외부로 나오면 failed 처리.
            status = "failed"
            error_payload = {
                "type": "empty_response",
                "message": str(exc),
            }

        # 구조화 요약은 status가 ``completed``/``drift``/``refused``일 때 시도한다.
        # ``failed``(LLM 호출 자체 실패)는 본 인터뷰에 답이 없는 상태라 생략한다.
        structured_summary: Optional[StructuredSummary] = None
        if status in ("completed", "drift", "refused") and raw_responses:
            try:
                structured_summary = await summarize_interview(
                    messages, self._client, self._llm_cfg
                )
            except Exception as exc:  # noqa: BLE001 - 안전망
                # summarize_interview 자체가 실패해도 인터뷰 본체는 보존한다.
                logger.warning(
                    "구조화 요약 단계 예외(structured_summary=None로 보존)",
                    extra={
                        "persona_id": self._persona.persona_id,
                        "reason": str(exc),
                    },
                )
                structured_summary = None

        finished_at = _now_iso()
        record = InterviewRecord(
            persona_id=self._persona.persona_id,
            persona_meta=self._persona,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            messages=list(messages),
            raw_responses=list(raw_responses),
            structured_summary=structured_summary,
            flags=flags,
            error=error_payload,
        )

        logger.info(
            "인터뷰 종료",
            extra={
                "persona_id": self._persona.persona_id,
                "status": status,
                "responses_count": len(raw_responses),
                "flags": dataclasses.asdict(flags),
                "summary_present": structured_summary is not None,
            },
        )
        return record

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _maybe_truncate(self, messages: list) -> tuple:
        """현재 messages가 토큰 예산을 넘으면 truncate한다.

        Returns:
            ``(possibly_truncated_messages, was_truncated)``.
        """

        return truncate_history(
            messages, max_tokens=self._llm_cfg.context_budget
        )

    async def _call_llm(self, messages: list) -> tuple:
        """``MlxLLMClient.chat``을 호출하고 (text, latency_ms, retry_count)를 반환한다.

        OpenAI 호환 dict 형식으로 변환하여 보낸다.
        """

        api_messages = [
            {"role": m.role, "content": m.content}
            if isinstance(m, MessageEntry)
            else m
            for m in messages
        ]
        chat_response: ChatResponse = await self._client.chat(api_messages)
        return (
            chat_response.content,
            chat_response.latency_ms,
            chat_response.retry_count,
        )


# ---------------------------------------------------------------------------
# 모듈 함수 진입점(테스트와 호출자 양쪽 호환)
# ---------------------------------------------------------------------------


async def run_interview(
    persona: PersonaMeta,
    product: str,
    questions: list,
    follow_ups: list,
    llm: MlxLLMClient,
    config: AppConfig,
) -> InterviewRecord:
    """``InterviewSession``의 함수형 진입점.

    호출자가 클래스 인스턴스를 만들지 않고도 동일한 결과를 얻을 수 있도록 둔다.
    """

    session = InterviewSession(
        persona=persona,
        product=product,
        questions=questions,
        follow_up_questions=follow_ups,
        client=llm,
        config=config,
    )
    return await session.run()
