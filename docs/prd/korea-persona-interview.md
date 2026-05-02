# PRD: korea-persona-interview

기능 슬러그는 `korea-persona-interview`로 확정한다. 저장소명, 패키지 디렉토리, 문서 경로, 출력 파일 prefix 모두 동일한 슬러그를 사용한다. 후보였던 `persona-interview`는 한국 페르소나라는 핵심 도메인을 잃고, `kpi-cli`는 핵심 성과 지표 약어와 충돌하므로 제외한다.

## 1. 배경

1인 창업자, 사이드 프로젝트 기획자, 초기 단계 프로덕트 매니저는 사업 아이템을 검증하기 위해 잠재 고객 인터뷰가 필요하지만 시간과 비용 제약이 크다. 한국인 응답자 10-30명을 모집해 1차 정성 인터뷰만 진행해도 모집비, 수당, 일정 조율로 수백만 원과 1-2주가 소요된다. 동시에 인터뷰 대상 모집은 친구/지인 편향에 빠지기 쉬워 통계적으로 가까운 분포의 표본을 얻기도 어렵다.

엔비디아가 2025년 공개한 `nvidia/Nemotron-Personas-Korea` 데이터셋은 약 100만 레코드, 약 700만 합성 페르소나를 CC BY 4.0 라이선스로 제공한다. 인구 통계 분포에 가깝게 합성된 한국인 페르소나에 OpenAI Chat Completions API를 결합하면, 실제 인터뷰 이전 단계의 빠른 가설 검증과 페르소나별 반응 시뮬레이션이 가능하다. v1은 OpenAI 백엔드(기본 모델 `gpt-4o-mini`)를 사용한다. 본 결정의 배경과 대안 비교는 ADR-002(`docs/adr/2026-05-02-openai-backend-migration.md`)에 기록한다.

본 도구는 진짜 인터뷰를 대체하지 않는다. 진짜 인터뷰 직전 단계의 가설 정리, 질문지 점검, 초기 페르소나 가설 수립을 빠르게 반복하는 용도로 한정한다. 결과의 한계와 합성 페르소나라는 사실, 그리고 사업 아이템 본문이 OpenAI 서버로 송신된다는 사실을 사용자 인터페이스와 출력물 모두에서 명시한다.

## 2. 목표

본 도구의 목표는 아래와 같다.

- 사업 아이템 한 줄 설명과 질문 리스트만 입력하면 N명의 한국인 합성 페르소나 인터뷰를 자동으로 수행하고 정량/정성 리포트를 생성한다
- 추론은 OpenAI Chat Completions API로 수행한다(기본 모델 `gpt-4o-mini`). 외부 텔레메트리와 외부 분석 서비스 의존은 금지한다
- 100명 인터뷰 1회를 30분 이내에 완료하는 처리 성능을 확보한다
- 페르소나 정보 주입과 멀티턴 흐름으로 답변의 페르소나 일관성을 유지한다(정량 지표로 5% 이하 페르소나 깨짐을 목표로 한다)
- CLI 명령 4개(`healthcheck`, `interview`, `report`, `list-personas`)로 단계별 진행을 분리한다. 한 번에 모두 실행하는 매크로 명령은 v1에서 제공하지 않는다
- 결과는 항상 JSON으로 저장한다. 재분석, 비교 분석, 외부 도구 연동의 기반을 만든다

비목표는 아래와 같다.

- 통계적으로 모집단을 대표하는 추정치 산출은 목표로 하지 않는다. 합성 페르소나의 분포는 모집단 분포와 일치하지 않을 수 있다
- 결과의 윤리적, 법적 검증은 사용자 책임이다. 본 도구는 의사결정 보조 자료를 제공할 뿐이다

## 3. 사용자 스토리

### 3.1. 1인 창업자

- As a 1인 창업자, I want 사업 아이템 한 줄과 질문 5개만 입력해 한국인 페르소나 30명에게 의향을 물어볼 수 있기를 원한다, So that 진짜 사용자 모집 전에 가설이 너무 좁거나 과한지 빠르게 점검할 수 있다
- As a 1인 창업자, I want 결과 리포트에서 거절 사유 상위 N개와 가격 수용 범위를 확인하기를 원한다, So that 다음 인터뷰 라운드에서 어디를 더 파고들지 결정할 수 있다

### 3.2. 사용자 리서처/PM

- As a 프로덕트 매니저, I want 연령대, 성별, 거주 지역, 직업 키워드로 페르소나를 필터링하기를 원한다, So that 타깃 세그먼트에 가까운 응답만 모아서 분석할 수 있다
- As a 프로덕트 매니저, I want 시드 값을 고정해서 같은 표본에 다른 질문을 던질 수 있기를 원한다, So that 질문 변경의 효과만 비교할 수 있다

### 3.3. 사이드 프로젝트 기획자

- As a 사이드 프로젝트 기획자, I want 인터뷰 결과를 JSON 파일로 보관하기를 원한다, So that 나중에 다른 도구로 추가 분석하거나 다른 인터뷰 결과와 비교할 수 있다
- As a 사이드 프로젝트 기획자, I want 모델 응답이 페르소나와 어울리지 않을 때 그 사실이 표시되기를 원한다, So that 신뢰할 수 없는 응답을 분석에서 제외할 수 있다

## 4. 수용 기준

각 기능별 수용 기준은 아래와 같다.

### 4.1. 헬스체크

- Given `OPENAI_API_KEY`가 설정되어 있고 OpenAI API에 접근 가능한 환경일 때, When 사용자가 `python main.py healthcheck`를 실행하면, Then 종료 코드 0과 함께 설정된 모델 ID(기본 `gpt-4o-mini`)와 응답 지연이 출력된다
- Given API 키가 설정되어 있지 않을 때, When 사용자가 `python main.py healthcheck`를 실행하면, Then 종료 코드 1과 함께 "OPENAI_API_KEY 환경변수를 설정해 주세요. https://platform.openai.com/api-keys 에서 발급 후 export OPENAI_API_KEY=sk-... 로 설정합니다"라는 한국어 안내가 출력된다
- Given API 키가 잘못되었거나 만료되어 401이 반환될 때, When 사용자가 `healthcheck`를 실행하면, Then 종료 코드 1과 함께 "OpenAI API 키가 유효하지 않습니다"라는 한국어 안내가 출력된다

### 4.2. 페르소나 목록

