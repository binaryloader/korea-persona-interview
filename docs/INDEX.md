# docs: korea-persona-interview

본 디렉토리는 korea-persona-interview 도구의 기획 산출물을 모은다. 단일 진입점으로 본 INDEX를 사용한다.

## 1. 문서 트리

- [prd/korea-persona-interview.md](prd/korea-persona-interview.md) - 제품 요구사항(배경/목표/스토리/수용 기준/기능/비기능/우선순위/제외/지표/리스크)
- [tdd/korea-persona-interview.md](tdd/korea-persona-interview.md) - 기술 설계(데이터셋 컬럼 매핑/모듈 책임/시그니처/JSON 스키마/에러/로깅/멀티턴/동시성/의존성/CLI/테스트/작업 분해)
- [adr/2026-05-02-multiturn-strategy.md](adr/2026-05-02-multiturn-strategy.md) - 멀티턴 + 단일턴 구조화 요약 채택 결정
- [adr/2026-05-02-openai-backend-migration.md](adr/2026-05-02-openai-backend-migration.md) - 로컬 MLX → OpenAI Chat Completions API 백엔드 전환 결정
- [ui/korea-persona-interview.md](ui/korea-persona-interview.md) - CLI 사용자 흐름과 콘솔 출력 명세, 한국어 에러 메시지 사전, 리포트 마크다운 섹션 트리
- [tasks/korea-persona-interview.md](tasks/korea-persona-interview.md) - 작업 표(T1-T11 + GATE-1/2), 의존성 그래프, 마일스톤

## 2. 작성 순서

1. PRD(`prd/`)
2. TDD(`tdd/`) + 데이터셋 viewer 직접 조회로 컬럼 매핑 박음
3. ADR-001(`adr/`)
4. UI 명세(`ui/`)
5. 작업 분해(`tasks/`)

## 3. 정합성 결정값 요약

- 종료 코드: 0 정상, 1 키 미설정/API 오류/입력 오류, 2 표본/필터 결과 0건, 3 부분 실패, 130 SIGINT
- 동시성: 기본 4, 한계 1-10(11 이상/0 이하 차단). v1.x OpenAI 백엔드 기준이며 v1.0의 1-3 상한은 로컬 MLX 메모리 가드라 무관
- 토큰 윈도우: 8000(system + 최근 N턴 보존, 가장 오래된 user/assistant 페어부터 truncate)
- 자동 follow-up 상한: 1회
- 페르소나 깨짐 임계값: 영어 단어 비율 30% 초과 또는 한자 비율 5% 초과 또는 페르소나 정보(연령/성별/지역/거주 형태) 정면 모순
- 코호트 마스킹 임계값: 셀별 표본 3명 미만
- base_url: `https://api.openai.com/v1`(OpenAI Chat Completions API)
- 모델 ID: `gpt-4o-mini`(기본값. `config.yaml`의 `llm.model` 또는 CLI `--model` 옵션으로 변경 가능. v1.x부터 KPI_LLM_MODEL 환경변수는 인정하지 않음)
- 환경변수: `OPENAI_API_KEY`(표준 비밀), `KPI_OPENAI_API_KEY`(fallback 비밀), `KPI_OUTPUT_DIR`(테스트/CI 격리용). v1.0의 KPI_LLM_*/KPI_BATCH_* 환경변수 override는 v1.x에서 제거됐다. 비밀은 코드/yaml/CLI에 하드코딩 금지(security.md §1)
- 시스템 프롬프트 출처: HANDOFF.md §시스템 프롬프트 템플릿
- 환경 도구: uv(가상 환경은 .venv, Python 3.12 고정)

## 4. ADR 인덱스

- [ADR-001 (2026-05-02)](adr/2026-05-02-multiturn-strategy.md) - 멀티턴 + 단일턴 구조화 요약 채택. 후속 supersede 후보: 단일턴 + 사후 요약(100명 30분 SLO 위반 시)
- [ADR-002 (2026-05-02)](adr/2026-05-02-openai-backend-migration.md) - 로컬 MLX → OpenAI Chat Completions API(`gpt-4o-mini`) 백엔드 전환. ADR-001 멀티턴 정책은 백엔드 무관이라 supersede 대상 아님. 후속 supersede 후보: drift 5% 초과 시 gpt-4o 상향, 비용 부담 시 로컬 백엔드 회귀

## 5. 갱신 이력

