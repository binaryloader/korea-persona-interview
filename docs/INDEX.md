# docs: korea-persona-interview

본 디렉토리는 korea-persona-interview 도구의 기획 산출물을 모은다. 단일 진입점으로 본 INDEX를 사용한다.

## 1. 문서 트리

- [prd/korea-persona-interview.md](prd/korea-persona-interview.md) - 제품 요구사항(배경/목표/스토리/수용 기준/기능/비기능/우선순위/제외/지표/리스크)
- [tdd/korea-persona-interview.md](tdd/korea-persona-interview.md) - 기술 설계(데이터셋 컬럼 매핑/모듈 책임/시그니처/JSON 스키마/에러/로깅/멀티턴/동시성/의존성/CLI/테스트/작업 분해)
- [adr/2026-05-02-multiturn-strategy.md](adr/2026-05-02-multiturn-strategy.md) - 멀티턴 + 단일턴 구조화 요약 채택 결정
- [adr/2026-05-02-openai-backend-migration.md](adr/2026-05-02-openai-backend-migration.md) - 로컬 MLX → OpenAI Chat Completions API 백엔드 전환 결정(ADR-003에 의해 supersede)
- [adr/2026-05-02-multi-provider-backend.md](adr/2026-05-02-multi-provider-backend.md) - multi-provider 백엔드(OpenAI / Anthropic / 로컬 LLM / MCP sampling) 결정. ADR-002 supersede
- [ui/korea-persona-interview.md](ui/korea-persona-interview.md) - CLI 사용자 흐름과 콘솔 출력 명세, 한국어 에러 메시지 사전, 리포트 마크다운 섹션 트리
- [tasks/korea-persona-interview.md](tasks/korea-persona-interview.md) - 작업 표(T1-T11 + GATE-1/2), 의존성 그래프, 마일스톤
- [backlog/v1.1.md](backlog/v1.1.md) - v1.1로 미룬 백로그 항목과 동기

본 도구의 코드 진입점, 설정, 보조 자산은 아래 위치를 참고한다.

- [/main.py](../main.py) - click CLI 진입점(4개 서브커맨드 + `--json` 모드)
- [/src/mcp_server.py](../src/mcp_server.py) - stdio MCP 서버 진입점(`python -m src.mcp_server` 또는 `kpi-mcp-server`)
- [/config.yaml](../config.yaml) - 기본 설정. `secrets via env, defaults via yaml, one-off via CLI` 정책의 정본
- [/prompts/system_prompt.txt](../prompts/system_prompt.txt) - 시스템 프롬프트 템플릿. `{persona_json}`/`{product}` placeholder 포함
- [/examples/mcp/](../examples/mcp/) - Claude Code/Cursor용 mcp.json drop-in 예시
- [/pyproject.toml](../pyproject.toml) - PEP 621 메타와 console script(`kpi`, `kpi-mcp-server`) 등록
- [/LICENSE](../LICENSE) - MIT 라이선스
- [/.env.example](../.env.example) - OpenAI API 키 템플릿(.env로 복사)

## 2. 작성 순서

1. PRD(`prd/`)
2. TDD(`tdd/`) + 데이터셋 viewer 직접 조회로 컬럼 매핑 박음
3. ADR-001(`adr/`)
4. UI 명세(`ui/`)
5. 작업 분해(`tasks/`)
6. ADR-002 + 라운드 A/B/C 보강(MCP, --json, single-turn 등)
7. v1.1 백로그(`backlog/v1.1.md`)

## 3. 정합성 결정값 요약

본 절은 코드/설정/CLI/문서가 모두 같은 값을 공유해야 하는 결정 사항을 한 곳에서 본다. 라운드 A+B+C 결과가 모두 반영되어 있다.

### 3.1. 종료 코드와 흐름

- 종료 코드는 0 정상, 1 키 미설정/API 오류/입력 오류, 2 표본/필터 결과 0건, 3 부분 실패, 130 SIGINT다
- 부분 실패 임계값은 `batch.partial_failure_threshold` 기본 0.5(완료된 record 비율이 본 값 미만이면 partial_failure)다
- CLI 흐름은 healthcheck → list-personas → interview(`--report` 자동) → (필요 시 report 재생성)이다

### 3.2. CLI/모델/동시성

