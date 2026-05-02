"""``src.config.load_config`` 우선순위와 검증 단위 테스트.

- default → yaml → env(``KPI_*``) → CLI 우선순위 머지
- localhost 가드 ``is_local_base_url``
- BatchConfig의 동시성 1-3 강제, 4 이상 ``ConfigError``
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import (
    AppConfig,
    BatchConfig,
    DatasetConfig,
    InterviewConfig,
    LlmConfig,
    is_local_base_url,
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
    assert cfg.llm.base_url == "http://localhost:8080/v1"
    assert cfg.llm.enable_thinking is False
    assert cfg.batch.concurrency == 2
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
    assert cfg.llm.base_url == "http://localhost:8080/v1"


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
# 환경변수 우선순위(yaml < env)
# ---------------------------------------------------------------------------


def test_load_config_env_yaml_우선(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("llm:\n  model: 'yaml-model'\n", encoding="utf-8")
    monkeypatch.setenv("KPI_LLM_MODEL", "env-model")

    cfg = load_config(yaml_path=yaml_path)
    assert cfg.llm.model == "env-model"


def test_load_config_env_int_변환(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPI_BATCH_CONCURRENCY", "3")
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.batch.concurrency == 3


def test_load_config_env_bool_변환(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPI_NO_COLOR", "true")
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.no_color is True


def test_load_config_env_persona_fields_콤마_분리(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPI_BATCH_PERSONA_FIELDS", "summary,professional,family")
    cfg = load_config(yaml_path=tmp_path / "no.yaml")
    assert cfg.batch.persona_fields == ("summary", "professional", "family")


def test_load_config_env_정수_변환_실패_ConfigError(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPI_BATCH_CONCURRENCY", "abc")
    with pytest.raises(ConfigError):
        load_config(yaml_path=tmp_path / "no.yaml")


# ---------------------------------------------------------------------------
# CLI override 최우선
# ---------------------------------------------------------------------------


def test_load_config_cli_override가_env보다_우선(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPI_LLM_MODEL", "env-model")
    cfg = load_config(
        yaml_path=tmp_path / "no.yaml",
        cli_overrides={"llm": {"model": "cli-model"}},
    )
    assert cfg.llm.model == "cli-model"


# ---------------------------------------------------------------------------
# is_local_base_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://localhost:8080/v1", True),
        ("http://127.0.0.1:8080/v1", True),
        ("http://localhost", True),
        ("https://api.example.com/v1", False),
        ("https://localhost:8080/v1", False),  # https는 명시적으로 가드 대상
        ("", False),
        (None, False),
    ],
)
def test_is_local_base_url_분기(url, expected) -> None:
    assert is_local_base_url(url) is expected


# ---------------------------------------------------------------------------
# 동시성 1-3 강제(BatchConfig.__post_init__)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("c", [1, 2, 3])
def test_batch_config_동시성_허용범위_생성_성공(c: int) -> None:
    BatchConfig(concurrency=c, persona_fields=("summary",))


@pytest.mark.parametrize("c", [0, 4, 8, -1])
def test_batch_config_동시성_범위외_ConfigError(c: int) -> None:
    with pytest.raises(ConfigError):
        BatchConfig(concurrency=c, persona_fields=("summary",))


def test_load_config_동시성_4_ConfigError(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("batch:\n  concurrency: 4\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(yaml_path=yaml_path)


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
