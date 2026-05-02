"""MCP 도구 응답 봉투(envelope) 헬퍼.

성공/에러 응답 모두 ``ok: true|false`` 필드와 ``backend`` 라벨이 박힌 공통 dict 형태를 따른다. 호출자가 단일 키로 분기 가능하도록 하기 위함이다(README Integration 섹션 참조).
"""

from __future__ import annotations

from typing import Optional

from ..models import PersonaMeta


def error_payload(
    code: str,
    message: str,
    *,
    exit_code: int = 1,
    backend: Optional[str] = None,
) -> dict:
    """모든 도구 핸들러에서 공통으로 쓰는 에러 응답 dict를 만든다.

    ``ok: false`` 필드는 CLI ``--json`` 모드 봉투와 같은 형태이므로 MCP 클라이언트는 도구 출력을 읽을 때 단일 키 하나로 분기할 수 있다.

    ``backend`` 라벨은 디버깅 편의를 위해 정상/에러 응답 모두에 동일하게 박는다. 호출자가 모드를 결정하기 전(예: ``load_config`` 자체 실패)에는 ``None``으로 넘기면 응답에 ``backend`` 필드가 빠진다.
    """

    payload: dict = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "exit_code": int(exit_code),
        },
    }
    if backend is not None:
        payload["backend"] = backend
    return payload


def persona_to_payload(persona: PersonaMeta) -> dict:
    """``PersonaMeta``를 JSON 친화적인 dict로 변환한다(``raw`` 필드 제외).

    list_personas와 build_persona_prompt 도구가 동일한 모양으로 페르소나를 노출하기 위해 한 곳에서 직렬화한다.
    """

    return {
        "persona_id": persona.persona_id,
        "name": persona.name,
        "gender": persona.gender,
        "age": persona.age,
        "region": persona.region,
        "subregion": persona.subregion,
        "occupation": persona.occupation,
        "marital": persona.marital,
        "education": persona.education,
        "family_type": persona.family_type,
        "housing_type": persona.housing_type,
    }
