"""계층형 설정 로더.

우선순위는 내장 기본값, ``config.yaml``, ``.env`` 파일, 환경 변수, CLI
오버라이드 순서로 적용한다. 결과는 frozen ``AppConfig`` dataclass로 반환한다.

본 모듈은 v1.2.0(ADR-005)부터 yaml을 카테고리별 섹션으로 재구조화했다. 섹션은
아래 6개다.

- ``common``: 모든 진입점에 적용(CLI / MCP server / MCP orchestrator). dataset,
  persona, report 정책의 정본
- ``llm``: CLI와 MCP server 진입점에서만 적용. MCP orchestrator는 호스트
  sub-agent가 자기 LLM을 사용하므로 본 섹션과 무관하다
- ``batch``: CLI와 MCP server 진입점에서만 적용. MCP orchestrator는 호스트
  정책을 따른다
- ``heuristics``: CLI와 MCP server에서 자동 적용. MCP orchestrator는 helper
  도구를 호스트가 명시 호출했을 때 같은 임계값과 키워드를 사용한다
- ``mcp``: MCP 서버 진입점에서만 적용. CLI는 본 섹션과 무관하다
- ``output``: 모든 진입점에 적용

시크릿(``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``)은 환경 변수나 프로젝트
루트의 ``.env`` 파일에서 읽는다. ``.env`` 파서는 ``python-dotenv`` 의존성을
피하기 위해 표준 라이브러리만으로 구현했다. ``KEY=value`` 라인, ``#`` 주석,
빈 줄, 작은따옴표/큰따옴표 감싼 값, ``export KEY=value`` 쉘 접두어를
지원한다. 여러 줄 값과 이스케이프는 의도적으로 지원하지 않는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .models import ConfigError


DEFAULT_YAML_PATH = Path("config.yaml")


_VALID_PROVIDERS = frozenset({"openai", "anthropic"})

_VALID_MCP_MODES = frozenset({"server", "orchestrator"})


@dataclass(frozen=True)
class LlmConfig:
    """LLM HTTP 클라이언트 설정.

    ``provider``는 OpenAI Chat Completions API와 Anthropic Messages API 중
    하나를 선택한다. 로컬 LLM(mlx_lm.server, vLLM, llama.cpp)은
    ``provider=openai``로 두고 ``base_url``을 ``http://localhost:PORT/v1``로
    덮어쓰면 동작한다. 비어 있지 않은 ``api_key`` 값이면 모두 허용한다.

    ``api_key``는 ``OPENAI_API_KEY``(또는 ``provider=anthropic``일 때
    ``ANTHROPIC_API_KEY``)에서만 읽는다. yaml 키 값은 의도적으로 무시하므로
    시크릿이 디스크나 쉘 히스토리에 남지 않는다.

    ``__post_init__``에서 강제하는 수치 범위는 비현실적인 yaml/환경 값에
    대한 운영 안전장치다.
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
    provider: str = "openai"
    anthropic_cache_control: bool = True
    extra_chat_kwargs: tuple = ()
    streaming: bool = False

    def __post_init__(self) -> None:
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
        if self.provider not in _VALID_PROVIDERS:
            raise ConfigError(
                f"llm.provider는 {sorted(_VALID_PROVIDERS)} 중 하나여야 한다. "
                f"입력값: {self.provider!r}"
            )
        if not isinstance(self.extra_chat_kwargs, tuple):
            raise ConfigError(
                "llm.extra_chat_kwargs는 (key, value) 튜플의 튜플이어야 한다"
            )

    def extra_chat_kwargs_dict(self) -> dict:
        """``extra_chat_kwargs`` 튜플을 dict로 풀어 반환한다.

        frozen dataclass는 hashable이어야 하므로 dict 자체를 필드로 보관할 수
        없다. 외부에 노출할 때는 dict로 풀어주는 헬퍼를 둔다.
        """

        return dict(self.extra_chat_kwargs)


@dataclass(frozen=True)
class BatchConfig:
    """배치 인터뷰 동시성과 부분 실패 임계값.

    동시성 1-10은 일반적인 OpenAI 티어 rate limit 아래에 여유를 두기 위한
    soft cap이다. ``partial_failure_threshold``(0.0-1.0)는 이 비율 아래로
    성공률이 떨어지면 ``BatchResultEnvelope.partial_failure``를 true로
    뒤집고 CLI는 종료 코드 3으로 빠져나오는 임계값이다.

    ``single_turn``은 CLI ``--single-turn`` 일회성 옵션이 cli_overrides 경로로
    주입되므로 본 dataclass에 보관한다. yaml에는 키가 없다.
    """

    concurrency: int
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
    """Hugging Face 데이터셋 이름, split, 컬럼 매핑 별칭."""

    name: str
    split: str
    field_map: dict
    gender_aliases: dict
    province_aliases: dict


