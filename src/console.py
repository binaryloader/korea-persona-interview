"""콘솔 출력 헬퍼와 한국어 메시지 사전.

CLI presentation 계층 코드를 ``main.py``에서 분리한다(architecture.md §1, §5.1).
``main.py``는 click 명령 라우팅에 집중하고, 본 모듈은 ``[OK]``/``[WARN]``/``[ERR]``
라벨, ANSI 컬러 적용, 한국어 안내 문구 단일 사전을 담당한다.

본 모듈은 외부 의존이 click 하나뿐이며 도메인 모델에 의존하지 않는다(presentation
계층). 한국어 메시지 변경은 ``MESSAGES`` 사전 수정만으로 끝난다(UI §3.1
한국어 단일 출처 원칙).
"""

from __future__ import annotations

import os
import sys

import click


# 한국어 단일 출처 메시지 사전(UI §3.1). 동일 예외가 명령마다 다른 문구로
# 출력되지 않도록 본 사전만 사용한다.
#
# v1.x부터 백엔드는 OpenAI Chat Completions API다. ``server_not_reachable``과
# ``api_key_missing``은 분리해 사용자가 어떤 문제인지 즉시 식별할 수 있게 한다.
MESSAGES: dict = {
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


def resolve_color(no_color_flag: bool) -> bool:
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