- Given 데이터셋이 캐시되어 있을 때, When 사용자가 `python main.py list-personas --filter "age:25-39,region:서울특별시" --limit 20`을 실행하면, Then 필터 조건에 맞는 페르소나 20명의 요약(`persona_id`, 이름, 성별, 연령, 지역, 직업)이 표 형식으로 출력된다
- Given 필터 결과가 0건일 때, When 사용자가 동일 명령을 실행하면, Then 종료 코드 2와 함께 "필터 조건에 맞는 페르소나가 없습니다. 필터를 완화해 주세요"라는 안내와 적용된 필터 요약이 출력된다

### 4.3. 단일 인터뷰(dry-run)

- Given 헬스체크 통과, 데이터셋 로드 완료, 시드 고정일 때, When 사용자가 `python main.py interview --product "반찬 정기배송" --questions "쓸 의향?" "월 얼마면?" "거절 이유?" --n 1 --seed 42 --dry-run`을 실행하면, Then 콘솔에 시스템 프롬프트, 페르소나 메타, 질문별 응답, 구조화 요약이 순서대로 출력되고 JSON 파일은 저장되지 않는다

### 4.4. 배치 인터뷰

- Given 헬스체크 통과, 필터 결과가 N명 이상일 때, When 사용자가 `python main.py interview --product {설명} --filter {필터} --n N --questions {질문들} --concurrency 2 --output outputs/`를 실행하면, Then `outputs/interview_{slug}_{YYYYMMDD_HHMMSS}.json` 파일이 생성되고 N개의 인터뷰 record가 포함된다
- Given 멀티턴 인터뷰가 진행 중일 때, When 한 페르소나의 답변 길이가 20자 미만이면, Then 시스템이 follow-up 질문을 1회 자동 추가해 더 자세한 답변을 유도한다(상한 1회)
- Given 모델 호출이 실패할 때(타임아웃, 연결 실패, HTTP 5xx), When 재시도가 진행되면, Then 지수 백오프(1s, 2s, 4s)로 최대 3회 재시도하고 최종 실패 시 해당 record에 `status: "failed"`와 에러 사유가 기록되며 다른 페르소나 인터뷰는 계속 진행된다

### 4.5. 리포트

- Given 정상 인터뷰 결과 JSON이 있을 때, When 사용자가 `python main.py report outputs/interview_{slug}_{timestamp}.json`을 실행하면, Then 같은 디렉토리에 `report_{slug}_{timestamp}.md`가 생성되고 의향률, 가격 수용 통계, 거절 사유 빈도, 코호트별 차이, 인사이트 5-10개가 포함된다
- Given 인터뷰 record 중 `status: "failed"` 또는 `status: "refused"`가 섞여 있을 때, When 리포트를 생성하면, Then 정량 통계는 정상 record만 사용하고 리포트 상단에 제외된 record 수와 사유 분포가 명시된다

### 4.6. 페르소나 일관성

- Given 응답 텍스트가 있을 때, When 페르소나 깨짐 감지가 동작하면, Then 영어 비율 30% 초과 또는 페르소나 정보(연령/성별/지역)와 명백히 모순되는 표현이 발견된 record에 `persona_drift: true` 플래그가 부착된다
- Given `persona_drift: true` record가 있을 때, When 리포트가 생성되면, Then 정량 통계 계산에서 해당 record는 기본적으로 제외되고 제외 비율이 리포트에 명시된다

## 5. 기능 요구사항

### 5.1. 인터뷰 흐름(멀티턴)

- 사용자는 질문 리스트를 순서대로 1턴씩 모델에 전달하고 모델 응답을 messages 히스토리에 누적할 수 있다
- 사용자는 각 질문 직후의 follow-up 트리거 조건을 선택할 수 있다
  - 자동 follow-up: 답변 길이가 20자 미만이거나 모호한 키워드(예: "글쎄요", "잘 모르겠습니다", "딱히")만 포함되면 시스템이 "조금만 더 자세히 말씀해 주실 수 있을까요?" 류 follow-up을 1회 추가한다(상한 1회)
  - 사용자 정의 follow-up: 명령행 인자 `--follow-up "질문 1" "질문 2"`로 모든 페르소나에 공통 적용되는 후속 질문을 추가할 수 있다
- 인터뷰 종료 후 별도 프롬프트로 구조화 요약을 생성한다(2단계 흐름)
- 멀티턴은 v1의 기본값이다. 단일턴 옵션은 `--single-turn` 플래그로 명시한 경우에만 동작한다(빠른 dry-run, 토큰 절약 목적). 단일턴 모드는 모든 질문(메인 + 사용자 정의 follow-up)을 한 번의 chat 호출에 묶어 보내고, 모델 응답 텍스트를 `1. ... 2. ... 3. ...` 번호 형식으로 question_index별 분리한다. 자동 follow-up은 단일턴에서 비활성화된다(한 번에 다 묶어 보내므로). 응답 번호 파싱이 실패하면 `flags.parse_failed: true`로 표시하고 마지막 question에 통째 텍스트를 fallback으로 저장해 데이터를 잃지 않는다
- v1.1.0부터 `--resume PATH` 옵션을 지원한다. 이전 결과 JSON 경로를 받아 status가 `failed`인 record만 재시도하고 나머지(`completed`/`refused`/`drift`)는 그대로 보존한다. personas는 입력 JSON과 같은 시드/필터/ID 매칭으로 다시 샘플링되며, 새 결과는 새 timestamp 파일로 저장되고 `meta_extra.previous_run_id`에 입력 JSON의 `interview_id`가 박힌다. 모든 record가 이미 completed인 경우 LLM 호출 자체를 건너뛴다

### 5.2. 페르소나 주입

시스템 프롬프트에는 핸드오프 문서의 시스템 프롬프트 템플릿을 기반으로 아래 페르소나 필드 묶음을 JSON 객체로 주입한다.

