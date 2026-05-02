"""모든 MCP mode 공통 핸들러: list_personas, report.

list_personas는 LLM 호출이 없으므로 어느 mode에서든 동일하게 동작한다.
report는 정량 집계는 LLM 무관, 정성 인사이트만 LLM 호출이 필요한데, MCP server 모드에서는 server-side LLM을 호출하고 MCP orchestrator 모드에서는 LLM=None을 넘겨 정성 섹션을 fallback 메시지로 채운다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..config import load_config
from ..llm_backend import LLMBackend, build_cli_backend
from ..load_personas import load_and_sample, parse_filter
from ..models import (
    ConfigError,
    DatasetUnavailableError,
    EmptyValidRecordsError,
    FilterMatchedZeroError,
    ServerNotReachableError,
)
from ..report import ReportOptions, generate_report
from ._payloads import error_payload, persona_to_payload
from ._setup import backend_label, setup_logging_for_run


logger = logging.getLogger(__name__)


async def list_personas(arguments: dict) -> dict:
    """필터 결과 페르소나 목록을 돌려준다(모든 mode 공통)."""

    filter_spec: Optional[str] = arguments.get("filter")
    limit = int(arguments.get("limit", 20))
    seed = int(arguments.get("seed", 42))
    persona_ids_raw = arguments.get("persona_ids") or []
    persona_ids_tuple = tuple(str(pid) for pid in persona_ids_raw if str(pid).strip())

    if limit < 1 and not persona_ids_tuple:
        return error_payload(
            "invalid_argument",
            f"limit은 1 이상이어야 합니다. 입력값: {limit}",
            exit_code=1,
        )

    try:
        config = load_config(yaml_path=None, cli_overrides=None)
    except ConfigError as exc:
        return error_payload("config_error", str(exc), exit_code=1)

    setup_logging_for_run(config)
    label = backend_label(config)

    try:
        parse_filter(
            filter_spec,
            config.common.dataset.gender_aliases,
            config.common.dataset.province_aliases,
        )
    except ConfigError as exc:
        return error_payload(
            "config_error", str(exc), exit_code=1, backend=label
        )

    try:
        personas = load_and_sample(
            filter_str=filter_spec,
            n=len(persona_ids_tuple) if persona_ids_tuple else limit,
            seed=seed,
            field_map=config.common.dataset.field_map,
            gender_aliases=config.common.dataset.gender_aliases,
            province_aliases=config.common.dataset.province_aliases,
            dataset_name=config.common.dataset.name,
            split=config.common.dataset.split,
            persona_ids=persona_ids_tuple or None,
        )
    except FilterMatchedZeroError as exc:
        return error_payload(
            "filter_matched_zero", str(exc), exit_code=2, backend=label
        )
    except DatasetUnavailableError as exc:
        return error_payload(
            "dataset_unavailable", str(exc), exit_code=1, backend=label
        )
    except ConfigError as exc:
        return error_payload(
            "config_error", str(exc), exit_code=1, backend=label
        )

    if not personas:
        return error_payload(
            "filter_matched_zero",
            "필터 결과가 비어 있습니다. 조건을 완화해 주세요",
            exit_code=2,
            backend=label,
        )

    return {
        "ok": True,
        "backend": label,
        "personas": [persona_to_payload(p) for p in personas],
        "count": len(personas),
        "filter": filter_spec,
        "seed": seed,
    }


async def report(arguments: dict) -> dict:
    """결과 JSON으로부터 마크다운 리포트를 생성한다(모든 mode 공통).

    MCP server 모드: server-side LLM으로 정성 인사이트까지 채움.
    MCP orchestrator 모드: LLM=None을 넘겨 정성 섹션 fallback. 호스트가 정성 인사이트까지 받으려면 호스트 sub-agent로 직접 인터뷰 흐름을 짜야 한다.
    """

    json_path_raw = arguments.get("json_path")
    if not isinstance(json_path_raw, str) or not json_path_raw.strip():
        return error_payload(
            "missing_argument",
            "json_path는 필수입니다",
            exit_code=1,
        )
    json_path = Path(json_path_raw)
    if not json_path.exists():
        return error_payload(
            "input_file_not_found",
            f"입력 JSON 파일을 찾을 수 없습니다: {json_path}",
            exit_code=1,
        )

    top_n = int(arguments.get("top_n", 10))
    include_drift = bool(arguments.get("include_drift", False))
    output_dir_raw = arguments.get("output_dir")
    output_dir = Path(str(output_dir_raw)) if output_dir_raw else None

    if top_n < 1:
        return error_payload(
            "invalid_argument",
            f"top_n은 1 이상이어야 합니다. 입력값: {top_n}",
            exit_code=1,
        )

    try:
        config = load_config(yaml_path=None, cli_overrides=None)
    except ConfigError as exc:
        return error_payload("config_error", str(exc), exit_code=1)

    setup_logging_for_run(config)
    label = backend_label(config)

    options = ReportOptions(
        top_n=top_n,
        include_drift=include_drift,
        output_dir=output_dir,
    )

    # MCP orchestrator 모드는 server-side LLM 호출이 없으므로 정성 인사이트는 fallback 메시지로 채우고 정량 지표만 렌더링한다.
    backend: Optional[LLMBackend] = None
    if config.mcp.mode != "orchestrator":
        try:
            backend = build_cli_backend(config.llm)
        except ConfigError as exc:
            return error_payload(
                "config_error", str(exc), exit_code=1, backend=label
            )

    try:
        if backend is None:
            report_path = await generate_report(
                json_path=json_path,
                options=options,
                llm=None,
                config=config,
            )
        else:
            async with backend as client:
                report_path = await generate_report(
                    json_path=json_path,
                    options=options,
                    llm=client,
                    config=config,
                )
    except FileNotFoundError:
        return error_payload(
            "input_file_not_found",
            f"입력 JSON 파일을 찾을 수 없습니다: {json_path}",
            exit_code=1,
            backend=label,
        )
    except EmptyValidRecordsError as exc:
        return error_payload(
            "empty_valid_records", str(exc), exit_code=2, backend=label
        )
    except ConfigError as exc:
        return error_payload(
            "input_file_schema", str(exc), exit_code=1, backend=label
        )
    except ServerNotReachableError as exc:
        return error_payload(
            "server_not_reachable",
            f"LLM 호출 실패: {exc}",
            exit_code=1,
            backend=label,
        )

    return {
        "ok": True,
        "backend": label,
        "output_path": str(report_path),
        "input_path": str(json_path),
        "top_n": top_n,
        "include_drift": include_drift,
    }
