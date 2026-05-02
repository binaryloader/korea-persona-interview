"""배치 인터뷰 러너.

페르소나 N명에 대해 ``run_interview``를 동시 실행하고 결과를 ``BatchResult``로
모은다. 동시성은 ``asyncio.Semaphore``로 제어하며 진행률은 tqdm 수동 패턴으로
표시한다(TDD §3.6, §9, UI §6).

application 계층이며 infrastructure(``MlxLLMClient``)와 domain
(``InterviewRecord``, ``RunMeta``, ``BatchResult``)을 조합한다(architecture.md
§1, §2). 단일 페르소나 task 실패가 다른 task를 죽이지 않도록 ``return_exceptions``
패턴을 사용하고, 시작 직전 ``client.healthcheck()``를 1회 호출해 서버 가용성을
검증한다.

SIGINT(Ctrl+C) 1회는 진행 중인 호출이 끝나는 대로 ``cancel_event``를 set해
남은 task를 cancel하고 partial 결과를 저장한다. 2회는 즉시 종료(파이썬 기본
``KeyboardInterrupt`` 흐름)다(UI §6.3).

JSON 직렬화는 ``dataclasses.asdict`` + ``json.dumps(..., ensure_ascii=False,
indent=2)``로 한다. 결과 파일명은 ``outputs/interview_{slug}_{YYYYMMDD_HHMMSS}.json``
이며 SIGINT로 partial 저장된 파일도 같은 형식을 사용한다(파일 메타에 partial
플래그를 박아 후속 분석에서 구분한다).
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
from typing import Optional

from tqdm import tqdm

from ._pricing import estimate_cost_usd
from .config import AppConfig
from .interview import run_interview
from .llm_client import MlxLLMClient
from .logging_setup import mask_product
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


# 도메인 예외 → ``error.type`` 문자열 매핑(UI §2.3.6 부분 실패 안내). 알려진
# 예외명만 명시 매핑하고 나머지는 ``unhandled_exception``으로 떨어진다.
_DOMAIN_EXC_TYPE_MAP: dict = {
    ServerNotReachableError: "server_not_reachable",
    RetryExhaustedError: "retry_exhausted",
    EmptyResponseError: "empty_response",
    ConfigError: "config_error",
    StructuredSummaryParseError: "structured_summary_parse_error",
}


logger = logging.getLogger(__name__)


# 부분 실패 판정 임계값. 완료(`completed`/`drift`/`refused`) record 비율이 본 값
# 미만이면 BatchResult.partial_failure를 True로 표시한다. CLI 단계에서 exit 3
# 처리에 활용한다(PRD §5.9, UI §6.4).
_PARTIAL_SUCCESS_RATIO = 0.5


@dataclass(frozen=True)
class BatchSummary:
    """tqdm 카운터/콘솔 요약에 쓰는 간단한 통계.

    UI §6.1의 ``완료=N 실패=M`` 카운터와 §2.3.1 종료 메시지에 활용한다.
    """

    requested: int
    completed: int
    refused: int
    failed: int
    drift: int
    cancelled: int

    @property
    def total_done(self) -> int:
        """진행 종료된 record 수(취소 제외)."""

        return self.completed + self.refused + self.failed + self.drift

    @property
    def success_count(self) -> int:
        """tqdm 우측 카운터의 ``완료``(거부/드리프트도 응답 자체는 수신)."""

        return self.completed + self.refused + self.drift

    @property
    def failure_count(self) -> int:
        """tqdm 우측 카운터의 ``실패``(LLM 호출 실패 등)."""

        return self.failed


@dataclass(frozen=True)
class BatchResultEnvelope:
    """``run_batch`` 반환 컨테이너.

    ``BatchResult``는 직렬화 단위지만 CLI는 종료 코드 판정과 사용자 안내에
    추가 메타가 필요하다. 본 envelope에 partial/cancelled/저장 경로/누적 토큰
    사용량/추정 비용을 담아 main.py가 sys.exit 처리와 비용 표시를 수행한다.

    ``usage``는 본 배치의 모든 chat 호출(인터뷰 멀티턴 + 자동 follow-up + 구조화
    요약 + 정성 인사이트는 별도 단계라 미포함) 합산이다. ``estimated_cost_usd``는
    해당 사용량과 모델 단가로 추정한 비용으로, 정확한 청구 단가와 다를 수 있다
    (호출자에서 "추정" 표기 명시).
    """

    result: BatchResult
    output_path: Optional[Path]
    summary: BatchSummary
    partial_failure: bool
    cancelled: bool
    failure_reason_counts: dict
    usage: TokenUsage = field(default_factory=lambda: TokenUsage())
    estimated_cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# 직렬화 헬퍼
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO 8601 UTC 타임스탬프(초 단위)."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp_filename() -> str:
    """파일명에 박는 ``YYYYMMDD_HHMMSS`` 형식. 사용자 로컬 시간이 아닌 UTC다."""

    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _serialize_batch(
    result: BatchResult,
    *,
    partial: bool,
    extra_meta: Optional[dict] = None,
) -> str:
    """BatchResult를 JSON 문자열로 직렬화한다.

    ``ensure_ascii=False``로 한국어 본문을 그대로 보존한다(security.md/PRD
    §6.6의 마스킹은 결과 JSON이 아니라 로그 본문에만 적용한다).
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
    """``outputs/interview_{slug}_{ts}.json``에 결과를 저장한다(TDD §3.6).

    Args:
        result: 직렬화 대상 ``BatchResult``.
        output_dir: 저장 디렉토리. 없으면 생성한다.
        slug: 파일명에 박을 슬러그.
        timestamp: ``YYYYMMDD_HHMMSS`` 형식. 미지정 시 현재 시각 사용.
        partial: True면 메타에 ``partial=True`` 플래그를 추가한다.
        extra_meta: 추가 메타(SIGINT 사유, 환경 정보 등).

    Returns:
        저장된 절대 경로.
    """

    ts = timestamp or _timestamp_filename()
    output_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"interview_{slug}_{ts}.json"
    target = output_dir / file_name

    serialized = _serialize_batch(result, partial=partial, extra_meta=extra_meta)

    # 직접 ``target.write_text``로 쓰면 SIGINT/kill -9 도중 절단된 JSON이 남는
    # 사례가 생긴다. 같은 디렉토리에 임시 파일을 만든 뒤 ``os.replace``로
    # 원자 교체해 부분 쓰기 흔적을 남기지 않는다(error-handling.md §1).
    tmp_target = target.with_suffix(target.suffix + ".tmp")
    tmp_target.write_text(serialized, encoding="utf-8")
    os.replace(tmp_target, target)

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
    구조화 요약과 정성 인사이트 단계는 별도 호출이라 본 합산에 포함되지 않는다
    (해당 단계의 usage는 v1.1 후속 작업에서 record/envelope에 추가 검토).
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
    llm: MlxLLMClient,
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
            #
            # 후속(v1.1): ``ServerNotReachableError``가 동시성 단위로 다발하면
            # ``cancel_event``를 set해 circuit breaker로 동작하도록 보강 후보.
            logger.warning(
                "페르소나 인터뷰 도메인 예외(흡수)",
                extra={
                    "persona_id": persona.persona_id,
                    "reason": str(exc),
                    "type": _classify_exception(exc),
                },
            )
            return _build_failed_record(persona, exc)
        except Exception as exc:  # noqa: BLE001 - 안전망
            # 예상 못한 예외가 새어 나오면 본 layer에서 흡수한다(다른 task가
            # 죽지 않도록). status="failed"로 변환해 record를 만든다.
            logger.error(
                "페르소나 인터뷰 예외(흡수)",
                extra={
                    "persona_id": persona.persona_id,
                    "reason": str(exc),
                },
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
        except Exception:
            # tqdm가 disable인 환경에서도 메시지를 보존하기 위해 직접 print.
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
    llm: MlxLLMClient,
    config: AppConfig,
    output_dir: Path,
    *,
    slug: str = "korea-persona-interview",
    seed: int = 0,
    save: bool = True,
    progress_disable: bool = False,
) -> BatchResultEnvelope:
    """페르소나 N명에 대한 배치 인터뷰를 수행한다(TDD §3.6, §9).

    동시성은 ``config.batch.concurrency``를 따른다. 1-3 범위는 ``BatchConfig``의
    ``__post_init__``이 강제하지만 본 함수에서도 방어적으로 검증한다.

    SIGINT 1회는 진행 중인 호출이 끝나는 대로 ``cancel_event``를 set해 남은
    페르소나의 task를 cancel한다. partial 결과는 ``save=True``일 때 자동 저장한다.

    Args:
        personas: 인터뷰할 페르소나 리스트(``PersonaMeta``).
        product: 사업 아이템 한 줄 설명.
        questions: 질문 리스트(1개 이상).
        follow_ups: 사용자 정의 follow-up 리스트(빈 리스트 허용).
        llm: ``async with`` 컨텍스트 안의 ``MlxLLMClient``.
        config: ``AppConfig`` 전체. ``llm``/``batch``/``interview`` 섹션을 사용한다.
        output_dir: 결과 JSON 저장 디렉토리.
        slug: 파일명 슬러그(기본 ``korea-persona-interview``).
        seed: ``RunMeta.seed``에 박을 시드. 단순 메타용이라 샘플링은 별도 모듈.
        save: True면 정상/부분 종료 모두 ``save_batch_result``로 저장한다.
        progress_disable: True면 tqdm을 끈다(테스트, dry-run 등).

    Returns:
        ``BatchResultEnvelope``. CLI는 본 객체로 종료 코드를 판정한다.

    Raises:
        ConfigError: ``personas``가 비었거나 ``concurrency``가 범위를 벗어날 때.
        ServerNotReachableError: 시작 직전 헬스체크 실패.
    """

    if not personas:
        raise ConfigError("personas가 비어 있다. 1명 이상 지정해 주세요")
    if not questions:
        raise ConfigError("questions가 비어 있다. 1개 이상 지정해 주세요")

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
            except Exception:
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
    summary = _summarize_records(
        records,
        requested=len(personas),
        cancelled=cancelled_count,
    )
    failure_reasons = _count_failure_reasons(records)

    cancelled = cancel_event.is_set() or keyboard_interrupt or cancelled_count > 0

    # 부분 실패 판정: 실제 종료된 record 중 success(=completed/refused/drift)
    # 비율이 50% 미만이면 partial로 본다(PRD §5.9, UI §6.4).
    if summary.requested == 0:
        partial_failure = False
    else:
        success_ratio = summary.success_count / summary.requested
        partial_failure = success_ratio < _PARTIAL_SUCCESS_RATIO or cancelled

    aggregated_usage = _aggregate_usage(list(records))
    estimated_cost = estimate_cost_usd(aggregated_usage, config.llm.model)

    config_snapshot = {
        "concurrency": concurrency,
        "temperature": config.llm.temperature,
        "max_tokens": config.llm.max_tokens,
        "timeout": config.llm.timeout,
        "context_budget": config.llm.context_budget,
        "single_turn": False,
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
            "estimated_cost_usd": round(estimated_cost, 6),
        }
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
            "estimated_cost_usd": round(estimated_cost, 6),
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
        estimated_cost_usd=round(estimated_cost, 6),
    )
