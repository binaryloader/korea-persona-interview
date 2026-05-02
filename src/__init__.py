"""korea-persona-interview: 합성 한국어 페르소나 인터뷰 파이프라인.

공개 API는 아래와 같다.

- ``InterviewSession``, ``run_interview``: 단일 페르소나 인터뷰 엔진
- ``run_batch``, ``BatchResultEnvelope``: 동시성 배치 실행기
- ``generate_report``, ``ReportOptions``: 마크다운 리포트 렌더러
- ``OpenAIBackend``, ``AnthropicBackend``, ``McpSamplingBackend``: ``LLMBackend``
  프로토콜을 만족하는 LLM 백엔드 구현
- ``load_config``, ``AppConfig``: 레이어드 설정 로더

CLI 진입점은 ``main.py``를 통해 노출된다(콘솔 스크립트 ``kpi``). MCP stdio
서버는 ``src.mcp_server``에 있다(콘솔 스크립트 ``kpi-mcp-server``).
"""

__version__ = "1.1.2"
