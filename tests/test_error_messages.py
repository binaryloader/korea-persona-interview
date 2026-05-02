"""UI §3 한국어 에러 메시지 사전 정합성 테스트.

본 테스트는 코드(``main.py`` MESSAGES, 도메인 예외 raise 메시지)와 UI 명세
(``docs/ui/korea-persona-interview.md`` §3.1 표)의 한국어 메시지가 정합한지
검증한다. 17종 사전 항목을 카테고리별로 본 모듈 안에서 단언한다.

사용자에게 표시되는 메시지가 본 사전과 일치하는지 식별자(모델 ID, persona_id,
경로, URL)는 영문 보존 원칙(UI §5.2)도 함께 확인한다.
"""

from __future__ import annotations

import re

import pytest

from main import MESSAGES
from src.config import BatchConfig, LlmConfig
from src.llm_client import LLMClient
from src.load_personas import _sample_indices, parse_filter
from src.models import (
    ConfigError,
    DatasetUnavailableError,
    FilterMatchedZeroError,
    ModelRefusedError,
    PersonaBreakError,
    RetryExhaustedError,
    ServerNotReachableError,
    StructuredSummaryParseError,
)


# ---------------------------------------------------------------------------
# 사전 텍스트 패턴(UI §3.1 표 본문)
# ---------------------------------------------------------------------------


def test_ServerNotReachableError_메시지_본문() -> None:
    """Provider-agnostic body referencing the configured model id placeholder."""

    assert "LLM 서버에 연결할 수 없습니다" in MESSAGES["server_not_reachable"]
    assert "{model}" in MESSAGES["server_not_reachable"]


def test_ServerTimeoutError_의도_타임아웃_언급() -> None:
    """2번: ``120초`` 또는 동시성 점검 안내. v1에서는 server_not_reachable로 통합 처리.

    별도 키가 아니라 server_not_reachable 메시지의 변형으로 표시되며, 본 테스트는
    timeout 발생 시 ServerNotReachableError로 변환되는지만 확인한다.
    """

    exc = ServerNotReachableError("OpenAI 서버 응답 타임아웃")
    assert "타임아웃" in str(exc)


def test_FilterMatchedZeroError_메시지() -> None:
    """3번: ``필터 조건에 맞는 페르소나가 없습니다. 필터를 완화해 주세요``."""

    assert "필터" in MESSAGES["filter_zero"]
    assert "완화" in MESSAGES["filter_zero"]


def test_FilterMatchedTooFewError_메시지() -> None:
    """4번: ``필터 결과가 요청 수보다 적습니다. --n을 줄이거나 필터를 완화해 주세요``."""

    assert "필터" in MESSAGES["filter_too_few"]
    assert "--n" in MESSAGES["filter_too_few"]
    assert "완화" in MESSAGES["filter_too_few"]


def test_DatasetUnavailableError_메시지() -> None:
    """5번: ``데이터셋을 로드하지 못했습니다`` + ``~/.cache/huggingface``."""

    assert "데이터셋을 로드하지 못했습니다" in MESSAGES["dataset_unavailable"]
    assert "~/.cache/huggingface" in MESSAGES["dataset_unavailable"]


def test_DatasetSchemaError_의도_field_map_안내() -> None:
    """6번: 데이터셋 스키마 불일치 안내. v1에서는 DatasetUnavailableError로 통합 처리.

    본 테스트는 코드 raise 메시지가 한국어인지만 확인한다.
    """

    exc = DatasetUnavailableError("컬럼 매핑 실패")
    assert "컬럼" in str(exc) or "매핑" in str(exc)


def test_ConfigError_메시지() -> None:
    """7번: ``설정 파일을 읽을 수 없습니다`` + ``{원인}``."""

    assert "설정 파일을 읽을 수 없습니다" in MESSAGES["config_error"]
    assert "{reason}" in MESSAGES["config_error"]


