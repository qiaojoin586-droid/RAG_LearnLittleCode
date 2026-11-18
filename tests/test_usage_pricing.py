"""Price versions, scope and money totals checked with isolated SQL fixtures."""

from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.core.model_trace import DbSink
from app.model_config import tables as t
from app.model_config.pricing import price_trace_batch, register_price
from app.model_config.service import uid, utcnow
from app.models.model_trace import ModelPricing, ModelTrace
from app.services.usage_service import get_usage_summary
from test_model_config_control_plane import AsyncFacade, admin, profile_op, store  # noqa: F401
from test_model_config_publication import mock_runtime, publish, ready, selected, snapshot


async def rate(admin, *, key, origin='https://models.example.com', model='fixture-model', currency='USD', value='2.10'):
    with Session(admin.engine) as session:
        result, status = await register_price(admin.service, AsyncFacade(session), admin.actor.user_id,
            origin=origin, model=model, currency=currency, unit_tokens=1000,
            input_price=value, output_price=value, source='fixture-rate-card', request_key=key)
        assert status == 201
        return result


@pytest.mark.asyncio
async def test_price_registration_is_admin_only_immutable_and_idempotent(admin):
    first = await rate(admin, key='rate-1')
    assert await rate(admin, key='rate-1', value='2.100') == first
    with pytest.raises(BusinessError) as error:
        await rate(admin, key='rate-1', value='3')
    assert error.value.code == 40922
    second = await rate(admin, key='rate-2', value='3')
    assert first['id'] != second['id'] and first['price_version'] == 1 and second['price_version'] == 2
    assert first['effective_from'] <= second['effective_from']
    assert len(admin.rows(t.model_price_versions)) == 2
    assert admin.count(t.ai_config_audits) == 3  # bootstrap plus two price versions
    admin.sql(t.ai_config_admin_grants.update().values(config_role_key='config_operator'))
    with pytest.raises(BusinessError) as denied:
        await rate(admin, key='denied')
    assert denied.value.code == 40312


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['-1', '1e-6', 'NaN', '0.00000000001', '10000000000'])
async def test_price_input_rejects_invalid_or_out_of_range_decimals(admin, value):
    with pytest.raises(BusinessError) as error:
        await rate(admin, key='invalid', value=value)
    assert error.value.data['reason'] == 'PRICE_RATE_INVALID' and admin.count(t.model_price_versions) == 0


@pytest.mark.asyncio
async def test_as_of_pricing_uses_exact_profile_identity_and_preserves_unknown_usage(admin):
    name = 'x' * 105 + '-same-model'
    draft = await admin.draft()
    policy = await admin.policy()
    operation = profile_op(policy)
    operation['profile']['model'] = name
    await admin.call('patch_draft', draft['id'], {'operations': [operation]}, 'profile', draft['etag'], admin.proof())
    profile = dict(admin.rows(t.ai_profile_revisions)[0])
    second_profile = {**profile, 'id': uid(), 'revision': 2, 'base_url': 'https://other.example/v1'}
    admin.sql(t.ai_profile_revisions.insert().values(**second_profile))
    first = await rate(admin, key='first', model=name)
    latest = await rate(admin, key='latest', model=name, value='3')
    other = await rate(admin, key='other', model=name, origin='https://other.example', currency='CNY', value='0')
    now = utcnow()
    admin.sql(t.model_price_versions.update().where(t.model_price_versions.c.id == first['id']).values(effective_from=now - timedelta(hours=2)))
    admin.sql(t.model_price_versions.update().where(t.model_price_versions.c.id == latest['id']).values(effective_from=now - timedelta(minutes=20)))
    event = {'profile_revision_id': profile['id'], 'release_id': 'release', 'provider_origin': policy['origin'],
             'model': name[:100], 'prompt_tokens': 1000, 'completion_tokens': 500, 'usage_status': 'complete',
             'pricing_status': 'unknown', 'cost_amount': None, '_price_at': now.isoformat()}
    events = [{**event, '_price_at': (now - timedelta(hours=1)).isoformat()}, event,
              {**event, 'profile_revision_id': second_profile['id'], 'provider_origin': 'https://other.example'},
              {**event, 'completion_tokens': None, 'usage_status': 'unknown'},
              {**event, 'provider_origin': 'https://unpriced.example'},
              {**event, '_price_at': (now - timedelta(days=1)).isoformat()}]
    with Session(admin.engine) as session:
        old, new, zero, unknown, mismatched, historical = await price_trace_batch(AsyncFacade(session), events)
    assert (old['price_version_id'], old['cost_amount']) == (first['id'], Decimal('3.15'))
    assert (new['price_version_id'], new['cost_amount']) == (latest['id'], Decimal('4.5'))
    assert (zero['price_version_id'], zero['cost_amount'], zero['pricing_status']) == (other['id'], 0, 'priced')
    assert unknown['price_version_id'] == latest['id'] and unknown['cost_amount'] is None
    assert 'price_version_id' not in mismatched and 'price_version_id' not in historical
    assert '_price_at' not in old and event['cost_amount'] is None


