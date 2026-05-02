"""``src.config.load_config`` 우선순위와 검증 단위 테스트(OpenAI 백엔드).

- default → yaml → env(``KPI_*``, ``OPENAI_API_KEY``) → CLI 우선순위 머지
- BatchConfig의 동시성 1-3 강제, 4 이상 ``ConfigError``
- OpenAI 외부 URL 허용(이전 버전의 localhost 가드 제거)
- ``OPENAI_API_KEY``/``KPI_OPENAI_API_KEY`` 환경변수 로드 정합
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import (
    AppConfig,
    BatchConfig,
    InterviewConfig,
    LlmConfig,
    load_config,
)
from src.models import ConfigError


# ---------------------------------------------------------------------------
# 기본값(default)
# ---------------------------------------------------------------------------


def test_load_config_default_사용가능_yaml_없을때(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """yaml 미존재 시 default가 그대로 적용된다."""

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert isinstance(cfg, AppConfig)
    assert cfg.llm.base_url == "https://api.openai.com/v1"
    assert cfg.llm.model == "gpt-4o-mini"
    assert cfg.llm.api_key is None
    assert cfg.batch.concurrency == 4
    assert cfg.dataset.name == "nvidia/Nemotron-Personas-Korea"


# ---------------------------------------------------------------------------
# yaml 로드
# ---------------------------------------------------------------------------


def test_load_config_yaml_부분_갱신_default와_머지(tmp_path: Path) -> None:
    """yaml 일부 키만 정의해도 default와 깊은 병합된다."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "llm:\n"
        "  model: 'custom-model'\n"
        "  max_tokens: 1234\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.llm.model == "custom-model"
    assert cfg.llm.max_tokens == 1234
    # 다른 키는 default 유지
    assert cfg.llm.base_url == "https://api.openai.com/v1"


