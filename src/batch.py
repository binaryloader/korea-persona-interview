"""Concurrent batch interview runner.

Runs ``run_interview`` for N personas in parallel and aggregates the results
into a ``BatchResult``. Concurrency is gated by ``asyncio.Semaphore``; the
progress bar uses tqdm in manual-update mode so partial-failure WARN lines
can be printed above the bar without breaking the carriage return.

This is the application layer that wires the LLM transport (``LLMBackend``)
to the domain model (``InterviewRecord``, ``RunMeta``, ``BatchResult``). A
single persona's failure must not take down sibling tasks, so each task is
wrapped in a try/except inside ``_run_single`` that converts known domain
exceptions into ``status=failed`` records. ``client.healthcheck()`` is
invoked once before the first persona starts so server outages surface
fast.

SIGINT handling has two stages, mirroring the documented UX in the UI
spec. First Ctrl+C: ``cancel_event.set()`` so no new task starts; tasks
already running finish their current chat call and save a partial JSON.
Second Ctrl+C: bubble ``KeyboardInterrupt`` and let the asyncio loop tear
everything down immediately.

Result JSON files land at ``outputs/interview_{slug}_{YYYYMMDD_HHMMSS}.json``
with ``partial: true`` marked in ``meta_extra`` when SIGINT or partial-
failure conditions hit. Serialization is plain ``dataclasses.asdict`` plus
``json.dumps(..., ensure_ascii=False, indent=2)`` so Korean content stays
readable in the file.

Resume mode (``resume_records=...``) reads a previous run's records, keeps
the completed/refused/drift entries verbatim, and only retries persona
ids whose status was ``failed``. The merged JSON gets a fresh timestamp
and ``meta_extra.previous_run_id`` linking back to the source run.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import signal
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from tqdm import tqdm

from .config import AppConfig
from .interview import run_interview

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from .llm_backend import LLMBackend
from .logging_setup import mask_persona_id, mask_product
from .models import (
    BatchResult,
    ConfigError,
    EmptyResponseError,
    InterviewRecord,
    PersonaMeta,
    RetryExhaustedError,
    RunMeta,
    SCHEMA_VERSION,
    ServerNotReachableError,
    StructuredSummaryParseError,
    TokenUsage,
)


# Maps known domain exception classes to the ``error.type`` string surfaced
# in the partial-failure summary. Anything not matched here drops into
# ``unhandled_exception`` so unexpected exceptions are still grouped in the
# distribution, just under a generic bucket.
_DOMAIN_EXC_TYPE_MAP: dict = {
    ServerNotReachableError: "server_not_reachable",
    RetryExhaustedError: "retry_exhausted",
    EmptyResponseError: "empty_response",
    ConfigError: "config_error",
    StructuredSummaryParseError: "structured_summary_parse_error",
}


logger = logging.getLogger(__name__)


# Fallback threshold for the partial-failure verdict. If the ratio of
# completed/drift/refused records dips below this value, the CLI exits with
# code 3 and the result JSON gets ``partial: true`` written into the meta.
# The single source of truth is ``BatchConfig.partial_failure_threshold``;
# this constant only kicks in on call paths that bypass yaml/CLI overrides.
_PARTIAL_SUCCESS_RATIO = 0.5


@dataclass(frozen=True)
class BatchSummary:
    """Lightweight aggregate used by the tqdm postfix and the end-of-run line.

    ``success_count`` includes refused and drift because the model returned a
    response in both cases; only ``failed`` (LLM call exhausted retries or
    raised a domain exception) counts as a hard failure.
    """

    requested: int
    completed: int
    refused: int
    failed: int
    drift: int
    cancelled: int

    @property
    def total_done(self) -> int:
        """Records that finished, excluding cancellations."""

        return self.completed + self.refused + self.failed + self.drift

    @property
    def success_count(self) -> int:
        """Successful records for the tqdm right-side counter."""

        return self.completed + self.refused + self.drift

    @property
    def failure_count(self) -> int:
        """Hard-failure records for the tqdm right-side counter."""

        return self.failed


@dataclass(frozen=True)
class BatchResultEnvelope:
    """Return container for ``run_batch``.

    ``BatchResult`` is the serialization unit, but the CLI also needs the
    output path, partial/cancellation flags, the failure-reason histogram,
    and the aggregated token usage to render the exit message and pick the
    exit code. This envelope bundles all of that so ``main.py`` does not have
    to reach into the result structure.

    ``usage`` covers every chat call inside the batch (multi-turn interview
    plus auto follow-up). Structured-summary and qualitative-insight calls
    happen in separate stages and are not folded in here.
    """

    result: BatchResult
    output_path: Optional[Path]
    summary: BatchSummary
    partial_failure: bool
    cancelled: bool
    failure_reason_counts: dict
    usage: TokenUsage = field(default_factory=lambda: TokenUsage())


# ---------------------------------------------------------------------------
# 직렬화 헬퍼
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO 8601 UTC timestamp truncated to seconds."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp_filename() -> str:
    """``YYYYMMDD_HHMMSS`` UTC string used in result file names."""

    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _serialize_batch(
    result: BatchResult,
    *,
    partial: bool,
    extra_meta: Optional[dict] = None,
) -> str:
    """Serialize a ``BatchResult`` to a JSON string for disk storage.

    ``ensure_ascii=False`` keeps Korean text readable on disk. Masking
    policy applies to log lines only - the result JSON intentionally retains
    the verbatim ``--product`` body and persona names so that downstream
    analysis tools can use the raw fields.
    """

    payload = dataclasses.asdict(result)
    if partial or extra_meta:
        payload["partial"] = bool(partial)
    if extra_meta:
        payload["meta_extra"] = dict(extra_meta)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def save_batch_result(
    result: BatchResult,
    output_dir: Path,
    *,
    slug: str,
    timestamp: Optional[str] = None,
    partial: bool = False,
    extra_meta: Optional[dict] = None,
) -> Path:
    """Atomically write a ``BatchResult`` to ``outputs/interview_{slug}_{ts}.json``.

    Args:
        result: ``BatchResult`` to serialize.
        output_dir: Destination directory. Created if missing.
        slug: File-name slug.
        timestamp: ``YYYYMMDD_HHMMSS`` UTC string. Defaults to ``now``.
        partial: When True, ``partial=True`` is added to the JSON meta.
        extra_meta: Additional meta block (SIGINT reason, env info, ...).

    Returns:
        Absolute path the JSON was written to.
    """

    ts = timestamp or _timestamp_filename()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Tighten outputs/ to mode 0700 so other local users cannot read interview
    # bodies that we already shipped to an external LLM. chmod is a no-op on
    # Windows but the call itself is safe.
    try:
        os.chmod(output_dir, 0o700)
    except (PermissionError, OSError):
        pass
    file_name = f"interview_{slug}_{ts}.json"
    target = output_dir / file_name

    serialized = _serialize_batch(result, partial=partial, extra_meta=extra_meta)

    # Atomic write via tmp + os.replace. A naive ``write_text`` can leave a
    # truncated JSON behind if SIGINT or kill -9 lands mid-write; the rename
    # guarantees readers either see the full file or the previous version.
    tmp_target = target.with_suffix(target.suffix + ".tmp")
    tmp_target.write_text(serialized, encoding="utf-8")
    os.replace(tmp_target, target)
    # 0600 on the result file matches the 0700 directory bound: prevents other
    # users on the same host from reading the response body.
    try:
        os.chmod(target, 0o600)
    except (PermissionError, OSError):
        pass

    logger.info(
        "배치 결과 저장",
        extra={
            "path": str(target),
            "records": len(result.records),
            "partial": partial,
        },
    )
    return target


# ---------------------------------------------------------------------------
# 통계 헬퍼
# ---------------------------------------------------------------------------


def _summarize_records(
    records: list,
    requested: int,
    cancelled: int,
) -> BatchSummary:
    """``InterviewRecord`` 리스트로 ``BatchSummary``를 만든다."""

    statuses = Counter(getattr(r, "status", "") for r in records)
    return BatchSummary(
        requested=requested,
        completed=int(statuses.get("completed", 0)),
        refused=int(statuses.get("refused", 0)),
        failed=int(statuses.get("failed", 0)),
        drift=int(statuses.get("drift", 0)),
        cancelled=cancelled,
    )


def _aggregate_usage(records: list) -> TokenUsage:
    """모든 record의 ``raw_responses[*].usage`` 합산.

    멀티턴 한 호출이 한 ``RawResponse``를 만든다. 자동 follow-up도 별도
    ``RawResponse``로 누적되므로 사실상 모든 chat 호출의 usage가 합산된다.
    구조화 요약과 정성 인사이트 단계는 별도 호출이라 본 합산에 포함되지 않는다.
    """

    total = TokenUsage()
    for r in records:
        for raw in getattr(r, "raw_responses", []) or []:
            usage = getattr(raw, "usage", None)
            if isinstance(usage, TokenUsage):
                total = total.add(usage)
    return total


def _count_failure_reasons(records: list) -> dict:
    """``status=failed`` record의 ``error.type`` 빈도 dict.

    UI §2.3.6 부분 실패 안내 메시지에 사용한다.
    """

    counts: Counter = Counter()
    for r in records:
        if getattr(r, "status", "") != "failed":
            continue
        err = getattr(r, "error", None)
        if isinstance(err, dict):
            reason = err.get("type") or "unknown"
        else:
            reason = "unknown"
        counts[str(reason)] += 1
    return dict(counts)


# ---------------------------------------------------------------------------
# 단일 페르소나 task 헬퍼
# ---------------------------------------------------------------------------


async def _run_single(
    persona: PersonaMeta,
    product: str,
    questions: list,
    follow_ups: list,
    llm: "LLMBackend",
    config: AppConfig,
    semaphore: asyncio.Semaphore,
    cancel_event: asyncio.Event,
) -> Optional[InterviewRecord]:
    """페르소나 1명에 대한 인터뷰 1회. semaphore로 동시성 제어한다.

    ``cancel_event``가 set되면 새 호출을 시작하지 않고 ``None``을 반환한다.
    이미 진행 중이던 호출은 끝까지 진행한 뒤 결과를 반환한다(UI §6.3).
    """

    if cancel_event.is_set():
        return None

    async with semaphore:
        if cancel_event.is_set():
            return None
        try:
            record = await run_interview(
                persona=persona,
                product=product,
                questions=questions,
                follow_ups=follow_ups,
                llm=llm,
                config=config,
            )
            return record
        except asyncio.CancelledError:
            # 본 페르소나는 시작 전에 취소된 것이라 None 반환으로 미진행 표기.
            raise
        except (
            ServerNotReachableError,
            RetryExhaustedError,
            EmptyResponseError,
            ConfigError,
            StructuredSummaryParseError,
        ) as exc:
            # 도메인 예외. ``InterviewSession``이 먼저 record로 변환하는 게 정상
            # 흐름이지만(인터뷰 본체 보존), 호출 자체가 시작 전에 실패하면
            # 본 layer에서 흡수한다. ``error.type``은 ``_classify_exception``이
            # 도메인 예외명을 매핑하므로 부분 실패 사유 분포에서 식별된다.
            logger.warning(
                "페르소나 인터뷰 도메인 예외(흡수)",
                extra={
                    "persona_id_hash": mask_persona_id(persona.persona_id),
                    "reason": str(exc),
                    "type": _classify_exception(exc),
                },
            )
            return _build_failed_record(persona, exc)
        except Exception as exc:  # noqa: BLE001 - 안전망
            # 예상 못한 예외가 새어 나오면 본 layer에서 흡수한다(다른 task가
            # 죽지 않도록). status="failed"로 변환해 record를 만든다. 추적
            # 정보를 stack trace로 남겨 사후 분석을 돕는다.
            logger.error(
                "페르소나 인터뷰 예외(흡수)",
                extra={
                    "persona_id_hash": mask_persona_id(persona.persona_id),
                    "reason": str(exc),
                    "exception_type": type(exc).__name__,
                },
                exc_info=True,
            )
            return _build_failed_record(persona, exc)


def _classify_exception(exc: BaseException) -> str:
    """예외 인스턴스를 ``error.type`` 문자열로 분류한다.

    도메인 예외는 ``_DOMAIN_EXC_TYPE_MAP``으로 명시 매핑하고, 나머지는
    ``unhandled_exception``으로 떨어진다. 부분 실패 안내(UI §2.3.6)에서 사유
    분포를 사람이 읽을 수 있게 표기하기 위함이다.
    """

    for cls, label in _DOMAIN_EXC_TYPE_MAP.items():
        if isinstance(exc, cls):
            return label
    return "unhandled_exception"


def _build_resume_only_envelope(
    *,
    records: list,
    product: str,
    questions: list,
    follow_ups: list,
    config: AppConfig,
    output_dir: Path,
    slug: str,
    seed: int,
    save: bool,
    previous_run_id: Optional[str] = None,
) -> "BatchResultEnvelope":
    """모든 record가 이미 completed인 resume 호출의 짧은 경로.

    LLM 호출과 SIGINT 핸들러 부착 없이 기존 결과를 그대로 ``BatchResultEnvelope``
    로 감싸 반환한다. JSON 저장도 새 timestamp로 한 번 더 수행해 호출 시점의
    파일이 완성된 결과와 동일한 형식으로 남는다.
    """

    started_at = _now_iso()
    file_timestamp = _timestamp_filename()
    interview_id = uuid.uuid4().hex
    summary = _summarize_records(records, requested=len(records), cancelled=0)
    failure_reasons = _count_failure_reasons(records)
    aggregated_usage = _aggregate_usage(list(records))

    config_snapshot = {
        "concurrency": int(config.batch.concurrency),
        "temperature": config.llm.temperature,
        "max_tokens": config.llm.max_tokens,
        "timeout": config.llm.timeout,
        "context_budget": config.llm.context_budget,
        "single_turn": bool(config.batch.single_turn),
        "persona_fields": list(config.batch.persona_fields),
    }
    meta = RunMeta(
        interview_id=interview_id,
        slug=slug,
        schema_version=SCHEMA_VERSION,
        product=product,
        questions=list(questions),
        follow_up_questions=list(follow_ups),
        model=config.llm.model,
        seed=int(seed),
        started_at=started_at,
        finished_at=_now_iso(),
        config_snapshot=config_snapshot,
    )
    result = BatchResult(meta=meta, records=list(records))

    output_path: Optional[Path] = None
    if save:
        extra_meta: dict = {
            "cancelled": False,
            "cancelled_count": 0,
            "summary": dataclasses.asdict(summary),
            "failure_reason_counts": failure_reasons,
            "product_masked": mask_product(product),
            "usage": dataclasses.asdict(aggregated_usage),
        }
        if previous_run_id:
            extra_meta["previous_run_id"] = previous_run_id
        output_path = save_batch_result(
            result,
            output_dir=output_dir,
            slug=slug,
            timestamp=file_timestamp,
            partial=False,
            extra_meta=extra_meta,
        )
    return BatchResultEnvelope(
        result=result,
        output_path=output_path,
        summary=summary,
        partial_failure=False,
        cancelled=False,
        failure_reason_counts=failure_reasons,
        usage=aggregated_usage,
    )


def _build_failed_record(
    persona: PersonaMeta,
    exc: BaseException,
) -> InterviewRecord:
    """예외를 ``status=failed`` record로 변환한다.

    ``error.type``은 ``_classify_exception``이 도메인 예외를 명시 매핑한 결과를
    채운다. 알려지지 않은 예외만 ``unhandled_exception``으로 떨어진다.
    """

    from .models import Flags  # 지역 import로 순환 의존 회피.

    now = _now_iso()
    return InterviewRecord(
        persona_id=persona.persona_id,
        persona_meta=persona,
        started_at=now,
        finished_at=now,
        status="failed",
        messages=[],
        raw_responses=[],
        structured_summary=None,
        flags=Flags(),
        error={"type": _classify_exception(exc), "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# tqdm 진행률 헬퍼
# ---------------------------------------------------------------------------


class _ProgressTracker:
    """tqdm 인스턴스와 카운터를 한 곳에 모은 헬퍼.

    UI §6.1의 우측 카운터(``완료=N 실패=M``)를 ``set_postfix_str``로 갱신한다.
    """

    def __init__(self, total: int, *, disable: bool = False) -> None:
        self._bar = tqdm(
            total=total,
            desc="인터뷰 진행 중",
            unit="persona",
            disable=disable,
            dynamic_ncols=True,
        )
        self._completed = 0
        self._failed = 0

    def update(self, record: Optional[InterviewRecord]) -> None:
        """완료된 record 1건을 반영한다. ``record``가 None이면 취소된 것이다."""

        if record is None:
            return
        if record.status == "failed":
            self._failed += 1
        else:
            self._completed += 1
        self._bar.update(1)
        self._bar.set_postfix_str(
            f"완료={self._completed} 실패={self._failed}",
            refresh=False,
        )

    def write(self, message: str) -> None:
        """진행률 라인을 깨뜨리지 않고 단발 메시지를 출력한다(UI §6.2)."""

        try:
            self._bar.write(message)
        except (AttributeError, OSError):
            # AttributeError: tqdm이 disable이라 일부 메서드가 결손인 케이스.
            # OSError: stderr가 닫혀 print/write 실패하는 케이스(긴 배치 종료 시).
            print(message)
        except Exception:  # noqa: BLE001 - tqdm 신규 버전 예외 안전망
            print(message)

    def close(self) -> None:
        self._bar.close()


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


async def run_batch(
    personas: list,
    product: str,
    questions: list,
    follow_ups: list,
    llm: "LLMBackend",
    config: AppConfig,
    output_dir: Path,
    *,
    slug: str = "korea-persona-interview",
    seed: int = 0,
    save: bool = True,
    progress_disable: bool = False,
    resume_records: Optional[list] = None,
    resume_run_id: Optional[str] = None,
) -> BatchResultEnvelope:
    """Run batch interviews for the given personas concurrently.

    Concurrency comes from ``config.batch.concurrency``. ``BatchConfig``
    enforces the 1-10 bound at construction time; we re-check here so direct
    callers (tests, scripts) cannot bypass it.

    First SIGINT sets ``cancel_event`` so no new persona starts; in-flight
    calls finish their current chat round-trip and the partial result is
    written when ``save=True``. Second SIGINT bubbles ``KeyboardInterrupt``.

    Resume mode reuses an earlier run's records: ``resume_records`` carries
    the previous ``InterviewRecord`` list, ``resume_run_id`` is stored in
    ``meta_extra.previous_run_id``. Personas whose previous status was not
    ``failed`` are kept verbatim and skipped in the new batch; only failed
    persona ids are retried, preserving stable identifiers for downstream
    diffing.

    Args:
        personas: Personas to interview.
        product: One-line product description.
        questions: Main question list (1 or more).
        follow_ups: User-defined common follow-ups (may be empty).
        llm: An ``LLMBackend`` already open in an ``async with`` block.
        config: Top-level ``AppConfig``; only ``llm``/``batch``/``interview``
            are touched here.
        output_dir: Result JSON directory.
        slug: File-name slug; defaults to ``korea-persona-interview``.
        seed: Stored on ``RunMeta.seed`` for traceability. Sampling itself
            happens upstream in ``load_personas``.
        save: When True, both clean and partial completions are persisted
            via ``save_batch_result``.
        progress_disable: Disable the tqdm bar (tests, dry-run).
        resume_records: Previous-run records for resume mode (optional).
        resume_run_id: ``interview_id`` of the previous run; written into
            ``meta_extra.previous_run_id`` when set.

    Returns:
        ``BatchResultEnvelope``. The CLI uses this to pick the exit code.

    Raises:
        ConfigError: Empty personas list or concurrency outside [1, 10].
        ServerNotReachableError: Pre-flight health check failed.
    """

    if not personas:
        raise ConfigError("personas가 비어 있다. 1명 이상 지정해 주세요")
    if not questions:
        raise ConfigError("questions가 비어 있다. 1개 이상 지정해 주세요")

    # Resume branch: keep every previously-completed record (completed,
    # refused, drift) untouched and only retry persona ids whose status was
    # ``failed``. The caller is expected to re-sample personas with the same
    # seed/filter so that persona ids match across the two runs.
    resume_completed_records: list = []
    if resume_records:
        completed_persona_ids: set = set()
        for r in resume_records:
            status = getattr(r, "status", None)
            pid = getattr(r, "persona_id", None)
            if not status or not pid:
                continue
            if status != "failed":
                resume_completed_records.append(r)
                completed_persona_ids.add(pid)
        # 같은 persona_id가 personas 목록에 있으면 재실행에서 제외한다.
        personas = [p for p in personas if p.persona_id not in completed_persona_ids]
        logger.info(
            "resume 모드 진입",
            extra={
                "resume_completed": len(resume_completed_records),
                "to_retry": len(personas),
                "previous_run_id": resume_run_id,
            },
        )

    if not personas and resume_completed_records:
        # 모든 record가 이미 completed인 resume 호출. 기존 결과만 보존하고 LLM
        # 호출 없이 envelope을 만들어 반환한다.
        logger.info(
            "resume 호출이지만 재시도할 failed record가 없다. 기존 결과 그대로 보존",
            extra={"completed": len(resume_completed_records)},
        )
        return _build_resume_only_envelope(
            records=resume_completed_records,
            product=product,
            questions=questions,
            follow_ups=follow_ups,
            config=config,
            output_dir=output_dir,
            slug=slug,
            seed=seed,
            save=save,
            previous_run_id=resume_run_id,
        )

    concurrency = int(config.batch.concurrency)
    if not (1 <= concurrency <= 10):
        # BatchConfig가 이미 검증하지만 방어적으로 한 번 더 차단.
        raise ConfigError(
            f"동시성은 1-10 범위만 허용한다. 입력값: {concurrency}"
        )

    # 시작 직전 헬스체크. 서버 다운이면 즉시 실패시켜 사용자가 빠르게 조치할 수
    # 있도록 한다(PRD §4.4, UI §2.3.3).
    try:
        models = await llm.healthcheck()
    except ServerNotReachableError:
        raise
    logger.info(
        "배치 시작 헬스체크 통과",
        extra={"models_available": len(models)},
    )

    started_at = _now_iso()
    file_timestamp = _timestamp_filename()
    interview_id = uuid.uuid4().hex
    cancel_event = asyncio.Event()

    # SIGINT 핸들러 등록(1회: cancel set, 2회: 기본 KeyboardInterrupt).
    loop = asyncio.get_running_loop()
    sigint_press_count = {"n": 0}
    handler_attached = False

    def _sigint_handler() -> None:
        sigint_press_count["n"] += 1
        if sigint_press_count["n"] == 1:
            # 진행 중 호출은 끝까지 보존하고 새 호출을 막는다(UI §6.3 1회).
            cancel_event.set()
            logger.warning("SIGINT 수신: 부분 결과 저장 후 종료 예정")
            try:
                # tqdm 라인 위로 안내 출력. 핸들러 안이라 직접 print.
                print(
                    "\n[WARN] 사용자 중단 신호를 받았습니다. "
                    "진행 중인 호출을 마무리한 뒤 부분 결과를 저장합니다. "
                    "한 번 더 Ctrl+C를 누르면 즉시 종료합니다.",
                    flush=True,
                )
            except (OSError, ValueError):
                # stderr가 닫혀 print 실패하는 케이스. 핸들러 안이라 무시한다.
                pass
        else:
            # 2회: 기본 KeyboardInterrupt 흐름 복원해 즉시 종료.
            try:
                signal.signal(signal.SIGINT, signal.default_int_handler)
            except (ValueError, OSError):
                pass
            raise KeyboardInterrupt()

    try:
        loop.add_signal_handler(signal.SIGINT, _sigint_handler)
        handler_attached = True
    except (NotImplementedError, RuntimeError):
        # Windows 또는 일부 이벤트 루프에서는 add_signal_handler가 동작하지 않는다.
        # 그 경우 일반 KeyboardInterrupt 흐름에 의존한다.
        handler_attached = False

    semaphore = asyncio.Semaphore(concurrency)
    progress = _ProgressTracker(total=len(personas), disable=progress_disable)

    tasks: list = []
    for persona in personas:
        coro = _run_single(
            persona=persona,
            product=product,
            questions=questions,
            follow_ups=follow_ups,
            llm=llm,
            config=config,
            semaphore=semaphore,
            cancel_event=cancel_event,
        )
        tasks.append(asyncio.create_task(coro))

    records: list = []
    cancelled_count = 0
    keyboard_interrupt = False

    try:
        # as_completed 패턴으로 완료 순서대로 진행률을 갱신한다(UI §6.1, TDD §9).
        for finished in asyncio.as_completed(tasks):
            try:
                record = await finished
            except asyncio.CancelledError:
                cancelled_count += 1
                continue
            except Exception as exc:  # noqa: BLE001 - 안전망
                # _run_single 안에서 흡수되지만 만일을 대비한다.
                logger.error(
                    "task 결과 취합 중 예외(흡수)",
                    extra={"reason": str(exc)},
                )
                cancelled_count += 1
                continue
            if record is None:
                # cancel_event 이후에 깨어난 task. 미진행으로 카운트한다.
                cancelled_count += 1
                continue
            records.append(record)
            progress.update(record)
    except KeyboardInterrupt:
        # 2번째 SIGINT. 모든 task를 cancel하고 partial 저장 흐름으로 진입한다.
        keyboard_interrupt = True
        cancel_event.set()
        for t in tasks:
            if not t.done():
                t.cancel()
        # cancel 처리 시간을 짧게 부여(완료 기록 누락 방지).
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:  # noqa: BLE001
            pass
        # 이미 완료된 task의 결과는 보존한다.
        for t in tasks:
            if t.done() and not t.cancelled():
                try:
                    result = t.result()
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(result, InterviewRecord) and result not in records:
                    records.append(result)
    finally:
        progress.close()
        if handler_attached:
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError):
                pass

    finished_at = _now_iso()
    # resume 모드: 기존 completed/refused/drift record를 새 records 앞에 합친다.
    # cancelled 카운트는 본 호출 안에서 발생한 것만 센다.
    if resume_completed_records:
        records = list(resume_completed_records) + list(records)
    summary = _summarize_records(
        records,
        requested=len(personas) + len(resume_completed_records),
        cancelled=cancelled_count,
    )
    failure_reasons = _count_failure_reasons(records)

    cancelled = cancel_event.is_set() or keyboard_interrupt or cancelled_count > 0

    # 부분 실패 판정: 실제 종료된 record 중 success(=completed/refused/drift)
    # 비율이 임계값 미만이면 partial로 본다(PRD §5.9, UI §6.4). 임계값은
    # ``BatchConfig.partial_failure_threshold``에서 받는다.
    if summary.requested == 0:
        partial_failure = False
    else:
        success_ratio = summary.success_count / summary.requested
        partial_failure = (
            success_ratio < config.batch.partial_failure_threshold or cancelled
        )

    aggregated_usage = _aggregate_usage(list(records))

    config_snapshot = {
        "concurrency": concurrency,
        "temperature": config.llm.temperature,
        "max_tokens": config.llm.max_tokens,
        "timeout": config.llm.timeout,
        "context_budget": config.llm.context_budget,
        "single_turn": bool(config.batch.single_turn),
        "persona_fields": list(config.batch.persona_fields),
    }

    meta = RunMeta(
        interview_id=interview_id,
        slug=slug,
        schema_version=SCHEMA_VERSION,
        product=product,
        questions=list(questions),
        follow_up_questions=list(follow_ups),
        model=config.llm.model,
        seed=int(seed),
        started_at=started_at,
        finished_at=finished_at,
        config_snapshot=config_snapshot,
    )
    result = BatchResult(meta=meta, records=list(records))

    output_path: Optional[Path] = None
    if save:
        extra_meta = {
            "cancelled": cancelled,
            "cancelled_count": cancelled_count,
            "summary": dataclasses.asdict(summary),
            "failure_reason_counts": failure_reasons,
            "product_masked": mask_product(product),
            "usage": dataclasses.asdict(aggregated_usage),
        }
        if resume_run_id:
            extra_meta["previous_run_id"] = resume_run_id
        output_path = save_batch_result(
            result,
            output_dir=output_dir,
            slug=slug,
            timestamp=file_timestamp,
            partial=partial_failure,
            extra_meta=extra_meta,
        )

    logger.info(
        "배치 인터뷰 종료",
        extra={
            "requested": summary.requested,
            "completed": summary.completed,
            "refused": summary.refused,
            "failed": summary.failed,
            "drift": summary.drift,
            "cancelled": summary.cancelled,
            "partial_failure": partial_failure,
            "prompt_tokens": aggregated_usage.prompt_tokens,
            "completion_tokens": aggregated_usage.completion_tokens,
            "cached_tokens": aggregated_usage.cached_tokens,
        },
    )

    if keyboard_interrupt:
        # 호출자(CLI)가 exit 130을 반환하도록 다시 raise한다. partial 저장은
        # 이미 완료된 상태다.
        raise KeyboardInterrupt()

    return BatchResultEnvelope(
        result=result,
        output_path=output_path,
        summary=summary,
        partial_failure=partial_failure,
        cancelled=cancelled,
        failure_reason_counts=failure_reasons,
        usage=aggregated_usage,
    )
