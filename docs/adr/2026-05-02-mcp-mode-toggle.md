# ADR-004: MCP 동작 모드 토글(server default, sampling opt-in)

- 일자: 2026-05-02
- 상태: 채택(historical, sampling 부분과 server-default 결정은 모두 ADR-005로 supersede)
- 결정자: 프로젝트 오너
- 관련: PRD §6.5, TDD §2.10, TDD §12, ADR-003(2026-05-02-multi-provider-backend.md, MCP sampling-only 결정 부분만 supersede), ADR-005(2026-05-02-orchestrator-mode-and-sampling-removal.md, 본 ADR의 sampling 부분과 server-default 결정을 모두 supersede)

> 본 ADR의 sampling opt-in 결정은 ADR-005에서 supersede됐다(보급률 한계 사유). server-default 결정도 v1.2.0 후속 정리에서 default가 `server`에서 `orchestrator`로 바뀌면서 함께 supersede됐다. 본 ADR의 단락들은 시점별 의사결정 기록 가치를 위해 historical context로 보존한다.

## 1. 배경

ADR-003에서 본 도구의 MCP 서버 진입점을 sampling 전용으로 단순화했다. 추론은 항상 호스트 에이전트의 `sampling/createMessage`로 위임하고 server-side에는 OpenAI/Anthropic 키를 두지 않는 정책이었다. 호스트가 sampling capability를 노출하지 않으면 ConfigError와 CLI fallback 안내로 차단된다.

본 결정은 MCP가 본질적으로 호스트 LLM을 활용하기 위한 프로토콜이라는 관점에서 깔끔하지만 두 가지 운영 마찰이 누적되었다.

- 2026년 4월 현재 sampling capability를 표준 노출하는 MCP 클라이언트가 매우 적다. cmux 빌드는 sampling을 지원하지 않고 Claude Code Desktop의 정식 빌드도 sampling 노출이 확정되지 않았으며 Cursor 일부 버전만 부분 지원한다
- 결과적으로 일반 사용자가 mcp.json에 본 도구를 등록하고 자연어로 호출하면 ConfigError가 항상 떨어진다. 도구가 부팅조차 되지 않으니 실 사용 가치가 사라진다

ADR-003의 sampling-only 정책은 정책 자체로는 일관되지만 보급률 한계로 대부분 사용자 환경에서 무용하다는 사실이 운영 데이터로 드러났다.

## 2. 결정

`config.yaml`에 `mcp.mode` 토글을 도입한다. 자동 fallback은 두지 않는다. 사용자가 명시 토글로 동작 경로를 선택한다.

- `mcp.mode: "server"`는 기본값이다. MCP 도구 호출이 server-side `OpenAIBackend`나 `AnthropicBackend`를 사용한다. CLI와 동일한 `LlmConfig` 필드를 그대로 활용한다. 적용 필드는 provider, base_url, model, api_key, timeout, retry, anthropic_cache_control, extra_chat_kwargs, streaming이다. mcp.json `env`에 `OPENAI_API_KEY` 또는 `ANTHROPIC_API_KEY`를 박아 주어야 한다. 응답에 `"backend": "mcp_server"` 라벨이 박힌다
- `mcp.mode: "sampling"`은 명시 opt-in이다. MCP 도구 호출이 호스트 에이전트의 `sampling/createMessage`에 위임한다. server-side 키 불필요. 호스트가 sampling capability를 노출하지 않으면 ConfigError와 CLI fallback 안내로 차단된다. 응답에 `"backend": "mcp_sampling"` 라벨이 박힌다

ADR-003 §2의 MCP sampling-only 결정은 본 ADR로 supersede된다. multi-provider backend 결정은 본 ADR과 무관하게 그대로 유효하다. 즉 provider 토글, AnthropicBackend, build_cli_backend factory는 그대로 동작한다.

## 3. 결과

긍정적 영향은 아래와 같다.

- 즉시 동작. mcp.json 등록 직후 server default가 호스트 sampling 보급률과 무관하게 동작한다. onboarding 마찰이 사라진다
- 명확성. 자동 fallback이 없으므로 도구 호출 결과가 어느 백엔드를 거쳤는지 응답 라벨로 명시된다. 디버깅 비용이 낮아진다
- CLI와의 정책 일관성. server mode는 CLI와 같은 `LlmConfig`를 쓰므로 같은 yaml로 두 진입점을 공유한다. 사용자가 익숙한 환경 변수와 모델 ID가 그대로 적용된다

부정적 영향은 아래와 같다.

- server mode default는 server-side 키를 요구한다. 보안 의식이 강한 사용자나 호스트 LLM 위임을 선호하는 사용자는 yaml에서 `mcp.mode: "sampling"`으로 명시 전환해야 한다. README, INDEX, PRD, TDD에 두 모드 차이와 트레이드오프를 명시한다
- 두 모드 모두 회귀 테스트가 필요하다. tests/test_mcp_server.py가 cwd config.yaml로 mode를 핀하는 방식으로 두 모드 분기를 모두 검증한다. 본 라운드에서 회귀 555개에서 571개로 증가했다

## 4. 대안

자동 fallback 안은 거부했다. server mode 시도 후 키 없으면 sampling으로 자동 전환하는 흐름은 surprise 동작과 디버깅 어려움이 가시화된다. 응답이 어느 경로로 갔는지 사용자가 추적할 수 없으면 비용 청구 주체가 불분명해진다.

sampling default 유지는 거부했다. ADR-003이 채택한 정책 자체는 깔끔하지만 보급률 한계로 onboarding 마찰이 너무 크다. 일반 사용자가 도구를 등록만 하고 동작을 못 보면 도구 자체가 사장된다.

MCP 자체를 v1.2.0으로 미루는 안은 거부했다. MCP는 외부 통합의 핵심 패턴이고 v1.0과 v1.1.0에 이미 4개 도구가 노출되어 있다. 도구 이름은 `healthcheck`, `list_personas`, `interview`, `report`다. 후행 모드 확장이 v1.1.x patch로 충분하다.

## 5. 다음 단계

- Claude Code Desktop 정식 빌드가 sampling capability를 표준 노출하는 시점에 default를 `sampling`으로 전환할지 검토한다. 별도 ADR로 supersede 처리한다
- sampling 호환 클라이언트 보급률이 50% 이상에 도달하면 default 전환 임계로 본다. 현재 추정 10% 미만이다(2026-04 기준)
- README, PRD, TDD가 본 ADR 결정값을 반영하도록 갱신한다. 별도 docs 커밋으로 처리한다
