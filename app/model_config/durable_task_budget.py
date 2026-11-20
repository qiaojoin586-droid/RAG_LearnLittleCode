"""Serialize a detached chat's model budget with the Worker ownership check."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

import sqlalchemy as sa

from app.model_config.task_budget import ModelBudgetDenied, ModelReservation, TaskModelBudget
from app.models.workflow_model_budget import workflow_model_budgets as budgets, workflow_model_reservations as reservations
from app.models.workflow_run import workflow_runs


async def create_budget(db, workflow_id, policy):
    await db.execute(budgets.insert().values(workflow_id=workflow_id, calls=0, tokens=0, costs={}, limits={
        'max_calls': str(policy.max_calls) if policy.max_calls is not None else None,
        'max_tokens': str(policy.max_tokens) if policy.max_tokens is not None else None,
        'max_tool_calls': str(policy.max_tool_calls) if policy.max_tool_calls is not None else None,
        'cost_limits': {currency: format(value, 'f') for currency, value in policy.cost_limits.items()},
    }))


async def get_budget_row(db, workflow_id, *, lock=False):
    query = sa.select(budgets).where(budgets.c.workflow_id == workflow_id)
    if lock:
        query = query.with_for_update()
    return (await db.execute(query)).mappings().one_or_none()


class DurableTaskModelBudget:
    def __init__(self, workflow, row):
        self.workflow = workflow
        self.stopped_reason = row['stopped_reason']
        self.tool_limited = row.get('limits', {}).get('max_tool_calls') is not None

    def check_open(self):
        if self.stopped_reason is not None:
            raise ModelBudgetDenied(self.stopped_reason)

    @asynccontextmanager
    async def _locked(self):
        from app.ai_service.workflows import lease_lost

        try:
            async with self.workflow.session_factory() as db:
                async with db.begin():
                    # Match claim/cancel ordering. The same workflow lock also serializes parallel calls.
                    active = sa.select(workflow_runs.c.id).where(*self.workflow._active(sa.func.current_timestamp()))
                    if await db.scalar(active.with_for_update()) is None:
                        raise lease_lost()
                    row = await get_budget_row(db, self.workflow.id, lock=True)
                    if row is None:
                        raise ModelBudgetDenied('MODEL_BUDGET_STORE_UNAVAILABLE')
                    limits = row['limits']
                    ledger = TaskModelBudget(
                        int(limits['max_calls']) if limits['max_calls'] is not None else None,
                        int(limits['max_tokens']) if limits['max_tokens'] is not None else None,
                        {key: Decimal(value) for key, value in limits['cost_limits'].items()},
                        calls=row['calls'], tokens=row['tokens'],
                        costs={key: Decimal(value) for key, value in row['costs'].items()},
                        stopped_reason=row['stopped_reason'],
                        max_tool_calls=int(limits['max_tool_calls']) if limits.get('max_tool_calls') is not None else None,
                        tool_calls=row['tool_calls'],
                    )
                    yield db, ledger
            self.stopped_reason = self.stopped_reason or ledger.stopped_reason
        except sa.exc.SQLAlchemyError:
            raise ModelBudgetDenied('MODEL_BUDGET_STORE_UNAVAILABLE') from None

    async def _save(self, db, ledger):
        await db.execute(budgets.update().where(budgets.c.workflow_id == self.workflow.id).values(
            calls=ledger.calls, tokens=ledger.tokens, tool_calls=ledger.tool_calls, stopped_reason=ledger.stopped_reason,
            costs={key: format(value, 'f') for key, value in ledger.costs.items()},
        ))

    async def reserve_tool(self):
        if not self.tool_limited:
            return
        self.check_open()
        denied = None
        async with self._locked() as (db, ledger):
            try:
                await ledger.reserve_tool()
            except ModelBudgetDenied as error:
                denied = error
            await self._save(db, ledger)
        if denied is not None:
            raise denied

    async def reserve(self, profile, price_at, session_factory, *, output_limit, input_count):
        self.check_open()
        denied = None
        reservation_id = str(uuid4())
        async with self._locked() as (db, ledger):
            @asynccontextmanager
            async def same_transaction():
                yield db

            try:
                # The request-local and durable ledgers use the same limits, estimates and price lookup.
                prepared = await ledger.reserve(profile, price_at, same_transaction, output_limit=output_limit,
                                                  input_count=input_count)
            except ModelBudgetDenied as error:
                denied = error
            else:
                quote = prepared.quote
                await db.execute(reservations.insert().values(
                    id=reservation_id, workflow_id=self.workflow.id, execution_attempt=self.workflow.attempt,
                    profile_revision_id=profile.get('id'), price_version_id=quote['id'] if quote else None,
                    input_bound=prepared.input_bound, output_bound=prepared.output_bound,
                    currency=quote['currency'] if quote else None, reserved_amount=prepared.amount, status='reserved',
                ))
            await self._save(db, ledger)
        if denied is not None:
            raise denied
        return DurableModelReservation(self, reservation_id, prepared)


@dataclass(frozen=True)
class DurableModelReservation:
    budget: DurableTaskModelBudget
    id: str
    prepared: ModelReservation

    async def settle(self, prompt, completion):
        if any(type(value) is not int or value < 0 for value in (prompt, completion)):
            return
        denied = None
        async with self.budget._locked() as (db, ledger):
            key = (reservations.c.id == self.id, reservations.c.workflow_id == self.budget.workflow.id,
                   reservations.c.execution_attempt == self.budget.workflow.attempt)
            status = await db.scalar(sa.select(reservations.c.status).where(*key))
            if status != 'reserved':
                return
            attempt = self.prepared
            settlement = ModelReservation(ledger, attempt.input_bound, attempt.output_bound, attempt.quote, attempt.amount)
            try:
                await settlement.settle(prompt, completion)
            except ModelBudgetDenied as error:
                denied = error
            await db.execute(reservations.update().where(*key).values(status='over_bound' if denied else 'settled'))
            await self.budget._save(db, ledger)
        if denied is not None:
            raise denied
