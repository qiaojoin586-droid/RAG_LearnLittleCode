"""
用量统计响应模型（P2-2 成本账单）
"""

from typing import Literal

from pydantic import BaseModel


class StageUsage(BaseModel):
    """按阶段聚合的调用统计"""
    stage: str | None = None
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class CurrencyUsage(BaseModel):
    currency: str | None
    priced_amount: str | None
    legacy_estimated_amount: str | None
    unknown_usage_count: int
    unknown_price_count: int
    legacy_estimate_count: int


class UsageCosts(BaseModel):
    currency_totals: list[CurrencyUsage]
    cost_status: Literal['complete', 'partial', 'unknown']
    unknown_cost_count: int
    unknown_usage_count: int


class ModelUsage(UsageCosts):
    """按模型聚合的调用统计与费用"""
    model: str | None = None
    calls: int = 0
    provider_origin: str | None = None
    cost_cny: float | None = None


class ConfigTestUsage(UsageCosts):
    calls: int
    known_prompt_tokens: int
    known_completion_tokens: int
    cost_amount: str | None


class UsageSummaryResponse(UsageCosts):
    """用量/费用汇总响应"""
    days: int
    total_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    total_cost_cny: float | None
    avg_latency_ms: float
    by_stage: list[StageUsage]
    by_model: list[ModelUsage]
    config_test_usage: ConfigTestUsage
