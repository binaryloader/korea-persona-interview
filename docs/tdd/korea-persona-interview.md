# TDD: korea-persona-interview

본 문서는 PRD `docs/prd/korea-persona-interview.md`에 정의된 한국인 합성 페르소나 인터뷰 CLI 도구의 기술 설계 문서다. 본 도구는 단일 도메인(Python 비동기 CLI)이라 클라이언트/서버 도메인 분할이 없다. 따라서 도메인별 섹션은 `## 기술 설계` 하위에서 모듈/계층 단위로 분해한다. 작성 원칙은 architecture.md(계층 분리, DI, SOLID), api-design.md(LLM HTTP 계약), security.md(시크릿/마스킹), logging.md(JSON Lines + 마스킹), error-handling.md(예외 계층), dependency.md(의존성 핀, leftpad 회피), markdown.md(헤딩 번호) 규칙을 따른다.

## 기술 설계

### 1. 데이터셋 실제 컬럼 매핑(사전 단계 결과)

PRD §5.10 게이트 2와 별개로, dev-planner가 TDD 작성 전에 Hugging Face 데이터셋 viewer를 직접 조회하여 컬럼 키와 값 표기를 확인했다. 결과는 아래와 같다.

#### 1.1. 확인된 컬럼 키 전체 목록(26개)

데이터셋 식별자는 `nvidia/Nemotron-Personas-Korea`, split은 `train`, 총 1,000,000 행이다.

- 식별자: `uuid`
- 7가지 페르소나 자유 서술: `professional_persona`, `sports_persona`, `arts_persona`, `travel_persona`, `culinary_persona`, `family_persona`, `persona`(요약)
- 부가 자유 서술: `cultural_background`, `skills_and_expertise`, `skills_and_expertise_list`, `hobbies_and_interests`, `hobbies_and_interests_list`, `career_goals_and_ambitions`
- 인구 통계: `sex`, `age`, `marital_status`, `military_status`, `family_type`, `housing_type`, `education_level`, `bachelors_field`, `occupation`
- 지역: `district`, `province`, `country`

#### 1.2. 1샘플 값 예시(이름 마스킹 적용)

데이터셋 본문에 등장하는 인물 이름은 본 문서의 마스킹 규칙(`성O이름끝글자`)을 적용하여 표기한다.

| 컬럼 | 값 |
| --- | --- |
| uuid | `03b4f36a18e6469386d0286dddd513c8` |
| sex | `남자` |
| age | `74` |
| marital_status | `배우자있음` |
| military_status | `비현역` |
| family_type | `배우자와 거주` |
| housing_type | `아파트` |
| education_level | `초등학교` |
| bachelors_field | `해당없음` |
| occupation | `하역 및 적재 관련 단순 종사원` |
| province | `광주` |
| district | `광주-서구` |
| country | `대한민국` |
| persona | `광주 서구에서 평생을 보내며 ...` |
| professional_persona | `전O태 씨는 광주 서구의 하역 현장에서 수십 년간 ...` |
| sports_persona | `주말마다 무등산 자락을 느릿느릿 걸으며 ...` |
| arts_persona | `거실 소파에 깊숙이 파묻혀 텔레비전에서 ...` |
| travel_persona | `아내와 함께 전국의 역사 유적지를 찾아다니며 ...` |
| culinary_persona | `일주일에 한 번 배달 짜장면과 탕수육을 ...` |
| family_persona | `전·월세 아파트에서 평생의 동반자인 아내와 ...` |

#### 1.3. PRD `persona_meta` 필드 매핑

PRD §5.4 결과 JSON 스키마의 `persona_meta` 필드는 데이터셋의 실제 컬럼에 아래와 같이 매핑한다.

| `persona_meta` 필드 | 데이터셋 컬럼 | 비고 |
| --- | --- | --- |
| `name` | (없음) | 데이터셋에 별도 이름 컬럼이 없다. `professional_persona` 본문의 첫 문장에 등장하는 이름을 추출하거나, 추출 실패 시 `null`로 둔다. v1은 추출 실패 시 `null` 채택(휴리스틱 추출 비용 회피) |
| `gender` | `sex` | 값 표기 `남자`/`여자` 그대로 보존 |
| `age` | `age` | int64 그대로 |
| `region` | `province` | 17개 짧은 표기(`서울`, `경기`, `광주`, `충청남` 등) |
| `subregion` | `district` | `광주-서구`, `서울-강남구` 형식의 시도-시군구 결합형 |
| `occupation` | `occupation` | 그대로 |
| `marital` | `marital_status` | 그대로 |
| `education` | `education_level` | 그대로 |
| `raw` | (전체) | `uuid`를 제외한 raw dict 그대로 보존(분석 시 원본 참조용) |

PRD §5.4의 원본 스키마에는 `marital`, `education` 필드가 없었으나 데이터셋에 풍부한 인구 통계 필드가 있어 분석 가치가 높다고 판단해 `persona_meta`에 추가 보존한다. JSON 스키마는 추가만 하므로 후방 호환성에 영향이 없다(`schema_version=1` 유지).

#### 1.4. 7가지 페르소나 자유 서술 매핑

PRD §5.2의 페르소나 토글 옵션은 아래와 같이 매핑한다. `--persona-fields` 옵션은 키워드만 받고 내부에서 컬럼 키로 변환한다.

| 토글 키워드 | 데이터셋 컬럼 | 항상 주입 여부 |
| --- | --- | --- |
| `summary` | `persona` | 기본 묶음 |
| `professional` | `professional_persona` | 토글 |
| `sports` | `sports_persona` | 토글 |
| `arts` | `arts_persona` | 토글 |
| `travel` | `travel_persona` | 토글 |
| `culinary` | `culinary_persona` | 토글 |
| `family` | `family_persona` | 토글 |

기본 묶음은 인구 통계 필드 전부(`sex`, `age`, `marital_status`, `education_level`, `occupation`, `province`, `district`)에 `persona`(요약)을 더한 형태다. 토큰 사용량은 약 200-400자 수준으로 페르소나 일관성 손실을 최소화한다.

#### 1.5. 값 타입 표기 확인 결과

- `sex`: `남자`/`여자`(한국어 2진 표기). PRD §5.5의 필터 예시 `gender:F`/`gender:M`은 데이터셋과 매핑이 필요하다. CLI 파서에서 `F`→`여자`, `M`→`남자`, 그리고 `여자`/`남자` 직접 입력도 모두 허용한다
- `age`: `int64`. PRD §5.5의 `age:25-39`(범위), `age:30`(단일값) 그대로 호환된다
- `province`: `서울`, `부산`, `대구`, `인천`, `광주`, `대전`, `울산`, `경기`, `강원`, `충청북`, `충청남`, `전북`, `전남`, `경상북`, `경상남`, `제주`, `세종`(짧은 표기 17개). PRD §4.2의 필터 예시 `region:서울특별시`는 데이터셋 표기 `서울`로 정규화한다. CLI 파서에서 `서울특별시`→`서울`, `광역시` 접미사 자동 제거, `~도` 접미사 제거 후 매칭하는 정규화 함수를 둔다
- `district`: `광주-서구` 형식이다. 시도가 prefix로 포함되어 있다. PRD §4.2 `subregion:강남구`는 `district`에 대해 `endswith("강남구")` 또는 부분 매칭을 적용한다

#### 1.6. config.yaml의 dataset.field_map 초기값

`config.yaml`에는 매핑을 박아둔다. 데이터셋 갱신 시 코드 변경 없이 매핑만 갱신하면 된다.

