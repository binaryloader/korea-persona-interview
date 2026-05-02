"""click-based CLI entry point.

Exposes four subcommands (``healthcheck``, ``list-personas``, ``interview``,
``report``) and maps them to exit codes:

- 0: success
- 1: server, input, or config error
- 2: filter matched zero records, or no valid records to summarize
- 3: partial failure (completed ratio below the configured threshold)
- 130: user interrupt (SIGINT)

Each command builds its own asyncio event loop with ``asyncio.run`` so the
process exits cleanly when ``click`` returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click

from src.batch import BatchResultEnvelope, run_batch
from src.cli_views import persona_to_json_dict as _persona_to_json_dict
from src.cli_views import render_persona_table as _render_persona_table
from src.config import AppConfig, load_config
from src.console import MESSAGES, Console, resolve_color as _resolve_color
from src.dry_run import run_dry_run as _run_dry_run
from src.llm_backend import LLMBackend, build_cli_backend
from src.load_personas import load_and_sample, parse_filter
from src.logging_setup import bind_request_id, configure_logging
from src.models import (
    ConfigError,
    DatasetUnavailableError,
    EmptyValidRecordsError,
    FilterMatchedZeroError,
    ServerNotReachableError,
)
from src.report import (
    ReportOptions,
    generate_report,
)


# ---------------------------------------------------------------------------
# 공통 옵션 / 컨텍스트
# ---------------------------------------------------------------------------


def _common_setup(
    *,
    config_path: Optional[Path],
    no_color: bool,
    log_level: Optional[str],
    cli_overrides: Optional[dict] = None,
) -> tuple:
    """모든 서브커맨드에서 호출하는 공통 초기화.

    Args:
        cli_overrides: 호출자가 미리 박아 둔 부분 갱신 dict. ``--model``처럼
            명령별로 다른 일회성 override를 본 함수가 그대로 깊은 병합한다.

    Returns:
        (config, console). config 로드 실패 시 ConfigError를 그대로 raise한다.
    """

    overrides: dict = dict(cli_overrides) if cli_overrides else {}
    if log_level:
        overrides.setdefault("output", {})["log_level"] = log_level
    if no_color:
        overrides.setdefault("output", {})["no_color"] = True

    config = load_config(yaml_path=config_path, cli_overrides=overrides)

    color_enabled = _resolve_color(config.no_color)
    console = Console(color=color_enabled)

    # 로그 파일은 outputs/logs/run_{ts}.jsonl. 콘솔(stderr) + 파일 동시 출력.
    log_dir = config.output_dir / "logs"
    log_path = log_dir / f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"
    configure_logging(level=config.log_level, json_path=log_path)
    bind_request_id(uuid.uuid4().hex)

    logging.getLogger(__name__).debug(
        "CLI 초기화 완료",
        extra={
            "log_level": config.log_level,
            "no_color": config.no_color,
            "log_path": str(log_path),
        },
    )

    return config, console


def _format_partial_ratio(completed: int, requested: int) -> str:
    if requested == 0:
        return "0.0%"
    return f"{completed / requested * 100:.1f}%"


def _print_filter_summary(console: Console, filter_spec: Optional[str]) -> None:
    if filter_spec:
        console.info(f"적용 필터: {filter_spec}")
    else:
        console.info("적용 필터: (없음, 전체에서 샘플링)")


# ---------------------------------------------------------------------------
# --json 모드 출력 헬퍼
# ---------------------------------------------------------------------------


def _emit_json(payload: dict) -> None:
    """``--json`` 모드의 stdout 출력. ``ensure_ascii=False``로 한국어 보존."""

    click.echo(json.dumps(payload, ensure_ascii=False))


def _emit_json_error(code: str, message: str, *, exit_code: int) -> None:
    """``--json`` 모드 에러 응답. stdout JSON + non-zero exit.

    페이로드 형태는 ``{"ok": false, "error": {"code": ..., "message": ...,
    "exit_code": N}}``로 고정한다. ``ok`` 필드는 healthcheck/interview/report 정상
    응답과 같은 위치에 놓여 외부 에이전트가 단일 키로 성공/실패를 분기할 수
    있다. 호출 후 ``sys.exit(exit_code)``는 호출자가 수행한다.
    """

    _emit_json(
        {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "exit_code": int(exit_code),
            },
        }
    )


def _warn_if_output_outside_cwd(
    path: Optional[Path],
    console: Optional["Console"],
    json_mode: bool,
) -> None:
    """``--output``/``--output-dir``가 현재 작업 디렉토리 외부면 경고를 찍는다.

    경로 정규화 결과 cwd 트리 외부에 결과/리포트가 떨어지면 일부 호스트
    환경에서 권한/sandbox 문제가 발생할 수 있다. 경로 자체를 막지는 않고 사람용
    모드에서만 한 번 안내한다(json_mode는 묵음).
    """

    if path is None:
        return
    try:
        resolved = Path(path).expanduser().resolve()
        cwd = Path.cwd().resolve()
        resolved.relative_to(cwd)
    except (ValueError, RuntimeError):
        if console is not None and not json_mode:
            console.warn(
                f"--output 경로가 작업 디렉토리 외부입니다: {resolved}. "
                "권한/sandbox 문제가 발생하면 cwd 안 경로로 변경해 주세요"
            )
    except OSError:
        # resolve() 자체가 실패하는 환경(존재하지 않는 부모 등)은 그대로 둔다.
        pass


def _exit_with_error(
    *,
    json_mode: bool,
    console: Optional["Console"],
    error_code: str,
    message: str,
    exit_code: int,
    hints: Optional[list] = None,
    show_exit_code_line: bool = True,
) -> None:
    """``--json`` 모드와 사람용 모드를 한 번에 분기 처리하는 종료 헬퍼.

    json 모드는 stdout JSON + ``sys.exit(exit_code)``, 사람용 모드는
    ``console.err(message)`` + ``console.hint(hint)``들 + ``종료 코드: N`` 라인을
    출력한다. 모든 호출 지점이 같은 형태를 유지하도록 본 헬퍼 한 곳에서 모은다.
    """

    if json_mode:
        _emit_json_error(error_code, message, exit_code=exit_code)
    elif console is not None:
        console.err(message)
        for hint in hints or []:
            console.hint(hint)
        if show_exit_code_line:
            click.echo(f"종료 코드: {exit_code}")
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# click 명령 정의
# ---------------------------------------------------------------------------


@click.group(
    help=(
        "korea-persona-interview: 한국인 합성 페르소나 인터뷰 CLI.\n"
        "헬스체크 → 페르소나 미리 보기 → 배치 인터뷰 → 리포트 4단계로 진행합니다."
    )
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="config.yaml 경로(기본: 작업 디렉토리의 config.yaml)",
)
@click.option(
    "--no-color",
    is_flag=True,
    default=False,
    help="ANSI 컬러를 끕니다(NO_COLOR 환경변수와 동등).",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default=None,
    help="로그 레벨(기본: config.yaml의 output.log_level).",
)
@click.option(
    "--json",
    "json_mode",
    is_flag=True,
    default=False,
    help=(
        "외부 에이전트(Claude Code, Cursor, Codex 등)와의 비대화형 통합용 JSON 출력 모드. "
        "tqdm 진행률, ANSI 컬러, [OK]/[INFO]/[ERR] 한국어 메시지를 모두 끄고 stdout에 결과 JSON 한 덩어리만 남깁니다. "
        "stderr/jsonl 로그는 그대로 유지됩니다."
    ),
)
@click.pass_context
def cli(
    ctx: click.Context,
    config_path: Optional[Path],
    no_color: bool,
    log_level: Optional[str],
    json_mode: bool,
) -> None:
    """루트 그룹. 공통 옵션을 ctx.obj에 적재한다."""

    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    # --json 모드는 --no-color를 묵시적으로 강제한다.
    ctx.obj["no_color"] = no_color or json_mode
    ctx.obj["log_level"] = log_level
    ctx.obj["json_mode"] = json_mode


# ---------------------------------------------------------------------------
# healthcheck
# ---------------------------------------------------------------------------


@cli.command(help="LLM 서버 응답과 모델 가용성을 확인합니다.")
@click.option(
    "--provider",
    type=click.Choice(["openai", "anthropic"], case_sensitive=False),
    default=None,
    help=(
        "LLM provider(openai|anthropic). 미지정 시 config.yaml의 llm.provider. "
        "로컬 LLM은 provider=openai로 두고 --base-url로 엔드포인트만 바꿉니다."
    ),
)
@click.option(
    "--base-url",
    default=None,
    help=(
        "LLM 서버 base URL. 로컬 LLM(mlx_lm.server, vLLM, llama.cpp 등) 호출 시 "
        "http://localhost:PORT/v1로 지정합니다(기본: config.yaml의 llm.base_url)."
    ),
)
@click.option(
    "--model",
    "model_override",
    default=None,
    help=(
        "이 호출에 한해 사용할 모델 ID(예: gpt-4o, gpt-4o-mini, claude-haiku-4-5). "
        "config.yaml의 llm.model을 일회성으로 덮어쓴다(우선순위: --model > config.yaml > 기본값)."
    ),
)
@click.pass_context
def healthcheck(
    ctx: click.Context,
    provider: Optional[str],
    base_url: Optional[str],
    model_override: Optional[str],
) -> None:
    """LLM 서버 가용성 확인."""

    json_mode: bool = bool(ctx.obj.get("json_mode"))

    cli_overrides: dict = {}
    if provider or base_url or model_override:
        llm_overrides: dict = {}
        if provider:
            llm_overrides["provider"] = provider.lower()
        if base_url:
            llm_overrides["base_url"] = base_url
        if model_override:
            llm_overrides["model"] = model_override
        cli_overrides["llm"] = llm_overrides

    try:
        config, console = _common_setup(
            config_path=ctx.obj["config_path"],
            no_color=ctx.obj["no_color"],
            log_level=ctx.obj["log_level"],
            cli_overrides=cli_overrides or None,
        )
    except ConfigError as exc:
        if json_mode:
            _emit_json_error(
                "config_error",
                MESSAGES["config_error"].format(reason=exc),
                exit_code=1,
            )
        else:
            Console(color=_resolve_color(False)).err(
                MESSAGES["config_error"].format(reason=exc)
            )
        sys.exit(1)

    try:
        models = asyncio.run(_run_healthcheck(config))
    except ServerNotReachableError as exc:
        if json_mode:
            _emit_json_error(
                "server_not_reachable",
                f"{MESSAGES['server_not_reachable'].format(model=config.llm.model)}: {exc}",
                exit_code=1,
            )
        else:
            console.err(MESSAGES["server_not_reachable"].format(model=config.llm.model))
            console.hint(f"Base URL: {config.llm.base_url}")
            console.hint(f"원인: {exc}")
            click.echo("종료 코드: 1")
        sys.exit(1)
    except ConfigError as exc:
        # API 키 누락/무효는 ConfigError 메시지에 키워드가 포함된다.
        # 사용자에게 명확히 분리 안내한다.
        message = str(exc)
        if json_mode:
            code = (
                "api_key_invalid_or_missing"
                if ("API 키" in message or "OPENAI_API_KEY" in message or "ANTHROPIC_API_KEY" in message)
                else "config_error"
            )
            _emit_json_error(code, message, exit_code=1)
        else:
            if "API 키" in message or "OPENAI_API_KEY" in message or "ANTHROPIC_API_KEY" in message:
                console.err(message)
            else:
                console.err(MESSAGES["config_error"].format(reason=exc))
        sys.exit(1)
    except KeyboardInterrupt:
        if json_mode:
            _emit_json_error(
                "user_interrupted", MESSAGES["user_interrupted"], exit_code=130
            )
        else:
            console.warn(MESSAGES["user_interrupted"])
        sys.exit(130)

    if json_mode:
        _emit_json(
            {
                "ok": True,
                "provider": config.llm.provider,
                "base_url": config.llm.base_url,
                "model": config.llm.model,
                "models": list(models),
            }
        )
        sys.exit(0)

    console.ok("LLM 서버 응답 정상")
    console.hint(f"Provider: {config.llm.provider}")
    console.hint(f"Base URL: {config.llm.base_url}")
    if models:
        console.hint(f"사용 가능한 모델 일부: {', '.join(models[:5])}")
    click.echo("종료 코드: 0")
    sys.exit(0)


async def _run_healthcheck(config: AppConfig) -> list:
    async with build_cli_backend(config.llm) as client:
        return await client.healthcheck()


# ---------------------------------------------------------------------------
# list-personas
# ---------------------------------------------------------------------------


@cli.command("list-personas", help="필터 결과를 미리 보여줍니다.")
@click.option(
    "--filter",
    "filter_spec",
    default=None,
    help="필터 DSL(예: age:25-39,region:서울,gender:F).",
)
@click.option(
    "--persona-id",
    "persona_ids",
    multiple=True,
    default=(),
    help=(
        "명시 페르소나 uuid 직접 지정(여러 번 지정 가능). 지정 시 --limit과 "
        "--seed는 무시되며 입력 ID 순서로 출력한다. --filter와 함께 지정하면 "
        "필터 통과 + ID 매칭의 교집합만 채택한다."
    ),
)
@click.option(
    "--limit",
    default=20,
    type=click.IntRange(min=1),
    show_default=True,
    help="출력 행 수.",
)
@click.option(
    "--seed",
    default=42,
    type=int,
    show_default=True,
    help="샘플링 시드.",
)
@click.pass_context
def list_personas(
    ctx: click.Context,
    filter_spec: Optional[str],
    persona_ids: tuple,
    limit: int,
    seed: int,
) -> None:
    """필터 적용 후 페르소나 표를 stdout에 출력한다(PRD §5.9, UI §2.2)."""

    json_mode: bool = bool(ctx.obj.get("json_mode"))

    try:
        config, console = _common_setup(
            config_path=ctx.obj["config_path"],
            no_color=ctx.obj["no_color"],
            log_level=ctx.obj["log_level"],
        )
    except ConfigError as exc:
        if json_mode:
            _emit_json_error(
                "config_error",
                MESSAGES["config_error"].format(reason=exc),
                exit_code=1,
            )
        else:
            Console(color=_resolve_color(False)).err(
                MESSAGES["config_error"].format(reason=exc)
            )
        sys.exit(1)

    # 필터 DSL 사전 검증(파싱 오류는 종료 코드 1).
    try:
        parse_filter(
            filter_spec,
            config.dataset.gender_aliases,
            config.dataset.province_aliases,
        )
    except ConfigError as exc:
        if json_mode:
            _emit_json_error(
                "config_error",
                MESSAGES["config_error"].format(reason=exc),
                exit_code=1,
            )
        else:
            console.err(MESSAGES["config_error"].format(reason=exc))
        sys.exit(1)

    if not json_mode:
        _print_filter_summary(console, filter_spec)

    try:
        personas = load_and_sample(
            filter_str=filter_spec,
            n=len(persona_ids) if persona_ids else limit,
            seed=seed,
            field_map=config.dataset.field_map,
            gender_aliases=config.dataset.gender_aliases,
            province_aliases=config.dataset.province_aliases,
            dataset_name=config.dataset.name,
            split=config.dataset.split,
            persona_ids=tuple(persona_ids) if persona_ids else None,
        )
    except FilterMatchedZeroError as exc:
        if json_mode:
            _emit_json_error("filter_matched_zero", str(exc), exit_code=2)
        else:
            console.warn(MESSAGES["filter_zero"])
            console.hint(f"원인: {exc}")
            click.echo("종료 코드: 2")
        sys.exit(2)
    except DatasetUnavailableError as exc:
        if json_mode:
            _emit_json_error(
                "dataset_unavailable",
                MESSAGES["dataset_unavailable"].format(reason=exc),
                exit_code=1,
            )
        else:
            console.err(MESSAGES["dataset_unavailable"].format(reason=exc))
            click.echo("종료 코드: 1")
        sys.exit(1)
    except ConfigError as exc:
        if json_mode:
            _emit_json_error(
                "config_error",
                MESSAGES["config_error"].format(reason=exc),
                exit_code=1,
            )
        else:
            console.err(MESSAGES["config_error"].format(reason=exc))
        sys.exit(1)
    except KeyboardInterrupt:
        if json_mode:
            _emit_json_error(
                "user_interrupted", MESSAGES["user_interrupted"], exit_code=130
            )
        else:
            console.warn(MESSAGES["user_interrupted"])
        sys.exit(130)

    if not personas:
        if json_mode:
            _emit_json_error(
                "filter_matched_zero", MESSAGES["filter_zero"], exit_code=2
            )
        else:
            console.warn(MESSAGES["filter_zero"])
            click.echo("종료 코드: 2")
        sys.exit(2)

    if json_mode:
        _emit_json(
            {
                "ok": True,
                "personas": [_persona_to_json_dict(p) for p in personas],
                "count": len(personas),
                "filter": filter_spec,
                "seed": seed,
            }
        )
        sys.exit(0)

    console.info(f"표본 출력: {len(personas)}명(seed={seed})")
    _render_persona_table(personas, console)
    click.echo("종료 코드: 0")
    sys.exit(0)


# ---------------------------------------------------------------------------
# interview
# ---------------------------------------------------------------------------


@cli.command(help="배치 인터뷰를 실행하고 결과 JSON을 저장합니다.")
@click.option(
    "--product",
    required=True,
    help="사업 아이템 한 줄 설명(필수).",
)
@click.option(
    "--questions",
    "questions",
    required=True,
    multiple=True,
    help="질문(여러 번 지정 가능, 1개 이상).",
)
@click.option(
    "--filter",
    "filter_spec",
    default=None,
    help="필터 DSL(예: age:25-39,region:서울).",
)
@click.option(
    "--persona-id",
    "persona_ids",
    multiple=True,
    default=(),
    help=(
        "명시 페르소나 uuid 직접 지정(여러 번 지정 가능). 같은 페르소나에 대해 "
        "다른 product/questions로 비교 인터뷰를 돌릴 때 사용한다. 지정 시 --n과 "
        "--seed는 무시되며 입력 ID 개수만큼 인터뷰가 실행된다. --filter와 함께 "
        "지정하면 필터 통과 + ID 매칭의 교집합만 채택한다."
    ),
)
@click.option(
    "--n",
    default=10,
    type=click.IntRange(min=1),
    show_default=True,
    help="인터뷰 인원.",
)
@click.option(
    "--seed",
    default=42,
    type=int,
    show_default=True,
    help="샘플링 시드.",
)
@click.option(
    "--concurrency",
    default=4,
    type=click.IntRange(1, 10),
    show_default=True,
    help="동시성 1-10(기본 4). OpenAI 백엔드 기준 안정 동시성.",
)
@click.option(
    "--persona-fields",
    default="summary",
    help="콤마 구분 토글(예: summary,professional).",
)
@click.option(
    "--follow-up",
    "follow_ups",
    multiple=True,
    help="공통 후속 질문(여러 번 지정 가능).",
)
@click.option(
    "--single-turn",
    is_flag=True,
    default=False,
    help="단일턴 모드(기본 비활성). 활성화 시 모든 질문을 한 chat 호출에 묶는다.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="JSON 저장 없이 콘솔에만 출력합니다(1명).",
)
@click.option(
    "--output",
    "output_dir",
    default="outputs/",
    type=click.Path(file_okay=False, path_type=Path),
    show_default=True,
    help="결과 JSON 저장 디렉토리.",
)
@click.option(
    "--report/--no-report",
    "auto_report",
    default=True,
    show_default=True,
    help=(
        "인터뷰 종료 후 마크다운 리포트를 자동 생성합니다. "
        "기본은 자동 생성이며, --no-report로 끄면 JSON만 저장합니다(외부 도구로 분석할 때). "
        "--dry-run에서는 본 옵션과 무관하게 리포트와 JSON을 모두 만들지 않습니다."
    ),
)
@click.option(
    "--resume",
    "resume_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "부분 실패 결과 JSON 경로. 본 옵션을 지정하면 status=failed record만 "
        "재시도하고 기존 completed/refused/drift record는 그대로 보존한다. "
        "personas는 같은 시드/필터로 재샘플링되며 persona_id 매칭으로 합쳐진다. "
        "결과는 새 timestamp 파일로 저장되고 meta_extra.previous_run_id에 입력 "
        "JSON의 interview_id가 박힌다."
    ),
)
@click.option(
    "--provider",
    type=click.Choice(["openai", "anthropic"], case_sensitive=False),
    default=None,
    help=(
        "LLM provider(openai|anthropic). 미지정 시 config.yaml의 llm.provider. "
        "로컬 LLM은 provider=openai로 두고 --base-url로 엔드포인트만 바꿉니다."
    ),
)
@click.option(
    "--base-url",
    default=None,
    help=(
        "LLM 서버 base URL. 로컬 LLM(mlx_lm.server, vLLM, llama.cpp 등) 호출 시 "
        "http://localhost:PORT/v1로 지정합니다(기본: config.yaml의 llm.base_url)."
    ),
)
@click.option(
    "--model",
    "model_override",
    default=None,
    help=(
        "이 인터뷰 호출에 한해 사용할 모델 ID(예: gpt-4o, gpt-4o-mini, claude-haiku-4-5). "
        "config.yaml의 llm.model을 일회성으로 덮어쓴다(우선순위: --model > config.yaml > 기본값)."
    ),
)
@click.pass_context
def interview(
    ctx: click.Context,
    product: str,
    questions: tuple,
    filter_spec: Optional[str],
    persona_ids: tuple,
    n: int,
    seed: int,
    concurrency: int,
    persona_fields: str,
    follow_ups: tuple,
    single_turn: bool,
    dry_run: bool,
    output_dir: Path,
    auto_report: bool,
    resume_path: Optional[Path],
    provider: Optional[str],
    base_url: Optional[str],
    model_override: Optional[str],
) -> None:
    """배치 인터뷰 진입점(PRD §5.9, UI §2.3)."""

    json_mode: bool = bool(ctx.obj.get("json_mode"))

    # CLI 옵션을 config에 반영. concurrency와 persona_fields는 batch 섹션에 박는다.
    fields_tuple = tuple(
        s.strip() for s in persona_fields.split(",") if s.strip()
    ) or ("summary",)

    overrides: dict = {
        "batch": {
            "concurrency": concurrency,
            "persona_fields": list(fields_tuple),
            "single_turn": bool(single_turn),
        },
        "output": {"output_dir": str(output_dir)},
    }
    if provider or base_url or model_override:
        llm_overrides: dict = {}
        if provider:
            llm_overrides["provider"] = provider.lower()
        if base_url:
            llm_overrides["base_url"] = base_url
        if model_override:
            llm_overrides["model"] = model_override
        overrides["llm"] = llm_overrides

    try:
        config, console = _common_setup(
            config_path=ctx.obj["config_path"],
            no_color=ctx.obj["no_color"],
            log_level=ctx.obj["log_level"],
            cli_overrides=overrides,
        )
    except ConfigError as exc:
        if json_mode:
            _emit_json_error(
                "config_error",
                MESSAGES["config_error"].format(reason=exc),
                exit_code=1,
            )
        else:
            Console(color=_resolve_color(False)).err(
                MESSAGES["config_error"].format(reason=exc)
            )
        sys.exit(1)

    if not questions:
        _exit_with_error(
            json_mode=json_mode,
            console=console,
            error_code="missing_questions",
            message="--questions를 1개 이상 지정해 주세요",
            exit_code=1,
            show_exit_code_line=False,
        )

    if single_turn and not json_mode:
        console.info(
            "--single-turn 모드: 모든 질문을 한 번의 chat 호출에 묶어 처리합니다. "
            "자동 follow-up은 비활성화됩니다."
        )

    questions_list = list(questions)
    follow_ups_list = list(follow_ups)

    _warn_if_output_outside_cwd(output_dir, console, json_mode)

    # 필터 DSL 사전 검증.
    try:
        parse_filter(
            filter_spec,
            config.dataset.gender_aliases,
            config.dataset.province_aliases,
        )
    except ConfigError as exc:
        _exit_with_error(
            json_mode=json_mode,
            console=console,
            error_code="config_error",
            message=MESSAGES["config_error"].format(reason=exc),
            exit_code=1,
            show_exit_code_line=False,
        )

    if not json_mode:
        console.info(f"모델: {config.llm.model}, 동시성: {config.batch.concurrency}")
        _print_filter_summary(console, filter_spec)
        console.info(
            f"질문 수: {len(questions_list)}개, 인원: {1 if dry_run else n}명, 시드: {seed}"
        )

    # dry-run은 1명만 진행하며 JSON 저장하지 않는다(PRD §4.3, UI §2.3.2).
    # ``--persona-id`` 지정 시 ``n``/``seed``를 무시하고 입력 ID 순서대로 사용한다.
    # dry-run + persona-id는 첫 ID만 사용한다(1명 미리 보기 의미를 유지).
    target_persona_ids: tuple = ()
    if persona_ids:
        target_persona_ids = tuple(persona_ids[:1]) if dry_run else tuple(persona_ids)
        target_n = len(target_persona_ids)
    else:
        target_n = 1 if dry_run else n

    try:
        personas = load_and_sample(
            filter_str=filter_spec,
            n=target_n,
            seed=seed,
            field_map=config.dataset.field_map,
            gender_aliases=config.dataset.gender_aliases,
            province_aliases=config.dataset.province_aliases,
            dataset_name=config.dataset.name,
            split=config.dataset.split,
            persona_ids=target_persona_ids or None,
        )
    except FilterMatchedZeroError as exc:
        if json_mode:
            _emit_json_error(
                "filter_matched_zero",
                MESSAGES["filter_too_few"].format(reason=exc),
                exit_code=2,
            )
        else:
            console.warn(MESSAGES["filter_too_few"].format(reason=exc))
            click.echo("종료 코드: 2")
        sys.exit(2)
    except DatasetUnavailableError as exc:
        _exit_with_error(
            json_mode=json_mode,
            console=console,
            error_code="dataset_unavailable",
            message=MESSAGES["dataset_unavailable"].format(reason=exc),
            exit_code=1,
        )
    except ConfigError as exc:
        _exit_with_error(
            json_mode=json_mode,
            console=console,
            error_code="config_error",
            message=MESSAGES["config_error"].format(reason=exc),
            exit_code=1,
            show_exit_code_line=False,
        )
    except KeyboardInterrupt:
        if json_mode:
            _emit_json_error(
                "user_interrupted", MESSAGES["user_interrupted"], exit_code=130
            )
        else:
            console.warn(MESSAGES["user_interrupted"])
        sys.exit(130)

    if dry_run:
        try:
            asyncio.run(
                _run_dry_run(
                    persona=personas[0],
                    product=product,
                    questions=questions_list,
                    follow_ups=follow_ups_list,
                    config=config,
                    console=console,
                    json_mode=json_mode,
                )
            )
            if json_mode:
                _emit_json(
                    {
                        "dry_run": True,
                        "persona": _persona_to_json_dict(personas[0]),
                    }
                )
                sys.exit(0)
            click.echo("종료 코드: 0")
            sys.exit(0)
        except ServerNotReachableError:
            _exit_with_error(
                json_mode=json_mode,
                console=console,
                error_code="server_not_reachable",
                message=MESSAGES["server_not_reachable"].format(model=config.llm.model),
                exit_code=1,
            )
        except KeyboardInterrupt:
            if json_mode:
                _emit_json_error(
                    "user_interrupted",
                    MESSAGES["user_interrupted"],
                    exit_code=130,
                )
            else:
                console.warn(MESSAGES["user_interrupted"])
            sys.exit(130)
        except (ConfigError, DatasetUnavailableError) as exc:
            _exit_with_error(
                json_mode=json_mode,
                console=console,
                error_code="config_error",
                message=str(exc),
                exit_code=1,
                show_exit_code_line=False,
            )

    # ``--resume`` 옵션이 있으면 입력 JSON에서 record 리스트와 interview_id를
    # 추출해 run_batch에 그대로 넘긴다. 본 단계는 LLM 호출 없는 디스크 read만
    # 수반하므로 헬스체크 전에 수행해도 안전하다.
    resume_records: Optional[list] = None
    resume_run_id: Optional[str] = None
    if resume_path is not None:
        try:
            from src.report import _records_from_payload, load_interview_json

            payload = load_interview_json(resume_path)
            resume_records = _records_from_payload(payload)
            meta = payload.get("meta") or {}
            if isinstance(meta, dict):
                resume_run_id = (
                    str(meta.get("interview_id"))
                    if meta.get("interview_id")
                    else None
                )
        except ConfigError as exc:
            _exit_with_error(
                json_mode=json_mode,
                console=console,
                error_code="input_file_schema",
                message=MESSAGES["input_file_schema"],
                exit_code=1,
                hints=[f"원인: {exc}"],
            )
        if not json_mode:
            failed_count = sum(
                1 for r in (resume_records or []) if r.status == "failed"
            )
            console.info(
                f"--resume 모드: 기존 record {len(resume_records or [])}건 중 "
                f"{failed_count}건 재시도"
            )

    # 배치 모드. 헬스체크 → run_batch → 결과 안내.
    try:
        envelope = asyncio.run(
            _run_batch_async(
                personas=personas,
                product=product,
                questions=questions_list,
                follow_ups=follow_ups_list,
                config=config,
                output_dir=output_dir,
                seed=seed,
                resume_records=resume_records,
                resume_run_id=resume_run_id,
            )
        )
    except ServerNotReachableError as exc:
        _exit_with_error(
            json_mode=json_mode,
            console=console,
            error_code="server_not_reachable",
            message=MESSAGES["server_not_reachable"].format(model=config.llm.model),
            exit_code=1,
            hints=[f"원인: {exc}"],
        )
    except DatasetUnavailableError as exc:
        _exit_with_error(
            json_mode=json_mode,
            console=console,
            error_code="dataset_unavailable",
            message=MESSAGES["dataset_unavailable"].format(reason=exc),
            exit_code=1,
        )
    except ConfigError as exc:
        _exit_with_error(
            json_mode=json_mode,
            console=console,
            error_code="config_error",
            message=MESSAGES["config_error"].format(reason=exc),
            exit_code=1,
            show_exit_code_line=False,
        )
    except KeyboardInterrupt:
        if json_mode:
            _emit_json_error(
                "user_interrupted", MESSAGES["user_interrupted"], exit_code=130
            )
        else:
            console.warn(MESSAGES["user_interrupted"])
            click.echo("종료 코드: 130")
        sys.exit(130)

    summary = envelope.summary
    output_path = envelope.output_path
    usage = envelope.usage

    if not json_mode:
        console.info(
            f"완료: {summary.completed}명, 거부: {summary.refused}명, "
            f"실패: {summary.failed}명, 드리프트: {summary.drift}명"
        )
        if usage.total_tokens > 0 or usage.prompt_tokens > 0:
            console.info(
                f"토큰 사용량: prompt {usage.prompt_tokens:,} / "
                f"completion {usage.completion_tokens:,} / "
                f"cached {usage.cached_tokens:,}"
            )
        if output_path:
            console.info(f"결과 저장: {output_path}")

    if envelope.partial_failure:
        ratio = _format_partial_ratio(summary.success_count, summary.requested)
        if json_mode:
            _emit_json(
                {
                    "ok": False,
                    "partial_failure": True,
                    "output_path": str(output_path) if output_path else None,
                    "summary": {
                        "requested": summary.requested,
                        "completed": summary.completed,
                        "refused": summary.refused,
                        "failed": summary.failed,
                        "drift": summary.drift,
                        "cancelled": summary.cancelled,
                        "success_ratio": ratio,
                    },
                    "usage": {
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                        "cached_tokens": usage.cached_tokens,
                    },
                    "model": config.llm.model,
                    "failure_reason_counts": dict(envelope.failure_reason_counts),
                    "report_path": None,
                }
            )
            sys.exit(3)
        console.err(
            MESSAGES["partial_failure"].format(
                x=summary.success_count, n=summary.requested, ratio=ratio
            )
        )
        if envelope.failure_reason_counts:
            console.hint("실패 사유 분포:")
            for reason, count in envelope.failure_reason_counts.items():
                console.hint(f"  - {reason}: {count}건")
        if output_path:
            console.echo(
                f"다음 단계 안내: 부분 결과로도 리포트를 생성할 수 있습니다."
            )
            console.echo(f"  python main.py report {output_path}")
        click.echo("종료 코드: 3")
        sys.exit(3)

    # 인터뷰 정상 종료(부분 실패 아님)는 기본적으로 리포트를 자동 생성한다.
    # ``--no-report``는 외부 분석 파이프라인이 JSON만 받아 처리할 때 사용한다.
    # ``--dry-run``은 위쪽 분기에서 이미 sys.exit(0)으로 빠져나갔으므로 본 분기에
    # 도달하지 않는다.
    report_path: Optional[Path] = None
    if auto_report and output_path is not None:
        if not json_mode:
            console.info(
                "리포트 자동 생성 시작(--no-report로 끌 수 있음, 정성 인사이트 LLM 호출 1회 추가)"
            )
        report_options = ReportOptions(
            top_n=10,
            include_drift=False,
            output_dir=None,
        )
        try:
            report_path = asyncio.run(
                _run_report_async(output_path, report_options, config)
            )
        except (ServerNotReachableError, ConfigError, EmptyValidRecordsError) as exc:
            if not json_mode:
                console.warn(
                    f"리포트 자동 생성 실패: {exc}. JSON은 저장되었으니 "
                    f"`python main.py report {output_path}`로 다시 시도할 수 있습니다"
                )
            report_path = None
        except FileNotFoundError as exc:
            if not json_mode:
                console.warn(
                    f"리포트 자동 생성 실패(입력 파일 누락): {exc}"
                )
            report_path = None
        else:
            if not json_mode:
                console.ok(f"리포트 저장: {report_path}")

    if json_mode:
        _emit_json(
            {
                "ok": True,
                "output_path": str(output_path) if output_path else None,
                "report_path": str(report_path) if report_path else None,
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
            }
        )
        sys.exit(0)

    if output_path:
        console.echo(f"다음 단계: python main.py report {output_path}")
    click.echo("종료 코드: 0")
    sys.exit(0)


async def _run_batch_async(
    personas: list,
    product: str,
    questions: list,
    follow_ups: list,
    config: AppConfig,
    output_dir: Path,
    seed: int,
    resume_records: Optional[list] = None,
    resume_run_id: Optional[str] = None,
) -> BatchResultEnvelope:
    async with build_cli_backend(config.llm) as client:
        return await run_batch(
            personas=personas,
            product=product,
            questions=questions,
            follow_ups=follow_ups,
            llm=client,
            config=config,
            output_dir=output_dir,
            slug="korea-persona-interview",
            seed=seed,
            resume_records=resume_records,
            resume_run_id=resume_run_id,
        )


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@cli.command(help="배치 결과 JSON에서 마크다운 리포트를 생성합니다.")
@click.argument(
    "result_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--top-n",
    default=10,
    type=click.IntRange(min=1),
    show_default=True,
    help="거절 사유 상위 N개.",
)
@click.option(
    "--include-drift",
    is_flag=True,
    default=False,
    help="드리프트 record도 정량 집계에 포함합니다.",
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="리포트 저장 디렉토리(기본: 입력 JSON과 같은 디렉토리).",
)
@click.option(
    "--provider",
    type=click.Choice(["openai", "anthropic"], case_sensitive=False),
    default=None,
    help=(
        "LLM provider(openai|anthropic). 미지정 시 config.yaml의 llm.provider."
    ),
)
@click.option(
    "--base-url",
    default=None,
    help=(
        "LLM 서버 base URL. 로컬 LLM 사용 시 http://localhost:PORT/v1로 지정합니다"
        "(기본: config.yaml의 llm.base_url)."
    ),
)
@click.option(
    "--model",
    "model_override",
    default=None,
    help=(
        "이 리포트 정성 인사이트 호출에 한해 사용할 모델 ID. "
        "config.yaml의 llm.model을 일회성으로 덮어쓴다(우선순위: --model > config.yaml > 기본값)."
    ),
)
@click.option(
    "--insight-model",
    "insight_model_override",
    default=None,
    help=(
        "정성 인사이트 호출에만 사용할 별도 모델 ID. 미지정 시 ``--model`` 또는 "
        "``config.yaml``의 ``report.insight_model`` 또는 ``llm.model``을 본다. "
        "예: 인터뷰는 mini로 돌리고 인사이트는 ``gpt-4o``/``claude-sonnet-4-5``로."
    ),
)
@click.pass_context
def report(
    ctx: click.Context,
    result_path: Path,
    top_n: int,
    include_drift: bool,
    output_dir: Optional[Path],
    provider: Optional[str],
    base_url: Optional[str],
    model_override: Optional[str],
    insight_model_override: Optional[str],
) -> None:
    """report 진입점(PRD §5.9, UI §2.4)."""

    json_mode: bool = bool(ctx.obj.get("json_mode"))

    cli_overrides: dict = {}
    if provider or base_url or model_override:
        llm_overrides: dict = {}
        if provider:
            llm_overrides["provider"] = provider.lower()
        if base_url:
            llm_overrides["base_url"] = base_url
        if model_override:
            llm_overrides["model"] = model_override
        cli_overrides["llm"] = llm_overrides
    if insight_model_override:
        cli_overrides.setdefault("report", {})["insight_model"] = insight_model_override

    try:
        config, console = _common_setup(
            config_path=ctx.obj["config_path"],
            no_color=ctx.obj["no_color"],
            log_level=ctx.obj["log_level"],
            cli_overrides=cli_overrides or None,
        )
    except ConfigError as exc:
        if json_mode:
            _emit_json_error(
                "config_error",
                MESSAGES["config_error"].format(reason=exc),
                exit_code=1,
            )
        else:
            Console(color=_resolve_color(False)).err(
                MESSAGES["config_error"].format(reason=exc)
            )
        sys.exit(1)

    options = ReportOptions(
        top_n=top_n,
        include_drift=include_drift,
        output_dir=output_dir,
    )
    _warn_if_output_outside_cwd(output_dir, console, json_mode)

    if not json_mode:
        console.info(f"입력 JSON: {result_path}")
        console.info(f"옵션: top_n={top_n}, include_drift={include_drift}")
        console.info("정성 인사이트 생성 중(모델 호출 1회)...")

    try:
        report_path = asyncio.run(_run_report_async(result_path, options, config))
    except FileNotFoundError:
        _exit_with_error(
            json_mode=json_mode,
            console=console,
            error_code="input_file_not_found",
            message=MESSAGES["input_file_not_found"],
            exit_code=1,
            hints=[f"경로: {result_path}"],
        )
    except EmptyValidRecordsError as exc:
        _exit_with_error(
            json_mode=json_mode,
            console=console,
            error_code="empty_valid_records",
            message=MESSAGES["empty_valid_records"],
            exit_code=2,
            hints=[f"원인: {exc}"],
        )
    except ConfigError as exc:
        # load_interview_json의 스키마/파싱 오류는 ConfigError로 변환되어 온다.
        _exit_with_error(
            json_mode=json_mode,
            console=console,
            error_code="input_file_schema",
            message=MESSAGES["input_file_schema"],
            exit_code=1,
            hints=[f"원인: {exc}"],
        )
    except ServerNotReachableError as exc:
        _exit_with_error(
            json_mode=json_mode,
            console=console,
            error_code="server_not_reachable",
            message=MESSAGES["server_not_reachable"].format(model=config.llm.model),
            exit_code=1,
            hints=[f"원인: {exc}"],
        )
    except KeyboardInterrupt:
        if json_mode:
            _emit_json_error(
                "user_interrupted", MESSAGES["user_interrupted"], exit_code=130
            )
        else:
            console.warn(MESSAGES["user_interrupted"])
            click.echo("종료 코드: 130")
        sys.exit(130)

    if json_mode:
        _emit_json(
            {
                "ok": True,
                "output_path": str(report_path),
                "input_path": str(result_path),
                "top_n": top_n,
                "include_drift": include_drift,
            }
        )
        sys.exit(0)

    console.ok(f"리포트 저장: {report_path}")
    click.echo("종료 코드: 0")
    sys.exit(0)


async def _run_report_async(
    json_path: Path,
    options: ReportOptions,
    config: AppConfig,
) -> Path:
    """리포트는 LLM 호출 1회를 포함한다. 호출 실패는 ``generate_report``에서 흡수.

    ``config.report.insight_model``이 지정되면 정성 인사이트 호출 한정으로
    ``LlmConfig.model``만 갈아끼운 별도 backend를 만든다. 인터뷰 단계는 mini,
    인사이트는 4o/sonnet 류 더 깊은 모델로 분리하는 흐름을 yaml/CLI에서 단일
    옵션으로 지원한다.
    """

    insight_model = (
        config.report.insight_model.strip()
        if config.report.insight_model
        else None
    )
    if insight_model and insight_model != config.llm.model:
        import dataclasses as _dc

        insight_llm = _dc.replace(config.llm, model=insight_model)
        async with build_cli_backend(insight_llm) as client:
            return await generate_report(
                json_path=json_path,
                options=options,
                llm=client,
                config=config,
            )

    async with build_cli_backend(config.llm) as client:
        return await generate_report(
            json_path=json_path,
            options=options,
            llm=client,
            config=config,
        )


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def main() -> None:
    """script 진입점. click.group이 sys.exit을 호출하므로 wrapper는 단순하다."""

    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