@dataclass(frozen=True)
class PersonaConfig:
    """페르소나 토글과 시스템 프롬프트 템플릿 경로.

    ``fields``는 시스템 프롬프트에 추가 주입할 페르소나 토글 키워드 튜플이다.
    ``("summary",)``가 기본값이며 ``professional``/``sports``/``arts``/``travel``/
    ``culinary``/``family``를 추가하면 해당 자유 서술 컬럼이 페르소나 객체에
    합쳐진다.

    ``system_prompt_path``는 시스템 프롬프트 템플릿 파일 경로다(절대 경로 또는
    프로젝트 루트 기준 상대 경로). pip-installed 환경에서 본 경로가 부재하고
    default 경로면 패키지 내부 ``src._prompts.system_prompt.txt``로 fallback한다.
    """

    fields: tuple
    system_prompt_path: str = "prompts/system_prompt.txt"

    def __post_init__(self) -> None:
        if not isinstance(self.system_prompt_path, str) or not self.system_prompt_path.strip():
            raise ConfigError(
                "common.persona.system_prompt_path는 빈 문자열이 아닌 str이어야 한다"
            )


@dataclass(frozen=True)
class HeuristicsConfig:
    """인터뷰 휴리스틱 임계값과 키워드 리스트.

    이 값들을 외부화하면 사용자는 코드를 건드리지 않고 yaml에서 자동
    follow-up 트리거와 페르소나 drift 감지기를 조정할 수 있다. 범위를
    벗어난 값은 ``__post_init__``에서 거부한다.

    v1.2.0(ADR-005)부터 ``InterviewConfig``에서 ``HeuristicsConfig``로 이름이
    바뀌었다. ``system_prompt_path``와 ``persona_fields``는 ``CommonConfig.persona``로
    이동했다.
    """

    short_answer_threshold: int
    english_ratio_threshold: float
    ambiguous_keywords: tuple
    refusal_keywords: tuple
    auto_follow_up_text: str = "조금만 더 자세히 말씀해 주실 수 있을까요?"
    auto_follow_up_max: int = 1
    occupation_english_whitelist: bool = True
    llm_drift_review: bool = False

    def __post_init__(self) -> None:
        if self.short_answer_threshold < 0:
            raise ConfigError(
                "heuristics.short_answer_threshold는 0 이상이어야 한다. "
                f"입력값: {self.short_answer_threshold}"
            )
        if not (0.0 <= self.english_ratio_threshold <= 1.0):
            raise ConfigError(
                "heuristics.english_ratio_threshold는 0.0-1.0 범위만 허용한다. "
                f"입력값: {self.english_ratio_threshold}"
            )
        if self.auto_follow_up_max < 0:
            raise ConfigError(
                "heuristics.auto_follow_up_max는 0 이상이어야 한다. "
                f"입력값: {self.auto_follow_up_max}"
            )
        if not isinstance(self.auto_follow_up_text, str) or not self.auto_follow_up_text.strip():
            raise ConfigError(
                "heuristics.auto_follow_up_text는 빈 문자열이 아닌 str이어야 한다"
            )


@dataclass(frozen=True)
class ReportConfig:
    """리포트 렌더링 임계값과 bin 폭."""

    cohort_min_cell: int = 3
    top_n_default: int = 10
    histogram_bins: int = 10
    bar_width: int = 30
    insight_model: Optional[str] = None
    estimate_wtp_from_signal: bool = False

    def __post_init__(self) -> None:
        if self.cohort_min_cell < 1:
            raise ConfigError(
                "common.report.cohort_min_cell는 1 이상이어야 한다. "
                f"입력값: {self.cohort_min_cell}"
            )
        if self.top_n_default < 1:
            raise ConfigError(
                "common.report.top_n_default는 1 이상이어야 한다. "
                f"입력값: {self.top_n_default}"
            )
        if self.histogram_bins < 1:
            raise ConfigError(
                "common.report.histogram_bins는 1 이상이어야 한다. "
                f"입력값: {self.histogram_bins}"
            )
        if not (1 <= self.bar_width <= 200):
            raise ConfigError(
                "common.report.bar_width는 1-200 범위만 허용한다. "
                f"입력값: {self.bar_width}"
            )