```yaml
dataset:
  name: "nvidia/Nemotron-Personas-Korea"
  split: "train"
  field_map:
    name: null            # 데이터셋에 없음. null 처리
    gender: "sex"
    age: "age"
    region: "province"
    subregion: "district"
    occupation: "occupation"
    marital: "marital_status"
    education: "education_level"
    summary: "persona"
    professional: "professional_persona"
    sports: "sports_persona"
    arts: "arts_persona"
    travel: "travel_persona"
    culinary: "culinary_persona"
    family: "family_persona"
  gender_aliases:
    F: "여자"
    M: "남자"
    여성: "여자"
    남성: "남자"
  province_aliases:
    서울특별시: "서울"
    부산광역시: "부산"
    대구광역시: "대구"
    인천광역시: "인천"
    광주광역시: "광주"
    대전광역시: "대전"
    울산광역시: "울산"
    세종특별자치시: "세종"
    경기도: "경기"
    강원도: "강원"
    강원특별자치도: "강원"
    충청북도: "충청북"
    충청남도: "충청남"
    전라북도: "전북"
    전북특별자치도: "전북"
    전라남도: "전남"
    경상북도: "경상북"
    경상남도: "경상남"
    제주특별자치도: "제주"
```

#### 1.7. GATE-2 통과 사실(2026-05-02)

PRD §5.10 게이트 2를 메인 세션에서 통과 처리했다. dev-planner가 viewer 직접 조회로 박은 §1.1, §1.2의 매핑값과 실제 `datasets.load_dataset` 결과가 100% 일치하는 것을 두 경로(in-memory + streaming)로 교차 확인한 결과다. 일치 항목은 아래와 같다.

- 컬럼 26개 전체 일치(`uuid`, 7개 페르소나 자유 서술, 6개 부가 자유 서술, 9개 인구 통계, 3개 지역)
- 인구 통계 13개 키와 값 표기 일치(특히 `sex`는 `남자`/`여자` 한국어 2진, `age`는 int, `marital_status`는 `배우자있음` 등 한국어 자연어, `district`는 `광주-서구` 형식의 시도-시군구 결합형, `province`는 `광주`/`서울` 등 짧은 17개 표기, `country`는 `대한민국`)
- `dev-planner`가 작성한 §1.6의 `dataset.field_map` 초기값을 그대로 코드에 박아둔 상태로 정합성 위반이 없음

별도 코드 갱신은 필요하지 않다. 본 섹션은 후속 작업(T5-T11)이 컬럼 매핑 가정을 그대로 사용해도 된다는 안전 신호다. 데이터셋 갱신 시에는 본 섹션의 GATE-2를 다시 통과해야 한다.

### 2. 모듈 책임 경계(계층 분리)

architecture.md §1의 계층 분리 원칙을 단일 도메인 CLI에 맞춰 4계층으로 단순화한다. 모놀리식이지만 모듈 단위로 컨텍스트를 분리한다(architecture.md §5).

- presentation 계층은 click 기반 CLI인 main.py 한 파일이며 사용자 입력 파싱과 결과 출력만 담당한다
- application 계층은 src/interview.py, src/batch.py, src/report.py 세 파일이며 유스케이스를 오케스트레이션한다
- domain 계층은 src/models.py에 정의된 dataclass와 도메인 예외, 그리고 src/load_personas.py 안의 PersonaFilter 클래스(도메인 규칙)다
- infrastructure 계층은 외부 LLM HTTP를 다루는 src/llm_client.py, HF datasets를 다루는 src/load_personas.py의 PersonaLoader, 그리고 src/logging_setup.py와 src/config.py다

의존성 방향은 main → batch/report → interview → llm_client/load_personas → models 한 방향이다. 순환 의존을 두지 않는다.

#### 2.1. src/models.py

도메인 모델과 예외만 담는다. 외부 의존성 0(stdlib만).

- `dataclass(frozen=True)`로 정의한 모든 결과 record(`InterviewRecord`, `RunMeta`, `BatchResult`, `StructuredSummary`, `MessageEntry`, `RawResponse`, `Flags`, `PersonaMeta`)
- 사용자 노출 도메인 예외(`ConfigError`, `ServerNotReachableError`, `DatasetUnavailableError`, `FilterMatchedZeroError`)
- 내부 도메인 예외(`PersonaBreakError`, `ResponseTooShortError`, `ModelRefusedError`, `RetryExhaustedError`, `StructuredSummaryParseError`)
- `schema_version: int = 1`(JSON 스키마 버전)

#### 2.2. src/config.py

설정 로드만 담당한다. AppConfig dataclass와 load_config 함수를 둔다.

- AppConfig는 `dataclass(frozen=True)`로 정의한다. LLM, 배치, 데이터셋 섹션을 중첩 dataclass로 보유한다
- 우선순위는 코드 default → `config.yaml` → 환경변수 `KPI_*` → CLI 옵션 순으로 적용한다
- 환경변수 키 명세는 `KPI_LLM_BASE_URL`, `KPI_LLM_MODEL`, `KPI_BATCH_CONCURRENCY` 등을 둔다
- `dataset.field_map`은 dict로 보존하고 정규화는 `PersonaLoader`에서 사용한다
- 사용자가 잘못된 형식의 yaml을 주면 `ConfigError`로 변환한다

#### 2.3. src/logging_setup.py

stdlib `logging` 모듈 위에 JSON Lines 포맷터를 얹는다. structlog 의존을 회피한다(dependency.md §1).

- `JsonLineFormatter` 클래스는 `record.__dict__`에서 표준 필드를 추출하고 `extra` dict를 병합하여 JSON으로 직렬화한다
- request_id는 `contextvars.ContextVar[str]`로 관리한다. `bind_request_id()` 헬퍼로 새 uuid4 값을 부여한다
- `mask_name(name: str) -> str`은 이름이 2글자면 `김O`, 3글자면 `김O수`, 4글자 이상이면 첫 글자와 마지막 글자만 남기고 가운데를 `O`로 채운다
- `mask_product(product: str) -> str`은 첫 30자에 ` ... (총 N자)` 형식의 꼬리를 붙인다
- 콘솔 핸들러(stderr)와 파일 핸들러(`outputs/logs/run_{timestamp}.jsonl`)를 동시에 부착한다
- `setup_logging(level: str, log_dir: Path)` 단일 진입점을 제공한다

#### 2.4. src/load_personas.py

데이터셋 로드, 필터, 시드 샘플링을 담당한다.

- `PersonaLoader`는 `load_dataset(name, split=split)`를 호출하고 캐시를 활용한다. 첫 로드 시간을 추적한다
- `PersonaFilter`는 필터 DSL 파서와 필터 함수를 둔다
- 필터 DSL은 `--filter "key1:value1,key2:value2"` 형식이다. 같은 키 반복 시 OR, 서로 다른 키는 AND로 결합한다
- 정규화 함수는 `_normalize_gender`, `_normalize_province`(별칭 적용), `_match_district_suffix`(시군구 부분 매칭)이다
- 샘플링은 `random.Random(seed).sample(filtered_indices, n)`으로 재현성을 보장한다

#### 2.5. src/llm_client.py

OpenAI 호환 비동기 클라이언트다. `httpx.AsyncClient` 위에 재시도, 타임아웃, 로깅을 얹는다. `openai`/`anthropic` SDK는 사용하지 않는다(dependency.md §1).

- `MlxLLMClient`는 `__aenter__`/`__aexit__` 컨텍스트 매니저 패턴을 채택한다
- `healthcheck()`는 `GET {base_url}/models`의 200 응답과 `data` 배열이 비어있지 않음을 검증한다
- `chat(messages, max_tokens, temperature)`는 `POST {base_url}/chat/completions`로 호출한다
- 재시도 정책은 HTTP 5xx, 타임아웃, 연결 실패에 한해 지수 백오프로 1초, 2초, 4초 간격을 두며 최대 3회 적용한다. 4xx는 즉시 실패한다. 재시도 간 jitter `random.uniform(0, 0.5)`를 추가한다(thundering herd 방지)
- tenacity 의존을 회피한다(dependency.md §1, 6줄 정도면 직접 작성 가능)
- `Authorization` 헤더는 코드에서 일체 다루지 않는다. base_url이 localhost가 아니면 chat 메서드 호출 자체를 차단한다(security.md)

#### 2.6. src/interview.py

페르소나 1명에 대한 멀티턴 인터뷰 1회를 수행한다.

