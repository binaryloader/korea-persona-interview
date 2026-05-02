"""MCP server 모드 전용 핸들러: healthcheck, interview.

본 모드는 server-side ``OpenAIBackend``/``AnthropicBackend``로 LLM을 직접 호출한다. CLI와 동일한 ``LlmConfig``를 그대로 활용하므로 사용자가 mcp.json의 ``env`` 또는 `.env`에 ``OPENAI_API_KEY``/``ANTHROPIC_API_KEY``를 박아 주어야 한다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..batch import run_batch
from ..config import load_config
from ..llm_backend import build_cli_backend
from ..load_personas import load_and_sample, parse_filter
from ..models import (
    ConfigError,
    DatasetUnavailableError,
    FilterMatchedZeroError,
    ServerNotReachableError,
)
from ._payloads import error_payload
from ._setup import backend_label, setup_logging_for_run


logger = logging.getLogger(__name__)


async def healthcheck(arguments: dict) -> dict:
    """MCP server 모드 healthcheck.

    CLI healthcheck와 동일하게 provider 엔드포인트에 ping 요청을 보낸다. OpenAI는 ``/models``, Anthropic은 1-token messages 호출.
    """

    try:
        config = load_config(yaml_path=None, cli_overrides=None)
    except ConfigError as exc:
        return error_payload("config_error", str(exc), exit_code=1)

    setup_logging_for_run(config)
    label = backend_label(config)

    try:
        backend = build_cli_backend(config.llm)
    except ConfigError as exc:
        return error_payload(
            "config_error", str(exc), exit_code=1, backend=label
        )

    try:
        async with backend as client:
            await client.healthcheck()
    except ServerNotReachableError as exc:
        return error_payload(
            "server_not_reachable",
            f"LLM 서버 도달 실패: {exc}",
            exit_code=1,
            backend=label,
        )
    except ConfigError as exc:
        return error_payload(
            "config_error", str(exc), exit_code=1, backend=label
        )

    return {
        "ok": True,
        "backend": label,
    }


async def interview(arguments: dict) -> dict:
    """배치 인터뷰를 server-side에서 실행한다.

    MCP orchestrator 모드에서는 본 도구가 노출되지 않으며, 호스트 sub-agent가 build_batch_prompts로 시스템 프롬프트를 받아 자기 LLM으로 인터뷰를 수행한 다음 aggregate_results로 리포트를 생성하는 흐름을 사용한다.
    """

    product = arguments.get("product")
    questions = arguments.get("questions")
    if not isinstance(product, str) or not product.strip():
        return error_payload(
            "missing_argument",
            "product(사업 아이템 설명)는 필수입니다",
            exit_code=1,
        )
    if not isinstance(questions, list) or not questions:
        return error_payload(
            "missing_argument",
            "questions(질문 리스트)는 1개 이상 필요합니다",
            exit_code=1,
        )

    filter_spec: Optional[str] = arguments.get("filter")
    persona_ids_raw = arguments.get("persona_ids") or []
    persona_ids_tuple = tuple(str(pid) for pid in persona_ids_raw if str(pid).strip())
    n = int(arguments.get("n", 10))
    seed = int(arguments.get("seed", 42))
    concurrency = int(arguments.get("concurrency", 5))
    persona_fields = arguments.get("persona_fields") or ["summary"]
    follow_ups = arguments.get("follow_ups") or []
    single_turn = bool(arguments.get("single_turn", False))
    output_dir_raw = arguments.get("output_dir") or "outputs/"

    if not (1 <= concurrency <= 10):
        return error_payload(
            "invalid_argument",
            f"concurrency는 1-10 범위만 허용합니다. 입력값: {concurrency}",
            exit_code=1,
        )
    if n < 1:
        return error_payload(
            "invalid_argument",
            f"n은 1 이상이어야 합니다. 입력값: {n}",
            exit_code=1,
        )

    output_dir = Path(str(output_dir_raw))

    overrides: dict = {
        "batch": {
            "concurrency": concurrency,
            "single_turn": single_turn,
        },
        "common": {
            "persona": {"fields": [str(f) for f in persona_fields]},
            "output": {"output_dir": str(output_dir)},
        },
    }

    try:
        config = load_config(yaml_path=None, cli_overrides=overrides)
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

    questions_list = [str(q) for q in questions]
    follow_ups_list = [str(f) for f in follow_ups]

    try:
        personas = load_and_sample(
            filter_str=filter_spec,
            n=len(persona_ids_tuple) if persona_ids_tuple else n,
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

    try:
        backend = build_cli_backend(config.llm)
    except ConfigError as exc:
        return error_payload(
            "config_error", str(exc), exit_code=1, backend=label
        )

    try:
        async with backend as client:
            envelope = await run_batch(
                personas=personas,
                product=product,
                questions=questions_list,
                follow_ups=follow_ups_list,
                llm=client,
                config=config,
                output_dir=output_dir,
                slug="korea-persona-interview",
                seed=seed,
                progress_disable=True,
            )
    except ServerNotReachableError as exc:
        return error_payload(
            "server_not_reachable",
            f"LLM 호출 실패: {exc}",
            exit_code=1,
            backend=label,
        )
    except DatasetUnavailableError as exc:
        return error_payload(
            "dataset_unavailable", str(exc), exit_code=1, backend=label
        )
    except ConfigError as exc:
        return error_payload(
            "config_error", str(exc), exit_code=1, backend=label
        )

    summary = envelope.summary
    usage = envelope.usage
    payload: dict = {
        "ok": not envelope.partial_failure,
        "backend": label,
        "partial_failure": envelope.partial_failure,
        "output_path": str(envelope.output_path) if envelope.output_path else None,
        "summary": {
            "requested": summary.requested,
            "completed": summary.completed,
            "refused": summary.refused,
            "failed": summary.failed,
            "drift": summary.drift,
            "cancelled": summary.cancelled,
        },
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cached_tokens": usage.cached_tokens,
        },
        "model": config.llm.model,
        "failure_reason_counts": dict(envelope.failure_reason_counts),
    }
    return payload
