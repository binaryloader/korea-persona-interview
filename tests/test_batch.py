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
    _aggregate_usage,
    _build_failed_record,
    _count_failure_reasons,
    _summarize_records,
    run_batch,
    save_batch_result,
)
from src.llm_client import LLMClient
from src.models import (
    BatchResult,
    ConfigError,
    EmptyResponseError,
    Flags,
    InterviewRecord,
    PersonaMeta,
    RawResponse,
    RetryExhaustedError,
    RunMeta,
    SCHEMA_VERSION,
    ServerNotReachableError,
    StructuredSummaryParseError,
    TokenUsage,
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


def _completed_record(persona_id: str = "p-x") -> InterviewRecord:
    """completed 상태의 ``InterviewRecord``를 만든다(resume 테스트용)."""

    return InterviewRecord(
        persona_id=persona_id,
        persona_meta=_persona(persona_id),
        started_at="2026-05-02T12:00:00+00:00",
        finished_at="2026-05-02T12:01:00+00:00",
        status="completed",
        messages=[],
        raw_responses=[],
        structured_summary=None,
        flags=Flags(),
        error=None,
    )


def _failed_record(persona_id: str = "p-x") -> InterviewRecord:
    return InterviewRecord(
        persona_id=persona_id,
        persona_meta=_persona(persona_id),
        started_at="2026-05-02T12:00:00+00:00",
        finished_at="2026-05-02T12:01:00+00:00",
        status="failed",
        messages=[],
        raw_responses=[],
        structured_summary=None,
        flags=Flags(),
        error={"type": "retry_exhausted", "message": "boom"},
    )


def _add_models_response(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "test-model"}]},
        status_code=200,
    )


def _add_chat_response(httpx_mock, content: str) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.openai.com/v1/chat/completions",
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


def test_aggregate_usage_빈_records_제로_사용량() -> None:
    """빈 record 리스트는 0으로 채운 ``TokenUsage``를 반환한다."""

    total = _aggregate_usage([])
    assert isinstance(total, TokenUsage)
    assert total.prompt_tokens == 0
    assert total.cached_tokens == 0


def test_aggregate_usage_record당_RawResponse_합산() -> None:
    """record가 여러 RawResponse를 가지면 모든 usage를 합산한다.

    멀티턴 + 자동 follow-up은 같은 record 안에 여러 RawResponse를 만든다.
    """

    rec_a = InterviewRecord(
        persona_id="p1",
        persona_meta=_persona("p1"),
        started_at="t1",
        finished_at="t2",
        status="completed",
        messages=[],
        raw_responses=[
            RawResponse(
                question_index=0,
                response="ok1",
                latency_ms=10,
                retry_count=0,
                usage=TokenUsage(
                    prompt_tokens=100,
                    completion_tokens=20,
                    total_tokens=120,
                    cached_tokens=80,
                ),
            ),
            RawResponse(
                question_index=1,
                response="ok2",
                latency_ms=10,
                retry_count=0,
                usage=TokenUsage(
                    prompt_tokens=200,
                    completion_tokens=30,
                    total_tokens=230,
                    cached_tokens=160,
                ),
            ),
        ],
        structured_summary=None,
        flags=Flags(),
        error=None,
    )
    rec_b = InterviewRecord(
        persona_id="p2",
        persona_meta=_persona("p2"),
        started_at="t1",
        finished_at="t2",
        status="completed",
        messages=[],
        raw_responses=[
            RawResponse(
                question_index=0,
                response="ok",
                latency_ms=10,
                retry_count=0,
                usage=TokenUsage(
                    prompt_tokens=50,
                    completion_tokens=5,
                    total_tokens=55,
                    cached_tokens=40,
                ),
            ),
        ],
        structured_summary=None,
        flags=Flags(),
        error=None,
    )
    total = _aggregate_usage([rec_a, rec_b])
    assert total.prompt_tokens == 350
    assert total.completion_tokens == 55
    assert total.total_tokens == 405
    assert total.cached_tokens == 280


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


@pytest.mark.parametrize(
    "exc, expected_type",
    [
        (ServerNotReachableError("연결 실패"), "server_not_reachable"),
        (RetryExhaustedError("3회 재시도 실패"), "retry_exhausted"),
        (EmptyResponseError("content empty"), "empty_response"),
        (ConfigError("설정 오류"), "config_error"),
        (StructuredSummaryParseError("JSON 파싱 실패"), "structured_summary_parse_error"),
        (ValueError("알 수 없음"), "unhandled_exception"),
        (RuntimeError("런타임 에러"), "unhandled_exception"),
    ],
)
def test_build_failed_record_도메인_예외_명시매핑(exc, expected_type) -> None:
    """도메인 예외는 ``error.type``이 명시 매핑된다(부분 실패 안내 가독성).

    회귀 보장 포인트: 새 도메인 예외 추가 시 ``_DOMAIN_EXC_TYPE_MAP``에도 같이
    등록해야 한다(누락 시 unhandled_exception으로 떨어진다).
    """

    persona = _persona("p-fail")
    record = _build_failed_record(persona, exc)
    assert record.status == "failed"
    assert record.error["type"] == expected_type


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

    async with LLMClient(config.llm) as client:
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

    # envelope.usage가 채워진다(모킹 응답에 usage 없으면 0).
    assert isinstance(envelope.usage, TokenUsage)
    # meta_extra에도 usage가 직렬화된다.
    extra = payload.get("meta_extra") or {}
    assert "usage" in extra


