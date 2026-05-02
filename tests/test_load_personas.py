"""``src.load_personas`` 단위/통합 테스트.

- 필터 DSL 파싱: AND/OR 결합, 잘못된 형식, age 범위/단일값
- 별칭 매핑: ``F`` → ``여자``, ``서울특별시`` → ``서울``, ``남성`` → ``남자``
- 시드 고정 샘플링 재현성
- 필터 결과 0건 → ``FilterMatchedZeroError``
- ``_build_persona_meta``의 raw dict 보존과 uuid 제외
- ``load_and_sample`` E2E(가짜 datasets로)
- ``inspect_columns``(streaming, in-memory)
"""

from __future__ import annotations

import pytest

from src.load_personas import (
    AgeRange,
    FilterSpec,
    _build_persona_meta,
    _row_matches,
    _sample_indices,
    apply_filter,
    inspect_columns,
    load_and_sample,
    parse_filter,
)
from src.models import (
    ConfigError,
    DatasetUnavailableError,
    FilterMatchedZeroError,
    PersonaMeta,
)


_ALIASES = {
    "F": "여자",
    "M": "남자",
    "여성": "여자",
    "남성": "남자",
}
_PROVINCE_ALIASES = {
    "서울특별시": "서울",
    "부산광역시": "부산",
    "경기도": "경기",
}
_FIELD_MAP = {
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
}


# ---------------------------------------------------------------------------
# parse_filter
# ---------------------------------------------------------------------------


def test_parse_filter_None_빈_spec() -> None:
    spec = parse_filter(None, _ALIASES, _PROVINCE_ALIASES)
    assert spec.is_empty()


def test_parse_filter_빈문자열_빈_spec() -> None:
    spec = parse_filter("   ", _ALIASES, _PROVINCE_ALIASES)
    assert spec.is_empty()


def test_parse_filter_age_범위() -> None:
    spec = parse_filter("age:25-39", _ALIASES, _PROVINCE_ALIASES)
    assert spec.age == (AgeRange(low=25, high=39),)


def test_parse_filter_age_단일값() -> None:
    spec = parse_filter("age:30", _ALIASES, _PROVINCE_ALIASES)
    assert spec.age == (AgeRange(low=30, high=30),)


def test_parse_filter_AND_결합_여러_키() -> None:
    spec = parse_filter(
        "age:25-39,region:서울특별시,gender:F",
        _ALIASES,
        _PROVINCE_ALIASES,
    )
    assert spec.age == (AgeRange(low=25, high=39),)
    assert spec.region == ("서울",)  # 별칭 적용
    assert spec.gender == ("여자",)  # 별칭 적용


def test_parse_filter_OR_결합_같은_키_반복() -> None:
    spec = parse_filter(
        "region:서울특별시,region:경기도",
        _ALIASES,
        _PROVINCE_ALIASES,
    )
    assert spec.region == ("서울", "경기")


def test_parse_filter_지원하지_않는_키_ConfigError() -> None:
    with pytest.raises(ConfigError):
        parse_filter("hobby:테니스", _ALIASES, _PROVINCE_ALIASES)


@pytest.mark.parametrize(
    "raw",
    [
        "age",  # 콜론 없음
        "age:",  # 값 없음
        ":25-39",  # 키 없음
        "age:abc",  # 정수 아님
        "age:30-",  # 범위 끝 없음
        "age:39-25",  # 시작 > 끝
        "age:-5",  # 음수
    ],
)
def test_parse_filter_잘못된_형식_ConfigError(raw: str) -> None:
    with pytest.raises(ConfigError):
        parse_filter(raw, _ALIASES, _PROVINCE_ALIASES)


def test_parse_filter_gender_별칭_정역_매핑() -> None:
    """``F``/``M``과 ``여성``/``남성``이 모두 데이터셋 표기로 매핑된다."""

    spec1 = parse_filter("gender:F", _ALIASES, _PROVINCE_ALIASES)
    assert spec1.gender == ("여자",)

    spec2 = parse_filter("gender:M", _ALIASES, _PROVINCE_ALIASES)
    assert spec2.gender == ("남자",)

    spec3 = parse_filter("gender:여성", _ALIASES, _PROVINCE_ALIASES)
    assert spec3.gender == ("여자",)

    spec4 = parse_filter("gender:남자", _ALIASES, _PROVINCE_ALIASES)
    assert spec4.gender == ("남자",)


def test_parse_filter_gender_허용외_ConfigError() -> None:
    with pytest.raises(ConfigError):
        parse_filter("gender:other", _ALIASES, _PROVINCE_ALIASES)


def test_parse_filter_subregion_그대로() -> None:
    spec = parse_filter(
        "subregion:강남구",
        _ALIASES,
        _PROVINCE_ALIASES,
    )
    assert spec.subregion == ("강남구",)


