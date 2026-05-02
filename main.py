"""click 기반 CLI 진입점.

PRD §5.9, TDD §15, UI §2를 따른다. 4개 서브커맨드(``healthcheck``,
``list-personas``, ``interview``, ``report``)를 노출하고 매크로 명령은 두지
않는다(PRD §5.9). 사용자 안내 문구는 한국어이며 종료 코드 매핑은 아래와 같다.

- 0: 정상
- 1: 서버/입력/설정 오류
- 2: 표본/필터 결과 0건 또는 정상 record 0건
- 3: 부분 실패(완료 record 50% 미만)
- 130: 사용자 중단(SIGINT)

비동기 진입은 ``asyncio.run(main_async())`` 패턴으로 click 명령 함수 안에서만
이벤트 루프를 만든다(TDD §9). [OK]/[WARN]/[ERR] 라벨은 텍스트로 병기해 컬러
비활성화 시에도 의미가 전달되게 한다(UI §5.1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click

from src.batch import BatchResultEnvelope, run_batch
from src.config import AppConfig, load_config
from src.interview import InterviewSession
from src.llm_client import MlxLLMClient
from src.load_personas import load_and_sample, parse_filter
from src.logging_setup import bind_request_id, configure_logging
from src.models import (
    ConfigError,
    DatasetUnavailableError,
    EmptyValidRecordsError,
    FilterMatchedZeroError,
    PersonaMeta,
    ServerNotReachableError,
)
from src.report import (
    ReportOptions,
    generate_report,
)


# 한국어 단일 출처 메시지 사전(UI §3.1). 동일 예외가 명령마다 다른 문구로
# 출력되지 않도록 본 사전만 사용한다.
#
# v1.x부터 백엔드는 OpenAI Chat Completions API다. ``server_not_reachable``과
# ``api_key_missing``은 분리해 사용자가 어떤 문제인지 즉시 식별할 수 있게 한다.
MESSAGES = {
    "server_not_reachable": (
        "OpenAI 서버에 연결할 수 없습니다. 인터넷 연결과 base_url, "
        "OPENAI_API_KEY 환경변수를 확인해 주세요(현재 모델: {model})"
    ),
    "api_key_missing": (
        "OpenAI API 키가 설정되지 않았습니다. "
        "https://platform.openai.com/api-keys 에서 발급 후 환경변수 "
        "OPENAI_API_KEY로 셸에 적용하거나(`export OPENAI_API_KEY=sk-...`) "
        "프로젝트 루트의 .env 파일에 `OPENAI_API_KEY=...` 형식으로 저장해 주세요"
    ),
    "api_key_invalid": (
        "OpenAI API 키가 유효하지 않거나 권한이 없습니다. "
        "환경변수 OPENAI_API_KEY를 다시 확인해 주세요"
    ),
    "config_error": "설정 파일을 읽을 수 없습니다: {reason}",
    "dataset_unavailable": (
        "데이터셋을 로드하지 못했습니다. 인터넷 연결과 ~/.cache/huggingface "
        "권한을 확인해 주세요. 원인: {reason}"
    ),
    "filter_zero": (
        "필터 조건에 맞는 페르소나가 없습니다. 필터를 완화해 주세요"
    ),
    "filter_too_few": (
        "필터 결과가 요청 수보다 적습니다. --n을 줄이거나 필터를 완화해 주세요. {reason}"
    ),
    "input_file_not_found": (
        "입력 파일을 읽지 못했습니다. 경로를 확인해 주세요. ls outputs/로 결과 JSON을 확인할 수 있습니다"
    ),
    "input_file_schema": (
        "입력 파일이 올바른 인터뷰 JSON 형식이 아닙니다. 본 도구의 interview 명령으로 생성된 JSON인지 확인해 주세요"
    ),
    "empty_valid_records": (
        "리포트를 생성할 수 있는 정상 record가 없습니다. 모델 동작과 필터를 점검한 뒤 다시 실행해 주세요"
    ),
    "user_interrupted": (
        "사용자 중단 신호를 받았습니다. 부분 결과를 outputs/에 저장합니다"
    ),
    "partial_failure": (
        "부분 실패로 종료합니다(완료 {x}명 / 요청 {n}명, {ratio}). 부분 결과는 저장되었습니다"
    ),
}


# ---------------------------------------------------------------------------
# 컬러/라벨 헬퍼
# ---------------------------------------------------------------------------


def _resolve_color(no_color_flag: bool) -> bool:
    """ANSI 컬러 활성화 여부.

    - ``--no-color``가 True면 끈다
    - 환경변수 ``NO_COLOR``가 set되어 있으면 끈다(no-color.org 표준)
    - stdout이 TTY가 아니면 끈다
    """

    if no_color_flag:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    return True


class Console:
    """[OK]/[WARN]/[ERR] 라벨과 ANSI 컬러를 일원화한 stdout/stderr 출력기."""

    def __init__(self, *, color: bool) -> None:
        self._color = color

    def _wrap(self, text: str, code: str) -> str:
        if not self._color:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    def ok(self, message: str) -> None:
        label = self._wrap("[OK]", "32")
        click.echo(f"{label} {message}")

    def info(self, message: str) -> None:
        label = self._wrap("[INFO]", "36")
        click.echo(f"{label} {message}")

    def warn(self, message: str) -> None:
        label = self._wrap("[WARN]", "33")
        click.echo(f"{label} {message}", err=True)

    def err(self, message: str) -> None:
        label = self._wrap("[ERR]", "31")
        click.echo(f"{label} {message}", err=True)

    def hint(self, message: str) -> None:
        click.echo(f"  {message}")

    def echo(self, message: str = "") -> None:
        click.echo(message)


# ---------------------------------------------------------------------------
# 공통 옵션 / 컨텍스트
# ---------------------------------------------------------------------------


def _common_setup(
    *,
    config_path: Optional[Path],
    no_color: bool,
    log_level: Optional[str],
) -> tuple:
    """모든 서브커맨드에서 호출하는 공통 초기화.

    Returns:
        (config, console). config 로드 실패 시 ConfigError를 그대로 raise한다.
    """

    cli_overrides: dict = {}
    if log_level:
        cli_overrides.setdefault("output", {})["log_level"] = log_level
    if no_color:
        cli_overrides.setdefault("output", {})["no_color"] = True

    config = load_config(yaml_path=config_path, cli_overrides=cli_overrides)

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

    페이로드 형태는 ``{"error": {"code": ..., "message": ..., "exit_code": N}}``로
    고정한다. 호출 후 ``sys.exit(exit_code)``는 호출자가 수행한다.
    """

    _emit_json(
        {
            "error": {
                "code": code,
                "message": message,
                "exit_code": int(exit_code),
            }
        }
    )


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


