# docs: korea-persona-interview

본 디렉토리는 korea-persona-interview 도구의 기획 산출물을 모은다. 단일 진입점으로 본 INDEX를 사용한다.

## 1. 문서 트리

- [prd/korea-persona-interview.md](prd/korea-persona-interview.md) - 제품 요구사항(배경/목표/스토리/수용 기준/기능/비기능/우선순위/제외/지표/리스크)
- [tdd/korea-persona-interview.md](tdd/korea-persona-interview.md) - 기술 설계(데이터셋 컬럼 매핑/모듈 책임/시그니처/JSON 스키마/에러/로깅/멀티턴/동시성/의존성/CLI/테스트/작업 분해)
- [adr/2026-05-02-multiturn-strategy.md](adr/2026-05-02-multiturn-strategy.md) - 멀티턴 + 단일턴 구조화 요약 채택 결정
- [ui/korea-persona-interview.md](ui/korea-persona-interview.md) - CLI 사용자 흐름과 콘솔 출력 명세, 한국어 에러 메시지 사전, 리포트 마크다운 섹션 트리
- [tasks/korea-persona-interview.md](tasks/korea-persona-interview.md) - 작업 표(T1-T11 + GATE-1/2), 의존성 그래프, 마일스톤

## 2. 작성 순서

1. PRD(`prd/`)
2. TDD(`tdd/`) + 데이터셋 viewer 직접 조회로 컬럼 매핑 박음
3. ADR-001(`adr/`)
4. UI 명세(`ui/`)
5. 작업 분해(`tasks/`)

## 3. 정합성 결정값 요약

- 종료 코드: 0 정상, 1 서버/입력 오류, 2 표본/필터 결과 0건, 3 부분 실패, 130 SIGINT
- 동시성: 기본 2, 한계 1-3(4 이상 차단)
- 토큰 윈도우: 8000(system + 최근 N턴 보존, 가장 오래된 user/assistant 페어부터 truncate)
- 자동 follow-up 상한: 1회
- 페르소나 깨짐 임계값: 영어 단어 비율 30% 초과 또는 페르소나 정보 정면 모순
- 코호트 마스킹 임계값: 셀별 표본 3명 미만
- base_url: `http://localhost:8080/v1`(localhost 외 chat 차단)
- 모델 ID: `unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit`(프로젝트 오너 결정. 콘솔 출력 샘플도 본 ID로 통일)
- enable_thinking: false(Qwen3 reasoning 토큰 폭증 회피, 검증된 35B-A3B 조합)
- 시스템 프롬프트 출처: HANDOFF.md §시스템 프롬프트 템플릿

## 4. ADR 인덱스

- [ADR-001 (2026-05-02)](adr/2026-05-02-multiturn-strategy.md) - 멀티턴 + 단일턴 구조화 요약 채택. 후속 supersede 후보: 단일턴 + 사후 요약(100명 30분 SLO 위반 시)

## 5. 갱신 이력

- 2026-05-02 PRD 작성(v0.1)
- 2026-05-02 PRD §7 MoSCoW 재분류(Could → Should: 자동 follow-up, 페르소나 깨짐 감지)
- 2026-05-02 PRD §5.6, §5.7 코호트 마스킹 임계값 5명 → 3명
- 2026-05-02 PRD §10.7 데이터셋 컬럼 확정 절차 명시
- 2026-05-02 TDD, ADR-001, UI, tasks 작성(v0.1)
- 2026-05-02 PRD §5.4 결과 JSON 스키마 갱신(`name` 옵셔널, `marital`/`education`/`truncated` 추가)
- 2026-05-02 PRD §5.5 필터 DSL 별칭 메커니즘 명시(서울 ↔ 서울특별시, F ↔ 여자, M ↔ 남자)
- 2026-05-02 TDD §12.2.1 Qwen3 thinking 토글(chat_template_kwargs) 보강 추가, GATE-1 검증 결과 반영
