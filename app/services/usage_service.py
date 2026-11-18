"""
用量统计服务（P2-2 成本账单）

- seed_model_pricing: 启动时把 config/pricing.yaml 的价格种子进 model_pricing 表（upsert）
- get_usage_summary: 按用户/会话聚合调用次数、token、估算费用、平均延迟
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger_handler import logger
from app.model_config.service import utcnow
from app.models.model_trace import ModelTrace, ModelPricing
from app.utils.config import get_pricing_config


async def seed_model_pricing() -> None:
    """
    把 config/pricing.yaml 的模型定价写入 model_pricing 表（upsert）

    在应用启动时调用；失败仅告警，缺少历史估算费率的调用保留未知状态。
    """
    # 延迟导入，避免启动阶段循环依赖
    from app.db.database import async_session_factory

    pricing = get_pricing_config().get("models", {})
    if not pricing:
        logger.warning("config/pricing.yaml 无模型定价配置，历史估算费率不可用")
        return

    try:
        async with async_session_factory() as db:
            for model, p in pricing.items():
                result = await db.execute(
                    select(ModelPricing).where(ModelPricing.model == model)
                )
                row = result.scalar_one_or_none()
                values = {
                    "input_price_per_1k": float(p.get("input_price_per_1k", 0)),
                    "output_price_per_1k": float(p.get("output_price_per_1k", 0)),
                    "currency": p.get("currency", "CNY"),
                }
                if row:
                    row.input_price_per_1k = values["input_price_per_1k"]
                    row.output_price_per_1k = values["output_price_per_1k"]
                    row.currency = values["currency"]
                else:
                    db.add(ModelPricing(model=model, **values))
            await db.commit()
        logger.info(f"模型定价种子完成: {len(pricing)} 个模型")
    except Exception as e:
        logger.warning(f"模型定价种子失败（保留未知费用）: {e}")


@dataclass
class CurrencyTotal:
    priced: Decimal | None = None
    legacy: Decimal | None = None
    unknown_usage: int = 0
    unknown_price: int = 0
    legacy_count: int = 0

    def view(self, currency):
        return {'currency': currency, 'priced_amount': format(self.priced, 'f') if self.priced is not None else None,
                'legacy_estimated_amount': format(self.legacy, 'f') if self.legacy is not None else None,
                'unknown_usage_count': self.unknown_usage, 'unknown_price_count': self.unknown_price,
                'legacy_estimate_count': self.legacy_count}


class UsageCosts:
    def __init__(self):
        self.currencies = defaultdict(CurrencyTotal)
        self.unknown_cost = 0
        self.unknown_usage = 0
        self.priced_calls = 0

    def count(self, *, currency, calls, unknown_usage, has_price, amount=None, estimate=None):
        group = self.currencies[currency]
        group.unknown_usage += unknown_usage
        self.unknown_usage += unknown_usage
        if not has_price:
            group.unknown_price += calls
        if amount is not None:
            group.priced = (group.priced or Decimal(0)) + amount
            self.priced_calls += calls
        elif estimate is not None:
            group.legacy = (group.legacy or Decimal(0)) + estimate
            group.legacy_count += calls
        else:
            self.unknown_cost += calls

    def view(self, total_calls):
        totals = [self.currencies[key].view(key) for key in sorted(self.currencies, key=lambda key: key or '')]
        available = any(group.priced is not None or group.legacy is not None for group in self.currencies.values())
        status = 'complete' if total_calls and self.priced_calls == total_calls else 'partial' if available else 'unknown'
        return {'currency_totals': totals, 'cost_status': status, 'unknown_cost_count': self.unknown_cost,
                'unknown_usage_count': self.unknown_usage}

    def cny(self):
        group = self.currencies.get('CNY')
        return float(group.priced) if group and group.priced is not None else None


async def get_usage_summary(db: AsyncSession, user_id: str, session_id: Optional[str] = None, days: int = 30) -> dict:
    trace = ModelTrace
    filters = [trace.user_id == user_id, trace.created_at >= utcnow() - timedelta(days=days)]
    if session_id:
        filters.append(trace.session_id == session_id)

    usage_known = ((trace.prompt_tokens >= 0) & (trace.completion_tokens >= 0) &
                   (trace.usage_status.is_(None) | trace.usage_status.in_(('complete', 'known'))))
    legacy = trace.release_id.is_(None) & trace.is_test.is_(False) & trace.profile_revision_id.is_(None)
    quote_known = trace.price_version_id.is_not(None) & trace.cost_currency.is_not(None)
    priced = (trace.pricing_status == 'priced') & (trace.cost_amount >= 0) & quote_known & usage_known
    dimensions = (trace.is_test, trace.model, trace.provider_origin, trace.cost_currency,
                  legacy, usage_known, quote_known, priced)
    rows = (await db.execute(select(*dimensions, func.count(), func.sum(trace.prompt_tokens),
                                   func.sum(trace.completion_tokens), func.sum(trace.cost_amount))
                             .where(*filters).group_by(*dimensions))).all()
    legacy_prices = {}
    legacy_models = {row[1] for row in rows if row[4] and row[1]}
    if legacy_models:
        prices = (await db.execute(select(ModelPricing).where(ModelPricing.model.in_(legacy_models)))).scalars().all()
        legacy_prices = {price.model: price for price in prices}

    costs, test_costs = UsageCosts(), UsageCosts()
    by_model = {}
    calls = prompt = completion = test_calls = test_prompt = test_completion = 0
    for is_test, model, origin, currency, is_legacy, known, quoted, has_amount, count, in_tokens, out_tokens, amount in rows:
        in_tokens, out_tokens = int(in_tokens or 0), int(out_tokens or 0)
        estimate = None
        if is_legacy and not quoted:
            price = legacy_prices.get(model)
            if price:
                currency = price.currency
                quoted = True
                if known:
                    estimate = (Decimal(str(price.input_price_per_1k)) * in_tokens +
                                Decimal(str(price.output_price_per_1k)) * out_tokens) / 1000
        values = {'currency': currency if quoted else None, 'calls': count, 'unknown_usage': 0 if known else count,
                  'has_price': bool(quoted), 'amount': amount if has_amount else None, 'estimate': estimate}
        if is_test:
            test_calls += count
            test_prompt += in_tokens
            test_completion += out_tokens
            test_costs.count(**values)
        else:
            calls += count
            prompt += in_tokens
            completion += out_tokens
            costs.count(**values)
            model_key = (model, origin)
            if model_key not in by_model:
                by_model[model_key] = [0, UsageCosts()]
            by_model[model_key][0] += count
            by_model[model_key][1].count(**values)

    business = [*filters, trace.is_test.is_(False)]
    stage_rows = (await db.execute(select(trace.stage, func.count(), func.sum(trace.prompt_tokens),
                                         func.sum(trace.completion_tokens)).where(*business).group_by(trace.stage))).all()
    latency = (await db.execute(select(func.avg(trace.latency_ms)).where(*business))).scalar_one()
    test_view = test_costs.view(test_calls)
    priced_totals = [row['priced_amount'] for row in test_view['currency_totals'] if row['priced_amount'] is not None]
    return {
        'days': days, 'total_calls': calls, 'total_prompt_tokens': prompt, 'total_completion_tokens': completion,
        'total_tokens': prompt + completion, 'total_cost_cny': costs.cny(),
        'avg_latency_ms': round(float(latency or 0), 1), **costs.view(calls),
        'by_stage': [{'stage': stage, 'calls': count, 'prompt_tokens': int(input_sum or 0),
                      'completion_tokens': int(output_sum or 0)} for stage, count, input_sum, output_sum in stage_rows],
        'by_model': [{'model': model, 'provider_origin': origin, 'calls': count, 'cost_cny': totals.cny(),
                      **totals.view(count)} for (model, origin), (count, totals) in
                     sorted(by_model.items(), key=lambda entry: (-entry[1][0], entry[0][0] or '', entry[0][1] or ''))],
        'config_test_usage': {'calls': test_calls, 'known_prompt_tokens': test_prompt,
                              'known_completion_tokens': test_completion, **test_view,
                              'cost_amount': priced_totals[0] if len(priced_totals) == 1 else None},
    }