- `InterviewSession`은 페르소나 메타, 시스템 프롬프트, messages 히스토리를 보유한다. 메서드는 `async run()` 1개다
- 자동 follow-up 트리거 함수 `_should_auto_follow_up(response: str) -> bool`은 길이 20자 미만 또는 모호 키워드 매칭을 판정한다. 순수 함수로 분리하여 테스트 용이성을 확보한다
- 페르소나 깨짐 감지 함수 `_detect_persona_drift(response: str, persona: PersonaMeta) -> bool`은 영어 단어 비율 30% 초과 또는 정면 모순 휴리스틱을 판정한다. 휴리스틱은 연령대 키워드, 성별 키워드, 지역명 부정 표현 세 축이다
- 거부 감지 함수 `_detect_refusal(response: str) -> bool`은 거부 키워드 매칭(예: `답변할 수 없습니다`, `I cannot`, `I'm sorry, but`)을 수행한다
- 구조화 요약 생성 함수 `_summarize(messages, client) -> StructuredSummary | None`은 별도 system 프롬프트로 단일턴 호출한다. JSON 파싱 실패 시 1회 retry, 그래도 실패하면 `None`을 반환한다
- 토큰 추정과 truncation은 src/interview.py의 모듈 함수로 분리한다(`_estimate_tokens`, `_truncate_history`)

#### 2.7. src/batch.py

N명 페르소나에 대한 배치 인터뷰를 수행한다.

- `BatchRunner`는 `async run(personas: list[PersonaMeta]) -> BatchResult`를 제공한다
- 동시성은 `asyncio.Semaphore(concurrency)`와 `asyncio.gather(*tasks, return_exceptions=True)`로 구현한다. concurrency 4 이상은 `ConfigError`로 차단한다
- 진행률은 tqdm.asyncio의 `tqdm.gather` 또는 `as_completed`와 수동 tqdm 업데이트 패턴을 사용한다
- SIGINT 핸들러는 `signal.SIGINT`를 `asyncio.Event`에 연결한다. 현재 진행 중인 인터뷰가 끝나면 partial 결과를 `outputs/interview_{slug}_{ts}_partial.json`으로 저장한다
- 결과 직렬화는 `dataclasses.asdict(batch_result)`와 `json.dumps(..., ensure_ascii=False, indent=2)`로 수행한다
- 헬스체크 자동 실행은 `run()` 시작 직전 `client.healthcheck()` 1회로 이루어진다. 실패 시 즉시 `ServerNotReachableError`를 발생시킨다

#### 2.8. src/report.py

JSON 결과 파일을 읽어 마크다운 리포트를 생성한다.

- `ReportGenerator`는 `generate(result_path: Path, options: ReportOptions) -> Path`를 제공한다
- 정량 집계는 의향률, 가격 수용가, 거절 사유 빈도, 코호트별 의향률을 포함한다. 의향률은 positive/neutral/negative 비율, 가격 수용가는 중앙값/IQR/min/max/null 비율과 10구간 히스토그램, 거절 사유 빈도는 상위 N, 코호트별 의향률은 연령대 × 지역 × 성별 표 형태다. `statistics` 모듈을 사용한다(scipy 의존 회피)
- 코호트 셀 표본이 3명 미만이면 `표본 부족`으로 마스킹한다
- 정성 인사이트는 정량 지표와 record 일부 샘플을 system 프롬프트에 묶어 같은 모델에게 단일턴으로 호출한다. 5-10개 인사이트, 공통 반응, 코호트 차이를 자유 서술로 생성한다
- 마크다운 출력 푸터에 데이터셋 라이선스인 CC BY 4.0과 출처, 합성 데이터 한계를 명시한다
- `--include-drift` 옵션이 True이면 `status="drift"` record도 정량 집계에 포함한다

#### 2.9. main.py

click CLI 엔트리다. 비동기 진입점은 `asyncio.run(main_async())` 패턴을 사용한다. 4개 서브커맨드만 노출하고 매크로 명령은 두지 않는다.

### 3. 클래스/함수 시그니처

타입 힌트는 PEP 604(`X | Y`) 표기를 사용한다(Python 3.10+).

#### 3.1. src/models.py

```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass(frozen=True)
class PersonaMeta:
    persona_id: str
    name: str | None
    gender: str
    age: int
    region: str
    subregion: str
    occupation: str
    marital: str
    education: str
    raw: dict

@dataclass(frozen=True)
class MessageEntry:
    role: str  # "system" | "user" | "assistant"
    content: str

@dataclass(frozen=True)
class RawResponse:
    question_index: int
    response: str
    latency_ms: int
    retry_count: int

@dataclass(frozen=True)
class StructuredSummary:
    intent: str  # "positive" | "neutral" | "negative"
    willingness_to_pay: int | None
    willingness_to_pay_currency: str
    rejection_reasons: list[str]
    one_line: str

@dataclass(frozen=True)
class Flags:
    persona_drift: bool = False
    auto_follow_up_used: bool = False
    refusal_detected: bool = False

@dataclass(frozen=True)
class InterviewRecord:
    persona_id: str
    persona_meta: PersonaMeta
    started_at: str
    finished_at: str
    status: str  # "completed" | "refused" | "failed" | "drift"
    messages: list[MessageEntry]
    raw_responses: list[RawResponse]
    structured_summary: StructuredSummary | None
    flags: Flags
    error: dict | None

@dataclass(frozen=True)
class RunMeta:
    interview_id: str
    slug: str
    schema_version: int
    product: str
    questions: list[str]
    follow_up_questions: list[str]
    model: str
    seed: int
    started_at: str
    finished_at: str
    config_snapshot: dict

@dataclass(frozen=True)
class BatchResult:
    meta: RunMeta
    records: list[InterviewRecord]


# 사용자 노출 예외(CLI 종료 코드와 매핑)
class ConfigError(Exception): ...
class ServerNotReachableError(Exception): ...
class DatasetUnavailableError(Exception): ...
class FilterMatchedZeroError(Exception): ...

# 내부 예외(record.status로 변환)
class PersonaBreakError(Exception): ...
class ResponseTooShortError(Exception): ...
class ModelRefusedError(Exception): ...
class RetryExhaustedError(Exception): ...
class StructuredSummaryParseError(Exception): ...
```

#### 3.2. src/config.py

```python
@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    model: str
    max_tokens: int
    temperature: float
    timeout: float

@dataclass(frozen=True)
class BatchConfig:
    concurrency: int
    persona_fields: tuple[str, ...]

@dataclass(frozen=True)
class DatasetConfig:
    name: str
    split: str
    field_map: dict[str, str | None]
    gender_aliases: dict[str, str]
    province_aliases: dict[str, str]

@dataclass(frozen=True)
class AppConfig:
    llm: LlmConfig
    batch: BatchConfig
    dataset: DatasetConfig
    output_dir: Path
    log_level: str
    no_color: bool

def load_config(
    yaml_path: Path = Path("config.yaml"),
    cli_overrides: dict | None = None,
) -> AppConfig: ...
```

#### 3.3. src/llm_client.py

```python
class MlxLLMClient:
    def __init__(self, config: LlmConfig): ...
    async def __aenter__(self) -> "MlxLLMClient": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    async def healthcheck(self) -> list[str]: ...  # 사용 가능한 모델 ID 리스트
    async def chat(
        self,
        messages: list[dict],
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> tuple[str, int]: ...  # (response, latency_ms)
```

#### 3.4. src/load_personas.py

```python
class PersonaLoader:
    def __init__(self, config: DatasetConfig): ...
    def load(self) -> "datasets.Dataset": ...

class PersonaFilter:
    def __init__(
        self,
        spec: str,
        gender_aliases: dict[str, str],
        province_aliases: dict[str, str],
    ): ...
    def apply(self, dataset: "datasets.Dataset") -> list[int]: ...  # 매칭된 인덱스
    @staticmethod
    def parse(spec: str) -> dict[str, list[str]]: ...

def sample_personas(
    dataset: "datasets.Dataset",
    indices: list[int],
    n: int,
    seed: int,
    field_map: dict[str, str | None],
) -> list[PersonaMeta]: ...
```

#### 3.5. src/interview.py