@pytest.mark.asyncio
async def test_run_batch_usage_누적_envelope(
    httpx_mock,
    make_app_config,
    tmp_path: Path,
) -> None:
    """실제 OpenAI 응답처럼 usage가 채워진 응답을 받으면 envelope.usage가
    합산된다."""

    _add_models_response(httpx_mock)

    # 1명 × (질문 1 + 요약 1) = 2번 호출. 각 호출에 usage 동봉.
    for _ in range(2):
        httpx_mock.add_response(
            method="POST",
            url="https://api.openai.com/v1/chat/completions",
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "한 번 시도해 보고 싶네요. 가격도 합리적입니다."}}
                ],
                "usage": {
                    "prompt_tokens": 1500,
                    "completion_tokens": 50,
                    "total_tokens": 1550,
                    "prompt_tokens_details": {"cached_tokens": 1200},
                },
            },
            status_code=200,
        )

    config = make_app_config(concurrency=1)
    personas = [_persona("p-0")]

    async with LLMClient(config.llm) as client:
        envelope = await run_batch(
            personas=personas,
            product="반찬",
            questions=["Q1"],
            follow_ups=[],
            llm=client,
            config=config,
            output_dir=tmp_path,
            slug="usage-test",
            seed=1,
            progress_disable=True,
        )

    # 인터뷰 본체 1회만 raw_responses에 들어간다(요약은 별도 단계라 합산 제외).
    assert envelope.usage.prompt_tokens == 1500
    assert envelope.usage.completion_tokens == 50
    assert envelope.usage.cached_tokens == 1200


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
            url="https://api.openai.com/v1/chat/completions",
            status_code=500,
        )

    config = make_app_config(concurrency=1, retry_max_attempts=3)
    personas = [_persona(f"p-{i}") for i in range(4)]

    async with LLMClient(config.llm) as client:
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
    async with LLMClient(config.llm) as client:
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
    async with LLMClient(config.llm) as client:
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
        url="https://api.openai.com/v1/models",
        status_code=503,
    )

    config = make_app_config()
    async with LLMClient(config.llm) as client:
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
    async with LLMClient(config.llm) as client:
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

    ``LLMClient.chat``을 monkeypatch로 가짜 함수로 교체해 카운터를 둔다.
    """

    from src.llm_client import LLMClient as _Client
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
    async with LLMClient(config.llm) as client:
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


# ---------------------------------------------------------------------------
# resume 모드(라운드 G9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_batch_resume_failed만_재시도(
    httpx_mock,
    make_app_config,
    tmp_path: Path,
) -> None:
    """resume_records로 status=failed record만 재시도한다.

    completed/refused/drift record는 재실행하지 않고 그대로 보존된다.
    """

    _add_models_response(httpx_mock)
    # 재시도되는 1명에 대한 chat 응답 2건(질문 + 요약). drift 휴리스틱을 자극하지
    # 않도록 평이한 한국어로 채운다.
    _add_chat_response(httpx_mock, "가격이 합리적이라 한번 시도해 볼 만한 것 같아요.")
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

    config = make_app_config(concurrency=1)
    # personas는 같은 시드/필터로 재샘플링된 가정. 3명 중 1명(p-2)이 직전 round
    # 에서 failed였고 나머지 2명은 completed였다고 가정한다.
    personas = [_persona(f"p-{i}") for i in range(3)]
    resume_records = [
        _completed_record("p-0"),
        _completed_record("p-1"),
        _failed_record("p-2"),
    ]

    async with LLMClient(config.llm) as client:
        envelope = await run_batch(
            personas=personas,
            product="반찬",
            questions=["Q1"],
            follow_ups=[],
            llm=client,
            config=config,
            output_dir=tmp_path,
            slug="resume-test",
            seed=42,
            progress_disable=True,
            resume_records=resume_records,
            resume_run_id="prev-id-abc",
        )

    # 기존 completed 2명 + 새로 완료된 1명 = 총 3명.
    assert envelope.summary.requested == 3
    assert envelope.summary.completed == 3
    assert envelope.summary.failed == 0
    # 결과 JSON에 previous_run_id가 박힌다.
    payload = json.loads(envelope.output_path.read_text(encoding="utf-8"))
    assert payload["meta_extra"]["previous_run_id"] == "prev-id-abc"


@pytest.mark.asyncio
async def test_run_batch_resume_모두_completed_LLM_미호출(
    make_app_config,
    tmp_path: Path,
) -> None:
    """모든 record가 이미 completed면 LLM 호출 없이 envelope을 만든다."""

    config = make_app_config(concurrency=1)
    personas = [_persona(f"p-{i}") for i in range(3)]
    resume_records = [
        _completed_record("p-0"),
        _completed_record("p-1"),
        _completed_record("p-2"),
    ]

    # llm은 빈 backend라도 healthcheck 자체가 실행되지 않아야 한다(짧은 경로).
    class _NoCallBackend:
        async def healthcheck(self) -> list:  # pragma: no cover - 호출되면 안 됨
            raise AssertionError("healthcheck가 호출되면 안 된다")

        async def chat(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("chat이 호출되면 안 된다")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    backend = _NoCallBackend()
    envelope = await run_batch(
        personas=personas,
        product="반찬",
        questions=["Q1"],
        follow_ups=[],
        llm=backend,  # type: ignore[arg-type]
        config=config,
        output_dir=tmp_path,
        slug="resume-only",
        seed=42,
        progress_disable=True,
        resume_records=resume_records,
        resume_run_id="prev-2",
    )

    assert envelope.summary.completed == 3
    assert envelope.partial_failure is False
    payload = json.loads(envelope.output_path.read_text(encoding="utf-8"))
    assert payload["meta_extra"]["previous_run_id"] == "prev-2"
