"""``src._pricing`` 단위 테스트.

- 모델별 단가 lookup, 알려지지 않은 모델 fallback
- ``estimate_cost_usd``: cached/uncached 분리 청구 계산
- TokenUsage가 모두 0이면 비용 0
"""

from __future__ import annotations

import pytest

from src._pricing import (
    PRICING_TABLE,
    ModelPricing,
    estimate_cost_usd,
    lookup_pricing,
)
from src.models import TokenUsage


def test_lookup_pricing_gpt_4o_mini() -> None:
    pricing = lookup_pricing("gpt-4o-mini")
    assert pricing.input == 0.15
    assert pricing.cached_input == 0.075
    assert pricing.output == 0.60


def test_lookup_pricing_알수없는_모델_fallback() -> None:
    """알려지지 않은 모델은 0이 아닌 보수 단가(gpt-4o-mini와 동일) fallback."""

    pricing = lookup_pricing("unknown-future-model")
    assert pricing.input > 0
    assert pricing.output > 0
    # fallback이 cached를 input의 절반으로 둔다(prompt caching 정책 동일).
    assert pricing.cached_input == pricing.input / 2


def test_estimate_cost_usd_빈_usage_0() -> None:
    cost = estimate_cost_usd(TokenUsage(), "gpt-4o-mini")
    assert cost == 0.0


def test_estimate_cost_usd_캐시_없음_단순_계산() -> None:
    """cached_tokens=0이면 입력 단가 × prompt + 출력 단가 × completion."""

    usage = TokenUsage(
        prompt_tokens=1_000_000,
        completion_tokens=500_000,
        total_tokens=1_500_000,
        cached_tokens=0,
    )
    cost = estimate_cost_usd(usage, "gpt-4o-mini")
    # 1M prompt × $0.15 + 0.5M completion × $0.60 = $0.15 + $0.30 = $0.45
    assert cost == pytest.approx(0.45, rel=1e-9)


def test_estimate_cost_usd_캐시_적용시_50_절감() -> None:
    """cached_tokens는 입력 단가의 50%로 청구된다(2026-05 정책).

    1M prompt 중 1M 모두 cached면 입력 비용이 절반(0.075/M)이 된다.
    """

    full_cached = TokenUsage(
        prompt_tokens=1_000_000,
        completion_tokens=0,
        total_tokens=1_000_000,
        cached_tokens=1_000_000,
    )
    full_uncached = TokenUsage(
        prompt_tokens=1_000_000,
        completion_tokens=0,
        total_tokens=1_000_000,
        cached_tokens=0,
    )
    cost_cached = estimate_cost_usd(full_cached, "gpt-4o-mini")
    cost_uncached = estimate_cost_usd(full_uncached, "gpt-4o-mini")
    assert cost_cached == pytest.approx(cost_uncached / 2, rel=1e-9)


def test_estimate_cost_usd_부분_캐시_분리_청구() -> None:
    """1M 중 0.8M cached면 0.2M는 표준 단가, 0.8M는 cached 단가."""

    usage = TokenUsage(
        prompt_tokens=1_000_000,
        completion_tokens=200_000,
        total_tokens=1_200_000,
        cached_tokens=800_000,
    )
    cost = estimate_cost_usd(usage, "gpt-4o-mini")
    # 0.2M × $0.15 + 0.8M × $0.075 + 0.2M × $0.60
    # = $0.030 + $0.060 + $0.120 = $0.210
    assert cost == pytest.approx(0.21, rel=1e-9)


def test_estimate_cost_usd_cached_초과_방어() -> None:
    """cached_tokens > prompt_tokens 같은 비정상 입력은 음수 분기를 0으로 클램프한다."""

    usage = TokenUsage(
        prompt_tokens=100,
        completion_tokens=0,
        total_tokens=100,
        cached_tokens=999,  # 비정상
    )
    cost = estimate_cost_usd(usage, "gpt-4o-mini")
    # uncached_prompt=0, cached_prompt=100 → 100 * 0.075 / 1M
    assert cost == pytest.approx(100 * 0.075 / 1_000_000, rel=1e-9)


def test_PRICING_TABLE_gpt_4o_별도_단가() -> None:
    """gpt-4o는 gpt-4o-mini보다 단가가 높다."""

    mini = PRICING_TABLE["gpt-4o-mini"]
    full = PRICING_TABLE["gpt-4o"]
    assert full.input > mini.input
    assert full.output > mini.output