- 기본 묶음(항상 주입): 데이터셋의 인구 통계 필드(이름, 성별, 연령, 혼인 상태, 교육, 직업, 거주 지역, 거주 형태, 주거 유형) + summary 페르소나(전체 요약 자유 서술)
- 거주 형태(`family_type`)와 주거 유형(`housing_type`)은 1인 가구 여부와 주거 환경을 모델이 추론으로 채우지 않도록 명시적으로 노출한다. 데이터셋에 컬럼이 없거나 값이 비면 해당 키를 JSON에서 생략한다
- 토글 옵션(`--persona-fields professional,sports,arts,travel,culinary,family` 형식의 다중 선택): 직업인/스포츠/예술/여행/미식/가족 페르소나 자유 서술 필드를 선택적으로 추가
- 토글 기본값은 기본 묶음만 주입한다. 토큰 사용량과 페르소나 일관성의 균형 관점에서 가장 안정적인 조합이다
- 시스템 프롬프트 [지침] 섹션에는 family_type 정보를 그대로 반영하고 거주 형태를 추측하지 않도록 한 줄을 명시한다. 25세 1인 가구 페르소나가 ``1인 가구가 아니라서 필요성을 못 느끼겠네요``로 응답하는 회귀 사례를 막기 위함이다
- 시스템 프롬프트 본문은 `prompts/system_prompt.txt` 외부 파일에 보관한다(라운드 B4). 사용자는 본 파일을 편집하거나 `interview.system_prompt_path` 설정으로 다른 파일을 가리켜 도메인 맞춤 톤/지침을 적용할 수 있다. 템플릿에는 `{persona_json}`과 `{product}` 두 placeholder가 반드시 포함되어야 하며, 누락 또는 파일 부재 시 ConfigError로 차단된다(에러 메시지에 경로와 조치 안내 포함)

데이터셋의 실제 컬럼명은 추측하지 않는다. 구현 단계 첫 게이트(§5.10)에서 `ds['train'].column_names` 출력을 확인한 후 위 묶음과 매핑한다. 매핑 결과를 `config.yaml`의 `dataset.field_map` 섹션에 기록해 어디서든 같은 매핑을 사용한다.

v1.1.0부터 사용자는 페르소나를 두 가지 방식으로 고를 수 있다. `--filter` + `--n` + `--seed` 조합은 시드 고정 샘플링이고, `--persona-id UUID` 다중 지정은 명시 페르소나 직접 매칭이다. `--persona-id`를 쓰면 `--n`과 `--seed`는 무시되며 입력한 ID 순서대로 인터뷰가 실행된다. 같은 페르소나 표본에 다른 product/questions로 비교 인터뷰를 돌릴 때(시드 고정 샘플링은 시드 충돌이 있을 수 있으므로) 사용한다. `--filter`와 `--persona-id`를 함께 지정하면 ID 매칭 후 추가로 필터를 통과한 row만 채택한다(교집합). 일부 ID가 데이터셋에 없으면 누락된 ID 목록을 ConfigError 메시지에 담아 즉시 차단한다.

### 5.3. 답변 포맷

본 도구는 답변을 두 단계로 받는다.

- 1단계 자연어 응답: 멀티턴 인터뷰. 시스템 프롬프트의 지침("2-4문장 간결, 솔직한 거절 허용")을 따른다
- 2단계 구조화 요약: 인터뷰 종료 후 별도의 single-turn 프롬프트로 같은 모델에게 1단계 messages 전체를 입력하고 정해진 JSON 스키마(§5.4의 `structured_summary`)를 출력하도록 요청한다. 본 단계는 모델 응답을 그대로 JSON 파싱하고, 파싱 실패 시 1회 retry 후에도 실패하면 `structured_summary: null`로 record를 저장한다

### 5.4. 결과 JSON 스키마

인터뷰 결과 파일은 record 배열을 포함한다. 한 record는 페르소나 1명에 대한 인터뷰 1회를 의미한다.

```json
{
  "interview_id": "string (uuid)",
  "slug": "korea-persona-interview",
  "product": "string",
  "questions": ["string"],
  "follow_up_questions": ["string"],
  "model": "string",
  "seed": 42,
  "started_at": "ISO 8601",
  "finished_at": "ISO 8601",
  "config_snapshot": {
    "concurrency": 2,
    "temperature": 0.8,
    "max_tokens": 500,
    "single_turn": false,
    "persona_fields": ["base", "summary"]
  },
  "records": [
    {
      "persona_id": "string",
      "persona_meta": {
        "name": "string | null",
        "gender": "string",
        "age": 0,
        "region": "string",
        "occupation": "string",
        "marital": "string",
        "education": "string",
        "family_type": "string | null",
        "housing_type": "string | null",
        "raw": {}
      },
      "started_at": "ISO 8601",
      "finished_at": "ISO 8601",
      "status": "completed | refused | failed | drift",
      "messages": [
        {"role": "system | user | assistant", "content": "string"}
      ],
      "raw_responses": [
        {"question_index": 0, "response": "string", "latency_ms": 0, "retry_count": 0}
      ],
      "structured_summary": {
        "intent": "positive | neutral | negative",
        "acceptable_price_signal": "cheap | fair | expensive | null",
        "willingness_to_pay": 39900,
        "willingness_to_pay_currency": "KRW",
        "rejection_reasons": ["string"],
        "one_line": "string"
      },
      "flags": {
        "persona_drift": false,
        "auto_follow_up_used": false,
        "refusal_detected": false,
        "truncated": false,
        "parse_failed": false
      },
      "error": null
    }
  ]
}
```

스키마 미세 조정 근거는 데이터셋 실제 컬럼 확인 결과(TDD §1)를 반영한 결정이다. `persona_meta.name`은 데이터셋에 별도 이름 컬럼이 없어 v1에서 `null`을 채택한다. `persona_meta.marital`과 `persona_meta.education`은 데이터셋의 `marital_status`, `education_level` 컬럼을 분석 가치를 위해 보존한다. `flags.truncated`는 멀티턴 누적 컨텍스트가 토큰 윈도우(8000)를 초과해 가장 오래된 페어를 제거한 경우를 표시한다(ADR-001 §2, TDD §7). `flags.parse_failed`는 단일턴 모드 응답에서 번호 파싱이 실패해 fallback으로 마지막 question에 통째 텍스트를 저장한 경우를 표시한다(라운드 B1 추가).

v1.1.0에서 schema_version을 1에서 2로 올렸다. 변경 사항은 두 가지다. 첫째, `structured_summary.acceptable_price_signal`을 신설했다. `cheap`/`fair`/`expensive`/`null` 네 값 중 하나가 들어가며, 인터뷰 본문에 명시 숫자가 없어도 정성 가격 신호를 모든 record에 가능한 한 채운다. 둘째, `structured_summary.willingness_to_pay`의 의미를 좁혔다. v1에서는 정성 신호와 명시 숫자가 모두 들어갈 수 있었지만 v2에서는 명시 숫자만 정수로 들어가고 그렇지 않으면 `null`이다. v1 JSON은 `load_interview_json` 단계에서 `acceptable_price_signal=null`로 채워 호환 로드된다. resume 모드(§5.1)에서 생성되는 결과 JSON은 `meta_extra.previous_run_id`에 입력 JSON의 `interview_id`를 함께 박는다.

