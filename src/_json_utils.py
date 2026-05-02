"""Internal JSON extraction helper shared by ``interview`` and ``report``.

The leading underscore signals "module-private"; nothing outside the package
should import from here.
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
    """Pull the outer-most JSON object out of an LLM response body.

    Tolerates leading/trailing prose and ```/```json code fences. Returns
    ``None`` for empty input, parse failure, or non-dict top level.
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
