"""Verify exact rate arithmetic and currency aggregation on disposable MySQL."""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_pricing_gate(engine, Database, actor, control, report):
    from app.model_config.endpoints import canonical_url
    from app.model_config.pricing import price_trace_batch, register_price
    from app.model_config.service import utcnow
    from app.model_config.tables import ai_profile_revisions
    from app.model_config.task_budget import ModelBudgetDenied, TaskModelBudget
    from app.models.model_trace import ModelTrace
    from app.services.usage_service import get_usage_summary

    with Session(engine) as session:
        profile = session.execute(sa.select(ai_profile_revisions).where(
            ai_profile_revisions.c.status == 'published').limit(1)).mappings().one()
        origin, _ = canonical_url(profile['base_url'])

    def register(key, currency, rate):
        with Session(engine) as session:
            result, status = asyncio.run(register_price(control, Database(session), actor.user_id,
                origin=origin, model=profile['model'], currency=currency, unit_tokens=1000,
                input_price=rate, output_price='0', source='isolated-price-fixture', request_key=key))
        assert status == 201
        return result

    usd = register('mysql-price-usd', 'USD', '0.1234567891')
    old_time = utcnow().isoformat()
    cny = register('mysql-price-cny', 'CNY', '0')
    event = {'release_id': 'fixture-release', 'profile_revision_id': profile['id'], 'provider_origin': origin,
             'user_id': actor.user_id, 'model': profile['model'][:100], 'prompt_tokens': 1000,
             'completion_tokens': 0, 'usage_status': 'complete', 'pricing_status': 'unknown'}
    events = [{**event, '_price_at': old_time}, {**event, '_price_at': utcnow().isoformat()},
              {**event, '_price_at': utcnow().isoformat(), 'usage_status': 'unknown', 'completion_tokens': None}]
    with Session(engine) as session:
        old, new, unknown = asyncio.run(price_trace_batch(Database(session), events))
    assert old['price_version_id'] == usd['id'] and old['cost_amount'] == Decimal('0.1234567891')
    assert new['price_version_id'] == unknown['price_version_id'] == cny['id'] and new['cost_amount'] == 0
    assert unknown.get('cost_amount') is None
    report['checks']['pricing_as_of_exact_decimal_and_configured_zero_mysql'] = True

    @asynccontextmanager
    async def session_factory():
        with Session(engine) as session:
            yield Database(session)

    async def check_budget_quotes():
        budget = TaskModelBudget(None, None, {'USD': Decimal('0.1234567891')})
        bounded = {**profile, 'context_window': 1000}
        call = await budget.reserve(bounded, old_time, session_factory, output_limit=0, input_count=1)
        assert call.quote['id'] == usd['id'] and budget.costs['USD'] == Decimal('0.1234567891')
        await call.settle(1, 0)
        assert budget.costs['USD'] == Decimal('0.0001234568')
        try:
            await budget.reserve(bounded, utcnow().isoformat(), session_factory, output_limit=0, input_count=1)
        except ModelBudgetDenied as error:
            assert error.data['reason'] == 'MODEL_BUDGET_CURRENCY_UNAVAILABLE'
        else:
            raise AssertionError('Unexpected debit in a currency absent from the task budget')
        assert budget.calls == 1

    asyncio.run(check_budget_quotes())
    report['checks']['budget_dispatch_quote_decimal_settlement_and_currency_gate_mysql'] = True

    # The existing migration gate has a minimal historical model_traces fixture.
    # Shadow it on this connection with the actual mapped columns for the query.
    table = ModelTrace.__table__
    ddl = str(sa.schema.CreateTable(table).compile(engine)).replace('CREATE TABLE', 'CREATE TEMPORARY TABLE', 1)
    with engine.connect() as conn:
        conn.exec_driver_sql(ddl)
        try:
            for row in (old, new, unknown):
                conn.execute(table.insert().values(**row))
            with Session(bind=conn) as session:
                summary = asyncio.run(get_usage_summary(Database(session), actor.user_id))
            totals = {row['currency']: row for row in summary['currency_totals']}
            assert summary['cost_status'] == 'partial' and summary['unknown_cost_count'] == 1
            assert totals['USD']['priced_amount'] == '0.1234567891'
            assert Decimal(totals['CNY']['priced_amount']) == 0 and totals['CNY']['unknown_usage_count'] == 1
        finally:
            conn.exec_driver_sql('DROP TEMPORARY TABLE model_traces')
    report['checks']['pricing_summary_currency_groups_and_null_amount_mysql'] = True
