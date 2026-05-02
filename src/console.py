"""사용자 대상 콘솔 출력 헬퍼와 한국어 메시지 사전.

``main.py``에서 분리한 CLI presentation 계층이다. ``MESSAGES`` dict가 한국어
문구의 단일 정본이라, 한 곳에서 문장을 갱신하면 본 도구의 모든 명령이 같은
문구를 노출한다.
"""

from __future__ import annotations

import os
import sys

import click


MESSAGES: dict = {
    "server_not_reachable": (
        "LLM 서버에 연결할 수 없습니다. 인터넷 연결과 base_url, API 키 환경변수를 "
        "확인해 주세요(현재 모델: {model})"
    ),
    "api_key_missing": (
        "API 키가 설정되지 않았습니다. provider=openai이면 OPENAI_API_KEY를, "
        "provider=anthropic이면 ANTHROPIC_API_KEY를 셸 환경변수 또는 프로젝트 "
        "루트의 .env 파일에 설정해 주세요"
    ),
    "api_key_invalid": (
        "API 키가 유효하지 않거나 권한이 없습니다. 환경변수(OPENAI_API_KEY 또는 "
        "ANTHROPIC_API_KEY)를 다시 확인해 주세요"
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
    """ANSI 컬러 escape 시퀀스를 출력할지 결정한다.

    ``no_color_flag``가 set되었거나, 환경변수 ``NO_COLOR``가 있거나
    (no-color.org 표준), stdout이 TTY가 아니면 컬러를 끈다.
    """

    if no_color_flag:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    return True


class Console:
    """ANSI 컬러를 옵션으로 가지는 prefix 부착 stdout/stderr 프린터.

    각 메서드는 ``[OK]``/``[INFO]``/``[WARN]``/``[ERR]`` 라벨을 붙여 한 줄을
    출력하므로, 컬러를 꺼도 의미가 그대로 전달된다.
    """

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
