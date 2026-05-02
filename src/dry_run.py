"""``interview --dry-run`` 흐름 전용 dry-run 콘솔 렌더러.

단일 페르소나 인터뷰 실행기다. 시스템 프롬프트, 페르소나 메타, 질문별
응답, 구조화 요약을 stdout에 찍어 빠른 프롬프트 디버깅 사이클을 지원한다.
사람이 읽는 콘솔 출력과 ``--json`` 모드 래퍼가 같은 코드 경로를 공유한다
(``--json`` 모드에서는 사람용 dump를 생략하고 caller가 JSON 페이로드를
한 번에 출력한다).

click 진입점이 라우팅 책임에 집중하고 dry-run 렌더 로직은 격리해 단위
테스트할 수 있도록 ``main.py``에서 분리해 본 모듈에 둔다.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .config import AppConfig
from .console import Console
from .interview import InterviewSession
from .llm_backend import build_cli_backend
from .models import PersonaMeta

if TYPE_CHECKING:  # pragma: no cover - 타입 체크 전용 import
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
    """단일 페르소나 인터뷰를 실행해 결과를 ``console``에 찍어 준다.

    인자:
        persona: 인터뷰 대상 페르소나
        product: 인터뷰 주제로 사용되는 사업 아이템 한 문장
        questions: 메인 질문 리스트(1개 이상)
        follow_ups: 공유 follow-up 질문 리스트(선택)
        config: 애플리케이션 설정(llm, batch, dataset, interview, report)
        console: 사람 대상 dump를 위한 콘솔 렌더러
        json_mode: ``True``면 사람용 dump 출력을 모두 생략한다. caller가 본
            코루틴 종료 후 페르소나 메타와 결과 envelope을 단일 JSON 페이로드로
            출력하는 흐름을 가정한다.
    """

    if not json_mode:
        console.info("dry-run 모드: JSON 저장 없이 콘솔에만 출력합니다")

    async with build_cli_backend(config.llm) as client:
        # 인터뷰 시작 전에 서버 장애가 먼저 표면화되도록 healthcheck를 암묵 호출한다.
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
        # caller가 페르소나 메타와 결과를 묶은 JSON 페이로드를 출력한다.
        return

    # 시스템 프롬프트(record.messages[0])
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
