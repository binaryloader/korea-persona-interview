"""MCP 도구 핸들러 모듈 묶음.

본 패키지는 MCP server 진입점(`src.mcp_server`)이 노출하는 도구별 핸들러를 모드별로 분리해 보관한다. 같은 도구라도 MCP server 모드와 MCP orchestrator 모드에서 노출 여부와 동작 정책이 다르기 때문이다(ADR-005).

모듈 구조는 아래와 같다.

- ``common``: 모든 mode 공통 도구(list_personas, report)
- ``server``: MCP server 모드 전용 도구(healthcheck, interview)
- ``orchestrator``: MCP orchestrator 모드 전용 도구(healthcheck, build_persona_prompt, build_batch_prompts, aggregate_results)
- ``helpers``: 모든 mode 공통 helper 도구(detect_persona_drift, should_auto_follow_up, parse_structured_summary, interview_record_schema)
- ``_payloads``: 응답 봉투 헬퍼(error_payload, persona_to_payload)

본 패키지의 ``HANDLERS`` 매핑이 (mode, tool_name) → coroutine을 dispatch한다. ``TOOLS_BY_MODE``는 mode별 노출 도구 이름 리스트를 박는다. ``mcp_server.dispatch_tool``이 이 두 매핑을 그대로 활용한다.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from . import common, helpers, orchestrator, server


# mode → 도구 이름 리스트. ``list_tools`` 응답이 mode에 맞춰 잘리도록 한다.
TOOLS_BY_MODE: dict = {
    "server": [
        "healthcheck",
        "list_personas",
        "interview",
        "report",
        "detect_persona_drift",
        "should_auto_follow_up",
        "parse_structured_summary",
        "interview_record_schema",
    ],
    "orchestrator": [
        "healthcheck",
        "list_personas",
        "report",
        "build_persona_prompt",
        "build_batch_prompts",
        "aggregate_results",
        "detect_persona_drift",
        "should_auto_follow_up",
        "parse_structured_summary",
        "interview_record_schema",
    ],
}


# (mode, tool_name) → 비동기 핸들러 매핑. ``dispatch_tool``이 본 매핑을 사용한다. 본 mode에서 본 도구가 노출되지 않으면 키가 없어 dispatch 실패가 안내된다.
HANDLERS: dict = {
    # MCP server 모드
    ("server", "healthcheck"): server.healthcheck,
    ("server", "list_personas"): common.list_personas,
    ("server", "interview"): server.interview,
    ("server", "report"): common.report,
    ("server", "detect_persona_drift"): helpers.detect_persona_drift_tool,
    ("server", "should_auto_follow_up"): helpers.should_auto_follow_up_tool,
    ("server", "parse_structured_summary"): helpers.parse_structured_summary_tool,
    ("server", "interview_record_schema"): helpers.interview_record_schema_tool,
    # MCP orchestrator 모드
    ("orchestrator", "healthcheck"): orchestrator.healthcheck,
    ("orchestrator", "list_personas"): common.list_personas,
    ("orchestrator", "report"): common.report,
    ("orchestrator", "build_persona_prompt"): orchestrator.build_persona_prompt,
    ("orchestrator", "build_batch_prompts"): orchestrator.build_batch_prompts,
    ("orchestrator", "aggregate_results"): orchestrator.aggregate_results,
    ("orchestrator", "detect_persona_drift"): helpers.detect_persona_drift_tool,
    ("orchestrator", "should_auto_follow_up"): helpers.should_auto_follow_up_tool,
    ("orchestrator", "parse_structured_summary"): helpers.parse_structured_summary_tool,
    ("orchestrator", "interview_record_schema"): helpers.interview_record_schema_tool,
}


__all__ = [
    "HANDLERS",
    "TOOLS_BY_MODE",
    "common",
    "helpers",
    "orchestrator",
    "server",
]
