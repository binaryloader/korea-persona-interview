"""MCP orchestrator 모드 도구 dispatch 회귀.

본 모듈은 v1.2.0(ADR-005)에서 도입된 MCP orchestrator 모드의 도구 흐름을
검증한다. orchestrator 모드 도구는 server-side LLM을 호출하지 않으므로 모든
테스트는 LLM mock 없이 동작한다.

도구 흐름은 아래와 같다.

1. ``build_persona_prompt`` 또는 ``build_batch_prompts``로 시스템 프롬프트와
   페르소나 dict를 받음
2. 호스트 sub-agent가 받은 프롬프트로 자기 LLM을 호출(테스트는 호출 흉내)
3. 호스트가 record를 모아 ``aggregate_results``로 정량 집계 + 리포트 생성

helper 도구(detect_persona_drift, should_auto_follow_up,
parse_structured_summary, interview_record_schema)도 본 모듈에서 검증한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.mcp_server import dispatch_tool


def _pin_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(f"mcp:\n  mode: '{mode}'\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# orchestrator healthcheck
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_healthcheck_LLM_호출_없이_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """orchestrator 모드 healthcheck는 LLM 호출 없이 ok와 cwd만 돌려준다."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    result = await dispatch_tool("healthcheck", {})

    assert result["ok"] is True
    assert result["backend"] == "mcp_orchestrator"
    assert "cwd" in result
    assert "dataset_name" in result


# ---------------------------------------------------------------------------
# interview 도구는 orchestrator 모드에서 차단된다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_interview_도구_차단(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """orchestrator 모드에서는 interview 도구가 노출되지 않는다."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    result = await dispatch_tool(
        "interview",
        {"product": "테스트", "questions": ["쓸 의향?"]},
    )

    assert "error" in result
    assert result["error"]["code"] == "tool_unavailable_in_mode"
    assert result["backend"] == "mcp_orchestrator"


# ---------------------------------------------------------------------------
# build_persona_prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_persona_prompt_정상(
    fake_load_dataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    result = await dispatch_tool(
        "build_persona_prompt",
        {
            "product": "1인 가구용 반찬 정기배송",
            "questions": ["쓸 의향?", "월 얼마면?", "거절 사유?"],
            "filter": "age:20-29",
            "n": 1,
            "seed": 42,
        },
    )

    assert result["ok"] is True
    assert result["backend"] == "mcp_orchestrator"
    assert "system_prompt" in result
    assert "persona_meta" in result
    assert "questions" in result
    assert "schema_hint" in result
    assert "1인 가구용 반찬" in result["system_prompt"]
    assert result["persona_meta"]["persona_id"]


