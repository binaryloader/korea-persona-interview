# ADR-002: 로컬 MLX 백엔드 → OpenAI Chat Completions API 백엔드 전환

- 상태: Superseded by ADR-003(2026-05-02-multi-provider-backend.md)
- 일자: 2026-05-02
- 결정자: 프로젝트 오너(승인 권한)
- 관련 문서: `docs/prd/korea-persona-interview.md` §1, §6.3, §6.5, §10, `docs/tdd/korea-persona-interview.md` §2.5, §11, §12, §13, `docs/adr/2026-05-02-multiturn-strategy.md`(ADR-001), `docs/adr/2026-05-02-multi-provider-backend.md`(ADR-003)

> 본 ADR의 OpenAI 단일 백엔드 결정은 ADR-003에서 multi-provider(OpenAI / Anthropic / 로컬 LLM / MCP sampling)로 확장되었다. 본 문서는 의사결정의 역사적 맥락 보존용이며, 현재 백엔드 정책은 ADR-003을 따른다.

## 1. 컨텍스트

`korea-persona-interview` v1은 초안 단계에서 로컬 MLX 백엔드(Apple Silicon + `mlx_lm.server`)를 채택했다. 외부 API 비용을 0으로 두고 사업 아이템 정보가 로컬에서만 흐르게 하여 보안과 비용 양면에서 우위를 잡는다는 설계였다.

GATE-1 통과 후 실제 운영 검증과 메인 세션 dry-run을 통해 아래 한계가 드러났다.

- 35B-A3B 4bit MLX 빌드는 토크나이저 EOS 인식이 불안정하다. 특정 입력에서 토큰 루프(`券后` 등 한자 반복)와 한자/영어 혼입이 재현된다. 27B Dense 6bit 빌드는 더 심한 토큰 루프로 후보에서 이미 제외된 상태다(TDD §12.2.1)
- Qwen3 thinking 토글 처리 부담이 크다. `enable_thinking=true`로 호출 시 영문 reasoning이 max_tokens를 모두 소진해 content가 빈 문자열이 된다. v1은 default false로 회피했지만 본 토글 자체가 OpenAI 표준 파라미터 외부의 OSS 추론 서버 전용 분기다
- 페르소나 일관성이 약하다. 직전 라운드에서 25세 1인 가구 페르소나가 ``1인 가구가 아니라서 필요성을 못 느끼겠네요``로 응답하는 회귀 사례가 발견되어 PRD §5.2와 TDD §8.2에서 family_type 명시 주입과 거주 형태 모순 휴리스틱을 추가했지만 모델의 페르소나 추종력이 본질적으로 약하다
- 동시성이 1-3으로 좁다. 35B-A3B 4bit가 약 8-10GB를 점유해 16GB 머신에서 동시성 4 이상은 OOM 위험이다. 32GB 권장은 사용자 진입 장벽이 높다
- 첫 모델 다운로드가 12-20GB로 크다. 첫 실행 5-10분 + 디스크 12GB+ 상태가 진입 마찰을 만든다
- 사용자가 별도 터미널에서 `mlx_lm.server`를 띄워야 한다. 도구가 직접 통제할 수 없는 외부 의존이라 헬스체크 분기가 늘 필요하고 사용자 마찰이 크다

대안 백엔드 검토 결과는 §4에 정리했다.

## 2. 결정

OpenAI Chat Completions API 백엔드로 전환한다.

세부 결정은 아래와 같다.

- 기본 모델은 `gpt-4o-mini`다. 가성비와 한국어 페르소나 응답 품질의 균형 관점에서 v1 기본값으로 채택한다
- `base_url`은 `https://api.openai.com/v1`이다. `config.yaml`의 `llm.base_url`로 변경 가능하다
- 인증은 `Authorization: Bearer ${OPENAI_API_KEY}` 헤더로 한다. 환경변수는 표준 `OPENAI_API_KEY`와 fallback `KPI_OPENAI_API_KEY` 두 가지를 허용한다. 코드/설정/.env 파일에 키 하드코딩을 금지한다(security.md §1)
- HTTP 클라이언트는 httpx를 그대로 유지한다. `openai` 공식 SDK는 도입하지 않는다(dependency.md §1 leftpad 안티패턴 회피와 직접 통제 목적)
- Qwen 전용 `chat_template_kwargs: {"enable_thinking": ...}` 같은 OSS 추론 서버 전용 파라미터는 제거한다. OpenAI API 표준 파라미터(`model`, `messages`, `max_tokens`, `temperature`)만 사용한다
- localhost-only chat 가드(이전 TDD §13)는 제거한다. 대신 base_url을 INFO 로그에 명시 출력하여 사용자가 잘못된 엔드포인트로 송신 사고를 즉시 감지할 수 있게 한다
- 토큰 루프 가드와 한자 비율 임계값(5% 초과)은 회귀 안전망으로 유지한다(PRD §5.8, TDD §8.2)

