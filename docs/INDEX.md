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
