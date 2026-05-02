"""도메인 모델과 예외.

본 모듈은 외부 의존이 없는 순수 도메인 계층이다(architecture.md §1).
인터뷰 결과 record, 페르소나 메타, 구조화 요약, 배치 결과를 담는 frozen dataclass
와 사용자 노출/내부 도메인 예외 9종을 정의한다.

사용자 노출 예외는 main.py에서 종료 코드로 매핑하고, 내부 예외는 InterviewRecord
의 status/flags/error로 변환한다(TDD §5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# 화이트리스트 상수: 도메인 enum-like 검증에 사용한다(TDD §4).
ALLOWED_STATUS = frozenset({"completed", "refused", "failed", "drift"})
ALLOWED_INTENT = frozenset({"positive", "neutral", "negative"})
ALLOWED_GENDER = frozenset({"남자", "여자"})
ALLOWED_ROLE = frozenset({"system", "user", "assistant"})


# 결과 JSON 스키마 버전. 변경 시 reader가 분기할 수 있도록 RunMeta에 박는다.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PersonaMeta:
    """페르소나 1명의 인구 통계 + 원본 raw dict.

    데이터셋의 컬럼 매핑 결과(TDD §1.3)를 보존한다. ``name``은 데이터셋에 별도
    이름 컬럼이 없어 v1에서 ``None``으로 두는 것을 기본 동작으로 한다.

    ``family_type``/``housing_type``은 1인 가구 여부와 주거 유형을 시스템
    프롬프트에 그대로 노출하기 위한 필드다. 데이터셋에 해당 컬럼이 없거나
    비어 있는 경우 ``None``으로 둔다(field_map 매핑 결과 부재 시 동일).
    """

    persona_id: str
    name: Optional[str]
    gender: str
    age: int
    region: str
    subregion: str
    occupation: str
    marital: str
    education: str
    raw: dict
    family_type: Optional[str] = None
    housing_type: Optional[str] = None

    def __post_init__(self) -> None:
        if self.gender not in ALLOWED_GENDER:
            raise ValueError(
                f"PersonaMeta.gender는 {sorted(ALLOWED_GENDER)} 중 하나여야 한다: {self.gender!r}"
            )
        if not isinstance(self.age, int):
            raise ValueError(f"PersonaMeta.age는 int여야 한다: {type(self.age).__name__}")
        if self.age < 0:
            raise ValueError(f"PersonaMeta.age는 음수가 될 수 없다: {self.age}")


@dataclass(frozen=True)
class MessageEntry:
    """OpenAI 호환 messages 배열의 한 항목."""

    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in ALLOWED_ROLE:
            raise ValueError(
                f"MessageEntry.role은 {sorted(ALLOWED_ROLE)} 중 하나여야 한다: {self.role!r}"
            )


@dataclass(frozen=True)
class RawResponse:
    """질문 단위 응답 메타. 지연/재시도 분석 용도."""

    question_index: int
    response: str
    latency_ms: int
    retry_count: int
    reasoning_trace: Optional[str] = None


@dataclass(frozen=True)
class ChatResponse:
    """LLM chat 호출 결과 컨테이너.

    OpenAI Chat Completions API에는 ``message.reasoning`` 확장 필드가 없으므로
    ``reasoning_trace``는 v1.x 환경에서 항상 ``None``이다(직렬화 backward
    compatibility 유지를 위해 필드는 보존). ``content``가 비면 호출자가 별도
    에러 처리를 수행한다.
    """

    content: str
    latency_ms: int
    retry_count: int
    reasoning_trace: Optional[str] = None


@dataclass(frozen=True)
class StructuredSummary:
    """인터뷰 종료 후 단일턴으로 생성한 구조화 요약(ADR-001 §2)."""

    intent: str
    willingness_to_pay: Optional[int]
    willingness_to_pay_currency: str
    rejection_reasons: list
    one_line: str

    def __post_init__(self) -> None:
        if self.intent not in ALLOWED_INTENT:
            raise ValueError(
                f"StructuredSummary.intent는 {sorted(ALLOWED_INTENT)} 중 하나여야 한다: {self.intent!r}"
            )
        if self.willingness_to_pay is not None and self.willingness_to_pay < 0:
            raise ValueError(
                f"StructuredSummary.willingness_to_pay는 0 이상이어야 한다: {self.willingness_to_pay}"
            )


@dataclass(frozen=True)
class Flags:
    """record 단위 부가 플래그. truncated는 TDD §7과 ADR-001 §2에서 추가됐다."""

    persona_drift: bool = False
    auto_follow_up_used: bool = False
    refusal_detected: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class InterviewRecord:
    """페르소나 1명에 대한 인터뷰 1회 결과."""

    persona_id: str
    persona_meta: PersonaMeta
    started_at: str
    finished_at: str
    status: str
    messages: list
    raw_responses: list
    structured_summary: Optional[StructuredSummary]
    flags: Flags
    error: Optional[dict]

    def __post_init__(self) -> None:
        if self.status not in ALLOWED_STATUS:
            raise ValueError(
                f"InterviewRecord.status는 {sorted(ALLOWED_STATUS)} 중 하나여야 한다: {self.status!r}"
            )


@dataclass(frozen=True)
class RunMeta:
    """배치 인터뷰 1회의 메타. ``schema_version``으로 후방 호환성을 가린다."""

    interview_id: str
    slug: str
    schema_version: int
    product: str
    questions: list
    follow_up_questions: list
    model: str
    seed: int
    started_at: str
    finished_at: str
    config_snapshot: dict


@dataclass(frozen=True)
class BatchResult:
    """직렬화 단위. dataclasses.asdict로 JSON 변환한다(TDD §4)."""

    meta: RunMeta
    records: list


# ---------------------------------------------------------------------------
# 사용자 노출 예외(CLI 종료 코드와 매핑, TDD §5.1)
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """설정 파일/환경변수/CLI 인자 검증 실패. 종료 코드 1."""


class ServerNotReachableError(Exception):
    """LLM 서버 응답 실패(연결 거부, 타임아웃, 5xx 누적). 종료 코드 1."""


class DatasetUnavailableError(Exception):
    """Hugging Face 데이터셋 로드 실패. 종료 코드 1."""


class FilterMatchedZeroError(Exception):
    """필터 결과 0건 또는 요청 N보다 적음. 종료 코드 2."""


class EmptyValidRecordsError(Exception):
    """리포트 정량 집계 가능한 record가 0건일 때. 종료 코드 2.

    PRD §5.9의 ``report`` 명령 종료 코드 2와 매핑된다. ``ConfigError``와 동일
    계층(사용자 노출 예외)이지만 종료 코드가 다르므로 별도 예외로 분리한다.
    """


# ---------------------------------------------------------------------------
# 내부 예외(record로 변환, 외부로 누출 금지, TDD §5.2)
# ---------------------------------------------------------------------------


class PersonaBreakError(Exception):
    """페르소나 깨짐 감지. status=drift, flags.persona_drift=True로 변환."""


class ResponseTooShortError(Exception):
    """짧은 답변(자동 follow-up으로 흡수). v1에서는 raise 대신 플래그로만 처리."""


class ModelRefusedError(Exception):
    """모델 응답 거부. status=refused, flags.refusal_detected=True로 변환."""


class RetryExhaustedError(Exception):
    """재시도 3회 모두 실패. status=failed로 변환."""


class StructuredSummaryParseError(Exception):
    """구조화 요약 JSON 파싱 실패. structured_summary=None으로 변환."""


class EmptyResponseError(Exception):
    """``message.content``가 비어 있는 응답. retry 대상으로 본다.

    OpenAI 응답에서는 거의 발생하지 않지만 안전망으로 보존한다. v1.0 시절
    Qwen3 reasoning 토큰 폭증 사례(``enable_thinking=true`` 호출 시 content가
    빈 문자열로 반환되던 케이스)에서 도입된 매핑이다.
    """