JSON 스키마 결정 근거는 아래와 같다.

- `slug` 필드를 record가 아닌 인터뷰 단위에 둔다. 한 인터뷰 안의 모든 record는 같은 도구로 생성되었음이 자명하다
- `messages`와 `raw_responses`를 분리한다. `messages`는 멀티턴 누적 컨텍스트, `raw_responses`는 질문 단위 분석(지연 시간, retry)에 쓴다
- `status` 값은 4종으로 한정한다. `completed`(정상), `refused`(모델이 응답 거부), `failed`(모든 retry 실패), `drift`(페르소나 깨짐 플래그)이다. `drift`는 `flags.persona_drift`와 중복으로 보일 수 있으나, 정량 집계에서 한 줄로 필터링할 수 있도록 status에도 반영한다

### 5.5. 필터 DSL

명령행 인자 `--filter "key1:value1,key2:value2"` 형식으로 페르소나 필터를 지정한다.

- 지원 키는 `age`, `gender`, `region`, `subregion`, `occupation_keyword`로 한정한다. v1에서 더 이상 확장하지 않는다
- 값 표기는 아래와 같다
  - `age:25-39` 형식의 범위, `age:30` 형식의 단일값
  - `gender:F` 또는 `gender:M`은 사용자 친화 별칭이다. 내부적으로 데이터셋 표기 `여자`/`남자`로 매핑한다. `여성`/`남성` 또는 `여자`/`남자` 직접 입력도 모두 허용한다
  - `region:서울특별시`는 사용자 친화 별칭이다. 내부적으로 데이터셋 표기 `서울`로 매핑한다. 17개 시도 별칭 매핑 표는 TDD §1.6 참고
  - `subregion:강남구`(시군구 단위, `district` 컬럼에 대해 부분 매칭)
  - `occupation_keyword:개발자`(부분 문자열 포함 매칭)
- 결합 규칙은 아래와 같다
  - 서로 다른 키는 AND로 결합한다(`age:25-39,region:서울특별시`는 둘 다 만족)
  - 같은 키를 반복하면 OR로 결합한다(`region:서울특별시,region:경기도`는 둘 중 하나만 만족하면 통과)
- 별칭 매핑 메커니즘은 `config.yaml`의 `dataset.field_map`(컬럼 키 매핑), `dataset.gender_aliases`(성별 별칭), `dataset.province_aliases`(시도 별칭)에 박는다. 데이터셋 표기 변경 시 코드 변경 없이 yaml만 갱신하면 된다
- 시드 옵션 `--seed N`을 제공한다. 같은 시드, 같은 필터, 같은 데이터셋 버전이면 항상 같은 페르소나 표본을 반환한다(`random.Random(seed).sample` 기반)
- 필터 결과 수가 요청 수 N보다 적으면 종료 코드 2와 함께 "필터 결과 X명, 요청 N명"이라는 안내를 출력하고 인터뷰를 시작하지 않는다

### 5.6. 리포트 정량 지표

리포트는 아래 정량 지표를 마크다운으로 생성한다.

- 의향률: `structured_summary.intent` 값별 비율(positive, neutral, negative). 막대 텍스트 차트로 표기한다
- 가격 수용가: `willingness_to_pay`의 중앙값, IQR(25퍼센타일/75퍼센타일), 최소/최대, null 비율. 10개 구간 히스토그램(텍스트 막대)을 함께 생성한다
- 거절 사유 빈도: `rejection_reasons` 배열을 펼쳐서 빈도 상위 N개(기본 N=10)를 표로 출력한다
- 코호트별 의향률: 연령대(20대/30대/40대/50대/60대 이상), 지역(시도), 성별 각 축에서 의향률을 비교 표로 출력한다(샘플이 3명 미만인 셀은 "표본 부족"으로 마스킹)

샘플 부족 마스킹 임계값을 3명으로 둔 근거는 아래와 같다. v1의 실제 사용 패턴은 인터뷰 10-30명 규모이므로 임계값을 5명으로 두면 코호트 셀 대부분이 마스킹되어 코호트 비교 자체가 빈 표로 출력된다. 3명도 비율 추정의 신뢰 구간이 넓다는 한계가 있으므로 리포트 코호트 섹션 상단에 "셀별 표본 수가 작아 차이는 참고용"이라는 주의 문구를 함께 출력한다.

### 5.7. 리포트 정성 인사이트

리포트는 아래 정성 인사이트를 같은 모델로 생성한다.

- 공통 반응: 페르소나 다수가 비슷하게 보인 반응을 5개 이내 항목으로 요약한다
- 인사이트: 사업 아이템 의사결정에 활용 가능한 시사점을 5-10개 도출한다(많을수록 좋은 게 아니다. 5-10개 범위로 강제한다)
- 페르소나 군별 차이: 코호트(연령대/지역/성별)별로 의향과 거절 사유의 차이를 자유 서술로 정리한다(셀별 표본 3명 이상에 한해 언급한다)

정성 인사이트 생성 프롬프트는 정량 지표를 함께 입력으로 넣어 모델이 숫자와 어긋나는 인사이트를 생성하지 않도록 유도한다.

### 5.8. 실패 모드

도구가 처리해야 할 실패 모드와 대응은 아래와 같다.