- 4개 CLI 서브커맨드는 `healthcheck`, `list-personas`, `interview`, `report`다. 매크로 명령은 두지 않는다
- 루트 옵션은 `--config`, `--no-color`, `--log-level`, `--json`이다. `--json`은 외부 에이전트와 비대화형 통합용이며 stdout JSON 한 덩어리만 남긴다
- `interview` 명령의 `--report/--no-report`는 기본 `--report`다. 인터뷰 종료 후 마크다운 리포트를 자동 생성한다. `--no-report`는 외부 분석 파이프라인용이며 `--dry-run`은 본 옵션과 무관하게 둘 다 만들지 않는다
- `--single-turn`은 모든 질문을 한 chat 호출에 묶는 모드다. 자동 follow-up은 단일턴에서 비활성, 파싱 실패 시 `flags.parse_failed=True`와 마지막 question 통째 fallback이다
- `--model`은 healthcheck/interview/report 세 명령 모두에서 일회성 모델 ID 덮어쓰기를 지원한다(우선순위 `--model > config.yaml > 기본값`)
- 동시성은 기본 4, 한계 1-10(11 이상/0 이하 차단)이다. v1.x OpenAI 백엔드 기준이며 v1.0의 1-3 상한은 로컬 MLX 메모리 가드라 무관하다
- 토큰 윈도우는 32000(system + 최근 N턴 보존, 가장 오래된 user/assistant 페어부터 truncate)이다
- 자동 follow-up 상한은 1회(`interview.auto_follow_up_max`)다. 본 값을 0으로 두면 비활성화된다

### 3.3. 휴리스틱과 임계값(라운드 B 외부화 결과)

- 짧은 답변 트리거는 `interview.short_answer_threshold`(기본 20자, 공백 제거)다
- 영어 비율 임계값은 `interview.english_ratio_threshold`(기본 0.30)다
- 한자 비율 임계값은 `interview.hanja_ratio_threshold`(기본 0.05)다
- 모호 키워드는 `interview.ambiguous_keywords` 리스트로 외부화한다(기본 6종)
- 거부 키워드는 `interview.refusal_keywords` 리스트로 외부화한다(기본 7종)
- 자동 follow-up 발화는 `interview.auto_follow_up_text`(기본 `조금만 더 자세히 말씀해 주실 수 있을까요?`)다
- 코호트 마스킹 임계값은 `report.cohort_min_cell`(기본 3)이다
- 거절 사유 상위 N 기본은 `report.top_n_default`(기본 10)다
- 가격 히스토그램 구간은 `report.histogram_bins`(기본 10)다
- 텍스트 막대 폭은 `report.bar_width`(기본 30)다
- 페르소나 깨짐 거주 형태 축은 `family_type` 기준 같은 문장 단위 1인칭 + 거주 동사 정밀 정규식만 매칭한다(false positive 방지)

### 3.4. 백엔드/시크릿/환경변수

- CLI 진입점은 `LlmConfig.provider`로 백엔드를 결정한다. `provider=openai`는 OpenAI 호환(공식 API + 로컬 mlx_lm.server/vLLM/llama.cpp)에 모두 사용한다. `provider=anthropic`은 Anthropic Messages API 직접 호출이다(httpx)
- base_url은 provider에 따라 자동 결정된다. `provider=openai`이면 `https://api.openai.com/v1`, `provider=anthropic`이면 `https://api.anthropic.com/v1`이 기본값이다. 로컬 LLM은 `--base-url http://localhost:PORT/v1`로 명시 override한다
- 모델 ID는 provider에 따라 자동 결정된다. openai 기본은 `gpt-4o-mini`, anthropic 기본은 `claude-haiku-4-5`다. `config.yaml`의 `llm.model` 또는 CLI `--model` 옵션으로 변경 가능하다
- MCP 서버 진입점은 sampling 전용이다. host agent의 LLM에 `sampling/createMessage`로 위임하며 server-side에는 키가 필요 없다. host가 sampling capability를 노출하지 않으면 ConfigError + CLI fallback 안내로 차단된다
- 환경변수는 비밀과 출력 디렉토리만 받는다. `OPENAI_API_KEY`/`KPI_OPENAI_API_KEY`(provider=openai), `ANTHROPIC_API_KEY`(provider=anthropic), `KPI_OUTPUT_DIR`(테스트/CI 격리용)이다. 비밀은 코드/yaml/CLI에 하드코딩 금지(security.md §1)다
- `.env` 파일은 stdlib 파서로 비밀만 환경에 승격한다. setdefault 의미라 이미 set된 환경변수는 덮지 않는다
- 기존 `LlmConfig.backend` 토글은 제거됐다. yaml에 잔존해도 graceful하게 무시된다(ADR-003)

### 3.5. 토큰 사용량과 비용 추정(라운드 A + multi-provider)