```python
class InterviewSession:
    def __init__(
        self,
        persona: PersonaMeta,
        product: str,
        questions: list[str],
        follow_up_questions: list[str],
        client: MlxLLMClient,
        config: AppConfig,
    ): ...
    async def run(self) -> InterviewRecord: ...

# 모듈 함수(테스트 용이성)
def build_system_prompt(
    persona: PersonaMeta,
    product: str,
    persona_fields: tuple[str, ...],
    field_map: dict[str, str | None],
) -> str: ...

def should_auto_follow_up(response: str) -> bool: ...

def detect_persona_drift(response: str, persona: PersonaMeta) -> bool: ...

def detect_refusal(response: str) -> bool: ...

def estimate_tokens(text: str) -> int: ...

def truncate_history(
    messages: list[MessageEntry],
    max_tokens: int = 8000,
) -> list[MessageEntry]: ...

async def summarize_interview(
    messages: list[MessageEntry],
    client: MlxLLMClient,
    config: LlmConfig,
) -> StructuredSummary | None: ...
```

#### 3.6. src/batch.py

```python
class BatchRunner:
    def __init__(
        self,
        personas: list[PersonaMeta],
        product: str,
        questions: list[str],
        follow_up_questions: list[str],
        client: MlxLLMClient,
        config: AppConfig,
    ): ...
    async def run(self) -> BatchResult: ...

def save_batch_result(result: BatchResult, output_dir: Path) -> Path: ...
```

#### 3.7. src/report.py

```python
@dataclass(frozen=True)
class ReportOptions:
    top_n: int
    include_drift: bool
    output_dir: Path | None

class ReportGenerator:
    def __init__(self, client: MlxLLMClient, config: AppConfig): ...
    async def generate(self, result_path: Path, options: ReportOptions) -> Path: ...

# 정량 집계 순수 함수
def compute_intent_distribution(records: list[InterviewRecord]) -> dict[str, float]: ...
def compute_price_stats(records: list[InterviewRecord]) -> dict[str, float | None]: ...
def compute_rejection_freq(
    records: list[InterviewRecord],
    top_n: int,
) -> list[tuple[str, int]]: ...
def compute_cohort_intent(
    records: list[InterviewRecord],
    axis: str,  # "age" | "region" | "gender"
    min_cell_size: int = 3,
) -> dict[str, dict[str, float | None]]: ...
```

### 4. JSON 스키마 정의

PRD §5.4의 스키마를 dataclass로 정의했다(§3.1 참조). 검증 정책은 아래와 같다.

- 직렬화는 `dataclasses.asdict(BatchResult)` 결과를 `json.dumps(..., ensure_ascii=False, indent=2)`로 처리한다
- 역직렬화는 단순 `json.load()` 후 `BatchResult(**raw)`로 변환한다. dataclass 중첩은 수동 변환 함수 `BatchResult.from_dict(raw: dict) -> BatchResult`를 제공한다
- 검증이 강한 영역만 `__post_init__`에서 수동 검증한다
  - `PersonaMeta.gender`는 `남자` 또는 `여자`만 허용
  - `InterviewRecord.status`는 `completed`, `refused`, `failed`, `drift` 중 하나만 허용
  - `StructuredSummary.intent`는 `positive`, `neutral`, `negative` 중 하나만 허용
- pydantic 의존을 회피하는 근거는 dependency.md §1, §3에 기반한다. 도메인 모델은 30개 미만 필드이고 검증은 4-5개 enum-like 영역에 한정된다. pydantic 도입 비용 즉 트랜지티브 의존, 빌드 시간을 정당화하기 어렵다
- `schema_version: int = 1`을 `RunMeta`에 도입한다. 향후 스키마 변경 시 reader가 분기할 수 있다

### 5. 에러 처리 계층

error-handling.md(예외 비우지 않기, 비즈니스 vs 시스템 구분)을 따른다.

#### 5.1. 사용자 노출 예외(main.py에서 종료 코드로 매핑)

| 예외 | CLI 종료 코드 | 안내 메시지 패턴 |
| --- | --- | --- |
| `ConfigError` | 1 | `설정 파일을 읽을 수 없습니다: {원인}` |
| `ServerNotReachableError` | 1 | `MLX 서버가 응답하지 않습니다. 별도 터미널에서 mlx_lm.server --model {model} --port 8080을 실행해 주세요` |
| `DatasetUnavailableError` | 1 | `데이터셋을 로드할 수 없습니다: {원인}. 인터넷 연결과 ~/.cache/huggingface 권한을 확인해 주세요` |
| `FilterMatchedZeroError` | 2 | `필터 결과 X명, 요청 N명. 필터를 완화해 주세요` |

#### 5.2. 내부 예외(record로 변환)

내부 예외는 외부로 누출하지 않고 `InterviewRecord`의 `status`/`flags`/`error`로 변환한다.

| 예외 | 변환 결과 |
| --- | --- |
| `RetryExhaustedError` | `status="failed"`, `error={"type": "retry_exhausted", "message": ...}` |
| `ModelRefusedError` | `status="refused"`, `flags.refusal_detected=True` |
| `PersonaBreakError` | `status="drift"`, `flags.persona_drift=True` |
| `StructuredSummaryParseError` | `structured_summary=None`(record는 `completed` 유지) |
| `ResponseTooShortError` | 사용 안 함(자동 follow-up으로 흡수) |

#### 5.3. main.py 매핑

```python
try:
    asyncio.run(main_async(...))
except FilterMatchedZeroError as e:
    click.echo(str(e), err=True); sys.exit(2)
except ServerNotReachableError as e:
    click.echo(str(e), err=True); sys.exit(1)
except (ConfigError, DatasetUnavailableError) as e:
    click.echo(str(e), err=True); sys.exit(1)
```

### 6. 로깅 전략

logging.md(레벨, 마스킹, 구조화)와 PRD §6.6을 따른다.

- 포맷은 JSON Lines다. 필드는 `timestamp`, `level`, `message`, `request_id`, `module`이고 `extra`에 도메인 키를 담는다
- request_id는 `contextvars.ContextVar[str]`로 관리한다. CLI 진입 시 `bind_request_id(uuid4().hex)`로 1회 설정한다. 인터뷰 record당 하위 식별자 `interview_id`는 별도 uuid4로 부여한다
- 마스킹 적용 키는 `product`(첫 30자와 길이)와 `persona.name`(이름 마스킹)이다. 이름 마스킹은 logging_setup.py의 `mask_name` 함수로 일원화한다
- 로그 레벨 가이드는 아래와 같다
  - INFO 레벨에는 인터뷰 시작/완료, 헬스체크 결과, 데이터셋 로드 시간, 배치 시작/종료를 기록한다
  - WARN 레벨에는 재시도 발생, 자동 follow-up 사용, 페르소나 드리프트 감지를 기록한다
  - ERROR 레벨에는 retry exhausted, 헬스체크 실패, 데이터셋 로드 실패를 기록한다
  - DEBUG 레벨에는 토큰 추정, truncation 동작, 필터 파싱 결과를 기록한다(개발 시에만)
- 출력처는 stderr 콘솔 핸들러와 `outputs/logs/run_{timestamp}.jsonl` 파일 핸들러 두 개다. 두 핸들러는 같은 포맷터를 공유한다
- structlog 의존 회피 근거는 dependency.md §1이다. stdlib `logging`과 `JsonLineFormatter` 50줄로 충분히 커버 가능하다

### 7. 멀티턴 messages 히스토리 관리

ADR-001(`docs/adr/2026-05-02-multiturn-strategy.md`)에서 멀티턴과 인터뷰 종료 후 단일턴 구조화 요약을 채택했다. 토큰 한계 대응 정책은 아래와 같다.

- system 메시지는 messages[0]에 항상 보존한다(절대 truncate하지 않음)
- 누적 토큰이 8000을 초과하면 system을 제외한 가장 오래된 user/assistant 페어부터 제거한다
- 토큰 추정 휴리스틱은 한국어/영어 혼합 텍스트에 맞춰 아래와 같이 정한다(테스트 용이를 위해 순수 함수로 분리)
  - 한글 1자는 1 토큰으로 간주한다
  - 영어 1자는 0.25 토큰으로 간주한다
  - 그 외 글자(숫자, 공백, 기호) 1자는 0.5 토큰으로 간주한다