def test_parse_filter_occupation_keyword_그대로() -> None:
    spec = parse_filter(
        "occupation_keyword:개발자",
        _ALIASES,
        _PROVINCE_ALIASES,
    )
    assert spec.occupation_keyword == ("개발자",)


# ---------------------------------------------------------------------------
# _row_matches / apply_filter
# ---------------------------------------------------------------------------


def _row(**overrides) -> dict:
    base = {
        "uuid": "u",
        "sex": "여자",
        "age": 30,
        "province": "서울",
        "district": "서울-강남구",
        "occupation": "소프트웨어 엔지니어",
        "persona": "요약",
    }
    base.update(overrides)
    return base


def test_row_matches_빈_spec_전부_통과() -> None:
    spec = parse_filter(None, _ALIASES, _PROVINCE_ALIASES)
    assert _row_matches(_row(), spec, _FIELD_MAP) is True


def test_row_matches_age_범위() -> None:
    spec = parse_filter("age:25-39", _ALIASES, _PROVINCE_ALIASES)
    assert _row_matches(_row(age=30), spec, _FIELD_MAP)
    assert _row_matches(_row(age=25), spec, _FIELD_MAP)
    assert _row_matches(_row(age=39), spec, _FIELD_MAP)
    assert not _row_matches(_row(age=24), spec, _FIELD_MAP)
    assert not _row_matches(_row(age=40), spec, _FIELD_MAP)


def test_row_matches_AND_여러_키() -> None:
    spec = parse_filter(
        "age:25-39,region:서울특별시,gender:F",
        _ALIASES,
        _PROVINCE_ALIASES,
    )
    assert _row_matches(_row(age=27, sex="여자", province="서울"), spec, _FIELD_MAP)
    assert not _row_matches(_row(age=27, sex="남자", province="서울"), spec, _FIELD_MAP)
    assert not _row_matches(_row(age=27, sex="여자", province="경기"), spec, _FIELD_MAP)


def test_row_matches_OR_같은_키() -> None:
    spec = parse_filter(
        "region:서울특별시,region:경기도",
        _ALIASES,
        _PROVINCE_ALIASES,
    )
    assert _row_matches(_row(province="서울"), spec, _FIELD_MAP)
    assert _row_matches(_row(province="경기"), spec, _FIELD_MAP)
    assert not _row_matches(_row(province="부산"), spec, _FIELD_MAP)


def test_row_matches_subregion_부분_매칭() -> None:
    spec = parse_filter("subregion:강남구", _ALIASES, _PROVINCE_ALIASES)
    assert _row_matches(_row(district="서울-강남구"), spec, _FIELD_MAP)
    assert not _row_matches(_row(district="서울-마포구"), spec, _FIELD_MAP)


def test_row_matches_occupation_keyword_부분_매칭() -> None:
    spec = parse_filter("occupation_keyword:개발자", _ALIASES, _PROVINCE_ALIASES)
    assert _row_matches(_row(occupation="백엔드 개발자"), spec, _FIELD_MAP)
    assert not _row_matches(_row(occupation="교사"), spec, _FIELD_MAP)


def test_apply_filter_인덱스_보존() -> None:
    rows = [
        _row(age=20),
        _row(age=30),
        _row(age=40),
    ]
    spec = parse_filter("age:25-39", _ALIASES, _PROVINCE_ALIASES)
    indices = apply_filter(rows, spec, _FIELD_MAP)
    assert indices == [1]


# ---------------------------------------------------------------------------
# 시드 고정 샘플링
# ---------------------------------------------------------------------------


def test_sample_indices_시드_고정_재현성() -> None:
    indices = list(range(100))
    a = _sample_indices(indices, n=10, seed=42)
    b = _sample_indices(indices, n=10, seed=42)
    assert a == b


def test_sample_indices_다른_시드_다른_결과_가능성() -> None:
    indices = list(range(100))
    a = _sample_indices(indices, n=10, seed=42)
    b = _sample_indices(indices, n=10, seed=99)
    # 동일할 확률은 낮지만 0은 아님 - 표본 길이만 검증
    assert len(a) == 10 and len(b) == 10


def test_sample_indices_표본_부족_FilterMatchedZeroError() -> None:
    with pytest.raises(FilterMatchedZeroError):
        _sample_indices(indices=[1, 2, 3], n=5, seed=42)


def test_sample_indices_n_0_ConfigError() -> None:
    with pytest.raises(ConfigError):
        _sample_indices(indices=[1, 2, 3], n=0, seed=42)


# ---------------------------------------------------------------------------
# _build_persona_meta
# ---------------------------------------------------------------------------


