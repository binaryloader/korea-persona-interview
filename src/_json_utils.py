"""``interview``와 ``report`` 모듈이 공유하는 내부 JSON 추출 헬퍼.

선행 언더스코어는 module-private 신호이며 패키지 외부에서 본 모듈을 import해서는 안 된다.
"""

from __future__ import annotations

import json
from typing import Optional


def _strip_code_fence(text: str) -> str:
    candidate = text.strip()
    if not candidate.startswith("```"):
        return candidate
    lines = candidate.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def extract_json_object(text: str) -> Optional[dict]:
    """LLM 응답 본문에서 가장 바깥쪽 JSON 객체를 추출한다.

    앞뒤 산문과 ```/```json 코드 펜스를 허용한다. 입력이 비었거나, 파싱이 실패하거나, top-level이 dict가 아니면 ``None``을 반환한다.
    """

    if not text or not text.strip():
        return None

    candidate = _strip_code_fence(text)

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    json_text = candidate[start : end + 1]

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None
    return data