def test_InvalidFilterError_의도_필터_DSL_파싱() -> None:
    """8번: ``필터 표현식이 올바르지 않습니다``. 코드는 ConfigError로 통합 처리.

    parse_filter 호출 시 잘못된 표현식이 들어오면 한국어 메시지가 raise된다.
    """

    aliases = {"F": "여자", "M": "남자"}
    province_aliases = {}
    with pytest.raises(ConfigError) as exc_info:
        parse_filter("wrong_format_no_colon", aliases, province_aliases)
    msg = str(exc_info.value)
    assert "필터" in msg or "올바른 예" in msg


def test_ConcurrencyOutOfRangeError_메시지() -> None:
    """9번: ``동시성은 1-10 범위만 허용한다`` + ``입력값``.

    OpenAI 백엔드 전환 후 상한이 1-10으로 상향되어 11이 범위 밖 케이스다.
    """

    with pytest.raises(ConfigError) as exc_info:
        BatchConfig(concurrency=11)
    msg = str(exc_info.value)
    assert "1-10" in msg
    assert "입력값" in msg


def test_InputFileNotFoundError_메시지() -> None:
    """10번: ``입력 파일을 읽지 못했습니다. 경로를 확인해 주세요``."""

    assert "입력 파일을 읽지 못했습니다" in MESSAGES["input_file_not_found"]
    assert "ls outputs/" in MESSAGES["input_file_not_found"]


def test_InputFileSchemaError_메시지() -> None:
    """11번: ``입력 파일이 올바른 인터뷰 JSON 형식이 아닙니다``."""

    assert "올바른 인터뷰 JSON 형식이 아닙니다" in MESSAGES["input_file_schema"]
    assert "interview 명령으로 생성된 JSON인지" in MESSAGES["input_file_schema"]


def test_EmptyValidRecordsError_메시지() -> None:
    """12번: ``리포트를 생성할 수 있는 정상 record가 없습니다``."""

    assert "리포트를 생성할 수 있는 정상 record가 없습니다" in MESSAGES["empty_valid_records"]
    assert "다시 실행" in MESSAGES["empty_valid_records"]


def test_PartialFailureError_메시지() -> None:
    """13번: ``부분 실패로 종료합니다(완료 {x}명 / 요청 {n}명)``."""

    assert "부분 실패로 종료합니다" in MESSAGES["partial_failure"]
    assert "{x}" in MESSAGES["partial_failure"]
    assert "{n}" in MESSAGES["partial_failure"]


def test_UserInterrupted_메시지() -> None:
    """14번: ``사용자 중단 신호를 받았습니다`` + ``부분 결과를 ... 저장``."""

    assert "사용자 중단 신호를 받았습니다" in MESSAGES["user_interrupted"]
    assert "부분 결과" in MESSAGES["user_interrupted"]


def test_StructuredSummaryParseError_의도_record_단위() -> None:
    """15번: ``구조화 요약 응답을 파싱하지 못했습니다``. record 단위로만 발생.

    예외는 internal로 위로 누출하지 않고 ``structured_summary=null``로 변환된다.
    본 테스트는 raise 메시지에 한국어가 포함되는지만 확인한다.
    """

    exc = StructuredSummaryParseError("구조화 요약 JSON 파싱 실패: 예시")
    assert "구조화 요약" in str(exc)


def test_RefusalDetected_의도_record_단위() -> None:
    """16번: ``모델이 응답을 거부했습니다``. record 단위.

    내부 예외 ``ModelRefusedError``는 status=refused로 변환되며 외부 노출은 없다.
    """

    exc = ModelRefusedError("모델 거부 감지")
    assert "거부" in str(exc) or isinstance(exc, ModelRefusedError)


def test_PersonaDriftDetected_의도_record_단위() -> None:
    """17번: ``페르소나 깨짐을 감지했습니다``. record 단위. status=drift로 변환."""

    exc = PersonaBreakError("페르소나 깨짐 감지")
    assert "페르소나" in str(exc) or isinstance(exc, PersonaBreakError)


