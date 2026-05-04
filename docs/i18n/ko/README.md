[English](../../../README.md) | 한국어 | [日本語](../ja/README.md)

# korea-persona-interview

[![CI](https://github.com/binaryloader/korea-persona-interview/actions/workflows/test.yml/badge.svg)](https://github.com/binaryloader/korea-persona-interview/actions/workflows/test.yml)

OpenAI, Anthropic Claude, OpenAI 호환 로컬 LLM(mlx_lm.server, vLLM, llama.cpp) 위에서 한국인 합성 페르소나 인터뷰를 실행하는 현장용 CLI이다. NVIDIA Nemotron-Personas-Korea 데이터셋(CC BY 4.0, 약 100만 한국인 합성 페르소나)을 원하는 모델과 결합해 실제 참가자를 모집하기 전 제품 아이디어, 인터뷰 가이드, 페르소나 가설을 압박 테스트한다.

이 도구는 네 개의 CLI 서브커맨드(`healthcheck`, `list-personas`, `interview`, `report`), 머신 간 통신용 JSON 출력 모드, MCP(Model Context Protocol) 진입점을 제공한다. MCP 진입점은 MCP 서버 모드(서버측 OpenAI/Anthropic 호출) 또는 MCP 오케스트레이터 모드(호스트 에이전트의 서브에이전트가 LLM 작업 수행) 중 하나로 동작한다.

## Features

- 100만 명 이상의 한국인 합성 페르소나(NVIDIA Nemotron-Personas-Korea, CC BY 4.0)를 활용한 멀티 턴 인터뷰
- 세 가지 추론 대상을 지원한다. OpenAI Chat Completions API, Anthropic Messages API, 모든 OpenAI 호환 로컬 서버
- 동시성 1-10, tqdm 진행률, SIGINT 부분 저장, 종료 코드 3 부분 실패 감지를 지원하는 비동기 배치 러너
- 성별/연령/지역/가족 유형 축에 대한 문장 단위 1인칭 주장 기반 페르소나 드리프트 감지(부정 가드, 3인칭 제외) 그리고 영어 비율 안전망
- `--persona-id`로 uuid를 지정해 특정 페르소나를 고정하여 A/B 비교, `--resume PATH`로 이전 배치의 실패 record만 재실행
- `--insight-model`로 인터뷰는 작은 모델, 정성 인사이트 호출은 더 큰 모델로 실행 가능
- OpenAI 스트리밍(`llm.streaming: true`)과 Anthropic 프롬프트 캐싱(`llm.anthropic_cache_control: true`, 기본 활성화)
- 거짓 양성을 정리하기 위한 LLM-as-judge 드리프트 정제(`heuristics.llm_drift_review`, 옵트인)
- 모든 구조화 요약에 `acceptable_price_signal`(`cheap`/`fair`/`expensive`/`null`)이 들어가고, 시그널 분포에서 WTP 추천을 선택적으로 산출한다
- Claude Code, Cursor, Codex용 MCP 진입점(`python -m src.mcp_server`). `mcp.mode`로 `orchestrator`(기본, 서버측 키 불필요)와 `server`(서버측 OpenAI/Anthropic 호출) 사이를 전환한다
- 매 실행 후 자동으로 마크다운 리포트 생성(`--no-report`로 비활성화) 그리고 셸 스크립트용 `--json` 루트 모드
- 모든 질문을 하나의 채팅 호출로 묶어 토큰을 절감하는 단일 턴 모드(`--single-turn`)
- 모든 실행 마지막에 토큰 사용량(prompt / completion / cached)을 출력하고 JSON과 리포트 헤더에 포함한다
- `--seed`를 통한 재현 가능한 샘플링. 동일 seed, 동일 필터, 동일 데이터셋 버전이면 동일한 페르소나를 반환한다
- 운영 강화 항목으로 로그에서 페르소나 id를 sha256 마스킹, `outputs/`를 모드 0700으로 생성(결과 파일은 0600), `--product`와 질문 텍스트는 2000자 제한과 프롬프트 인젝션 가드를 적용한다
- 외부 텔레메트리를 보내지 않는다. 외부 통신은 설정한 LLM 엔드포인트, 그리고 최초 실행 시 데이터셋용 Hugging Face Hub로만 나간다

## Requirements

- Python 3.12(`.python-version`에 고정)
- [uv](https://docs.astral.sh/uv/) 패키지 매니저
- 사용할 제공자에 맞는 API 키
  - `provider=openai`(기본)에는 `OPENAI_API_KEY`. https://platform.openai.com/api-keys 에서 발급한다
  - `provider=anthropic`에는 `ANTHROPIC_API_KEY`. https://console.anthropic.com/ 에서 발급한다
  - 로컬 LLM(mlx_lm.server, vLLM, llama.cpp)은 `provider=openai`을 유지하고 비어있지 않은 임의의 값을 사용한다
- LLM API 호출과 최초 데이터셋 다운로드(약 100만 record, 이후 `~/.cache/huggingface`에 캐시됨)를 위한 인터넷 접근
- macOS, Linux, Windows 모두 지원한다. Apple Silicon, GPU, 로컬 런타임 요구 사항이 없다

## Installation

`.python-version`이 Python 3.12를 고정하고 있어 `uv venv`가 자동으로 적절한 인터프리터를 선택한다. 프로덕션 배포는 환경 간 의존성 그래프를 동일하게 유지하기 위해 lockfile에서 설치해야 한다.

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip sync requirements.lock requirements-dev.lock
```

`requirements*.txt`를 수정한 뒤에는 lockfile을 다시 컴파일한다.

```bash
uv pip compile requirements.txt -o requirements.lock
uv pip compile requirements-dev.txt -o requirements-dev.lock
```

CLI를 `kpi`로, MCP 서버를 `kpi-mcp-server`로 어디서나 실행하려면 의존성 동기화 후 프로젝트를 editable 모드로 설치한다.

```bash
uv pip install -e .
```

uv를 사용할 수 없다면 일반 pip도 동작한다.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

직접 런타임 의존성은 `pyproject.toml`(`[project.dependencies]`)에 있다. 공식 `openai`, `anthropic` SDK는 의도적으로 사용하지 않는다. 호출은 `httpx`로 수행하므로 의존성 트리가 작게 유지되고 재시도, 타임아웃, 로깅 정책을 프로젝트가 직접 소유한다. 근거는 [docs/adr/2026-05-02-openai-backend-migration.md](../../adr/2026-05-02-openai-backend-migration.md)에 있다.

## Quick Start

다섯 개 명령으로 새 체크아웃에서 완성된 리포트까지 도달한다. 첫 인터뷰 실행은 데이터셋을 다운로드한다(5-10분 소요). 이후 실행은 30초 안에 시작된다.

```bash
export OPENAI_API_KEY=sk-...
python main.py healthcheck
python main.py list-personas --filter "age:25-39,region:서울특별시" --limit 20
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원, 주 2회 배송" --filter "age:25-39,region:서울특별시" --n 10 --questions "이 서비스 쓰실 의향 있나요?" "월 얼마면 적당한가요?" "거절한다면 왜요?"
python main.py report outputs/interview_korea-persona-interview_20260502_120000.json
```

`interview` 명령은 마크다운 리포트를 자동 생성한다(기본 `--report`). 단독 `report` 단계는 `--no-report`를 썼거나 JSON을 편집했거나 다른 `--top-n`/`--include-drift` 설정으로 다시 생성하고 싶을 때만 필요하다.

프로젝트 루트의 `.env` 파일에 `OPENAI_API_KEY=sk-...`(또는 `ANTHROPIC_API_KEY=sk-ant-...`)를 두면 자동으로 인식된다. 이미 설정된 셸 환경 변수가 `.env`보다 우선한다.

Claude를 사용하려면 `ANTHROPIC_API_KEY`를 설정하고 `--provider anthropic`을 전달한다.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python main.py interview --provider anthropic --model claude-haiku-4-5 --product "..." --questions "..." --n 10
```

로컬 OpenAI 호환 서버를 사용하려면 `provider=openai`을 유지하고 `--base-url`을 덮어쓴다. 비어있지 않은 `OPENAI_API_KEY`라면 무엇이든 동작한다. 로컬 서버는 값을 무시한다.

```bash
export OPENAI_API_KEY=local
python main.py interview --base-url http://localhost:8080/v1 --model llama-3-8b --product "..." --questions "..." --n 10
```

## Usage Examples

### Validate a product idea

```bash
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원, 주 2회 배송" --filter "age:25-39,region:서울특별시" --n 10 --seed 42 --questions "이 서비스 쓰실 의향 있나요?" "월 얼마면 적당한가요?" "거절한다면 왜요?"
```

의향률(긍정/중립/부정), 가격 수용가 중간값과 IQR, 상위 거절 사유, 다음 라운드를 위한 5-10개 실행 가능 인사이트를 담은 마크다운 리포트가 생성된다.

### A/B test product copy on the same personas

첫 배치에서 페르소나 id를 추출해 두 번째 실행에 재생함으로써 두 실행에 동일한 페르소나 id를 고정한다.

```bash
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬, 월 39,900원" --filter "age:25-39,region:서울특별시" --n 10 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/copy-a/

python -c "import json,sys; d=json.load(open(sys.argv[1])); print('\n'.join(r['persona_id'] for r in d['records']))" outputs/copy-a/interview_*.json > /tmp/persona_ids.txt

xargs -I {} echo --persona-id {} < /tmp/persona_ids.txt | xargs python main.py interview --product "주말에 받는 1주일치 한식 반찬 박스, 월 39,900원" --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/copy-b/
```

두 실행 모두 정확히 같은 페르소나 id를 인터뷰하므로 유일한 변수는 제품 카피이다.

### Cohort comparison

```bash
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬 정기배송" --filter "age:20-29" --n 15 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/cohort-20s/
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬 정기배송" --filter "age:30-39" --n 15 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/cohort-30s/
```

각 리포트의 코호트 의향률 표는 지역과 성별로 더 분할되므로 20대/30대 격차가 모든 지역에서 유지되는지 아니면 하나의 세그먼트에서 비롯되는지 확인할 수 있다.

### Large-scale screen with single-turn mode

단일 턴 모드는 모든 질문을 하나의 채팅 호출로 묶는다. 이로써 멀티 턴 대비 프롬프트 토큰을 약 절반으로 줄인다. 이 모드에서는 자동 후속 질문이 비활성화된다.

```bash
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원" --filter "age:20-49" --n 100 --seed 42 --concurrency 8 --single-turn --questions "이 서비스 쓸 의향?" "월 얼마면 적당?" "거절 사유?"
```

### Resume after a partial-failure exit

30명 배치가 rate-limit 폭주에 부딪혀 종료 코드 3으로 끝났다고 하자. 이전 JSON 위에 실패한 record만 다시 실행한다.

```bash
python main.py interview --product "..." --filter "..." --n 30 --seed 42 --questions "..." --resume outputs/interview_korea-persona-interview_20260502_120000.json
```

`meta_extra.previous_run_id`가 원본 `interview_id`로 설정되므로 두 실행을 서로 연결할 수 있다.

### Tip: ask explicit value-pricing questions

`willingness_to_pay`는 페르소나가 구체적인 숫자를 말할 때만 채워진다. 명시적 숫자 비율을 최대화하려면 직접적인 가치-가격 질문을 한다.

- "본인은 월 얼마면 가입하시겠어요?"(월 구독에 앵커링)
- "월 39,900원이면 가입할 의향이 있으세요? 아니면 얼마면 적당할까요?"(역제안 프롬프트)
- "비슷한 서비스에 한 달에 얼마까지 쓸 수 있어요?"(상한 탐색)

개방형 가격 질문은 종종 정성 시그널(`acceptable_price_signal`)만 반환한다. 이 시그널은 모든 record에 채워지지만 `willingness_to_pay` 정수는 만들어내지 않는다.

## CLI Reference

### Subcommands

| Command | Description | Exit codes |
| --- | --- | --- |
| `healthcheck` | 제공자 도달성과 모델 가용성을 검증한다 | 0 ok, 1 missing key / 401 / 429 / unreachable |
| `list-personas` | 필터에 매칭되는 페르소나를 미리 본다 | 0 ok, 2 no match |
| `interview` | 배치 인터뷰를 실행하고 JSON을 저장한 뒤 리포트를 자동 생성한다 | 0 ok, 1 server error, 2 sample shortfall, 3 partial failure |
| `report` | 인터뷰 JSON에서 마크다운 리포트를 생성한다 | 0 ok, 1 input error, 2 no valid records |

종료 코드 130은 `SIGINT`(Ctrl-C)를 위해 예약되어 있다. 첫 번째 인터럽트는 부분 JSON을 저장하고 두 번째 인터럽트는 즉시 종료한다.

### Root options

모든 서브커맨드에 적용되며 서브커맨드 이름 앞에 위치해야 한다.

| Option | Default | Description |
| --- | --- | --- |
| `--config PATH` | cwd의 `config.yaml` | 설정 파일 경로를 덮어쓴다 |
| `--no-color` | off | ANSI 컬러 출력을 비활성화한다(`NO_COLOR` 환경변수도 인식한다) |
| `--log-level LEVEL` | yaml 기준 `INFO` | 로그 레벨을 설정한다. `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `--json` | off | stdout에 단일 JSON 문서를 출력한다. tqdm, 컬러, 한국어 라벨을 비활성화한다. 에러는 `{"error": {...}}` 형식으로 떨어지며 종료 코드는 0이 아니다 |

### `interview` options

| Option | Default | Description |
| --- | --- | --- |
| `--product TEXT` | required | 한 줄 제품 설명(최대 2000자) |
| `--questions TEXT` | required, repeatable | 각 질문은 하나의 `--questions` 플래그(각각 최대 2000자) |
| `--filter SPEC` | none | 필터 DSL(아래 참조) |
| `--persona-id UUID` | none, repeatable | uuid로 특정 페르소나 id를 고정한다. `--n`과 `--seed` 무작위화를 비활성화한다. `--filter`와 결합하면 교집합이 된다 |
| `--n N` | `10` | 페르소나 수 |
| `--seed N` | `42` | 샘플링 seed |
| `--concurrency N` | `4` | 비동기 동시성, 범위 1-10 |
| `--persona-fields LIST` | `summary` | 쉼표로 구분된 토글. `summary`, `professional`, `sports`, `arts`, `travel`, `culinary`, `family` |
| `--follow-up TEXT` | none, repeatable | 모든 페르소나에 공통으로 사용할 후속 질문 |
| `--single-turn` | off | 모든 질문을 하나의 채팅 호출로 묶는다. 자동 후속 질문이 비활성화된다 |
| `--dry-run` | off | 페르소나 한 명만 실행하고 콘솔에 출력하며 JSON과 리포트를 모두 쓰지 않는다 |
| `--output DIR` | `outputs/` | 결과 JSON 디렉토리 |
| `--report / --no-report` | `--report` | 인터뷰 후 마크다운 리포트를 자동 생성한다 |
| `--resume PATH` | none | 이전 결과 JSON의 `failed` record만 다시 실행한다 |
| `--provider {openai,anthropic}` | `llm.provider`에서 | LLM 제공자 |
| `--base-url URL` | `llm.base_url`에서 | LLM 서버 base URL |
| `--model MODEL_ID` | `llm.model`에서 | 일회성 모델 덮어쓰기 |

### `report` options

| Option | Default | Description |
| --- | --- | --- |
| `RESULT_PATH` | required(positional) | 인터뷰 JSON 경로 |
| `--top-n N` | `10` | 상위 거절 사유 개수 |
| `--include-drift` | off | 정량 집계에 `status: drift` record를 포함한다 |
| `--output-dir DIR` | 입력 JSON 옆 | 마크다운 리포트를 저장할 위치 |
| `--insight-model MODEL_ID` | `common.report.insight_model` 또는 `--model`에서 | 정성 인사이트 호출에만 다른 모델을 사용한다 |

`healthcheck`와 `list-personas`는 동일한 provider/base-url/model 트리오와 filter/limit/seed를 받는다. 전체 목록은 `python main.py {sub} --help`에서 확인한다.

### Filter DSL

필터는 쉼표로 구분된 `key:value` 쌍을 사용한다. 다른 키끼리는 AND로 결합되고 같은 키가 반복되면 OR로 결합된다.

- `age:25-39`(범위), `age:30`(정확)
- `gender:F`, `gender:M`, `gender:여자`, `gender:남자`, `gender:여성`, `gender:남성`(모두 `여자`/`남자`로 매핑된다)
- `region:서울특별시`, `region:서울`(17개 시도, 정식 명칭 별칭 포함)
- `subregion:강남구`(`district` 컬럼에 대한 접미 매칭)
- `occupation_keyword:개발자`(부분 문자열 매칭)

예시는 아래와 같다.

```text
--filter "age:25-39,region:서울특별시"                    # 25-39 AND Seoul
--filter "age:25-39,region:서울특별시,region:경기도"      # 25-39 AND (Seoul OR Gyeonggi)
--filter "gender:F,occupation_keyword:디자이너"          # female AND occupation contains 디자이너
```

## Output Format

### Result JSON

인터뷰 결과는 `outputs/interview_{slug}_{YYYYMMDD_HHMMSS}.json`에 기록된다. 봉투에는 실행 메타데이터(`interview_id`, `slug`, `product`, `model`, `seed`, `config_snapshot`)와 `records` 배열이 들어 있다. 각 record는 `persona_meta`, 멀티 턴 `messages`, 질문별 `raw_responses`, `structured_summary`, `flags`를 담는다.

| Field | Notes |
| --- | --- |
| `interview_id` | uuid, 실행마다 하나 |
| `schema_version` | v1.1.0부터 `2`(v1.0.x에서는 `1`이었다). 리더는 이 값으로 `acceptable_price_signal` 필드 처리를 분기할 수 있다 |
| `model` | 결정된 모델 id(예: `gpt-4o-mini`) |
| `meta_extra.usage` | 집계된 `prompt_tokens`, `completion_tokens`, `total_tokens`, `cached_tokens` |
| `meta_extra.previous_run_id` | `--resume`로 시작된 실행에 설정된다. 원본 실행의 `interview_id`를 담는다 |
| `records[].status` | `completed` / `refused` / `failed` / `drift` |
| `records[].structured_summary` | `intent`, `acceptable_price_signal`, `willingness_to_pay`, `willingness_to_pay_currency`, `rejection_reasons`, `one_line` |
| `records[].flags` | `persona_drift`, `auto_follow_up_used`, `refusal_detected`, `truncated`, `parse_failed` |

전체 스키마는 `docs/prd/korea-persona-interview.md` 5.4절을 참고한다. v1 JSON 파일은 v1.1.0+에서 정상 로드된다(로더가 `acceptable_price_signal=null`을 채운다).

### Markdown report

report 서브커맨드는 기본적으로 입력 JSON 옆에 `outputs/report_{slug}_{YYYYMMDD_HHMMSS}.md`를 생성한다.

```text
# 가상 인터뷰 리포트: {product}
| meta table | model, seed, persona counts, dataset, usage |

## 1. 정량 지표
### 1.1. 의향률          # intent share table + bar chart
### 1.2. 가격 수용가     # WTP median, IQR, histogram
### 1.3. 거절 사유 빈도  # top-N rejection reasons table
### 1.4. 코호트별 의향률 # age x region x gender, masked under min cell size

## 2. 정성 인사이트
### 2.1. 공통 반응       # up to 5 shared reactions
### 2.2. 인사이트        # 5-10 actionable insights
### 2.3. 코호트 차이     # cohort-level qualitative differences

## 3. 제외 record 요약   # excluded record counts and reasons

## 4. 한계와 출처        # synthetic-data caveat, dataset citation, model id
```

## Configuration

설정 정책은 `시크릿은 환경 변수로, 기본값은 yaml로, 일회성 덮어쓰기는 CLI로`이다. 설정 우선순위(뒤가 앞을 덮어쓴다)는 내장 기본값 → `config.yaml` → CLI 옵션이다.

이 도구가 읽는 환경 변수는 시크릿과 출력 디렉토리뿐이다.

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI API 키(`provider=openai`일 때 사용) |
| `ANTHROPIC_API_KEY` | Anthropic API 키(`provider=anthropic`일 때 사용) |
| `KPI_OUTPUT_DIR` | 출력 디렉토리 덮어쓰기(테스트/CI 격리용으로 유지) |

전체 주석이 달린 yaml은 [config.yaml](../../../config.yaml)에 있다. 주요 키는 아래와 같다.

- `llm.provider` / `llm.base_url` / `llm.model` - 제공자와 엔드포인트. 기본값은 `--provider anthropic`로 전환된다(`https://api.anthropic.com/v1`의 `claude-haiku-4-5`)
- `llm.context_budget` - 멀티 턴 히스토리에 대한 32000 토큰 예산(가장 오래된 user/assistant 쌍부터 제거되며 시스템 프롬프트는 보존된다)
- `llm.streaming` / `llm.anthropic_cache_control` / `llm.extra_chat_kwargs` - 제공자별 튜닝
- `batch.concurrency`(1-10, 기본 4)와 `batch.partial_failure_threshold`(기본 0.5)
- `common.dataset.field_map`, `common.dataset.gender_aliases`, `common.dataset.province_aliases` - 데이터셋 스키마 변경에 대비한 컬럼/값 별칭
- `common.persona.fields`와 `common.persona.system_prompt_path` - 페르소나 토글과 시스템 프롬프트 템플릿 경로
- `common.report.cohort_min_cell` / `histogram_bins` / `bar_width` / `insight_model` / `estimate_wtp_from_signal`
- `common.output.output_dir` / `log_level` / `no_color`
- `heuristics.short_answer_threshold` / `english_ratio_threshold` / `ambiguous_keywords` / `refusal_keywords` / `auto_follow_up_text` / `auto_follow_up_max` / `occupation_english_whitelist` / `llm_drift_review`
- `mcp.mode` - `orchestrator`(기본, 서버측 키 불필요) 또는 `server`(서버측 OpenAI/Anthropic). 근거는 ADR-005를 참고한다

### Choosing a model

`gpt-4o-mini`가 기본값이며 이 워크로드의 강력한 베이스라인을 제공한다. 자체 실행에서 페르소나 드리프트율이 5% 이상으로 측정되면 아래 대안을 시도한다.

- `gpt-4o-mini`(OpenAI) - 기본값. 한국어 유창성과 페르소나 준수도가 우수하다
- `gpt-4o`(OpenAI) - 더 높은 품질
- `claude-haiku-4-5`(Anthropic) - `--provider anthropic`의 기본값
- `claude-sonnet-4-5` / `claude-opus-4-5`(Anthropic) - 더 높은 품질
- `mlx_lm.server`, `vLLM`, `llama.cpp`로 제공되는 로컬 LLM은 OpenAI Chat Completions API 표면을 노출하기만 하면 동작한다. 한국어 유창성은 기본 가중치에 의존하므로 작은 샘플에서 페르소나 드리프트를 먼저 검증한다

페르소나 드리프트 동작은 `gpt-4o-mini`로 종단 간 검증되었다. 다른 모델은 임계값 튜닝이 필요할 수 있다(`heuristics.english_ratio_threshold`, `heuristics.short_answer_threshold`).

### Customization

- 시스템 프롬프트는 `prompts/system_prompt.txt`에서 편집한다(반드시 `{persona_json}`과 `{product}` 자리표시자를 포함해야 한다). 자체 템플릿을 사용하려면 `common.persona.system_prompt_path`를 다른 파일로 지정한다
- 휴리스틱 임계값은 `config.yaml`의 `heuristics.*`에서 튜닝한다(후속 질문을 더 엄격하게 하려면 `short_answer_threshold`를 낮추고 기술 도메인에서는 `english_ratio_threshold`를 높이며 `refusal_keywords`/`ambiguous_keywords`에 도메인별 표현을 추가한다)
- 리포트 출력은 더 엄격한 마스킹을 위해 `common.report.cohort_min_cell`을 5나 7로 올리고 좁은 터미널을 위해 `bar_width`를 줄이고 다른 가격 해상도를 위해 `histogram_bins`를 조정한다

## Integration with External Agents

진입점은 세 가지이다. CLI, MCP 서버, MCP 오케스트레이터. 셋은 서로 호환되지 않는다. 서버측 LLM 호출(CLI, MCP 서버)을 원하는지 또는 호스트 에이전트의 서브에이전트가 LLM 작업을 수행하기를 원하는지(MCP 오케스트레이터)에 따라 선택이 달라진다.

### Entry point matrix

| Entry point | mode (yaml) | Server-side LLM call | Host LLM call | API key required |
| --- | --- | --- | --- | --- |
| CLI(`kpi`) | n/a | yes | no | provider-dependent |
| MCP server | `mcp.mode: "server"` | yes | no | provider-dependent |
| MCP orchestrator | `mcp.mode: "orchestrator"`(default) | no | yes(host sub-agent) | none |

모드 사이의 자동 폴백은 없다. 선택된 경로는 모든 응답에 `"backend": "mcp_server"` 또는 `"backend": "mcp_orchestrator"`로 반영된다. ADR-005가 근거를 담고 있다(주류 MCP 클라이언트가 해당 capability를 광고하지 않아 v1.2.0에서 sampling 모드가 제거되었다).

`python -m src.mcp_server`를 `mcp.mode: "orchestrator"` 상태에서 MCP 호스트 외부에서 실행하면 헬퍼 도구는 여전히 동작하지만 `interview`는 차단되며 `build_batch_prompts` + 서브에이전트 + `aggregate_results`를 사용하라는 힌트를 출력한다.

### Tool exposure by mode

| Tool | MCP server | MCP orchestrator | Notes |
| --- | --- | --- | --- |
| `healthcheck` | yes | yes | 서버 모드는 제공자에 ping을 보내고, 오케스트레이터 모드는 ok와 cwd를 반환한다 |
| `list_personas` | yes | yes | 필터에 매칭되는 페르소나를 미리 본다 |
| `interview` | yes | no(blocked) | 서버측 배치 인터뷰 |
| `report` | yes | yes | 서버 모드는 정성 인사이트 LLM 호출을 실행하고 오케스트레이터 모드는 이를 건너뛴다 |
| `build_persona_prompt` | no | yes | 페르소나 한 명의 시스템 프롬프트와 페르소나 dict |
| `build_batch_prompts` | no | yes | N명의 페르소나에 대한 시스템 프롬프트(호스트 서브에이전트 fan-out) |
| `aggregate_results` | no | yes | 호스트로부터 record를 받아 마크다운 리포트를 생성한다 |
| `detect_persona_drift` / `should_auto_follow_up` / `parse_structured_summary` / `interview_record_schema` | yes | yes | 헬퍼. CLI와 MCP 서버는 자동 적용한다. MCP 오케스트레이터는 명시적으로 호출해야 한다 |

### Registering the MCP entry point

서버를 수동으로 실행해 시작 여부를 확인한다.

```bash
python -m src.mcp_server
```

`~/.claude/mcp.json`에 아래 스니펫을 추가해 Claude Code에 등록한다(파일이 없다면 새로 만든다). `cwd`는 프로젝트 루트를 가리켜야 `config.yaml`, `prompts/system_prompt.txt`, `.env`, `outputs/`가 올바르게 해결된다.

```json
{
  "mcpServers": {
    "korea-persona-interview": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/absolute/path/to/korea-persona-interview"
    }
  }
}
```

Cursor의 경우 프로젝트 루트의 `.cursor/mcp.json`에 스니펫을 추가한다. 드롭인 사본은 [examples/mcp/](../../../examples/mcp/) 아래에 있다.

MCP 서버 모드에서는 첫 실행 전 프로젝트 `.env`에 `OPENAI_API_KEY`(또는 `ANTHROPIC_API_KEY`)를 넣는다. 표준 라이브러리 `.env` 로더는 `setdefault` 시맨틱을 사용하므로 셸에서 이미 export된 키가 우선한다. 에이전트 mcp.json의 `env` 블록에 키를 넣어도 동작하지만 시크릿이 에이전트 설정에 평문으로 남아 git, dotfile 동기화, 스크린샷을 통해 유출될 위험이 더 크다.

### MCP orchestrator mode usage (default)

호스트 에이전트가 LLM을 소유한다. 흐름은 아래와 같다.

1. `product`, `questions`, `n`(선택적으로 `filter`, `seed`, `persona_ids`)으로 `build_batch_prompts`를 호출한다. N개의 시스템 프롬프트와 페르소나 dict를 반환한다
2. 호스트는 N개의 서브에이전트(페르소나당 하나)로 fan-out한다. 각 서브에이전트는 자신의 LLM에 반환된 시스템 프롬프트를 시스템 메시지로, 질문을 user 턴으로 사용한다. 호스트는 CLI 휴리스틱과의 동작 패리티를 유지하기 위해 턴 사이에 `should_auto_follow_up`과 `detect_persona_drift`를 호출할 수도 있다
3. LLM 호출 후 호스트는 LLM의 구조화 요약 텍스트에 `parse_structured_summary`를 호출해 정규화된 dict를 얻고, `interview_record_schema`에 따라 record를 조립한다
4. 호스트는 조립한 `records`로 `aggregate_results`를 호출한다. 이 도구는 정량 집계를 수행하고 마크다운 리포트를 작성한다. 정성 인사이트는 기본적으로 폴백 메시지로 채워지며, 호스트가 `insights`를 전달해 본문에 임베드되도록 할 수 있다

### MCP server mode usage

`config.yaml`에서 `mcp.mode: "server"`로 설정해 OpenAI/Anthropic을 서버측에서 호출한다. 에이전트에게 평이한 한국어로 "1인 가구 대상 반찬 정기배송(월 39,900원)을 25-39세 서울 30명에게 인터뷰 돌리고 리포트까지 만들어 줘"라고 요청하면 `interview` 다음 `report`를 차례로 호출하고 마크다운 경로를 반환한다.

### --json mode for shell scripts

CLI를 직접 구동하는 에이전트의 경우 루트 그룹에 `--json`을 전달한다. tqdm, 컬러, 한국어 라벨이 비활성화되며 stdout에 단일 JSON 문서가 출력된다. 로그는 stderr와 `outputs/logs/run_*.jsonl`로 계속 흐른다.

```bash
python main.py --json healthcheck
# {"ok": true, "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "models": [...]}

python main.py --json interview --product "..." --questions "..." --n 10
# {"ok": true, "output_path": "outputs/interview_*.json", "report_path": "outputs/report_*.md", "summary": {...}, "usage": {...}, "model": "gpt-4o-mini"}
```

에러는 `{"error": {"code": "...", "message": "...", "exit_code": N}}` 형식으로 0이 아닌 종료 코드와 함께 출력된다.

## Development

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip sync requirements.lock requirements-dev.lock
pytest tests/ -v
```

테스트 스위트는 `pytest-httpx`로 OpenAI/Anthropic API를 모킹하고 monkeypatch fixture로 데이터셋을 모킹하므로 실제 API 키나 네트워크 접근이 필요하지 않다. 커버리지는 config, filter DSL, persona loader, LLM client/backend, 인터뷰 세션, 페르소나 드리프트, 배치 러너, 리포트 정량, 두 모드의 MCP dispatch, MCP 오케스트레이터 헬퍼 도구, 에러 메시지, 로깅, CLI 통합을 아우른다.

실제 LLM API 호출을 사용하는 수동 smoke 테스트는 `tests/manual/`에 있으며 기본 실행에서 제외된다.

Conventional Commits를 사용한다(`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`). 커밋에 `Co-Authored-By` 트레일러를 넣지 않는다.

## Limitations and Disclaimer

합성 페르소나는 실제 사용자 인터뷰의 대체재가 아니다. 데이터셋은 실제 응답자에게서 표본을 추출한 것이 아니라 생성된 것이므로 인구 분포가 실제 한국 인구와 차이가 날 수 있다. 출력은 실제 참가자를 모집하기 전의 빠른 직관 점검과 모집 예산을 쓰기 전 인터뷰 질문, 제품 카피를 압박 테스트하는 수단으로 다룬다.

이 도구가 생성하는 모든 리포트와 JSON 파일에는 푸터에 합성 데이터 디스클레이머가 함께 들어간다.

각 인터뷰에 사용되는 `--product` 텍스트와 페르소나 메타데이터는 사용자가 설정한 LLM 엔드포인트(OpenAI, Anthropic, 로컬 서버, MCP 호스트 에이전트의 LLM)로 전송된다. 미공개 IP, 영업비밀, 개인 식별 정보를 `--product`에 넣지 않는다. 도구 실행 전에 민감한 부분을 추상화하거나 다르게 표현한다. 도구 자체는 LLM 호출과 Hugging Face로부터의 최초 데이터셋 다운로드를 넘어서는 외부 텔레메트리를 보내지 않는다.

API 청구는 사용자의 책임이다. 토큰 사용량(prompt / completion / cached)은 각 실행 마지막에 출력되고 결과 JSON `meta_extra.usage`에 기록되며 리포트 헤더에 노출되어 제공자 인보이스와 대조할 수 있다. 도구는 USD 비용을 추정하지 않는다. 페르소나 드리프트 품질은 `gpt-4o-mini`에 대해 검증되어 있다. 다른 모델은 임계값 튜닝이 필요할 수 있다.

출력에 대한 법적/윤리적 검토는 사용자의 책임이다. 도구는 입력 시크릿 정책을 넘어서는 컴플라이언스나 PII 필터를 실행하지 않는다.

## Roadmap

v1.3.0 후보의 짧은 목록이다. 자세한 내용은 [docs/backlog/v1.3.0.md](../../backlog/v1.3.0.md)에 있다.

- 동일한 애플리케이션 레이어 위의 FastAPI REST API
- 오프라인 실행을 위한 OpenAI Batch API 경로
- 멀티 모델 A/B 라우팅(같은 페르소나 샘플을 두 모델에서 실행하고 출력을 diff)
- 제공자 품질 검증 리포트(OpenAI, Anthropic, 로컬 LLM의 골든 데이터셋 드리프트 측정)
- API 키를 위한 macOS Keychain / Linux libsecret / Windows Credential Manager 통합
- record 단위 디스크 스트리밍 쓰기(배치 도중 OOM/크래시 시 SIGINT 부분 저장보다 적은 record를 잃도록)

## Dataset and Credits

이 프로젝트는 [nvidia/Nemotron-Personas-Korea](https://huggingface.co/datasets/nvidia/Nemotron-Personas-Korea) 데이터셋을 사용한다.

- Title: Nemotron-Personas-Korea
- Author: NVIDIA Corporation (2025)
- Source: https://huggingface.co/datasets/nvidia/Nemotron-Personas-Korea
- License: [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/)
- Modifications: 없음. 데이터셋은 런타임에 Hugging Face Hub에서 다운로드되어 인메모리에서 샘플링된다. 이 저장소는 어떤 파생 데이터셋도 재배포하지 않는다

이름, 성별, 연령, 결혼 여부, 학력, 직업, 거주지(시도와 시군구), 일곱 개의 페르소나 facet(professional, sports, arts, travel, culinary, family, summary)을 다루는 약 100만 record와 700만 한국인 합성 페르소나를 포함한다.

CC BY 4.0은 출처 표시를 조건으로 상업적 이용을 허용한다. 크레딧은 NVIDIA Corporation에 있다. 이 도구가 생성하는 모든 마크다운 리포트와 JSON record는 푸터에 데이터셋 인용과 라이선스를 함께 담아 다운스트림 산출물에도 출처가 따라가도록 한다.

## Acknowledgments

이 프로젝트는 [Claude Code](https://claude.com/claude-code)와 함께 개발했다.

## License

This project is licensed under the MIT License - see the [LICENSE](../../../LICENSE) file for details.
