"""동시성 배치 인터뷰 실행기.

페르소나 N명에 대해 ``run_interview``를 병렬 실행하고 결과를 ``BatchResult``로
취합한다. 동시성은 ``asyncio.Semaphore``로 제어한다. 진행률 표시는 수동 update
모드의 tqdm을 사용해 부분 실패 WARN 라인을 progress bar 위에 carriage return을
깨뜨리지 않고 출력할 수 있다.

본 모듈은 LLM transport(``LLMBackend``)와 도메인 모델(``InterviewRecord``,
``RunMeta``, ``BatchResult``)을 잇는 application 계층이다. 한 페르소나의 실패가
형제 task를 함께 죽이면 안 되므로 ``_run_single`` 내부에서 try/except로
알려진 도메인 예외를 흡수해 ``status=failed`` record로 변환한다. 첫 페르소나가
시작하기 전에 ``client.healthcheck()``를 한 번 호출해 서버 장애를 빠르게
표면화한다.

SIGINT 처리는 UI 명세에 정의된 UX 그대로 두 단계로 동작한다. Ctrl+C 1회:
``cancel_event.set()``으로 새 task 시작을 막는다. 이미 진행 중인 task는 현재
chat 호출을 끝까지 마치고 partial JSON을 저장한다. Ctrl+C 2회:
``KeyboardInterrupt``를 그대로 전파해 asyncio 루프가 즉시 모든 작업을
종료한다.

결과 JSON 파일은 ``outputs/interview_{slug}_{YYYYMMDD_HHMMSS}.json`` 위치에
저장된다. SIGINT나 부분 실패 조건에 걸리면 ``meta_extra``에 ``partial: true``가
표기된다. 직렬화는 ``dataclasses.asdict`` + ``json.dumps(..., ensure_ascii=False,
indent=2)`` 조합이라 파일 안 한국어 본문이 그대로 사람이 읽기 좋게 남는다.

resume 모드(``resume_records=...``)는 이전 run의 record를 읽어
completed/refused/drift 항목을 그대로 보존하고 ``status=failed`` 페르소나 ID만
재시도한다. 합쳐진 JSON에는 새 timestamp가 부여되고 ``meta_extra.previous_run_id``로
원본 run으로 거슬러 올라갈 수 있다.
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

if TYPE_CHECKING:  # pragma: no cover - 타입 체크 전용 import
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


# 알려진 도메인 예외 클래스를 부분 실패 안내에 노출되는 ``error.type``
# 문자열로 매핑한다. 매핑되지 않은 예외는 ``unhandled_exception`` 버킷으로
# 떨어져, 예상 못한 예외도 사유 분포에 함께 집계된다.
_DOMAIN_EXC_TYPE_MAP: dict = {
    ServerNotReachableError: "server_not_reachable",
    RetryExhaustedError: "retry_exhausted",
    EmptyResponseError: "empty_response",
    ConfigError: "config_error",
    StructuredSummaryParseError: "structured_summary_parse_error",
}


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchSummary:
    """tqdm postfix와 종료 라인이 사용하는 경량 집계 dataclass.

    ``success_count``는 refused와 drift도 포함한다. 두 경우 모두 모델이 응답을
    돌려준 케이스이기 때문이다. ``failed``(LLM 호출이 retry를 모두 소진했거나
    도메인 예외가 raise된 경우)만 hard failure로 본다.
    """

    requested: int
    completed: int
    refused: int
    failed: int
    drift: int
    cancelled: int

    @property
    def total_done(self) -> int:
        """취소된 record를 제외한 완료 record 수."""

        return self.completed + self.refused + self.failed + self.drift

    @property
    def success_count(self) -> int:
        """tqdm 우측 카운터용 성공 record 수."""

        return self.completed + self.refused + self.drift

    @property
    def failure_count(self) -> int:
        """tqdm 우측 카운터용 hard failure record 수."""

        return self.failed


@dataclass(frozen=True)
class BatchResultEnvelope:
    """``run_batch``의 반환 컨테이너.

    직렬화 단위는 ``BatchResult``지만, CLI가 종료 메시지를 그리고 exit code를
    결정하려면 출력 경로, partial/cancellation 플래그, 실패 사유 히스토그램,
    합산 토큰 사용량까지 함께 필요하다. 본 envelope이 그 전부를 묶어
    ``main.py``가 결과 구조 내부로 손을 뻗지 않아도 되게 한다.

    ``usage``는 배치 내부의 모든 chat 호출을 합산한다(멀티턴 인터뷰 + 자동
    follow-up). 구조화 요약과 정성 인사이트 호출은 별도 단계라 본 합산에
    포함되지 않는다.
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
    """초 단위로 자른 ISO 8601 UTC 타임스탬프를 반환한다."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp_filename() -> str:
    """결과 파일명에 쓰이는 ``YYYYMMDD_HHMMSS`` UTC 문자열."""

    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _serialize_batch(
    result: BatchResult,
    *,
    partial: bool,
    extra_meta: Optional[dict] = None,
) -> str:
    """``BatchResult``를 디스크 저장용 JSON 문자열로 직렬화한다.

    ``ensure_ascii=False``로 한국어 본문을 그대로 보존한다. 마스킹 정책은
    로그 라인에만 적용된다. 결과 JSON은 의도적으로 ``--product`` 원문과
    페르소나 이름을 그대로 보존해, 다운스트림 분석 도구가 원본 필드를
    그대로 사용할 수 있게 한다.
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
    """``BatchResult``를 ``outputs/interview_{slug}_{ts}.json``에 atomic하게 저장한다.

    Args:
        result: 직렬화할 ``BatchResult``.
        output_dir: 저장 디렉토리. 없으면 생성한다.
        slug: 파일명 슬러그.
        timestamp: ``YYYYMMDD_HHMMSS`` UTC 문자열. 기본값은 ``now``.
        partial: True면 JSON meta에 ``partial=True``가 추가된다.
        extra_meta: 추가 meta 블록(SIGINT 사유, 환경 정보 등).

    Returns:
        JSON이 기록된 절대 경로.
    """

    ts = timestamp or _timestamp_filename()
    output_dir.mkdir(parents=True, exist_ok=True)
    # outputs/ 권한을 0700으로 조여, 같은 호스트의 다른 로컬 사용자가 외부
    # LLM에 이미 송신한 인터뷰 본문을 읽지 못하게 한다. Windows에서는 chmod가
    # no-op이지만 호출 자체는 안전하다.
    try:
        os.chmod(output_dir, 0o700)
    except (PermissionError, OSError):
        pass
    file_name = f"interview_{slug}_{ts}.json"
    target = output_dir / file_name

    serialized = _serialize_batch(result, partial=partial, extra_meta=extra_meta)

    # tmp + os.replace로 atomic write를 수행한다. 단순 ``write_text``는 쓰기
    # 도중 SIGINT나 kill -9가 들어오면 절단된 JSON을 남길 수 있다. rename
    # 패턴은 reader가 항상 완성된 파일 또는 이전 버전 중 하나를 보게 한다.
    tmp_target = target.with_suffix(target.suffix + ".tmp")
    tmp_target.write_text(serialized, encoding="utf-8")
    os.replace(tmp_target, target)
    # 결과 파일 0600은 디렉토리 0700과 짝을 이룬다. 같은 호스트의 다른
    # 사용자가 응답 본문을 읽지 못하게 한다.
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
        "persona_fields": list(config.common.persona.fields),
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
    """주어진 페르소나에 대해 배치 인터뷰를 동시 실행한다.

    동시성은 ``config.batch.concurrency``에서 결정된다. ``BatchConfig``는
    생성 시점에 1-10 범위를 강제하지만, 본 함수에서도 한 번 더 검증해 직접
    호출(tests, scripts)이 우회하지 못하게 한다.

    SIGINT 1회는 ``cancel_event``를 set해 새 페르소나가 시작되지 않게 한다.
    이미 진행 중인 호출은 현재 chat round-trip을 끝까지 마치고 ``save=True``
    일 때 partial 결과가 디스크에 기록된다. SIGINT 2회는 ``KeyboardInterrupt``를
    그대로 전파한다.

    resume 모드는 이전 run의 record를 재사용한다. ``resume_records``에 이전
    ``InterviewRecord`` 리스트를 넣고, ``resume_run_id``는
    ``meta_extra.previous_run_id``에 저장된다. 이전 status가 ``failed``가 아닌
    페르소나는 그대로 보존되고 새 배치에서 건너뛴다. 실패한 persona ID만 재시도되어
    다운스트림 diff용 안정 식별자를 유지한다.

    Args:
        personas: 인터뷰 대상 페르소나.
        product: 한 줄 product 설명.
        questions: 메인 질문 리스트(1개 이상).
        follow_ups: 사용자 정의 공통 follow-up(빈 리스트 허용).
        llm: ``async with`` 블록 안에서 이미 열린 ``LLMBackend``.
        config: 최상위 ``AppConfig``. 본 함수는 ``llm``/``batch``/``interview``만 사용한다.
        output_dir: 결과 JSON 저장 디렉토리.
        slug: 파일명 슬러그(기본값 ``korea-persona-interview``).
        seed: 추적용으로 ``RunMeta.seed``에 보존된다. 실제 샘플링은 상위
            ``load_personas`` 단계에서 발생한다.
        save: True면 정상 완료/partial 완료 모두 ``save_batch_result``로 저장한다.
        progress_disable: tqdm bar를 끈다(tests, dry-run 용).
        resume_records: resume 모드용 이전 run의 record(선택).
        resume_run_id: 이전 run의 ``interview_id``. 지정 시
            ``meta_extra.previous_run_id``에 기록된다.

    Returns:
        ``BatchResultEnvelope``. CLI가 본 값으로 exit code를 결정한다.

    Raises:
        ConfigError: 페르소나 리스트가 비었거나 concurrency가 [1, 10] 밖.
        ServerNotReachableError: 사전 healthcheck가 실패한 경우.
    """

    if not personas:
        raise ConfigError("personas가 비어 있다. 1명 이상 지정해 주세요")
    if not questions:
        raise ConfigError("questions가 비어 있다. 1개 이상 지정해 주세요")

    # resume 분기: 이전에 완료된 record(completed, refused, drift)는 그대로
    # 보존하고, status가 ``failed``인 persona ID만 재시도한다. caller는 같은
    # seed/필터로 페르소나를 재샘플링해 두 run 사이에서 persona ID가 일치하도록
    # 책임진다.
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
        "persona_fields": list(config.common.persona.fields),
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