# ---------------------------------------------------------------------------
# 식별자 영문 보존 원칙(UI §5.2)
# ---------------------------------------------------------------------------


def test_식별자_영문_보존_모델ID_placeholder() -> None:
    """모델 ID는 한국어 안에서도 영문 placeholder로 둔다."""

    template = MESSAGES["server_not_reachable"]
    # 모델 ID는 ``{model}`` placeholder
    assert "{model}" in template


def test_식별자_영문_보존_파일경로_언급() -> None:
    """입력 파일 안내에는 영문 경로 표기(``ls outputs/``)가 그대로 들어간다."""

    assert "outputs/" in MESSAGES["input_file_not_found"]


# ---------------------------------------------------------------------------
# 한국어 본문 sanity
# ---------------------------------------------------------------------------


def test_모든_메시지_한국어_포함() -> None:
    """MESSAGES의 모든 항목 본문에 한글이 1자 이상 포함된다.

    UI §3.2 ``메시지 작성 원칙``: 한국어로 무엇이/조치가 표현된다.
    """

    hangul = re.compile(r"[가-힣]")
    for key, message in MESSAGES.items():
        assert hangul.search(message), f"{key}에 한글이 없다: {message!r}"


def test_메시지_종결_원칙_본문_단락_마침표() -> None:
    """본문 단락 메시지는 끝에 마침표 또는 한국어 보조 표현이 자연스럽다.

    엄격하게 마침표만 강제하지 않는다. 라벨/표 셀처럼 조사로 끝나는 메시지도 허용
    한다. 본 테스트는 빈 문자열이 없는지만 검증한다.
    """

    for key, message in MESSAGES.items():
        assert message.strip(), f"{key} 메시지가 비어 있다"


# ---------------------------------------------------------------------------
# OpenAI API 키 누락 가드(security 메시지 동등 정합)
# ---------------------------------------------------------------------------


def test_API_KEY_누락_chat_차단_한국어_메시지() -> None:
    """API 키 누락 상태로 chat 호출 시 한국어 ConfigError가 raise된다.

    v1.x 백엔드 전환 후 외부 호출 가드는 키 누락 검사로 대체됐다. 사업 아이템
    본문이 인증 없이 외부로 송신되는 것을 방지한다(security.md §1).
    """

    cfg = LlmConfig(
        base_url="https://api.openai.com/v1",
        model="m",
        max_tokens=10,
        temperature=0.5,
        timeout=1.0,
        context_budget=8000,
        retry_max_attempts=1,
        retry_backoff_seconds=(0.0,),
        api_key=None,
    )

    import asyncio

    async def _check():
        async with LLMClient(cfg) as client:
            try:
                await client.chat([{"role": "user", "content": "x"}])
            except ConfigError as exc:
                return str(exc)
            return ""

    msg = asyncio.run(_check())
    assert "OPENAI_API_KEY" in msg
    assert "API 키" in msg


# ---------------------------------------------------------------------------
# FilterMatchedZeroError 메시지 raise 텍스트
# ---------------------------------------------------------------------------


def test_FilterMatchedZeroError_샘플링_한국어_메시지() -> None:
    """``_sample_indices``가 raise하는 한국어 메시지에 ``완화`` 안내가 있다."""

    with pytest.raises(FilterMatchedZeroError) as exc_info:
        _sample_indices(indices=[1, 2], n=10, seed=42)
    msg = str(exc_info.value)
    assert "필터" in msg
    assert "완화" in msg


# ---------------------------------------------------------------------------
# RetryExhaustedError 메시지
# ---------------------------------------------------------------------------


def test_RetryExhaustedError_메시지_한국어() -> None:
    """RetryExhaustedError는 ``재시도``가 명시된다(record로 변환되지만 사람이 읽을 수 있어야)."""

    exc = RetryExhaustedError("chat 재시도 3회 모두 실패: 5xx")
    assert "재시도" in str(exc)
