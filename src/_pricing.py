"""OpenAI 모델별 토큰 단가 표와 비용 추정 헬퍼.

본 모듈은 외부 의존이 없는 순수 도메인 헬퍼다(architecture.md §1). 단가 표는
모델 추가/변경 시 본 파일만 갱신하면 된다(infrastructure 단가 정보는 OpenAI
공식 가격 페이지 https://openai.com/api/pricing 을 출처로 한다. 변경 시점에
따라 실제 청구 단가와 미세 차이가 있을 수 있어 ``estimate_cost_usd`` 결과는
"추정" 표기를 호출자에서 명시한다).

단가 단위는 1M 토큰당 USD다. ``cached_input``은 OpenAI prompt caching 적용 시
입력 토큰의 환급 단가로, 표준 입력 단가의 50%다. cached 환급은 ``input``
단가에서 ``cached_input`` 단가로 자동 대체되어 청구된다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import TokenUsage


@dataclass(frozen=True)
class ModelPricing:
    """모델별 1M 토큰당 USD 단가.

    - input: 캐시 미적용 입력 토큰 단가
    - cached_input: prompt caching 적용 입력 토큰 단가(보통 input의 50%)
    - output: 출력 토큰 단가
    """

    input: float
    cached_input: float
    output: float


# OpenAI 공식 가격 페이지 단가 스냅샷. 모델 추가 시 본 dict에만 항목을 더하면
# 된다. 정확한 단가는 OpenAI 공식 페이지를 우선하며 본 표는 추정용이다.
PRICING_TABLE: dict = {
    "gpt-4o-mini": ModelPricing(input=0.15, cached_input=0.075, output=0.60),
    "gpt-4o-mini-2024-07-18": ModelPricing(
        input=0.15, cached_input=0.075, output=0.60
    ),
    "gpt-4o": ModelPricing(input=2.50, cached_input=1.25, output=10.00),
    "gpt-4o-2024-08-06": ModelPricing(input=2.50, cached_input=1.25, output=10.00),
    "gpt-4-turbo": ModelPricing(input=10.00, cached_input=5.00, output=30.00),
    "gpt-3.5-turbo": ModelPricing(input=0.50, cached_input=0.25, output=1.50),
}


# 알려지지 않은 모델 ID에 대한 fallback 단가. 비용 0으로 두면 비용이 거의 0인
# 것처럼 표시되어 호출자가 비용을 인지하지 못할 위험이 있다. gpt-4o-mini와
# 같은 단가를 fallback으로 사용해 보수적으로 표시하고 호출자에서 "추정"임을
# 명시한다.
_FALLBACK_PRICING = ModelPricing(input=0.15, cached_input=0.075, output=0.60)


def lookup_pricing(model: str) -> ModelPricing:
    """모델 ID에 해당하는 단가를 반환한다.

    매핑이 없으면 ``_FALLBACK_PRICING``을 반환한다(0이 아닌 보수 단가). 호출자
    는 결과 표시 시 "추정"임을 명시해야 한다.
    """

    return PRICING_TABLE.get(model, _FALLBACK_PRICING)


def estimate_cost_usd(usage: TokenUsage, model: str) -> float:
    """토큰 사용량과 모델 단가로 비용을 추정한다(USD).

    수식은 아래와 같다.

    ::

        cost = (prompt_tokens - cached_tokens) * input_price / 1M
             + cached_tokens * cached_input_price / 1M
             + completion_tokens * output_price / 1M

    cached_tokens가 prompt_tokens보다 클 일은 표준 OpenAI 응답에서 발생하지
    않지만 안전망으로 음수 분기는 0으로 클램프한다.
    """

    pricing = lookup_pricing(model)
    uncached_prompt = max(0, usage.prompt_tokens - usage.cached_tokens)
    cached_prompt = min(usage.cached_tokens, usage.prompt_tokens)
    cost = (
        uncached_prompt * pricing.input
        + cached_prompt * pricing.cached_input
        + usage.completion_tokens * pricing.output
    ) / 1_000_000.0
    return cost
