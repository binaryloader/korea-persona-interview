"""애플리케이션 설정 로드.

우선순위는 코드 default → ``config.yaml`` → ``.env`` 파일 → 환경변수
``KPI_*``/``OPENAI_API_KEY`` → CLI 옵션이다(TDD §10). 로드 결과는
``AppConfig`` frozen dataclass로 반환하며, 도메인 모델과 달리 외부
의존(yaml/.env)을 갖는 infrastructure 계층 코드다(architecture.md §1).

v1.x 백엔드 전환 시점부터 본 도구는 OpenAI Chat Completions API로 호출한다.
이전 v1.0의 로컬 MLX 서버 가드(``is_local_base_url`` 강제 차단)는 제거되었고,
사업 아이템 본문은 OpenAI 서버로 송신된다. 사용자는 본 사실을 이해하고 사용한다
(README/PRD에 명시).

``.env`` 로더는 ``python-dotenv`` 의존을 회피하고 stdlib만으로 직접 파싱한다
(dependency.md §1, leftpad 회피). ``KEY=value`` 한 줄 형식, ``#`` 주석, 공백 라인,
값 주변 따옴표(``"..."``/``'...'``), ``export KEY=value`` 접두만 지원한다.
멀티라인 값과 escape 처리 같은 복잡한 형식은 미지원이다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .models import ConfigError


# 기본 yaml 경로. 호출자가 명시적으로 다른 경로를 줄 수 있다.
DEFAULT_YAML_PATH = Path("config.yaml")


# ---------------------------------------------------------------------------
# 중첩 설정 dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LlmConfig:
    """LLM 호출 관련 설정.

    ``base_url``과 ``model``은 OpenAI Chat Completions 호환 엔드포인트를 가리킨다.
    기본값은 OpenAI 공식 엔드포인트(``https://api.openai.com/v1``)와
    ``gpt-4o-mini``(비용/속도/품질 균형)다.

    ``api_key``는 환경변수 ``OPENAI_API_KEY``(우선) 또는 ``KPI_OPENAI_API_KEY``
    fallback에서 로드된다. ``healthcheck``/``chat`` 호출 시점에 누락이면
    ``ConfigError``로 변환되어 친절한 한국어 안내가 출력된다. ``list-personas``
    같이 키가 필요 없는 명령은 누락 상태로도 동작한다.

    각 수치 필드의 상하한은 운영 안전망이다. 잘못된 yaml 또는 환경변수가 비현실
    수치를 박을 때 ``ConfigError``로 막아 추론 무한 대기, OOM, 무한 재시도를
    예방한다(security.md §4, error-handling.md §1).
    """

    base_url: str
    model: str
    max_tokens: int
    temperature: float
    timeout: float
    context_budget: int
    retry_max_attempts: int
    retry_backoff_seconds: tuple
    api_key: Optional[str] = None

    def __post_init__(self) -> None:
        # 상한값은 본 도구의 v1 운영 가정에 맞춘 보수적 상한이다.
        # max_tokens 1-16000: gpt-4o-mini의 출력 토큰 상한(약 16k)을 수용한다.
        # MLX 시절 reasoning 토큰 폭증 가드(8k)는 OpenAI 백엔드에서 의미가 없다.
        # retry_max_attempts 1-5: 5회 초과 재시도는 사용자 대기를 길게 만든다
        # (120s timeout x 5 = 10분).
        # timeout 1-600초: 600초(10분)를 넘는 단일 호출은 v1 SLO 밖이다.
        # context_budget 1000-128000: gpt-4o-mini 입력 컨텍스트(128k)를 수용한다.
        # 1000 미만은 system 프롬프트 수용 불가.
        if not (1 <= self.max_tokens <= 16000):
            raise ConfigError(
                f"llm.max_tokens는 1-16000 범위만 허용한다. 입력값: {self.max_tokens}"
            )
        if not (1 <= self.retry_max_attempts <= 5):
            raise ConfigError(
                "llm.retry_max_attempts는 1-5 범위만 허용한다. "
                f"입력값: {self.retry_max_attempts}"
            )
        if not (1 <= self.timeout <= 600):
            raise ConfigError(
                f"llm.timeout(초)은 1-600 범위만 허용한다. 입력값: {self.timeout}"
            )
        if not (1000 <= self.context_budget <= 128000):
            raise ConfigError(
                "llm.context_budget는 1000-128000 범위만 허용한다. "
                f"입력값: {self.context_budget}"
            )


@dataclass(frozen=True)
class BatchConfig:
    """배치 인터뷰 동시성/페르소나 토글 설정.

    동시성 상한은 v1.0 시절 로컬 MLX 메모리 가드(Apple Silicon 단일 모델
    인스턴스에서 1-3 동시 호출만 안정적이었음) 때문에 1-3으로 묶여 있었다.
    OpenAI 백엔드 전환 이후 메모리 가드가 무관해 1-10으로 상향한다. 동시성
    10은 OpenAI rate limit(tier별 분당 요청 수)을 한 번에 다 쓰지 않도록 둔
    완만한 상한이며, 그 이상은 비용 폭증과 rate limit 회귀를 동반한다.

    ``partial_failure_threshold``는 부분 실패 판정 임계값(0.0-1.0)이다.
    완료된 record 비율이 본 값 미만이면 ``BatchResultEnvelope.partial_failure``가
    True로 표시되고 CLI는 종료 코드 3을 반환한다(라운드 B3 외부화).
    """

    concurrency: int
    persona_fields: tuple
    single_turn: bool = False
    partial_failure_threshold: float = 0.5

    def __post_init__(self) -> None:
        if not (1 <= self.concurrency <= 10):
            raise ConfigError(
                f"동시성은 1-10 범위만 허용한다. 입력값: {self.concurrency}"
            )
        if not (0.0 <= self.partial_failure_threshold <= 1.0):
            raise ConfigError(
                "batch.partial_failure_threshold는 0.0-1.0 범위만 허용한다. "
                f"입력값: {self.partial_failure_threshold}"
            )


@dataclass(frozen=True)
class DatasetConfig:
    """데이터셋 컬럼 매핑/별칭(TDD §1.6)."""

    name: str
    split: str
    field_map: dict
    gender_aliases: dict
    province_aliases: dict


@dataclass(frozen=True)
class InterviewConfig:
    """인터뷰 임계값/키워드(TDD §8).

    임계값/키워드를 외부화한 결과 사용자가 yaml에서 인터뷰 휴리스틱을 직접
    조정할 수 있다(라운드 B2). 영어 비율 임계값을 0.5로 올리면 영어 단어가 더
    많이 섞여도 drift로 보지 않고, 짧은 답변 임계값을 30자로 올리면 자동
    follow-up이 더 자주 발동된다.

    상하한 검증은 ``__post_init__``에서 한다. 음수 임계값이나 1.0 초과 영어
    비율 같은 비현실 값은 ConfigError로 차단한다(error-handling.md §1).
    """

    short_answer_threshold: int
    english_ratio_threshold: float
    ambiguous_keywords: tuple
    refusal_keywords: tuple
    auto_follow_up_text: str = "조금만 더 자세히 말씀해 주실 수 있을까요?"
    auto_follow_up_max: int = 1

    def __post_init__(self) -> None:
        if self.short_answer_threshold < 0:
            raise ConfigError(
                "interview.short_answer_threshold는 0 이상이어야 한다. "
                f"입력값: {self.short_answer_threshold}"
            )
        if not (0.0 <= self.english_ratio_threshold <= 1.0):
            raise ConfigError(
                "interview.english_ratio_threshold는 0.0-1.0 범위만 허용한다. "
                f"입력값: {self.english_ratio_threshold}"
            )
        if self.auto_follow_up_max < 0:
            raise ConfigError(
                "interview.auto_follow_up_max는 0 이상이어야 한다. "
                f"입력값: {self.auto_follow_up_max}"
            )
        if not isinstance(self.auto_follow_up_text, str) or not self.auto_follow_up_text.strip():
            raise ConfigError(
                "interview.auto_follow_up_text는 빈 문자열이 아닌 str이어야 한다"
            )


@dataclass(frozen=True)
class ReportConfig:
    """리포트 생성 임계값/렌더 파라미터(라운드 B3 외부화).

    이전에는 ``src/report.py``에 모듈 상수(`_MIN_COHORT_CELL`,
    `_PRICE_HIST_BINS`, `_BAR_CHART_WIDTH`)로 박혀 있어 사용자가 yaml에서
    조정할 수 없었다. 본 dataclass로 외부화해 리포트 표본 마스킹 임계값,
    히스토그램 구간 수, 텍스트 막대 폭, 거절 사유 top N 기본값을 yaml에서
    조정할 수 있다.
    """

    cohort_min_cell: int = 3
    top_n_default: int = 10
    histogram_bins: int = 10
    bar_width: int = 30

    def __post_init__(self) -> None:
        if self.cohort_min_cell < 1:
            raise ConfigError(
                "report.cohort_min_cell는 1 이상이어야 한다. "
                f"입력값: {self.cohort_min_cell}"
            )
        if self.top_n_default < 1:
            raise ConfigError(
                "report.top_n_default는 1 이상이어야 한다. "
                f"입력값: {self.top_n_default}"
            )
        if self.histogram_bins < 1:
            raise ConfigError(
                "report.histogram_bins는 1 이상이어야 한다. "
                f"입력값: {self.histogram_bins}"
            )
        if not (1 <= self.bar_width <= 200):
            raise ConfigError(
                "report.bar_width는 1-200 범위만 허용한다. "
                f"입력값: {self.bar_width}"
            )


@dataclass(frozen=True)
class AppConfig:
    """전체 애플리케이션 설정. 모든 모듈은 본 객체에 의존한다."""

    llm: LlmConfig
    batch: BatchConfig
    dataset: DatasetConfig
    interview: InterviewConfig
    report: ReportConfig
    output_dir: Path
    log_level: str
    no_color: bool


# ---------------------------------------------------------------------------
# 코드 default 값(가장 낮은 우선순위)
# ---------------------------------------------------------------------------


def _default_dict() -> dict:
    """우선순위 머지의 기준이 되는 default dict.

    ``config.yaml``이 없거나 일부 섹션만 있어도 본 default가 보강한다.
    """

    return {
        "llm": {
            # OpenAI Chat Completions API 공식 엔드포인트.
            "base_url": "https://api.openai.com/v1",
            # gpt-4o-mini는 비용/속도/품질 균형. 사용자 결정에 따라 변경 가능.
            "model": "gpt-4o-mini",
            "max_tokens": 500,
            "temperature": 0.8,
            "timeout": 120,
            "context_budget": 32000,
            "retry_max_attempts": 3,
            "retry_backoff_seconds": [1, 2, 4],
            # API 키는 환경변수에서만 받는다. yaml/CLI override는 허용하지 않아
            # 시크릿이 디스크 또는 명령행 히스토리에 남지 않게 한다(security.md §1).
            "api_key": None,
        },
        "batch": {
            # 기본 동시성. OpenAI 백엔드는 동시성 4-5에서 안정적 처리량과
            # rate limit 여유의 균형이 좋다(MLX 시절 2 → OpenAI 4).
            "concurrency": 4,
            "persona_fields": ["summary"],
            # single_turn은 PRD §5.1, §5.9의 ``--single-turn`` 옵션 매핑.
            # 멀티턴 흐름 대신 모든 질문을 한 번의 chat 호출에 묶어 처리한다.
            # 토큰 절약 + 빠른 dry-run 용도.
            "single_turn": False,
            # 부분 실패 판정 임계값(0.0-1.0). 완료 비율이 본 값 미만이면 partial.
            # 0.5는 PRD §5.9 종료 코드 3 매핑(완료 record 50% 미만).
            "partial_failure_threshold": 0.5,
        },
        "dataset": {
            "name": "nvidia/Nemotron-Personas-Korea",
            "split": "train",
            "field_map": {
                "name": None,
                "gender": "sex",
                "age": "age",
                "region": "province",
                "subregion": "district",
                "occupation": "occupation",
                "marital": "marital_status",
                "education": "education_level",
                "family_type": "family_type",
                "housing_type": "housing_type",
                "summary": "persona",
                "professional": "professional_persona",
                "sports": "sports_persona",
                "arts": "arts_persona",
                "travel": "travel_persona",
                "culinary": "culinary_persona",
                "family": "family_persona",
            },
            "gender_aliases": {
                "F": "여자",
                "M": "남자",
                "여성": "여자",
                "남성": "남자",
            },
            "province_aliases": {},
        },
        "interview": {
            "short_answer_threshold": 20,
            "english_ratio_threshold": 0.30,
            "ambiguous_keywords": [
                "글쎄요",
                "잘 모르겠습니다",
                "잘 모르겠어요",
                "딱히",
                "별로 생각 안 해봤",
                "모르겠",
            ],
            "refusal_keywords": [
                "답변할 수 없습니다",
                "답변하기 어렵",
                "I cannot",
                "I'm sorry, but",
                "As an AI",
                "저는 인공지능",
                "AI 모델",
            ],
            "auto_follow_up_text": "조금만 더 자세히 말씀해 주실 수 있을까요?",
            "auto_follow_up_max": 1,
        },
        "report": {
            # 코호트 셀 표본 부족 마스킹 임계값. PRD §5.6: 3명 미만 셀은
            # ``표본 부족``으로 마스킹한다.
            "cohort_min_cell": 3,
            # ``--top-n`` CLI 기본값(거절 사유 상위 N개).
            "top_n_default": 10,
            # 가격 히스토그램 구간 수.
            "histogram_bins": 10,
            # 텍스트 막대 차트 폭(컬럼 수). 좁은 터미널은 20-25 권장.
            "bar_width": 30,
        },
        "output": {
            "output_dir": "outputs/",
            "log_level": "INFO",
            "no_color": False,
        },
    }


# ---------------------------------------------------------------------------
# 머지 헬퍼
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, override: dict) -> dict:
    """dict 두 개를 깊은 병합한다. 같은 키는 override가 우선한다.

    ``dataset.field_map`` 같은 중첩 dict를 yaml에서 부분 갱신할 수 있게 한다.
    """

    out = dict(base)
    for key, value in override.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict:
    """yaml을 읽어 dict로 반환. 실패 시 ConfigError로 변환한다."""

    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml 파싱 실패: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"config.yaml 최상위는 dict여야 한다: {type(data).__name__}"
        )
    return data


# ---------------------------------------------------------------------------
# .env 파일 로더(python-dotenv 의존 회피)
# ---------------------------------------------------------------------------


# .env 파일 탐색 경로. 작업 디렉토리 우선, 그다음 프로젝트 루트(본 파일 기준
# 두 단계 위)를 본다. 본 모듈이 ``src/config.py``라 ``parents[1]``이 프로젝트
# 루트에 해당한다.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Optional[Path] = None) -> dict:
    """``.env`` 파일을 stdlib만으로 파싱해 dict로 돌려준다.

    파서 규칙은 의도적으로 좁게 잡는다(dependency.md §1, leftpad 회피).

    - ``KEY=value`` 한 줄 형식만 지원한다
    - ``#``로 시작하는 줄과 공백 라인은 무시한다
    - 값 주변의 ``"..."``/``'...'``는 한 쌍에 한해 제거한다
    - ``export KEY=value`` 접두를 허용한다(셸 호환)
    - ``=``가 없거나 KEY가 비면 해당 라인은 무시한다(파싱 실패시 조용히 스킵)
    - 멀티라인 값/escape/변수 참조 같은 고급 기능은 지원하지 않는다

    Args:
        path: 명시 경로. ``None``이면 작업 디렉토리 → 프로젝트 루트 순서로 탐색.

    Returns:
        파싱된 ``{key: value}`` dict. 파일 없으면 빈 dict.
    """

    candidates: list = []
    if path is not None:
        candidates.append(path)
    else:
        # 현재 작업 디렉토리와 프로젝트 루트 양쪽을 본다. 둘 다 존재하면 cwd 우선.
        candidates.append(Path.cwd() / ".env")
        if _PROJECT_ROOT not in candidates and _PROJECT_ROOT != Path.cwd():
            candidates.append(_PROJECT_ROOT / ".env")

    for candidate in candidates:
        if candidate.exists():
            return _parse_dotenv_file(candidate)
    return {}


def _parse_dotenv_file(path: Path) -> dict:
    """``.env`` 본문을 한 줄씩 파싱해 dict를 만든다."""

    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # 권한 등의 이유로 읽지 못하면 조용히 빈 dict를 돌려준다. .env는 선택
        # 사양이라 전체 로드를 막지 않는다.
        return {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # ``export KEY=value`` 접두 허용.
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # 값에 인라인 ``# 주석``이 따라오는 케이스는 미지원이다(따옴표 없는
        # 일반 값에서만 모호함이 생기므로 단순화를 위해 그대로 둔다).
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"')
            or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# 환경변수 매핑
# ---------------------------------------------------------------------------


def _apply_env(merged: dict) -> dict:
    """환경변수에서 비밀만 받아 merged dict에 덮어쓴다.

    v1.x부터 본 도구의 설정 정책은 아래와 같다.

    - 비밀(API 키)은 환경변수에서만 받는다. yaml/CLI override는 허용하지 않아
      디스크/명령행 히스토리에 시크릿이 남지 않게 한다(security.md §1)
    - 그 외 설정(모델 ID, 동시성, 토큰 한도, 타임아웃 등)은 ``config.yaml``의
      기본값과 CLI override(예: ``--model``, ``--concurrency``)로만 다룬다.
      ``KPI_LLM_*``/``KPI_BATCH_*`` 같은 환경변수 override는 v1.x에서 제거됐다.
      "비밀=env, 기본=yaml, 일회성=CLI" 한 가지 규칙으로 우선순위를 단순화한다

    keep된 환경변수 키는 아래 두 개뿐이다.

    - ``OPENAI_API_KEY``(표준)
    - ``KPI_OPENAI_API_KEY``(격리된 테스트/CI 환경의 fallback)

    두 키 모두 누락이면 ``llm.api_key``는 None으로 남고 chat 호출 시점에
    ``ConfigError``로 변환된다(security.md §1, error-handling.md §1).

    추가로 출력 디렉토리만 ``KPI_OUTPUT_DIR`` 환경변수를 유지한다. 본 키는
    비밀이 아니지만 CI/테스트가 작업 디렉토리를 ``tmp_path``로 바꾸려 할 때
    cli flag보다 환경변수로 일괄 처리하는 것이 편리하다. 다른 구성값은 yaml/CLI
    경로로 일원화했다.
    """

    out = {k: dict(v) if isinstance(v, dict) else v for k, v in merged.items()}

    # 출력 디렉토리만 비밀이 아닌 환경변수로 유지(테스트 격리 편의).
    output_dir_env = os.environ.get("KPI_OUTPUT_DIR")
    if output_dir_env:
        out.setdefault("output", {})["output_dir"] = output_dir_env

    # OpenAI API 키 로드. ``OPENAI_API_KEY``를 표준으로 하고
    # ``KPI_OPENAI_API_KEY``는 격리된 테스트/CI 환경의 fallback이다.
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get(
        "KPI_OPENAI_API_KEY"
    )
    if api_key:
        out.setdefault("llm", {})["api_key"] = api_key

    return out


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def load_config(
    yaml_path: Optional[Path] = None,
    cli_overrides: Optional[dict] = None,
) -> AppConfig:
    """AppConfig를 우선순위 머지로 로드한다.

    우선순위는 default → yaml → ``.env`` 파일 → env(``KPI_*``/``OPENAI_API_KEY``)
    → ``cli_overrides``이다. ``.env``는 ``os.environ``에 ``setdefault``로 주입되어
    이미 set된 명시 환경변수를 덮어쓰지 않는다(셸/CI 환경의 명시 키가 우선).

    Args:
        yaml_path: ``config.yaml`` 경로. ``None``이면 ``DEFAULT_YAML_PATH``.
        cli_overrides: CLI 옵션이 주는 dict 부분 갱신.

    Returns:
        검증된 AppConfig.

    Raises:
        ConfigError: yaml 파싱/타입 오류, 동시성 범위 위반 등.
    """

    # ``.env``를 환경변수로 승격한다. 이미 명시 환경변수가 있으면 ``setdefault``
    # 가 덮어쓰지 않으므로 명시 환경변수 우선 원칙이 보장된다.
    for key, value in _load_dotenv().items():
        os.environ.setdefault(key, value)

    yaml_path = yaml_path or DEFAULT_YAML_PATH
    merged = _default_dict()
    merged = _deep_merge(merged, _load_yaml(yaml_path))
    merged = _apply_env(merged)
    if cli_overrides:
        merged = _deep_merge(merged, cli_overrides)

    try:
        api_key_raw = merged["llm"].get("api_key")
        api_key_val: Optional[str] = (
            str(api_key_raw) if api_key_raw not in (None, "") else None
        )
        llm_cfg = LlmConfig(
            base_url=str(merged["llm"]["base_url"]),
            model=str(merged["llm"]["model"]),
            max_tokens=int(merged["llm"]["max_tokens"]),
            temperature=float(merged["llm"]["temperature"]),
            timeout=float(merged["llm"]["timeout"]),
            context_budget=int(merged["llm"]["context_budget"]),
            retry_max_attempts=int(merged["llm"]["retry_max_attempts"]),
            retry_backoff_seconds=tuple(
                float(x) for x in merged["llm"]["retry_backoff_seconds"]
            ),
            api_key=api_key_val,
        )
        batch_cfg = BatchConfig(
            concurrency=int(merged["batch"]["concurrency"]),
            persona_fields=tuple(str(x) for x in merged["batch"]["persona_fields"]),
            single_turn=bool(merged["batch"].get("single_turn", False)),
            partial_failure_threshold=float(
                merged["batch"].get("partial_failure_threshold", 0.5)
            ),
        )
        dataset_cfg = DatasetConfig(
            name=str(merged["dataset"]["name"]),
            split=str(merged["dataset"]["split"]),
            field_map=dict(merged["dataset"]["field_map"]),
            gender_aliases=dict(merged["dataset"]["gender_aliases"]),
            province_aliases=dict(merged["dataset"]["province_aliases"]),
        )
        interview_cfg = InterviewConfig(
            short_answer_threshold=int(merged["interview"]["short_answer_threshold"]),
            english_ratio_threshold=float(
                merged["interview"]["english_ratio_threshold"]
            ),
            ambiguous_keywords=tuple(
                str(x) for x in merged["interview"]["ambiguous_keywords"]
            ),
            refusal_keywords=tuple(
                str(x) for x in merged["interview"]["refusal_keywords"]
            ),
            auto_follow_up_text=str(
                merged["interview"].get(
                    "auto_follow_up_text",
                    "조금만 더 자세히 말씀해 주실 수 있을까요?",
                )
            ),
            auto_follow_up_max=int(
                merged["interview"].get("auto_follow_up_max", 1)
            ),
        )
        report_raw = merged.get("report") or {}
        report_cfg = ReportConfig(
            cohort_min_cell=int(report_raw.get("cohort_min_cell", 3)),
            top_n_default=int(report_raw.get("top_n_default", 10)),
            histogram_bins=int(report_raw.get("histogram_bins", 10)),
            bar_width=int(report_raw.get("bar_width", 30)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"설정 필드 변환 실패: {exc}") from exc

    return AppConfig(
        llm=llm_cfg,
        batch=batch_cfg,
        dataset=dataset_cfg,
        interview=interview_cfg,
        report=report_cfg,
        output_dir=Path(str(merged["output"]["output_dir"])),
        log_level=str(merged["output"]["log_level"]),
        no_color=bool(merged["output"]["no_color"]),
    )
