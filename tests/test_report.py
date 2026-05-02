"""``src.report`` 단위/통합 테스트.

- 정량 집계: 의향률, 가격 수용가(중앙값/IQR), 거절 사유 빈도, 코호트 마스킹(<3)
- drift 제외 정책(기본 제외, ``--include-drift`` 포함)
- ``_records_from_payload``: 중첩 dataclass 복원
- ``render_markdown``: H2 4개 섹션, 헤더 메타, CC BY 4.0 + 엔비디아 출처 푸터
- ``generate_qualitative_insights``: LLM mock 정상, 실패 시 fallback_message
- ``generate_report`` E2E: JSON 입력 → 마크다운 출력
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.llm_client import MlxLLMClient
from src.models import (
    Flags,
    InterviewRecord,
    PersonaMeta,
    SCHEMA_VERSION,
    StructuredSummary,
)
from src.report import (
    EmptyValidRecordsError,
    QualitativeInsights,
    QuantStats,
    ReportOptions,
    _build_histogram,
    _filter_valid_records,
    _parse_insight_payload,
    _percentile,
    _records_from_payload,
    compute_cohort,
    compute_intent_distribution,
    compute_price_stats,
    compute_quant,
    compute_rejection_freq,
    generate_qualitative_insights,
    generate_report,
    load_interview_json,
    render_markdown,
)


# ---------------------------------------------------------------------------
# 헬퍼: record 빌더
# ---------------------------------------------------------------------------


def _persona(
    persona_id: str = "p",
    age: int = 27,
    gender: str = "여자",
    region: str = "서울",
) -> PersonaMeta:
    return PersonaMeta(
        persona_id=persona_id,
        name=None,
        gender=gender,
        age=age,
        region=region,
        subregion=f"{region}-X",
        occupation="x",
        marital="x",
        education="x",
        raw={},
    )


def _summary(
    intent: str = "positive",
    wtp: int | None = 30000,
    rejection_reasons: list = None,
) -> StructuredSummary:
    return StructuredSummary(
        intent=intent,
        willingness_to_pay=wtp,
        willingness_to_pay_currency="KRW",
        rejection_reasons=list(rejection_reasons or []),
        one_line="x",
    )


def _record(
    *,
    persona_id: str = "p",
    age: int = 27,
    gender: str = "여자",
    region: str = "서울",
    status: str = "completed",
    summary: StructuredSummary | None = None,
    flags: Flags | None = None,
) -> InterviewRecord:
    persona = _persona(persona_id, age=age, gender=gender, region=region)
    return InterviewRecord(
        persona_id=persona_id,
        persona_meta=persona,
        started_at="t1",
        finished_at="t2",
        status=status,
        messages=[],
        raw_responses=[],
        structured_summary=summary,
        flags=flags or Flags(),
        error=None,
    )


# ---------------------------------------------------------------------------
# _filter_valid_records / drift 제외 정책
# ---------------------------------------------------------------------------


def test_filter_valid_records_기본_completed만() -> None:
    records = [
        _record(persona_id="c", status="completed", summary=_summary()),
        _record(persona_id="d", status="drift", summary=_summary()),
        _record(persona_id="r", status="refused", summary=_summary()),
        _record(persona_id="f", status="failed"),
    ]
    valid = _filter_valid_records(records, include_drift=False)
    assert len(valid) == 1
    assert valid[0].persona_id == "c"


def test_filter_valid_records_include_drift_True() -> None:
    records = [
        _record(persona_id="c", status="completed", summary=_summary()),
        _record(persona_id="d", status="drift", summary=_summary()),
        _record(persona_id="r", status="refused", summary=_summary()),
    ]
    valid = _filter_valid_records(records, include_drift=True)
    ids = sorted(r.persona_id for r in valid)
    assert ids == ["c", "d"]


# ---------------------------------------------------------------------------
# compute_intent_distribution
# ---------------------------------------------------------------------------


def test_compute_intent_distribution_5명_정상() -> None:
    records = [
        _record(persona_id=f"p{i}", summary=_summary(intent="positive")) for i in range(3)
    ] + [
        _record(persona_id="n", summary=_summary(intent="neutral")),
        _record(persona_id="x", summary=_summary(intent="negative")),
    ]
    dist = compute_intent_distribution(records)
    assert dist.total == 5
    assert dist.counts["positive"] == 3
    assert abs(dist.ratios["positive"] - 0.6) < 1e-9


def test_compute_intent_distribution_summary_None_제외() -> None:
    records = [
        _record(persona_id="a", summary=_summary(intent="positive")),
        _record(persona_id="b", summary=None),
    ]
    dist = compute_intent_distribution(records)
    assert dist.total == 1


def test_compute_intent_distribution_빈_리스트() -> None:
    dist = compute_intent_distribution([])
    assert dist.total == 0
    assert dist.counts == {}
    assert dist.ratios == {}


# ---------------------------------------------------------------------------
# _percentile
# ---------------------------------------------------------------------------


def test_percentile_빈_리스트_None() -> None:
    assert _percentile([], 50) is None


def test_percentile_단일_값() -> None:
    assert _percentile([100], 50) == 100.0


def test_percentile_보간() -> None:
    values = [1, 2, 3, 4, 5]
    assert _percentile(values, 50.0) == 3.0
    assert abs(_percentile(values, 25.0) - 2.0) < 1e-9
    assert abs(_percentile(values, 75.0) - 4.0) < 1e-9


# ---------------------------------------------------------------------------
# compute_price_stats
# ---------------------------------------------------------------------------


def test_compute_price_stats_정상_5명() -> None:
    records = [
        _record(persona_id=f"p{i}", summary=_summary(wtp=v))
        for i, v in enumerate([10000, 20000, 30000, 40000, 50000])
    ]
    stats = compute_price_stats(records)
    assert stats.median == 30000.0
    assert stats.minimum == 10000
    assert stats.maximum == 50000
    assert stats.valid_count == 5
    assert stats.null_count == 0
    assert len(stats.histogram) > 0


def test_compute_price_stats_null_비율() -> None:
    records = [
        _record(persona_id="a", summary=_summary(wtp=10000)),
        _record(persona_id="b", summary=_summary(wtp=None)),
        _record(persona_id="c", summary=_summary(wtp=None)),
    ]
    stats = compute_price_stats(records)
    assert stats.valid_count == 1
    assert stats.null_count == 2


def test_compute_price_stats_summary_None_제외() -> None:
    records = [_record(persona_id="x", summary=None)]
    stats = compute_price_stats(records)
    assert stats.median is None
    assert stats.valid_count == 0


def test_build_histogram_동일_min_max_단일_구간() -> None:
    hist = _build_histogram([100, 100, 100], bins=10)
    assert len(hist) == 1
    assert hist[0][2] == 3


def test_build_histogram_정상_분포() -> None:
    values = list(range(0, 100))
    hist = _build_histogram(values, bins=10)
    assert len(hist) == 10
    assert sum(c for _, _, c in hist) == 100


# ---------------------------------------------------------------------------
# compute_rejection_freq
# ---------------------------------------------------------------------------


def test_compute_rejection_freq_상위_N_빈도순() -> None:
    records = [
        _record(persona_id="a", summary=_summary(rejection_reasons=["가격", "메뉴"])),
        _record(persona_id="b", summary=_summary(rejection_reasons=["가격"])),
        _record(persona_id="c", summary=_summary(rejection_reasons=["배송", "가격"])),
        _record(persona_id="d", summary=_summary(rejection_reasons=["메뉴"])),
    ]
    freq = compute_rejection_freq(records, top_n=10)
    # 가격 3, 메뉴 2, 배송 1
    assert freq[0] == ("가격", 3)
    assert freq[1] == ("메뉴", 2)
    assert freq[2] == ("배송", 1)


def test_compute_rejection_freq_빈도_동률_사전순() -> None:
    records = [
        _record(persona_id="a", summary=_summary(rejection_reasons=["가나", "다라"])),
    ]
    freq = compute_rejection_freq(records, top_n=10)
    # 사전 순(한국어): 가나 < 다라
    assert freq[0][0] == "가나"
    assert freq[1][0] == "다라"


def test_compute_rejection_freq_top_n_제한() -> None:
    records = [
        _record(
            persona_id="a",
            summary=_summary(rejection_reasons=["a", "b", "c", "d", "e"]),
        ),
    ]
    freq = compute_rejection_freq(records, top_n=2)
    assert len(freq) == 2


def test_compute_rejection_freq_top_n_0() -> None:
    records = [_record(summary=_summary(rejection_reasons=["a"]))]
    assert compute_rejection_freq(records, top_n=0) == []


# ---------------------------------------------------------------------------
# compute_cohort: 코호트 마스킹 임계값 3
# ---------------------------------------------------------------------------


def test_compute_cohort_표본_3미만_masked() -> None:
    """20대 1명, 30대 5명 → 20대 셀은 표본 부족 마스킹."""

    records = (
        [_record(persona_id=f"a{i}", age=27, summary=_summary()) for i in range(1)]
        + [
            _record(persona_id=f"b{i}", age=33, summary=_summary())
            for i in range(5)
        ]
    )
    cohort = compute_cohort(records, min_cell=3)
    age_cells = {c.label: c for c in cohort.by_age}
    assert age_cells["20대"].masked is True
    assert age_cells["30대"].masked is False


def test_compute_cohort_정확히_3명_masked_False() -> None:
    """경계값: 표본 3명이면 masked=False."""

    records = [_record(persona_id=f"p{i}", age=27, summary=_summary()) for i in range(3)]
    cohort = compute_cohort(records, min_cell=3)
    cell = next(c for c in cohort.by_age if c.label == "20대")
    assert cell.sample == 3
    assert cell.masked is False


def test_compute_cohort_지역축_데이터에_있는_시도만_노출() -> None:
    records = [_record(persona_id=f"p{i}", region="서울", summary=_summary()) for i in range(3)]
    cohort = compute_cohort(records, min_cell=3)
    region_labels = [c.label for c in cohort.by_region]
    assert "서울" in region_labels
    # 데이터에 없는 시도(부산)는 노출되지 않는다(또는 빈 셀)
    if "부산" in region_labels:
        bs_cell = next(c for c in cohort.by_region if c.label == "부산")
        assert bs_cell.sample == 0


def test_compute_cohort_성별_2축() -> None:
    records = [
        _record(persona_id=f"f{i}", gender="여자", summary=_summary()) for i in range(3)
    ] + [
        _record(persona_id=f"m{i}", gender="남자", summary=_summary()) for i in range(3)
    ]
    cohort = compute_cohort(records, min_cell=3)
    labels = sorted(c.label for c in cohort.by_gender)
    assert labels == ["남자", "여자"]


# ---------------------------------------------------------------------------
# compute_quant 종합
# ---------------------------------------------------------------------------


def test_compute_quant_종합_drift_제외() -> None:
    records = (
        [_record(persona_id=f"c{i}", summary=_summary()) for i in range(3)]
        + [_record(persona_id="d", status="drift", summary=_summary())]
        + [_record(persona_id="r", status="refused", summary=_summary())]
        + [_record(persona_id="f", status="failed")]
    )
    quant = compute_quant(records, top_n=5, include_drift=False)
    assert quant.total_records == 6
    assert quant.valid_records == 3
    assert quant.excluded_total == 3  # drift + refused + failed
    assert quant.excluded_counts == {"failed": 1, "refused": 1, "drift": 1}


def test_compute_quant_include_drift_True() -> None:
    records = [
        _record(persona_id="c", summary=_summary()),
        _record(persona_id="d", status="drift", summary=_summary()),
        _record(persona_id="r", status="refused", summary=_summary()),
    ]
    quant = compute_quant(records, top_n=5, include_drift=True)
    assert quant.valid_records == 2
    # drift는 제외 list에서 빠진다(refused, failed만)
    assert "drift" not in quant.excluded_counts


# ---------------------------------------------------------------------------
# load_interview_json / _records_from_payload
# ---------------------------------------------------------------------------


def _make_payload(records: list = None) -> dict:
    return {
        "meta": {
            "interview_id": "iv-1",
            "slug": "korea-persona-interview",
            "schema_version": SCHEMA_VERSION,
            "product": "반찬",
            "questions": ["Q1"],
            "follow_up_questions": [],
            "model": "test-model",
            "seed": 42,
            "started_at": "t1",
            "finished_at": "t2",
            "config_snapshot": {},
        },
        "records": records or [],
    }


def test_load_interview_json_정상(tmp_path: Path) -> None:
    payload = _make_payload()
    path = tmp_path / "x.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    data = load_interview_json(path)
    assert data["meta"]["slug"] == "korea-persona-interview"


def test_load_interview_json_파일_없음_ConfigError(tmp_path: Path) -> None:
    from src.models import ConfigError

    with pytest.raises(ConfigError):
        load_interview_json(tmp_path / "missing.json")


def test_load_interview_json_파싱_실패_ConfigError(tmp_path: Path) -> None:
    from src.models import ConfigError

    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_interview_json(path)


def test_load_interview_json_meta_누락_ConfigError(tmp_path: Path) -> None:
    from src.models import ConfigError

    path = tmp_path / "no_meta.json"
    path.write_text(json.dumps({"records": []}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_interview_json(path)


def test_records_from_payload_중첩_dataclass_복원() -> None:
    record = _record(
        persona_id="r1",
        age=30,
        summary=_summary(intent="neutral", wtp=15000, rejection_reasons=["가격"]),
        flags=Flags(persona_drift=False, auto_follow_up_used=True),
    )
    import dataclasses

    payload = _make_payload(records=[dataclasses.asdict(record)])
    restored = _records_from_payload(payload)
    assert len(restored) == 1
    r = restored[0]
    assert r.persona_id == "r1"
    assert r.persona_meta.age == 30
    assert r.structured_summary.intent == "neutral"
    assert r.flags.auto_follow_up_used is True


def test_records_from_payload_summary_None_허용() -> None:
    record = _record(persona_id="r1", summary=None)
    import dataclasses

    payload = _make_payload(records=[dataclasses.asdict(record)])
    restored = _records_from_payload(payload)
    assert restored[0].structured_summary is None


def test_records_from_payload_records_list_아님_ConfigError() -> None:
    from src.models import ConfigError

    with pytest.raises(ConfigError):
        _records_from_payload({"records": "not-a-list"})


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


def _make_quant_for_render() -> QuantStats:
    records = [
        _record(persona_id=f"p{i}", summary=_summary(intent="positive", wtp=30000))
        for i in range(3)
    ]
    return compute_quant(records, top_n=10, include_drift=False)


def test_render_markdown_H2_4개_섹션() -> None:
    quant = _make_quant_for_render()
    insights = QualitativeInsights(
        common_reactions=["반응1"],
        insights=[f"인사이트 {i}" for i in range(5)],
        cohort_differences="자유 서술",
    )
    md = render_markdown(
        quant=quant,
        insights=insights,
        meta={"product": "반찬", "model": "test", "seed": 42, "started_at": "t1"},
        records_summary={"requested": 3, "completed": 3, "refused": 0, "failed": 0, "drift": 0},
        json_path=Path("input.json"),
        include_drift=False,
        top_n=10,
    )
    # H2 4개 섹션 확인
    assert "## 1. 정량 지표" in md
    assert "## 2. 정성 인사이트" in md
    assert "## 3. 제외 record 요약" in md
    assert "## 4. 한계와 출처" in md


def test_render_markdown_H1_제목과_사업아이템() -> None:
    quant = _make_quant_for_render()
    insights = QualitativeInsights()
    md = render_markdown(
        quant=quant,
        insights=insights,
        meta={"product": "반찬 정기배송", "model": "x", "seed": 0, "started_at": "t"},
        records_summary={"requested": 3, "completed": 3, "refused": 0, "failed": 0, "drift": 0},
        json_path=Path("x.json"),
        include_drift=False,
        top_n=10,
    )
    assert md.startswith("# 가상 인터뷰 리포트: 반찬 정기배송")


def test_render_markdown_데이터셋_라이선스_푸터() -> None:
    quant = _make_quant_for_render()
    insights = QualitativeInsights()
    md = render_markdown(
        quant=quant,
        insights=insights,
        meta={"product": "x", "model": "test-model", "seed": 0, "started_at": "t"},
        records_summary={"requested": 3, "completed": 3, "refused": 0, "failed": 0, "drift": 0},
        json_path=Path("x.json"),
        include_drift=False,
        top_n=10,
    )
    # CC BY 4.0 + 엔비디아 출처 명시
    assert "CC BY 4.0" in md
    assert "nvidia/Nemotron-Personas-Korea" in md
    assert "test-model" in md  # 모델 ID


def test_render_markdown_drift_포함_안내() -> None:
    quant = _make_quant_for_render()
    insights = QualitativeInsights()
    md_excluded = render_markdown(
        quant=quant,
        insights=insights,
        meta={"product": "x", "model": "x", "seed": 0, "started_at": "t"},
        records_summary={"requested": 3, "completed": 3, "refused": 0, "failed": 0, "drift": 0},
        json_path=Path("x.json"),
        include_drift=False,
        top_n=10,
    )
    assert "drift 제외" in md_excluded

    md_included = render_markdown(
        quant=quant,
        insights=insights,
        meta={"product": "x", "model": "x", "seed": 0, "started_at": "t"},
        records_summary={"requested": 3, "completed": 3, "refused": 0, "failed": 0, "drift": 0},
        json_path=Path("x.json"),
        include_drift=True,
        top_n=10,
    )
    assert "drift 포함" in md_included


# ---------------------------------------------------------------------------
# _parse_insight_payload
# ---------------------------------------------------------------------------


def test_parse_insight_payload_정상() -> None:
    text = json.dumps(
        {
            "common_reactions": ["반응 1", "반응 2"],
            "insights": [f"인사이트 {i}" for i in range(7)],
            "cohort_differences": "코호트 자유 서술",
        },
        ensure_ascii=False,
    )
    insights = _parse_insight_payload(text)
    assert len(insights.common_reactions) == 2
    assert len(insights.insights) == 7
    assert insights.cohort_differences == "코호트 자유 서술"
    assert insights.fallback_message == ""


def test_parse_insight_payload_5개_미만_fallback() -> None:
    text = json.dumps(
        {
            "common_reactions": [],
            "insights": ["하나뿐"],
            "cohort_differences": "",
        },
        ensure_ascii=False,
    )
    insights = _parse_insight_payload(text)
    assert insights.fallback_message  # 비어있지 않음


def test_parse_insight_payload_10개_초과_잘림() -> None:
    text = json.dumps(
        {
            "common_reactions": [],
            "insights": [f"i{i}" for i in range(15)],
            "cohort_differences": "",
        },
        ensure_ascii=False,
    )
    insights = _parse_insight_payload(text)
    assert len(insights.insights) == 10


def test_parse_insight_payload_빈_본문_fallback_message() -> None:
    insights = _parse_insight_payload("")
    assert insights.fallback_message  # 비어있지 않음


def test_parse_insight_payload_JSON_없음_fallback() -> None:
    insights = _parse_insight_payload("자유 서술만 있고 JSON 없음")
    assert "JSON" in insights.fallback_message


def test_parse_insight_payload_파싱_실패_fallback() -> None:
    insights = _parse_insight_payload("{invalid json")
    assert insights.fallback_message  # 비어있지 않음


# ---------------------------------------------------------------------------
# generate_qualitative_insights
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_qualitative_insights_정상(httpx_mock, make_app_config) -> None:
    text = json.dumps(
        {
            "common_reactions": ["반응 1", "반응 2"],
            "insights": [f"인사이트 {i}" for i in range(7)],
            "cohort_differences": "코호트 차이",
        },
        ensure_ascii=False,
    )
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": text}}]},
        status_code=200,
    )

    records = [_record(persona_id=f"p{i}", summary=_summary()) for i in range(3)]
    quant = compute_quant(records, top_n=10, include_drift=False)

    config = make_app_config()
    async with MlxLLMClient(config.llm) as client:
        insights = await generate_qualitative_insights(
            records, quant, client, config, product="반찬"
        )
    assert len(insights.insights) == 7


@pytest.mark.asyncio
async def test_generate_qualitative_insights_LLM_실패_fallback(
    httpx_mock, make_app_config
) -> None:
    """LLM이 retry 모두 실패해도 정성 섹션은 fallback_message로 채운다."""

    for _ in range(3):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/v1/chat/completions",
            status_code=500,
        )

    records = [_record(persona_id=f"p{i}", summary=_summary()) for i in range(3)]
    quant = compute_quant(records, top_n=10, include_drift=False)

    config = make_app_config(retry_max_attempts=3)
    async with MlxLLMClient(config.llm) as client:
        insights = await generate_qualitative_insights(
            records, quant, client, config, product="반찬"
        )
    assert insights.fallback_message  # 채워짐
    assert insights.insights == []


@pytest.mark.asyncio
async def test_generate_qualitative_insights_valid_0_fallback(
    make_app_config,
) -> None:
    """정량 대상 record가 0건이면 LLM을 호출하지 않고 fallback만 반환."""

    quant = QuantStats(
        total_records=0,
        valid_records=0,
        intent=compute_intent_distribution([]),
        price=compute_price_stats([]),
        rejection_reasons=[],
        cohort=compute_cohort([], min_cell=3),
        excluded_counts={},
        excluded_total=0,
    )
    config = make_app_config()
    # llm 호출 없음을 보장하기 위해 클라이언트는 만들되 chat을 등록하지 않는다.
    async with MlxLLMClient(config.llm) as client:
        insights = await generate_qualitative_insights(
            [], quant, client, config, product="x"
        )
    assert insights.fallback_message
    assert insights.insights == []


# ---------------------------------------------------------------------------
# generate_report E2E
# ---------------------------------------------------------------------------


def _build_full_payload(records: list) -> dict:
    import dataclasses

    return {
        "meta": {
            "interview_id": "iv-1",
            "slug": "korea-persona-interview",
            "schema_version": SCHEMA_VERSION,
            "product": "반찬",
            "questions": ["Q1"],
            "follow_up_questions": [],
            "model": "test-model",
            "seed": 42,
            "started_at": "t1",
            "finished_at": "t2",
            "config_snapshot": {},
        },
        "records": [dataclasses.asdict(r) for r in records],
    }


@pytest.mark.asyncio
async def test_generate_report_E2E_마크다운_저장(
    httpx_mock, make_app_config, tmp_path: Path
) -> None:
    # 정성 인사이트 LLM 응답 모킹
    text = json.dumps(
        {
            "common_reactions": ["반응 1"],
            "insights": [f"i{i}" for i in range(6)],
            "cohort_differences": "차이",
        },
        ensure_ascii=False,
    )
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": text}}]},
        status_code=200,
    )

    records = [
        _record(persona_id=f"p{i}", summary=_summary(intent="positive", wtp=30000))
        for i in range(3)
    ]
    json_path = tmp_path / "interview_korea-persona-interview_20260502_120000.json"
    json_path.write_text(
        json.dumps(_build_full_payload(records), ensure_ascii=False),
        encoding="utf-8",
    )

    config = make_app_config()
    options = ReportOptions(top_n=10, include_drift=False, output_dir=None)

    async with MlxLLMClient(config.llm) as client:
        report_path = await generate_report(
            json_path=json_path,
            options=options,
            llm=client,
            config=config,
        )

    assert report_path.exists()
    assert report_path.name == "report_korea-persona-interview_20260502_120000.md"
    md = report_path.read_text(encoding="utf-8")
    assert "## 1. 정량 지표" in md
    assert "CC BY 4.0" in md


@pytest.mark.asyncio
async def test_generate_report_정상_record_0건_EmptyValidRecordsError(
    make_app_config, tmp_path: Path
) -> None:
    records = [
        _record(persona_id="r", status="refused", summary=_summary()),
        _record(persona_id="f", status="failed"),
    ]
    json_path = tmp_path / "interview_x_t.json"
    json_path.write_text(
        json.dumps(_build_full_payload(records), ensure_ascii=False),
        encoding="utf-8",
    )
    config = make_app_config()
    options = ReportOptions(top_n=10, include_drift=False, output_dir=None)

    async with MlxLLMClient(config.llm) as client:
        with pytest.raises(EmptyValidRecordsError):
            await generate_report(
                json_path=json_path,
                options=options,
                llm=client,
                config=config,
            )


@pytest.mark.asyncio
async def test_generate_report_LLM_None_fallback_message_사용(
    make_app_config, tmp_path: Path
) -> None:
    """``llm=None``이어도 정량 리포트는 채워진다."""

    records = [
        _record(persona_id=f"p{i}", summary=_summary(intent="positive", wtp=30000))
        for i in range(3)
    ]
    json_path = tmp_path / "interview_x_t.json"
    json_path.write_text(
        json.dumps(_build_full_payload(records), ensure_ascii=False),
        encoding="utf-8",
    )
    config = make_app_config()
    options = ReportOptions(top_n=10, include_drift=False, output_dir=None)

    report_path = await generate_report(
        json_path=json_path,
        options=options,
        llm=None,
        config=config,
    )
    md = report_path.read_text(encoding="utf-8")
    assert "## 1. 정량 지표" in md
    # fallback 메시지가 정성 섹션에 표시된다
    assert "정성 인사이트" in md
