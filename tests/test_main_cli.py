"""``main.py`` click CLI 단위/통합 테스트.

- 4개 서브커맨드 ``--help`` 정상
- 종료 코드 매핑(0/1/2/3/130)
- 한국어 에러 메시지 포함 검증
- ``healthcheck``, ``list-personas``, ``interview``, ``report`` E2E
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import main as main_module
from main import MESSAGES, cli
from src.models import SCHEMA_VERSION


# ---------------------------------------------------------------------------
# --help 출력
# ---------------------------------------------------------------------------


def test_cli_root_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "korea-persona-interview" in result.output


@pytest.mark.parametrize(
    "subcommand",
    ["healthcheck", "list-personas", "interview", "report"],
)
def test_cli_subcommand_help(subcommand: str) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, [subcommand, "--help"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# healthcheck 명령
# ---------------------------------------------------------------------------


def test_healthcheck_정상_exit_0(httpx_mock, tmp_path: Path) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "gpt-4o-mini"}]},
        status_code=200,
    )

    runner = CliRunner()
    # output_dir(logs)을 tmp_path로 격리
    result = runner.invoke(
        cli,
        ["--no-color", "healthcheck"],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 0
    assert "[OK]" in result.output


def test_healthcheck_서버_다운_exit_1(httpx_mock, tmp_path: Path) -> None:
    import httpx

    httpx_mock.add_exception(
        httpx.ConnectError("connection refused"),
        url="https://api.openai.com/v1/models",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--no-color", "healthcheck"],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 1
    # 한국어 안내 메시지 포함
    assert "OpenAI 서버에 연결할 수 없습니다" in result.output


# ---------------------------------------------------------------------------
# list-personas 명령
# ---------------------------------------------------------------------------


def test_list_personas_정상_exit_0(
    fake_load_dataset, tmp_path: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--no-color", "list-personas", "--limit", "2", "--seed", "42"],
        env={"KPI_OUTPUT_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0
    # 헤더 행 출력
    assert "persona_id" in result.output


def test_list_personas_필터_0건_exit_2(
    fake_load_dataset, tmp_path: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--no-color",
            "list-personas",
            "--filter",
            "age:90-99",  # 데이터셋 fixture에 90대 없음
            "--limit",
            "1",
        ],
        env={"KPI_OUTPUT_DIR": str(tmp_path)},
    )
    assert result.exit_code == 2
    assert "필터" in result.output


def test_list_personas_잘못된_필터_DSL_exit_1(
    fake_load_dataset, tmp_path: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--no-color", "list-personas", "--filter", "wrong:value"],
        env={"KPI_OUTPUT_DIR": str(tmp_path)},
    )
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# interview 명령
# ---------------------------------------------------------------------------


def test_interview_표본_부족_exit_2(
    fake_load_dataset, tmp_path: Path
) -> None:
    """가짜 데이터셋은 5명. n=100 요청하면 표본 부족(exit 2)."""

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--no-color",
            "interview",
            "--product",
            "반찬",
            "--questions",
            "Q1",
            "--n",
            "100",
        ],
        env={"KPI_OUTPUT_DIR": str(tmp_path)},
    )
    assert result.exit_code == 2
    # 필터 결과 안내
    assert "필터" in result.output or "요청" in result.output


def test_interview_questions_없음_UsageError(
    fake_load_dataset, tmp_path: Path
) -> None:
    """``--questions`` 미지정 시 click의 UsageError(exit 2)로 차단된다."""

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--no-color", "interview", "--product", "반찬"],
        env={"KPI_OUTPUT_DIR": str(tmp_path)},
    )
    # click의 missing option은 exit 2로 처리된다
    assert result.exit_code == 2


def test_interview_헬스체크_실패_exit_1(
    httpx_mock, fake_load_dataset, tmp_path: Path
) -> None:
    """배치 시작 직전 헬스체크 5xx → ServerNotReachableError → exit 1."""

    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        status_code=503,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--no-color",
            "interview",
            "--product",
            "반찬",
            "--questions",
            "Q1",
            "--n",
            "1",
        ],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 1
    assert "OpenAI 서버에 연결할 수 없습니다" in result.output


# ---------------------------------------------------------------------------
# report 명령
# ---------------------------------------------------------------------------


def _write_full_payload(path: Path, records: list) -> None:
    import dataclasses

    payload = {
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
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_report_입력_파일_없음_exit_2(tmp_path: Path) -> None:
    """click이 ``exists=True``로 파일 미존재 시 UsageError(exit 2)를 발생시킨다."""

    runner = CliRunner()
    missing = tmp_path / "missing.json"
    result = runner.invoke(
        cli,
        ["--no-color", "report", str(missing)],
        env={"KPI_OUTPUT_DIR": str(tmp_path)},
    )
    # exists=True로 click이 차단하면 exit 2(UsageError) 발생
    assert result.exit_code == 2


def test_report_정상_record_0건_exit_2(
    httpx_mock, tmp_path: Path
) -> None:
    """모두 failed인 JSON에 대해 report → EmptyValidRecordsError → exit 2."""

    from src.models import Flags, InterviewRecord, PersonaMeta

    persona = PersonaMeta(
        persona_id="x",
        name=None,
        gender="여자",
        age=27,
        region="서울",
        subregion="서울-X",
        occupation="x",
        marital="x",
        education="x",
        raw={},
    )
    record = InterviewRecord(
        persona_id="x",
        persona_meta=persona,
        started_at="t1",
        finished_at="t2",
        status="failed",
        messages=[],
        raw_responses=[],
        structured_summary=None,
        flags=Flags(),
        error={"type": "x", "message": "y"},
    )

    json_path = tmp_path / "interview_x_t.json"
    _write_full_payload(json_path, records=[record])

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--no-color", "report", str(json_path)],
        env={"KPI_OUTPUT_DIR": str(tmp_path)},
    )
    assert result.exit_code == 2
    assert "정상 record가 없습니다" in result.output


def test_report_입력_JSON_스키마_불일치_exit_1(tmp_path: Path) -> None:
    """``records`` 키 누락 → ConfigError → exit 1."""

    json_path = tmp_path / "interview_x_t.json"
    json_path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--no-color", "report", str(json_path)],
        env={"KPI_OUTPUT_DIR": str(tmp_path)},
    )
    assert result.exit_code == 1
    assert "올바른 인터뷰 JSON 형식이 아닙니다" in result.output


# ---------------------------------------------------------------------------
# 한국어 메시지 사전 노출
# ---------------------------------------------------------------------------


def test_messages_사전_핵심_키_존재() -> None:
    """main.MESSAGES에 UI §3의 핵심 메시지 키가 모두 존재한다."""

    required = {
        "server_not_reachable",
        "config_error",
        "dataset_unavailable",
        "filter_zero",
        "filter_too_few",
        "input_file_not_found",
        "input_file_schema",
        "empty_valid_records",
        "user_interrupted",
        "partial_failure",
    }
    assert required.issubset(set(MESSAGES.keys()))


# ---------------------------------------------------------------------------
# interview 정상 경로 E2E
# ---------------------------------------------------------------------------


def _add_interview_chat_responses(httpx_mock, *, n: int) -> None:
    """``n``명에 대한 인터뷰 멀티턴 + 구조화 요약 응답을 mock에 등록한다.

    한 페르소나당 메인 질문 응답 1회 + 구조화 요약 응답 1회로 총 2회 호출이 발생한다.
    구조화 요약은 ``StructuredSummary`` JSON 스키마로 정상 응답한다.
    """

    for _ in range(n):
        httpx_mock.add_response(
            method="POST",
            url="https://api.openai.com/v1/chat/completions",
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "가격이 합리적이라 한번 시도해 볼 만한 것 같아요. 좋은 옵션입니다.",
                        }
                    }
                ]
            },
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://api.openai.com/v1/chat/completions",
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "intent": "positive",
                                    "willingness_to_pay": 30000,
                                    "willingness_to_pay_currency": "KRW",
                                    "rejection_reasons": [],
                                    "one_line": "긍정",
                                },
                                ensure_ascii=False,
                            ),
                        }
                    }
                ]
            },
            status_code=200,
        )


def _add_qualitative_insight_response(httpx_mock) -> None:
    """리포트 정성 인사이트 LLM 호출 mock 응답."""

    insight_text = json.dumps(
        {
            "common_reactions": ["반응 1"],
            "insights": [f"i{i}" for i in range(6)],
            "cohort_differences": "차이",
        },
        ensure_ascii=False,
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.openai.com/v1/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": insight_text}}]},
        status_code=200,
    )


def test_interview_정상_3명_completed_exit_0_no_report(
    httpx_mock,
    fake_load_dataset,
    tmp_path: Path,
) -> None:
    """``--no-report``: 3명 인터뷰가 정상 완료되고 JSON만 저장된다(MD 미생성)."""

    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "test-model"}]},
        status_code=200,
    )
    _add_interview_chat_responses(httpx_mock, n=3)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--no-color",
            "interview",
            "--product",
            "반찬 정기배송",
            "--questions",
            "Q1",
            "--n",
            "3",
            "--output",
            str(tmp_path),
            "--no-report",
        ],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 0, result.output
    assert "다음 단계" in result.output
    # JSON은 생성, MD는 미생성
    json_files = list(tmp_path.glob("interview_*.json"))
    md_files = list(tmp_path.glob("report_*.md"))
    assert json_files
    assert not md_files


def test_interview_정상_3명_completed_exit_0_auto_report(
    httpx_mock,
    fake_load_dataset,
    tmp_path: Path,
) -> None:
    """기본 동작(``--report``): 인터뷰 종료 후 리포트 마크다운까지 자동 생성한다."""

    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "test-model"}]},
        status_code=200,
    )
    _add_interview_chat_responses(httpx_mock, n=3)
    # 자동 리포트 정성 인사이트 호출 1회.
    _add_qualitative_insight_response(httpx_mock)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--no-color",
            "interview",
            "--product",
            "반찬 정기배송",
            "--questions",
            "Q1",
            "--n",
            "3",
            "--output",
            str(tmp_path),
        ],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 0, result.output
    # JSON과 MD 모두 생성
    json_files = list(tmp_path.glob("interview_*.json"))
    md_files = list(tmp_path.glob("report_*.md"))
    assert json_files
    assert md_files
    # INFO 안내 한 줄 노출
    assert "리포트 자동 생성 시작" in result.output


def _make_json_runner() -> CliRunner:
    """``--json`` 모드 테스트용 CliRunner. stdout과 stderr를 분리해 stdout에서만 결과 JSON을 검증한다.

    stderr에는 logging JSON Lines가 흘러 들어오므로 stdout만 파싱해야 한다. click 8.1의
    ``mix_stderr=False``로 분리한다(click 8.2에서는 기본 분리 정책으로 바뀐다).
    """

    return CliRunner(mix_stderr=False)


def _stdout_json(result) -> dict:
    """``CliRunner(mix_stderr=False)`` 결과의 stdout만 한 덩어리 JSON으로 파싱한다."""

    return json.loads(result.stdout.strip())


def test_json_mode_healthcheck_정상_stdout_JSON(httpx_mock, tmp_path: Path) -> None:
    """``--json`` 모드 healthcheck: stdout에 ``{"ok": true, ...}`` 한 줄."""

    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]},
        status_code=200,
    )

    runner = _make_json_runner()
    result = runner.invoke(
        cli,
        ["--json", "healthcheck"],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _stdout_json(result)
    assert payload["ok"] is True
    assert payload["model"] == "gpt-4o-mini"
    assert "gpt-4o-mini" in payload["models"]
    # 사람용 라벨이 stdout에 등장하지 않아야 한다.
    assert "[OK]" not in result.stdout
    assert "[INFO]" not in result.stdout


def test_json_mode_healthcheck_서버_다운_stdout_JSON_error(
    httpx_mock, tmp_path: Path
) -> None:
    import httpx

    httpx_mock.add_exception(
        httpx.ConnectError("connection refused"),
        url="https://api.openai.com/v1/models",
    )

    runner = _make_json_runner()
    result = runner.invoke(
        cli,
        ["--json", "healthcheck"],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 1
    payload = _stdout_json(result)
    assert payload["error"]["code"] == "server_not_reachable"
    assert payload["error"]["exit_code"] == 1


def test_json_mode_list_personas_정상_stdout_JSON(
    fake_load_dataset, tmp_path: Path
) -> None:
    runner = _make_json_runner()
    result = runner.invoke(
        cli,
        ["--json", "list-personas", "--limit", "2", "--seed", "42"],
        env={"KPI_OUTPUT_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _stdout_json(result)
    assert payload["count"] == 2
    assert len(payload["personas"]) == 2
    # 첫 페르소나에 핵심 키들이 들어 있고 raw는 빠져 있다(요약 응답).
    first = payload["personas"][0]
    assert "persona_id" in first
    assert "gender" in first
    assert "raw" not in first


def test_json_mode_list_personas_필터_0건_error_payload(
    fake_load_dataset, tmp_path: Path
) -> None:
    runner = _make_json_runner()
    result = runner.invoke(
        cli,
        [
            "--json",
            "list-personas",
            "--filter",
            "age:90-99",
            "--limit",
            "1",
        ],
        env={"KPI_OUTPUT_DIR": str(tmp_path)},
    )
    assert result.exit_code == 2
    payload = _stdout_json(result)
    assert payload["error"]["code"] == "filter_matched_zero"
    assert payload["error"]["exit_code"] == 2


def test_json_mode_interview_정상_3명_stdout_JSON(
    httpx_mock, fake_load_dataset, tmp_path: Path
) -> None:
    """``--json`` 모드 인터뷰: stdout에 결과 메타 JSON 한 줄, JSON 파일은 파일에 저장."""

    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "test-model"}]},
        status_code=200,
    )
    _add_interview_chat_responses(httpx_mock, n=3)
    # 자동 report 정성 인사이트 호출.
    _add_qualitative_insight_response(httpx_mock)

    runner = _make_json_runner()
    result = runner.invoke(
        cli,
        [
            "--json",
            "interview",
            "--product",
            "반찬",
            "--questions",
            "Q1",
            "--n",
            "3",
            "--output",
            str(tmp_path),
        ],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _stdout_json(result)
    assert payload["ok"] is True
    assert payload["output_path"].endswith(".json")
    assert payload["report_path"].endswith(".md")
    assert payload["summary"]["completed"] == 3
    assert payload["summary"]["requested"] == 3
    # config default 모델 ID가 들어온다(테스트는 KPI_LLM_MODEL을 설정하지 않았다).
    assert payload["model"] == "gpt-4o-mini"
    # 파일은 실제로 생성되었어야 한다.
    assert Path(payload["output_path"]).exists()
    assert Path(payload["report_path"]).exists()


def test_json_mode_report_정상_stdout_JSON(httpx_mock, tmp_path: Path) -> None:
    """``--json`` 모드 report: stdout에 ``{"ok": true, "output_path": ...}``."""

    from src.models import Flags, InterviewRecord, PersonaMeta, StructuredSummary

    persona = PersonaMeta(
        persona_id="x",
        name=None,
        gender="여자",
        age=27,
        region="서울",
        subregion="서울-X",
        occupation="x",
        marital="x",
        education="x",
        raw={},
    )
    summary = StructuredSummary(
        intent="positive",
        willingness_to_pay=30000,
        willingness_to_pay_currency="KRW",
        rejection_reasons=[],
        one_line="x",
    )
    records = [
        InterviewRecord(
            persona_id=f"p{i}",
            persona_meta=persona,
            started_at="t",
            finished_at="t",
            status="completed",
            messages=[],
            raw_responses=[],
            structured_summary=summary,
            flags=Flags(),
            error=None,
        )
        for i in range(3)
    ]

    json_path = tmp_path / "interview_korea-persona-interview_20260502_120000.json"
    _write_full_payload(json_path, records=records)
    _add_qualitative_insight_response(httpx_mock)

    runner = _make_json_runner()
    result = runner.invoke(
        cli,
        ["--json", "report", str(json_path)],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _stdout_json(result)
    assert payload["ok"] is True
    assert payload["output_path"].endswith(".md")
    assert Path(payload["output_path"]).exists()


def test_healthcheck_model_override_CLI(httpx_mock, tmp_path: Path) -> None:
    """``--model`` CLI 옵션이 config.yaml의 llm.model을 일회성으로 덮는다."""

    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "gpt-4o"}]},
        status_code=200,
    )

    runner = _make_json_runner()
    result = runner.invoke(
        cli,
        ["--json", "healthcheck", "--model", "gpt-4o"],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _stdout_json(result)
    assert payload["model"] == "gpt-4o"


def test_interview_model_override_CLI(
    httpx_mock, fake_load_dataset, tmp_path: Path
) -> None:
    """``--model`` 옵션이 인터뷰 호출에 적용되고 결과 JSON에도 반영된다."""

    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "gpt-4o"}]},
        status_code=200,
    )
    _add_interview_chat_responses(httpx_mock, n=1)

    runner = _make_json_runner()
    result = runner.invoke(
        cli,
        [
            "--json",
            "interview",
            "--product",
            "반찬",
            "--questions",
            "Q1",
            "--n",
            "1",
            "--no-report",
            "--model",
            "gpt-4o",
            "--output",
            str(tmp_path),
        ],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _stdout_json(result)
    assert payload["model"] == "gpt-4o"


def test_interview_콘솔_토큰_비용_한_줄_표시(
    httpx_mock, fake_load_dataset, tmp_path: Path
) -> None:
    """interview 명령 종료 시 콘솔에 토큰 사용량 + 비용 추정 한 줄이 노출된다.

    배치 응답에 ``usage``가 포함되면 envelope.usage가 누적되고 ``$0.XXXX`` 형태의
    비용 추정이 콘솔에 출력된다(파일 JSON에는 meta_extra.usage로 보존).
    """

    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "test-model"}]},
        status_code=200,
    )
    # usage가 동봉된 응답을 사용한다. n=1 + 구조화 요약 1.
    for _ in range(2):
        httpx_mock.add_response(
            method="POST",
            url="https://api.openai.com/v1/chat/completions",
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "긍정적입니다. 한번 시도해 보고 싶어요. 가격도 적당해 보입니다.",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1500,
                    "completion_tokens": 80,
                    "total_tokens": 1580,
                    "prompt_tokens_details": {"cached_tokens": 1200},
                },
            },
            status_code=200,
        )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--no-color",
            "interview",
            "--product",
            "반찬",
            "--questions",
            "Q1",
            "--n",
            "1",
            "--no-report",
            "--output",
            str(tmp_path),
        ],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 0, result.output
    # 토큰 한 줄과 비용 한 줄이 같이 노출된다(prefix가 같은 [INFO] 안에 있음).
    assert "토큰 사용량" in result.output
    assert "prompt 1,500" in result.output
    assert "cached 1,200" in result.output
    assert "비용 추정: $" in result.output


def test_interview_dry_run은_자동_리포트도_안만든다(
    httpx_mock,
    fake_load_dataset,
    tmp_path: Path,
) -> None:
    """dry-run은 JSON 저장과 리포트 생성을 모두 건너뛴다(콘솔 출력만)."""

    httpx_mock.add_response(
        method="GET",
        url="https://api.openai.com/v1/models",
        json={"data": [{"id": "test-model"}]},
        status_code=200,
    )
    # dry-run은 1명 + 구조화 요약. 자동 report 호출은 없어야 한다.
    _add_interview_chat_responses(httpx_mock, n=1)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--no-color",
            "interview",
            "--product",
            "반찬",
            "--questions",
            "Q1",
            "--n",
            "1",
            "--dry-run",
            "--output",
            str(tmp_path),
        ],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 0, result.output
    json_files = list(tmp_path.glob("interview_*.json"))
    md_files = list(tmp_path.glob("report_*.md"))
    assert not json_files
    assert not md_files


def test_report_정상_E2E_exit_0(httpx_mock, tmp_path: Path) -> None:
    """정상 record 3명에 대해 report 명령이 마크다운 파일을 만든다."""

    from src.models import Flags, InterviewRecord, PersonaMeta, StructuredSummary

    persona = PersonaMeta(
        persona_id="x",
        name=None,
        gender="여자",
        age=27,
        region="서울",
        subregion="서울-X",
        occupation="x",
        marital="x",
        education="x",
        raw={},
    )
    summary = StructuredSummary(
        intent="positive",
        willingness_to_pay=30000,
        willingness_to_pay_currency="KRW",
        rejection_reasons=[],
        one_line="x",
    )
    records = [
        InterviewRecord(
            persona_id=f"p{i}",
            persona_meta=persona,
            started_at="t",
            finished_at="t",
            status="completed",
            messages=[],
            raw_responses=[],
            structured_summary=summary,
            flags=Flags(),
            error=None,
        )
        for i in range(3)
    ]

    json_path = tmp_path / "interview_korea-persona-interview_20260502_120000.json"
    _write_full_payload(json_path, records=records)

    # 정성 인사이트 LLM 응답
    insight_text = json.dumps(
        {
            "common_reactions": ["반응 1"],
            "insights": [f"i{i}" for i in range(6)],
            "cohort_differences": "차이",
        },
        ensure_ascii=False,
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.openai.com/v1/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": insight_text}}]},
        status_code=200,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--no-color", "report", str(json_path)],
        env={
            "KPI_OUTPUT_DIR": str(tmp_path),
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert result.exit_code == 0, result.output
    md_files = list(tmp_path.glob("report_*.md"))
    assert md_files