- 페르소나 깨짐 감지: 응답 텍스트의 영어 단어 비율이 30%를 초과하거나, 페르소나의 연령/성별/지역/거주 형태(`family_type`)와 명백히 모순되는 자기소개가 발견되면 `flags.persona_drift: true`와 `status: "drift"`로 기록한다. v1.1.0부터 연령/성별/지역 축은 거주 형태 축과 동일한 정밀도로 격상되었다. 같은 문장(`.`/`!`/`?` boundary) 안에서 1인칭 주어(`저는`/`나는`/`제가`/`내가`/`난`)와 단언/계사가 함께 등장할 때만 trigger한다. 부정문(`아니`/`아닌`/`아닙`)이 같은 문장에 있으면 정합한 답변으로 보고 제외하고, 3인칭 일반화 표현(`다른 사람들은`/`보통 사람`/`남들`/`타인`)이 같은 문장에 있어도 제외한다. 거주 형태 축은 1인칭 주어 + 거주 동사 정밀 정규식만 매칭하며 `혼자 사시는 분들에겐` 같은 3인칭, `혼자서 끼니를 해결` 같은 행동 표현, 응답에 우연히 들어온 product 키워드(`1인 가구용`)는 trigger에서 제외한다. 영어 비율 분모에서 페르소나 직업명에 등장하는 영문 토큰(`IT 컨설턴트`, `UX 디자이너`)은 옵션(`interview.occupation_english_whitelist: true`, 기본 ON)에 따라 제외한다. v1.1.0에 도입된 `interview.llm_drift_review: true`(기본 OFF) 옵션은 휴리스틱이 drift 의심으로 판정한 record에 한해 1-token LLM 호출로 재판정한다. ok 판정이면 drift 플래그를 해제해 false positive를 줄이며, drift 판정이거나 호출 실패면 보수적으로 drift 라벨을 유지한다(TDD §8.2 참조)
- 짧은 답변: 답변 길이가 20자 미만이면 자동 follow-up 1회 시도 후에도 짧으면 그대로 record에 기록한다. 자동 follow-up 사용 여부는 `flags.auto_follow_up_used`에 기록한다
- 모델 거부: 응답에 거부 키워드(예: "답변할 수 없습니다", "I cannot", "I'm sorry, but")가 포함되면 `flags.refusal_detected: true`와 `status: "refused"`로 기록한다. retry는 시도하지 않는다(같은 거부가 반복될 가능성이 높다)
- 토큰 루프 가드: 동일 토큰/구절이 max_tokens 한도에 가까워질 때까지 반복되는 응답을 감지하면 해당 record를 `status: "failed"`로 기록한다. OpenAI gpt-4o-mini에서는 거의 발생하지 않지만 회귀 안전망으로 둔다
- 호출 실패: 타임아웃 120s, HTTP 5xx, 연결 실패, 429(rate limit) 시 지수 백오프(1s, 2s, 4s)로 최대 3회 retry한다. 최종 실패 시 `status: "failed"`와 `error.message`를 기록하고 다음 페르소나로 진행한다
- 인증 실패: HTTP 401(API 키 무효 또는 만료)은 즉시 실패하고 인터뷰 전체를 중단한다. 종료 코드 1과 함께 "OpenAI API 키가 유효하지 않습니다"를 출력한다
- 필터 결과 0건: 인터뷰 시작 전에 종료 코드 2와 함께 안내한다(§4.2)
- API 키 미설정 또는 OpenAI 도달 불가: 인터뷰 시작 직전 헬스체크에서 감지하면 인터뷰를 시작하지 않는다(§4.1)

### 5.9. CLI 서브커맨드

CLI는 4개 서브커맨드를 제공한다. 매크로 명령(예: `run-all`)은 v1에서 제공하지 않는다. 각 단계가 독립적으로 검증 가능해야 한다는 원칙이다.

| 명령 | 설명 | 주요 인자/옵션 | 종료 코드 |
| --- | --- | --- | --- |
| `healthcheck` | LLM API 응답과 모델 가용성 확인 | `--provider`(openai/anthropic), `--base-url`(provider별 default), `--model`(일회성 모델 ID 덮어쓰기, 기본 config.yaml의 `llm.model`) | 0 정상, 1 키 미설정/401/서버 도달 불가 |
| `list-personas` | 필터 결과 미리 보기 | `--filter`, `--persona-id`(다중, 명시 uuid), `--limit`(기본 20), `--seed` | 0 정상, 2 결과 0건 |
| `interview` | 배치 인터뷰 실행 | `--product`(필수, 2000자 상한), `--questions`(다중, 필수, 각 2000자 상한), `--filter`, `--persona-id`(다중, 명시 uuid 페르소나 고정), `--n`(기본 10), `--seed`, `--concurrency`(기본 4, 1-10), `--persona-fields`, `--follow-up`(다중), `--single-turn`, `--dry-run`, `--output`(기본 `outputs/`), `--report/--no-report`(기본 `--report`. 인터뷰 종료 후 마크다운 리포트 자동 생성. `--no-report`는 외부 분석 도구로 JSON만 받을 때 사용. `--dry-run`은 본 옵션과 무관하게 JSON/리포트 모두 미생성), `--resume PATH`(이전 결과 JSON에서 status=failed record만 재시도. `meta_extra.previous_run_id`로 link), `--provider`, `--base-url`, `--model`(일회성 모델 ID 덮어쓰기) | 0 정상, 1 서버 오류, 2 표본 부족, 3 부분 실패(완료된 record 50% 미만) |
| `report` | 결과 JSON에서 리포트 생성 | 인자 1개(JSON 파일 경로), `--top-n`(기본 10), `--include-drift`(드리프트 record 포함), `--output-dir`(기본 입력 JSON과 같은 디렉토리), `--provider`, `--base-url`, `--model`(정성 인사이트 호출의 일회성 모델 ID 덮어쓰기), `--insight-model`(인사이트 호출만 다른 모델로. 인터뷰는 mini, 인사이트는 4o/sonnet 류 흐름) | 0 정상, 1 입력 파일 오류, 2 정상 record 0건 |

### 5.10. 사용자 상호작용 게이트(개발 단계)

코드 작성 단계에서 아래 게이트를 거친다. 이 게이트는 사용자에게 직접 확인을 요청하는 단계로, 잘못된 가정을 코드에 박는 사고를 막는다.

- 게이트 1(OpenAI API 키 설정 확인): `llm_client.py`를 작성하고 sanity 호출(`/v1/models` GET, "안녕" 메시지 POST)을 한 시점에 사용자에게 "`OPENAI_API_KEY` 환경변수를 설정해 주세요. 사용할 모델 ID도 알려주세요(기본 `gpt-4o-mini`)" 안내. 응답이 정상 200으로 확인되기 전까지 다음 단계 진행을 막는다. 키 누락은 401, 모델 ID 오기재는 404로 즉시 감지한다
- 게이트 2(데이터셋 컬럼 구조 확인): `load_personas.py`에서 데이터셋을 처음 로드한 직후 `ds['train'].column_names`와 첫 record 1개를 콘솔 출력. 사용자가 컬럼명과 의미를 확인하고 `config.yaml`의 `dataset.field_map`에 매핑을 기록한 뒤에 필터 함수와 시스템 프롬프트 주입 코드를 작성한다

