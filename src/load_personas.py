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


# 지원하는 필터 키. v1에서 더 이상 확장하지 않는다(PRD §5.5).
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

    return PersonaMeta(
        persona_id=persona_id,
        name=name_value,
        gender=str(row.get(field_map.get("gender", "sex"), "")),
        age=age_int,
        region=str(row.get(field_map.get("region", "province"), "")),
        subregion=str(row.get(field_map.get("subregion", "district"), "")),
        occupation=str(row.get(field_map.get("occupation", "occupation"), "")),
        marital=str(row.get(field_map.get("marital", "marital_status"), "")),
        education=str(row.get(field_map.get("education", "education_level"), "")),
        raw=raw_dict,
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
    except Exception as exc:  # datasets 내부 예외 종류가 광범위하다
        raise DatasetUnavailableError(
            f"데이터셋을 로드할 수 없습니다: {exc}. "
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
) -> list:
    """필터 DSL 적용 후 시드 샘플링으로 ``PersonaMeta`` 리스트를 반환한다.

    Args:
        filter_str: 필터 DSL 문자열. None이면 전체에서 샘플링.
        n: 샘플 인원.
        seed: 샘플링 시드. 같은 시드/필터/데이터셋 버전이면 같은 결과.
        field_map: yaml의 ``dataset.field_map`` dict.
        gender_aliases: yaml의 ``dataset.gender_aliases`` dict.
        province_aliases: yaml의 ``dataset.province_aliases`` dict.
        dataset_name: 데이터셋 식별자(예: ``nvidia/Nemotron-Personas-Korea``).
        split: 데이터셋 split(예: ``train``).

    Returns:
        길이 n의 ``PersonaMeta`` 리스트. 시드 동일 시 동일 표본 보장.

    Raises:
        ConfigError: 필터 DSL 파싱 실패 또는 n <= 0.
        DatasetUnavailableError: 데이터셋 로드 실패.
        FilterMatchedZeroError: 필터 결과가 n보다 적음.
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
    except Exception as exc:  # 데이터셋 내부 예외(KeyError 등)
        raise DatasetUnavailableError(
            f"데이터셋 row 순회 또는 select 실패: {exc}"
        ) from exc

    personas = [_build_persona_meta(row, field_map) for row in sampled_subset]
    logger.info(
        "페르소나 샘플링 완료",
        extra={"sampled": len(personas), "seed": seed},
    )
    return personas


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
        except Exception as exc:
            raise DatasetUnavailableError(
                f"데이터셋 첫 row 추출 실패(streaming): {exc}"
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