@cli.command(help="OpenAI 서버 응답과 모델 가용성을 확인합니다.")
@click.option(
    "--base-url",
    default=None,
    help="OpenAI 호환 서버 base URL(기본: config.yaml의 llm.base_url).",
)
@click.pass_context
def healthcheck(ctx: click.Context, base_url: Optional[str]) -> None:
    """``GET /v1/models`` 200 응답과 모델 ID를 출력한다(PRD §5.9, UI §2.1)."""

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

    cli_overrides: dict = {}
    if base_url:
        cli_overrides["llm"] = {"base_url": base_url}
        try:
            config = load_config(
                yaml_path=ctx.obj["config_path"],
                cli_overrides=cli_overrides,
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
                if ("API 키" in message or "OPENAI_API_KEY" in message)
                else "config_error"
            )
            _emit_json_error(code, message, exit_code=1)
        else:
            if "API 키" in message or "OPENAI_API_KEY" in message:
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
                "base_url": config.llm.base_url,
                "model": config.llm.model,
                "models": list(models),
            }
        )
        sys.exit(0)

    console.ok("OpenAI 서버 응답 정상")
    console.hint(f"Base URL: {config.llm.base_url}")
    if models:
        console.hint(f"사용 가능한 모델 일부: {', '.join(models[:5])}")
    click.echo("종료 코드: 0")
    sys.exit(0)


async def _run_healthcheck(config: AppConfig) -> list:
    async with MlxLLMClient(config.llm) as client:
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
            n=limit,
            seed=seed,
            field_map=config.dataset.field_map,
            gender_aliases=config.dataset.gender_aliases,
            province_aliases=config.dataset.province_aliases,
            dataset_name=config.dataset.name,
            split=config.dataset.split,
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


def _persona_to_json_dict(persona: PersonaMeta) -> dict:
    """``PersonaMeta``를 ``--json`` 모드에서 stdout에 실어 보낼 dict로 변환한다.

    ``raw`` dict는 데이터셋 원본 컬럼 전체이므로 stdout 페이로드 크기가 커진다.
    외부 통합에서 stream 파싱 부담을 줄이기 위해 본 함수에서는 ``raw``를 빼고
    분석에 충분한 인구 통계 핵심 키만 노출한다(파일에 저장되는 JSON에는 raw가
    그대로 보존된다).
    """

    return {
        "persona_id": persona.persona_id,
        "name": persona.name,
        "gender": persona.gender,
        "age": persona.age,
        "region": persona.region,
        "subregion": persona.subregion,
        "occupation": persona.occupation,
        "marital": persona.marital,
        "education": persona.education,
        "family_type": persona.family_type,
        "housing_type": persona.housing_type,
    }


