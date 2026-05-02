"""``src.batch.run_batch`` 단위/통합 테스트.

- Semaphore 동시성: 페르소나 N명 task 생성과 ``return_exceptions`` 격리
- JSON 직렬화 형식: ``schema_version``, ``slug``, ``meta``, ``records``
- 부분 실패 임계값(50%)
- ``save_batch_result`` 파일 저장과 partial 플래그
- ``_count_failure_reasons`` 헬퍼
- 한 task 실패가 다른 task를 죽이지 않음(``_run_single`` 안전망)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.batch import (
    BatchSummary,
    _build_failed_record,
    _count_failure_reasons,
    _summarize_records,
    run_batch,
    save_batch_result,
)
from src.llm_client import MlxLLMClient
from src.models import (
    BatchResult,
    ConfigError,
    Flags,
    InterviewRecord,
    PersonaMeta,
    RunMeta,
    SCHEMA_VERSION,
    ServerNotReachableError,
)


def _persona(persona_id: str = "p-x", age: int = 27) -> PersonaMeta:
    return PersonaMeta(
        persona_id=persona_id,
        name=None,
        gender="여자",
        age=age,
        region="서울",
        subregion="서울-X",
        occupation="x",
        marital="x",
        education="x",
        raw={"persona": "요약"},
    )


def _add_models_response(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8080/v1/models",
        json={"data": [{"id": "test-model"}]},
        status_code=200,
    )


def _add_chat_response(httpx_mock, content: str) -> None:
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# 헬퍼 함수
# ---------------------------------------------------------------------------


def test_summarize_records_상태별_집계() -> None:
    records = [
        InterviewRecord(
            persona_id="p1",
            persona_meta=_persona("p1"),
            started_at="t1",
            finished_at="t2",
            status="completed",
            messages=[],
            raw_responses=[],
            structured_summary=None,
            flags=Flags(),
            error=None,
        ),
        InterviewRecord(
            persona_id="p2",
            persona_meta=_persona("p2"),
            started_at="t1",
            finished_at="t2",
            status="failed",
            messages=[],
            raw_responses=[],
            structured_summary=None,
            flags=Flags(),
            error={"type": "retry_exhausted", "message": "fail"},
        ),
    ]
    summary = _summarize_records(records, requested=2, cancelled=0)
    assert summary.completed == 1
    assert summary.failed == 1
    assert summary.success_count == 1
    assert summary.failure_count == 1
    assert summary.total_done == 2


def test_count_failure_reasons_타입별_빈도() -> None:
    records = [
        InterviewRecord(
            persona_id="p1",
            persona_meta=_persona("p1"),
            started_at="t1",
            finished_at="t2",
            status="failed",
            messages=[],
            raw_responses=[],
            structured_summary=None,
            flags=Flags(),
            error={"type": "retry_exhausted", "message": "x"},
        ),
        InterviewRecord(
            persona_id="p2",
            persona_meta=_persona("p2"),
            started_at="t1",
            finished_at="t2",
            status="failed",
            messages=[],
            raw_responses=[],
            structured_summary=None,
            flags=Flags(),
            error={"type": "retry_exhausted"},
        ),
        InterviewRecord(
            persona_id="p3",
            persona_meta=_persona("p3"),
            started_at="t1",
            finished_at="t2",
            status="completed",
            messages=[],
            raw_responses=[],
            structured_summary=None,
            flags=Flags(),
            error=None,
        ),
    ]
    counts = _count_failure_reasons(records)
    assert counts == {"retry_exhausted": 2}


def test_build_failed_record_미처리_예외_변환() -> None:
    persona = _persona("p-fail")
    record = _build_failed_record(persona, ValueError("터졌다"))
    assert record.status == "failed"
    assert record.error["type"] == "unhandled_exception"
    assert "터졌다" in record.error["message"]


# ---------------------------------------------------------------------------
# save_batch_result
# ---------------------------------------------------------------------------


def _make_batch_result(records: list = None) -> BatchResult:
    meta = RunMeta(
        interview_id="iv-1",
        slug="korea-persona-interview",
        schema_version=SCHEMA_VERSION,
        product="반찬",
        questions=["Q1"],
        follow_up_questions=[],
        model="test-model",
        seed=42,
        started_at="2026-05-02T00:00:00+00:00",
        finished_at="2026-05-02T00:00:10+00:00",
        config_snapshot={},
    )
    return BatchResult(meta=meta, records=records or [])


def test_save_batch_result_파일_저장_형식(tmp_path: Path) -> None:
    result = _make_batch_result()
    path = save_batch_result(
        result,
        output_dir=tmp_path,
        slug="korea-persona-interview",
        timestamp="20260502_120000",
    )
    assert path.name == "interview_korea-persona-interview_20260502_120000.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["meta"]["slug"] == "korea-persona-interview"
    assert payload["meta"]["schema_version"] == SCHEMA_VERSION
    assert payload["records"] == []


def test_save_batch_result_partial_플래그(tmp_path: Path) -> None:
    result = _make_batch_result()
    path = save_batch_result(
        result,
        output_dir=tmp_path,
        slug="korea-persona-interview",
        timestamp="20260502_120000",
        partial=True,
        extra_meta={"cancelled": True},
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["partial"] is True
    assert payload["meta_extra"]["cancelled"] is True


def test_save_batch_result_한국어_본문_보존(tmp_path: Path) -> None:
    """``ensure_ascii=False``로 한글이 그대로 저장된다."""

    persona = _persona("p1")
    record = InterviewRecord(
        persona_id="p1",
        persona_meta=persona,
        started_at="t1",
        finished_at="t2",
        status="completed",
        messages=[],
        raw_responses=[],
        structured_summary=None,
        flags=Flags(),
        error=None,
    )
    result = _make_batch_result(records=[record])
    path = save_batch_result(
        result,
        output_dir=tmp_path,
        slug="x",
        timestamp="t",
    )
    text = path.read_text(encoding="utf-8")
    assert "여자" in text
    assert "서울" in text


def test_save_batch_result_atomic_write_tmp_파일_미존재(tmp_path: Path) -> None:
    """atomic write 회귀: 저장 후 ``.tmp`` 파일이 남아 있지 않다.

    SIGINT/kill 도중 절단된 JSON이 출력 디렉토리에 남는 사고를 방지하기 위해
    같은 디렉토리에 임시 파일을 만들고 ``os.replace``로 교체한다. 정상 흐름에서
    임시 파일은 교체로 사라져야 한다.
    """

    result = _make_batch_result()
    path = save_batch_result(
        result,
        output_dir=tmp_path,
        slug="x",
        timestamp="t",
    )
    tmp_candidate = path.with_suffix(path.suffix + ".tmp")
    assert path.exists()
    assert not tmp_candidate.exists()
    # 원본도 정상 JSON이어야 한다.
    json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# run_batch 동시성과 JSON 저장 E2E
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_batch_정상_3명_completed(
    httpx_mock,
    make_app_config,
    tmp_path: Path,
) -> None:
    """3명에 대해 정상 인터뷰가 완료되고 JSON이 저장된다."""

    _add_models_response(httpx_mock)

    # 페르소나 3명 × (질문 1개 + 요약 1개) = 6번의 chat 호출
    for _ in range(3):
        _add_chat_response(
            httpx_mock,
            "가격이 합리적이라 한번 시도해 볼 만한 것 같아요. 최근에 비슷한 서비스를 찾고 있었거든요.",
        )
        _add_chat_response(
            httpx_mock,
            json.dumps(
                {
                    "intent": "positive",
                    "willingness_to_pay": 39900,
                    "willingness_to_pay_currency": "KRW",
                    "rejection_reasons": [],
                    "one_line": "긍정",
                },
                ensure_ascii=False,
            ),
        )

    config = make_app_config(concurrency=2)
    personas = [_persona(f"p-{i}", age=25 + i) for i in range(3)]

    async with MlxLLMClient(config.llm) as client:
        envelope = await run_batch(
            personas=personas,
            product="반찬",
            questions=["Q1"],
            follow_ups=[],
            llm=client,
            config=config,
            output_dir=tmp_path,
            slug="test-slug",
            seed=42,
            progress_disable=True,
        )

    assert envelope.summary.requested == 3
    assert envelope.summary.completed == 3
    assert envelope.partial_failure is False
    assert envelope.output_path is not None
    assert envelope.output_path.exists()

    # 저장된 JSON 검증
    payload = json.loads(envelope.output_path.read_text(encoding="utf-8"))
    assert payload["meta"]["slug"] == "test-slug"
    assert payload["meta"]["seed"] == 42
    assert len(payload["records"]) == 3
    for r in payload["records"]:
        assert r["status"] == "completed"


@pytest.mark.asyncio
async def test_run_batch_부분_실패_50_미만_partial_failure(
    httpx_mock,
    make_app_config,
    tmp_path: Path,
) -> None:
    """4명 중 3명이 5xx로 실패하면 partial_failure True."""

    _add_models_response(httpx_mock)

    # 첫 페르소나: 정상 + 요약
    _add_chat_response(httpx_mock, "가격이 합리적이라 한번 시도해 볼 만한 것 같아요. 최근에 관심 있었거든요.")
    _add_chat_response(
        httpx_mock,
        json.dumps(
            {
                "intent": "positive",
                "willingness_to_pay": 30000,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": [],
                "one_line": "x",
            },
            ensure_ascii=False,
        ),
    )
    # 나머지 3명: 5xx 9번(3명 × 3 retry)
    for _ in range(9):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/v1/chat/completions",
            status_code=500,
        )

    config = make_app_config(concurrency=1, retry_max_attempts=3)
    personas = [_persona(f"p-{i}") for i in range(4)]

    async with MlxLLMClient(config.llm) as client:
        envelope = await run_batch(
            personas=personas,
            product="반찬",
            questions=["Q1"],
            follow_ups=[],
            llm=client,
            config=config,
            output_dir=tmp_path,
            slug="x",
            seed=0,
            progress_disable=True,
        )

    # success(=completed)는 1명, failed 3명. 1/4 = 25% < 50% → partial_failure
    assert envelope.summary.completed == 1
    assert envelope.summary.failed == 3
    assert envelope.partial_failure is True


@pytest.mark.asyncio
async def test_run_batch_personas_비어있음_ConfigError(
    httpx_mock,
    make_app_config,
    tmp_path: Path,
) -> None:
    config = make_app_config()
    async with MlxLLMClient(config.llm) as client:
        with pytest.raises(ConfigError):
            await run_batch(
                personas=[],
                product="반찬",
                questions=["Q1"],
                follow_ups=[],
                llm=client,
                config=config,
                output_dir=tmp_path,
                slug="x",
                seed=0,
                progress_disable=True,
            )


@pytest.mark.asyncio
async def test_run_batch_questions_비어있음_ConfigError(
    httpx_mock,
    make_app_config,
    tmp_path: Path,
) -> None:
    config = make_app_config()
    async with MlxLLMClient(config.llm) as client:
        with pytest.raises(ConfigError):
            await run_batch(
                personas=[_persona("p")],
                product="반찬",
                questions=[],
                follow_ups=[],
                llm=client,
                config=config,
                output_dir=tmp_path,
                slug="x",
                seed=0,
                progress_disable=True,
            )


@pytest.mark.asyncio
async def test_run_batch_헬스체크_실패_ServerNotReachableError(
    httpx_mock,
    make_app_config,
    tmp_path: Path,
) -> None:
    """헬스체크 실패 시 즉시 ServerNotReachableError로 차단된다."""

    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8080/v1/models",
        status_code=503,
    )

    config = make_app_config()
    async with MlxLLMClient(config.llm) as client:
        with pytest.raises(ServerNotReachableError):
            await run_batch(
                personas=[_persona("p")],
                product="반찬",
                questions=["Q1"],
                follow_ups=[],
                llm=client,
                config=config,
                output_dir=tmp_path,
                slug="x",
                seed=0,
                progress_disable=True,
            )


@pytest.mark.asyncio
async def test_run_batch_save_False_파일_미저장(
    httpx_mock,
    make_app_config,
    tmp_path: Path,
) -> None:
    _add_models_response(httpx_mock)
    _add_chat_response(httpx_mock, "가격이 합리적이라 한번 시도해 볼 만한 것 같아요. 좋은 옵션이네요.")
    _add_chat_response(
        httpx_mock,
        json.dumps(
            {
                "intent": "positive",
                "willingness_to_pay": 30000,
                "willingness_to_pay_currency": "KRW",
                "rejection_reasons": [],
                "one_line": "x",
            },
            ensure_ascii=False,
        ),
    )

    config = make_app_config()
    async with MlxLLMClient(config.llm) as client:
        envelope = await run_batch(
            personas=[_persona("p")],
            product="반찬",
            questions=["Q1"],
            follow_ups=[],
            llm=client,
            config=config,
            output_dir=tmp_path,
            slug="x",
            seed=0,
            save=False,
            progress_disable=True,
        )

    assert envelope.output_path is None
    # 디렉토리에 파일이 없다(또는 logs만 있다)
    interview_files = list(tmp_path.glob("interview_*.json"))
    assert interview_files == []


@pytest.mark.asyncio
async def test_run_batch_concurrency_제한_2_동시실행(
    httpx_mock,
    make_app_config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """동시성 2일 때 동시에 실행되는 task 수가 2를 초과하지 않는다.

    ``MlxLLMClient.chat``을 monkeypatch로 가짜 함수로 교체해 카운터를 둔다.
    """

    from src.llm_client import MlxLLMClient as _Client
    from src.models import ChatResponse

    _add_models_response(httpx_mock)

    counter = {"current": 0, "peak": 0}

    async def fake_chat(self, messages, max_tokens=None, temperature=None):
        counter["current"] += 1
        counter["peak"] = max(counter["peak"], counter["current"])
        try:
            await asyncio.sleep(0.05)
            # messages 길이로 분기: 요약 호출은 system + user 2개, 인터뷰는 더 많을 수도 있음
            content = json.dumps(
                {
                    "intent": "positive",
                    "willingness_to_pay": 30000,
                    "willingness_to_pay_currency": "KRW",
                    "rejection_reasons": [],
                    "one_line": "x",
                },
                ensure_ascii=False,
            ) if any("인터뷰 분석가" in m.get("content", "") for m in messages) else (
                "가격이 합리적이라 한번 시도해 볼 만한 것 같아요. 최근에 비슷한 서비스를 찾고 있었어요."
            )
            return ChatResponse(content=content, latency_ms=10, retry_count=0)
        finally:
            counter["current"] -= 1

    monkeypatch.setattr(_Client, "chat", fake_chat, raising=True)

    config = make_app_config(concurrency=2)
    personas = [_persona(f"p-{i}") for i in range(5)]
    async with MlxLLMClient(config.llm) as client:
        envelope = await run_batch(
            personas=personas,
            product="반찬",
            questions=["Q1"],
            follow_ups=[],
            llm=client,
            config=config,
            output_dir=tmp_path,
            slug="x",
            seed=0,
            progress_disable=True,
        )

    assert envelope.summary.requested == 5
    assert counter["peak"] <= 2  # Semaphore(2) 제한 보장
