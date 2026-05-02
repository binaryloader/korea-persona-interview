"""리포트 생성기.

배치 인터뷰 결과 JSON을 읽어 정량 집계와 LLM 정성 인사이트를 결합한 마크다운
리포트를 생성한다(TDD §3.7, UI §4). ``statistics`` 모듈만 사용하고 numpy/scipy
의존을 도입하지 않는다(dependency.md §1).

application 계층이며 infrastructure(``MlxLLMClient``)와 domain(``InterviewRecord``,
``StructuredSummary``)을 조합한다(architecture.md §1).

리포트 마크다운은 H2 4개 섹션이다.

- ``## 1. 정량 지표``: 의향률, 가격 수용가, 거절 사유, 코호트(연령/지역/성별)
- ``## 2. 정성 인사이트``: 공통 반응, 인사이트 5-10개, 코호트 차이
- ``## 3. 제외 record 요약``: status별 인원과 비율
- ``## 4. 한계와 출처``: CC BY 4.0과 합성 데이터 한계 명시

정성 인사이트 LLM 호출이 실패하면 정량만 채우고 정성 섹션은 안내 문구로
대체한다(PRD §6.4, UI §4.3과 대비). v1은 모델 변경 없이도 사용 가능한 안전망을
우선한다.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ._json_utils import extract_json_object
from .config import AppConfig
from .llm_client import MlxLLMClient
from .models import (
    ConfigError,
    EmptyValidRecordsError,
    InterviewRecord,
    PersonaMeta,
    RetryExhaustedError,
    ServerNotReachableError,
    StructuredSummary,
)


logger = logging.getLogger(__name__)


# 코호트 셀 표본 부족 임계값. PRD §5.6, TDD §3.7.
_MIN_COHORT_CELL = 3

# 가격 히스토그램 구간 수. UI §4.2.2.
_PRICE_HIST_BINS = 10

# 텍스트 막대 차트 폭. UI §4.2.1, §4.2.2의 시각 일관성을 위한 고정값.
_BAR_CHART_WIDTH = 30

# 17개 시도 짧은 표기. 코호트 그룹 정렬 키로 사용한다.
_PROVINCE_ORDER: tuple = (
    "서울",
    "부산",
    "대구",
    "인천",
    "광주",
    "대전",
    "울산",
    "세종",
    "경기",
    "강원",
    "충청북",
    "충청남",
    "전북",
    "전남",
    "경상북",
    "경상남",
    "제주",
)


# ---------------------------------------------------------------------------
# 옵션과 결과 컨테이너
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportOptions:
    """``generate_report`` 옵션(TDD §3.7)."""

    top_n: int = 10
    include_drift: bool = False
    output_dir: Optional[Path] = None


@dataclass(frozen=True)
class IntentDistribution:
    """의향 카테고리 인원과 비율."""

    counts: dict
    ratios: dict
    total: int


@dataclass(frozen=True)
class PriceStats:
    """가격 수용가 통계(KRW)."""

    median: Optional[float]
    p25: Optional[float]
    p75: Optional[float]
    minimum: Optional[int]
    maximum: Optional[int]
    null_count: int
    valid_count: int
    histogram: list  # [(low, high, count), ...]
    currency: str = "KRW"


@dataclass(frozen=True)
class CohortCell:
    """단일 코호트 셀의 의향률 분포."""

    label: str
    sample: int
    ratios: dict  # {"positive": 0.5, "neutral": 0.2, "negative": 0.3} 또는 빈 dict
    masked: bool


@dataclass(frozen=True)
class CohortStats:
    """3축 코호트 의향률 묶음."""

    by_age: list  # list[CohortCell]
    by_region: list
    by_gender: list


@dataclass(frozen=True)
class QuantStats:
    """정량 집계 결과 컨테이너."""

    total_records: int
    valid_records: int
    intent: IntentDistribution
    price: PriceStats
    rejection_reasons: list  # [(reason, count)]
    cohort: CohortStats
    excluded_counts: dict  # {"failed": 1, "refused": 2, "drift": 0}
    excluded_total: int


@dataclass(frozen=True)
class QualitativeInsights:
    """정성 인사이트 결과 컨테이너."""

    common_reactions: list = field(default_factory=list)
    insights: list = field(default_factory=list)
    cohort_differences: str = ""
    fallback_message: str = ""  # LLM 호출/파싱 실패 시 안내 문구


# ---------------------------------------------------------------------------
# JSON 입력 로딩
# ---------------------------------------------------------------------------


def load_interview_json(path: Path) -> dict:
    """배치 결과 JSON을 dict로 읽는다.

    스키마 검증은 최소한만 수행한다(필수 키 ``meta``, ``records``). 자세한
    검증은 ``_records_from_payload``에서 수행한다.

    Args:
        path: 입력 JSON 경로.

    Returns:
        파싱된 dict.

    Raises:
        ConfigError: 파일 미존재, JSON 파싱 실패, 필수 키 누락.
    """

    if not path.exists():
        raise ConfigError(f"입력 파일을 찾을 수 없다: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"입력 파일을 읽을 수 없다: {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"입력 JSON 파싱 실패: {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"입력 JSON 최상위는 dict여야 한다: {type(data).__name__}"
        )
    if "meta" not in data or "records" not in data:
        raise ConfigError(
            "입력 JSON에 meta 또는 records 필드가 없다. "
            "본 도구의 interview 명령으로 생성된 JSON인지 확인해 주세요"
        )
    return data


def _records_from_payload(payload: dict) -> list:
    """JSON dict에서 record 리스트를 ``InterviewRecord`` dataclass로 복원한다.

    중첩 dataclass(``PersonaMeta``, ``StructuredSummary``, ``Flags``,
    ``RawResponse``, ``MessageEntry``)도 함께 복원한다. 검증은
    각 dataclass의 ``__post_init__``이 수행한다.
    """

    from .models import Flags, MessageEntry, RawResponse  # 지역 import.

    raw_records = payload.get("records", [])
    if not isinstance(raw_records, list):
        raise ConfigError("records는 list여야 한다")

    records: list = []
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        try:
            persona_raw = raw.get("persona_meta") or {}
            persona = PersonaMeta(
                persona_id=str(persona_raw.get("persona_id", raw.get("persona_id", ""))),
                name=persona_raw.get("name"),
                gender=str(persona_raw.get("gender", "여자")),
                age=int(persona_raw.get("age", 0)),
                region=str(persona_raw.get("region", "")),
                subregion=str(persona_raw.get("subregion", "")),
                occupation=str(persona_raw.get("occupation", "")),
                marital=str(persona_raw.get("marital", "")),
                education=str(persona_raw.get("education", "")),
                raw=dict(persona_raw.get("raw", {})),
                family_type=persona_raw.get("family_type"),
                housing_type=persona_raw.get("housing_type"),
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "persona_meta 복원 실패. record 건너뜀",
                extra={"reason": str(exc)},
            )
            continue

        summary_raw = raw.get("structured_summary")
        summary: Optional[StructuredSummary] = None
        if isinstance(summary_raw, dict):
            try:
                summary = StructuredSummary(
                    intent=str(summary_raw.get("intent", "neutral")),
                    willingness_to_pay=summary_raw.get("willingness_to_pay"),
                    willingness_to_pay_currency=str(
                        summary_raw.get("willingness_to_pay_currency", "KRW")
                    ),
                    rejection_reasons=list(summary_raw.get("rejection_reasons", [])),
                    one_line=str(summary_raw.get("one_line", "")),
                )
            except (TypeError, ValueError):
                summary = None

        flags_raw = raw.get("flags") or {}
        flags = Flags(
            persona_drift=bool(flags_raw.get("persona_drift", False)),
            auto_follow_up_used=bool(flags_raw.get("auto_follow_up_used", False)),
            refusal_detected=bool(flags_raw.get("refusal_detected", False)),
            truncated=bool(flags_raw.get("truncated", False)),
        )

        raw_responses_raw = raw.get("raw_responses") or []
        raw_responses: list = []
        for r in raw_responses_raw:
            if not isinstance(r, dict):
                continue
            raw_responses.append(
                RawResponse(
                    question_index=int(r.get("question_index", 0)),
                    response=str(r.get("response", "")),
                    latency_ms=int(r.get("latency_ms", 0)),
                    retry_count=int(r.get("retry_count", 0)),
                    reasoning_trace=r.get("reasoning_trace"),
                )
            )

        messages_raw = raw.get("messages") or []
        messages: list = []
        for m in messages_raw:
            if not isinstance(m, dict):
                continue
            try:
                messages.append(
                    MessageEntry(
                        role=str(m.get("role", "user")),
                        content=str(m.get("content", "")),
                    )
                )
            except ValueError:
                continue

        try:
            record = InterviewRecord(
                persona_id=str(raw.get("persona_id", persona.persona_id)),
                persona_meta=persona,
                started_at=str(raw.get("started_at", "")),
                finished_at=str(raw.get("finished_at", "")),
                status=str(raw.get("status", "completed")),
                messages=messages,
                raw_responses=raw_responses,
                structured_summary=summary,
                flags=flags,
                error=raw.get("error"),
            )
        except ValueError as exc:
            logger.warning(
                "InterviewRecord 복원 실패. record 건너뜀",
                extra={"reason": str(exc)},
            )
            continue
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# 정량 집계
# ---------------------------------------------------------------------------


def _filter_valid_records(
    records: list,
    *,
    include_drift: bool,
) -> list:
    """정량 집계 대상 record를 골라낸다.

    기본은 ``status="completed"``. ``include_drift=True``면 ``drift``도 포함한다
    (PRD §4.6, §5.6, TDD §3.7). ``failed``/``refused``는 항상 제외한다.
    """

    allowed = {"completed"}
    if include_drift:
        allowed.add("drift")
    return [r for r in records if r.status in allowed]


def compute_intent_distribution(records: list) -> IntentDistribution:
    """``structured_summary.intent``의 카테고리별 인원과 비율(PRD §5.6).

    ``structured_summary``가 없는 record는 ``unknown``으로 분류해 비율 계산에서
    제외한다(분모는 intent가 있는 record 수).
    """

    counts: Counter = Counter()
    valid = 0
    for r in records:
        summary = r.structured_summary
        if summary is None:
            continue
        intent = summary.intent
        counts[intent] += 1
        valid += 1

    if valid == 0:
        return IntentDistribution(counts={}, ratios={}, total=0)

    ratios = {k: counts[k] / valid for k in counts}
    return IntentDistribution(counts=dict(counts), ratios=ratios, total=valid)


def _percentile(values: list, q: float) -> Optional[float]:
    """0-100 범위 percentile. ``statistics.quantiles``가 없는 환경 대응 백업.

    Python 3.8+에서는 ``statistics.quantiles``를 사용 가능하지만 q=25/75 두
    값만 필요해 직접 구현한다. 빈 리스트면 None.
    """

    if not values:
        return None
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (q / 100.0) * (len(sorted_values) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = pos - lower
    return float(sorted_values[lower]) * (1 - weight) + float(sorted_values[upper]) * weight


def compute_price_stats(records: list) -> PriceStats:
    """가격 수용가 통계(중앙값/IQR/min/max/null/히스토그램)(PRD §5.6).

    ``willingness_to_pay``가 정수일 때만 집계에 포함한다. None 비율을 별도로
    보고한다.
    """

    valid_values: list = []
    null_count = 0
    currency = "KRW"
    for r in records:
        summary = r.structured_summary
        if summary is None:
            continue
        if summary.willingness_to_pay is None:
            null_count += 1
            continue
        try:
            valid_values.append(int(summary.willingness_to_pay))
        except (TypeError, ValueError):
            null_count += 1
            continue
        if summary.willingness_to_pay_currency:
            currency = summary.willingness_to_pay_currency

    if not valid_values:
        return PriceStats(
            median=None,
            p25=None,
            p75=None,
            minimum=None,
            maximum=None,
            null_count=null_count,
            valid_count=0,
            histogram=[],
            currency=currency,
        )

    median = float(statistics.median(valid_values))
    p25 = _percentile(valid_values, 25.0)
    p75 = _percentile(valid_values, 75.0)
    minimum = min(valid_values)
    maximum = max(valid_values)

    histogram = _build_histogram(valid_values, bins=_PRICE_HIST_BINS)

    return PriceStats(
        median=median,
        p25=p25,
        p75=p75,
        minimum=minimum,
        maximum=maximum,
        null_count=null_count,
        valid_count=len(valid_values),
        histogram=histogram,
        currency=currency,
    )


def _build_histogram(values: list, bins: int) -> list:
    """동일 폭 구간 히스토그램 ``[(low, high, count), ...]``을 만든다."""

    if not values or bins <= 0:
        return []
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        return [(minimum, maximum, len(values))]
    width = (maximum - minimum) / bins
    buckets = [0] * bins
    for v in values:
        idx = int((v - minimum) / width)
        if idx >= bins:
            idx = bins - 1
        buckets[idx] += 1
    out: list = []
    for i in range(bins):
        low = minimum + width * i
        high = minimum + width * (i + 1) if i < bins - 1 else maximum
        out.append((float(low), float(high), int(buckets[i])))
    return out


def compute_rejection_freq(records: list, top_n: int) -> list:
    """``rejection_reasons``를 펼쳐서 빈도 상위 ``top_n``개를 반환한다(PRD §5.6).

    빈도 동률 시 사전 순으로 정렬한다(UI §4.2.3).
    """

    if top_n <= 0:
        return []
    counts: Counter = Counter()
    for r in records:
        summary = r.structured_summary
        if summary is None:
            continue
        for reason in summary.rejection_reasons or []:
            text = str(reason).strip()
            if not text:
                continue
            counts[text] += 1
    items = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return items[:top_n]


def _age_bucket(age: int) -> str:
    """연령을 5개 버킷 중 하나로 매핑한다(PRD §5.6 코호트)."""

    if age < 30:
        return "20대"
    if age < 40:
        return "30대"
    if age < 50:
        return "40대"
    if age < 60:
        return "50대"
    return "60대 이상"


def _all_age_labels() -> tuple:
    return ("20대", "30대", "40대", "50대", "60대 이상")


def _bucket_intent_ratios(records: list) -> dict:
    """records 안 record들의 intent 비율 dict.

    ``structured_summary``가 없는 record는 분모에서 제외한다.
    """

    counts: Counter = Counter()
    valid = 0
    for r in records:
        if r.structured_summary is None:
            continue
        counts[r.structured_summary.intent] += 1
        valid += 1
    if valid == 0:
        return {}
    return {k: counts[k] / valid for k in counts}


def _build_cohort(
    records: list,
    *,
    key,
    labels: tuple,
    min_cell: int,
) -> list:
    """단일 축 코호트 셀 리스트를 만든다.

    Args:
        records: 정량 집계 대상 record 리스트.
        key: ``record -> str`` 매핑 함수(연령대, 시도, 성별 라벨 도출).
        labels: 셀 정렬 순서.
        min_cell: 표본 최소 인원. 미만이면 masked=True.
    """

    grouped: dict = {label: [] for label in labels}
    for r in records:
        try:
            label = key(r)
        except Exception:
            continue
        if label not in grouped:
            grouped[label] = []
        grouped[label].append(r)

    out: list = []
    for label in labels:
        bucket = grouped.get(label, [])
        sample = len(bucket)
        if sample < min_cell:
            out.append(
                CohortCell(label=label, sample=sample, ratios={}, masked=True)
            )
        else:
            out.append(
                CohortCell(
                    label=label,
                    sample=sample,
                    ratios=_bucket_intent_ratios(bucket),
                    masked=False,
                )
            )
    return out


def compute_cohort(records: list, *, min_cell: int = _MIN_COHORT_CELL) -> CohortStats:
    """3축 코호트 의향률 묶음을 계산한다(PRD §5.6, UI §4.2.4).

    표본 ``min_cell`` 미만 셀은 ``masked=True``로 둔다.
    """

    by_age = _build_cohort(
        records,
        key=lambda r: _age_bucket(r.persona_meta.age),
        labels=_all_age_labels(),
        min_cell=min_cell,
    )

    # 시도 라벨은 데이터셋 표기(짧은 17개)를 사용한다. 표본 0인 시도도
    # 표시할지 여부는 출력량 관점에서 표본 1 이상만 보여주고 싶지만, PRD
    # §5.6은 "셀별 표본 3명 미만 표본 부족 마스킹"으로 명시되어 0 셀도
    # 보존한다. 다만 0 셀은 너무 많아질 수 있어 데이터에 등장한 시도만 골라
    # 정렬 순서대로 출력한다.
    region_present = {r.persona_meta.region for r in records if r.persona_meta.region}
    region_labels = tuple(p for p in _PROVINCE_ORDER if p in region_present)
    # _PROVINCE_ORDER에 없는 표기(예: 데이터셋 갱신)도 노출.
    extra_regions = sorted(region_present - set(_PROVINCE_ORDER))
    region_labels = region_labels + tuple(extra_regions)

    by_region = _build_cohort(
        records,
        key=lambda r: r.persona_meta.region,
        labels=region_labels or ("",),
        min_cell=min_cell,
    )

    by_gender = _build_cohort(
        records,
        key=lambda r: r.persona_meta.gender,
        labels=("남자", "여자"),
        min_cell=min_cell,
    )

    return CohortStats(by_age=by_age, by_region=by_region, by_gender=by_gender)


def _excluded_counts(records: list, *, include_drift: bool) -> dict:
    """제외 record 사유별 인원 dict."""

    keys = ["failed", "refused"]
    if not include_drift:
        keys.append("drift")
    counts: Counter = Counter()
    for r in records:
        if r.status in keys:
            counts[r.status] += 1
    return {k: int(counts.get(k, 0)) for k in keys}


def compute_quant(
    records: list,
    *,
    top_n: int,
    include_drift: bool,
) -> QuantStats:
    """정량 집계 4종을 모두 수행해 ``QuantStats``로 반환한다."""

    valid = _filter_valid_records(records, include_drift=include_drift)
    excluded = _excluded_counts(records, include_drift=include_drift)
    excluded_total = sum(excluded.values())

    intent = compute_intent_distribution(valid)
    price = compute_price_stats(valid)
    rejection = compute_rejection_freq(valid, top_n)
    cohort = compute_cohort(valid, min_cell=_MIN_COHORT_CELL)

    return QuantStats(
        total_records=len(records),
        valid_records=len(valid),
        intent=intent,
        price=price,
        rejection_reasons=rejection,
        cohort=cohort,
        excluded_counts=excluded,
        excluded_total=excluded_total,
    )


# ---------------------------------------------------------------------------
# 정성 인사이트(LLM)
# ---------------------------------------------------------------------------


_INSIGHT_SCHEMA_HINT = (
    "{\n"
    '  "common_reactions": ["문장1", ...],\n'
    '  "insights": ["문장1", ...],\n'
    '  "cohort_differences": "한 단락 자유 서술"\n'
    "}"
)


def _format_intent_for_llm(intent: IntentDistribution) -> str:
    if intent.total == 0:
        return "(intent 데이터 없음)"
    parts = [
        f"{label} {intent.counts.get(label, 0)}명 ({intent.ratios.get(label, 0.0) * 100:.1f}%)"
        for label in ("positive", "neutral", "negative")
        if label in intent.counts
    ]
    return ", ".join(parts) if parts else "(intent 데이터 없음)"


def _format_price_for_llm(price: PriceStats) -> str:
    if price.valid_count == 0:
        return "(가격 데이터 없음)"
    return (
        f"중앙값 {int(price.median):,}, "
        f"25퍼센타일 {int(price.p25 or 0):,}, 75퍼센타일 {int(price.p75 or 0):,}, "
        f"최소 {price.minimum:,}, 최대 {price.maximum:,}, "
        f"null {price.null_count}건"
    )


def _format_rejection_for_llm(rejection: list) -> str:
    if not rejection:
        return "(거절 사유 없음)"
    return "; ".join(f"{r}({c})" for r, c in rejection[:10])


def _sample_records_for_llm(records: list, *, limit: int = 8) -> list:
    """LLM 입력용 record 샘플(요약과 한 줄 응답).

    토큰 절약을 위해 최대 ``limit``명만 골라 핵심 필드만 직렬화한다.
    """

    out: list = []
    for r in records[:limit]:
        summary = r.structured_summary
        intent = summary.intent if summary else "unknown"
        wtp = summary.willingness_to_pay if summary else None
        one_line = summary.one_line if summary else ""
        out.append(
            {
                "persona_id": r.persona_id,
                "age": r.persona_meta.age,
                "gender": r.persona_meta.gender,
                "region": r.persona_meta.region,
                "intent": intent,
                "willingness_to_pay": wtp,
                "one_line": one_line[:100],
            }
        )
    return out


def _build_insight_messages(
    records: list,
    quant: QuantStats,
    *,
    product: str,
) -> list:
    """정성 인사이트 LLM 호출용 messages."""

    valid = [r for r in records if r.status == "completed" or r.status == "drift"]
    samples = _sample_records_for_llm(valid)
    samples_json = json.dumps(samples, ensure_ascii=False, indent=2)

    rejection_text = _format_rejection_for_llm(quant.rejection_reasons)
    intent_text = _format_intent_for_llm(quant.intent)
    price_text = _format_price_for_llm(quant.price)

    system_prompt = (
        "당신은 인터뷰 분석가입니다. 정량 지표와 인터뷰 record 일부를 보고 "
        "사업 의사결정에 활용 가능한 인사이트를 도출하세요. 정해진 JSON 스키마로만 "
        "답변하고 추가 설명이나 마크다운 코드 펜스를 붙이지 마세요. JSON 외 텍스트가 "
        "포함되면 후처리 단계에서 파싱이 실패합니다.\n"
        "\n"
        "[출력 JSON 스키마]\n"
        f"{_INSIGHT_SCHEMA_HINT}\n"
        "\n"
        "[작성 규칙]\n"
        "- common_reactions: 5개 이내. 페르소나 다수가 비슷하게 보인 반응을 한 문장씩.\n"
        "- insights: 5-10개 강제. 각 항목은 시사점 한 문장 + 정량 근거 한 문장.\n"
        "- cohort_differences: 한 단락 자유 서술. 표본 3명 이상 코호트만 언급."
    )

    user_prompt = (
        f"[사업 아이템]\n{product}\n\n"
        f"[정량 요약]\n"
        f"- 의향률: {intent_text}\n"
        f"- 가격 수용가(KRW): {price_text}\n"
        f"- 거절 사유 상위: {rejection_text}\n"
        f"- 정량 대상 record 수: {quant.valid_records}명, 제외 record: {quant.excluded_total}명\n\n"
        f"[인터뷰 샘플(최대 8명)]\n"
        f"{samples_json}\n\n"
        "위 정보를 바탕으로 출력 JSON 스키마를 채워주세요."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _parse_insight_payload(text: str) -> QualitativeInsights:
    """LLM 응답을 ``QualitativeInsights``로 변환한다.

    JSON 파싱 실패 시 ``QualitativeInsights(fallback_message=...)``로 안전하게
    돌려준다(예외를 위로 던지지 않음). 정량 리포트는 그대로 채워야 한다.

    코드 펜스 제거와 가장 바깥 ``{ ... }`` 추출은 ``_json_utils.extract_json_object``
    가 일원화한다. 본 함수는 dict 추출에 성공한 뒤의 후처리(정성 필드 정규화,
    insights 5-10개 강제)만 담당한다.
    """

    if not text or not text.strip():
        return QualitativeInsights(
            fallback_message="정성 인사이트 응답이 비어 있어 본 섹션을 생성하지 못했습니다."
        )

    data = extract_json_object(text)
    if data is None:
        return QualitativeInsights(
            fallback_message=(
                "정성 인사이트 응답에서 JSON 객체를 찾지 못해 본 섹션을 생성하지 못했습니다."
            )
        )

    common = data.get("common_reactions") or []
    if not isinstance(common, list):
        common = []
    insights = data.get("insights") or []
    if not isinstance(insights, list):
        insights = []
    cohort_differences = data.get("cohort_differences") or ""
    if not isinstance(cohort_differences, str):
        cohort_differences = str(cohort_differences)

    # PRD/UI 요구: insights는 5-10개 강제. 범위 밖이면 잘라낸다(상한 10)
    # 하한 5 미만은 LLM이 실패한 사례라 fallback_message만 추가한다.
    insights_clean = [str(s).strip() for s in insights if str(s).strip()]
    common_clean = [str(s).strip() for s in common if str(s).strip()][:5]

    fallback = ""
    if len(insights_clean) > 10:
        insights_clean = insights_clean[:10]
    if len(insights_clean) < 5:
        fallback = (
            "LLM이 인사이트를 5개 미만으로 생성했습니다. "
            "모델 변경 또는 프롬프트 강화를 검토해 주세요."
        )

    return QualitativeInsights(
        common_reactions=common_clean,
        insights=insights_clean,
        cohort_differences=cohort_differences.strip(),
        fallback_message=fallback,
    )


async def generate_qualitative_insights(
    records: list,
    quant: QuantStats,
    llm: MlxLLMClient,
    config: AppConfig,
    *,
    product: str,
) -> QualitativeInsights:
    """정성 인사이트 LLM 호출 1회.

    실패(연결 실패, 타임아웃, 파싱 실패) 시 ``fallback_message``만 채운
    ``QualitativeInsights``를 반환한다(정량 리포트는 그대로 채워야 한다).
    """

    if quant.valid_records == 0:
        return QualitativeInsights(
            fallback_message="정량 집계 대상 record가 없어 정성 인사이트를 생성하지 않았습니다."
        )

    messages = _build_insight_messages(records, quant, product=product)
    logger.info(
        "정성 인사이트 LLM 호출 시작",
        extra={
            "valid_records": quant.valid_records,
            "samples_in_prompt": min(8, quant.valid_records),
        },
    )

    try:
        chat_response = await llm.chat(
            messages,
            max_tokens=min(900, config.llm.max_tokens * 2),
            temperature=0.4,
        )
    except (RetryExhaustedError, ServerNotReachableError, ConfigError) as exc:
        logger.warning(
            "정성 인사이트 LLM 호출 실패. 정량만 채워서 진행",
            extra={"reason": str(exc)},
        )
        return QualitativeInsights(
            fallback_message=(
                "정성 인사이트 LLM 호출에 실패해 본 섹션을 생성하지 못했습니다. "
                f"사유: {exc}"
            )
        )

    insights = _parse_insight_payload(chat_response.content)
    logger.info(
        "정성 인사이트 LLM 호출 완료",
        extra={
            "common_reactions": len(insights.common_reactions),
            "insights": len(insights.insights),
            "fallback": bool(insights.fallback_message),
        },
    )
    return insights


# ---------------------------------------------------------------------------
# 마크다운 렌더링
# ---------------------------------------------------------------------------


def _format_ratio(value: float) -> str:
    return f"{value * 100:.1f}%"


def _bar(value: float, *, width: int = _BAR_CHART_WIDTH) -> str:
    """0-1 비율을 블록 문자(▇)로 렌더링한다."""

    filled = max(0, min(width, int(round(value * width))))
    return "▇" * filled + " " * (width - filled)


def _format_price(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{int(value):,}"


def _render_intent_section(intent: IntentDistribution) -> str:
    if intent.total == 0:
        return "집계 가능한 의향 데이터가 없습니다.\n"

    rows = []
    for label in ("positive", "neutral", "negative"):
        count = intent.counts.get(label, 0)
        ratio = intent.ratios.get(label, 0.0)
        rows.append(f"| {label} | {count} | {_format_ratio(ratio)} |")
    table = "| 의향 | 인원 | 비율 |\n| --- | --- | --- |\n" + "\n".join(rows)

    bars = []
    for label in ("positive", "neutral", "negative"):
        ratio = intent.ratios.get(label, 0.0)
        label_pad = label.ljust(9)
        bars.append(f"{label_pad} {_bar(ratio)}  {_format_ratio(ratio)}")
    bar_block = "```\n" + "\n".join(bars) + "\n```"

    return (
        f"집계 대상: 정상 record {intent.total}명\n\n{table}\n\n{bar_block}\n"
    )


def _render_price_section(price: PriceStats) -> str:
    if price.valid_count == 0:
        if price.null_count > 0:
            return f"가격 응답이 모두 null입니다(전체 {price.null_count}건).\n"
        return "집계 가능한 가격 데이터가 없습니다.\n"

    null_ratio = (
        price.null_count / (price.valid_count + price.null_count)
        if (price.valid_count + price.null_count) > 0
        else 0.0
    )

    table_rows = [
        f"| 중앙값 | {_format_price(price.median)} |",
        f"| 25퍼센타일 | {_format_price(price.p25)} |",
        f"| 75퍼센타일 | {_format_price(price.p75)} |",
        f"| 최소 | {_format_price(price.minimum)} |",
        f"| 최대 | {_format_price(price.maximum)} |",
        f"| null 비율 | {_format_ratio(null_ratio)}({price.null_count}/{price.valid_count + price.null_count}) |",
    ]
    table = "| 지표 | 값 |\n| --- | --- |\n" + "\n".join(table_rows)

    if not price.histogram:
        return f"통화: {price.currency}\n\n{table}\n"

    max_count = max(c for _, _, c in price.histogram) or 1
    hist_lines = []
    for low, high, count in price.histogram:
        ratio = count / max_count
        hist_lines.append(
            f"{int(low):>9,} - {int(high):>9,}  {_bar(ratio)}  {count}명"
        )
    hist_block = "```\n" + "\n".join(hist_lines) + "\n```"

    return f"통화: {price.currency}\n\n{table}\n\n{hist_block}\n"


def _render_rejection_section(rejection: list, top_n: int) -> str:
    if not rejection:
        return "거절 사유 데이터가 없습니다.\n"
    rows = []
    for rank, (reason, count) in enumerate(rejection, start=1):
        rows.append(f"| {rank} | {reason} | {count} |")
    table = "| 순위 | 사유 | 빈도 |\n| --- | --- | --- |\n" + "\n".join(rows)
    return f"상위 {min(top_n, len(rejection))}개\n\n{table}\n"


def _render_cohort_axis(
    title_level: int,
    title: str,
    cells: list,
    axis_label: str,
) -> str:
    """단일 축 코호트 테이블을 렌더링한다."""

    header = "#" * title_level + " " + title
    if not cells:
        return f"{header}\n\n표시할 셀이 없습니다.\n"

    rows = []
    for cell in cells:
        if cell.masked:
            rows.append(
                f"| {cell.label} | {cell.sample} | 표본 부족 | 표본 부족 | 표본 부족 |"
            )
        else:
            pos = _format_ratio(cell.ratios.get("positive", 0.0))
            neu = _format_ratio(cell.ratios.get("neutral", 0.0))
            neg = _format_ratio(cell.ratios.get("negative", 0.0))
            rows.append(
                f"| {cell.label} | {cell.sample} | {pos} | {neu} | {neg} |"
            )

    table = (
        f"| {axis_label} | 표본 | positive | neutral | negative |\n"
        "| --- | --- | --- | --- | --- |\n" + "\n".join(rows)
    )
    return f"{header}\n\n{table}\n"


def _render_cohort_section(cohort: CohortStats) -> str:
    intro = (
        "셀별 표본 수가 작아 차이는 참고용입니다. "
        f"표본 {_MIN_COHORT_CELL}명 미만 셀은 \"표본 부족\"으로 마스킹합니다.\n"
    )
    parts = [intro]
    parts.append(
        _render_cohort_axis(4, "1.4.1. 연령대별", cohort.by_age, "연령대")
    )
    parts.append(
        _render_cohort_axis(4, "1.4.2. 지역별", cohort.by_region, "지역")
    )
    parts.append(
        _render_cohort_axis(4, "1.4.3. 성별", cohort.by_gender, "성별")
    )
    return "\n".join(parts)


def _render_insights_section(insights: QualitativeInsights) -> str:
    parts = []

    if insights.common_reactions:
        items = "\n".join(f"- {s}" for s in insights.common_reactions)
        parts.append("### 2.1. 공통 반응\n\n" + items + "\n")
    else:
        parts.append("### 2.1. 공통 반응\n\n공통 반응이 추출되지 않았습니다.\n")

    if insights.insights:
        items = "\n".join(
            f"{i + 1}. {s}" for i, s in enumerate(insights.insights)
        )
        parts.append("### 2.2. 인사이트\n\n" + items + "\n")
    else:
        parts.append("### 2.2. 인사이트\n\n인사이트가 추출되지 않았습니다.\n")

    cohort_text = insights.cohort_differences or "코호트 차이 자유 서술이 비어 있습니다."
    parts.append("### 2.3. 페르소나 군별 차이\n\n" + cohort_text + "\n")

    if insights.fallback_message:
        parts.append(
            "> 참고: " + insights.fallback_message + "\n"
        )
    return "\n".join(parts)


def _render_excluded_section(quant: QuantStats, *, include_drift: bool) -> str:
    total = quant.total_records
    rows = []
    for key in ("refused", "failed", "drift"):
        count = quant.excluded_counts.get(key, 0)
        ratio = (count / total) if total else 0.0
        label = {
            "refused": "refused(모델 응답 거부)",
            "failed": "failed(모든 retry 실패)",
            "drift": "drift(페르소나 깨짐)",
        }[key]
        rows.append(f"| {label} | {count} | {_format_ratio(ratio)} |")
    table = "| 사유 | 인원 | 비율 |\n| --- | --- | --- |\n" + "\n".join(rows)
    drift_note = (
        "정량 집계는 위 record를 제외한 record를 기준으로 합니다."
        if include_drift
        else "정량 집계는 위 record를 제외한 record를 기준으로 합니다. "
             "`--include-drift` 옵션을 적용하면 drift record도 정량 집계에 포함됩니다."
    )
    return table + "\n\n" + drift_note + "\n"


def _render_footer(model_id: str) -> str:
    return (
        "본 리포트는 합성 페르소나 데이터(`nvidia/Nemotron-Personas-Korea`)와 "
        "로컬 LLM 추론 결과를 결합하여 생성되었습니다. 합성 페르소나의 분포는 실제 "
        "인구 통계 분포와 일치하지 않을 수 있고, 응답은 모델의 추론 결과이므로 실제 "
        "한국인 응답자의 의견을 대체하지 않습니다. 본 도구는 실제 인터뷰 직전 단계의 "
        "가설 검증과 질문지 점검 용도로 사용하시기 바랍니다.\n\n"
        "- 데이터셋 출처: nvidia/Nemotron-Personas-Korea(https://huggingface.co/datasets/nvidia/Nemotron-Personas-Korea)\n"
        "- 데이터셋 라이선스: CC BY 4.0\n"
        f"- 추론 모델: {model_id}(로컬 MLX 서버)\n"
    )


def render_markdown(
    *,
    quant: QuantStats,
    insights: QualitativeInsights,
    meta: dict,
    records_summary: dict,
    json_path: Path,
    include_drift: bool,
    top_n: int,
) -> str:
    """마크다운 문자열을 만든다(UI §4.6 트리)."""

    product = str(meta.get("product", ""))
    model = str(meta.get("model", "(unknown)"))
    seed = meta.get("seed", "-")
    started_at = str(meta.get("started_at", ""))

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    requested = records_summary.get("requested", quant.total_records)
    completed = records_summary.get("completed", 0)
    refused = records_summary.get("refused", 0)
    failed = records_summary.get("failed", 0)
    drift = records_summary.get("drift", 0)

    title = f"# 가상 인터뷰 리포트: {product}".rstrip()

    header_table = (
        "| 항목 | 값 |\n"
        "| --- | --- |\n"
        f"| 생성 시각 | {generated_at} |\n"
        f"| 입력 JSON | {json_path} |\n"
        f"| 모델 | {model} |\n"
        f"| 시드 | {seed} |\n"
        f"| 인터뷰 시작 시각 | {started_at} |\n"
        f"| 페르소나 | 요청 {requested}명, 완료 {completed}명, 거부 {refused}명, "
        f"실패 {failed}명, 드리프트 {drift}명 |\n"
        "| 데이터셋 | nvidia/Nemotron-Personas-Korea(CC BY 4.0) |\n"
    )

    intent_md = _render_intent_section(quant.intent)
    price_md = _render_price_section(quant.price)
    rejection_md = _render_rejection_section(quant.rejection_reasons, top_n)
    cohort_md = _render_cohort_section(quant.cohort)

    insights_md = _render_insights_section(insights)
    excluded_md = _render_excluded_section(quant, include_drift=include_drift)
    footer_md = _render_footer(model)

    parts = [
        title,
        "",
        header_table,
        "## 1. 정량 지표",
        "",
        f"집계 대상 record: {quant.valid_records}명 / 전체 {quant.total_records}명 "
        + ("(drift 포함)" if include_drift else "(drift 제외)"),
        "",
        "### 1.1. 의향률",
        "",
        intent_md,
        "### 1.2. 가격 수용가",
        "",
        price_md,
        "### 1.3. 거절 사유 빈도",
        "",
        rejection_md,
        "### 1.4. 코호트별 의향률",
        "",
        cohort_md,
        "## 2. 정성 인사이트",
        "",
        insights_md,
        "## 3. 제외 record 요약",
        "",
        excluded_md,
        "## 4. 한계와 출처",
        "",
        footer_md,
    ]
    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def _records_summary(payload: dict, records: list) -> dict:
    """헤더 표용 record 통계.

    먼저 payload의 ``meta_extra.summary``를 사용하고, 없으면 record 리스트로
    재계산한다(SIGINT partial 저장본도 호환).
    """

    extra = payload.get("meta_extra") or {}
    summary = extra.get("summary") if isinstance(extra, dict) else None
    if isinstance(summary, dict):
        return {
            "requested": int(summary.get("requested", len(records))),
            "completed": int(summary.get("completed", 0)),
            "refused": int(summary.get("refused", 0)),
            "failed": int(summary.get("failed", 0)),
            "drift": int(summary.get("drift", 0)),
        }
    counts = Counter(r.status for r in records)
    return {
        "requested": len(records),
        "completed": int(counts.get("completed", 0)),
        "refused": int(counts.get("refused", 0)),
        "failed": int(counts.get("failed", 0)),
        "drift": int(counts.get("drift", 0)),
    }


def _resolve_output_path(json_path: Path, output_dir: Optional[Path]) -> Path:
    """리포트 출력 경로 ``report_{slug}_{ts}.md``를 만든다.

    입력 JSON 파일명(``interview_{slug}_{ts}.json``)에서 slug와 ts를 추출하고,
    실패 시 단순 치환으로 fallback한다.
    """

    name = json_path.stem  # 확장자 제외
    if name.startswith("interview_"):
        report_name = "report_" + name[len("interview_") :] + ".md"
    else:
        report_name = name + "_report.md"

    target_dir = output_dir if output_dir is not None else json_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / report_name


def _validate_records_for_report(
    records: list,
    *,
    include_drift: bool,
) -> None:
    """리포트 생성 가능한 정상 record가 있는지 검증한다.

    PRD §5.9: report 명령은 정상 record 0건이면 종료 코드 2. ``EmptyValidRecordsError``
    를 raise하면 main.py가 해당 예외를 종료 코드 2로 매핑한다.
    """

    valid = _filter_valid_records(records, include_drift=include_drift)
    if not valid:
        msg = (
            "리포트를 생성할 수 있는 정상 record가 없습니다. "
            "모델 동작과 필터를 점검한 뒤 인터뷰를 다시 실행해 주세요. "
            "--include-drift 옵션을 사용하면 드리프트 record를 정량 집계에 포함할 수 있습니다."
        )
        raise EmptyValidRecordsError(msg)


async def generate_report(
    json_path: Path,
    *,
    options: ReportOptions,
    llm: Optional[MlxLLMClient],
    config: AppConfig,
) -> Path:
    """배치 결과 JSON에서 마크다운 리포트를 만들고 파일로 저장한다.

    Args:
        json_path: 입력 JSON 경로(``interview_{slug}_{ts}.json``).
        options: 리포트 옵션.
        llm: 정성 인사이트 LLM 호출용 클라이언트. ``None``이면 정성 섹션은
            fallback 메시지만 채운다.
        config: AppConfig.

    Returns:
        저장된 마크다운 절대 경로.

    Raises:
        ConfigError: 입력 파일 미존재/스키마 불일치.
        EmptyValidRecordsError: 정상 record 0건.
    """

    payload = load_interview_json(json_path)
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        raise ConfigError("meta 필드는 dict여야 한다")

    records = _records_from_payload(payload)
    _validate_records_for_report(records, include_drift=options.include_drift)

    quant = compute_quant(
        records,
        top_n=options.top_n,
        include_drift=options.include_drift,
    )

    if llm is not None:
        insights = await generate_qualitative_insights(
            records,
            quant,
            llm,
            config,
            product=str(meta.get("product", "")),
        )
    else:
        insights = QualitativeInsights(
            fallback_message=(
                "LLM 클라이언트가 제공되지 않아 정성 인사이트를 생성하지 않았습니다."
            )
        )

    summary = _records_summary(payload, records)
    markdown_text = render_markdown(
        quant=quant,
        insights=insights,
        meta=meta,
        records_summary=summary,
        json_path=json_path,
        include_drift=options.include_drift,
        top_n=options.top_n,
    )

    output_path = _resolve_output_path(json_path, options.output_dir)
    output_path.write_text(markdown_text, encoding="utf-8")

    logger.info(
        "리포트 저장",
        extra={
            "input_json": str(json_path),
            "output_md": str(output_path),
            "valid_records": quant.valid_records,
            "include_drift": options.include_drift,
        },
    )
    return output_path
