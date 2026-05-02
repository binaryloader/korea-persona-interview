"""Per-model token-price table and cost estimation helper.

Prices are USD per 1M tokens. ``cached_input`` is the per-token rate applied
to the cached portion of input tokens (50% of ``input`` for OpenAI, the
provider-published cache_read rate for Anthropic). Numbers are reference
snapshots from the official pricing pages and should be treated as estimates;
callers must surface "estimated" wording in user-facing output.

Sources:

- OpenAI: https://openai.com/api/pricing
- Anthropic: https://www.anthropic.com/pricing
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import TokenUsage


@dataclass(frozen=True)
class ModelPricing:
    """USD price per 1M tokens for one model."""

    input: float
    cached_input: float
    output: float


PRICING_TABLE: dict = {
    # OpenAI Chat Completions
    "gpt-4o-mini": ModelPricing(input=0.15, cached_input=0.075, output=0.60),
    "gpt-4o-mini-2024-07-18": ModelPricing(
        input=0.15, cached_input=0.075, output=0.60
    ),
    "gpt-4o": ModelPricing(input=2.50, cached_input=1.25, output=10.00),
    "gpt-4o-2024-08-06": ModelPricing(input=2.50, cached_input=1.25, output=10.00),
    "gpt-4-turbo": ModelPricing(input=10.00, cached_input=5.00, output=30.00),
    "gpt-3.5-turbo": ModelPricing(input=0.50, cached_input=0.25, output=1.50),
    # Anthropic Messages
    # Numbers are estimates based on https://www.anthropic.com/pricing snapshots.
    # cache_read pricing is published separately at 0.1x of input for current
    # Claude generations.
    "claude-haiku-4-5": ModelPricing(input=1.00, cached_input=0.10, output=5.00),
    "claude-sonnet-4-5": ModelPricing(input=3.00, cached_input=0.30, output=15.00),
    "claude-opus-4-5": ModelPricing(input=15.00, cached_input=1.50, output=75.00),
}


_FALLBACK_PRICING = ModelPricing(input=0.15, cached_input=0.075, output=0.60)


def lookup_pricing(model: str) -> ModelPricing:
    """Return the price for ``model`` or a conservative fallback."""

    return PRICING_TABLE.get(model, _FALLBACK_PRICING)


def estimate_cost_usd(usage: TokenUsage, model: str) -> float:
    """Estimate USD cost for one request given token usage and model id.

    The formula splits ``prompt_tokens`` into a cached and uncached portion
    so the cache-read rate applies only to ``cached_tokens``::

        cost = (prompt_tokens - cached_tokens) * input_price / 1M
             + cached_tokens * cached_input_price / 1M
             + completion_tokens * output_price / 1M

    Negative branches are clamped to zero as a safety net for malformed input.
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