- 임계값 8000은 14B 4bit 모델의 컨텍스트 윈도우인 32K 대비 충분한 여유를 두면서도 응답 생성 공간을 확보하는 값이다. config.yaml에서 `llm.context_budget`로 조정 가능하게 둔다
- truncation 발동 시 `flags`에 `truncated=True`를 추가하고 WARN 로그를 남긴다. 현재 PRD `flags` 스키마에는 없으나 v1에서 추가한다. 추가만 하므로 호환성 영향이 없다

### 8. 자동 follow-up과 페르소나 깨짐 감지(Should 상향됨)

PRD §7.2에서 v1 Should로 상향된 두 가드레일이다.

#### 8.1. 자동 follow-up

- 트리거 1은 답변 길이가 20자 미만일 때다
- 트리거 2는 모호 키워드 매칭이다. 정확 매칭이 아닌 부분 문자열 매칭으로 한다
  - 키워드 리스트는 `글쎄요`, `잘 모르겠습니다`, `잘 모르겠어요`, `딱히`, `별로 생각 안 해봤`, `모르겠`이다
  - 키워드 리스트는 config.yaml로 외부화한다(`interview.ambiguous_keywords`)
- 동작은 매칭 시 `조금만 더 자세히 말씀해 주실 수 있을까요?`를 1회 추가하는 것이다. 상한은 1회로 같은 질문 인덱스에서만 적용한다
- 기록은 `flags.auto_follow_up_used=True`로 남기고 `raw_responses[i]`에 추가 응답을 별도 record로 append한다(`question_index`는 같고 `retry_count`가 증가)

#### 8.2. 페르소나 깨짐 감지

- 영어 비율은 `re.findall(r"[A-Za-z]+", text)` 글자 수를 전체 글자 수(공백/구두점 제외)로 나눠 0.30 초과를 임계로 본다
- 정면 모순 휴리스틱은 연령대, 성별, 지역 세 축으로 적용한다
  - 연령대 축은 페르소나가 70대(70 이상 80 미만)인데 응답에 `저는 20대`, `저는 학생인데`, `미성년자` 등이 등장하는 케이스를 본다. 연령 구간은 10대, 20대, 30대, 40대, 50대, 60대 이상의 6개로 산출한다. 자기 구간이 아닌 구간을 자기소개로 단언하면 매칭한다
  - 성별 축은 페르소나가 `여자`인데 응답에 `저는 남자`/`아저씨`를 단언하거나, `남자`인데 `저는 여자`/`아줌마`를 단언하는 케이스를 본다
  - 지역 축은 페르소나가 `서울`인데 응답에 `저는 부산 사람`을 단언하는 케이스를 본다. 17개 시도 중 자기 시도가 아닌 시도를 거주지로 단언하면 매칭한다
- 매칭 휴리스틱은 `re.search(rf"저는\s*{대안_시도}\s*(사람|에서|살고)", text)` 형태의 정규식 묶음으로 구현한다
- LLM 기반 감지는 v1에서 제외한다(비용/지연 문제). 필요하면 v1.1에서 도입한다
- 결과는 `status="drift"`와 `flags.persona_drift=True`로 기록한다. 정량 집계에서 자동 제외하며 `--include-drift` 플래그로 선택적 포함이 가능하다

#### 8.3. 모델 거부 감지

- 키워드 리스트(부분 문자열 매칭)는 `답변할 수 없습니다`, `답변하기 어렵`, `I cannot`, `I'm sorry, but`, `As an AI`, `저는 인공지능`, `AI 모델`이다
- 매칭 시 `status="refused"`와 `flags.refusal_detected=True`로 기록한다. 재시도하지 않는다(같은 거부 반복 가능성)
- 키워드 리스트는 config.yaml의 `interview.refusal_keywords`로 외부화한다

### 9. 동시성 모델

- 단일 진입점은 `asyncio.run(main_async())`이다. main_async는 click 콜백에서 `asyncio.run`을 직접 호출하지 않고 click 명령 함수 내부에서만 호출한다(click과 asyncio 통합 패턴)
- BatchRunner는 `asyncio.Semaphore(N)`을 만들어 페르소나 1명당 task 1개를 만든다. `asyncio.gather(*tasks, return_exceptions=True)`로 한 task 예외가 다른 task를 죽이지 않게 한다
- 진행률은 tqdm.asyncio의 `tqdm.gather` 또는 `as_completed`와 수동 tqdm 업데이트 패턴 중 후자를 채택한다. 후자는 완료 순서대로 진행률을 업데이트할 수 있어 사용자 체감 응답성이 좋다
- SIGINT 처리는 메인 루프에서 `loop.add_signal_handler(SIGINT, ...)`를 등록하는 방식이다. 핸들러는 `cancel_event: asyncio.Event`를 set한다. 각 task는 인터뷰 1회가 끝날 때마다 `cancel_event.is_set()`을 확인 후 종료한다. 진행 분량은 `outputs/interview_{slug}_{ts}_partial.json`으로 저장한다
- 동시성 한계는 `concurrency >= 4`일 때 `ConfigError`로 차단한다. PRD §6.1에 따른 OOM 방지 목적이다

### 10. 설정 로드 우선순위

config.py의 `load_config(yaml_path, cli_overrides)`는 아래 순서로 머지한다.

1. 코드 default 값(AppConfig 생성자 default)
2. `config.yaml`(YAML 파일이 있으면 머지하고 없으면 default만 사용)
3. 환경변수 `KPI_*`(있으면 덮어쓰기)
4. CLI 옵션(있으면 덮어쓰기)

환경변수 키 명세는 아래와 같다.

- `KPI_LLM_BASE_URL`, `KPI_LLM_MODEL`, `KPI_LLM_MAX_TOKENS`, `KPI_LLM_TEMPERATURE`, `KPI_LLM_TIMEOUT`
- `KPI_BATCH_CONCURRENCY`, `KPI_BATCH_PERSONA_FIELDS`(콤마 구분)
- `KPI_OUTPUT_DIR`, `KPI_LOG_LEVEL`, `KPI_NO_COLOR`

config.yaml은 일부 섹션만 정의해도 default와 머지된다. dataset.field_map은 default 코드값(§1.6)과 yaml 값을 깊은 병합(deep merge)한다.

### 11. 의존성 핀

dependency.md §1, §2(lock 파일, 안정 버전)에 따라 production/dev를 분리하고 안정 버전을 핀한다.

#### 11.1. 환경 도구

환경 도구는 uv를 사용한다. 글로벌 룰 `~/.claude/rules/python.md` §1을 따른다. 가상 환경은 프로젝트 로컬 `.venv`에 두고 `uv venv --python 3.12`로 생성한다. 의존성 설치는 `source .venv/bin/activate && uv pip install -r requirements.txt -r requirements-dev.txt`로 수행하며 활성화 없이 `uv run`을 사용해도 된다. Python 버전은 프로젝트 루트의 `.python-version`에 `3.12`로 고정한다(`uv venv --python 3.12`와 호환). 시스템 Python 또는 다른 패키지 매니저(pip 직접, poetry, pipenv, conda)는 본 프로젝트에 도입하지 않는다.

#### 11.2. requirements.txt(production)

```
httpx==0.27.*
datasets==3.*
pyyaml==6.0.*
tqdm==4.66.*
click==8.1.*
aiohttp>=3.13.5,<3.14
```