- 2026-05-02 PRD 작성(v0.1)
- 2026-05-02 PRD §7 MoSCoW 재분류(Could → Should: 자동 follow-up, 페르소나 깨짐 감지)
- 2026-05-02 PRD §5.6, §5.7 코호트 마스킹 임계값 5명 → 3명
- 2026-05-02 PRD §10.7 데이터셋 컬럼 확정 절차 명시
- 2026-05-02 TDD, ADR-001, UI, tasks 작성(v0.1)
- 2026-05-02 PRD §5.4 결과 JSON 스키마 갱신(`name` 옵셔널, `marital`/`education`/`truncated` 추가)
- 2026-05-02 PRD §5.5 필터 DSL 별칭 메커니즘 명시(서울 ↔ 서울특별시, F ↔ 여자, M ↔ 남자)
- 2026-05-02 TDD §12.2.1 Qwen3 thinking 토글(chat_template_kwargs) 보강 추가, GATE-1 검증 결과 반영
- 2026-05-02 환경 도구 uv 채택, .python-version 3.12 고정
- 2026-05-02 GATE-2 통과(데이터셋 컬럼 26개 + 인구 통계 13개 표기 100% 일치 확인)
- 2026-05-02 의존성 lock 파일(`requirements.lock`, `requirements-dev.lock`) 추가, aiohttp는 GHSA-9548-qrrj-x5pj 대응으로 `>=3.13.5,<3.14` 명시 핀(3.14 정식 릴리즈 시 재핀)
- 2026-05-02 PRD §5.2 페르소나 주입에 `family_type`/`housing_type` 명시 추가(거주 형태 추측 회귀 방지)
- 2026-05-02 페르소나 깨짐 감지 재설계 - 한자 비율 임계값 5% 추가, false positive 방지를 위해 페르소나 메타에 등장하는 영문/한자 토큰을 분모에서 제외
- 2026-05-02 토큰 루프 가드 도입(동일 토큰/구절이 max_tokens 한도까지 반복되는 응답 감지 및 `status: "failed"` 처리)
- 2026-05-02 ADR-002 채택, 백엔드 OpenAI Chat Completions API(`gpt-4o-mini`)로 전환. 환경변수 `OPENAI_API_KEY`/`KPI_OPENAI_API_KEY` 표준화. PRD §6.3 보안 정책 갱신(외부 송신 사실 명시), §6.5 호환성에서 mlx-lm 의존 제거, §10.4/§10.5/§10.6 리스크 갱신, §10.7/§10.8 재번호. INDEX, UI, README, LICENSE 동시 갱신
- 2026-05-02 라운드 B1 단일턴 모드 정식 구현(`--single-turn`). 모든 질문을 한 번의 chat 호출로 묶어 보내고 응답 텍스트를 번호별로 분리한다. 자동 follow-up은 단일턴에서 비활성. 파싱 실패 시 `flags.parse_failed=true` + fallback으로 마지막 question에 통째 텍스트 저장. PRD §5.1, §5.4 스키마 갱신
- 2026-05-02 라운드 B2 인터뷰 휴리스틱 임계값/키워드 외부화. `InterviewConfig`에 `auto_follow_up_text`, `auto_follow_up_max` 필드를 추가하고 `english_ratio_threshold`/`short_answer_threshold`/`ambiguous_keywords`/`refusal_keywords`도 사용자가 yaml에서 직접 조정 가능. 모든 임계값은 `__post_init__`에서 범위 검증한다(음수/1.0 초과 차단). config.yaml에 항목별 의미 주석 추가
- 2026-05-02 라운드 B3 리포트/배치 매직 넘버 외부화. `ReportConfig` 신규(cohort_min_cell, top_n_default, histogram_bins, bar_width)와 `BatchConfig.partial_failure_threshold` 추가. `src/report.py`의 `_MIN_COHORT_CELL`/`_PRICE_HIST_BINS`/`_BAR_CHART_WIDTH` 모듈 상수는 backward compat용 fallback으로 보존하지만 `compute_quant`/`render_markdown`이 ReportConfig 값을 받아 동작한다. config.yaml에 report/batch 섹션 항목 추가
- 2026-05-02 라운드 B4 시스템 프롬프트 템플릿 파일 분리. `prompts/system_prompt.txt`로 본문을 옮기고 `InterviewConfig.system_prompt_path`로 경로 외부화. `build_system_prompt`는 파일에서 템플릿을 읽어 `{persona_json}`/`{product}` placeholder를 채운다. 파일 부재/placeholder 누락 시 ConfigError로 친절한 한국어 안내. 프로세스 단위 mtime 기반 in-memory 캐시로 디스크 I/O 최소화. PRD §5.2, README Configuration 섹션 갱신
- 2026-05-02 라운드 B5 리팩토링 1. `main.py`의 `interview` 명령에서 `_common_setup` + 즉시 `load_config` 재호출 중복을 제거하고 `cli_overrides`를 한 번에 박아 일원화. `src/load_personas.py`/`src/report.py`/`src/batch.py`의 광범위 `except Exception`을 명시 도메인 예외(OSError/ValueError/RuntimeError 등)로 좁히고, datasets/tqdm 신규 버전 안전망만 BLE001 noqa로 명시 유지. `src/interview.py`의 구조화 요약 단계 예외 분기도 도메인 예외 4종으로 좁힘. `_run_single`의 안전망 except는 `exc_info=True`로 stack trace 추적 정보 추가
- 2026-05-02 라운드 B6 SRP 분리. `MESSAGES` 사전과 `Console` 클래스, `resolve_color` 헬퍼를 `src/console.py`로 옮기고, 페르소나 표 렌더와 `--json` 모드 dict 변환은 `src/cli_views.py`로 분리한다. `main.py`는 click 명령 라우팅과 dry-run 흐름에 집중한다(라인 수 1465 → 1278, 187줄 감소). `_run_dry_run`은 v1.1 백로그
- 2026-05-02 라운드 B7 잔여 정리. 모듈 내 `_age_bucket` 헬퍼를 도메인 의도에 맞게 리네임했다. interview 모듈은 `_age_bucket_for_drift`, report 모듈은 `_age_bucket_for_cohort`로 분리했다. models 모듈의 list/dict 필드 원소 타입을 매개화하여 가독성을 높였다. requirements 파일의 minor 와일드카드를 정확한 버전으로 핀하여 lock 파일과 일치시켰다. 패키지 진입에 docstring과 `__version__ = "0.1.0"`을 추가했고 config 모듈의 미사용 `field` import를 제거했다