@dataclass(frozen=True)
class CommonConfig:
    """모든 진입점이 공유하는 공통 설정 묶음.

    dataset, persona, report 세 하위 dataclass를 담는다. CLI / MCP server /
    MCP orchestrator 어떤 진입점이든 본 섹션은 동일하게 적용된다.
    """

    dataset: DatasetConfig
    persona: PersonaConfig
    report: ReportConfig


@dataclass(frozen=True)
class McpConfig:
    """MCP 서버 진입점의 동작 모드 토글.

    v1.2.0(ADR-005)부터 ``mode``는 ``server``와 ``orchestrator`` 두 값만 허용한다.
    sampling 모드는 v1.2.0에서 제거됐다(보급률 한계).

    - ``server`` (기본): MCP 도구 호출이 server-side ``OpenAIBackend``/
      ``AnthropicBackend``를 사용한다. CLI와 동일한 ``LlmConfig`` 필드를 그대로
      활용한다. mcp.json의 ``env``에 ``OPENAI_API_KEY``/``ANTHROPIC_API_KEY``를
      박아 주어야 한다
    - ``orchestrator``: server-side에서 LLM을 호출하지 않는다. 호스트 sub-agent
      가 자기 LLM으로 인터뷰를 수행하고, 본 도구는 데이터/프롬프트 helper만
      노출한다. server-side 키 불필요

    자동 fallback은 하지 않는다. 모드 전환은 명시 토글로만 가능하다(ADR-005
    §3 결정 근거).
    """

    mode: str = "server"

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MCP_MODES:
            raise ConfigError(
                f"mcp.mode는 {sorted(_VALID_MCP_MODES)} 중 하나여야 한다. "
                f"입력값: {self.mode!r}"
            )


@dataclass(frozen=True)
class AppConfig:
    """최상위 애플리케이션 설정.

    v1.2.0(ADR-005)부터 카테고리별 섹션 재구조화의 정본이다. 호환성 깨짐은
    아래와 같다.

    - ``dataset`` 필드는 ``common.dataset``으로 이동
    - ``report`` 필드는 ``common.report``로 이동
    - ``interview`` 필드는 ``heuristics``로 리네임. ``system_prompt_path``와
      ``persona_fields``는 ``common.persona``로 이동
    - ``batch.persona_fields``는 ``common.persona.fields``로 이동
    """

    common: CommonConfig
    llm: LlmConfig
    batch: BatchConfig
    heuristics: HeuristicsConfig
    mcp: McpConfig
    output_dir: Path
    log_level: str
    no_color: bool


def _default_dict() -> dict:
    """가장 낮은 우선순위의 머지 레이어로 쓰이는 내장 기본값."""

    return {
        "common": {
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
            "persona": {
                "fields": ["summary"],
                "system_prompt_path": "prompts/system_prompt.txt",
            },
            "report": {
                "cohort_min_cell": 3,
                "top_n_default": 10,
                "histogram_bins": 10,
                "bar_width": 30,
                "insight_model": None,
                "estimate_wtp_from_signal": False,
            },
        },
        "llm": {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "max_tokens": 500,
            "temperature": 0.8,
            "timeout": 120,
            "context_budget": 32000,
            "retry_max_attempts": 3,
            "retry_backoff_seconds": [1, 2, 4],
            "api_key": None,
            "anthropic_cache_control": True,
            "extra_chat_kwargs": {},
            "streaming": False,
        },
        "batch": {
            "concurrency": 4,
            "single_turn": False,
            "partial_failure_threshold": 0.5,
        },
        "heuristics": {
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
            "occupation_english_whitelist": True,
            "llm_drift_review": False,
        },
        "mcp": {
            "mode": "server",
        },
        "output": {
            "output_dir": "outputs/",
            "log_level": "INFO",
            "no_color": False,
        },
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """``override``를 ``base``에 재귀적으로 머지한다. 충돌 시 ``override``가 이긴다."""

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
    """yaml 파일을 dict로 읽는다. 파일이 없으면 빈 dict를 돌려준다."""

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


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Optional[Path] = None) -> dict:
    """표준 라이브러리만으로 ``.env`` 파일을 파싱한다.

    ``path``가 ``None``일 때 탐색 순서는 현재 작업 디렉토리, 그 다음 프로젝트
    루트다. 파일이 없으면 빈 dict를 돌려준다.
    """

    candidates: list = []
    if path is not None:
        candidates.append(path)
    else:
        candidates.append(Path.cwd() / ".env")
        if _PROJECT_ROOT not in candidates and _PROJECT_ROOT != Path.cwd():
            candidates.append(_PROJECT_ROOT / ".env")

    for candidate in candidates:
        if candidate.exists():
            return _parse_dotenv_file(candidate)
    return {}