- OpenAI 응답: `usage.prompt_tokens_details.cached_tokens`를 `TokenUsage.cached_tokens`로 매핑한다
- Anthropic 응답: `usage.input_tokens`/`output_tokens`/`cache_read_input_tokens`를 `TokenUsage` 같은 모양으로 매핑한다
- MCP sampling 응답: usage 미반환이라 0으로 채운다(`estimated_cost_usd`도 0)
- 배치 종료 시 모든 record의 `RawResponse.usage`를 `BatchResultEnvelope.usage`로 합산한다
- `src/_pricing.py`의 `PRICING_TABLE`이 모델별 단가를 박는다. OpenAI 모델은 cached_tokens가 input의 50%, Anthropic 모델은 약 0.1x 수준이다. 알려지지 않은 모델 ID는 `_FALLBACK_PRICING`으로 보수적으로 표시한다
- 콘솔 출력, 결과 JSON `meta_extra.usage`/`meta_extra.estimated_cost_usd`, 리포트 헤더 표 세 곳에 같은 값을 박는다. "추정" 표기를 명시한다
- prompt caching: OpenAI는 prefix 1024 토큰 이상 자동 적용 구조로 설계되어 있다(TDD §9.1). Anthropic의 `cache_control` 마커는 v1.1 백로그

### 3.6. 시스템 프롬프트와 페르소나 풀(라운드 B4, B5)

- 시스템 프롬프트 본문은 `prompts/system_prompt.txt` 외부 파일에 있다. `{persona_json}`/`{product}` placeholder가 누락되면 ConfigError로 차단한다
- 프로세스 단위 mtime 기반 in-memory 캐시로 디스크 I/O를 최소화한다. 파일 편집 후 다음 호출에서 자동 반영된다
- 페르소나 풀은 `(filter_str, n, seed, field_map, gender_aliases, province_aliases, dataset_name, split)` 키로 in-memory 캐싱된다. `clear_persona_pool_cache()`로 무효화 가능하다

### 3.7. MCP 서버(라운드 C + sampling 전용)

- 진입점은 `python -m src.mcp_server`(모듈 단위)와 console script `kpi-mcp-server` 두 가지다
- 4개 도구는 `healthcheck`, `list_personas`, `interview`, `report`다(MCP 관례 snake_case)
- 추론은 `McpSamplingBackend`로 host agent에 위임한다(sampling 전용). server-side에 OpenAI/Anthropic 키가 필요 없다
- host가 sampling capability를 노출하지 않거나 MCP 호스트가 attach되지 않으면 ConfigError + CLI fallback 안내(`python main.py interview ...`)를 돌려준다
- 도구 응답 형태는 `{"ok": true, ...}` 또는 `{"error": {"code", "message", "exit_code"}}`로 통일한다. 모든 응답은 `"backend": "mcp_sampling"` 라벨을 박는다
- 진행률 표시는 `progress_disable=True`로 끈다. 로그는 stderr/`outputs/logs/run_*.jsonl`에 그대로 흘려 stdio JSON-RPC 채널을 오염시키지 않는다
- `mcp` SDK import는 `_serve_stdio()` 안에서 lazy하게 수행한다. SDK 부재 시 친절한 한국어 안내 + exit 1로 종료한다

### 3.8. 환경 도구

- uv(가상 환경은 .venv, Python 3.12 고정)다
- `pyproject.toml`(라운드 C4)이 PEP 621 메타와 console script(`kpi`, `kpi-mcp-server`)를 등록한다. requirements 계열을 정본으로 두고 pyproject는 동기화 상태를 유지한다
- 회귀 테스트는 521개로 multi-provider, MCP sampling 전용, AnthropicBackend, Claude 단가까지 모두 포함한다

## 4. ADR 인덱스

