"""Process-local admission for a request's configured model attempts."""
from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, localcontext
import re
from typing import TYPE_CHECKING

import sqlalchemy as sa

from app.core.failed_response import BusinessError, ErrorCode
from app.model_config.endpoints import canonical_url
from app.model_config.pricing import MONEY_SCALE, parse_rate
from app.model_config.security import scoped
from app.model_config.tables import model_price_versions


if TYPE_CHECKING:
    from app.model_config.durable_task_budget import DurableTaskModelBudget

current_model_budget: ContextVar[TaskModelBudget | DurableTaskModelBudget | None] = ContextVar('task_model_budget', default=None)
TOOL_CALL_LIMIT_EXCEEDED = 'TOOL_CALL_LIMIT_EXCEEDED'


class ModelBudgetDenied(BusinessError):
    def __init__(self, reason, *, reported_usage=None):
        message = '本次任务的工具调用次数已用完' if reason == TOOL_CALL_LIMIT_EXCEEDED else None
        super().__init__(ErrorCode.MODEL_BUDGET_EXCEEDED, message=message, http_status=429, data={'reason': reason})
        self.reported_usage = reported_usage

    def stream_event(self):
        return {'type': 'error', 'code': self.code, 'reason': self.data['reason'],
                'retryable': False, 'content': self.message}


@dataclass
class TaskModelBudget:
    max_calls: int | None
    max_tokens: int | None
    cost_limits: Mapping[str, Decimal]
    calls: int = 0
    tokens: int = 0
    costs: dict[str, Decimal] = field(default_factory=dict)
    stopped_reason: str | None = None
    max_tool_calls: int | None = None
    tool_calls: int = 0

    @property
    def has_model_limits(self):
        return self.max_calls is not None or self.max_tokens is not None or bool(self.cost_limits)

    @property
    def tool_limited(self):
        return self.max_tool_calls is not None

    @classmethod
    def configured(cls):
        from app.utils.config import load_yaml

        config = load_yaml('agent.yaml').get('task_model_budget', {})
        if not isinstance(config, dict) or config.keys() - {'max_calls', 'max_tokens', 'cost_limits', 'max_tool_calls'}:
            raise ValueError('Invalid task_model_budget configuration')
        calls, tokens = config.get('max_calls'), config.get('max_tokens')
        if any(value is not None and (type(value) is not int or value <= 0) for value in (calls, tokens)):
            raise ValueError('Model call and token limits must be positive integers or null')
        tool_calls = config.get('max_tool_calls')
        if tool_calls is not None and (type(tool_calls) is not int or not 1 <= tool_calls <= 2**63 - 1):
            raise ValueError('Tool call limit must be a positive integer or null')
        limits = config.get('cost_limits', {})
        if not isinstance(limits, dict) or any(not isinstance(key, str) or not re.fullmatch('[A-Z]{3}', key)
                                               or not isinstance(value, str) for key, value in limits.items()):
            raise ValueError('Cost limits must map currency codes to quoted decimal amounts')
        try:
            costs = {key: parse_rate(value) for key, value in limits.items()}
        except BusinessError as error:
            raise ValueError('Invalid model cost limit') from error
        budget = cls(calls, tokens, costs, max_tool_calls=tool_calls)
        return budget if budget.has_model_limits or tool_calls is not None else None

    def check_open(self):
        if self.stopped_reason is not None:
            raise ModelBudgetDenied(self.stopped_reason)

    def stop(self, reason, *, reported_usage=None):
        if self.stopped_reason is None:
            self.stopped_reason = reason
        raise ModelBudgetDenied(reason, reported_usage=reported_usage)

    async def reserve_tool(self):
        if self.max_tool_calls is not None:
            self.check_open()
            if self.tool_calls >= self.max_tool_calls:
                self.stop(TOOL_CALL_LIMIT_EXCEEDED)
            self.tool_calls += 1

    async def reserve(self, profile, price_at, session_factory, *, output_limit, input_count):
        self.check_open()
        needs_bound = self.max_tokens is not None or bool(self.cost_limits)
        window = profile.get('context_window')
        if needs_bound and (type(window) is not int or window <= 0 or output_limit > window):
            self.stop('MODEL_CONTEXT_BOUND_UNKNOWN')
        input_bound = window * input_count if needs_bound else 0
        quote = None
        if self.cost_limits:
            try:
                quote = await active_quote(session_factory, profile, price_at)
            except sa.exc.SQLAlchemyError:
                self.stop('MODEL_PRICE_LOOKUP_FAILED')

        # Apply all counters together, without suspension between checking and reserving.
        self.check_open()
        if self.max_calls is not None and self.calls >= self.max_calls:
            self.stop('MODEL_CALL_LIMIT_EXCEEDED')
        total = input_bound + output_limit if needs_bound else 0
        if self.max_tokens is not None and self.tokens + total > self.max_tokens:
            self.stop('MODEL_TOKEN_LIMIT_EXCEEDED')
        if self.cost_limits:
            if quote is None:
                self.stop('MODEL_PRICE_UNAVAILABLE')
            if quote['currency'] not in self.cost_limits:
                self.stop('MODEL_BUDGET_CURRENCY_UNAVAILABLE')
            amount = charge(quote, input_bound, output_limit)
            currency = quote['currency']
            spent = self.costs.get(currency, Decimal(0)) + amount
            if spent > self.cost_limits[currency]:
                self.stop('MODEL_COST_LIMIT_EXCEEDED')
            self.costs[currency] = spent
        else:
            amount = Decimal(0)
        self.calls += 1
        self.tokens += total
        return ModelReservation(self, input_bound, output_limit if needs_bound else 0, quote, amount)


@dataclass
class ModelReservation:
    budget: TaskModelBudget
    input_bound: int
    output_bound: int
    quote: Mapping | None
    amount: Decimal
    settled: bool = False

    async def settle(self, prompt, completion):
        if self.settled or any(type(value) is not int or value < 0 for value in (prompt, completion)):
            return
        self.settled = True
        if not self.input_bound:
            return
        if prompt > self.input_bound or completion > self.output_bound:
            self.budget.stop('MODEL_USAGE_EXCEEDS_DECLARED_BOUND', reported_usage=(prompt, completion))
        self.budget.tokens -= self.input_bound + self.output_bound - prompt - completion
        if self.quote is not None:
            self.budget.costs[self.quote['currency']] -= self.amount - charge(self.quote, prompt, completion)


def charge(quote, prompt, completion):
    with localcontext() as context:
        context.prec = 50
        amount = (prompt * quote['input_unit_price'] + completion * quote['output_unit_price']) / quote['unit_tokens']
        return amount.quantize(MONEY_SCALE, rounding=ROUND_CEILING)


async def active_quote(session_factory, profile, price_at):
    origin, _ = canonical_url(profile['base_url'])
    table = model_price_versions
    query = sa.select(table).where(scoped(table), table.c.origin == origin, table.c.model == profile['model'],
        table.c.effective_from <= datetime.fromisoformat(price_at)).order_by(
            table.c.effective_from.desc(), table.c.price_version.desc()).limit(1)
    async with session_factory() as db:
        quote = (await db.execute(query)).mappings().first()
    if quote and quote['origin'] == origin and quote['model'] == profile['model']:
        return quote
