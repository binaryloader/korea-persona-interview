# ADR-005: MCP orchestrator 모드 도입과 sampling 모드 제거

- 일자: 2026-05-02
- 상태: 채택
- 결정자: 프로젝트 오너
- 관련: ADR-004(2026-05-02-mcp-mode-toggle.md, sampling 부분 supersede), PRD §6.5, TDD §2, TDD §12, INDEX §3.7

## 1. 배경

ADR-004에서 MCP 진입점에 `mcp.mode` 토글을 도입했다. `mode: "server"`(기본)는 server-side OpenAI/Anthropic 백엔드를 직접 호출하고, `mode: "sampling"`(opt-in)는 호스트 LLM에 `sampling/createMessage`로 위임하는 정책이었다. server default 결정으로 onboarding 마찰은 해소됐다.

그러나 v1.1.1 운영 데이터에서 sampling 모드는 사실상 사용되지 않는다는 사실이 드러났다.

- 2026-04 기준 mainstream MCP 클라이언트(Claude Code Desktop release builds, Cursor stable, cmux)가 sampling capability를 표준 노출하지 않는다. 일부 Cursor 빌드만 부분 지원한다
- ADR-004의 후속 supersede 임계 기준은 sampling 호환 클라이언트 보급률 50%+다. 2026-05 현재 추정 10% 미만이다. 가까운 시기에 임계에 도달할 신호가 없다
- 한편 호스트 sub-agent 도구(Claude Code의 Task tool, Cursor의 sub-agent 같은 패턴)는 mainstream에서 안정 지원 중이다. 호스트가 직접 sub-agent를 띄워 자기 LLM으로 인터뷰를 수행하면 sampling 의존 없이 동일한 가치(server-side 키 불필요 + 호스트 LLM 활용)를 제공할 수 있다

sampling 모드는 정책 일관성은 있지만 실 사용처가 0에 수렴해 유지보수 부담만 남았다. 동시에 호스트 sub-agent 활용 패턴이 일반화되어 같은 가치를 다른 방식으로 제공할 길이 열렸다.

## 2. 결정

`mcp.mode` 화이트리스트를 아래와 같이 변경한다(BREAKING).

- `mcp.mode: "server"`(기본): 그대로 유지된다. ADR-004의 server default 결정은 본 ADR에서도 유효하다
- `mcp.mode: "orchestrator"`(신규): server-side에서 LLM을 호출하지 않는다. 호스트 sub-agent가 자기 LLM으로 인터뷰를 수행하고, 본 도구는 데이터/프롬프트 helper만 노출한다. server-side 키 불필요. 응답 backend 라벨은 `"mcp_orchestrator"`다
- `mcp.mode: "sampling"`(제거): v1.2.0에서 화이트리스트와 코드 양쪽에서 제거된다. `McpSamplingBackend` 클래스, sampling capability check, `_convert_to_sampling_messages`, `_extract_sampling_text` 헬퍼도 함께 정리된다

도구 노출 정책은 mode별로 분리된다.

- 모든 mode 공통: `healthcheck`(mode별 체크 내용 다름), `list_personas`, `report`(orchestrator 모드는 정성 인사이트 fallback), helper 4종(`detect_persona_drift`, `should_auto_follow_up`, `parse_structured_summary`, `interview_record_schema`)
- MCP server 모드 전용: `interview`
- MCP orchestrator 모드 전용: `build_persona_prompt`, `build_batch_prompts`, `aggregate_results`

자동 fallback은 두지 않는다. 사용자가 mode를 명시 토글로 선택해 동작 경로를 분명히 한다.

진입점은 세 개로 정리된다(ADR-005 이후).

| 진입점 | mode (yaml) | server-side LLM 호출 | 호스트 LLM 호출 | 키 |
| --- | --- | --- | --- | --- |
| CLI(`kpi`) | n/a | 적용 | 미적용 | provider에 따라 |
| MCP server | `mcp.mode: "server"` | 적용 | 미적용 | provider에 따라 |
| MCP orchestrator | `mcp.mode: "orchestrator"` | 미적용 | 적용(sub-agent) | 불필요 |

## 3. 결과

긍정적 영향은 아래와 같다.

- sampling 보급률 한계 해소. 호스트 sub-agent는 mainstream에서 이미 안정이라 즉시 가치를 낸다
- 모드 단순화. v1.1.1의 server/sampling 두 모드에서 본 라운드의 server/orchestrator 두 모드로 동일한 이름 수를 유지하면서 실 사용처가 모두 있는 상태로 변환된다
- 도구 모듈화. `src/mcp_handlers/`로 분리되어 mode별 핸들러를 독립적으로 관리한다
- 새 도구 7개로 호스트 sub-agent 흐름이 정합한 데이터 모양과 임계값을 그대로 재사용한다(휴리스틱 재구현 부담 없음)

부정적 영향은 아래와 같다.

- BREAKING. `mcp.mode: "sampling"`을 사용하던 사용자는 yaml에서 `"orchestrator"` 또는 `"server"`로 마이그레이션해야 한다. CHANGELOG와 README에 마이그레이션 가이드를 박는다
- MCP orchestrator 모드는 휴리스틱 자동 적용이 빠진다. 호스트가 helper 도구를 명시 호출해야 같은 임계값으로 drift/follow-up을 판정할 수 있다. README와 PRD §10에 본 한계를 명시한다
- yaml 카테고리 재구조화(common/llm/batch/heuristics/mcp/output)와 함께 적용되어 사용자 yaml 수정 범위가 커진다. CHANGELOG에 일괄 정리한다

## 4. 대안

`mcp.mode: "sampling"` 유지는 거부했다. 사실 무용한 옵션을 화이트리스트에 남겨 두면 사용자가 설정해도 기대 동작을 못 보고, 코드 분기는 유지보수 부담만 남긴다.

`mcp.mode: "orchestrator"` 추가하되 sampling도 같이 두는 안은 거부했다. 모드 3개 동시 유지보수 부담이 크고, sampling 대비 orchestrator의 가치가 명확하다(같은 기능을 보급률 100%인 sub-agent로 제공). v1.2.0 minor 릴리즈에서는 한쪽으로 정리한다.

자동 fallback(server → orchestrator)은 거부했다. ADR-004 §4의 sampling 자동 fallback 거부 사유와 같다. 응답이 어느 경로로 갔는지 사용자가 추적할 수 없으면 비용 청구 주체와 데이터 흐름이 불분명해진다.

## 5. 다음 단계

- 호스트 sub-agent 도구의 표준 인터페이스(예: Claude Code의 Task tool 안정 API, Cursor의 sub-agent API)가 progressional하게 진화하면 build_persona_prompt와 aggregate_results 도구의 응답 모양을 정합 유지하도록 갱신한다
- sampling 호환 클라이언트 보급률이 50%+에 도달하면 sampling 재도입을 별도 ADR로 검토한다. 현재는 trigger 미달
- README "Integration with External Agents" 섹션을 3 진입점 매트릭스로 재작성한다(별도 docs 커밋)
- PRD §6.5 호환성과 TDD §12 LLM HTTP 계약도 본 결정값을 반영하도록 갱신한다(별도 docs 커밋)