@pytest.mark.asyncio
async def test_build_persona_prompt_product_누락(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    result = await dispatch_tool(
        "build_persona_prompt",
        {"questions": ["쓸 의향?"]},
    )

    assert "error" in result
    assert result["error"]["code"] == "missing_argument"


# ---------------------------------------------------------------------------
# build_batch_prompts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_batch_prompts_정상(
    fake_load_dataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    result = await dispatch_tool(
        "build_batch_prompts",
        {
            "product": "테스트 상품",
            "questions": ["쓸 의향?"],
            "n": 3,
            "seed": 42,
        },
    )

    assert result["ok"] is True
    assert result["backend"] == "mcp_orchestrator"
    assert result["count"] >= 1
    assert isinstance(result["prompts"], list)
    for p in result["prompts"]:
        assert "persona_id" in p
        assert "system_prompt" in p
        assert "persona_meta" in p


@pytest.mark.asyncio
async def test_build_batch_prompts_n_없음_persona_ids도_없음_에러(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    result = await dispatch_tool(
        "build_batch_prompts",
        {"product": "테스트 상품", "questions": ["쓸 의향?"]},
    )

    assert "error" in result
    assert result["error"]["code"] == "missing_argument"


# ---------------------------------------------------------------------------
# aggregate_results
# ---------------------------------------------------------------------------


def _make_record(persona_id: str, age: int = 27, intent: str = "positive") -> dict:
    """aggregate_results 도구에 넘길 minimal record dict."""

    return {
        "persona_id": persona_id,
        "persona_meta": {
            "persona_id": persona_id,
            "name": None,
            "gender": "여자",
            "age": age,
            "region": "서울",
            "subregion": "서울-강남구",
            "occupation": "소프트웨어 엔지니어",
            "marital": "미혼",
            "education": "대학교",
            "family_type": "1인 가구",
            "housing_type": "원룸",
        },
        "started_at": "2026-05-02T12:00:00+00:00",
        "finished_at": "2026-05-02T12:00:30+00:00",
        "status": "completed",
        "messages": [
            {"role": "system", "content": "(시스템 프롬프트)"},
            {"role": "user", "content": "이 서비스 쓸 의향?"},
            {"role": "assistant", "content": "네, 가격이 합리적이라면 한 번 써보고 싶어요."},
        ],
        "raw_responses": [
            {
                "question_index": 0,
                "response": "네, 가격이 합리적이라면 한 번 써보고 싶어요.",
                "latency_ms": 1200,
                "retry_count": 0,
            }
        ],
        "structured_summary": {
            "intent": intent,
            "willingness_to_pay": 30000,
            "willingness_to_pay_currency": "KRW",
            "rejection_reasons": [],
            "one_line": "가격 합리적이면 시도 의향",
            "acceptable_price_signal": "fair",
        },
        "flags": {
            "persona_drift": False,
            "auto_follow_up_used": False,
            "refusal_detected": False,
            "truncated": False,
            "parse_failed": False,
        },
        "error": None,
    }


@pytest.mark.asyncio
async def test_aggregate_results_정상(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    output_dir = tmp_path / "outputs"
    records = [
        _make_record("p-0001", age=27, intent="positive"),
        _make_record("p-0002", age=34, intent="positive"),
        _make_record("p-0003", age=41, intent="neutral"),
    ]

    result = await dispatch_tool(
        "aggregate_results",
        {
            "records": records,
            "product": "테스트 상품",
            "output_dir": str(output_dir),
        },
    )

    assert result["ok"] is True, f"unexpected: {result}"
    assert result["backend"] == "mcp_orchestrator"
    assert result["valid_records"] == 3
    assert Path(result["output_path"]).exists()


@pytest.mark.asyncio
async def test_aggregate_results_빈_records_에러(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    result = await dispatch_tool("aggregate_results", {"records": []})

    assert "error" in result
    assert result["error"]["code"] == "missing_argument"


@pytest.mark.asyncio
async def test_aggregate_results_스키마_위반_에러(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status 값이 화이트리스트 외이면 스키마 검증으로 차단된다."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    bad_record = _make_record("p-0001")
    bad_record["status"] = "weird"

    result = await dispatch_tool(
        "aggregate_results",
        {"records": [bad_record]},
    )

    assert "error" in result
    assert result["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------------
# helper 도구: detect_persona_drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_persona_drift_helper_drift_없음(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    persona = _make_record("p-0001")["persona_meta"]
    result = await dispatch_tool(
        "detect_persona_drift",
        {
            "text": "네, 가격이 합리적이라면 한번 써보고 싶어요.",
            "persona_meta": persona,
        },
    )

    assert result["ok"] is True
    assert result["is_drift"] is False


@pytest.mark.asyncio
async def test_detect_persona_drift_helper_drift_감지(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """27세 여성 페르소나가 ``저는 50대 남자``라고 단언하면 drift."""

    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    persona = _make_record("p-0001")["persona_meta"]
    result = await dispatch_tool(
        "detect_persona_drift",
        {
            "text": "저는 50대 남자라서 그런 서비스에는 관심 없어요.",
            "persona_meta": persona,
        },
    )

    assert result["ok"] is True
    assert result["is_drift"] is True


@pytest.mark.asyncio
async def test_detect_persona_drift_helper_persona_meta_누락(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    result = await dispatch_tool(
        "detect_persona_drift",
        {"text": "네"},
    )

    assert "error" in result
    assert result["error"]["code"] == "missing_argument"


# ---------------------------------------------------------------------------
# helper 도구: should_auto_follow_up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_auto_follow_up_짧은_답변_트리거(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    result = await dispatch_tool(
        "should_auto_follow_up",
        {"text": "네"},
    )

    assert result["ok"] is True
    assert result["should_follow_up"] is True


@pytest.mark.asyncio
async def test_should_auto_follow_up_충분한_답변_트리거_안함(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    result = await dispatch_tool(
        "should_auto_follow_up",
        {
            "text": (
                "네, 가격이 합리적이라면 한번 써보고 싶어요. "
                "주 2회 배송이라는 점이 1인 가구에 잘 맞아 보입니다."
            )
        },
    )

    assert result["ok"] is True
    assert result["should_follow_up"] is False


# ---------------------------------------------------------------------------
# helper 도구: parse_structured_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_structured_summary_정상(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    raw = json.dumps(
        {
            "intent": "positive",
            "willingness_to_pay": 30000,
            "willingness_to_pay_currency": "KRW",
            "rejection_reasons": [],
            "one_line": "좋아 보입니다",
            "acceptable_price_signal": "fair",
        },
        ensure_ascii=False,
    )
    result = await dispatch_tool(
        "parse_structured_summary",
        {"raw_response": raw},
    )

    assert result["ok"] is True
    assert result["parse_failed"] is False
    summary = result["structured_summary"]
    assert summary["intent"] == "positive"
    assert summary["willingness_to_pay"] == 30000
    assert summary["acceptable_price_signal"] == "fair"


@pytest.mark.asyncio
async def test_parse_structured_summary_파싱_실패(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    result = await dispatch_tool(
        "parse_structured_summary",
        {"raw_response": "JSON이 아닌 텍스트"},
    )

    assert result["ok"] is True
    assert result["parse_failed"] is True
    assert result["structured_summary"] is None


# ---------------------------------------------------------------------------
# helper 도구: interview_record_schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interview_record_schema_정상(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "orchestrator")

    result = await dispatch_tool("interview_record_schema", {})

    assert result["ok"] is True
    assert "schema" in result
    assert "example" in result
    assert "completed" in result["schema"]["enums"]["status"]


# ---------------------------------------------------------------------------
# helper 도구는 server mode에서도 동일하게 노출된다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_도구_server_mode에서도_접근가능(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPI_OUTPUT_DIR", str(tmp_path))
    _pin_mode(tmp_path, monkeypatch, "server")

    result = await dispatch_tool("interview_record_schema", {})

    assert result["ok"] is True
    assert result["backend"] == "mcp_server"