def _parse_dotenv_file(path: Path) -> dict:
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"')
            or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def _apply_env(merged: dict) -> dict:
    """환경 변수를 머지된 설정 dict에 반영한다.

    시크릿과 출력 디렉토리만 인식한다.

    - ``OPENAI_API_KEY``는 ``llm.provider == "openai"``일 때 사용한다
    - ``ANTHROPIC_API_KEY``는 ``llm.provider == "anthropic"``일 때 사용한다
    - ``KPI_OUTPUT_DIR``는 테스트/CI 편의용 오버라이드다
    """

    out = {k: dict(v) if isinstance(v, dict) else v for k, v in merged.items()}

    output_dir_env = os.environ.get("KPI_OUTPUT_DIR")
    if output_dir_env:
        out.setdefault("output", {})["output_dir"] = output_dir_env

    llm_section = out.setdefault("llm", {})
    provider = str(llm_section.get("provider", "openai")).strip().lower()

    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        llm_section["api_key"] = api_key

    return out


def load_config(
    yaml_path: Optional[Path] = None,
    cli_overrides: Optional[dict] = None,
) -> AppConfig:
    """계층화된 소스에서 ``AppConfig``를 로드하고 검증한다.

    Args:
        yaml_path: ``config.yaml`` 경로. 기본값은 ``DEFAULT_YAML_PATH``
        cli_overrides: 다른 모든 레이어를 덮어쓰는 CLI 옵션의 부분 dict

    Raises:
        ConfigError: yaml 파싱 실패, 타입 불일치, 범위 초과 등.
    """

    for key, value in _load_dotenv().items():
        os.environ.setdefault(key, value)

    yaml_path = yaml_path or DEFAULT_YAML_PATH
    merged = _default_dict()
    merged = _deep_merge(merged, _load_yaml(yaml_path))
    # 기존 yaml 파일이 깨지지 않도록 legacy ``llm.backend`` 항목은 제거한다.
    # MCP가 sampling 전용으로 바뀌면서 토글 자체가 사라졌다.
    if isinstance(merged.get("llm"), dict):
        merged["llm"].pop("backend", None)
    merged = _apply_env(merged)
    if cli_overrides:
        merged = _deep_merge(merged, cli_overrides)
        # CLI가 환경 변수 패스 이후에 ``provider``를 뒤집을 수 있다. 그래서
        # ``--provider anthropic``이면 ``ANTHROPIC_API_KEY``를,
        # ``--provider openai``이면 ``OPENAI_API_KEY``를 다시 잡도록 환경
        # 변수 패스를 한 번 더 돌린다. CLI 자체는 ``api_key``를 넘길 수 없고
        # (yaml과 CLI 모두 거부) 사용자가 설정한 키는 덮어쓰지 않는다.
        merged = _apply_env(merged)

    try:
        api_key_raw = merged["llm"].get("api_key")
        api_key_val: Optional[str] = (
            str(api_key_raw) if api_key_raw not in (None, "") else None
        )
        provider = str(merged["llm"].get("provider", "openai")).strip().lower()
        # provider가 기존 기본값과 달라지면 호출자가 ``--provider anthropic``
        # 한 줄로 바꿀 수 있도록 매칭되는 기본 base_url과 model로 자동 전환
        # 한다. 사용자가 별도로 지정한 ``base_url``(``api.openai.com`` 기본
        # 값이 아닌 값)은 그대로 존중한다.
        base_url_raw = merged["llm"].get("base_url")
        if (
            provider == "anthropic"
            and (
                not base_url_raw
                or str(base_url_raw).rstrip("/") == "https://api.openai.com/v1"
            )
        ):
            base_url_raw = "https://api.anthropic.com/v1"
        elif not base_url_raw:
            base_url_raw = "https://api.openai.com/v1"
        model_raw = merged["llm"].get("model")
        if (
            provider == "anthropic"
            and (not model_raw or model_raw == "gpt-4o-mini")
        ):
            model_raw = "claude-haiku-4-5"
        elif not model_raw:
            model_raw = "gpt-4o-mini"
        extra_chat_kwargs_raw = merged["llm"].get("extra_chat_kwargs") or {}
        if not isinstance(extra_chat_kwargs_raw, dict):
            raise ConfigError(
                "llm.extra_chat_kwargs는 dict여야 한다. "
                f"입력 타입: {type(extra_chat_kwargs_raw).__name__}"
            )
        llm_cfg = LlmConfig(
            base_url=str(base_url_raw),
            model=str(model_raw),
            max_tokens=int(merged["llm"]["max_tokens"]),
            temperature=float(merged["llm"]["temperature"]),
            timeout=float(merged["llm"]["timeout"]),
            context_budget=int(merged["llm"]["context_budget"]),
            retry_max_attempts=int(merged["llm"]["retry_max_attempts"]),
            retry_backoff_seconds=tuple(
                float(x) for x in merged["llm"]["retry_backoff_seconds"]
            ),
            api_key=api_key_val,
            provider=provider,
            anthropic_cache_control=bool(
                merged["llm"].get("anthropic_cache_control", True)
            ),
            extra_chat_kwargs=tuple(
                (str(k), v) for k, v in extra_chat_kwargs_raw.items()
            ),
            streaming=bool(merged["llm"].get("streaming", False)),
        )
        batch_cfg = BatchConfig(
            concurrency=int(merged["batch"]["concurrency"]),
            single_turn=bool(merged["batch"].get("single_turn", False)),
            partial_failure_threshold=float(
                merged["batch"].get("partial_failure_threshold", 0.5)
            ),
        )
        common_raw = merged.get("common") or {}
        dataset_raw = common_raw.get("dataset") or {}
        dataset_cfg = DatasetConfig(
            name=str(dataset_raw["name"]),
            split=str(dataset_raw["split"]),
            field_map=dict(dataset_raw["field_map"]),
            gender_aliases=dict(dataset_raw["gender_aliases"]),
            province_aliases=dict(dataset_raw["province_aliases"]),
        )
        persona_raw = common_raw.get("persona") or {}
        persona_fields_raw = persona_raw.get("fields") or ["summary"]
        persona_cfg = PersonaConfig(
            fields=tuple(str(x) for x in persona_fields_raw),
            system_prompt_path=str(
                persona_raw.get(
                    "system_prompt_path", "prompts/system_prompt.txt"
                )
            ),
        )
        report_raw = common_raw.get("report") or {}
        insight_model_raw = report_raw.get("insight_model")
        insight_model_val: Optional[str] = (
            str(insight_model_raw).strip() or None
            if insight_model_raw not in (None, "")
            else None
        )
        report_cfg = ReportConfig(
            cohort_min_cell=int(report_raw.get("cohort_min_cell", 3)),
            top_n_default=int(report_raw.get("top_n_default", 10)),
            histogram_bins=int(report_raw.get("histogram_bins", 10)),
            bar_width=int(report_raw.get("bar_width", 30)),
            insight_model=insight_model_val,
            estimate_wtp_from_signal=bool(
                report_raw.get("estimate_wtp_from_signal", False)
            ),
        )
        common_cfg = CommonConfig(
            dataset=dataset_cfg,
            persona=persona_cfg,
            report=report_cfg,
        )
        heuristics_cfg = HeuristicsConfig(
            short_answer_threshold=int(merged["heuristics"]["short_answer_threshold"]),
            english_ratio_threshold=float(
                merged["heuristics"]["english_ratio_threshold"]
            ),
            ambiguous_keywords=tuple(
                str(x) for x in merged["heuristics"]["ambiguous_keywords"]
            ),
            refusal_keywords=tuple(
                str(x) for x in merged["heuristics"]["refusal_keywords"]
            ),
            auto_follow_up_text=str(
                merged["heuristics"].get(
                    "auto_follow_up_text",
                    "조금만 더 자세히 말씀해 주실 수 있을까요?",
                )
            ),
            auto_follow_up_max=int(
                merged["heuristics"].get("auto_follow_up_max", 1)
            ),
            occupation_english_whitelist=bool(
                merged["heuristics"].get("occupation_english_whitelist", True)
            ),
            llm_drift_review=bool(
                merged["heuristics"].get("llm_drift_review", False)
            ),
        )
        mcp_raw = merged.get("mcp") or {}
        mcp_cfg = McpConfig(
            mode=str(mcp_raw.get("mode", "server")).strip().lower(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"설정 필드 변환 실패: {exc}") from exc

    return AppConfig(
        common=common_cfg,
        llm=llm_cfg,
        batch=batch_cfg,
        heuristics=heuristics_cfg,
        mcp=mcp_cfg,
        output_dir=Path(str(merged["output"]["output_dir"])),
        log_level=str(merged["output"]["log_level"]),
        no_color=bool(merged["output"]["no_color"]),
    )