def _render_persona_table(personas: list, console: Console) -> None:
    """간단 표 출력. 한글 폭 2 가정으로 단순 정렬한다(UI §5.4의 v1 방침)."""

    headers = ("persona_id", "이름", "성별", "연령", "지역", "직업")
    rows = []
    for p in personas:
        rows.append(
            (
                p.persona_id[:16],
                (p.name or "-")[:10],
                p.gender,
                str(p.age),
                f"{p.region} {p.subregion}".strip(),
                (p.occupation or "-")[:24],
            )
        )

    # 컬럼 폭은 헤더 + 본문의 max로. 한글은 2 셀 폭 가정.
    def _width(s: str) -> int:
        w = 0
        for ch in s:
            cp = ord(ch)
            if cp >= 0x1100:
                w += 2
            else:
                w += 1
        return w

    widths = [_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(cell))

    def _pad(cell: str, width: int) -> str:
        return cell + " " * max(0, width - _width(cell))

    header_line = "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers))
    sep_line = "  ".join("-" * widths[i] for i in range(len(headers)))
    console.echo("")
    console.echo("  " + header_line)
    console.echo("  " + sep_line)
    for row in rows:
        line = "  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row))
        console.echo("  " + line)
    console.echo("")


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
    help="단일턴 모드(v1은 기본 비활성).",
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
@click.pass_context
def interview(
    ctx: click.Context,
    product: str,
    questions: tuple,
    filter_spec: Optional[str],
    n: int,
    seed: int,
    concurrency: int,
    persona_fields: str,
    follow_ups: tuple,
    single_turn: bool,
    dry_run: bool,
    output_dir: Path,
    auto_report: bool,
) -> None:
    """배치 인터뷰 진입점(PRD §5.9, UI §2.3)."""

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

    # CLI 옵션을 config에 반영. concurrency와 persona_fields는 batch 섹션에 박는다.
    fields_tuple = tuple(
        s.strip() for s in persona_fields.split(",") if s.strip()
    ) or ("summary",)

    overrides = {
        "batch": {
            "concurrency": concurrency,
            "persona_fields": list(fields_tuple),
            "single_turn": bool(single_turn),
        },
        "output": {"output_dir": str(output_dir)},
    }
    try:
        config = load_config(
            yaml_path=ctx.obj["config_path"],
            cli_overrides=overrides,
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
                f"cached {usage.cached_tokens:,} / "
                f"비용 추정: ${envelope.estimated_cost_usd:.4f}"
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
                    "estimated_cost_usd": envelope.estimated_cost_usd,
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
                "estimated_cost_usd": envelope.estimated_cost_usd,
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
) -> BatchResultEnvelope:
    async with MlxLLMClient(config.llm) as client:
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
        )


async def _run_dry_run(
    persona: PersonaMeta,
    product: str,
    questions: list,
    follow_ups: list,
    config: AppConfig,
    console: Console,
    json_mode: bool = False,
) -> None:
    """dry-run: 단일 페르소나 인터뷰를 콘솔에만 출력한다(UI §2.3.2).

    ``json_mode``가 True면 사람용 메시지/시스템 프롬프트 덤프를 출력하지 않는다.
    호출자가 헬스체크와 인터뷰만 수행한 뒤 페르소나 메타를 JSON으로 출력한다.
    """

    if not json_mode:
        console.info("dry-run 모드: JSON 저장 없이 콘솔에만 출력합니다")

    async with MlxLLMClient(config.llm) as client:
        # 헬스체크 자동 수행.
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
        # json 모드는 호출자에서 페르소나 메타와 결과 본체를 별도 페이로드로 출력한다.
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
        console.echo(
            json.dumps(
                {
                    "intent": s.intent,
                    "willingness_to_pay": s.willingness_to_pay,
                    "willingness_to_pay_currency": s.willingness_to_pay_currency,
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
@click.pass_context
def report(
    ctx: click.Context,
    result_path: Path,
    top_n: int,
    include_drift: bool,
    output_dir: Optional[Path],
) -> None:
    """report 진입점(PRD §5.9, UI §2.4)."""

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

    options = ReportOptions(
        top_n=top_n,
        include_drift=include_drift,
        output_dir=output_dir,
    )

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
    """리포트는 LLM 호출 1회를 포함한다. 호출 실패는 ``generate_report``에서 흡수."""

    async with MlxLLMClient(config.llm) as client:
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