- httpx는 비동기 HTTP를 담당한다. aiohttp 대신 동기/비동기 양쪽을 지원하며 OpenAI/Anthropic SDK도 내부적으로 사용한다
- datasets는 Hugging Face 표준이다. 캐시, 스트리밍, 필터를 모두 지원한다
- pyyaml은 설정 파일 파싱을 담당한다
- tqdm은 진행률 표시를 담당한다
- click은 CLI 프레임워크다. argparse 대비 데코레이터 기반으로 가독성에서 우위를 갖는다
- aiohttp는 datasets가 트랜지티브로 끌어오는 패키지다. 본 도구가 직접 import하지 않지만 GHSA-9548-qrrj-x5pj 보안 권고를 lock 파일에서 명시 통제하기 위해 상한을 박는다(security.md §1, dependency.md §4). 정식 패치인 3.14는 본 문서 작성 시점에 PyPI에 정식 릴리즈되지 않아 가용 최신 정식 버전 3.13.5를 일시 핀하며 3.14 정식 릴리즈 시 갱신한다

#### 11.3. requirements-dev.txt

```
-r requirements.txt
pytest==8.*
pytest-asyncio==0.23.*
pytest-httpx==0.30.*
```

#### 11.4. lock 파일

`uv pip compile`로 생성한 lock 파일을 함께 커밋한다(python.md §2.2, dependency.md §2).

- `requirements.lock`은 production 그래프(`requirements.txt` → 트랜지티브)이며 약 60종 핀을 담는다
- `requirements-dev.lock`은 dev 그래프(`requirements-dev.txt` → 트랜지티브)이며 production 핀을 그대로 포함한다
- production 환경 설치는 `uv pip sync requirements.lock requirements-dev.lock`으로 frozen 상태를 강제한다
- lock 파일을 갱신할 때는 `uv pip compile requirements.txt -o requirements.lock` 후 `uv pip compile requirements-dev.txt -o requirements-dev.lock` 두 명령을 순서대로 실행한다
- aiohttp의 보안 핀(§11.2)은 lock 파일에서 정확한 버전(3.13.5)으로 박힌다. SLA 내에 3.14 정식 릴리즈가 발표되면 본 핀과 lock을 함께 갱신한다

#### 11.5. 도입을 거부하는 의존성과 근거

dependency.md §1 leftpad 안티패턴 회피와 직접 통제 목적으로 아래 라이브러리는 도입하지 않는다.

- pydantic은 dataclass와 `__post_init__` 검증으로 충분하다(도메인 모델 30개 미만 필드)
- structlog는 stdlib `logging`과 `JsonLineFormatter` 50줄로 커버 가능하다
- tenacity는 지수 백오프 재시도 6줄 직접 구현으로 커버 가능하다
- openai와 anthropic SDK는 PRD §6.5에 명시되어 있다. httpx로 직접 POST한다
- rich와 colorama는 click 자체에 컬러 옵션이 있고 `--no-color` 처리가 충분하다

### 12. API 인터페이스(LLM HTTP 계약)

본 도구는 외부 API를 제공하지 않는다. LLM 서버에 대한 호출은 OpenAI Chat Completions API를 그대로 따른다.

#### 12.1. 헬스체크

```
GET {base_url}/models
```

응답은 200 상태 코드와 body `{"data": [{"id": "string", ...}, ...]}`를 기대한다. 200이 아니거나 data 배열이 비면 `ServerNotReachableError`를 발생시킨다.

#### 12.2. Chat Completions

```
POST {base_url}/chat/completions
Content-Type: application/json
```

Request body는 아래와 같다.

```json
{
  "model": "string",
  "messages": [{"role": "system|user|assistant", "content": "string"}],
  "max_tokens": 500,
  "temperature": 0.8,
  "chat_template_kwargs": {"enable_thinking": false}
}
```

Response body는 아래와 같다.

```json
{
  "choices": [
    {
      "message": {"role": "assistant", "content": "string"},
      "finish_reason": "stop|length"
    }
  ]
}
```

`choices[0].message.content`만 사용한다. `usage` 필드는 v1에서 사용하지 않는다(MLX 서버 구현마다 누락 가능).

#### 12.2.1. Qwen3 thinking 토글(chat_template_kwargs) 보강

GATE-1에서 검증된 사실은 아래와 같다.

- 정본 모델 `unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit`는 chat template default가 thinking on이다. 본 도구의 chat 호출은 항상 `chat_template_kwargs: {"enable_thinking": <config>}`를 body에 포함한다. config의 default 값은 False다
- `enable_thinking=true`로 호출하면 응답이 `{role, content, reasoning}` 3키 구조로 오며 영문 reasoning이 토큰 예산(`max_tokens`)을 모두 소진해 `content`가 빈 문자열로 반환되는 사례가 다수다. v1은 default False로 둬서 본 사례를 회피한다
- `enable_thinking=false`로 호출한 정상 응답은 `finish_reason: stop`을 반환하고 message는 `role`과 `content` 두 키만 포함한다. content는 한국어 자연스러운 페르소나 답변으로 채워진다(예시: `가격이 합리적이라 믿었지만 저는 이미 치킨이나 피자 같은 배달 음식에 지출하고 있어서 반찬 구독까지 쓸 돈이 없습니다. 1인 가구라 식재료를 남기거나 보관하는 게 귀찮아서 오히려 불필요한 구독이 될까 봐 걱정되네요`)
- `enable_thinking=true`를 사용자가 의도적으로 켤 때 reasoning은 분석 가치가 있어 `ChatResponse.reasoning_trace`에 보존한다. False면 reasoning 필드가 와도 무시한다
- 후보였던 27B unsloth 6bit 빌드는 토크나이저 EOS 인식 실패로 토큰 루프(`券后` 반복)가 발생해 후보에서 제외했다. 캐시도 메인 세션에서 삭제 완료했다. 정본 모델은 35B-A3B 그대로 유지한다

빈 content 응답은 `EmptyResponseError`로 변환해 retry 대상으로 본다. 동일 페르소나에 대해 retry가 모두 실패하면 `RetryExhaustedError`로 승격되어 record는 `status="failed"`가 된다(TDD §5.2).

#### 12.3. 에러 매핑

- HTTP 5xx, 타임아웃, 연결 실패는 재시도 대상이다. 지수 백오프로 1초, 2초, 4초 간격을 두고 최대 3회 적용한다. 모두 실패 시 `RetryExhaustedError`로 변환한다
- HTTP 4xx는 즉시 실패한다. 재시도하지 않으며 `ConfigError`로 변환한다(서버 측 요청 거부)
- HTTP 200이지만 `choices`가 비거나 `content`가 빈 문자열이면 `RetryExhaustedError`로 처리한다. 2회 재시도 후 실패로 본다

### 13. 보안과 관측성

security.md §1(시크릿), §3(입력 검증), §4(데이터 보호)와 PRD §6.3(보안과 개인정보)을 따른다.