게이트 위반(컬럼 추측 코딩, 서버 미확인 진행)은 PR 차단 사유로 본다.

## 6. 비기능 요구사항

### 6.1. 성능

- 100명 인터뷰 1회를 5-10분 이내에 완료한다(질문 5개, 동시성 4 가정). gpt-4o-mini 기준 한 턴 응답이 약 1-3초로 추정되며 동시성 4-10 구간은 OpenAI rate limit 여유 안에서 처리량을 크게 끌어올린다. v1.0의 30분 SLO는 로컬 MLX 시절 보수 추정치였고 v1.x OpenAI 백엔드에서는 5-10분 SLO로 갱신한다
- 데이터셋 첫 로드는 5분 이내에 완료한다(`~/.cache/huggingface` 캐시 활용). 두 번째 실행부터는 30초 이내에 시작한다
- 동시성 기본값은 4로 둔다. `asyncio.Semaphore(4)` 기준이다. 사용자가 `--concurrency` 옵션으로 1-10 범위에서 조정할 수 있다. 11 이상은 차단한다(OpenAI rate limit 부하 방지). v1.0 시절 1-3 범위는 로컬 MLX 메모리 가드였고, OpenAI 백엔드에서는 메모리 가드가 무관해 1-10으로 상향했다
- v1.1.0부터 OpenAI 호환 streaming 응답을 옵션(`llm.streaming: true`, 기본 OFF)으로 지원한다. 첫 토큰 시간이 빨라지지만 일부 호환 서버는 SSE 형식이 미묘하게 다르므로 default OFF를 유지한다. provider=anthropic이나 MCP sampling 경로에서는 무시된다

### 6.2. 신뢰성

- 호출 실패 시 지수 백오프 retry 최대 3회(`1s, 2s, 4s`)
- 타임아웃 120초(로컬 추론 변동성 흡수)
- 한 페르소나 record가 실패해도 다른 페르소나 인터뷰는 계속 진행한다
- 모든 인터뷰 결과는 JSON 파일로 저장하고 부분 실패도 함께 기록한다(재분석 가능)

### 6.3. 보안과 개인정보

- OpenAI API 키는 환경변수 `OPENAI_API_KEY`(또는 fallback으로 `KPI_OPENAI_API_KEY`)에서만 로드한다. 코드/설정/로그에 하드코딩하지 않는다(security.md §1)
- `Authorization: Bearer ${OPENAI_API_KEY}` 헤더로 API 호출을 인증한다. 로그 출력 시 `Bearer sk-***` 형식으로 마스킹한다
- 외부 텔레메트리와 외부 분석 서비스 의존은 일체 부재한다
- 외부 LLM API 의존은 OpenAI Chat Completions API 1종이다. 다른 외부 서비스는 호출하지 않는다
- 사업 아이템 본문(`--product`)과 페르소나 정보(인구 통계, 자유 서술)는 OpenAI 서버로 송신된다. 사용자가 도구 실행 전에 이 사실을 인지해야 한다. 민감한 사업 아이템(미공개 IP, 개인정보 포함)은 입력하지 않거나 추상화하여 입력하기를 권고한다. 본 사실은 README와 도구 첫 실행 메시지에서 명시한다
- OpenAI의 데이터 이용 정책은 OpenAI 약관에 따른다(API 호출 데이터는 기본적으로 모델 학습에 사용되지 않으나 30일간 abuse monitoring 목적으로 보관됨). 본 도구는 OpenAI 약관 동의 책임을 사용자에게 둔다
- 인터뷰 결과 JSON에 사용자 개인 식별 정보를 기록하지 않는다(합성 페르소나만 기록)
- 인터뷰 결과 JSON과 로그 파일은 로컬 `outputs/` 디렉토리에만 저장된다(`.gitignore` 처리)

### 6.4. 라이선스와 크레딧

- README와 모든 결과 JSON, 리포트 마크다운에 데이터셋 라이선스(CC BY 4.0)와 출처(`nvidia/Nemotron-Personas-Korea`)를 명시한다
- OpenAI Chat Completions API는 출처 표기 의무가 없다. 다만 결과 리포트 메타에 사용한 모델 ID(예: `gpt-4o-mini`)와 백엔드 출처를 기록하여 재현성과 비교 분석을 돕는다
- 결과 리포트 푸터에 합성 페르소나 기반이라는 사실, 실제 인구 통계와 다를 수 있다는 한계를 명시한다

### 6.5. 호환성과 환경

- Python 3.12에서 동작한다(`.python-version` 고정). 3.10 이상에서도 동작 가능하지만 v1 검증 환경은 3.12다
- 운영 체제 제약은 없다. macOS, Linux, Windows 모두에서 동작한다
- LLM provider는 세 가지를 지원한다(ADR-003)
  - OpenAI Chat Completions API(기본)
  - Anthropic Messages API(`provider=anthropic`)
  - OpenAI 호환 로컬 서버(mlx_lm.server, vLLM, llama.cpp 등). `provider=openai` + `--base-url` override
- MCP 서버 진입점은 sampling 전용이다. host agent의 LLM에 위임하며 server-side 키가 불필요하다
- 인터넷 접근은 직접 호출 provider(OpenAI/Anthropic)와 데이터셋 첫 로드 시 Hugging Face Hub에 한해 필요하다. 로컬 LLM 또는 MCP sampling 경로는 인터넷 없이도 인터뷰가 가능하다(데이터셋 캐시 필요)
- 의존성은 `httpx`, `datasets`, `pyyaml`, `tqdm`, `click`, `mcp`로 한정한다. `openai`/`anthropic` SDK는 도입하지 않는다(`dependency.md` §1 leftpad 안티패턴 회피와 직접 통제 목적). `mlx-lm` 의존도 v1에서 제거했다
- 모든 의존성 버전은 `requirements.txt`에 안정 버전으로 고정하고 lock 파일을 함께 커밋한다(`dependency.md` §2)

### 6.6. 관측 가능성

