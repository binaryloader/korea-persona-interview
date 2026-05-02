"""구조화 로깅(JSON Lines) 설정.

stdlib ``logging`` 위에 ``JsonLineFormatter``를 얹는다. structlog/loguru 의존을 회피한다(dependency.md §1). request_id는 ``contextvars.ContextVar``로 관리하여 비동기 task 간 안전하게 격리된다.

마스킹 규칙은 logging.md §2와 PRD §6.6을 따른다. 사업 아이템 본문(``product``)과 페르소나 이름(``persona.name``)은 본 모듈의 헬퍼로 일원화 마스킹한다.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# request_id 컨텍스트. CLI 진입 시 1회 설정하면 자식 task에 자동 전파된다.
_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "kpi_request_id", default="-"
)

# JsonLineFormatter가 기본 record dict에서 빼낼 표준 필드. 그 외는 extra로 간주하여 JSON에 그대로 합친다.
_RESERVED_LOG_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
    }
)


# ---------------------------------------------------------------------------
# 컨텍스트 헬퍼
# ---------------------------------------------------------------------------


def bind_request_id(request_id: Optional[str] = None) -> str:
    """요청 단위 request_id를 컨텍스트에 설정한다.

    Args:
        request_id: 기존 ID를 이어 받을 때 명시. ``None``이면 새 uuid4를 만든다.

    Returns:
        설정된 request_id 값.
    """

    rid = request_id or uuid.uuid4().hex
    _REQUEST_ID.set(rid)
    return rid


def get_request_id() -> str:
    """현재 컨텍스트의 request_id를 반환한다. 미설정 시 ``"-"``."""

    return _REQUEST_ID.get()


# ---------------------------------------------------------------------------
# 마스킹 헬퍼(security.md §1, logging.md §2)
# ---------------------------------------------------------------------------


def mask_name(name: Optional[str]) -> str:
    """페르소나 이름 마스킹.

    - 1글자: 그대로 반환(가릴 게 없다)
    - 2글자: 첫 글자 + ``O``(예: ``김O``)
    - 3글자: 첫 글자 + ``O`` + 마지막 글자(예: ``김O수``)
    - 4글자 이상: 첫 글자 + ``O`` 반복 + 마지막 글자

    None 또는 빈 문자열은 그대로 반환한다.
    """

    if not name:
        return name or ""
    n = len(name)
    if n == 1:
        return name
    if n == 2:
        return name[0] + "O"
    if n == 3:
        return name[0] + "O" + name[-1]
    return name[0] + ("O" * (n - 2)) + name[-1]


def mask_product(product: Optional[str], head: int = 30) -> str:
    """사업 아이템 본문 마스킹. 첫 head 글자 + ``(N자)`` 꼬리.

    PRD §6.6에 따라 로그 본문에 그대로 기록하지 않는다. 결과 JSON에는 원문이 저장되지만 그것은 로컬 파일 한정이다.
    """

    if product is None:
        return ""
    n = len(product)
    if n <= head:
        return f"{product}({n}자)"
    return f"{product[:head]}({n}자)"


def mask_persona_id(persona_id: Optional[str], prefix_len: int = 12) -> str:
    """persona_id를 sha256 prefix로 마스킹한다(라운드 G16).

    데이터셋의 uuid가 그대로 로그에 남으면 동일 페르소나의 다른 실행 결과를 cross-link할 수 있다.
    sha256 hex prefix 12자만 노출해 동일 ID라는 사실은 유지하되 원본 uuid를 추적할 수 없게 한다(security.md §1, logging.md §2).
    """

    if not persona_id:
        return ""
    import hashlib as _hashlib

    digest = _hashlib.sha256(str(persona_id).encode("utf-8")).hexdigest()
    return digest[:prefix_len]


# ---------------------------------------------------------------------------
# JSON Lines 포맷터
# ---------------------------------------------------------------------------


class JsonLineFormatter(logging.Formatter):
    """LogRecord를 JSON Lines로 직렬화한다.

    필드는 ``timestamp``(ISO 8601 UTC), ``level``, ``logger``, ``message``, ``request_id``, ``module``이고 LogRecord의 extra 키는 그대로 합친다.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        # base 메시지는 logging이 args interpolation을 적용한 결과를 사용한다.
        message = record.getMessage()

        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "message": message,
            "request_id": get_request_id(),
        }

        # 호출자가 logger.info("...", extra={"key": "value"}) 식으로 넣은 키를 합친다. 표준 LogRecord 속성과 충돌하는 키는 건너뛴다.
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_KEYS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 설정 진입점
# ---------------------------------------------------------------------------


def configure_logging(
    level: str = "INFO",
    json_path: Optional[Path] = None,
) -> None:
    """루트 로거에 콘솔(stderr) + 파일(JSON Lines) 핸들러를 부착한다.

    동일 루트에 이미 핸들러가 있으면 중복 부착을 막기 위해 비운 뒤 재설정한다. 호출자(main.py)는 CLI 진입점에서 1회만 호출하면 된다.

    Args:
        level: ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR`` 중 하나(대소문자 무관).
        json_path: 파일 핸들러 경로. ``None``이면 콘솔만 사용한다.
    """

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = JsonLineFormatter()

    console_handler = logging.StreamHandler(stream=sys.stderr)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(json_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
