"""애플리케이션 설정 로드.

우선순위는 코드 default → ``config.yaml`` → 환경변수 ``KPI_*`` → CLI 옵션이다
(TDD §10). 로드 결과는 ``AppConfig`` frozen dataclass로 반환하며, 도메인 모델과
달리 외부 의존(yaml)을 갖는 infrastructure 계층 코드다(architecture.md §1).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import ConfigError


# 기본 yaml 경로. 호출자가 명시적으로 다른 경로를 줄 수 있다.
DEFAULT_YAML_PATH = Path("config.yaml")

# localhost 가드용 prefix(security.md §1, TDD §13). chat() 차단 판정에 쓴다.
LOCAL_BASE_URL_PREFIXES = ("http://localhost", "http://127.0.0.1")


# ---------------------------------------------------------------------------
# 중첩 설정 dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LlmConfig:
    """LLM 호출 관련 설정.

    ``enable_thinking``은 Qwen3 계열의 reasoning trace 출력을 토글한다. default는
    False(끄기)이며, GATE-1 검증 결과 ``enable_thinking=true``로 호출하면
    토큰 예산을 영문 reasoning이 모두 소진해 ``message.content``가 비어 오는
    사례가 확인됐다(검증된 정본 모델: ``unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit``).
    chat 요청 본문에는 항상 ``chat_template_kwargs``로 명시 전달한다.
    """

    base_url: str
    model: str
    max_tokens: int
    temperature: float
    timeout: float
    context_budget: int
    retry_max_attempts: int
    retry_backoff_seconds: tuple
    enable_thinking: bool = False


@dataclass(frozen=True)
class BatchConfig:
    """배치 인터뷰 동시성/페르소나 토글 설정."""

    concurrency: int
    persona_fields: tuple

    def __post_init__(self) -> None:
        if not (1 <= self.concurrency <= 3):
            raise ConfigError(
                f"동시성은 1-3 범위만 허용한다. 입력값: {self.concurrency}"
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
    """인터뷰 임계값/키워드(TDD §8)."""

    short_answer_threshold: int
    english_ratio_threshold: float
    ambiguous_keywords: tuple
    refusal_keywords: tuple


@dataclass(frozen=True)
class AppConfig:
    """전체 애플리케이션 설정. 모든 모듈은 본 객체에 의존한다."""

    llm: LlmConfig
    batch: BatchConfig
    dataset: DatasetConfig
    interview: InterviewConfig
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
            "base_url": "http://localhost:8080/v1",
            "model": "unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit",
            "max_tokens": 500,
            "temperature": 0.8,
            "timeout": 120,
            "context_budget": 8000,
            "retry_max_attempts": 3,
            "retry_backoff_seconds": [1, 2, 4],
            # GATE-1에서 검증: Qwen3.6-35B-A3B는 default가 thinking on이라
            # reasoning이 토큰 예산을 소진해 content가 비어 온다. False가 정상.
            "enable_thinking": False,
        },
        "batch": {
            "concurrency": 2,
            "persona_fields": ["summary"],
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
# 환경변수 매핑
# ---------------------------------------------------------------------------


def _coerce(value: str, target: Any) -> Any:
    """환경변수 문자열을 target 타입에 맞춰 변환한다."""

    if isinstance(target, bool):
        return value.lower() in ("1", "true", "yes", "on")
    if isinstance(target, int):
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"환경변수 정수 변환 실패: {value!r}") from exc
    if isinstance(target, float):
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(f"환경변수 실수 변환 실패: {value!r}") from exc
    return value


def _apply_env(merged: dict) -> dict:
    """KPI_* 환경변수를 merged dict에 덮어쓴다.

    명세는 TDD §10이다. 누락된 키는 무시한다(전체 set 일관성을 강요하지 않음).
    """

    out = {k: dict(v) if isinstance(v, dict) else v for k, v in merged.items()}

    env_map = [
        ("KPI_LLM_BASE_URL", "llm", "base_url"),
        ("KPI_LLM_MODEL", "llm", "model"),
        ("KPI_LLM_MAX_TOKENS", "llm", "max_tokens"),
        ("KPI_LLM_TEMPERATURE", "llm", "temperature"),
        ("KPI_LLM_TIMEOUT", "llm", "timeout"),
        ("KPI_LLM_ENABLE_THINKING", "llm", "enable_thinking"),
        ("KPI_BATCH_CONCURRENCY", "batch", "concurrency"),
        ("KPI_OUTPUT_DIR", "output", "output_dir"),
        ("KPI_LOG_LEVEL", "output", "log_level"),
        ("KPI_NO_COLOR", "output", "no_color"),
    ]
    for env_key, section, key in env_map:
        raw = os.environ.get(env_key)
        if raw is None:
            continue
        target = out.get(section, {}).get(key)
        out[section][key] = _coerce(raw, target)

    # 콤마 구분 리스트 처리
    fields_raw = os.environ.get("KPI_BATCH_PERSONA_FIELDS")
    if fields_raw is not None:
        items = [s.strip() for s in fields_raw.split(",") if s.strip()]
        out.setdefault("batch", {})["persona_fields"] = items

    return out


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def is_local_base_url(base_url: str) -> bool:
    """base_url이 localhost 계열인지 판정한다.

    chat() 차단 가드용 헬퍼다(security.md §1, TDD §13). healthcheck()에는
    경고만 남기고 실제 차단은 chat() 진입 시점에서 확인한다.
    """

    if not isinstance(base_url, str):
        return False
    return base_url.startswith(LOCAL_BASE_URL_PREFIXES)


def load_config(
    yaml_path: Optional[Path] = None,
    cli_overrides: Optional[dict] = None,
) -> AppConfig:
    """AppConfig를 우선순위 머지로 로드한다.

    우선순위는 default → yaml → env(``KPI_*``) → ``cli_overrides``이다.

    Args:
        yaml_path: ``config.yaml`` 경로. ``None``이면 ``DEFAULT_YAML_PATH``.
        cli_overrides: CLI 옵션이 주는 dict 부분 갱신.

    Returns:
        검증된 AppConfig.

    Raises:
        ConfigError: yaml 파싱/타입 오류, 동시성 범위 위반 등.
    """

    yaml_path = yaml_path or DEFAULT_YAML_PATH
    merged = _default_dict()
    merged = _deep_merge(merged, _load_yaml(yaml_path))
    merged = _apply_env(merged)
    if cli_overrides:
        merged = _deep_merge(merged, cli_overrides)

    try:
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
            enable_thinking=bool(merged["llm"].get("enable_thinking", False)),
        )
        batch_cfg = BatchConfig(
            concurrency=int(merged["batch"]["concurrency"]),
            persona_fields=tuple(str(x) for x in merged["batch"]["persona_fields"]),
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
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"설정 필드 변환 실패: {exc}") from exc

    return AppConfig(
        llm=llm_cfg,
        batch=batch_cfg,
        dataset=dataset_cfg,
        interview=interview_cfg,
        output_dir=Path(str(merged["output"]["output_dir"])),
        log_level=str(merged["output"]["log_level"]),
        no_color=bool(merged["output"]["no_color"]),
    )