def test_load_config_yaml_파싱_실패_ConfigError(tmp_path: Path) -> None:
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text("llm:\n  model: 'unbalanced", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(yaml_path=yaml_path)


def test_load_config_yaml_최상위_dict_아님_ConfigError(tmp_path: Path) -> None:
    yaml_path = tmp_path / "list.yaml"
    yaml_path.write_text("- item1\n- item2", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(yaml_path=yaml_path)


# ---------------------------------------------------------------------------
# 환경변수 정책(v1.x): 비밀만 환경변수에서 받는다
# ---------------------------------------------------------------------------
#
# v1.0 시절의 ``KPI_LLM_*``/``KPI_BATCH_*`` 환경변수 override는 v1.x에서 제거됐다.
# 정책은 "비밀=env, 기본=yaml, 일회성=CLI" 한 가지로 단순화한다. 본 절의 회귀
# 테스트는 (1) 일반 설정은 환경변수로 덮이지 않고 (2) 비밀(API 키)과 출력 디렉토리
# 두 키만 환경변수에서 받는다는 사실을 박는다.


def test_load_config_v1_x_KPI_LLM_MODEL_env_무시(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1.x: ``KPI_LLM_MODEL``은 더 이상 인정되지 않는다. yaml 기본이 우선이다.

    v1.0의 KPI_LLM_* 환경변수 override는 비밀이 아닌 일반 설정값을 셸/CI 환경에서
    조작 가능하게 했지만, 우선순위 분기가 늘면서 디버깅 비용이 컸다. v1.x부터는
    yaml(기본)과 CLI(일회성) 두 경로만 인정한다.
    """

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("llm:\n  model: 'yaml-model'\n", encoding="utf-8")
    monkeypatch.setenv("KPI_LLM_MODEL", "env-model-should-be-ignored")

    cfg = load_config(yaml_path=yaml_path)
    assert cfg.llm.model == "yaml-model"


def test_load_config_v1_x_KPI_BATCH_CONCURRENCY_env_무시(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1.x: ``KPI_BATCH_CONCURRENCY``는 더 이상 인정되지 않는다."""

    monkeypatch.setenv("KPI_BATCH_CONCURRENCY", "3")
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    # default 4가 그대로 적용된다.
    assert cfg.batch.concurrency == 4


def test_load_config_v1_x_KPI_NO_COLOR_env_무시(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1.x: ``KPI_NO_COLOR``는 더 이상 인정되지 않는다(CLI ``--no-color``로 일원화)."""

    monkeypatch.setenv("KPI_NO_COLOR", "true")
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.no_color is False


def test_load_config_v1_x_KPI_BATCH_PERSONA_FIELDS_env_무시(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1.x: ``KPI_BATCH_PERSONA_FIELDS``는 더 이상 인정되지 않는다."""

    monkeypatch.setenv("KPI_BATCH_PERSONA_FIELDS", "summary,professional,family")
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.batch.persona_fields == ("summary",)


def test_load_config_v1_x_KPI_OUTPUT_DIR_env_유지(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``KPI_OUTPUT_DIR``은 비밀은 아니지만 테스트/CI 격리 편의를 위해 환경변수 유지."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path / "outdir"))
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.output_dir == tmp_path / "outdir"


# ---------------------------------------------------------------------------
# CLI override
# ---------------------------------------------------------------------------


def test_load_config_cli_override_model(
    tmp_path: Path,
) -> None:
    """``--model`` CLI override가 yaml 기본값을 덮는다(v1.x 모델 변경 표준 경로)."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("llm:\n  model: 'yaml-model'\n", encoding="utf-8")
    cfg = load_config(
        yaml_path=yaml_path,
        cli_overrides={"llm": {"model": "cli-model"}},
    )
    assert cfg.llm.model == "cli-model"


def test_load_config_cli_override_default_model_없을때(
    tmp_path: Path,
) -> None:
    """yaml 미존재 + cli override 미지정이면 default(``gpt-4o-mini``)가 그대로 들어온다."""

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.model == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# 외부 base_url 허용(이전 버전의 localhost 가드 제거)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "https://api.example.com/v1",
        "http://localhost:8080/v1",  # OpenAI 호환 로컬 프록시도 허용
    ],
)
def test_load_config_외부_URL_허용(url: str, tmp_path: Path) -> None:
    """v1.x 백엔드 전환 후 external base_url을 그대로 허용한다.

    사업 아이템이 외부로 송신되는 사실은 README/PRD에 명시되어 있다. 본 테스트는
    AppConfig 생성 자체가 차단되지 않음을 확인한다.
    """

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(f"llm:\n  base_url: '{url}'\n", encoding="utf-8")
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.llm.base_url == url


# ---------------------------------------------------------------------------
# OPENAI_API_KEY / KPI_OPENAI_API_KEY 로드
# ---------------------------------------------------------------------------


def test_load_config_OPENAI_API_KEY_표준_환경변수(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OPENAI_API_KEY``는 표준 환경변수로 ``llm.api_key``에 박힌다."""

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-standard")
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.api_key == "sk-from-standard"


def test_load_config_KPI_OPENAI_API_KEY_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """표준 키가 없을 때 ``KPI_OPENAI_API_KEY``가 fallback으로 적용된다."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("KPI_OPENAI_API_KEY", "sk-from-fallback")
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.api_key == "sk-from-fallback"


def test_load_config_OPENAI_API_KEY가_KPI보다_우선(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """둘 다 set되어 있으면 표준 ``OPENAI_API_KEY``가 fallback을 덮는다."""

    monkeypatch.setenv("OPENAI_API_KEY", "sk-standard")
    monkeypatch.setenv("KPI_OPENAI_API_KEY", "sk-fallback")
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.api_key == "sk-standard"


def test_load_config_API_KEY_누락_None(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """둘 다 누락이면 ``api_key``는 None으로 남고 호출 시점 ConfigError로 차단된다."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KPI_OPENAI_API_KEY", raising=False)
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.api_key is None


# ---------------------------------------------------------------------------
# provider 옵션 + ANTHROPIC_API_KEY env
# ---------------------------------------------------------------------------


def test_load_config_provider_default_openai(tmp_path: Path) -> None:
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.provider == "openai"


def test_load_config_provider_anthropic_yaml(tmp_path: Path) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "llm:\n  provider: 'anthropic'\n  model: 'claude-haiku-4-5'\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.llm.provider == "anthropic"
    assert cfg.llm.model == "claude-haiku-4-5"


def test_load_config_provider_anthropic는_anthropic_base_url_default(
    tmp_path: Path,
) -> None:
    """``provider=anthropic``이고 ``base_url`` override가 없으면 anthropic 엔드포인트가 적용된다."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "llm:\n  provider: 'anthropic'\n  base_url: null\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.llm.base_url == "https://api.anthropic.com/v1"


def test_load_config_provider_anthropic_default_model_claude_haiku(
    tmp_path: Path,
) -> None:
    """``provider=anthropic``이면 default model이 claude-haiku로 바뀐다."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "llm:\n  provider: 'anthropic'\n  model: null\n  base_url: null\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.llm.model == "claude-haiku-4-5"


def test_load_config_허용_외_provider_ConfigError(tmp_path: Path) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("llm:\n  provider: 'cohere'\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(yaml_path=yaml_path)


def test_load_config_ANTHROPIC_API_KEY_provider_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``provider=anthropic``이면 ``ANTHROPIC_API_KEY``가 ``llm.api_key``에 박힌다."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("llm:\n  provider: 'anthropic'\n", encoding="utf-8")
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.llm.api_key == "sk-ant-from-env"


def test_load_config_ANTHROPIC_API_KEY는_provider_openai에선_미사용(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``provider=openai``이면 ANTHROPIC_API_KEY가 set되어 있어도 무시된다."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-used")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.api_key is None


def test_load_config_cli_override_base_url(
    tmp_path: Path,
) -> None:
    """``--base-url``로 들어온 CLI override가 yaml/default를 덮어쓴다."""

    cfg = load_config(
        yaml_path=tmp_path / "no.yaml",
        cli_overrides={"llm": {"base_url": "http://localhost:8080/v1"}},
    )
    assert cfg.llm.base_url == "http://localhost:8080/v1"


def test_load_config_cli_override_provider(tmp_path: Path) -> None:
    cfg = load_config(
        yaml_path=tmp_path / "no.yaml",
        cli_overrides={"llm": {"provider": "anthropic"}},
    )
    assert cfg.llm.provider == "anthropic"


def test_load_config_legacy_backend_필드_무시(tmp_path: Path) -> None:
    """이전 버전 yaml에 남은 ``llm.backend``는 graceful하게 무시된다."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "llm:\n  backend: 'auto'\n  provider: 'openai'\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.llm.provider == "openai"
    # ``backend`` field is not on LlmConfig anymore.
    assert not hasattr(cfg.llm, "backend")


# ---------------------------------------------------------------------------
# 동시성 1-3 강제(BatchConfig.__post_init__)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("c", [1, 2, 3, 4, 5, 8, 10])
def test_batch_config_동시성_허용범위_생성_성공(c: int) -> None:
    BatchConfig(concurrency=c, persona_fields=("summary",))


@pytest.mark.parametrize("c", [0, 11, 16, -1])
def test_batch_config_동시성_범위외_ConfigError(c: int) -> None:
    with pytest.raises(ConfigError):
        BatchConfig(concurrency=c, persona_fields=("summary",))


def test_load_config_동시성_11_ConfigError(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("batch:\n  concurrency: 11\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(yaml_path=yaml_path)


def test_load_config_동시성_10_허용(tmp_path: Path) -> None:
    """OpenAI 백엔드 전환 후 동시성 10은 허용 범위 내다."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("batch:\n  concurrency: 10\n", encoding="utf-8")
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.batch.concurrency == 10


def test_load_config_동시성_default_4(tmp_path: Path) -> None:
    """default 동시성은 4(OpenAI 백엔드 안정 동시성, MLX 시절 2 → OpenAI 4)."""

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.batch.concurrency == 4


@pytest.mark.parametrize("c", [4, 5, 8, 10])
def test_load_config_동시성_상향_허용_4_to_10(tmp_path: Path, c: int) -> None:
    """v1.0의 1-3 상한이 v1.x OpenAI 백엔드에서 1-10으로 상향됐는지 검증한다.

    회귀를 막는 목적이라 yaml 갱신/cli override 두 경로 모두 검증한다.
    """

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(f"batch:\n  concurrency: {c}\n", encoding="utf-8")
    cfg_yaml = load_config(yaml_path=yaml_path)
    assert cfg_yaml.batch.concurrency == c

    cfg_cli = load_config(
        yaml_path=tmp_path / "no.yaml",
        cli_overrides={"batch": {"concurrency": c}},
    )
    assert cfg_cli.batch.concurrency == c


# ---------------------------------------------------------------------------
# AppConfig 구조 sanity
# ---------------------------------------------------------------------------


def test_load_config_dataset_field_map_default_보존(tmp_path: Path) -> None:
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.dataset.field_map["gender"] == "sex"
    assert cfg.dataset.field_map["region"] == "province"
    assert cfg.dataset.field_map["name"] is None


def test_load_config_interview_default_keywords(tmp_path: Path) -> None:
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert "글쎄요" in cfg.interview.ambiguous_keywords
    assert "I cannot" in cfg.interview.refusal_keywords
    assert cfg.interview.short_answer_threshold == 20
    assert cfg.interview.english_ratio_threshold == 0.30
    # 라운드 B2: auto_follow_up_text/max도 default에서 노출된다.
    assert cfg.interview.auto_follow_up_text == "조금만 더 자세히 말씀해 주실 수 있을까요?"
    assert cfg.interview.auto_follow_up_max == 1


def test_load_config_interview_yaml_override(tmp_path: Path) -> None:
    """yaml에서 인터뷰 임계값을 변경하면 그대로 반영된다(라운드 B2)."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "interview:\n"
        "  short_answer_threshold: 30\n"
        "  english_ratio_threshold: 0.5\n"
        "  ambiguous_keywords:\n"
        "    - \"별 생각 없어\"\n"
        "  refusal_keywords:\n"
        "    - \"못하겠습니다\"\n"
        "  auto_follow_up_text: \"한 번만 더 부탁드릴게요\"\n"
        "  auto_follow_up_max: 0\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.interview.short_answer_threshold == 30
    assert cfg.interview.english_ratio_threshold == 0.5
    assert cfg.interview.ambiguous_keywords == ("별 생각 없어",)
    assert cfg.interview.refusal_keywords == ("못하겠습니다",)
    assert cfg.interview.auto_follow_up_text == "한 번만 더 부탁드릴게요"
    assert cfg.interview.auto_follow_up_max == 0


@pytest.mark.parametrize("threshold", [-1, -100])
def test_InterviewConfig_short_answer_threshold_음수_ConfigError(threshold: int) -> None:
    """short_answer_threshold 음수는 ConfigError(라운드 B2)."""

    with pytest.raises(ConfigError):
        from src.config import InterviewConfig

        InterviewConfig(
            short_answer_threshold=threshold,
            english_ratio_threshold=0.3,
            ambiguous_keywords=(),
            refusal_keywords=(),
        )


@pytest.mark.parametrize("ratio", [-0.1, 1.1, 2.0])
def test_InterviewConfig_english_ratio_범위외_ConfigError(ratio: float) -> None:
    """english_ratio_threshold가 0-1 범위 밖이면 ConfigError(라운드 B2)."""

    with pytest.raises(ConfigError):
        from src.config import InterviewConfig

        InterviewConfig(
            short_answer_threshold=20,
            english_ratio_threshold=ratio,
            ambiguous_keywords=(),
            refusal_keywords=(),
        )


def test_InterviewConfig_auto_follow_up_text_빈_문자열_ConfigError() -> None:
    """auto_follow_up_text가 비면 ConfigError(라운드 B2)."""

    with pytest.raises(ConfigError):
        from src.config import InterviewConfig

        InterviewConfig(
            short_answer_threshold=20,
            english_ratio_threshold=0.3,
            ambiguous_keywords=(),
            refusal_keywords=(),
            auto_follow_up_text="   ",
        )


# ---------------------------------------------------------------------------
# 라운드 B3: ReportConfig/BatchConfig 외부화
# ---------------------------------------------------------------------------


def test_load_config_report_default(tmp_path: Path) -> None:
    """ReportConfig default 값이 yaml 없을 때도 노출된다(라운드 B3)."""

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.report.cohort_min_cell == 3
    assert cfg.report.top_n_default == 10
    assert cfg.report.histogram_bins == 10
    assert cfg.report.bar_width == 30


def test_load_config_report_yaml_override(tmp_path: Path) -> None:
    """yaml에서 ReportConfig 값을 변경하면 그대로 반영된다."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "report:\n"
        "  cohort_min_cell: 5\n"
        "  top_n_default: 20\n"
        "  histogram_bins: 5\n"
        "  bar_width: 20\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.report.cohort_min_cell == 5
    assert cfg.report.top_n_default == 20
    assert cfg.report.histogram_bins == 5
    assert cfg.report.bar_width == 20


@pytest.mark.parametrize("c", [0, -1])
def test_ReportConfig_cohort_min_cell_범위외_ConfigError(c: int) -> None:
    with pytest.raises(ConfigError):
        from src.config import ReportConfig

        ReportConfig(cohort_min_cell=c)


@pytest.mark.parametrize("w", [0, -1, 201, 1000])
def test_ReportConfig_bar_width_범위외_ConfigError(w: int) -> None:
    with pytest.raises(ConfigError):
        from src.config import ReportConfig

        ReportConfig(bar_width=w)


def test_load_config_batch_partial_failure_threshold_default(tmp_path: Path) -> None:
    """partial_failure_threshold default는 0.5(라운드 B3)."""

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.batch.partial_failure_threshold == 0.5


def test_load_config_report_insight_model_default_None(tmp_path: Path) -> None:
    """report.insight_model default는 None(라운드 G13)."""

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.report.insight_model is None


def test_load_config_report_insight_model_yaml_override(tmp_path: Path) -> None:
    """yaml의 report.insight_model이 ReportConfig에 그대로 반영된다."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "report:\n  insight_model: gpt-4o\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.report.insight_model == "gpt-4o"


def test_load_config_report_insight_model_cli_override(tmp_path: Path) -> None:
    """CLI overrides도 report.insight_model을 갈아 끼운다."""

    cfg = load_config(
        yaml_path=tmp_path / "no.yaml",
        cli_overrides={"report": {"insight_model": "claude-sonnet-4-5"}},
    )
    assert cfg.report.insight_model == "claude-sonnet-4-5"


def test_load_config_batch_partial_failure_threshold_override(tmp_path: Path) -> None:
    """yaml override가 그대로 반영된다."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "batch:\n"
        "  partial_failure_threshold: 0.8\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.batch.partial_failure_threshold == 0.8


@pytest.mark.parametrize("t", [-0.1, 1.1, 2.0])
def test_BatchConfig_partial_failure_threshold_범위외_ConfigError(t: float) -> None:
    with pytest.raises(ConfigError):
        from src.config import BatchConfig

        BatchConfig(
            concurrency=2,
            persona_fields=("summary",),
            partial_failure_threshold=t,
        )


# ---------------------------------------------------------------------------
# LlmConfig 상한 검증(__post_init__)
# ---------------------------------------------------------------------------


def _llm_config_kwargs(**override) -> dict:
    base = {
        "base_url": "https://api.openai.com/v1",
        "model": "test-model",
        "max_tokens": 500,
        "temperature": 0.5,
        "timeout": 60.0,
        "context_budget": 32000,
        "retry_max_attempts": 3,
        "retry_backoff_seconds": (1.0, 2.0, 4.0),
        "api_key": "test-key",
    }
    base.update(override)
    return base


@pytest.mark.parametrize("max_tokens", [0, -1, 16001, 100000])
def test_LlmConfig_max_tokens_범위외_ConfigError(max_tokens: int) -> None:
    with pytest.raises(ConfigError):
        LlmConfig(**_llm_config_kwargs(max_tokens=max_tokens))


@pytest.mark.parametrize("attempts", [0, -1, 6, 100])
def test_LlmConfig_retry_max_attempts_범위외_ConfigError(attempts: int) -> None:
    with pytest.raises(ConfigError):
        LlmConfig(**_llm_config_kwargs(retry_max_attempts=attempts))


@pytest.mark.parametrize("timeout", [0, -1, 0.5, 601, 3600])
def test_LlmConfig_timeout_범위외_ConfigError(timeout) -> None:
    with pytest.raises(ConfigError):
        LlmConfig(**_llm_config_kwargs(timeout=timeout))


@pytest.mark.parametrize("budget", [0, 999, 128001, 1000000])
def test_LlmConfig_context_budget_범위외_ConfigError(budget: int) -> None:
    with pytest.raises(ConfigError):
        LlmConfig(**_llm_config_kwargs(context_budget=budget))


def test_LlmConfig_허용_범위_생성_성공() -> None:
    """경계값(1, 16000, 5, 600, 1000, 128000)이 모두 통과한다."""

    LlmConfig(
        **_llm_config_kwargs(
            max_tokens=1,
            retry_max_attempts=1,
            timeout=1,
            context_budget=1000,
        )
    )
    LlmConfig(
        **_llm_config_kwargs(
            max_tokens=16000,
            retry_max_attempts=5,
            timeout=600,
            context_budget=128000,
        )
    )


# ---------------------------------------------------------------------------
# .env 파일 로더(_load_dotenv)
# ---------------------------------------------------------------------------


from src.config import _load_dotenv, _parse_dotenv_file  # noqa: E402


def test_load_dotenv_파일_없음_빈_dict(tmp_path: Path) -> None:
    """탐색 경로에 .env가 없으면 빈 dict를 반환한다(조용히 스킵)."""

    missing = tmp_path / "missing.env"
    assert _load_dotenv(missing) == {}


def test_parse_dotenv_file_기본_KEY_VALUE(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=bar\nBAZ=qux\n", encoding="utf-8")

    result = _parse_dotenv_file(env_path)
    assert result == {"FOO": "bar", "BAZ": "qux"}


def test_parse_dotenv_file_주석_빈줄_무시(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# 주석 라인\n"
        "\n"
        "FOO=bar\n"
        "   # 들여쓰기 주석\n"
        "BAZ=qux\n",
        encoding="utf-8",
    )

    result = _parse_dotenv_file(env_path)
    assert result == {"FOO": "bar", "BAZ": "qux"}


def test_parse_dotenv_file_따옴표_제거(tmp_path: Path) -> None:
    """``"..."``와 ``'...'``는 한 쌍에 한해 제거된다."""

    env_path = tmp_path / ".env"
    env_path.write_text(
        'DOUBLE="value with spaces"\n'
        "SINGLE='another value'\n"
        "BARE=naked\n",
        encoding="utf-8",
    )

    result = _parse_dotenv_file(env_path)
    assert result["DOUBLE"] == "value with spaces"
    assert result["SINGLE"] == "another value"
    assert result["BARE"] == "naked"


def test_parse_dotenv_file_export_접두_허용(tmp_path: Path) -> None:
    """``export KEY=value``는 셸 호환을 위해 ``export ``를 떼고 파싱한다."""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "export OPENAI_API_KEY=sk-deadbeef\n"
        "export FOO='quoted'\n",
        encoding="utf-8",
    )

    result = _parse_dotenv_file(env_path)
    assert result["OPENAI_API_KEY"] == "sk-deadbeef"
    assert result["FOO"] == "quoted"


def test_parse_dotenv_file_등호_없는_라인_무시(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("INVALID_LINE\nFOO=bar\n", encoding="utf-8")

    result = _parse_dotenv_file(env_path)
    assert result == {"FOO": "bar"}


def test_load_config_dotenv_파일에서_OPENAI_API_KEY_로드(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.env`` 파일에 ``OPENAI_API_KEY``가 있으면 ``llm.api_key``로 들어온다."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KPI_OPENAI_API_KEY", raising=False)

    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=sk-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.api_key == "sk-from-dotenv"


def test_load_config_dotenv보다_명시_환경변수_우선(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.env``에 키가 있어도 명시 환경변수가 set돼 있으면 명시값이 우선이다."""

    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=sk-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-explicit-env")

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.api_key == "sk-from-explicit-env"


def test_load_config_dotenv_파일_없을때_조용히_무시(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.env``가 없는 디렉토리에서도 load_config가 정상 동작한다."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KPI_OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.api_key is None


def test_load_config_dotenv_따옴표_둘러싼_키_제거(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OPENAI_API_KEY="sk-..."``처럼 따옴표 감싸진 값도 정상 로드된다."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KPI_OPENAI_API_KEY", raising=False)

    env_path = tmp_path / ".env"
    env_path.write_text('OPENAI_API_KEY="sk-quoted-value"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.api_key == "sk-quoted-value"


def test_load_config_dotenv_export_접두로_OPENAI_API_KEY_로드(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KPI_OPENAI_API_KEY", raising=False)

    env_path = tmp_path / ".env"
    env_path.write_text("export OPENAI_API_KEY=sk-from-export\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.api_key == "sk-from-export"


def test_load_config_dotenv_주석_라인_무시(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KPI_OPENAI_API_KEY", raising=False)

    env_path = tmp_path / ".env"
    env_path.write_text(
        "# 시크릿 키\n"
        "# OPENAI_API_KEY=sk-commented-out\n"
        "OPENAI_API_KEY=sk-actual\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.llm.api_key == "sk-actual"


# ---------------------------------------------------------------------------
# mcp.mode 토글(ADR-004)
# ---------------------------------------------------------------------------


def test_mcp_config_default_mode_server(tmp_path: Path) -> None:
    """yaml 미존재일 때 ``mcp.mode`` default는 ``server``다(ADR-004)."""

    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.mcp.mode == "server"


def test_mcp_config_yaml_sampling_override(tmp_path: Path) -> None:
    """yaml에서 ``mcp.mode: sampling``을 명시하면 그대로 반영된다."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("mcp:\n  mode: 'sampling'\n", encoding="utf-8")
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.mcp.mode == "sampling"


def test_mcp_config_yaml_server_override(tmp_path: Path) -> None:
    """yaml에서 ``mcp.mode: server``를 명시해도 정상 통과한다."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("mcp:\n  mode: 'server'\n", encoding="utf-8")
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.mcp.mode == "server"


def test_mcp_config_허용_외_mode_ConfigError(tmp_path: Path) -> None:
    """``mcp.mode``는 화이트리스트(server/sampling) 외 값을 거부한다."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("mcp:\n  mode: 'auto'\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(yaml_path=yaml_path)


def test_mcp_config_대소문자_정규화(tmp_path: Path) -> None:
    """``mcp.mode``는 대문자 입력을 소문자로 정규화한다."""

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("mcp:\n  mode: 'SAMPLING'\n", encoding="utf-8")
    cfg = load_config(yaml_path=yaml_path)
    assert cfg.mcp.mode == "sampling"


def test_mcp_config_dataclass_직접_생성_허용() -> None:
    from src.config import McpConfig

    assert McpConfig().mode == "server"
    assert McpConfig(mode="sampling").mode == "sampling"


def test_mcp_config_dataclass_직접_생성_검증() -> None:
    from src.config import McpConfig

    with pytest.raises(ConfigError):
        McpConfig(mode="auto")
