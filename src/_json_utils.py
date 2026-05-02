"""LLM 응답 텍스트에서 JSON 객체를 안전하게 추출하는 모듈 공용 유틸.

본 모듈은 ``interview.py``와 ``report.py``가 동일하게 수행하던 코드 펜스
제거와 가장 바깥 ``{ ... }`` 추출 로직을 한 곳에 모은다(architecture.md §5의
공유 모듈은 횡단 관심사에만 둔다 원칙). 모듈 prefix ``_``는 외부 노출 의도가
없는 내부 유틸임을 표시한다.

LLM이 정해진 JSON 스키마를 요청받아도 마크다운 코드 펜스(```json``)나 추가
설명을 함께 출력하는 사례가 잦다. 본 헬퍼는 그런 변형을 흡수해 가장 바깥
``{ ... }`` 본문만 ``json.loads``에 넘긴다.
"""

from __future__ import annotations

import json
from typing import Optional


def _strip_code_fence(text: str) -> str:
    """선두/말미의 마크다운 코드 펜스(``` 또는 ```json)를 제거한다."""

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
    """LLM 응답에서 가장 바깥 ``{ ... }`` JSON 객체를 dict로 추출한다.

    추출 규칙은 아래와 같다.

    - 빈 문자열이거나 공백뿐이면 ``None``
    - 선두/말미의 마크다운 코드 펜스(``` 또는 ```json)는 제거
    - 본문에서 ``find('{')``과 ``rfind('}')``로 가장 바깥 객체 범위 추출
    - 추출 본문이 ``json.loads`` 실패면 ``None``
    - 최상위가 dict가 아니면 ``None``

    Args:
        text: LLM 응답 본문(자유 서술 + JSON 혼합 가능).

    Returns:
        파싱된 dict 또는 ``None``(파싱 실패/빈 입력/dict 아님).
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