- API 키 코드는 일체 부재하다. `Authorization` 헤더는 코드에서 다루지 않는다
- `base_url`이 localhost(`http://localhost`, `http://127.0.0.1`)가 아니면 `chat()` 호출을 차단한다(`healthcheck()`만 허용). 사용자가 실수로 외부 URL을 넣어도 사업 아이템 본문이 외부로 송신되지 않게 강제 가드한다. 환경변수 또는 CLI로 `KPI_ALLOW_REMOTE=1`을 명시 설정한 경우만 허용하는 hook만 둔다(v1 Won't 범위)
- product 본문 마스킹은 로그에 `mask_product()`를 적용한다. 결과 JSON에는 원문 그대로 저장한다(로컬 파일이므로 외부 노출 위험 없음)
- 페르소나 이름 마스킹은 로그에만 적용한다. 결과 JSON의 `persona_meta.name`은 원문을 보존한다(분석 시 필요)
- `outputs/`는 `.gitignore` 처리한다. 결과 JSON과 로그 파일은 커밋되지 않는다
- 사용자 입력 검증 정책은 아래와 같다. `--filter` DSL 파싱 시 알 수 없는 키는 `ConfigError`로 처리한다. `--concurrency` 4 이상은 `ConfigError`로 처리한다. `--n` 0 이하는 `ConfigError`로 처리한다
- CORS, JWT, RBAC은 외부 API 미제공이라 v1 비대상이다

### 14. 폴더/파일 트리(확정안)

```
korea-persona-interview/
├── README.md
├── requirements.txt
├── requirements-dev.txt
├── config.yaml
├── .gitignore
├── docs/
│   ├── prd/korea-persona-interview.md
│   ├── tdd/korea-persona-interview.md
│   ├── adr/2026-05-02-multiturn-strategy.md
│   ├── ui/korea-persona-interview.md
│   └── tasks/korea-persona-interview.md
├── src/
│   ├── __init__.py
│   ├── models.py
│   ├── config.py
│   ├── logging_setup.py
│   ├── load_personas.py
│   ├── llm_client.py
│   ├── interview.py
│   ├── batch.py
│   └── report.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_filter_dsl.py
│   ├── test_persona_loader.py
│   ├── test_llm_client.py
│   ├── test_interview_session.py
│   ├── test_persona_drift.py
│   ├── test_batch_runner.py
│   ├── test_report_quant.py
│   ├── test_config.py
│   ├── test_logging.py
│   └── manual/
│       └── smoke_e2e.py
├── outputs/
│   └── .gitkeep
└── main.py
```

### 15. CLI 명세

PRD §5.9의 표를 click 데코레이터로 정의한다. 모든 사용자 안내 문구는 한국어다.

#### 15.1. healthcheck

```python
@cli.command()
@click.option("--base-url", default=None, help="MLX 서버 base URL(기본: config.yaml의 llm.base_url)")
def healthcheck(base_url: str | None): ...
```

종료 코드는 0(정상)과 1(서버 다운)이다.

#### 15.2. list-personas

```python
@cli.command("list-personas")
@click.option("--filter", "filter_spec", default=None, help="필터 DSL(예: age:25-39,region:서울)")
@click.option("--limit", default=20, type=int, help="출력 행 수(기본 20)")
@click.option("--seed", default=42, type=int, help="샘플링 시드(기본 42)")
def list_personas(filter_spec, limit, seed): ...
```

종료 코드는 0(정상)과 2(결과 0건)이다.

#### 15.3. interview

```python
@cli.command()
@click.option("--product", required=True, help="사업 아이템 한 줄 설명")
@click.option("--questions", "questions", required=True, multiple=True, help="질문(여러 번 지정)")
@click.option("--filter", "filter_spec", default=None)
@click.option("--n", default=10, type=int, help="인터뷰 인원(기본 10)")
@click.option("--seed", default=42, type=int)
@click.option("--concurrency", default=2, type=click.IntRange(1, 3), help="동시성 1-3(기본 2)")
@click.option("--persona-fields", default="summary", help="콤마 구분(예: summary,professional)")
@click.option("--follow-up", "follow_ups", multiple=True, help="공통 후속 질문")
@click.option("--single-turn", is_flag=True, default=False)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--output", "output_dir", default="outputs/", type=click.Path())
def interview(...): ...
```

종료 코드는 0(정상), 1(서버 오류), 2(표본 부족), 3(부분 실패: 정상 record 50% 미만)이다.

#### 15.4. report

```python
@cli.command()
@click.argument("result_path", type=click.Path(exists=True))
@click.option("--top-n", default=10, type=int)
@click.option("--include-drift", is_flag=True, default=False)
@click.option("--output-dir", default=None, type=click.Path())
def report(...): ...
```

종료 코드는 0(정상), 1(입력 파일 오류), 2(정상 record 0건)이다.

### 16. 테스트 전략

PRD §6.8과 dependency.md §10(빌드 도구 핀)을 따른다.

- 프레임워크는 pytest 8, pytest-asyncio 0.23, pytest-httpx 0.30 조합이다
- LLM 호출 mock은 pytest-httpx로 `chat/completions` 응답을 픽스처별로 정의한다
- datasets 로드 mock은 conftest.py에서 `datasets.load_dataset`을 `monkeypatch`로 가짜 함수로 교체한다. 가짜 데이터셋은 5-10명짜리 dict 리스트로 둔다
- 핵심 테스트 케이스는 아래와 같다
  - `test_filter_dsl.py`는 AND/OR 결합, 별칭(`F` → `여자`, `서울특별시` → `서울`), 잘못된 키 거부, 시군구 부분 매칭을 검증한다
  - `test_persona_loader.py`는 시드 고정 샘플링 재현성(같은 시드면 같은 인덱스)과 persona_meta 매핑 정확성을 검증한다
  - `test_llm_client.py`는 200 정상, 5xx 재시도 3회 후 실패, 4xx 즉시 실패, 타임아웃 재시도, healthcheck 200 또는 실패, localhost 외 base_url에서 chat 차단을 검증한다
  - `test_interview_session.py`는 멀티턴 messages 누적, system 메시지 보존, truncation 동작(8001 토큰 입력 시 가장 오래된 페어 제거), 자동 follow-up 1회 상한, 거부 키워드 status 변환을 검증한다
  - `test_persona_drift.py`는 영어 비율 30% 임계값과 정면 모순 휴리스틱(연령대/성별/지역) 6개 케이스를 검증한다
  - `test_batch_runner.py`는 동시성 2 정확 적용, 한 task 실패가 다른 task를 죽이지 않음, SIGINT 시 partial 저장을 검증한다
  - `test_report_quant.py`는 의향률 계산, 가격 통계(IQR), 거절 사유 빈도 정렬, 코호트 셀 표본 부족 마스킹, drift 자동 제외를 검증한다
  - `test_config.py`는 우선순위(default → yaml → env → CLI)와 잘못된 yaml 거부를 검증한다
  - `test_logging.py`는 mask_name(2/3/4글자), mask_product, request_id 컨텍스트 전파를 검증한다
- 수동 smoke 테스트는 `tests/manual/smoke_e2e.py`에 둔다. 실제 MLX 서버를 띄우고 1명 dry-run, 3명 배치, report 생성을 수행한다. CI에는 포함하지 않으며 README에 실행 안내를 둔다

### 17. 인프라 변경

본 도구는 로컬 CLI라 클라우드 인프라 변경이 없다. 사용자 환경 요구사항만 명시한다.

- Apple Silicon Mac(M1 이상)이 필요하다. 16GB 이상 통합 메모리를 권장한다(14B 4bit 모델 기준 약 8-10GB 점유)
- Python 3.10 이상이 필요하다. `pyenv` 또는 `uv`로 관리하기를 권장한다
- mlx-lm 패키지는 별도 설치한다. 사용자가 별도 터미널에서 서버를 기동한다
- `~/.cache/huggingface` 디렉토리에 쓰기 권한이 필요하다(데이터셋 캐시 약 4-6GB)

배포 변경, Kubernetes, GitHub Actions, AWS 인프라는 v1에서 모두 비대상이다.

### 18. 보안/성능/관측성 고려사항

architecture.md §10(변경 영향과 테스트), security.md, PRD §6의 통합 정리는 아래와 같다.

- 외부 호출이 없다. localhost MLX 서버만 호출한다
- 동시성은 1-3 범위로 강제한다. 4 이상은 `ConfigError`로 차단한다
- 진행률은 tqdm 콘솔 출력으로 표시한다. 100명 배치 시 1초마다 업데이트된다
- 로그는 stderr와 `outputs/logs/run_{ts}.jsonl` 두 핸들러로 출력한다. JSON Lines로 기록되어 grep과 jq에 친화적이다
- 마스킹은 product 본문(첫 30자와 길이)과 페르소나 이름에 적용한다. 로그에만 적용한다
- 페르소나 일관성 모니터링은 배치 종료 후 stderr에 `drift 비율: X%`, `refusal 비율: Y%`, `failed 비율: Z%` 요약을 INFO 로그로 남긴다. PRD §9 성공 지표(drift 5% 이하)와 비교 가능하다
- 메모리는 데이터셋 자체가 디스크 기반 메모리 매핑으로 관리되어 100만 행 전체를 메모리에 올리지 않는다. 필터 적용 후 `select(indices)`로 작은 부분만 메모리화한다

## 작업 분해(모듈별 예상 시간)

본 도구는 단일 도메인(Python CLI/ML 엔지니어링)이라 도메인 분할이 없다. 전 작업을 ml-engineer가 담당한다. 각 작업은 1일(8시간) 이내로 분해했다.

| ID | 제목 | 담당 | 예상 시간 | 선행 |
| --- | --- | --- | --- | --- |
| T1 | 프로젝트 스캐폴드(디렉토리, requirements, .gitignore, config.yaml 골격) | ml-engineer | 2h | - |
| T2 | 횡단 모듈(models.py 도메인 모델/예외, config.py 로드 우선순위, logging_setup.py JSON 포맷터+마스킹) | ml-engineer | 5h | T1 |
| T3 | llm_client.py(httpx 비동기, healthcheck, chat, 재시도, localhost 가드) | ml-engineer | 4h | T2 |
| 게이트 1 | MLX 서버 기동 확인(사용자 휴먼 검증) | 사용자 | - | T3 |
| T4 | load_personas.py(데이터셋 로드, PersonaFilter DSL, 정규화, 시드 샘플링) | ml-engineer | 6h | T2 |
| 게이트 2 | 데이터셋 컬럼 매핑 휴먼 검증(TDD §1과 일치 확인) | 사용자 | - | T4 |
| T5 | interview.py(InterviewSession, 시스템 프롬프트 빌드, 자동 follow-up, drift/refusal 감지, 토큰 truncation, 구조화 요약) | ml-engineer | 8h | T3, T4 |
| T6 | batch.py(BatchRunner, Semaphore 동시성, tqdm, SIGINT partial 저장, JSON 직렬화) | ml-engineer | 5h | T5 |
| T7 | report.py(정량 집계, 정성 인사이트 LLM 호출, 마크다운 출력) | ml-engineer | 7h | T6 |
| T8 | main.py(click CLI 4개 서브커맨드, 종료 코드 매핑) | ml-engineer | 4h | T6, T7 |
| T9 | tests/(11개 테스트 파일, conftest fixtures) | ml-engineer | 8h | T6, T7, T8 |
| T10 | README.md(설치, 사용법, MLX 서버 안내, 라이선스, 합성 데이터 한계) | ml-engineer | 3h | T8 |

총 예상 시간은 52시간(약 6.5 영업일)이다.

## 의존성 그래프

```
T1 스캐폴드
  └─> T2 횡단(models/config/logging)
        ├─> T3 llm_client ── 게이트 1(MLX 서버 휴먼 검증) ──┐
        └─> T4 load_personas ── 게이트 2(컬럼 매핑 휴먼 검증) ─┤
                                                              ↓
                                                          T5 interview
                                                              ↓
                                                          T6 batch
                                                              ├─> T7 report
                                                              └─> T8 main
                                                                    ↓
                                                                  T9 tests
                                                                    ↓
                                                                  T10 README
```

병렬화 가능 구간은 아래와 같다.

- T3와 T4는 T2 완료 후 병렬 진행이 가능하다
- T7과 T8은 T6 완료 후 병렬 진행이 가능하다
- T9 테스트는 모듈별로 분할 작성하여 T2~T8 진행 중에도 일부 병렬화가 가능하다

## 기술적 리스크

### 1. 데이터셋 컬럼 검증 누락

- 위험은 dev-planner가 viewer로 확인한 컬럼 키와 실제 `load_dataset` 결과가 미세하게 다를 가능성이다(예: viewer 표기는 `광주`이지만 raw에는 `광주광역시`로 들어 있을 수도 있음)
- 완화는 게이트 2에서 `ds['train'].column_names`와 첫 record를 stdout에 출력하고 사용자가 TDD §1과 대조한 뒤 `config.yaml`을 갱신하는 절차를 강제하는 것이다. PRD §10.2 완화책과 동일하다

### 2. 페르소나 이름 누락

- 위험은 데이터셋에 별도 `name` 컬럼이 없다는 점이다(§1.3 매핑표). PRD §5.4 스키마는 `persona_meta.name`을 string으로 정의했지만 실제로는 `null`이 된다
- 완화는 TDD에서 `name: str | None`으로 타입을 변경하는 것이다. v1.1에서 `professional_persona` 본문에서 정규식으로 이름 추출(`(?P<name>[가-힣]{2,4})\s*씨는`)을 실험적으로 추가하는 안을 검토한다. 이 결정은 ADR로 기록하지 않는다(번복 가능성 낮음)

### 3. MLX 서버 컨텍스트 윈도우 초과

- 위험은 14B 4bit 모델의 컨텍스트 윈도우가 8K로 설정된 인스턴스가 있어 멀티턴과 긴 페르소나 토글 시 초과 가능성이 있다는 것이다
- 완화는 TDD §7의 truncation 정책과 `llm.context_budget` 설정값으로 8000 기본값을 두는 것이다. `mlx_lm.server` 기동 명령에 `--max-context`를 명시하도록 README에 안내한다

### 4. 페르소나 깨짐 휴리스틱의 false positive

- 위험은 페르소나가 영문 직업명을 가진 경우(예: occupation `IT 컨설턴트`) 응답에 영문 단어가 자연스럽게 30%에 근접할 수 있다는 것이다
- 완화는 영어 비율 산정 시 페르소나 메타 자체에 등장하는 영문 토큰을 제외하는 화이트리스트 보정을 도입하는 것이다. v1에서는 단순 비율로 출시한 뒤 측정값을 보고 v1.1에서 보정한다. config로 임계값 조정도 가능하게 둔다

### 5. tqdm + asyncio 호환성

- 위험은 tqdm.asyncio가 일부 환경에서 진행률 갱신을 누락하는 사례가 있다는 것이다
- 완화는 `as_completed`와 수동 `tqdm.update(1)` 패턴으로 통일하는 것이다. tqdm.asyncio.gather 대신 수동 패턴이 안정적이다

### 6. 한국어 정규화 누락 시도

- 위험은 사용자가 `region:서울특별시`로 입력했는데 데이터셋 표기는 `서울`이라 매칭에 실패할 가능성이다
- 완화는 §1.6의 `province_aliases`로 시도 별칭을 폭넓게 수용하는 것이다. 매칭 실패 시 `필터 결과 0건` 안내에 "별칭이 적용되지 않았을 수 있습니다. config.yaml의 province_aliases를 확인해 주세요"라는 힌트를 추가한다

### 7. structured_summary JSON 파싱 실패 누적

- 위험은 일부 모델이 자유 서술과 JSON을 혼합 응답으로 반환해 파싱 실패가 누적되면 정성 리포트 품질이 떨어진다는 것이다
- 완화는 1회 retry 후에도 실패하면 `structured_summary=None`으로 두는 것이다. 정량 리포트 상단에 `structured_summary 결측 비율`을 명시한다. 결측이 30%를 넘으면 WARN 로그와 사용자 안내를 출력하고 모델 변경 또는 프롬프트 강화를 권장한다

### 8. SIGINT partial 저장 시 직렬화 실패

- 위험은 인터뷰 진행 중 record가 dataclass 미완성 상태에서 강제 종료되면 직렬화에 실패한다는 것이다
- 완화는 BatchRunner가 항상 완료된 record만 메모리 리스트에 추가하도록 설계하는 것이다(진행 중 task의 부분 record는 제외). partial 저장은 완료된 N건만 직렬화한다

### 9. 모델 응답 인코딩

- 위험은 일부 MLX 빌드가 응답 본문에 escape된 유니코드(`\uXXXX`)를 그대로 반환하는 것이다
- 완화는 `chat()` 응답 처리 시 `response.json()`을 사용해 자동 디코딩하는 것이다. 추가 처리는 불필요하다

### 10. 100만 레코드 첫 로드 메모리 폭발

- 위험은 `load_dataset`이 캐시 미존재 시 다운로드와 변환에 시간/메모리를 사용한다는 것이다. 16GB 머신에서 OOM 가능성은 낮지만 장시간 작업이 된다
- 완화는 첫 로드는 README에 "5-10분 소요"로 명시하는 것이다. 두 번째부터는 캐시를 사용한다. PRD §10.3 완화책과 동일하다. 추가로 streaming 옵션은 필터링과 호환성이 떨어져 v1에서는 비-streaming 캐시 모드만 지원한다