- 표준 출력에 진행률(tqdm)과 단계별 INFO 로그를 출력한다
- 로그는 구조화된 형태(JSON Lines)로 `outputs/logs/run_{timestamp}.jsonl`에도 동시 기록한다
- 민감 정보(사용자가 `--product`에 적은 사업 아이템 본문)는 로그 본문에 그대로 기록하지 않고 첫 30자 + 길이 형태로 마스킹한다
- API 키는 로그에 절대 기록하지 않는다. `Authorization` 헤더 출력이 필요한 경우 `Bearer sk-***` 형식으로 마스킹한다(security.md §1, logging.md §2)
- v1.1.0부터 페르소나 식별자 보호를 강화했다. `persona_id`는 sha256 hex prefix 12자(`persona_id_hash`) 형태로 로그에 박힌다. 동일 페르소나라는 사실은 유지되지만 원본 uuid 추적은 차단된다. 인구통계 필드(연령/성별/지역)는 INFO에서 DEBUG로 격하되어 기본 운영 환경에서는 식별 가능한 인구통계 자체가 노출되지 않는다
- 토큰 사용량(prompt/completion/cached)을 인터뷰 종료 시 콘솔에 한 줄 노출하고 결과 JSON `meta_extra.usage`, 리포트 마크다운 헤더 표에도 함께 박는다. USD 비용 추정은 v1.0.0 시점에 제거했다. 단가 표 갱신 부담과 추정-실제 청구 차이가 도구 신뢰성을 해친다는 판단이며 사용자가 토큰 카운트를 자신의 provider 청구서와 직접 대조하는 흐름으로 이관한다

### 6.7. 접근성과 출력

- CLI 출력은 ANSI 컬러 코드를 사용한다. `--no-color` 옵션으로 비활성화 가능하다
- 모든 사용자 안내 문구와 에러 메시지는 한국어로 작성한다(에러 코드, 식별자 등 영문 관용어는 그대로)
- 리포트 마크다운은 옵시디안, GitHub, VS Code 미리보기에서 모두 정상 렌더된다(특수 문자, em dash 사용 금지)
- 외부 에이전트(Claude Code, Cursor, Codex 등) 통합용으로 root group에 `--json` 옵션을 둔다. 본 옵션을 켜면 tqdm 진행률, ANSI 컬러, [OK]/[INFO]/[ERR] 한국어 메시지를 모두 끄고 stdout에 결과 JSON 한 덩어리만 출력한다. 에러도 `{"error": {"code", "message", "exit_code"}}` 형태로 stdout에 내보낸다. logging JSON Lines는 그대로 stderr와 `outputs/logs/run_*.jsonl` 두 곳에 남는다

### 6.8. 테스트

- 단위 테스트: 필터 DSL 파서, 페르소나 깨짐 감지 휴리스틱, 거부 키워드 감지, 리포트 정량 집계
- 통합 테스트: 모킹된 LLM 클라이언트로 1명 인터뷰 → JSON 저장 → 리포트 생성 E2E
- 실제 MLX 서버 의존 테스트는 수동 smoke 테스트로 분리한다(`tests/manual/`)

## 7. 우선순위(MoSCoW)

자동 follow-up과 페르소나 깨짐 감지는 합성 인터뷰 신뢰도의 핵심 가드레일이라 v1 Should로 상향한다. 두 기능 없이 인터뷰를 돌리면 짧은 답변과 페르소나 이탈 record가 정량 통계를 오염시켜 도구 자체의 활용 가치가 떨어진다.

### 7.1. Must

- `healthcheck` 명령
- `list-personas` 명령(필터 DSL 포함)
- `interview` 명령의 멀티턴, 페르소나 주입(기본 묶음), 시드 고정, 동시성 2
- 결과 JSON 저장(§5.4 스키마)
- 필터 DSL(`age`, `gender`, `region`, `subregion`, `occupation_keyword`, AND/OR 결합)
- MLX 서버 헬스체크 자동 수행(인터뷰 시작 직전)
- 데이터셋 컬럼 구조 확인 게이트(§5.10)

### 7.2. Should

- `report` 명령의 정량 지표(의향률, 가격 수용가 통계, 거절 사유 빈도, 코호트 비교)
- `report` 명령의 정성 인사이트(공통 반응, 인사이트 5-10개, 코호트 차이)
- 페르소나 토글(`--persona-fields`)
- 구조화 요약 2단계 흐름
- 자동 follow-up(짧은 답변 감지 1회)
- 페르소나 깨짐 감지(영어 비율 + 정면 모순 휴리스틱)

### 7.3. Could

- 사용자 정의 follow-up(`--follow-up`)
- 모델 거부 감지(거부 키워드 매칭)
- `--single-turn`, `--dry-run` 옵션

### 7.4. Won't(v1 제외)

- GUI, 웹 인터페이스
- 한국어 외 다국어 페르소나/모델
- 외부 페르소나 입력(사용자 정의 JSON 페르소나 import)
- 결제, 구독 기능
- 클라우드 LLM 백엔드(OpenAI, Anthropic 등)
- 진짜 인구 통계 분포 가중 표집(현재는 단순 랜덤 샘플)
- 인터뷰 결과 실시간 스트리밍 출력
- 답변 음성 합성, 오디오 출력
- 실시간 대화 모드(사용자가 한 줄씩 직접 추가 질문)

## 8. 제외 범위(Out of Scope)

- 진짜 한국인 응답자를 모집하는 모듈, 패널 관리, 보상 처리
- 합성 페르소나 추가 학습/파인튜닝
- 결과 데이터의 장기 저장소(DB), 다인 협업, 권한 관리
- 인터뷰 결과를 외부에 자동 송신하는 기능(Slack, 이메일 등)
- 한국 외 국가 페르소나 데이터셋 지원
- 사업 아이템 설명을 자동 생성/개선하는 기능
- 모델 라우팅(여러 모델 자동 비교)

## 9. 성공 지표

- 기능 완결성: 헬스체크 → 1명 dry-run → 10명 배치 → 리포트 생성을 끊김 없이 진행할 수 있다
- 처리 성능: 100명 인터뷰 1회를 5-10분 이내에 완료한다(질문 5개, 동시성 4 기준. v1.0의 30분 SLO를 OpenAI 백엔드에 맞춰 갱신)
- 페르소나 일관성: `flags.persona_drift: true` 비율 5% 이하(샘플 200명 이상에서 측정)
- 결과 활용성: 같은 시드, 같은 필터, 같은 질문으로 두 번 실행 시 두 결과의 의향률 차이가 ±10%p 이내(모델 응답의 stochasticity는 인정)
- 신뢰성: 100명 배치에서 `status: "failed"` 비율 2% 이하
- 토큰 사용량: 100명 인터뷰 1회 종료 시 콘솔과 결과 JSON에 prompt/completion/cached 카운트가 정상적으로 노출된다. 사용자가 카운트와 자신의 provider 청구서를 대조해 비정상 누적을 즉시 파악할 수 있다

