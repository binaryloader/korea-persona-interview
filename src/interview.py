"""멀티턴 인터뷰 세션과 단일턴 구조화 요약(ADR-001).

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

application 계층이며, infrastructure(``LLMClient``, OpenAI 호환 클라이언트)와
domain(``PersonaMeta``, ``InterviewRecord`` 등)을 조합한다(architecture.md §1, §2).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from ._json_utils import extract_json_object
from .config import AppConfig, InterviewConfig, LlmConfig
from .logging_setup import mask_name, mask_persona_id, mask_product

if TYPE_CHECKING:  # pragma: no cover - 타입 체크 전용 import
    from .llm_backend import LLMBackend
from .models import (
    ChatResponse,
    ConfigError,
    Flags,
    InterviewRecord,
    MessageEntry,
    PersonaMeta,
    RawResponse,
    RetryExhaustedError,
    ServerNotReachableError,
    StructuredSummary,
    StructuredSummaryParseError,
    TokenUsage,
)


logger = logging.getLogger(__name__)


# 자동 follow-up trigger가 발동했을 때 모델에 추가로 보내는 기본 user 발화.
# 단일 정본은 ``InterviewConfig.auto_follow_up_text``이며, ``InterviewSession``은
# 항상 config에서 읽는다. 본 모듈 레벨 상수는 backward import 호환을 위해
# 그대로 둔다.
AUTO_FOLLOW_UP_PROMPT = "조금만 더 자세히 말씀해 주실 수 있을까요?"


# 단일턴 응답 포맷 분리용 정규식. 라인 시작에 숫자와 ``.`` 또는 ``)``가 오는
# 패턴(예: ``1.``, ``2)``, ``3.``)을 잡고 다음 번호 마커 또는 입력 끝까지를
# 본문으로 읽는다. MULTILINE + DOTALL 플래그를 함께 사용해 라인 시작 기준으로만
# 앵커링하면서 줄바꿈을 가로지를 수 있다. 본문 중간에 등장하는 번호 인용이
# 분리자로 오해되지 않도록 한 안전 장치다.
_NUMBERED_SEGMENT_RE = re.compile(
    r"^\s*(\d+)[.)]\s*(.+?)(?=^\s*\d+[.)]|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _parse_single_turn_response(text: str, expected_count: int) -> tuple:
    """단일턴 응답 텍스트를 질문별 답변 청크로 분리한다.

    인자:
        text: LLM 응답 본문. 시스템 프롬프트가 모델에게 ``1. ... 2. ... 3. ...``
            번호 segment 형식을 출력하도록 지시한다.
        expected_count: caller가 기대하는 segment 수(메인 질문 + 공유 follow-up).

    반환:
        ``(answers, parse_failed)``. 성공 시 ``answers``는 ``expected_count``
        길이 리스트로, 각 슬롯 0..N-1에 trim된 segment 본문이 들어간다. 실패
        시에는 마지막 슬롯에 전체 응답 텍스트가 들어가고 나머지는 빈 문자열,
        ``parse_failed``는 True가 된다. 데이터를 잃지 않으면서 사후 분석에서
        식별 가능하도록 하기 위함이다.
    """

    if expected_count <= 0:
        return [], False

    matches = _NUMBERED_SEGMENT_RE.finditer(text or "")
    found: dict = {}
    for m in matches:
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        # 1-based 번호를 0-based question_index로 변환한다.
        zero_idx = idx - 1
        if 0 <= zero_idx < expected_count:
            # 같은 번호가 두 번 등장하면 마지막 매칭이 우선이다(LLM이 자가
            # 정정해 다시 적는 사례를 흡수).
            found[zero_idx] = m.group(2).strip()

    # 모든 인덱스를 채웠는지 확인. 하나라도 빠지면 fallback.
    if len(found) == expected_count and all(
        i in found for i in range(expected_count)
    ):
        return [found[i] for i in range(expected_count)], False

    # fallback: 통째 텍스트를 마지막 question에 담고 나머지는 빈 문자열로.
    # 데이터를 잃지 않으면서도 parse_failed 플래그로 사후 분석에서 식별
    # 가능하게 한다.
    fallback_answers = [""] * expected_count
    fallback_answers[-1] = (text or "").strip()
    return fallback_answers, True


# 구조화 요약 출력 스키마. 모델이 자유 서술 대신 정해진 JSON만 출력하도록 강제한다.
# 키 순서/타입은 PRD §5.4와 ``StructuredSummary`` dataclass에 맞춘다.
_SUMMARY_SCHEMA_HINT = (
    "{\n"
    '  "intent": "positive | neutral | negative",\n'
    '  "acceptable_price_signal": "cheap | fair | expensive | null",\n'
    '  "willingness_to_pay": 정수 또는 null(원화 KRW 기준 월/회 1회 지불 의사),\n'
    '  "willingness_to_pay_currency": "KRW",\n'
    '  "rejection_reasons": ["거절 사유 1", "거절 사유 2", ...],\n'
    '  "one_line": "인터뷰 한 줄 요약(한국어, 최대 80자)"\n'
    "}"
)


# 17개 시도 짧은 표기. drift 감지가 응답에서 페르소나 거주지가 아닌 다른 시도에
# 살고 있다고 단언하는 케이스를 잡는 데 사용한다(TDD §8.2). 데이터셋은 짧은
# 표기만 저장하지만 응답은 짧거나 풀네임을 쓸 수 있으므로 caller가 substring
# 의미로 본 리스트를 비교한다.
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


# gender/age/region drift 축이 사용하는 1인칭 주어 토큰 패턴. cohabitation 축은
# 본 패턴 대신 전용 정밀 정규식(_SOLO_ASSERTION_RE / _COHABIT_ASSERTION_RE)을
# 사용한다. cohabitation에서 본 광범위 패턴을 재사용하면 라운드 G에서 제거한
# false positive가 재현된다(응답이 product 키워드로 ``1인 가구용``을 언급하면
# 단독 거주 체크가 잘못 트리거되는 문제).
#
# 매치 윈도우는 문장 구두점(``.``/``!``/``?``)으로 제한된다. 이전 30자 윈도우는
# 너무 넓어 문장 경계를 넘어 잘못 매칭되었다.
_SELF_INTRO_PATTERN = re.compile(
    r"(?:저는|나는|제가|내가)\s*([^\.\?!\n,]{0,30})"
)


# 문장 boundary. ``.``/``!``/``?``과 그 뒤 공백을 분리자로 본다.
_SENTENCE_SPLIT_RE = re.compile(r"[\.!?]\s*")


# family_type 가족 동거 표현. 데이터셋에 등장하는 표기를 망라한다.
# ``혼자 거주``/``1인 가구``는 단독 거주를 의미하므로 본 집합에 포함하지 않는다.
_FAMILY_COHABITATION_TOKENS: tuple = (
    "배우자",
    "자녀",
    "부모",
    "어머니",
    "아버지",
    "조부모",
    "형제",
    "자매",
    "친척",
    "가족",
)


# 거주 형태 모순 감지는 단언 방향(긍정/부정)을 분리해 처리한다. 동일 토큰이
# 부정문에 들어오면 실제로 페르소나와 정합한 답변일 수 있다(예: 가족 동거
# 페르소나가 ``1인 가구가 아니라서``라고 답하는 경우 → drift False).
#
# 매칭 분류는 아래와 같다.
#
# - solo_assertion: 단독 거주 긍정 단언("저는 혼자 사", "저는 1인 가구라").
#   가족 동거 페르소나가 본 단언을 보이면 drift True
# - cohabit_assertion: 가족 동거 긍정 단언("저는 가족과 살아", "남편과 살아").
#   단독 거주 페르소나가 본 단언을 보이면 drift True
# - 부정 단언("1인 가구가 아니", "혼자 살지 않")은 두 페르소나 모두에게
#   정합 또는 무관이라 drift 트리거에서 제외한다
#
# false positive 방지를 위해 아래 패턴은 trigger에서 제외한다.
#
# - 3인칭 언급(예: ``혼자 사시는 분들에겐 좋은 서비스``)
# - 행동 표현(예: ``혼자서 끼니를 해결할 수 있기 때문에``의 ``혼자서`` + 비-거주 동사)
# - 응답에 product 키워드만 등장하고 본인 단언 1인칭 동사는 없는 경우
#   (예: ``저는 부모님과 같이 살고 있어서 1인 가구용 반찬 서비스는``)


# 단독 거주 긍정 단언 정규식. 본 패턴이 한 문장 안에서 매칭되면 응답자가 본인을
# 단독 거주(1인 가구)라고 단언하는 것으로 본다. 가족 동거 페르소나에서 본
# 매칭이 발견되면 drift다.
#
# 분기는 두 가지다.
#
# 1. ``저는/나는/제가/내가/난`` 류 1인칭 주어 + ``혼자/홀로`` + 거주 동사
#    (살/사는/사시/사니/살고/살아/지내/거주/지냄). ``혼자 사시는 분들`` 같은
#    3인칭(``분/사람/이들``)은 ``혼자\s*살``과 인접한 ``분|사람|이들`` 매칭으로
#    별도 가드한다
# 2. ``저는/제가/나는/내가/난`` 류 1인칭 주어 + ``1인 가구/일인 가구/독거``
#    + 단언 동사/계사(``라|이|입|이라|예요|에요|입니다|라서|이라서``)
_SOLO_ASSERTION_RE = re.compile(
    r"(?:저는|나는|제가|내가|난)\s*"
    r"(?:"
    r"(?:혼자|홀로)\s*(?:살고|살아|살며|살아요|살아서|살아도|살았|사니|사니까|"
    r"살|사는|사시|살게|살지|지내|지내고|지내며|거주)"
    r"|"
    r"(?:1인\s*가구|일인\s*가구|독거)\s*(?:라|이|입|이라|예요|에요|입니다|라서|이라서|이니|이니까)"
    r")"
)


# 단독 거주 부정 단언 정규식. 본 패턴이 매칭되면 응답자가 단독 거주를 부정하는
# 것이므로 가족 동거 페르소나와 정합한 답변이라 drift 트리거에서 제외한다.
# 예: ``저는 1인 가구가 아니라서``, ``혼자 살지 않아서``.
_SOLO_NEGATION_RE = re.compile(
    r"(?:저는|나는|제가|내가|난|저희는)?\s*"
    r"(?:"
    r"(?:혼자|홀로)\s*(?:살지\s*않|살고\s*있지\s*않|사는\s*것\s*아니|"
    r"살게\s*된\s*건\s*아니)"
    r"|"
    r"(?:1인\s*가구|일인\s*가구|독거)\s*(?:가\s*아니|이\s*아니|는\s*아니|은\s*아니)"
    r")"
)


# 가족 동거 긍정 단언 정규식. 본 패턴이 매칭되면 응답자가 본인을 가족과 함께
# 거주한다고 단언하는 것이다. 단독 거주 페르소나에서 매칭되면 drift다.
#
# 1인칭 주어 + (가족 토큰)와/이랑 + (같이/함께)? + 거주 동사 형태로 좁힌다.
# 가족 토큰은 가족/부모(님)/배우자/남편/아내/아이/아이들/어머니/아버지/엄마/
# 아빠/조부모/형제/자매/친척까지 포함한다.
_COHABIT_ASSERTION_RE = re.compile(
    r"(?:저는|나는|제가|내가|난|저희는|우리는)\s*"
    r"(?:가족|부모님?|배우자|남편|아내|아이|아이들|어머니|아버지|엄마|아빠|"
    r"조부모|형제|자매|친척)(?:과|와|이랑|랑|하고)?\s*"
    r"(?:같이|함께|동거|모시고)?\s*"
    r"(?:살고|살아|살며|살아요|살아서|살아도|살았|사니|사니까|살|사는|사시|살게|"
    r"살지|지내|지내고|지내며|거주|있어요|있어서|있고|있다)"
)


# 가족 동거 부정 단언 정규식. ``저는 가족과 살지 않아``류. 단독 거주 페르소나에서
# 발견되면 정합이라 drift 트리거에서 제외한다.
_COHABIT_NEGATION_RE = re.compile(
    r"(?:저는|나는|제가|내가|난|저희는|우리는)\s*"
    r"(?:가족|부모님?|배우자|남편|아내|아이|아이들|어머니|아버지|엄마|아빠|"
    r"조부모|형제|자매|친척)(?:과|와|이랑|랑|하고)?\s*"
    r"(?:같이|함께)?\s*"
    r"(?:살지\s*않|살고\s*있지\s*않|사는\s*것\s*아니|있지\s*않)"
)


# ---------------------------------------------------------------------------
# 시스템 프롬프트 빌드
# ---------------------------------------------------------------------------


# 시스템 프롬프트 템플릿 in-memory 캐시. 프로세스 단위로 디스크 read를 1회만
# 수행한다(매 인터뷰 호출마다 디스크 I/O 회피). 키는 (resolved_path, mtime_ns)
# 튜플이라 사용자가 파일을 수정하면 자동으로 캐시가 무효화된다.
_SYSTEM_PROMPT_TEMPLATE_CACHE: dict = {}


# product/질문 본문 길이 상한. 사용자가 의도치 않게 거대한 본문을 넣어 토큰
# 폭증을 일으키는 사례를 방지한다(security.md §3 입력 검증). 한도를 넘는 본문은
# 호출 시점에 ConfigError로 차단해 호출자가 즉시 인지할 수 있게 한다.
_MAX_PRODUCT_LENGTH = 2000
_MAX_QUESTION_LENGTH = 2000


# 시스템 프롬프트 템플릿이 ``[페르소나 정보]``/``[인터뷰 주제]`` 같은 마커로
# 가변 본문 영역을 구분한다. product/질문 본문이 동일한 마커 텍스트를 그대로
# 포함하면 모델이 새 시스템 지시로 잘못 해석할 수 있다(prompt injection).
# escape는 마커의 첫 글자를 zero-width space로 갈아 끼워 형태는 보존하면서
# 마커 일치를 깨뜨린다.
_PROMPT_INJECTION_MARKERS: tuple = (
    "[페르소나 정보]",
    "[인터뷰 주제]",
    "[말투와 1인칭 일관성 지침]",
    "[답변 내용 지침]",
    "[출력 형식]",
)
_ZERO_WIDTH_SPACE = "​"


def _sanitize_user_text(text: str, *, max_length: int, label: str) -> str:
    """길이 검증 + 시스템 프롬프트 마커 escape를 한 번에 수행한다.

    호출자(InterviewSession.__init__, run_batch 등)에서 product/questions을
    검증할 때 사용한다.

    Raises:
        ConfigError: 본문이 비어 있거나 ``max_length``를 초과하거나 str이 아님.
    """

    if text is None:
        raise ConfigError(f"{label}이 비어 있다")
    if not isinstance(text, str):
        raise ConfigError(f"{label}은 str이어야 한다: {type(text).__name__}")
    if len(text) > max_length:
        raise ConfigError(
            f"{label}이 {max_length}자 상한을 초과했다(입력 {len(text)}자). "
            "본문을 줄여 다시 호출해 주세요"
        )
    cleaned = text
    for marker in _PROMPT_INJECTION_MARKERS:
        if marker in cleaned:
            replacement = _ZERO_WIDTH_SPACE + marker[1:]
            cleaned = cleaned.replace(marker, replacement)
    return cleaned


# 프로젝트 루트(본 모듈이 ``src/interview.py``라 ``parents[1]``이 루트). yaml에
# 적은 상대 경로(``prompts/system_prompt.txt``)를 프로젝트 루트 기준으로
# 해석할 때 사용한다. cwd 기반 해석은 작업 디렉토리에 따라 결과가 달라져
# 테스트/CI 격리를 깬다.
from pathlib import Path as _Path  # noqa: E402

_PROJECT_ROOT = _Path(__file__).resolve().parents[1]


def _load_system_prompt_template(path_str: str) -> str:
    """시스템 프롬프트 템플릿 파일을 읽어 캐시한다.

    Args:
        path_str: yaml의 ``interview.system_prompt_path``. 절대 경로 또는 프로젝트
            루트 기준 상대 경로를 모두 받는다.

    Returns:
        템플릿 본문 문자열(``{persona_json}``, ``{product}`` placeholder 포함).

    Raises:
        ConfigError: 파일이 없거나 읽기 실패. 사용자에게 친절한 한국어 안내를 단다.

    pip-installed 사용자에 대한 fallback:
        프로젝트 루트 경로에서 파일을 찾지 못하고 본 함수가 default 경로
        ``prompts/system_prompt.txt``를 받았다면 패키지 내부의
        ``src._prompts.system_prompt`` 리소스로 fallback한다. 사용자가 명시
        경로를 지정한 경우(default와 다른 경로)에는 fallback을 사용하지 않고
        ConfigError로 차단해 의도치 않게 패키지 내부 템플릿이 사용되는 일을
        막는다.
    """

    candidate = _Path(path_str)
    if not candidate.is_absolute():
        candidate = _PROJECT_ROOT / candidate

    try:
        mtime = candidate.stat().st_mtime_ns
    except FileNotFoundError:
        # default 경로 + 패키지 내부 리소스로 fallback.
        if path_str == "prompts/system_prompt.txt":
            packaged = _read_packaged_system_prompt()
            if packaged is not None:
                return packaged
        raise ConfigError(
            f"시스템 프롬프트 템플릿 파일을 찾을 수 없습니다: {candidate}. "
            "config.yaml의 interview.system_prompt_path를 확인해 주세요. "
            "기본 템플릿 경로는 'prompts/system_prompt.txt'입니다"
        )
    except OSError as exc:
        raise ConfigError(
            f"시스템 프롬프트 템플릿 파일에 접근할 수 없습니다: {candidate}: {exc}"
        ) from exc

    cache_key = (str(candidate.resolve()), mtime)
    cached = _SYSTEM_PROMPT_TEMPLATE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"시스템 프롬프트 템플릿 파일을 읽을 수 없습니다: {candidate}: {exc}"
        ) from exc

    if "{persona_json}" not in text or "{product}" not in text:
        raise ConfigError(
            "시스템 프롬프트 템플릿에 {persona_json} 또는 {product} placeholder가 없다. "
            f"파일 경로: {candidate}"
        )

    _SYSTEM_PROMPT_TEMPLATE_CACHE[cache_key] = text
    return text


def _read_packaged_system_prompt() -> Optional[str]:
    """패키지 내부 ``src._prompts.system_prompt`` 리소스를 읽어 반환한다.

    pip-installed 환경에서 프로젝트 루트 경로가 부재할 때 fallback으로
    사용된다. 캐시 키는 패키지 리소스 경로 한 가지로 고정한다(파일 mtime은
    importlib.resources 인터페이스가 노출하지 않음).

    Returns:
        템플릿 본문 또는 placeholder가 누락되었거나 리소스가 없을 때 ``None``.
    """

    cache_key = ("__packaged__", "src._prompts.system_prompt.txt")
    cached = _SYSTEM_PROMPT_TEMPLATE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        from importlib.resources import files

        resource = files("src._prompts").joinpath("system_prompt.txt")
        text = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None
    except Exception:  # noqa: BLE001 - 패키지 데이터 누락 등 fallback 안전망
        return None

    if "{persona_json}" not in text or "{product}" not in text:
        return None

    _SYSTEM_PROMPT_TEMPLATE_CACHE[cache_key] = text
    return text


def clear_system_prompt_cache() -> None:
    """프로세스 캐시를 비운다. 테스트 격리와 외부 수동 무효화용."""

    _SYSTEM_PROMPT_TEMPLATE_CACHE.clear()


def build_system_prompt(
    persona: PersonaMeta,
    product: str,
    persona_fields: tuple,
    field_map: dict,
    system_prompt_path: str = "prompts/system_prompt.txt",
) -> str:
    """시스템 프롬프트 템플릿 파일에 페르소나 정보를 주입한다.

    템플릿은 ``prompts/system_prompt.txt``(기본)에서 읽으며, 본문에는
    ``{persona_json}``과 ``{product}`` 두 개의 str.format placeholder가 들어 있어야
    한다. 사용자는 본 파일을 직접 편집해 시스템 프롬프트의 톤/지침을 도메인에
    맞게 조정할 수 있다.

    기본 묶음은 인구 통계 7개 필드와 ``persona``(요약 자유 서술)다(TDD §1.4).
    토글 키워드(``professional``/``sports``/``arts``/``travel``/``culinary``/
    ``family``)가 ``persona_fields``에 있으면 해당 자유 서술 컬럼을 raw에서
    꺼내 추가한다.

    Args:
        persona: 페르소나 메타와 raw dict.
        product: 사업 아이템 한 줄 설명. 시스템 프롬프트의 인터뷰 주제로 사용.
        persona_fields: 토글 키워드 튜플. ``("summary",)``가 기본값.
        field_map: ``DatasetConfig.field_map``. 토글 키워드 → 데이터셋 컬럼 매핑.
        system_prompt_path: 템플릿 파일 경로. 절대 경로 또는 프로젝트 루트 기준
            상대 경로. ``InterviewConfig.system_prompt_path``에서 받는다.

    Returns:
        시스템 프롬프트 문자열.

    Raises:
        ConfigError: 템플릿 파일 없음/접근 실패/placeholder 누락.
    """

    # 기본 묶음: 인구 통계 + summary 페르소나(TDD §1.4).
    # family_type/housing_type은 1인 가구 여부와 주거 유형을 모델이 추론으로
    # 채우지 않도록 명시적으로 노출한다(거주 형태를 임의로 추측하던 회귀 차단).
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
    if persona.family_type:
        persona_obj["family_type"] = persona.family_type
    if persona.housing_type:
        persona_obj["housing_type"] = persona.housing_type

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

    # HANDOFF.md §시스템 프롬프트 템플릿 + 페르소나 1인칭 일관성 강화 지침을
    # 외부 파일로 분리했다. OpenAI prompt caching 적합 구조(정적 prefix가 앞쪽,
    # 가변 부분이 뒤쪽)는 템플릿 파일 자체에 박혀 있다. ``persona_json``과
    # ``product``만 가변이라 같은 템플릿을 반복 호출하면 OpenAI가 자동으로
    # 입력 토큰 단가의 50%를 환급한다(prefix 1024 토큰 이상 + 동일 prefix 반복).
    template = _load_system_prompt_template(system_prompt_path)
    try:
        return template.format(persona_json=persona_json, product=product).rstrip()
    except KeyError as exc:
        # str.format 안에 알려지지 않은 placeholder가 있는 경우.
        raise ConfigError(
            f"시스템 프롬프트 템플릿에 알려지지 않은 placeholder가 있다: {exc}. "
            "허용 placeholder: {persona_json}, {product}"
        ) from exc


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

    # 진입 시 1회 전체 토큰을 계산하고, 페어 제거 시 제거된 메시지들의 토큰만
    # 차감한다(O(n) 보장). 기존 구현은 매 iteration마다 ``head + body`` 전체를
    # 재계산해 O(n²)였다.
    total_tokens = estimate_messages_tokens(messages)
    if total_tokens <= max_tokens:
        return list(messages), False

    body: list = list(messages)
    head: list = []
    first = messages[0]
    first_role = first.role if isinstance(first, MessageEntry) else first.get("role")
    if first_role == "system":
        head = [body.pop(0)]

    truncated = False

    # 가장 오래된 user/assistant 페어를 2개씩 제거한다.
    while total_tokens > max_tokens and len(body) >= 2:
        # 제거 대상 페어의 토큰 합만 차감한다(전체 재계산 회피).
        total_tokens -= estimate_messages_tokens(body[:2])
        body = body[2:]
        truncated = True

    # 페어가 남지 않을 정도로 빠진 경우 마지막 단일 메시지까지 제거한다.
    while total_tokens > max_tokens and body:
        total_tokens -= estimate_messages_tokens(body[:1])
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


_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+")
_LETTER_WORD_RE = re.compile(r"[가-힣A-Za-z]+")


def _english_ratio(text: str, occupation_whitelist: frozenset = frozenset()) -> float:
    """전체 단어(한글+영문) 대비 영문 단어 비율(TDD §8.2 명세).

    글자 단위 비율은 ``solo`` 같은 4글자 영단어가 한국어 1문장 안에 섞여도
    임계값을 넘기 어려워 false negative가 잦았다. 단어 단위 비율은 영어 단어
    개수를 직접 세므로 ``I think this is solo``처럼 영어 위주 응답을 더 잘
    잡아낸다. 한자/숫자/구두점은 분모에서 제외한다.

    ``occupation_whitelist``로 페르소나 직업명에 등장하는 영문 토큰을 분모에서
    제외할 수 있다(``IT 컨설턴트``, ``UX 디자이너``류 페르소나가 본인 직업명을
    자연스럽게 사용해도 false positive로 drift 처리되지 않게 한다). 화이트리스트
    토큰은 본인 단언 여부와 무관하게 분모에서 제외한다.
    """

    if not text:
        return 0.0
    words_total = _LETTER_WORD_RE.findall(text)
    english_words = _ENGLISH_WORD_RE.findall(text)
    if occupation_whitelist:
        # 화이트리스트 매칭은 case-insensitive로 본다.
        words_total = [w for w in words_total if w.lower() not in occupation_whitelist]
        english_words = [w for w in english_words if w.lower() not in occupation_whitelist]
    if not words_total:
        return 0.0
    return len(english_words) / len(words_total)


def _occupation_english_tokens(persona: PersonaMeta) -> frozenset:
    """페르소나 직업명에 들어 있는 영문 토큰을 lower-case set으로 반환한다.

    ``IT 컨설턴트`` → ``{'it'}``, ``UX/UI 디자이너`` → ``{'ux', 'ui'}``. 직업명
    필드가 비면 빈 set을 돌려준다.
    """

    if not persona or not persona.occupation:
        return frozenset()
    return frozenset(t.lower() for t in _ENGLISH_WORD_RE.findall(persona.occupation))


def _age_bucket_for_drift(age: int) -> str:
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


def _is_solo_living(family_type: Optional[str]) -> bool:
    """``family_type`` 값이 단독 거주(1인 가구)에 해당하는지 판정한다.

    데이터셋 표기 ``혼자 거주``/``1인 가구``를 모두 인식한다. ``None`` 또는
    빈 문자열이면 판정 불가로 보고 False를 반환한다.
    """

    if not family_type:
        return False
    return ("혼자 거주" in family_type) or ("1인 가구" in family_type)


def _is_cohabiting(family_type: Optional[str]) -> bool:
    """``family_type``이 가족 동거에 해당하는지 판정한다.

    ``배우자/자녀/부모/어머니/아버지/조부모/형제/자매/친척/가족`` 토큰이 하나라도
    들어 있으면 동거로 본다. 단독 거주 표기와 겹치는 경우(``배우자와 거주``
    옆에 ``혼자 거주`` 가 동시에 있을 일은 없지만)는 단독 거주가 우선한다.
    """

    if not family_type:
        return False
    if _is_solo_living(family_type):
        return False
    return any(token in family_type for token in _FAMILY_COHABITATION_TOKENS)


def _split_sentences(text: str) -> list:
    """문장 boundary로 텍스트를 자른다.

    ``.``/``!``/``?``과 그 뒤 공백을 분리자로 본다. 빈 토막은 제거한다.
    거주 형태 단언 검사는 같은 문장 안에서만 매칭해야 다음 문장 토큰이 자기
    단언 컨텍스트로 누설되는 false positive를 막을 수 있다.
    """

    if not text:
        return []
    return [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _has_solo_living_assertion(text: str) -> bool:
    """본문에 단독 거주 1인칭 긍정 단언이 한 문장이라도 들어 있는지 판정한다.

    부정 단언(``혼자 살지 않``, ``1인 가구가 아니``)이 같은 문장에 있으면
    그 문장은 정합으로 보고 매칭에서 제외한다. 단독 거주 페르소나가 본 단언을
    하면 정합이지만, 가족 동거 페르소나가 본 단언을 하면 drift다.
    """

    for sentence in _split_sentences(text):
        if _SOLO_NEGATION_RE.search(sentence):
            continue
        if _SOLO_ASSERTION_RE.search(sentence):
            return True
    return False


def _has_cohabit_assertion(text: str) -> bool:
    """본문에 가족 동거 1인칭 긍정 단언이 한 문장이라도 들어 있는지 판정한다.

    부정 단언(``가족과 살지 않``)이 같은 문장에 있으면 정합으로 보고 매칭에서
    제외한다. 가족 동거 페르소나가 본 단언을 하면 정합이지만, 단독 거주
    페르소나가 본 단언을 하면 drift다.
    """

    for sentence in _split_sentences(text):
        if _COHABIT_NEGATION_RE.search(sentence):
            continue
        if _COHABIT_ASSERTION_RE.search(sentence):
            return True
    return False


# 1인칭 주어 토큰. 같은 문장에 본 패턴이 매칭되어야 ``저는 OO``류 자기 단언으로
# 본다. 라운드 G10에서 연령/성별/지역 축에도 적용했다.
_FIRST_PERSON_SUBJECT_RE = re.compile(r"(?:저는|나는|제가|내가|난)")


# 3인칭 일반화 표현. ``다른 사람들``/``일반적으로``/``남들``류가 같은 문장에
# 들어오면 본인 단언이 아닌 generic 서술일 가능성이 크다. 모든 축에서 보수적
# 으로 trigger에서 제외한다.
_GENERIC_THIRD_PERSON_RE = re.compile(
    r"(?:다른\s*사람|일반적으로|보통\s*사람|남들|타인)"
)


def _has_age_bucket_assertion(sentence: str, bucket_label: str) -> bool:
    """문장 안에서 ``저는 {bucket_label}`` 형태 자기 단언이 발견되면 True.

    1인칭 주어가 같은 문장에 있어야 하며, 부정문(``20대가 아니라``)은 정합으로
    보고 trigger에서 제외한다.
    """

    if bucket_label not in sentence:
        return False
    if not _FIRST_PERSON_SUBJECT_RE.search(sentence):
        return False
    # 부정문 가드: 같은 문장에 ``아니``류가 들어 있으면 정합으로 본다.
    if re.search(r"아니|아닌|아닙", sentence):
        return False
    return True


def _has_gender_assertion(sentence: str, opposing_tokens: tuple) -> bool:
    """문장 안에 1인칭 + 반대 성별 토큰 + 단언/계사가 발견되면 True."""

    if not _FIRST_PERSON_SUBJECT_RE.search(sentence):
        return False
    # 부정문 가드.
    if re.search(r"아니|아닌|아닙", sentence):
        return False
    for token in opposing_tokens:
        if token not in sentence:
            continue
        # 계사/단언 어미가 같은 문장에 동반되어야 매칭한다.
        if re.search(
            rf"{re.escape(token)}\s*(?:라|이라|예요|에요|입니다|이에요|이라서|"
            r"라서|이고|입니다만|로서|로요|이니|이니까)",
            sentence,
        ):
            return True
        # 짧은 단언(``저는 남자``).
        if re.search(
            rf"(?:저는|나는|제가|내가|난)\s*{re.escape(token)}(?!\w)",
            sentence,
        ):
            return True
    return False


def _has_region_assertion(sentence: str, own_region: str, others: tuple) -> bool:
    """문장 안에서 자기 시도가 아닌 다른 시도를 거주지로 1인칭 단언하면 True.

    같은 문장에 자기 시도가 함께 등장하면 ``저는 서울 출신이지만 부산에도``류
    false positive를 막기 위해 매칭에서 제외한다.
    """

    if not _FIRST_PERSON_SUBJECT_RE.search(sentence):
        return False
    if own_region and own_region in sentence:
        return False
    # 부정문 가드: 같은 문장에 ``아니``/``아닌``/``아닙``이 들어 있으면 정합으로
    # 본다. 예: ``저는 부산 사람이 아니라서 거기 사정은 잘 모릅니다``.
    if re.search(r"아니|아닌|아닙", sentence):
        return False
    for province in others:
        if province not in sentence:
            continue
        if re.search(
            rf"{re.escape(province)}\s*(?:사람|에\s*살|에서\s*살|에서\s*자랐|"
            r"에서\s*태어|출신|에서\s*나고|에서\s*근무|에\s*거주)",
            sentence,
        ):
            return True
    return False


def detect_persona_drift(
    response: str,
    persona: PersonaMeta,
    english_ratio_threshold: float = 0.30,
    occupation_english_whitelist: bool = True,
) -> bool:
    """페르소나 정면 모순 또는 영어 비율 임계값 초과 여부를 판정한다(TDD §8.2).

    감지 축은 아래와 같다.

    - 영어 비율: ``_english_ratio`` > ``english_ratio_threshold``이면 True
    - 연령대 모순: ``저는 20대``처럼 자기 연령 버킷이 아닌 버킷을 단언
    - 성별 모순: 여자 페르소나가 ``저는 남자``를 단언, 또는 그 반대
    - 지역 모순: 자기 시도가 아닌 다른 시도를 거주지로 단언
    - 거주 형태 모순: family_type이 단독 거주인데 가족 동거 긍정 단언, 또는
      가족 동거인데 단독 거주 긍정 단언. 부정 단언은 정합한 답변이라 trigger
      대상에서 제외한다(예: 가족 동거 페르소나가 ``1인 가구가 아니라서``라고
      답하는 경우 drift False)

    가짜 양성을 줄이기 위해 두 단계로 좁힌다.

    - 연령/성별/지역 축은 ``저는``/``나는``/``제가``/``내가`` 자기 단언 컨텍스트
      30자 윈도우만 검사한다(기존 휴리스틱 유지)
    - 거주 형태 축은 같은 문장(``.``/``!``/``?`` boundary) 안에서 1인칭 주어와
      거주 동사가 함께 등장하는 정밀 정규식만 매칭한다. ``혼자 사시는 분들``
      같은 3인칭 표현, ``혼자서 끼니를 해결`` 같은 행동 표현, 응답에 우연히
      등장한 product 키워드(``1인 가구용``)는 trigger에서 제외된다

    Args:
        response: 모델 응답 본문.
        persona: 페르소나 메타.
        english_ratio_threshold: 영어 비율 임계값(0.0-1.0). 기본 0.30은
            ``InterviewConfig.english_ratio_threshold``의 기본값과 일치한다.
            yaml 또는 사용자 설정에서 조정 가능하다.
    """

    if not response:
        return False

    whitelist = (
        _occupation_english_tokens(persona)
        if occupation_english_whitelist
        else frozenset()
    )
    if _english_ratio(response, whitelist) > english_ratio_threshold:
        return True

    own_bucket = _age_bucket_for_drift(persona.age)
    other_buckets = tuple(b for b in _all_age_buckets() if b != own_bucket)
    own_gender = persona.gender  # "남자" 또는 "여자"
    own_region = persona.region

    # 시도 비교는 prefix 매칭으로 수행한다(데이터셋 표기는 짧은 형태).
    other_provinces = tuple(p for p in _KOREAN_PROVINCES if p != own_region)

    solo_living = _is_solo_living(persona.family_type)
    cohabiting = _is_cohabiting(persona.family_type)

    # 거주 형태 축은 self-intro 컨텍스트와 무관하게 같은 문장 단위 정밀 정규식
    # 으로 검사한다. self-intro 윈도우 30자 안에 product 키워드가 우연히 들어와
    # 단독 거주 단언으로 오인되는 false positive(record 3/4 사례)를 차단한다.
    if cohabiting and _has_solo_living_assertion(response):
        return True
    if solo_living and _has_cohabit_assertion(response):
        return True

    # 라운드 G10: 연령/성별/지역 축도 거주 형태 축과 같은 정밀도로 같은 문장
    # 단위 검사로 갈아 끼웠다. 1인칭 주어, 부정문 가드, 3인칭 제외를 함께
    # 적용해 false positive를 줄인다.
    sentences = _split_sentences(response)
    if not sentences:
        return False

    if own_gender == "여자":
        opposing_gender_tokens: tuple = ("남자", "남성", "아저씨")
    elif own_gender == "남자":
        opposing_gender_tokens = ("여자", "여성", "아줌마")
    else:
        opposing_gender_tokens = ()

    for sentence in sentences:
        # 3인칭 일반화 표현이 같은 문장에 있으면 본 문장은 다른 사람 단언일
        # 가능성이 크다(예: ``다른 사람들은 30대일 수도 있어요``). 모든 축에서
        # 보수적으로 trigger에서 제외한다.
        if _GENERIC_THIRD_PERSON_RE.search(sentence):
            continue

        # 연령대 모순.
        for bucket in other_buckets:
            if bucket == "60대 이상":
                if "60대" in sentence and "이상" in sentence:
                    if _has_age_bucket_assertion(sentence, "60대"):
                        return True
            else:
                if bucket in sentence and _has_age_bucket_assertion(sentence, bucket):
                    return True
        if persona.age >= 30 and _FIRST_PERSON_SUBJECT_RE.search(sentence):
            if "학생이" in sentence or "미성년" in sentence:
                return True

        # 성별 모순.
        if opposing_gender_tokens and _has_gender_assertion(
            sentence, opposing_gender_tokens
        ):
            return True

        # 지역 모순.
        if _has_region_assertion(sentence, own_region, other_provinces):
            return True

    return False


async def review_drift_with_llm(
    response: str,
    persona: PersonaMeta,
    client: "LLMBackend",
    config: LlmConfig,
) -> bool:
    """LLM-as-judge로 페르소나 일관성을 1회 재판정한다.

    휴리스틱이 drift 의심으로 판정한 record에 한해 호출된다(yaml
    ``interview.llm_drift_review: true`` 옵트인). 호출자는 judge가 ``True``를
    돌려주면 drift 라벨을 유지하고, ``False``를 돌려주면 drift 플래그를 해제해
    false positive를 줄인다.

    LLM 응답이 ``judge: drift``/``judge: ok`` 둘 중 하나로만 떨어지도록
    프롬프트를 좁혔다. 그 외 응답은 보수적으로 ``True``(drift 유지)로 본다.

    Raises:
        반환값에 모든 LLM 호출 실패를 흡수한다(``True``로 fallback). 호출 실패가
        다른 task를 죽이지 않게 한다.
    """

    if not response:
        return False

    persona_summary = json.dumps(
        {
            "gender": persona.gender,
            "age": persona.age,
            "region": persona.region,
            "occupation": persona.occupation,
            "family_type": persona.family_type,
            "housing_type": persona.housing_type,
        },
        ensure_ascii=False,
    )

    judge_messages = [
        {
            "role": "system",
            "content": (
                "당신은 인터뷰 검수자입니다. 주어진 페르소나와 인터뷰 답변을 보고 "
                "응답자가 페르소나의 정체성에 충실했는지 한 단어로만 판정하세요. "
                "답변 본문이 페르소나의 연령/성별/지역/직업/가족 형태와 정면으로 "
                "모순되거나, 응답이 모델 디폴트 톤(인공지능 자체 언급, 일반론, "
                "교과서적 답변)을 보이면 'drift'로 판정합니다. 그렇지 않으면 'ok'. "
                "다른 단어, 마크다운, 설명, JSON을 모두 금지합니다."
            ),
        },
        {
            "role": "user",
            "content": (
                f"[페르소나]\n{persona_summary}\n\n"
                f"[답변]\n{response[:1000]}\n\n"
                "판정: drift 또는 ok 중 하나만 출력하세요."
            ),
        },
    ]

    try:
        chat_response = await client.chat(
            judge_messages, max_tokens=10, temperature=0.0
        )
    except Exception:  # noqa: BLE001 - judge 호출 실패는 보수적으로 drift 유지
        logger.warning(
            "LLM-as-judge drift 호출 실패. 보수적으로 drift 라벨 유지",
            extra={"persona_id": persona.persona_id},
        )
        return True

    verdict = chat_response.content.strip().lower()
    return "drift" in verdict and "ok" not in verdict


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
        "- acceptable_price_signal: 응답에 가격 신호가 있으면 cheap/fair/expensive "
        "셋 중 하나, 없으면 null. ``비싸다``/``너무 비싸요``/``부담된다``는 expensive, "
        "``적당``/``합리적``은 fair, ``저렴``/``값싸다``는 cheap. 응답에 가격 언급이 "
        "전혀 없으면 null.\n"
        "- willingness_to_pay: 응답에 명시된 숫자가 있을 때만 정수(원)로 박는다. "
        "정성 신호만 있고 숫자가 없으면 null. 거절한 경우도 null.\n"
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
    price_signal_raw = data.get("acceptable_price_signal")

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

    price_signal: Optional[str] = None
    if isinstance(price_signal_raw, str):
        candidate = price_signal_raw.strip().lower()
        if candidate in ("cheap", "fair", "expensive"):
            price_signal = candidate
        elif candidate in ("", "null", "none"):
            price_signal = None

    try:
        return StructuredSummary(
            intent=str(intent) if intent is not None else "",
            willingness_to_pay=wtp_int,
            willingness_to_pay_currency=str(currency) if currency else "KRW",
            rejection_reasons=reasons_list,
            one_line=str(one_line) if one_line else "",
            acceptable_price_signal=price_signal,
        )
    except ValueError as exc:
        # __post_init__의 enum 검증 실패도 파싱 실패로 본다(retry 대상).
        raise StructuredSummaryParseError(
            f"구조화 요약 검증 실패: {exc}"
        ) from exc


async def summarize_interview(
    messages: list,
    client: "LLMBackend",
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

        async with LLMClient(cfg.llm) as client:
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
        client: "LLMBackend",
        config: AppConfig,
    ) -> None:
        if not questions:
            raise ConfigError("questions가 비어 있다. 1개 이상 지정해 주세요")

        # 라운드 G16: product/questions 본문에 길이 상한과 prompt-injection
        # 마커 escape를 적용한다(security.md §3). 한도를 넘으면 즉시 ConfigError.
        product_clean = _sanitize_user_text(
            product, max_length=_MAX_PRODUCT_LENGTH, label="--product"
        )
        questions_clean = [
            _sanitize_user_text(q, max_length=_MAX_QUESTION_LENGTH, label="질문")
            for q in questions
        ]
        follow_ups_clean = [
            _sanitize_user_text(q, max_length=_MAX_QUESTION_LENGTH, label="follow-up")
            for q in (follow_up_questions or [])
        ]

        self._persona = persona
        self._product = product_clean
        self._questions = questions_clean
        self._follow_ups = follow_ups_clean
        self._client = client
        self._config = config
        self._llm_cfg: LlmConfig = config.llm
        self._interview_cfg: InterviewConfig = config.interview

    async def run(self) -> InterviewRecord:
        """인터뷰 1회를 끝까지 진행하고 ``InterviewRecord``를 반환한다.

        ``config.batch.single_turn``이 True면 모든 질문을 한 번의 chat 호출에
        묶어 처리하는 단일턴 흐름으로 진입한다(``_run_single_turn``). 그렇지
        않으면 v1 기본 멀티턴 흐름(``_run_multi_turn``)을 사용한다.

        에러 처리는 TDD §5.2에 따른다. 내부 예외는 ``status``/``flags``/``error``
        로 변환하고 외부로 누출하지 않는다.
        """

        if self._config.batch.single_turn:
            return await self._run_single_turn()
        return await self._run_multi_turn()

    async def _run_multi_turn(self) -> InterviewRecord:
        """멀티턴 인터뷰(v1 기본). 질문별로 user/assistant 턴을 누적한다."""

        started_at = _now_iso()
        system_prompt = build_system_prompt(
            self._persona,
            self._product,
            self._config.batch.persona_fields,
            self._config.dataset.field_map,
            self._interview_cfg.system_prompt_path,
        )
        messages: list = [MessageEntry(role="system", content=system_prompt)]
        raw_responses: list = []
        flags = Flags()
        status = "completed"
        error_payload: Optional[dict] = None

        # persona_id는 sha256 prefix로 마스킹하고, 인구통계 필드는 DEBUG로 격하
        # 한다(security.md §1, logging.md §1, §2). INFO 라인은 sequence 추적을
        # 가능하게 하되 식별 가능한 인구통계 자체는 노출하지 않는다.
        logger.info(
            "인터뷰 시작",
            extra={
                "persona_id_hash": mask_persona_id(self._persona.persona_id),
                "product": mask_product(self._product),
                "questions_count": len(self._questions),
                "follow_ups_count": len(self._follow_ups),
                "mode": "multi_turn",
            },
        )
        logger.debug(
            "인터뷰 페르소나 인구통계(DEBUG 격하)",
            extra={
                "persona_id_hash": mask_persona_id(self._persona.persona_id),
                "persona_name": mask_name(self._persona.name),
                "persona_age": self._persona.age,
                "persona_gender": self._persona.gender,
                "persona_region": self._persona.region,
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
                (
                    response_text,
                    latency_ms,
                    retry_count,
                    usage,
                ) = await self._call_llm(messages)
                messages.append(
                    MessageEntry(role="assistant", content=response_text)
                )
                raw_responses.append(
                    RawResponse(
                        question_index=q_index,
                        response=response_text,
                        latency_ms=latency_ms,
                        retry_count=retry_count,
                        usage=usage,
                    )
                )

                # 거부 감지(가장 강한 신호. 즉시 중단).
                if detect_refusal(response_text, self._interview_cfg.refusal_keywords):
                    flags = dataclasses.replace(flags, refusal_detected=True)
                    status = "refused"
                    logger.warning(
                        "모델 거부 감지",
                        extra={
                            "persona_id_hash": mask_persona_id(self._persona.persona_id),
                            "question_index": q_index,
                        },
                    )
                    break

                # 페르소나 깨짐 감지(중단하지 않고 플래그만 기록, PRD §5.8).
                if detect_persona_drift(
                    response_text,
                    self._persona,
                    self._interview_cfg.english_ratio_threshold,
                    self._interview_cfg.occupation_english_whitelist,
                ):
                    drift_confirmed = True
                    if self._interview_cfg.llm_drift_review:
                        drift_confirmed = await review_drift_with_llm(
                            response_text,
                            self._persona,
                            self._client,
                            self._llm_cfg,
                        )
                    if drift_confirmed:
                        flags = dataclasses.replace(flags, persona_drift=True)
                        status = "drift"
                        logger.warning(
                            "페르소나 깨짐 감지",
                            extra={
                                "persona_id_hash": mask_persona_id(self._persona.persona_id),
                                "question_index": q_index,
                            },
                        )
                    else:
                        logger.info(
                            "페르소나 깨짐 휴리스틱 trigger되었지만 LLM judge가 ok로 판정",
                            extra={
                                "persona_id_hash": mask_persona_id(self._persona.persona_id),
                                "question_index": q_index,
                            },
                        )

                # 자동 follow-up은 메인 질문 구간(q_index < len(self._questions))
                # 에서만, ``auto_follow_up_max`` 만큼 적용한다(기본 1회).
                if (
                    q_index < len(self._questions)
                    and not flags.auto_follow_up_used
                    and self._interview_cfg.auto_follow_up_max > 0
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
                            "persona_id_hash": mask_persona_id(self._persona.persona_id),
                            "question_index": q_index,
                        },
                    )

                    messages, was_truncated = self._maybe_truncate(messages)
                    if was_truncated:
                        flags = dataclasses.replace(flags, truncated=True)

                    messages.append(
                        MessageEntry(
                            role="user",
                            content=self._interview_cfg.auto_follow_up_text,
                        )
                    )
                    (
                        fu_text,
                        fu_latency_ms,
                        fu_retry,
                        fu_usage,
                    ) = await self._call_llm(messages)
                    messages.append(MessageEntry(role="assistant", content=fu_text))
                    # 같은 question_index, retry_count는 1 증가로 표기한다.
                    raw_responses.append(
                        RawResponse(
                            question_index=q_index,
                            response=fu_text,
                            latency_ms=fu_latency_ms,
                            retry_count=fu_retry + 1,
                            usage=fu_usage,
                        )
                    )

                    # follow-up 응답에서도 거부/drift는 감지한다.
                    if detect_refusal(
                        fu_text, self._interview_cfg.refusal_keywords
                    ):
                        flags = dataclasses.replace(flags, refusal_detected=True)
                        status = "refused"
                        break
                    if detect_persona_drift(
                        fu_text,
                        self._persona,
                        self._interview_cfg.english_ratio_threshold,
                        self._interview_cfg.occupation_english_whitelist,
                    ):
                        drift_confirmed = True
                        if self._interview_cfg.llm_drift_review:
                            drift_confirmed = await review_drift_with_llm(
                                fu_text,
                                self._persona,
                                self._client,
                                self._llm_cfg,
                            )
                        if drift_confirmed:
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
                    "persona_id_hash": mask_persona_id(self._persona.persona_id),
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
                    "persona_id_hash": mask_persona_id(self._persona.persona_id),
                    "reason": str(exc),
                },
            )

        # 구조화 요약은 status가 ``completed``/``drift``/``refused``일 때 시도한다.
        # ``failed``(LLM 호출 자체 실패)는 본 인터뷰에 답이 없는 상태라 생략한다.
        structured_summary: Optional[StructuredSummary] = None
        if status in ("completed", "drift", "refused") and raw_responses:
            try:
                structured_summary = await summarize_interview(
                    messages, self._client, self._llm_cfg
                )
            except (
                RetryExhaustedError,
                ServerNotReachableError,
                ConfigError,
                StructuredSummaryParseError,
            ) as exc:
                # summarize_interview 자체가 실패해도 인터뷰 본체는 보존한다.
                logger.warning(
                    "구조화 요약 단계 예외(structured_summary=None로 보존)",
                    extra={
                        "persona_id_hash": mask_persona_id(self._persona.persona_id),
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
                "persona_id_hash": mask_persona_id(self._persona.persona_id),
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
        """``LLMClient.chat``을 호출하고 응답 메타를 반환한다.

        OpenAI 호환 dict 형식으로 변환하여 보낸다.

        Returns:
            ``(text, latency_ms, retry_count, usage)``.
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
            chat_response.usage,
        )

    async def _run_single_turn(self) -> InterviewRecord:
        """단일턴 인터뷰 흐름(PRD §5.1, §5.9의 ``--single-turn``).

        모든 질문을 한 번의 chat 호출에 묶어 처리한다. 자동 follow-up은
        비활성화한다(한 번에 다 묶어서 진행하므로). 사용자 정의 follow-up은
        같은 묶음 끝에 추가 질문으로 합쳐진다.

        시스템 프롬프트는 멀티턴과 동일한 ``build_system_prompt``를 사용하되
        본 메서드에서 "각 질문에 번호 순서대로 답변" 형식 지침을 추가 user
        메시지에 넣는다. 응답 텍스트는 ``^\\s*(\\d+)[.)]\\s*...`` 정규식으로
        question_index별 응답으로 분리한다. 파싱이 한 항목이라도 실패하면
        flags.parse_failed=True로 표시하고, 통째 텍스트를 마지막 question에
        담아 fallback한다(데이터를 잃지 않음).

        Returns:
            ``InterviewRecord``. 멀티턴과 같은 스키마.
        """

        started_at = _now_iso()
        system_prompt = build_system_prompt(
            self._persona,
            self._product,
            self._config.batch.persona_fields,
            self._config.dataset.field_map,
            self._interview_cfg.system_prompt_path,
        )
        all_questions = list(self._questions) + list(self._follow_ups)

        # 단일턴 user 메시지: 모든 질문을 번호 순서로 나열하고 동일 형식의 답변
        # 출력을 강제한다. 멀티턴과 비교해 messages 배열이 항상 [system, user,
        # assistant] 3개라 prompt cache 히트가 잘 잡히고 비용이 약 1/N으로
        # 줄어든다(N은 질문 수, 누적 컨텍스트 미발생 가정).
        numbered = "\n".join(
            f"{i + 1}. {q}" for i, q in enumerate(all_questions)
        )
        user_prompt = (
            "아래 질문들에 번호 순서대로 답변해 주세요. 각 답변은 새 줄에서 "
            "`1. ...`, `2. ...` 형식으로 시작하고, 본문은 한 단락으로 짧게 답해 "
            "주세요. 질문 번호를 빠뜨리거나 합쳐 답하지 말아 주세요.\n"
            "\n"
            f"{numbered}"
        )
        messages: list = [
            MessageEntry(role="system", content=system_prompt),
            MessageEntry(role="user", content=user_prompt),
        ]
        flags = Flags()
        status = "completed"
        error_payload: Optional[dict] = None
        raw_responses: list = []

        logger.info(
            "인터뷰 시작",
            extra={
                "persona_id_hash": mask_persona_id(self._persona.persona_id),
                "persona_name": mask_name(self._persona.name),
                "persona_age": self._persona.age,
                "persona_gender": self._persona.gender,
                "persona_region": self._persona.region,
                "product": mask_product(self._product),
                "questions_count": len(self._questions),
                "follow_ups_count": len(self._follow_ups),
                "mode": "single_turn",
            },
        )

        try:
            (
                response_text,
                latency_ms,
                retry_count,
                usage,
            ) = await self._call_llm(messages)
            messages.append(
                MessageEntry(role="assistant", content=response_text)
            )

            # 거부 감지(가장 강한 신호. 즉시 status 갱신, 파싱은 시도하지 않음).
            if detect_refusal(response_text, self._interview_cfg.refusal_keywords):
                flags = dataclasses.replace(flags, refusal_detected=True)
                status = "refused"
                # refused여도 raw_responses에 응답 1건은 보존(분석 가능성).
                raw_responses.append(
                    RawResponse(
                        question_index=0,
                        response=response_text,
                        latency_ms=latency_ms,
                        retry_count=retry_count,
                        usage=usage,
                    )
                )
            else:
                # 페르소나 깨짐 감지(중단 없이 플래그만).
                if detect_persona_drift(
                    response_text,
                    self._persona,
                    self._interview_cfg.english_ratio_threshold,
                    self._interview_cfg.occupation_english_whitelist,
                ):
                    drift_confirmed = True
                    if self._interview_cfg.llm_drift_review:
                        drift_confirmed = await review_drift_with_llm(
                            response_text,
                            self._persona,
                            self._client,
                            self._llm_cfg,
                        )
                    if drift_confirmed:
                        flags = dataclasses.replace(flags, persona_drift=True)
                        status = "drift"
                        logger.warning(
                            "페르소나 깨짐 감지(단일턴)",
                            extra={"persona_id_hash": mask_persona_id(self._persona.persona_id)},
                        )

                parsed, parse_failed = _parse_single_turn_response(
                    response_text, len(all_questions)
                )
                if parse_failed:
                    flags = dataclasses.replace(flags, parse_failed=True)
                    logger.warning(
                        "단일턴 응답 번호 파싱 실패. fallback으로 마지막 "
                        "question에 통째 텍스트 저장",
                        extra={
                            "persona_id_hash": mask_persona_id(self._persona.persona_id),
                            "questions_count": len(all_questions),
                        },
                    )

                # usage는 단일 호출이므로 첫 응답에만 박는다(중복 합산 회피).
                # latency도 마찬가지다. 두 번째 이후 응답은 같은 호출의 분리
                # 결과라 latency_ms=0, retry_count=0, 빈 usage로 둔다.
                first = True
                for q_index, segment in enumerate(parsed):
                    raw_responses.append(
                        RawResponse(
                            question_index=q_index,
                            response=segment,
                            latency_ms=latency_ms if first else 0,
                            retry_count=retry_count if first else 0,
                            usage=usage if first else TokenUsage(),
                        )
                    )
                    first = False

        except RetryExhaustedError as exc:
            status = "failed"
            error_payload = {
                "type": "retry_exhausted",
                "message": str(exc),
            }
            logger.error(
                "인터뷰 실패(재시도 한도 초과, 단일턴)",
                extra={
                    "persona_id_hash": mask_persona_id(self._persona.persona_id),
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
                "인터뷰 실패(서버 응답 없음, 단일턴)",
                extra={
                    "persona_id_hash": mask_persona_id(self._persona.persona_id),
                    "reason": str(exc),
                },
            )

        # 구조화 요약(멀티턴과 동일 정책).
        structured_summary: Optional[StructuredSummary] = None
        if status in ("completed", "drift", "refused") and raw_responses:
            try:
                structured_summary = await summarize_interview(
                    messages, self._client, self._llm_cfg
                )
            except (
                RetryExhaustedError,
                ServerNotReachableError,
                ConfigError,
                StructuredSummaryParseError,
            ) as exc:
                logger.warning(
                    "구조화 요약 단계 예외(structured_summary=None로 보존, 단일턴)",
                    extra={
                        "persona_id_hash": mask_persona_id(self._persona.persona_id),
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
                "persona_id_hash": mask_persona_id(self._persona.persona_id),
                "status": status,
                "responses_count": len(raw_responses),
                "flags": dataclasses.asdict(flags),
                "summary_present": structured_summary is not None,
                "mode": "single_turn",
            },
        )
        return record


# ---------------------------------------------------------------------------
# 모듈 함수 진입점(테스트와 호출자 양쪽 호환)
# ---------------------------------------------------------------------------


async def run_interview(
    persona: PersonaMeta,
    product: str,
    questions: list,
    follow_ups: list,
    llm: "LLMBackend",
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