def test_build_persona_meta_기본_매핑(fake_persona_row: dict) -> None:
    persona = _build_persona_meta(fake_persona_row, _FIELD_MAP)
    assert isinstance(persona, PersonaMeta)
    assert persona.persona_id == fake_persona_row["uuid"]
    assert persona.gender == fake_persona_row["sex"]
    assert persona.age == fake_persona_row["age"]
    assert persona.region == fake_persona_row["province"]
    assert persona.subregion == fake_persona_row["district"]
    assert persona.occupation == fake_persona_row["occupation"]
    assert persona.marital == fake_persona_row["marital_status"]
    assert persona.education == fake_persona_row["education_level"]
    # name은 매핑이 None이라 None
    assert persona.name is None
    # raw에는 uuid 빠지고 나머지 보존
    assert "uuid" not in persona.raw
    assert "persona" in persona.raw


def test_build_persona_meta_age_정수아님_DatasetUnavailableError() -> None:
    row = _row(age="abc")
    with pytest.raises(DatasetUnavailableError):
        _build_persona_meta(row, _FIELD_MAP)


# ---------------------------------------------------------------------------
# load_and_sample E2E (가짜 datasets)
# ---------------------------------------------------------------------------


def test_load_and_sample_E2E_정상(fake_load_dataset, fake_persona_rows: list) -> None:
    """가짜 datasets로 5명 데이터에서 필터 + 샘플링."""

    personas = load_and_sample(
        filter_str="region:서울특별시",
        n=2,
        seed=42,
        field_map=_FIELD_MAP,
        gender_aliases=_ALIASES,
        province_aliases=_PROVINCE_ALIASES,
        dataset_name="fake/dataset",
        split="train",
    )
    assert len(personas) == 2
    for p in personas:
        assert p.region == "서울"


def test_load_and_sample_시드_고정_재현성(
    fake_load_dataset, fake_persona_rows: list
) -> None:
    a = load_and_sample(
        filter_str=None,
        n=3,
        seed=42,
        field_map=_FIELD_MAP,
        gender_aliases=_ALIASES,
        province_aliases=_PROVINCE_ALIASES,
        dataset_name="fake/dataset",
        split="train",
    )
    b = load_and_sample(
        filter_str=None,
        n=3,
        seed=42,
        field_map=_FIELD_MAP,
        gender_aliases=_ALIASES,
        province_aliases=_PROVINCE_ALIASES,
        dataset_name="fake/dataset",
        split="train",
    )
    assert [p.persona_id for p in a] == [p.persona_id for p in b]


def test_load_and_sample_필터_0건_FilterMatchedZeroError(
    fake_load_dataset, fake_persona_rows: list
) -> None:
    with pytest.raises(FilterMatchedZeroError):
        load_and_sample(
            filter_str="age:90-99",  # 데이터에 90대 없음
            n=1,
            seed=42,
            field_map=_FIELD_MAP,
            gender_aliases=_ALIASES,
            province_aliases=_PROVINCE_ALIASES,
            dataset_name="fake/dataset",
            split="train",
        )


def test_load_and_sample_n_초과_FilterMatchedZeroError(
    fake_load_dataset, fake_persona_rows: list
) -> None:
    with pytest.raises(FilterMatchedZeroError):
        load_and_sample(
            filter_str=None,
            n=100,  # 가짜 데이터셋은 5명
            seed=42,
            field_map=_FIELD_MAP,
            gender_aliases=_ALIASES,
            province_aliases=_PROVINCE_ALIASES,
            dataset_name="fake/dataset",
            split="train",
        )


# ---------------------------------------------------------------------------
# inspect_columns(GATE-2 휴먼 검증 헬퍼)
# ---------------------------------------------------------------------------


def test_inspect_columns_streaming_첫_row_반환(
    fake_load_dataset, fake_persona_rows: list
) -> None:
    result = inspect_columns(
        dataset_name="fake/dataset",
        split="train",
        use_streaming=True,
    )
    assert "uuid" in result["column_names"]
    assert "sex" in result["column_names"]
    assert isinstance(result["first_record"], dict)


def test_inspect_columns_in_memory_column_names(
    fake_load_dataset, fake_persona_rows: list
) -> None:
    result = inspect_columns(
        dataset_name="fake/dataset",
        split="train",
        use_streaming=False,
    )
    assert "uuid" in result["column_names"]


def test_inspect_columns_빈_dataset_DatasetUnavailableError(
    fake_load_dataset,
) -> None:
    fake_load_dataset([])
    with pytest.raises(DatasetUnavailableError):
        inspect_columns(
            dataset_name="fake/dataset",
            split="train",
            use_streaming=True,
        )


# ---------------------------------------------------------------------------
# FilterSpec 헬퍼
# ---------------------------------------------------------------------------


def test_filter_spec_is_empty() -> None:
    empty = FilterSpec(age=(), gender=(), region=(), subregion=(), occupation_keyword=())
    assert empty.is_empty()
    not_empty = FilterSpec(
        age=(AgeRange(20, 30),),
        gender=(),
        region=(),
        subregion=(),
        occupation_keyword=(),
    )
    assert not not_empty.is_empty()
