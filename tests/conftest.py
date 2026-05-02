"""테스트 공통 fixture와 픽스처 데이터.

테스트 격리 원칙은 아래와 같다.

- LLM 호출은 ``pytest-httpx``로 100% 모킹한다(실제 OpenAI API 호출 없음)
- 데이터셋은 ``monkeypatch``로 ``datasets.load_dataset``을 가짜 함수로 교체한다
- 환경변수는 ``monkeypatch``로 격리하고 테스트 종료 시 원복된다
- 임시 디렉토리는 ``tmp_path``를 사용한다

비동기 테스트는 ``pytest-asyncio``의 ``@pytest.mark.asyncio`` 데코레이터로 마킹한다.
``pytest_httpx``의 fixture ``httpx_mock``과 함께 쓴다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest


# 프로젝트 루트를 sys.path에 추가하여 ``src`` 패키지와 ``main`` 모듈을 import 가능하게 한다.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# 환경변수 격리
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``KPI_*``와 ``OPENAI_API_KEY`` 환경변수를 제거해 테스트 간 누수를 막는다.

    OpenAI 키가 사용자 셸에 set되어 있으면 의도치 않게 외부 호출이 발생할
    위험이 있으므로 테스트 fixture가 명시적으로 키를 박는 경우에만 동작하게
    한다.

    프로젝트 루트의 ``.env``가 ``_load_dotenv``의 fallback 탐색 경로에 잡혀
    실제 ``OPENAI_API_KEY``를 주입하던 회귀를 막기 위해 ``src.config._load_dotenv``
    를 빈 dict로 모킹한다. 테스트는 자체적으로 ``.env`` 경로를 ``tmp_path``로
    제어하기 때문에 본 모킹이 .env 파일 격리에 영향을 주지 않는다.
    """

    for key in list(os.environ):
        if key.startswith("KPI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # 페르소나 풀 in-memory 캐시는 프로세스 단위로 누적되므로 테스트 간 격리
    # 위해 매 테스트마다 비운다.
    try:
        from src.load_personas import clear_persona_pool_cache

        clear_persona_pool_cache()
    except Exception:  # noqa: BLE001 - import 실패 시 안전망
        pass

    # 시스템 프롬프트 템플릿 캐시도 비운다(라운드 B4 외부화로 도입). 같은
    # 경로를 다른 내용으로 가리키는 테스트가 캐시 hit으로 잘못된 결과를 받지
    # 않도록 한다.
    try:
        from src.interview import clear_system_prompt_cache

        clear_system_prompt_cache()
    except Exception:  # noqa: BLE001 - import 실패 시 안전망
        pass

    # 프로젝트 루트 .env가 자동 탐색되어 실제 ``OPENAI_API_KEY``를 주입하던
    # 회귀를 막는다. 명시 경로를 받는 호출과 cwd가 프로젝트 루트가 아닌
    # 호출(monkeypatch.chdir(tmp_path) 사용)은 그대로 통과시킨다. 그래야 cwd를
    # tmp_path로 옮긴 .env 파싱 회귀 테스트가 정상 동작한다.
    import src.config as _config

    original_load_dotenv = _config._load_dotenv
    project_env = _config._PROJECT_ROOT / ".env"

    def _isolated_load_dotenv(path=None):
        if path is not None:
            return original_load_dotenv(path)
        # cwd가 프로젝트 루트인 경우(또는 cwd .env가 없고 프로젝트 루트 .env가
        # 있는 경우) 외부 .env 자동 주입을 차단한다.
        cwd_env = Path.cwd() / ".env"
        if cwd_env.exists() and cwd_env.resolve() != project_env.resolve():
            return original_load_dotenv(None)
        return {}

    monkeypatch.setattr(_config, "_load_dotenv", _isolated_load_dotenv)


# ---------------------------------------------------------------------------
# 페르소나 데이터 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_persona_row() -> dict:
    """단일 페르소나 row(데이터셋 표기 그대로)."""

    return {
        "uuid": "test-uuid-0001",
        "sex": "여자",
        "age": 27,
        "marital_status": "미혼",
        "military_status": "비현역",
        "family_type": "1인 가구",
        "housing_type": "원룸",
        "education_level": "대학교",
        "bachelors_field": "컴퓨터공학",
        "occupation": "소프트웨어 엔지니어",
        "province": "서울",
        "district": "서울-강남구",
        "country": "대한민국",
        "persona": "서울 강남구에서 1인 가구로 일하며 자기 시간을 중요하게 여긴다.",
        "professional_persona": "스타트업에서 백엔드 개발을 담당한다.",
        "sports_persona": "주말마다 한강에서 러닝.",
        "arts_persona": "독립 영화관에서 관람.",
        "travel_persona": "1년에 한 번 해외 여행.",
        "culinary_persona": "혼밥과 배달 중심 식생활.",
        "family_persona": "본가는 부산에 있고 명절에만 방문.",
    }


@pytest.fixture
def fake_persona_rows() -> list:
    """5명짜리 가짜 데이터셋. 필터/샘플링 테스트에 사용한다."""

    return [
        {
            "uuid": "p-0001",
            "sex": "여자",
            "age": 27,
            "marital_status": "미혼",
            "military_status": "비현역",
            "family_type": "1인 가구",
            "housing_type": "원룸",
            "education_level": "대학교",
            "bachelors_field": "컴퓨터공학",
            "occupation": "소프트웨어 엔지니어",
            "province": "서울",
            "district": "서울-강남구",
            "country": "대한민국",
            "persona": "27세 서울 강남구 1인 가구.",
            "professional_persona": "백엔드 개발자.",
            "sports_persona": "러닝.",
            "arts_persona": "영화.",
            "travel_persona": "여행.",
            "culinary_persona": "혼밥.",
            "family_persona": "본가는 부산.",
        },
        {
            "uuid": "p-0002",
            "sex": "남자",
            "age": 34,
            "marital_status": "배우자있음",
            "military_status": "비현역",
            "family_type": "배우자와 거주",
            "housing_type": "아파트",
            "education_level": "대학교",
            "bachelors_field": "경영학",
            "occupation": "마케팅 매니저",
            "province": "서울",
            "district": "서울-마포구",
            "country": "대한민국",
            "persona": "34세 마포구 마케터.",
            "professional_persona": "B2C 마케팅.",
            "sports_persona": "골프.",
            "arts_persona": "전시회.",
            "travel_persona": "가족 여행.",
            "culinary_persona": "외식.",
            "family_persona": "아내와 거주.",
        },
        {
            "uuid": "p-0003",
            "sex": "여자",
            "age": 41,
            "marital_status": "배우자있음",
            "military_status": "비현역",
            "family_type": "자녀와 거주",
            "housing_type": "아파트",
            "education_level": "대학교",
            "bachelors_field": "교육학",
            "occupation": "교사",
            "province": "경기",
            "district": "경기-수원-팔달구",
            "country": "대한민국",
            "persona": "41세 경기도 교사.",
            "professional_persona": "초등 교사.",
            "sports_persona": "요가.",
            "arts_persona": "독서.",
            "travel_persona": "가족 여행.",
            "culinary_persona": "집밥.",
            "family_persona": "남편과 자녀 둘.",
        },
        {
            "uuid": "p-0004",
            "sex": "남자",
            "age": 65,
            "marital_status": "배우자있음",
            "military_status": "비현역",
            "family_type": "배우자와 거주",
            "housing_type": "아파트",
            "education_level": "고등학교",
            "bachelors_field": "해당없음",
            "occupation": "퇴직",
            "province": "부산",
            "district": "부산-해운대구",
            "country": "대한민국",
            "persona": "65세 부산 퇴직자.",
            "professional_persona": "퇴직 후 자영업.",
            "sports_persona": "등산.",
            "arts_persona": "TV.",
            "travel_persona": "국내 여행.",
            "culinary_persona": "집밥.",
            "family_persona": "아내와 거주.",
        },
        {
            "uuid": "p-0005",
            "sex": "여자",
            "age": 22,
            "marital_status": "미혼",
            "military_status": "비현역",
            "family_type": "1인 가구",
            "housing_type": "원룸",
            "education_level": "대학교 재학",
            "bachelors_field": "디자인",
            "occupation": "대학생",
            "province": "서울",
            "district": "서울-동대문구",
            "country": "대한민국",
            "persona": "22세 서울 대학생.",
            "professional_persona": "디자인 학과 학생.",
            "sports_persona": "필라테스.",
            "arts_persona": "전시회.",
            "travel_persona": "친구와 여행.",
            "culinary_persona": "외식.",
            "family_persona": "본가는 대전.",
        },
    ]


@pytest.fixture
def fake_persona_meta(fake_persona_row: dict):
    """``PersonaMeta`` 인스턴스 단일."""

    from src.models import PersonaMeta

    return PersonaMeta(
        persona_id=fake_persona_row["uuid"],
        name=None,
        gender=fake_persona_row["sex"],
        age=fake_persona_row["age"],
        region=fake_persona_row["province"],
        subregion=fake_persona_row["district"],
        occupation=fake_persona_row["occupation"],
        marital=fake_persona_row["marital_status"],
        education=fake_persona_row["education_level"],
        raw={k: v for k, v in fake_persona_row.items() if k != "uuid"},
        family_type=fake_persona_row.get("family_type"),
        housing_type=fake_persona_row.get("housing_type"),
    )


# ---------------------------------------------------------------------------
# AppConfig 빌더
# ---------------------------------------------------------------------------


@pytest.fixture
def make_app_config():
    """``AppConfig`` 빌더. 기본값을 깔고 일부 키만 override할 수 있게 한다."""

    from src.config import (
        AppConfig,
        BatchConfig,
        DatasetConfig,
        InterviewConfig,
        LlmConfig,
        ReportConfig,
    )

    def _build(
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "test-model",
        max_tokens: int = 100,
        temperature: float = 0.5,
        timeout: float = 5.0,
        context_budget: int = 32000,
        retry_max_attempts: int = 3,
        retry_backoff_seconds: tuple = (0.0, 0.0, 0.0),
        api_key: str = "test-key",
        concurrency: int = 2,
        persona_fields: tuple = ("summary",),
        single_turn: bool = False,
        output_dir: Path = Path("outputs/"),
        log_level: str = "INFO",
        no_color: bool = True,
        short_answer_threshold: int = 20,
        english_ratio_threshold: float = 0.30,
        ambiguous_keywords: tuple = ("글쎄요", "잘 모르겠습니다", "딱히"),
        refusal_keywords: tuple = (
            "답변할 수 없습니다",
            "I cannot",
            "I'm sorry, but",
            "저는 인공지능",
        ),
        auto_follow_up_text: str = "조금만 더 자세히 말씀해 주실 수 있을까요?",
        auto_follow_up_max: int = 1,
        system_prompt_path: str = "prompts/system_prompt.txt",
        partial_failure_threshold: float = 0.5,
        cohort_min_cell: int = 3,
        top_n_default: int = 10,
        histogram_bins: int = 10,
        bar_width: int = 30,
    ) -> AppConfig:
        llm = LlmConfig(
            base_url=base_url,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            context_budget=context_budget,
            retry_max_attempts=retry_max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            api_key=api_key,
        )
        batch = BatchConfig(
            concurrency=concurrency,
            persona_fields=persona_fields,
            single_turn=single_turn,
            partial_failure_threshold=partial_failure_threshold,
        )
        dataset = DatasetConfig(
            name="nvidia/Nemotron-Personas-Korea",
            split="train",
            field_map={
                "name": None,
                "gender": "sex",
                "age": "age",
                "region": "province",
                "subregion": "district",
                "occupation": "occupation",
                "marital": "marital_status",
                "education": "education_level",
                "summary": "persona",
                "professional": "professional_persona",
                "sports": "sports_persona",
                "arts": "arts_persona",
                "travel": "travel_persona",
                "culinary": "culinary_persona",
                "family": "family_persona",
            },
            gender_aliases={
                "F": "여자",
                "M": "남자",
                "여성": "여자",
                "남성": "남자",
            },
            province_aliases={
                "서울특별시": "서울",
                "부산광역시": "부산",
                "경기도": "경기",
            },
        )
        interview = InterviewConfig(
            short_answer_threshold=short_answer_threshold,
            english_ratio_threshold=english_ratio_threshold,
            ambiguous_keywords=ambiguous_keywords,
            refusal_keywords=refusal_keywords,
            auto_follow_up_text=auto_follow_up_text,
            auto_follow_up_max=auto_follow_up_max,
            system_prompt_path=system_prompt_path,
        )
        report = ReportConfig(
            cohort_min_cell=cohort_min_cell,
            top_n_default=top_n_default,
            histogram_bins=histogram_bins,
            bar_width=bar_width,
        )
        return AppConfig(
            llm=llm,
            batch=batch,
            dataset=dataset,
            interview=interview,
            report=report,
            output_dir=output_dir,
            log_level=log_level,
            no_color=no_color,
        )

    return _build


# ---------------------------------------------------------------------------
# 가짜 datasets 모듈 fixture
# ---------------------------------------------------------------------------


class FakeDataset:
    """``datasets.Dataset`` 인터페이스의 최소 부분만 흉내 낸다.

    ``len``, ``__getitem__``, ``column_names``, ``select``, ``__iter__``,
    ``filter``, ``shuffle``을 지원한다. ``filter``/``shuffle``은 column 메모리
    매핑 동작 흉내가 아니라 단순 row 변환이다(테스트 격리).
    """

    def __init__(self, rows: list) -> None:
        self._rows = list(rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __getitem__(self, idx: int) -> dict:
        return dict(self._rows[idx])

    def __iter__(self):
        for row in self._rows:
            yield dict(row)

    @property
    def column_names(self) -> list:
        return list(self._rows[0].keys()) if self._rows else []

    def select(self, indices) -> "FakeDataset":
        # ``range``/iterable 모두 받도록 list 변환을 둔다.
        return FakeDataset([self._rows[i] for i in list(indices)])

    def filter(self, predicate, **_kwargs) -> "FakeDataset":
        """``Dataset.filter`` 흉내. row dict를 predicate에 그대로 넘긴다."""

        return FakeDataset([r for r in self._rows if predicate(dict(r))])

    def shuffle(self, seed: int = 0, **_kwargs) -> "FakeDataset":
        """``Dataset.shuffle`` 흉내. seed 결정적 셔플."""

        import random as _random

        rng = _random.Random(seed)
        ordered = list(self._rows)
        rng.shuffle(ordered)
        return FakeDataset(ordered)


@pytest.fixture
def fake_load_dataset(monkeypatch: pytest.MonkeyPatch, fake_persona_rows: list):
    """``datasets.load_dataset``을 ``FakeDataset`` 반환 함수로 교체하는 fixture.

    호출자는 추가 row 셋을 인자로 줄 수 있다.
    """

    rows_holder = {"rows": list(fake_persona_rows)}

    def _fake(name: str, split: str = "train", streaming: bool = False, **kwargs: Any):
        if streaming:
            return iter(dict(r) for r in rows_holder["rows"])
        return FakeDataset(rows_holder["rows"])

    # ``datasets`` 패키지가 없는 환경에서도 동작하도록 sys.modules에 mock을 주입한다.
    import types

    fake_mod = types.ModuleType("datasets")
    fake_mod.load_dataset = _fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "datasets", fake_mod)

    def _set_rows(rows: list) -> None:
        rows_holder["rows"] = list(rows)

    return _set_rows
