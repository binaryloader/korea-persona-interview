"""MCP 도구 호출 공통 셋업(로깅, 라벨).

각 도구 핸들러가 호출 시작 시점에 동일하게 적용하는 setup 코드를 한 곳에서 둔다.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from ..config import AppConfig
from ..logging_setup import bind_request_id, configure_logging


logger = logging.getLogger(__name__)


def setup_logging_for_run(config: AppConfig) -> None:
    """도구 호출마다 새로운 request id로 구조화 로깅을 구성한다."""

    log_dir = config.output_dir / "logs"
    log_path = log_dir / f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"
    configure_logging(level=config.log_level, json_path=log_path)
    bind_request_id(uuid.uuid4().hex)


def backend_label(config: AppConfig) -> str:
    """현재 모드에 대응하는 응답 라벨(``mcp_server`` 또는 ``mcp_orchestrator``)."""

    return "mcp_server" if config.mcp.mode == "server" else "mcp_orchestrator"
