"""``src.logging_setup`` 단위 테스트.

- ``mask_name``: 1/2/3/4글자 분기와 None/빈 문자열
- ``mask_product``: 30자 미만/이상 분기, None
- ``bind_request_id``/``get_request_id``: contextvars 왕복
- ``JsonLineFormatter``: JSON Lines 출력 형식, request_id 포함, ISO 타임스탬프
- ``configure_logging``: 콘솔(stderr) + 파일 핸들러 부착
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from src.logging_setup import (
    JsonLineFormatter,
    bind_request_id,
    configure_logging,
    get_request_id,
    mask_name,
    mask_persona_id,
    mask_product,
)


# ---------------------------------------------------------------------------
# mask_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, ""),
        ("", ""),
        ("김", "김"),  # 1글자: 그대로
        ("김민", "김O"),  # 2글자: 첫 + O
        ("김민수", "김O수"),  # 3글자: 첫 + O + 마지막
        ("홍길동전", "홍OO전"),  # 4글자: 첫 + OO + 마지막
        ("이몽룡선생", "이OOO생"),  # 5글자
    ],
)
def test_mask_name_분기(raw, expected) -> None:
    assert mask_name(raw) == expected


# ---------------------------------------------------------------------------
# mask_persona_id(라운드 G16)
# ---------------------------------------------------------------------------


def test_mask_persona_id_None_빈문자열() -> None:
    """None과 빈 문자열은 빈 문자열로 반환한다."""

    assert mask_persona_id(None) == ""
    assert mask_persona_id("") == ""


def test_mask_persona_id_sha256_prefix_12자() -> None:
    """동일 입력은 동일 출력(deterministic), 출력은 hex 12자."""

    out1 = mask_persona_id("test-uuid-0001")
    out2 = mask_persona_id("test-uuid-0001")
    assert out1 == out2
    assert len(out1) == 12
    # hex만 들어 있어야 한다.
    assert all(c in "0123456789abcdef" for c in out1)


def test_mask_persona_id_원본_uuid_노출_방지() -> None:
    """원본 uuid 본문은 마스킹 결과에 포함되지 않는다."""

    pid = "p-0001-real"
    out = mask_persona_id(pid)
    assert "p-0001" not in out
    assert pid not in out


# ---------------------------------------------------------------------------
# mask_product
# ---------------------------------------------------------------------------


def test_mask_product_None_빈문자열_반환() -> None:
    assert mask_product(None) == ""


def test_mask_product_30자_이하_본문_보존() -> None:
    body = "1인 가구용 반찬 정기배송"
    masked = mask_product(body)
    assert masked == f"{body}({len(body)}자)"
    assert body in masked


def test_mask_product_31자_이상_첫30자_보존() -> None:
    body = "1인 가구용 반찬 정기배송, 월 39,900원, 주 2회 배송, 알레르기 옵션 제공"
    assert len(body) > 30
    masked = mask_product(body)
    assert masked.startswith(body[:30])
    assert f"({len(body)}자)" in masked
    # 본문 후반부는 마스킹되어 노출되지 않는다
    assert body[31:] not in masked


def test_mask_product_정확히_30자_경계() -> None:
    body = "가" * 30
    masked = mask_product(body)
    assert masked == f"{body}(30자)"


# ---------------------------------------------------------------------------
# request_id contextvars
# ---------------------------------------------------------------------------


def test_bind_request_id_명시_값_왕복() -> None:
    rid = bind_request_id("abc-123")
    assert rid == "abc-123"
    assert get_request_id() == "abc-123"


def test_bind_request_id_None_uuid4_생성() -> None:
    rid1 = bind_request_id(None)
    rid2 = bind_request_id(None)
    assert rid1 != rid2
    assert get_request_id() == rid2


def test_get_request_id_기본값_dash() -> None:
    """새로 생성된 컨텍스트는 default ``"-"``을 가진다.

    pytest 메인 스레드에 이전 테스트에서 set된 값이 남아있을 수 있어, 본 테스트는
    contextvars의 default 값 자체를 확인한다.
    """

    import contextvars

    from src.logging_setup import _REQUEST_ID  # type: ignore[attr-defined]

    ctx = contextvars.copy_context()
    # 빈 컨텍스트에서 default 확인
    fresh = contextvars.Context()
    value = fresh.run(_REQUEST_ID.get)
    assert value == "-"


# ---------------------------------------------------------------------------
# JsonLineFormatter
# ---------------------------------------------------------------------------


def _format_record(formatter: JsonLineFormatter, record: logging.LogRecord) -> dict:
    line = formatter.format(record)
    return json.loads(line)


def test_json_formatter_표준_필드_출력() -> None:
    bind_request_id("rid-1")
    formatter = JsonLineFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="x.py",
        lineno=1,
        msg="hello",
        args=None,
        exc_info=None,
    )
    payload = _format_record(formatter, record)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "hello"
    assert payload["request_id"] == "rid-1"
    assert "timestamp" in payload
    # ISO 8601 형식인지 sanity 검증(YYYY-...)
    assert payload["timestamp"][:4].isdigit()


def test_json_formatter_extra_dict_병합() -> None:
    bind_request_id("rid-2")
    formatter = JsonLineFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="x.py",
        lineno=1,
        msg="event",
        args=None,
        exc_info=None,
    )
    record.persona_id = "p-0001"  # type: ignore[attr-defined]
    record.latency_ms = 120  # type: ignore[attr-defined]
    payload = _format_record(formatter, record)
    assert payload["persona_id"] == "p-0001"
    assert payload["latency_ms"] == 120


def test_json_formatter_한국어_본문_ensure_ascii_false() -> None:
    bind_request_id("rid-3")
    formatter = JsonLineFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="x.py",
        lineno=1,
        msg="한국어 메시지",
        args=None,
        exc_info=None,
    )
    line = formatter.format(record)
    # JSON 라인은 한국어 본문을 그대로 포함한다(`\uXXXX` 이스케이프 X)
    assert "한국어 메시지" in line


def test_json_formatter_exc_info_포함() -> None:
    bind_request_id("rid-4")
    formatter = JsonLineFormatter()
    try:
        raise ValueError("부우엥")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname="x.py",
            lineno=1,
            msg="에러 발생",
            args=None,
            exc_info=sys.exc_info(),
        )
    payload = _format_record(formatter, record)
    assert "exc_info" in payload
    assert "ValueError" in payload["exc_info"]


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------


def test_configure_logging_파일_핸들러_부착(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "run.jsonl"
    configure_logging(level="DEBUG", json_path=log_path)

    logger = logging.getLogger("kpi.test")
    bind_request_id("rid-file-1")
    logger.info("디스크 기록 테스트", extra={"key": "value"})

    # 핸들러 flush
    for h in logging.getLogger().handlers:
        h.flush()

    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert text  # 빈 파일 아님
    last_line = text.strip().splitlines()[-1]
    payload = json.loads(last_line)
    assert payload["message"] == "디스크 기록 테스트"
    assert payload["request_id"] == "rid-file-1"
    assert payload["key"] == "value"


def test_configure_logging_중복_부착_방지(tmp_path: Path) -> None:
    """동일 경로로 재호출 시 핸들러가 누적되지 않는다."""

    log_path = tmp_path / "logs" / "r1.jsonl"
    configure_logging(level="INFO", json_path=log_path)
    first_count = len(logging.getLogger().handlers)
    configure_logging(level="INFO", json_path=log_path)
    second_count = len(logging.getLogger().handlers)
    assert first_count == second_count
