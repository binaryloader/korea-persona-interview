# ADR-003: multi-provider backend (OpenAI / Anthropic / local LLM / MCP sampling)

- 일자: 2026-05-02
- 상태: 채택
- 관련: ADR-002(2026-05-02-openai-backend-migration.md, OpenAI 단일 백엔드 → 본 ADR로 supersede)

## 1. 배경

ADR-002에서 본 도구의 기본 추론 백엔드를 로컬 MLX에서 OpenAI Chat Completions API로 옮겼다. 이후 두 가지 사용자 요구가 누적되었다.

- Claude를 인터뷰 백엔드로 직접 쓰고 싶다(Anthropic 크레딧 보유 사용자, Claude 한국어 톤 평가)
- 로컬 LLM(mlx_lm.server, vLLM, llama.cpp)에서 오프라인으로 돌리고 싶다(보안 도메인, 사내 LLM)

또한 MCP 서버에서 OpenAI 키 없이 클라이언트(Claude Code 등)의 LLM에 위임할 수 있도록 sampling 경로가 도입되어 있다. 기존 `llm.backend=auto` 토글은 CLI/MCP 양쪽에서 의미가 충돌하는 복잡도를 만들었다. CLI에서 `mcp_sampling`은 항상 차단되며 MCP에서 `auto` 의미는 비결정적이었다.

## 2. 결정

CLI와 MCP의 진입점을 분리하고 각각의 추론 경로를 단일 정책으로 단순화한다.

- CLI 진입점은 `LlmConfig.provider`로 결정한다.
  - `provider=openai` (기본): `OpenAIBackend`. base_url을 `http://localhost:PORT/v1`로 바꾸면 OpenAI 호환 로컬 서버(mlx_lm.server, vLLM, llama.cpp)에 그대로 붙는다
  - `provider=anthropic`: `AnthropicBackend`. Anthropic Messages API에 직접 httpx로 호출한다. anthropic SDK 의존을 추가하지 않는다(dependency.md §1)
- MCP 서버 진입점은 sampling 전용이다. `McpSamplingBackend`만 사용하며 host agent의 LLM에 추론을 위임한다. host가 sampling capability를 노출하지 않으면 친절한 한국어 안내와 함께 CLI fallback을 권유하는 ConfigError로 차단한다
- 기존 `LlmConfig.backend` 토글을 제거한다. yaml에 남아 있어도 graceful하게 무시한다

## 3. 결과

- 코드 단순화: CLI는 `build_cli_backend(config.llm)`만 부르고 MCP는 `McpSamplingBackend(session)`만 생성한다. `select_backend` / `normalize_backend_choice` 정책 함수는 제거되었다
- 진입점별 의미가 1:1로 고정되어 사용자/AI 에이전트가 어떤 추론 경로가 활성화될지 추적하기 쉬워진다
- 토큰 사용량 추적: OpenAI는 `cached_tokens`, Anthropic은 `cache_read_input_tokens`를 모두 `TokenUsage.cached_tokens`로 정규화해 도구 전반이 같은 인터페이스로 합산한다. MCP sampling은 usage 미반환이라 0으로 들어간다. v1.0.0 시점에 USD 비용 추정은 제거됐다(별도 문서 §3.5 참고)
- breaking change: `llm.backend` 옵션이 제거되었다. 기존 yaml 파일은 그대로 유효하지만 필드는 무시되며 새 사용자는 `llm.provider`로 갈아탄다

## 4. 대안

- (대안 1) provider 옵션을 추가하지 않고 base_url + model만으로 분기한다.
  - 거절: Anthropic은 OpenAI Chat Completions와 호환되지 않는 별도 Messages API 스키마를 쓴다(top-level `system` 필드, x-api-key 헤더). base_url 매칭으로 분기하는 휴리스틱은 안정적이지 않다
- (대안 2) anthropic SDK 도입.
  - 거절: dependency.md §1(leftpad 회피, 트랜지티브 트리 최소화). httpx 직접 호출로 retry/timeout/logging 정책을 동일하게 유지한다
- (대안 3) MCP에서 OpenAI fallback을 유지한다.
  - 거절: MCP는 본질적으로 host LLM을 활용하기 위한 프로토콜이다. fallback 유지는 두 가지 코드 경로를 마련하는 비용 대비 사용자 가치가 작다. CLI가 항상 fallback 경로를 제공하므로 분리가 더 단순하다

## 5. 다음 단계

- v1.1.0 백로그: Anthropic prompt caching `cache_control` 마커, persona quality 검증 보고서(provider별), 로컬 LLM thinking 옵션 등
- ADR-002는 본 ADR로 supersede된다. OpenAI 단일 백엔드 결정의 역사적 맥락은 그대로 보존한다
