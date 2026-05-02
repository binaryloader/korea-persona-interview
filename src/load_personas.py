"""Hugging Face 페르소나 데이터셋 로더와 필터 DSL.

본 모듈은 TDD §3.4, §1.6의 매핑을 그대로 따른다. 책임은 아래와 같다.

- ``nvidia/Nemotron-Personas-Korea`` 데이터셋 로드(캐시 활용)
- 필터 DSL 파싱(`age:25-39,region:서울특별시,gender:F,occupation_keyword:개발자`)
- 데이터셋 표기 정규화(별칭 적용)
- 시드 고정 샘플링(``random.Random(seed).sample`` 기반)
- ``PersonaMeta`` 변환

infrastructure 계층(``datasets`` 의존)과 domain 계층(``PersonaFilter``의 결합
규칙)이 같은 파일에 공존한다(architecture.md §1, §5의 단일 도메인 단순화).

Hugging Face 데이터셋의 컬럼 키와 값 표기는 사전 단계에서 viewer 직접 조회로
확인했고 TDD §1.1, §1.2, §1.3에 박혀 있다. 게이트 2(PRD §5.10)는 본 모듈의
``--inspect-columns`` CLI 헬퍼로 수행한다.

streaming 옵션과 in-memory 옵션 둘 다 지원한다. 기본 동작은 in-memory + 필터
적용 후 ``select(indices)``로 메모리 점유를 최소화한다(TDD §10.3 리스크 완화).
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from dataclasses import dataclass
from typing import Iterable, Optional

from .config import DatasetConfig, load_config
from .models import (
    ConfigError,
    DatasetUnavailableError,
    FilterMatchedZeroError,
    PersonaMeta,
)


logger = logging.getLogger(__name__)


# 지원하는 필터 키(PRD §5.5). 새 키 추가는 PRD 갱신과 함께 진행한다.
ALLOWED_FILTER_KEYS = frozenset(
    {"age", "gender", "region", "subregion", "occupation_keyword"}
)


# ---------------------------------------------------------------------------
# 필터 DSL 데이터 구조
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgeRange:
    """``age:25-39`` 또는 ``age:30`` 형식의 필터 값."""

    low: int
    high: int  # inclusive

    def matches(self, age: int) -> bool:
        return self.low <= age <= self.high


@dataclass(frozen=True)
class FilterSpec:
    """파싱된 필터 DSL.

    같은 키 반복은 OR 결합이라 각 키의 값이 list 형태다. 다른 키는 AND 결합이라
    각 키 모두를 만족해야 한다(PRD §5.5).

    age 필드만 ``AgeRange`` 리스트로 별도 보관해 정수 비교 효율성을 확보한다.
    문자열 키(gender/region/subregion/occupation_keyword)는 정규화된 값으로 보관한다.
    """

    age: tuple
    gender: tuple
    region: tuple
    subregion: tuple
    occupation_keyword: tuple

    def is_empty(self) -> bool:
        """필터 항목이 하나도 없으면 True. 전체 데이터셋 통과를 의미한다."""

        return not (
            self.age
            or self.gender
            or self.region
            or self.subregion
            or self.occupation_keyword
        )


# ---------------------------------------------------------------------------
# 필터 DSL 파싱
# ---------------------------------------------------------------------------


def _parse_age_value(raw: str) -> AgeRange:
    """``25-39`` 또는 ``30`` 형식의 문자열을 ``AgeRange``로 변환한다."""

    raw = raw.strip()
    if not raw:
        raise ConfigError("age 값이 비어 있다")
    if "-" in raw:
        parts = raw.split("-")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise ConfigError(
                f"age 범위 형식이 잘못됐다: {raw!r}. 올바른 예: '25-39'"
            )
        try:
            low = int(parts[0].strip())
            high = int(parts[1].strip())
        except ValueError as exc:
            raise ConfigError(
                f"age 값이 정수가 아니다: {raw!r}"
            ) from exc
        if low < 0 or high < 0:
            raise ConfigError(f"age 값은 0 이상이어야 한다: {raw!r}")
        if low > high:
            raise ConfigError(
                f"age 범위 시작이 끝보다 크다: {raw!r}"
            )
        return AgeRange(low=low, high=high)
    try:
        single = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"age 값이 정수 또는 범위가 아니다: {raw!r}"
        ) from exc
    if single < 0:
        raise ConfigError(f"age 값은 0 이상이어야 한다: {raw!r}")
    return AgeRange(low=single, high=single)


def _normalize_gender(raw: str, aliases: dict) -> str:
    """성별 입력을 데이터셋 표기(`남자`/`여자`)로 정규화한다.

    별칭은 yaml의 ``dataset.gender_aliases``를 사용한다(TDD §1.6).
    데이터셋 표기 직접 입력(`남자`/`여자`)도 그대로 통과한다.
    """

    if not raw:
        raise ConfigError("gender 값이 비어 있다")
    normalized = aliases.get(raw, raw)
    if normalized not in ("남자", "여자"):
        raise ConfigError(
            f"gender 값을 정규화할 수 없다: {raw!r}. "
            f"허용: '남자', '여자', 또는 별칭({sorted(aliases)})"
        )
    return normalized


def _normalize_province(raw: str, aliases: dict) -> str:
    """시도 입력을 데이터셋 표기(짧은 17개)로 정규화한다.

    별칭은 yaml의 ``dataset.province_aliases``를 사용한다(TDD §1.6).
    별칭에 없으면 원문을 그대로 둔다(데이터셋 표기 직접 입력 가능).
    """

    if not raw:
        raise ConfigError("region 값이 비어 있다")
    return aliases.get(raw, raw)


def parse_filter(
    filter_str: Optional[str],
    gender_aliases: dict,
    province_aliases: dict,
) -> FilterSpec:
    """필터 DSL 문자열을 ``FilterSpec``으로 변환한다.

    형식은 ``key1:value1,key2:value2`` 콤마 구분. 같은 키 반복은 OR, 다른 키는
    AND로 결합한다(PRD §5.5). 빈 문자열 또는 None이면 전체 통과 spec 반환.

    Args:
        filter_str: 필터 DSL 문자열. None 또는 빈 문자열이면 빈 spec.
        gender_aliases: ``DatasetConfig.gender_aliases``.
        province_aliases: ``DatasetConfig.province_aliases``.

    Returns:
        파싱된 ``FilterSpec``.

    Raises:
        ConfigError: 잘못된 키, 잘못된 age 형식, 빈 값 등.
    """

    if filter_str is None or not filter_str.strip():
        return FilterSpec(
            age=(),
            gender=(),
            region=(),
            subregion=(),
            occupation_keyword=(),
        )

    age_list: list = []
    gender_list: list = []
    region_list: list = []
    subregion_list: list = []
    occupation_list: list = []

    pairs = [p.strip() for p in filter_str.split(",") if p.strip()]
    if not pairs:
        raise ConfigError(
            f"필터 DSL이 비어 있다: {filter_str!r}"
        )

    for pair in pairs:
        if ":" not in pair:
            raise ConfigError(
                f"필터 항목 형식이 잘못됐다: {pair!r}. 올바른 예: 'age:25-39'"
            )
        key, _, value = pair.partition(":")
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise ConfigError(
                f"필터 키 또는 값이 비어 있다: {pair!r}"
            )
        if key not in ALLOWED_FILTER_KEYS:
            raise ConfigError(
                f"지원하지 않는 필터 키: {key!r}. "
                f"허용: {sorted(ALLOWED_FILTER_KEYS)}"
            )
        if key == "age":
            age_list.append(_parse_age_value(value))
        elif key == "gender":
            gender_list.append(_normalize_gender(value, gender_aliases))
        elif key == "region":
            region_list.append(_normalize_province(value, province_aliases))
        elif key == "subregion":
            subregion_list.append(value)
        elif key == "occupation_keyword":
            occupation_list.append(value)

    return FilterSpec(
        age=tuple(age_list),
        gender=tuple(gender_list),
        region=tuple(region_list),
        subregion=tuple(subregion_list),
        occupation_keyword=tuple(occupation_list),
    )


# ---------------------------------------------------------------------------
# 필터 적용
# ---------------------------------------------------------------------------


def _row_matches(row: dict, spec: FilterSpec, field_map: dict) -> bool:
    """단일 row가 ``FilterSpec``을 만족하는지 판정한다.

    같은 키 내부는 OR, 다른 키 사이는 AND다. 본 함수는 row 단위 ad-hoc
    호출(테스트 등)을 위해 그대로 유지하며, 매 호출마다 field 키를 dict에서
    조회한다. 데이터셋 전량 순회 경로는 ``_make_row_predicate``를 사용해 키
    조회를 사전에 한 번만 수행하도록 한다(N+1 회피).
    """

    return _make_row_predicate(spec, field_map)(row)


def _make_row_predicate(spec: FilterSpec, field_map: dict):
    """row → bool 클로저를 반환한다.

    field_map의 키 resolve를 함수 진입 시 1회만 수행해 클로저 변수로 캡처한다.
    100만 row 순회 경로에서 매 row마다 dict.get을 4회 반복하던 비용을 제거한다
    (TDD §10.3 리스크 완화 추가 보강).
    """

    if spec.is_empty():
        return lambda _row: True

    age_field = field_map.get("age", "age")
    gender_field = field_map.get("gender", "sex")
    region_field = field_map.get("region", "province")
    subregion_field = field_map.get("subregion", "district")
    occupation_field = field_map.get("occupation", "occupation")

    age_specs = spec.age
    gender_set = frozenset(spec.gender) if spec.gender else None
    region_set = frozenset(spec.region) if spec.region else None
    subregion_tokens = spec.subregion
    occupation_tokens = spec.occupation_keyword

    def predicate(row: dict) -> bool:
        if age_specs:
            raw_age = row.get(age_field)
            if not isinstance(raw_age, int):
                try:
                    raw_age = int(raw_age)
                except (TypeError, ValueError):
                    return False
            if not any(r.matches(raw_age) for r in age_specs):
                return False

        if gender_set is not None:
            if row.get(gender_field, "") not in gender_set:
                return False

        if region_set is not None:
            if row.get(region_field, "") not in region_set:
                return False

        if subregion_tokens:
            raw_sub = row.get(subregion_field, "") or ""
            # district는 `광주-서구` 형식(시도 prefix 결합형)이라 부분 매칭한다.
            if not any(token in raw_sub for token in subregion_tokens):
                return False

        if occupation_tokens:
            raw_occ = row.get(occupation_field, "") or ""
            if not any(kw in raw_occ for kw in occupation_tokens):
                return False

        return True

    return predicate


def apply_filter(
    rows: Iterable[dict],
    spec: FilterSpec,
    field_map: dict,
) -> list:
    """필터 spec을 만족하는 row들의 인덱스 리스트를 반환한다.

    원본 데이터셋 인덱스를 보존하기 위해 ``enumerate``를 사용한다. field 키
    resolve는 ``_make_row_predicate``로 진입 시 1회만 수행한다.
    """

    predicate = _make_row_predicate(spec, field_map)
    return [i for i, row in enumerate(rows) if predicate(row)]


# ---------------------------------------------------------------------------
# PersonaMeta 변환과 샘플링
# ---------------------------------------------------------------------------


def _build_persona_meta(row: dict, field_map: dict) -> PersonaMeta:
    """원본 row를 ``PersonaMeta``로 변환한다.

    ``field_map``은 PRD `persona_meta` 키 → 데이터셋 컬럼 키 매핑이다.
    ``name``은 데이터셋에 별도 이름 컬럼이 없어 ``field_map["name"] is None``이면
    ``None``으로 둔다(TDD §1.3). raw dict는 uuid를 제외하고 그대로 보존한다.
    """

    persona_id = str(row.get("uuid", ""))
    name_field = field_map.get("name")
    name_value: Optional[str] = None
    if name_field:
        raw_name = row.get(name_field)
        name_value = str(raw_name) if raw_name else None

    raw_dict = {k: v for k, v in row.items() if k != "uuid"}

    age_field = field_map.get("age", "age")
    raw_age = row.get(age_field)
    try:
        age_int = int(raw_age) if raw_age is not None else 0
    except (TypeError, ValueError) as exc:
        raise DatasetUnavailableError(
            f"페르소나 age 값을 정수로 변환할 수 없다: persona_id={persona_id!r}, "
            f"age={raw_age!r}"
        ) from exc

    family_type_field = field_map.get("family_type", "family_type")
    housing_type_field = field_map.get("housing_type", "housing_type")
    raw_family_type = row.get(family_type_field) if family_type_field else None
    raw_housing_type = row.get(housing_type_field) if housing_type_field else None
    family_type_value: Optional[str] = (
        str(raw_family_type) if raw_family_type else None
    )
    housing_type_value: Optional[str] = (
        str(raw_housing_type) if raw_housing_type else None
    )

    raw_gender = str(row.get(field_map.get("gender", "sex"), ""))
    # 데이터셋이 ``남자``/``여자`` 외 표기(``남성``/``여성``/``M``/``F``)로 갱신되어도
    # PersonaMeta 검증을 통과할 수 있도록 reverse alias를 적용한다(라운드 G16).
    # gender_aliases는 yaml에 ``F``/``M``/``남성``/``여성`` → ``남자``/``여자`` 매핑이
    # 들어 있으므로 그대로 활용한다. 함수 인자로 alias dict를 받지 않는 호출
    # 경로(테스트 등)에서는 본 정규화가 no-op이다.
    if raw_gender not in ("남자", "여자"):
        # field_map과 같은 흐름으로 raw에서 alias를 시도한다. _build_persona_meta
        # 단계는 alias dict를 직접 받지 않으므로 hard-coded 표준 매핑만 적용한다.
        _gender_normalize = {
            "남성": "남자",
            "여성": "여자",
            "M": "남자",
            "F": "여자",
            "male": "남자",
            "female": "여자",
        }
        raw_gender = _gender_normalize.get(raw_gender, raw_gender)

    return PersonaMeta(
        persona_id=persona_id,
        name=name_value,
        gender=raw_gender,
        age=age_int,
        region=str(row.get(field_map.get("region", "province"), "")),
        subregion=str(row.get(field_map.get("subregion", "district"), "")),
        occupation=str(row.get(field_map.get("occupation", "occupation"), "")),
        marital=str(row.get(field_map.get("marital", "marital_status"), "")),
        education=str(row.get(field_map.get("education", "education_level"), "")),
        raw=raw_dict,
        family_type=family_type_value,
        housing_type=housing_type_value,
    )


def _sample_indices(indices: list, n: int, seed: int) -> list:
    """``random.Random(seed).sample``로 시드 고정 샘플을 추출한다.

    같은 시드/같은 indices면 항상 같은 순서/같은 값 보장(PRD §5.5).
    """

    if n <= 0:
        raise ConfigError(f"샘플링 인원 n은 1 이상이어야 한다: {n}")
    if len(indices) < n:
        raise FilterMatchedZeroError(
            f"필터 결과 {len(indices)}명, 요청 {n}명. 필터를 완화해 주세요"
        )
    rng = random.Random(seed)
    # sample은 `random.Random` 인스턴스의 시드만 의존하므로 호출자 환경에서도
    # 재현 가능하다. 순서까지 동일하다.
    return rng.sample(indices, n)


# ---------------------------------------------------------------------------
# 데이터셋 로더
# ---------------------------------------------------------------------------


def _load_dataset_inner(
    config: DatasetConfig,
    streaming: bool,
):
    """실제 ``datasets.load_dataset`` 호출. 실패 시 도메인 예외로 변환한다.

    streaming 모드는 첫 1샘플만 빠르게 보고 싶을 때(``--inspect-columns``) 사용한다.
    필터링과 샘플링은 in-memory 경로(streaming=False)에서만 동작한다.
    """

    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise DatasetUnavailableError(
            "datasets 라이브러리를 찾을 수 없다. "
            "`pip install -r requirements.txt`로 설치해 주세요"
        ) from exc

    try:
        ds = load_dataset(
            config.name,
            split=config.split,
            streaming=streaming,
        )
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        # datasets 라이브러리는 네트워크/캐시/스키마 오류를 주로 위 4종 또는 그 하위
        # 클래스로 던진다. 본 명시 분기로 좁히되, 본 라이브러리 새 버전이 다른
        # Exception 계열을 도입할 가능성을 안전망으로 흡수한다(아래 BLE001).
        raise DatasetUnavailableError(
            f"데이터셋을 로드할 수 없습니다: {exc}. "
            "인터넷 연결과 ~/.cache/huggingface 권한을 확인해 주세요"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - datasets 내부 예외가 광범위해 안전망 유지
        raise DatasetUnavailableError(
            f"데이터셋을 로드할 수 없습니다(예상치 못한 예외 흡수): {exc}. "
            "인터넷 연결과 ~/.cache/huggingface 권한을 확인해 주세요"
        ) from exc
    return ds


def load_and_sample(
    filter_str: Optional[str],
    n: int,
    seed: int,
    field_map: dict,
    gender_aliases: dict,
    province_aliases: dict,
    dataset_name: str,
    split: str,
    persona_ids: Optional[tuple] = None,
) -> list:
    """필터 DSL 적용 후 시드 샘플링으로 ``PersonaMeta`` 리스트를 반환한다.

    같은 ``(filter_str, n, seed, field_map, dataset_name, split, persona_ids)``
    조합으로 다시 호출하면 in-memory 캐시 hit으로 즉시 반환한다.
    ``list-personas``/``interview``/``dry-run``에서 같은 표본을 반복 조회하는
    흐름의 중복 비용을 제거한다. 캐시는 프로세스 단위라 다른 프로세스에서는
    재사용되지 않는다.

    Args:
        filter_str: 필터 DSL 문자열. None이면 전체에서 샘플링.
        n: 샘플 인원.
        seed: 샘플링 시드. 같은 시드/필터/데이터셋 버전이면 같은 결과.
        field_map: yaml의 ``dataset.field_map`` dict.
        gender_aliases: yaml의 ``dataset.gender_aliases`` dict.
        province_aliases: yaml의 ``dataset.province_aliases`` dict.
        dataset_name: 데이터셋 식별자(예: ``nvidia/Nemotron-Personas-Korea``).
        split: 데이터셋 split(예: ``train``).
        persona_ids: 명시 페르소나 uuid 튜플. 지정 시 ``filter_str``/``seed``는
            정렬 안정성에만 영향을 주고 표본은 본 인자가 우선한다. 데이터셋
            row의 ``uuid`` 컬럼과 매칭한다. 일부 ID가 데이터셋에 없으면 누락된
            ID 목록을 ``ConfigError`` 메시지에 담아 raise한다. ``filter_str``과
            함께 지정되면 ID 매칭 후 추가로 필터를 통과한 row만 남긴다(교집합).

    Returns:
        ``persona_ids`` 미지정 시 길이 n의 ``PersonaMeta`` 리스트(시드 동일 시
        동일 표본 보장). ``persona_ids`` 지정 시 입력 ID 순서와 동일한 길이의
        리스트.

    Raises:
        ConfigError: 필터 DSL 파싱 실패, n <= 0, 또는 ``persona_ids``의 일부
            ID가 데이터셋에 없는 경우.
        DatasetUnavailableError: 데이터셋 로드 실패.
        FilterMatchedZeroError: 필터 결과가 n보다 적음.
    """

    if persona_ids:
        # ``persona_ids``가 지정된 경로는 시드 샘플링과 무관하게 명시 ID로 행을
        # 추출한다. 본 분기는 사용자가 ``--persona-id``로 같은 페르소나 표본에
        # 대해 다른 product/questions로 비교 인터뷰를 돌릴 때 사용한다.
        return _load_by_persona_ids(
            persona_ids=persona_ids,
            filter_str=filter_str,
            field_map=field_map,
            gender_aliases=gender_aliases,
            province_aliases=province_aliases,
            dataset_name=dataset_name,
            split=split,
        )

    cache_key = _build_cache_key(
        filter_str=filter_str,
        n=n,
        seed=seed,
        field_map=field_map,
        gender_aliases=gender_aliases,
        province_aliases=province_aliases,
        dataset_name=dataset_name,
        split=split,
    )
    cached = _PERSONA_POOL_CACHE.get(cache_key)
    if cached is not None:
        logger.info(
            "페르소나 풀 캐시 hit",
            extra={
                "filter": filter_str or "(전체)",
                "n": n,
                "seed": seed,
                "cached_count": len(cached),
            },
        )
        # frozen dataclass 리스트라 얕은 복사로 호출자가 누적/수정해도 캐시
        # 원본을 오염시키지 않게 한다.
        return list(cached)

    spec = parse_filter(filter_str, gender_aliases, province_aliases)

    cfg_for_load = DatasetConfig(
        name=dataset_name,
        split=split,
        field_map=dict(field_map),
        gender_aliases=dict(gender_aliases),
        province_aliases=dict(province_aliases),
    )

    logger.info(
        "데이터셋 로드 시작",
        extra={"dataset": dataset_name, "split": split},
    )
    ds = _load_dataset_inner(cfg_for_load, streaming=False)

    # in-memory 경로. datasets는 디스크 기반 메모리 매핑이라 100만 행을 한 번에
    # 파이썬 dict로 변환하지 않도록 분기한다(TDD §10.3 리스크 완화).
    #
    # - 빈 spec(필터 없음): 전체에서 시드 고정 샘플 추출만 필요하므로
    #   ``Dataset.shuffle(seed).select(range(n))`` 단축 경로를 사용한다. 100만
    #   row를 dict로 변환하지 않는다.
    # - 필터 있음: ``Dataset.filter(predicate, batched=False)``로 column 메모리
    #   매핑 위에서 평가한 뒤 ``select`` 인덱스를 만든다. predicate는
    #   ``_make_row_predicate``로 field 키 resolve를 진입 시 1회만 수행한다.
    try:
        if spec.is_empty():
            sampled_subset = _select_random_subset(ds, n=n, seed=seed)
        else:
            sampled_subset = _filter_and_sample(
                ds, spec=spec, field_map=field_map, n=n, seed=seed
            )
    except FilterMatchedZeroError:
        raise
    except (KeyError, IndexError, ValueError, RuntimeError) as exc:
        # datasets의 row 순회/select는 컬럼 누락(KeyError), 인덱스 범위 오류,
        # 매개변수 검증 실패(ValueError), 내부 RuntimeError를 던진다.
        raise DatasetUnavailableError(
            f"데이터셋 row 순회 또는 select 실패: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - datasets 신규 버전 예외 안전망
        raise DatasetUnavailableError(
            f"데이터셋 row 순회 또는 select 실패(예상치 못한 예외 흡수): {exc}"
        ) from exc

    personas = [_build_persona_meta(row, field_map) for row in sampled_subset]
    logger.info(
        "페르소나 샘플링 완료",
        extra={"sampled": len(personas), "seed": seed},
    )
    _PERSONA_POOL_CACHE[cache_key] = list(personas)
    return personas


# ---------------------------------------------------------------------------
# 페르소나 풀 in-memory 캐시(같은 프로세스 안 반복 호출 단축)
# ---------------------------------------------------------------------------


# key는 ``_build_cache_key``가 반환하는 hashable 튜플, value는 ``PersonaMeta``
# 리스트다. CLI 단일 프로세스 한 번 실행 안에서 ``list-personas``/``interview``/
# ``dry-run``이 같은 spec으로 반복 호출되는 흐름을 단축한다. 프로세스 종료 시
# 함께 사라진다.
_PERSONA_POOL_CACHE: dict = {}


def _build_cache_key(
    *,
    filter_str: Optional[str],
    n: int,
    seed: int,
    field_map: dict,
    gender_aliases: dict,
    province_aliases: dict,
    dataset_name: str,
    split: str,
) -> tuple:
    """샘플링 입력 전체를 hashable 튜플 키로 만든다.

    field_map/gender_aliases/province_aliases 같은 dict는 정렬된 항목 튜플로
    동결해 키에 포함한다. dict 순서가 같아도 파이썬 dict는 hashable이 아니라
    캐시 키로 직접 쓸 수 없다. 같은 데이터셋 컬럼 매핑/별칭 변경이 캐시 무효화
    조건에 정확히 들어가도록 한다.
    """

    def _freeze(d: dict) -> tuple:
        return tuple(sorted((str(k), v) for k, v in d.items()))

    return (
        filter_str or "",
        int(n),
        int(seed),
        _freeze(field_map),
        _freeze(gender_aliases),
        _freeze(province_aliases),
        dataset_name,
        split,
    )


def clear_persona_pool_cache() -> None:
    """캐시를 비운다. 테스트 격리와 모듈 외부 수동 무효화용."""

    _PERSONA_POOL_CACHE.clear()


def _select_random_subset(ds, *, n: int, seed: int):
    """필터가 비었을 때 전체에서 시드 고정 n행을 골라 ``Dataset`` 슬라이스를 반환한다.

    ``Dataset.shuffle(seed)``는 결정적(determinstic) 셔플이라 같은 seed면 같은
    순서를 반환한다. ``select(range(n))``으로 메모리 점유를 최소화한다.
    """

    total = len(ds)
    if n <= 0:
        raise ConfigError(f"샘플링 인원 n은 1 이상이어야 한다: {n}")
    if total < n:
        raise FilterMatchedZeroError(
            f"필터 결과 {total}명, 요청 {n}명. 필터를 완화해 주세요"
        )
    logger.info(
        "필터 미적용 전체 샘플링",
        extra={"total": total, "requested_n": n},
    )
    shuffled = ds.shuffle(seed=seed)
    return shuffled.select(range(n))


def _load_by_persona_ids(
    *,
    persona_ids: tuple,
    filter_str: Optional[str],
    field_map: dict,
    gender_aliases: dict,
    province_aliases: dict,
    dataset_name: str,
    split: str,
) -> list:
    """명시 ``persona_ids`` 매칭으로 ``PersonaMeta`` 리스트를 반환한다.

    ``filter_str``이 함께 지정되면 ID 매칭 후 필터를 추가로 적용한다(교집합).
    누락된 ID가 있으면 ``ConfigError``로 차단해 사용자가 정확히 어떤 ID가
    데이터셋에 없는지 즉시 알 수 있게 한다.

    캐시는 사용하지 않는다. ID 직접 지정 경로는 빈도가 낮고, 같은 프로세스
    안에서도 ID 부분 집합을 바꿔 가며 호출되는 사례가 흔해 캐시 hit 효과가
    낮기 때문이다.
    """

    spec = parse_filter(filter_str, gender_aliases, province_aliases)

    cfg_for_load = DatasetConfig(
        name=dataset_name,
        split=split,
        field_map=dict(field_map),
        gender_aliases=dict(gender_aliases),
        province_aliases=dict(province_aliases),
    )

    logger.info(
        "데이터셋 로드 시작(persona_ids 분기)",
        extra={"dataset": dataset_name, "split": split, "ids_count": len(persona_ids)},
    )
    ds = _load_dataset_inner(cfg_for_load, streaming=False)

    requested_ids = tuple(str(pid) for pid in persona_ids if str(pid).strip())
    if not requested_ids:
        raise ConfigError("persona_ids가 비어 있다. 1개 이상 지정해 주세요")
    requested_set = set(requested_ids)

    # ID 매칭 + (있다면) 필터를 같은 한 번의 ``filter`` 호출로 평가한다.
    predicate = _make_row_predicate(spec, field_map)

    def _match(row: dict) -> bool:
        if str(row.get("uuid", "")) not in requested_set:
            return False
        return predicate(row)

    try:
        filtered = ds.filter(_match)
    except (KeyError, IndexError, ValueError, RuntimeError) as exc:
        raise DatasetUnavailableError(
            f"데이터셋 row 순회 또는 select 실패: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - datasets 신규 버전 예외 안전망
        raise DatasetUnavailableError(
            f"데이터셋 row 순회 또는 select 실패(예상치 못한 예외 흡수): {exc}"
        ) from exc

    # 인덱스를 ID 키로 변환해 입력 순서대로 정렬한다.
    by_id: dict = {}
    for row in filtered:
        pid = str(row.get("uuid", ""))
        if pid in requested_set and pid not in by_id:
            by_id[pid] = row

    missing = [pid for pid in requested_ids if pid not in by_id]
    if missing:
        sample = ", ".join(missing[:5])
        more = f" 외 {len(missing) - 5}건" if len(missing) > 5 else ""
        raise ConfigError(
            f"--persona-id로 지정된 일부 ID가 데이터셋에 없거나 필터를 통과하지 못했습니다: "
            f"{sample}{more}. 총 {len(missing)}건 누락"
        )

    ordered_rows = [by_id[pid] for pid in requested_ids]
    personas = [_build_persona_meta(row, field_map) for row in ordered_rows]
    logger.info(
        "persona_ids 분기 매칭 완료",
        extra={"matched": len(personas), "requested": len(requested_ids)},
    )
    return personas


def _filter_and_sample(ds, *, spec: FilterSpec, field_map: dict, n: int, seed: int):
    """필터를 ``Dataset.filter``로 평가한 뒤 시드 고정 샘플을 ``select``로 반환한다.

    ``Dataset.filter``는 디스크 기반 메모리 매핑 위에서 column을 읽으며 매칭
    인덱스만 수집한다. ``apply_filter`` 같은 dict 변환 순회를 우회한다(O(n)
    유지, 메모리 점유 최소화).

    인덱스 보존을 위해 ``with_indices=True``로 호출하고, 매칭된 부분
    ``filtered_ds``의 길이로 ``_sample_indices``를 만들어 ``select``한다.
    같은 seed/같은 spec/같은 데이터셋 버전이면 동일한 결과를 보장한다.
    """

    predicate = _make_row_predicate(spec, field_map)

    # ``filter``는 새 ``Dataset`` 객체를 반환한다(원본 인덱스가 아니라 매칭된
    # row만 0..M-1로 재배열된다). 본 도구는 필터 후 시드 샘플링만 보장하면
    # 충분하므로 원본 인덱스 보존은 요구하지 않는다.
    filtered = ds.filter(predicate)

    matched = len(filtered)
    logger.info(
        "필터 적용 결과",
        extra={"matched": matched, "requested_n": n},
    )

    if matched < n:
        raise FilterMatchedZeroError(
            f"필터 결과 {matched}명, 요청 {n}명. 필터를 완화해 주세요"
        )
    if n <= 0:
        raise ConfigError(f"샘플링 인원 n은 1 이상이어야 한다: {n}")

    # 시드 고정 샘플링. 같은 seed/같은 matched면 같은 순서를 보장한다.
    rng = random.Random(seed)
    sampled = rng.sample(range(matched), n)
    return filtered.select(sampled)


# ---------------------------------------------------------------------------
# GATE-2 디버그 헬퍼: 컬럼 구조 휴먼 검증
# ---------------------------------------------------------------------------


def inspect_columns(
    dataset_name: str,
    split: str,
    use_streaming: bool = True,
) -> dict:
    """데이터셋의 컬럼 이름과 1샘플을 dict로 반환한다(GATE-2).

    streaming 모드를 default로 사용한다. 100만 레코드 전량 로드를 회피하면서
    첫 1샘플과 컬럼 키만 확인할 수 있다(PRD §5.10, TDD §10.3 리스크 완화).

    streaming 모드에서는 ``len(ds)``가 동작하지 않을 수 있어 첫 row를 ``next``로
    꺼낸 뒤 ``row.keys()``로 컬럼명을 확인한다. in-memory 경로(streaming=False)는
    ``ds.column_names``를 그대로 사용한다.

    Args:
        dataset_name: 데이터셋 식별자.
        split: 데이터셋 split.
        use_streaming: True면 streaming(전량 로드 회피).

    Returns:
        ``{"dataset": ..., "split": ..., "column_names": [...], "first_record": {...}}``.

    Raises:
        DatasetUnavailableError: 로드 또는 첫 row 추출 실패.
    """

    cfg = DatasetConfig(
        name=dataset_name,
        split=split,
        field_map={},
        gender_aliases={},
        province_aliases={},
    )
    ds = _load_dataset_inner(cfg, streaming=use_streaming)

    if use_streaming:
        # IterableDataset. 첫 1행만 꺼낸다.
        try:
            iterator = iter(ds)
            first_record = next(iterator)
        except StopIteration as exc:
            raise DatasetUnavailableError(
                "데이터셋이 비어 있다(streaming 모드, 첫 row 없음)"
            ) from exc
        except (OSError, ValueError, RuntimeError) as exc:
            raise DatasetUnavailableError(
                f"데이터셋 첫 row 추출 실패(streaming): {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - datasets 신규 버전 예외 안전망
            raise DatasetUnavailableError(
                f"데이터셋 첫 row 추출 실패(streaming, 예상치 못한 예외): {exc}"
            ) from exc
        column_names = list(first_record.keys()) if isinstance(first_record, dict) else []
    else:
        # in-memory. column_names가 풍부한 메타로 제공된다.
        column_names = list(getattr(ds, "column_names", []) or [])
        if len(ds) == 0:
            raise DatasetUnavailableError("데이터셋이 비어 있다(in-memory 모드)")
        first_record = dict(ds[0])

    return {
        "dataset": dataset_name,
        "split": split,
        "streaming": use_streaming,
        "column_names": column_names,
        "first_record": first_record,
    }


# ---------------------------------------------------------------------------
# CLI 진입점(`python -m src.load_personas --inspect-columns`)
# ---------------------------------------------------------------------------


def _main(argv: Optional[list] = None) -> int:
    """모듈 단위 실행 진입점. 게이트 2 휴먼 검증용이다.

    사용 예는 아래와 같다.

    ::

        python -m src.load_personas --inspect-columns
        python -m src.load_personas --inspect-columns --no-streaming

    config.yaml의 ``dataset.name``/``dataset.split``을 사용한다. 환경변수와 yaml
    경로는 ``load_config``를 그대로 거친다(우선순위 default → yaml → env → CLI).
    """

    parser = argparse.ArgumentParser(
        description="페르소나 데이터셋 컬럼 구조와 첫 1샘플을 출력한다(GATE-2)."
    )
    parser.add_argument(
        "--inspect-columns",
        action="store_true",
        help="데이터셋 컬럼 이름과 첫 1샘플을 JSON으로 출력한다",
    )
    parser.add_argument(
        "--no-streaming",
        action="store_true",
        help="streaming 모드를 끈다(전량 로드, 캐시 활용 시 빠름)",
    )
    args = parser.parse_args(argv)

    if not args.inspect_columns:
        parser.print_help()
        return 0

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"설정 로드 실패: {exc}", file=sys.stderr)
        return 1

    try:
        result = inspect_columns(
            dataset_name=cfg.dataset.name,
            split=cfg.dataset.split,
            use_streaming=not args.no_streaming,
        )
    except DatasetUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # 한국어 컬럼명/값을 그대로 보존한다. ensure_ascii=False.
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