## 10. 리스크와 의존성

### 10.1. 모델 페르소나 이탈

- 위험: 모델이 페르소나 정보를 무시하고 모델 자신의 디폴트 톤(중립, 영어 혼용)으로 답변하는 경우가 잦다
- 완화: 시스템 프롬프트의 지침 강화(연령/지역에 맞는 말투 명시), `temperature` 0.8로 변동성 확보, 페르소나 깨짐 감지로 정량 지표에서 자동 제외

### 10.2. 데이터셋 컬럼 스키마 변동

- 위험: 엔비디아가 데이터셋을 갱신하면 컬럼명, 값 표기(성별 `F`/`Female`/`여성`)가 달라질 수 있다
- 완화: 컬럼 매핑을 `config.yaml`에 분리, 코드에는 매핑 키만 사용, 첫 로드 시 컬럼 검증 단계 추가, 게이트 2에서 사용자 확인

### 10.3. 100만 레코드 초기 로드

- 위험: 첫 로드 시 5분 이상 걸리고 메모리 사용량이 크다
- 완화: `datasets`의 캐시 활용, 필터 후 `select(indices)`로 메모리 점유 최소화, 한 번에 모두 메모리에 적재하지 않고 스트리밍 로드 옵션 사용 검토

### 10.4. OpenAI API 키 설정과 유효성

- 위험: 사용자가 `OPENAI_API_KEY`를 설정하지 않거나, 만료된 키, 사용량 한도 초과(429), 결제 정보 누락 등으로 호출이 실패할 수 있다. 또한 OpenAI API 자체의 가용성 장애(공식 status 페이지 기준 분기당 수회)와 네트워크 단절도 가능성이 있다
- 완화: 매 명령 시작 직전 헬스체크 수행, 친절한 한국어 안내 메시지로 사용자가 즉시 조치 가능하도록 한다(키 발급 URL 안내, 401/429 메시지 분기). 429는 지수 백오프로 재시도 후 최종 실패 시 record `status: "failed"`로 격리한다

### 10.5. LLM API 토큰 누적

- 위험: 멀티턴 토큰 누적, 토글 옵션 다수 사용, 동시성 상향, 큰 모델(`gpt-4o`) 선택 시 토큰 사용량이 빠르게 늘어 사용자 provider 청구서가 의도보다 커질 수 있다. v1.0.0 시점부터 USD 비용 추정을 제거했으므로 사용자가 token 카운트로 직접 누적을 모니터링해야 한다
- 완화: gpt-4o-mini 기본값 유지, 동시성 1-10 범위 강제, 멀티턴 토큰 윈도우 truncation 정책 유지(TDD §7), 토글 옵션 기본값을 `summary` 1종으로 제한, 인터뷰 종료 시 콘솔과 JSON에 prompt/completion/cached 토큰 카운트를 노출해 사용자가 즉시 비정상 누적을 발견할 수 있도록 한다. gpt-4o 같은 상위 모델은 사용자가 명시 선택할 때만 사용한다

### 10.6. 사업 아이템 본문 외부 송신

- 위험: `--product`에 적은 사업 아이템 본문과 페르소나 정보가 OpenAI 서버로 송신된다. 사용자가 본 사실을 인지하지 못하면 미공개 IP, 영업 비밀, 개인정보가 외부로 노출될 수 있다
- 완화: README와 도구 첫 실행 메시지에 외부 송신 사실을 명시, 민감 정보 입력 자제 권고. 향후 v1.1에서 `--no-network` 플래그(또는 로컬 백엔드 회귀 옵션)를 검토한다

### 10.7. 합성 페르소나 한계

- 위험: 합성 페르소나는 실제 인구 통계와 일치하지 않을 수 있다. 결과를 진짜 인터뷰처럼 신뢰하면 의사결정 오류로 이어진다
- 완화: 모든 결과 리포트와 README에 합성 데이터 한계를 명시, 실제 인터뷰 직전 단계의 가설 검증 도구로 포지셔닝

### 10.8. 모델 변경 가능성

- 위험: 기본 모델 `gpt-4o-mini`가 deprecated되거나 사용자가 다른 OpenAI 모델(`gpt-4o`, `gpt-4-turbo` 등)을 선택할 수 있다. 모델별로 응답 품질, 토큰당 단가, rate limit이 달라 품질/사용량 트레이드오프가 발생한다
- 완화: 모델 ID는 `config.yaml`의 `llm.model`(기본)과 CLI `--model` 옵션(일회성)으로만 변경하도록 설계한다. v1.x에서는 "비밀=env, 기본=yaml, 일회성=CLI" 정책으로 단순화한다. README에 대표 모델별 품질 가이드를 둔다(예: gpt-4o-mini는 기본값, gpt-4o는 품질 우선 시 검토). gpt-4o-mini 페르소나 깨짐 비율 측정 결과가 5%를 초과하면 ADR-002 supersede로 모델 상향을 검토한다

### 10.9. provider별 응답 품질 차이

- 위험: ADR-003 채택으로 OpenAI/Anthropic/로컬 LLM/MCP sampling 네 경로가 활성화되었지만 페르소나 일관성과 drift 비율은 `gpt-4o-mini` 기준으로만 검증된 상태다. 다른 provider 또는 모델로 전환했을 때 페르소나 추종력이 달라질 수 있다
- 완화: README "Choosing a model" 섹션에 검증 기준을 명시하고, 새 provider 도입 시 작은 표본(10-20명)으로 drift 비율을 먼저 측정하도록 안내한다. v1.1 백로그에 provider별 검증 보고서 작업을 등록한다

데이터셋 실제 컬럼명 확정은 dev-planner가 TDD 작성 전에 `datasets.load_dataset(..., streaming=True)`로 1샘플만 로드해 컬럼 키와 값 표기를 직접 확인한 뒤 TDD에 매핑값(예: `gender_field: sex`, `region_field: residence_region`)까지 박는 방식으로 처리한다. 게이트 2(§5.10)는 구현 단계 휴먼 검증으로 그대로 유지하며, 두 단계가 중복되어도 비용이 거의 없으므로 안전망으로 둘 다 운영한다.

데이터셋 컬럼 키와 값 표기는 dev-planner가 viewer 직접 조회로 확인 후 TDD §1, `config.yaml`의 `dataset.field_map`에 박았다. 게이트 2(§5.10)는 휴먼 검증 안전망으로 그대로 유지한다.
