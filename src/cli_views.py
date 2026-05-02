"""CLI 표/뷰 렌더 헬퍼.

``main.py``의 presentation 책임 중 페르소나 표 렌더링과 ``--json`` 모드용 dict 변환을 담당한다. ``Console``과 도메인 모델(``PersonaMeta``) 사이를 잇는 얇은 어댑터 계층(architecture.md §1, §5.1).
"""

from __future__ import annotations

from .console import Console
from .models import PersonaMeta


def persona_to_json_dict(persona: PersonaMeta) -> dict:
    """``PersonaMeta``를 ``--json`` 모드에서 stdout에 실어 보낼 dict로 변환한다.

    ``raw`` dict는 데이터셋 원본 컬럼 전체이므로 stdout 페이로드 크기가 커진다.
    외부 통합에서 stream 파싱 부담을 줄이기 위해 본 함수에서는 ``raw``를 빼고 분석에 충분한 인구 통계 핵심 키만 노출한다(파일에 저장되는 JSON에는 raw가 그대로 보존된다).
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


def render_persona_table(personas: list, console: Console) -> None:
    """간단 표 출력. 한글 폭 2 가정으로 단순 정렬한다(UI §5.4).

    터미널 환경에 따라 한글 폭 1 케이스(특히 일부 SSH 클라이언트)가 있지만 본 함수는 가장 흔한 한글 폭 2를 가정한다. 정확한 wcwidth가 필요하면 외부 라이브러리 도입을 검토한다(dependency.md §1 leftpad 회피 원칙에 따라 표준 라이브러리 외 의존 추가는 신중히 결정).
    """

    headers = ("persona_id", "이름", "성별", "연령", "지역", "직업")
    rows = []
    for p in personas:
        rows.append(
            (
                p.persona_id[:16],
                (p.name or "-")[:10],
                p.gender,
                str(p.age),
                f"{p.region} {p.subregion}".strip(),
                (p.occupation or "-")[:24],
            )
        )

    # 컬럼 폭은 헤더 + 본문의 max로. 한글은 2 셀 폭 가정.
    def _width(s: str) -> int:
        w = 0
        for ch in s:
            cp = ord(ch)
            if cp >= 0x1100:
                w += 2
            else:
                w += 1
        return w

    widths = [_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(cell))

    def _pad(cell: str, width: int) -> str:
        return cell + " " * max(0, width - _width(cell))

    header_line = "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers))
    sep_line = "  ".join("-" * widths[i] for i in range(len(headers)))
    console.echo("")
    console.echo("  " + header_line)
    console.echo("  " + sep_line)
    for row in rows:
        line = "  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row))
        console.echo("  " + line)
    console.echo("")