ADR-001(멀티턴 + 단일턴 구조화 요약)은 백엔드 무관한 인터뷰 흐름 결정이라 본 ADR로 supersede 대상이 아니다. ADR-001은 그대로 유효하다.

## 3. 결과

### 3.1. 긍정적 결과

- 페르소나 일관성 향상이 기대된다. gpt-4o-mini는 페르소나 지시 추종력이 35B-A3B 4bit MLX 빌드보다 우월하며 한국어 자연스러움도 개선된다
- EOS 인식이 안정적이라 토큰 루프 사례가 거의 발생하지 않는다. 한자/영어 혼입도 드물다
- 응답 지연이 줄어든다. gpt-4o-mini 기준 한 턴 약 1-3초로 추정되며 로컬 MLX(평균 4-10초/턴) 대비 처리 시간이 단축된다. PRD §6.1의 100명 30분 SLO에 여유가 더 생긴다
- 운영 체제 제약이 사라진다. macOS, Linux, Windows 모두에서 동작한다. Apple Silicon 의존 제거로 사용자 풀이 넓어진다
- mlx_lm.server 별도 기동 부담이 사라진다. 사용자는 `OPENAI_API_KEY` 환경변수만 설정하면 된다
- 12-20GB 모델 다운로드 부담이 사라진다. 디스크 점유는 데이터셋 캐시 4-6GB만 남는다
- 동시성 OOM 위험이 사라진다. 동시성 1-3 범위는 OpenAI rate limit 부하와 비용 폭증 방지 목적으로 유지한다
- Qwen 전용 분기(`chat_template_kwargs`, reasoning 필드 처리)가 사라져 코드가 단순해진다

### 3.2. 부정적 결과

- 비용이 발생한다. gpt-4o-mini 기준 100명 인터뷰 1회 약 $0.50 - $2.00로 추정된다(질문 5개 + 구조화 요약 + 정성 인사이트 1회 호출 가정). 100명을 매일 돌리면 월 $15-60 수준이라 도구의 활용 가치 대비 부담은 작지만 0은 아니다
- 외부 API 의존이 추가된다. OpenAI API 가용성, 인증 실패(401), 사용량 한도(429), 장애가 새 실패 모드로 등장한다. PRD §10.4가 이를 반영해 갱신되었다
- 사업 아이템 본문(`--product`)과 페르소나 정보가 OpenAI 서버로 송신된다. 미공개 IP, 영업 비밀, 개인정보가 외부로 흘러갈 위험이 새로 생긴다. 본 사실은 README와 도구 첫 실행 메시지에서 명시한다(PRD §6.3, §10.6)
- 인터넷 접근이 필요해진다. 오프라인 사용 사례는 비대상이 된다
- OpenAI 약관 동의 책임이 사용자에게 새로 생긴다. 도구는 동의 절차를 자동화하지 않는다

### 3.3. 후속 재검토 트리거

본 결정은 아래 조건 중 하나가 발생하면 재검토한다(supersede 후보 ADR 작성).

- gpt-4o-mini 페르소나 깨짐 비율이 PRD §9 목표(5% 이하)를 초과해서 측정되면 gpt-4o로 모델 상향을 검토한다(비용 약 5-10배). 본 트리거는 가장 가능성이 높은 갱신 사유다
- OpenAI API 비용이 도구 활용 가치를 초과하면 로컬 백엔드 회귀(MLX 또는 vLLM)를 검토한다. 가설은 OpenAI를 100명 매일 돌리는 사용자가 누적 월 $50+를 부담스러워할 때다
- OpenAI API 약관 변경(데이터 학습 사용 정책 강화 등)이 사업 아이템 외부 송신 사실의 무게를 키우면 로컬 회귀 또는 사용자 동의 흐름 강화를 검토한다
- OpenAI 호환 사내 프록시(Azure OpenAI, 사내 LiteLLM 게이트웨이)의 사용 사례가 추가되면 base_url 옵션 확장 ADR을 별도로 작성한다