- [ADR-001 (2026-05-02)](adr/2026-05-02-multiturn-strategy.md) - 멀티턴 + 단일턴 구조화 요약 채택. 후속 supersede 후보: 단일턴 + 사후 요약(100명 30분 SLO 위반 시)
- [ADR-002 (2026-05-02)](adr/2026-05-02-openai-backend-migration.md) - 로컬 MLX → OpenAI Chat Completions API(`gpt-4o-mini`) 백엔드 전환. ADR-003에 의해 supersede(multi-provider로 확장)
- [ADR-003 (2026-05-02)](adr/2026-05-02-multi-provider-backend.md) - multi-provider 백엔드 채택. CLI는 `provider=openai|anthropic` + 로컬 LLM via base_url override, MCP는 sampling 전용. 후속 supersede 후보: provider별 페르소나 품질 검증 결과에 따른 default 모델 변경

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
- 2026-05-02 라운드 A1 토큰 사용량 합산과 cached_tokens 추적. `_extract_usage`/`_aggregate_usage`/`BatchResultEnvelope.usage` 도입, 인터뷰 종료 시 콘솔/JSON/리포트 헤더 세 곳에 같은 토큰 수치를 노출
- 2026-05-02 라운드 A2 USD 비용 추정 계산. `src/_pricing.py` 모델별 단가 표(2026-05 기준), cached_tokens 50% 할인 반영, 알려지지 않은 모델 ID는 fallback 단가로 보수적 표시
- 2026-05-02 라운드 A3 `--report` 자동 생성 default 적용. interview 정상 종료 직후 같은 JSON으로 마크다운 리포트를 자동 생성한다. `--no-report`로 끄면 JSON만 저장한다. `--dry-run`은 본 옵션과 무관하게 둘 다 만들지 않는다
- 2026-05-02 라운드 A4 `--json` 루트 모드 도입. tqdm/ANSI/Korean 라벨 모두 끄고 stdout에 결과 JSON 한 덩어리만 남긴다. 에러도 `{"error": {...}}` + non-zero exit으로 통일
- 2026-05-02 라운드 A5 동시성 상한 1-3 → 1-10. v1.0 로컬 MLX 메모리 가드 해제. PRD §6.1, TDD §9, README, config.yaml 동기 갱신
- 2026-05-02 라운드 A6 prompt caching 적합 구조 검증. 시스템 프롬프트 정적 prefix를 앞쪽에, 가변 부분(persona JSON + product)을 뒤쪽에 배치해 1024 토큰 이상 prefix 반복 호출에 자동 캐시 적용
- 2026-05-02 라운드 B1 단일턴 모드 정식 구현(`--single-turn`). 모든 질문을 한 번의 chat 호출로 묶어 보내고 응답 텍스트를 번호별로 분리한다. 자동 follow-up은 단일턴에서 비활성. 파싱 실패 시 `flags.parse_failed=true` + fallback으로 마지막 question에 통째 텍스트 저장. PRD §5.1, §5.4 스키마 갱신
- 2026-05-02 라운드 B2 인터뷰 휴리스틱 임계값/키워드 외부화. `InterviewConfig`에 `auto_follow_up_text`, `auto_follow_up_max` 필드를 추가하고 `english_ratio_threshold`/`short_answer_threshold`/`ambiguous_keywords`/`refusal_keywords`도 사용자가 yaml에서 직접 조정 가능. 모든 임계값은 `__post_init__`에서 범위 검증한다(음수/1.0 초과 차단). config.yaml에 항목별 의미 주석 추가
- 2026-05-02 라운드 B3 리포트/배치 매직 넘버 외부화. `ReportConfig` 신규(cohort_min_cell, top_n_default, histogram_bins, bar_width)와 `BatchConfig.partial_failure_threshold` 추가. `src/report.py`의 `_MIN_COHORT_CELL`/`_PRICE_HIST_BINS`/`_BAR_CHART_WIDTH` 모듈 상수는 backward compat용 fallback으로 보존하지만 `compute_quant`/`render_markdown`이 ReportConfig 값을 받아 동작한다. config.yaml에 report/batch 섹션 항목 추가
- 2026-05-02 라운드 B4 시스템 프롬프트 템플릿 파일 분리. `prompts/system_prompt.txt`로 본문을 옮기고 `InterviewConfig.system_prompt_path`로 경로 외부화. `build_system_prompt`는 파일에서 템플릿을 읽어 `{persona_json}`/`{product}` placeholder를 채운다. 파일 부재/placeholder 누락 시 ConfigError로 친절한 한국어 안내. 프로세스 단위 mtime 기반 in-memory 캐시로 디스크 I/O 최소화. PRD §5.2, README Configuration 섹션 갱신
- 2026-05-02 라운드 B5 리팩토링 1. `main.py`의 `interview` 명령에서 `_common_setup` + 즉시 `load_config` 재호출 중복을 제거하고 `cli_overrides`를 한 번에 박아 일원화. `src/load_personas.py`/`src/report.py`/`src/batch.py`의 광범위 `except Exception`을 명시 도메인 예외(OSError/ValueError/RuntimeError 등)로 좁히고, datasets/tqdm 신규 버전 안전망만 BLE001 noqa로 명시 유지. `src/interview.py`의 구조화 요약 단계 예외 분기도 도메인 예외 4종으로 좁힘. `_run_single`의 안전망 except는 `exc_info=True`로 stack trace 추적 정보 추가
- 2026-05-02 라운드 B6 SRP 분리. `MESSAGES` 사전과 `Console` 클래스, `resolve_color` 헬퍼를 `src/console.py`로 옮기고, 페르소나 표 렌더와 `--json` 모드 dict 변환은 `src/cli_views.py`로 분리한다. `main.py`는 click 명령 라우팅과 dry-run 흐름에 집중한다(라인 수 1465 → 1278, 187줄 감소). `_run_dry_run`은 v1.1 백로그
- 2026-05-02 라운드 B7 잔여 정리. 모듈 내 `_age_bucket` 헬퍼를 도메인 의도에 맞게 리네임했다. interview 모듈은 `_age_bucket_for_drift`, report 모듈은 `_age_bucket_for_cohort`로 분리했다. models 모듈의 list/dict 필드 원소 타입을 매개화하여 가독성을 높였다. requirements 파일의 minor 와일드카드를 정확한 버전으로 핀하여 lock 파일과 일치시켰다. 패키지 진입에 docstring과 `__version__ = "0.1.0"`을 추가했고 config 모듈의 미사용 `field` import를 제거했다
- 2026-05-02 라운드 C1 MCP(Model Context Protocol) 서버 모듈 추가. `src/mcp_server.py`가 `mcp` Python SDK(공식, 1.27.0) 위에 stdio JSON-RPC 서버를 띄우고 4개 도구(`healthcheck`, `list_personas`, `interview`, `report`)를 노출한다. 각 도구는 application 계층(`run_batch`, `generate_report`, `MlxLLMClient`)을 그대로 재사용하고 한국어를 보존한 JSON 응답을 돌려준다. 진입점은 `python -m src.mcp_server`. 외부 에이전트(Claude Code, Cursor, Codex)가 자연어 호출로 본 도구를 사용할 수 있다
- 2026-05-02 라운드 C2 MCP 서버 dispatch 단위 테스트 23개 추가. 도구 메타, 알려지지 않은 도구/잘못된 인자 dispatch, 4개 도구별 입력 검증과 정상/에러 응답 형태를 모킹 기반으로 검증한다. 회귀 447 → 470개
- 2026-05-02 라운드 C3 MCP 통합 가이드와 예시 추가. README "Integration with External Agents" 섹션을 두 갈래(MCP 서버 + `--json` 모드)로 재구성하고, `examples/mcp/`에 Claude Code/Cursor용 `mcp.json` 예시 두 개와 가이드 README를 둔다. 자연어 호출 예시("1인 가구 대상 반찬 정기배송 30명 인터뷰 돌리고 리포트까지 만들어 줘")까지 안내한다
- 2026-05-02 라운드 C4 패키징 정비. `pyproject.toml` 신규 작성(PEP 621 메타, MIT 라이선스, Python 3.12 핀, 직접 의존성, dev extra). console script 두 개를 등록한다(`kpi = "main:main"`, `kpi-mcp-server = "src.mcp_server:main"`). README Installation 섹션에 `uv pip install -e .` 옵션을 추가한다. requirements.txt와 pyproject.toml은 본 라운드에서 동시 유지하며 이중 관리 단순화는 v1.1 백로그
- 2026-05-02 배포 전 README/docs 최종 정비. README를 실제 배포용으로 전면 재작성(Quick Start 5단계, Usage Examples 5종, CLI Reference 옵션 표 4종 + Filter DSL, Output Format 섹션, Customization 섹션, Project Structure 풀 트리, Roadmap, Contributing 추가). docs/INDEX 정합성 결정값을 라운드 A+B+C 결과로 8개 소절 재구성. v1.1 백로그를 별도 문서(`docs/backlog/v1.1.md`)로 분리해 15개 항목 동기/영향 범위와 함께 정리
- 2026-05-02 multi-provider + MCP sampling 전용 단순화. ADR-003 채택. `LlmConfig.provider`(openai/anthropic) 도입, `AnthropicBackend` 추가(httpx 직접, anthropic SDK 의존 없음), 로컬 LLM은 provider=openai + `--base-url` override 패턴. CLI에 `--provider`/`--base-url` 옵션 추가. MCP 서버는 sampling 전용으로 단순화(host LLM 위임, 키 불필요). `LlmConfig.backend` 토글 제거. `src/_pricing.py`에 Claude 단가 추가(haiku/sonnet/opus). 콘솔 메시지 사전을 provider-agnostic하게 갱신("OpenAI 서버" → "LLM 서버"). 코드 주석을 SDK 공개 수준으로 재작성(internal-only 한국어 주석 정리, 영어 docstring 통일). 회귀 504 → 521개. 본 라운드 ADR-002 supersede