@pytest.mark.asyncio
async def test_summary_keeps_currencies_history_test_calls_and_users_separate(admin):
    ModelTrace.__table__.create(admin.engine)
    ModelPricing.__table__.create(admin.engine)
    admin.sql(ModelPricing.__table__.insert().values(model='fixture', input_price_per_1k=2,
              output_price_per_1k=3, currency='EUR'))
    trace = ModelTrace.__table__
    business = {'user_id': admin.actor.user_id, 'session_id': 'mine', 'model': 'fixture', 'stage': 'chat',
                'release_id': 'release', 'prompt_tokens': 1000, 'completion_tokens': 1000, 'usage_status': 'complete'}
    quotes = [{'cost_currency': 'USD', 'price_version_id': 'rate-us', 'cost_amount': Decimal('1.2345678912')},
              {'cost_currency': 'CNY', 'price_version_id': 'rate-cn', 'cost_amount': Decimal('0')}]
    for index, quote in enumerate(quotes, start=1):
        admin.sql(trace.insert().values(**business, **quote, id=index, provider_origin=f'https://p{index}.example', pricing_status='priced'))
    admin.sql(trace.insert().values(**{**business, 'release_id': None}, id=3))
    admin.sql(trace.insert().values(**{**business, 'completion_tokens': None, 'usage_status': 'unknown'}, id=4,
              provider_origin='https://p1.example', price_version_id='rate-us', cost_currency='USD'))
    admin.sql(trace.insert().values(**business, id=5, provider_origin='https://unpriced.example'))
    admin.sql(trace.insert().values(**business, **quotes[0], id=6, is_test=True, pricing_status='priced'))
    admin.sql(trace.insert().values(**{**business, 'user_id': admin.other.user_id}, **quotes[0], id=7, pricing_status='priced'))
    admin.sql(trace.insert().values(**{**business, 'session_id': 'elsewhere'}, **quotes[0], id=8, pricing_status='priced'))
    with Session(admin.engine) as session:
        summary = await get_usage_summary(AsyncFacade(session), admin.actor.user_id, session_id='mine')
    totals = {row['currency']: row for row in summary['currency_totals']}
    assert summary['total_calls'] == 5 and summary['cost_status'] == 'partial'
    assert summary['unknown_cost_count'] == 2 and summary['unknown_usage_count'] == 1
    assert totals['USD']['priced_amount'] == '1.2345678912' and totals['USD']['unknown_usage_count'] == 1
    assert Decimal(totals['CNY']['priced_amount']) == 0 and summary['total_cost_cny'] == 0
    assert totals['EUR']['legacy_estimated_amount'] == '5.0' and totals['EUR']['legacy_estimate_count'] == 1
    assert totals[None]['priced_amount'] is None and totals[None]['unknown_price_count'] == 1
    assert summary['config_test_usage']['calls'] == 1 and summary['config_test_usage']['cost_status'] == 'complete'
    assert summary['config_test_usage']['cost_amount'] == '1.2345678912'


@pytest.mark.asyncio
async def test_normal_chat_trace_persists_the_rate_active_at_dispatch(admin, monkeypatch):
    from app.model_config.runtime import model_for

    draft = await ready(admin, local=False)
    await publish(admin, draft)
    pinned = await snapshot(admin)
    first = await rate(admin, key='before-call', model='example-model', value='2')
    captured = []
    monkeypatch.setattr('app.core.model_trace._emit', captured.append)

    async def respond(_):
        await rate(admin, key='during-call', model='example-model', value='8')
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}],
            'usage': {'prompt_tokens': 1000, 'completion_tokens': 500}})

    mock_runtime(admin, monkeypatch, respond)
    with selected(admin, pinned):
        assert (await model_for('chat').ainvoke('fictional test')).content == 'OK'
    event = next(row for row in captured if row['success'])
    ModelTrace.__table__.create(admin.engine)

    class TraceSession(AsyncFacade):
        def add_all(self, models):
            self.session.add_all(models)

        async def commit(self):
            self.session.commit()

    @asynccontextmanager
    async def session_factory():
        with Session(admin.engine) as session:
            yield TraceSession(session)

    monkeypatch.setattr('app.db.database.async_session_factory', session_factory)
    await DbSink()._flush([{**event, 'id': 1}])
    row = admin.rows(ModelTrace.__table__)[0]
    assert row['price_version_id'] == first['id'] and row['cost_amount'] == Decimal('3')
    assert row['release_id'] == pinned.release_id and row['model'] == 'example-model'
