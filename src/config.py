"""Layered configuration loader.

Precedence is built-in defaults, then ``config.yaml``, then ``.env`` file,
then environment variables, then CLI overrides. The result is returned as a
frozen ``AppConfig`` dataclass.

Secrets (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``) come from the environment
or a project-root ``.env`` file. The ``.env`` parser is implemented with the
standard library to avoid a ``python-dotenv`` dependency. It supports
``KEY=value`` lines, ``#`` comments, blank lines, single/double quoted values,
and the ``export KEY=value`` shell prefix. Multiline values and escapes are
intentionally not supported.
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


@dataclass(frozen=True)
class LlmConfig:
    """LLM HTTP client settings.

    ``provider`` selects between the OpenAI Chat Completions API and the
    Anthropic Messages API. Local LLMs (mlx_lm.server, vLLM, llama.cpp) are
    addressed through ``provider=openai`` with ``base_url`` overridden to
    ``http://localhost:PORT/v1``; they accept any non-empty ``api_key``.

    ``api_key`` is read from ``OPENAI_API_KEY`` (or ``ANTHROPIC_API_KEY`` for
    ``provider=anthropic``). The yaml key is intentionally not honored so
    secrets never land on disk or in shell history.

    The numeric ranges enforced in ``__post_init__`` are operational guard
    rails against unrealistic yaml or environment values.
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
    """Batch interview concurrency and persona-toggle settings.

    Concurrency 1-10 is a soft cap intended to leave headroom under typical
    OpenAI tier rate limits. ``partial_failure_threshold`` (0.0-1.0) is the
    success ratio under which ``BatchResultEnvelope.partial_failure`` flips
    true and the CLI exits with code 3.
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
    """Hugging Face dataset name, split, and column-mapping aliases."""

    name: str
    split: str
    field_map: dict
    gender_aliases: dict
    province_aliases: dict


@dataclass(frozen=True)
class InterviewConfig:
    """Interview heuristic thresholds and keyword lists.

    Externalizing these lets users tune the auto follow-up trigger and persona
    drift detector from yaml without touching code. Out-of-range values are
    rejected in ``__post_init__``.
    """

    short_answer_threshold: int
    english_ratio_threshold: float
    ambiguous_keywords: tuple
    refusal_keywords: tuple
    auto_follow_up_text: str = "조금만 더 자세히 말씀해 주실 수 있을까요?"
    auto_follow_up_max: int = 1
    system_prompt_path: str = "prompts/system_prompt.txt"
    occupation_english_whitelist: bool = True

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
        if not isinstance(self.system_prompt_path, str) or not self.system_prompt_path.strip():
            raise ConfigError(
                "interview.system_prompt_path는 빈 문자열이 아닌 str이어야 한다"
            )


@dataclass(frozen=True)
class ReportConfig:
    """Report rendering thresholds and bin widths."""

    cohort_min_cell: int = 3
    top_n_default: int = 10
    histogram_bins: int = 10
    bar_width: int = 30
    insight_model: Optional[str] = None

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
    """Top-level application configuration."""

    llm: LlmConfig
    batch: BatchConfig
    dataset: DatasetConfig
    interview: InterviewConfig
    report: ReportConfig
    output_dir: Path
    log_level: str
    no_color: bool


def _default_dict() -> dict:
    """Built-in defaults used as the lowest precedence merge layer."""

    return {
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
            "persona_fields": ["summary"],
            "single_turn": False,
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
            "system_prompt_path": "prompts/system_prompt.txt",
            "occupation_english_whitelist": True,
        },
        "report": {
            "cohort_min_cell": 3,
            "top_n_default": 10,
            "histogram_bins": 10,
            "bar_width": 30,
            "insight_model": None,
        },
        "output": {
            "output_dir": "outputs/",
            "log_level": "INFO",
            "no_color": False,
        },
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into ``base``. ``override`` wins."""

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
    """Read a yaml file into a dict. Missing files yield an empty dict."""

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
    """Parse a ``.env`` file with the standard library only.

    Search order when ``path`` is ``None``: current working directory, then the
    project root. Returns an empty dict when no file is found.
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
    """Promote environment variables into the merged config dict.

    Only secrets and the output directory are honored:

    - ``OPENAI_API_KEY`` (or ``KPI_OPENAI_API_KEY`` fallback): used when
      ``llm.provider == "openai"``.
    - ``ANTHROPIC_API_KEY``: used when ``llm.provider == "anthropic"``.
    - ``KPI_OUTPUT_DIR``: convenience override for tests/CI.
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
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get(
            "KPI_OPENAI_API_KEY"
        )
    if api_key:
        llm_section["api_key"] = api_key

    return out


def load_config(
    yaml_path: Optional[Path] = None,
    cli_overrides: Optional[dict] = None,
) -> AppConfig:
    """Load and validate ``AppConfig`` from layered sources.

    Args:
        yaml_path: Path to ``config.yaml``. Defaults to ``DEFAULT_YAML_PATH``.
        cli_overrides: Partial dict from CLI options that overrides everything
            else.

    Raises:
        ConfigError: yaml parse failure, type mismatch, or out-of-range value.
    """

    for key, value in _load_dotenv().items():
        os.environ.setdefault(key, value)

    yaml_path = yaml_path or DEFAULT_YAML_PATH
    merged = _default_dict()
    merged = _deep_merge(merged, _load_yaml(yaml_path))
    # Drop legacy ``llm.backend`` entries so existing yaml files do not break
    # construction. The toggle was removed when MCP became sampling-only.
    if isinstance(merged.get("llm"), dict):
        merged["llm"].pop("backend", None)
    merged = _apply_env(merged)
    if cli_overrides:
        merged = _deep_merge(merged, cli_overrides)
        # CLI may flip ``provider`` after the env pass already chose a key. Run
        # the env pass once more so ``--provider anthropic`` picks up
        # ``ANTHROPIC_API_KEY`` and ``--provider openai`` picks up
        # ``OPENAI_API_KEY``. The CLI cannot supply ``api_key`` itself (yaml
        # and CLI both reject it), so this never overwrites a user-set key.
        merged = _apply_env(merged)

    try:
        api_key_raw = merged["llm"].get("api_key")
        api_key_val: Optional[str] = (
            str(api_key_raw) if api_key_raw not in (None, "") else None
        )
        provider = str(merged["llm"].get("provider", "openai")).strip().lower()
        # When provider differs from the historical default, swap the matching
        # default base_url and model so callers can flip with just
        # ``--provider anthropic``. A user-customized ``base_url`` (anything
        # other than the ``api.openai.com`` default) is respected as-is.
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
            system_prompt_path=str(
                merged["interview"].get(
                    "system_prompt_path", "prompts/system_prompt.txt"
                )
            ),
            occupation_english_whitelist=bool(
                merged["interview"].get("occupation_english_whitelist", True)
            ),
        )
        report_raw = merged.get("report") or {}
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