재검토 시 새 ADR(`docs/adr/{날짜}-backend-revision.md`)을 작성하고 본 ADR의 상태를 `Superseded by ADR-NNN`으로 갱신한다(architecture.md §11).

## 4. 대안과 거부 사유

### 4.1. 대안 A: MLX 35B-A3B + thinking off 유지

지금까지 사용한 정본 모델(`unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit`) + `enable_thinking=false` 조합을 유지한다.

거부 사유는 아래와 같다.

- §1에 정리한 한계(토큰 루프, 한자 혼입, 페르소나 일관성 약화, OOM 위험, 별도 서버 기동 부담, 12GB+ 모델 다운로드)가 그대로 남는다
- 페르소나 일관성 정량 지표(drift 5% 이하)를 달성하기 어려운 상태가 측정으로 확인되었다
- 도구의 입문 마찰을 줄이려는 public 저장소 배포 목표와 충돌한다

### 4.2. 대안 B: MLX 27B Dense 6bit

후보 시점에 검토된 27B unsloth 6bit 빌드는 토크나이저 EOS 인식 실패로 토큰 루프(`券后` 반복)가 35B-A3B보다 더 심하게 발생해 이미 후보에서 제외된 상태다(TDD §12.2.1).

거부 사유는 아래와 같다.

- 토큰 루프가 35B-A3B보다 심해 v1 출시 기준선을 넘기지 못한다
- 캐시도 메인 세션에서 삭제 완료한 상태라 재도입 비용이 크다

### 4.3. 대안 C: Anthropic Claude API(claude-haiku-4.5 등)

Claude의 한국어 응답 품질은 우수하지만 API 인터페이스가 OpenAI Chat Completions API와 호환되지 않는다.

거부 사유는 아래와 같다.

- API 스펙이 다르다(`/v1/messages` 엔드포인트, `system` 파라미터 분리, 응답 형식 차이). 본 도구의 httpx 직접 호출 코드를 수정해야 한다. 비용은 적지 않은 추가 작업이다
- OpenAI 호환 인터페이스가 부재하다. LiteLLM 같은 어댑터를 도입하면 의존성 트리가 커진다(dependency.md §1 leftpad 안티패턴)
- gpt-4o-mini와 가격대가 비슷하지만 v1에서 두 백엔드를 동시 지원할 이점이 작다. 사용자 분리 부담이 크다

### 4.4. 대안 D: gpt-4o(상위 모델)

gpt-4o는 한국어 페르소나 응답 품질이 gpt-4o-mini보다 약간 더 높다.

거부 사유는 아래와 같다.

- 비용이 5-10배 더 비싸다(100명 약 $5-15). v1 기본값으로는 부담이 크다
- 품질 향상 폭이 비용 증가를 정당화할 만큼 크지 않다(작은 데모로 확인)
- 사용자가 페르소나 깨짐 비율을 측정해 필요 시 `config.yaml`의 `llm.model`을 `gpt-4o`로 변경할 수 있다. 옵션으로는 열어두되 기본값은 gpt-4o-mini로 둔다

### 4.5. 대안 E: 로컬 vLLM 또는 llama.cpp 서버

vLLM이나 llama.cpp 같은 로컬 추론 서버를 OpenAI 호환 모드로 띄워 base_url만 갈아끼우는 방식이다.

거부 사유는 아래와 같다.

- MLX와 동일한 한계(별도 서버 기동, 모델 다운로드, 메모리, 토크나이저 안정성)를 그대로 가진다
- public 저장소 배포 단계에서 사용자 환경 다양성을 흡수하기 어렵다(GPU 유무, CUDA 버전, 메모리)
- v1.1.0 옵션으로는 가치가 있다. base_url을 `http://localhost:8080/v1`로 바꾸면 그대로 동작하는 설계라 회귀 비용은 작다(ADR-002 §3.3 후속 재검토 트리거 참고)
