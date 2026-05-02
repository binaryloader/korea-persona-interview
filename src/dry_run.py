"""Dry-run console renderer for the ``interview --dry-run`` flow.

Single-persona interview executor that prints the system prompt, persona
meta, per-question response, and structured summary to stdout for fast
prompt-debugging cycles. The same code path is used for both the human
console output and the ``--json`` mode wrapper (which suppresses the
human-facing dump and emits a JSON payload from the caller).

Lives separately from ``main.py`` so the click entry point stays focused on
routing and the dry-run rendering logic can be tested in isolation.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .config import AppConfig
from .console import Console
from .interview import InterviewSession
from .llm_backend import build_cli_backend
from .models import PersonaMeta

if TYPE_CHECKING:  # pragma: no cover - type-only import
    pass


async def run_dry_run(
    persona: PersonaMeta,
    product: str,
    questions: list,
    follow_ups: list,
    config: AppConfig,
    console: Console,
    json_mode: bool = False,
) -> None:
    """Run a one-persona interview and print the result to ``console``.

    Args:
        persona: Persona to interview.
        product: Business idea sentence used as the interview topic.
        questions: Main question list (1 or more).
        follow_ups: Optional shared follow-up questions.
        config: Application config (llm, batch, dataset, interview, report).
        console: Console renderer used for the human-facing dump.
        json_mode: When ``True``, skip the human-facing dump entirely. The
            caller is expected to emit the persona meta and result envelope
            as a single JSON payload after this coroutine returns.
    """

    if not json_mode:
        console.info("dry-run 모드: JSON 저장 없이 콘솔에만 출력합니다")

    async with build_cli_backend(config.llm) as client:
        # Health-check is implicit so server failures surface before the
        # interview runs.
        await client.healthcheck()

        session = InterviewSession(
            persona=persona,
            product=product,
            questions=questions,
            follow_up_questions=follow_ups,
            client=client,
            config=config,
        )
        record = await session.run()

    if json_mode:
        # The caller emits a JSON payload covering persona meta and result.
        return

    # System prompt(record.messages[0])
    if record.messages and record.messages[0].role == "system":
        console.echo("--- 시스템 프롬프트 ---")
        console.echo(record.messages[0].content)
        console.echo("")

    console.echo("--- 페르소나 메타 ---")
    console.echo(f"persona_id: {persona.persona_id}")
    console.echo(
        f"이름: {persona.name or '-'}, 성별: {persona.gender}, 연령: {persona.age}, "
        f"지역: {persona.region} {persona.subregion}, 직업: {persona.occupation}"
    )
    console.echo("")

    for i, raw in enumerate(record.raw_responses):
        if i < len(questions):
            console.echo(f"--- 질문 {i + 1}: {questions[i]} ---")
        else:
            fu_idx = i - len(questions)
            label = follow_ups[fu_idx] if fu_idx < len(follow_ups) else "follow-up"
            console.echo(f"--- 사용자 정의 follow-up: {label} ---")
        console.echo(f"응답: {raw.response}")
        console.echo(f"지연: {raw.latency_ms}ms")
        console.echo("")

    console.echo("--- 구조화 요약 ---")
    if record.structured_summary is not None:
        s = record.structured_summary
        # ``acceptable_price_signal``은 schema v2에서 도입된 정성 신호 필드라
        # 본 dump에 함께 포함한다(인터뷰 본문에 명시 숫자가 없어도 정성 가격
        # 신호로 채워진다).
        console.echo(
            json.dumps(
                {
                    "intent": s.intent,
                    "willingness_to_pay": s.willingness_to_pay,
                    "willingness_to_pay_currency": s.willingness_to_pay_currency,
                    "acceptable_price_signal": s.acceptable_price_signal,
                    "rejection_reasons": s.rejection_reasons,
                    "one_line": s.one_line,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        console.echo("(구조화 요약 생성 실패 또는 응답 없음, structured_summary=null)")
    console.echo("")
    console.echo(f"status: {record.status}")
