"""A detached model budget survives the lease that first reserved it."""

import asyncio
from decimal import Decimal

import httpx
import pytest

from app.ai_service.detached_worker import DetachedChatWorker
from app.ai_service.workflow_events import read_workflow_events
from app.core.failed_response import BusinessError
from app.core.model_trace import price_time
from app.model_config.durable_task_budget import DurableTaskModelBudget
from app.model_config.runtime import model_for
from app.model_config.task_budget import ModelBudgetDenied, TaskModelBudget
from app.models.workflow_model_budget import workflow_model_budgets as budgets, workflow_model_reservations as reservations
from test_chat_workflows import fixture as checkpoint_fixture, read  # noqa: F401
from test_detached_worker import enqueue, expire_lease
from test_model_config_control_plane import admin, store  # noqa: F401
from test_model_config_publication import mock_runtime, selected
from test_model_fallback import verified
from test_task_model_budget import use_budget
from test_transactional_tool_operations import local_tools  # noqa: F401
from test_usage_pricing import rate
from test_workflow_event_replay import journal_fixture  # noqa: F401


@pytest.fixture
def durable(journal_fixture):
    budgets.create(journal_fixture.store.engine)
    reservations.create(journal_fixture.store.engine)
    return journal_fixture


async def start(durable, policy, release_id=None):
    with use_budget(policy):
        workflow_id = await enqueue(durable, release_id)
    return workflow_id, await DetachedChatWorker(durable.factory, None).claim()


@pytest.mark.asyncio
async def test_resumed_model_accounts_for_unknown_prior_attempt_and_original_limits(durable, admin, monkeypatch):
    pinned = await verified(admin)
    await rate(admin, key='usd', model='example-model', value='1')
    workflow_id, first = await start(durable, TaskModelBudget(2, 17000, {'USD': Decimal('17')}), pinned.release_id)
    profile = pinned.profiles[model_for_profile_id(pinned)]
    lost = await first.budget.reserve(profile, price_time(), durable.factory, output_limit=256, input_count=1)
    expire_lease(durable)
    monkeypatch.setattr(TaskModelBudget, 'configured', classmethod(lambda cls: cls(1, None, {})))
    resumed = await DetachedChatWorker(durable.factory, None).claim()
    assert resumed.workflow.id == workflow_id and resumed.workflow.attempt == 2
    with pytest.raises(BusinessError) as stale:
        await lost.settle(1, 1)
    assert stale.value.data['reason'] == 'WORKFLOW_LEASE_LOST'
    with pytest.raises(BusinessError):
        await first.budget.reserve(profile, price_time(), durable.factory, output_limit=256, input_count=1)

    requests = mock_runtime(admin, monkeypatch, lambda _: httpx.Response(200, json={
        'choices': [{'message': {'content': 'recovered attempt'}}], 'usage': {'prompt_tokens': 90, 'completion_tokens': 10},
    }))
    with selected(admin, pinned), use_budget(resumed.budget):
        assert (await model_for('chat').ainvoke('resume')).content == 'recovered attempt'
        with pytest.raises(ModelBudgetDenied) as denied:
            await model_for('chat').ainvoke('cannot start a third attempt')
    assert denied.value.data['reason'] == 'MODEL_CALL_LIMIT_EXCEEDED'
    assert len(requests) == 1
    ledger, = durable.store.rows(budgets)
    assert ledger['calls'] == 2 and ledger['tokens'] == 8548
    assert Decimal(ledger['costs']['USD']) == Decimal('8.548')
    assert ledger['stopped_reason'] == 'MODEL_CALL_LIMIT_EXCEEDED'
    assert {row['execution_attempt']: row['status'] for row in durable.store.rows(reservations)} == {1: 'reserved', 2: 'settled'}


def model_for_profile_id(snapshot):
    return snapshot.manifest['resolved_static_bindings']['chat']['profile_revision_id']


@pytest.mark.asyncio
async def test_settlement_is_idempotent_and_uses_the_reserved_price(durable, admin):
    pinned = await verified(admin)
    await rate(admin, key='price-a', model='example-model', value='1')
    _, job = await start(durable, TaskModelBudget(None, 12000, {'USD': Decimal('10')}), pinned.release_id)
    profile = pinned.profiles[model_for_profile_id(pinned)]
    reserved = await job.budget.reserve(profile, price_time(), durable.factory, output_limit=256, input_count=1)
    await rate(admin, key='price-b', model='example-model', value='90')
    await reserved.settle(False, 1)
    assert durable.store.rows(reservations)[0]['status'] == 'reserved'
    await reserved.settle(50, 30)
    await reserved.settle(2, 2)
    ledger, = durable.store.rows(budgets)
    assert (ledger['calls'], ledger['tokens'], Decimal(ledger['costs']['USD'])) == (1, 80, Decimal('0.08'))
    assert durable.store.rows(reservations)[0]['status'] == 'settled'


@pytest.mark.asyncio
async def test_parallel_readers_share_the_same_limit_and_persist_the_stop(durable):
    workflow_id, job = await start(durable, TaskModelBudget(None, 15, {}))
    second = DurableTaskModelBudget(job.workflow, durable.store.rows(budgets)[0])
    results = await asyncio.gather(
        job.budget.reserve({'context_window': 10}, price_time(), durable.factory, output_limit=0, input_count=1),
        second.reserve({'context_window': 10}, price_time(), durable.factory, output_limit=0, input_count=1),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ModelBudgetDenied) for result in results) == 1
    ledger, = durable.store.rows(budgets)
    assert ledger['tokens'] == 10 and ledger['calls'] == 1
    expire_lease(durable)
    assert await DetachedChatWorker(durable.factory, None).claim() is None
    assert (await read(durable, workflow_id))['error_code'] == 'MODEL_TOKEN_LIMIT_EXCEEDED'


@pytest.mark.asyncio
async def test_tool_budget_survives_takeover_and_rejects_stale_reservations(durable, monkeypatch):
    workflow_id, first = await start(durable, TaskModelBudget(None, None, {}, max_tool_calls=2))
    await first.budget.reserve_tool()
    expire_lease(durable)
    monkeypatch.setattr(TaskModelBudget, 'configured', classmethod(lambda cls: cls(None, None, {}, max_tool_calls=100)))
    resumed = await DetachedChatWorker(durable.factory, None).claim()
    assert resumed.workflow.id == workflow_id and resumed.workflow.attempt == 2
    with pytest.raises(BusinessError) as stale:
        await first.budget.reserve_tool()
    assert stale.value.data['reason'] == 'WORKFLOW_LEASE_LOST'
    await resumed.budget.reserve_tool()
    with pytest.raises(ModelBudgetDenied) as denied:
        await resumed.budget.reserve_tool()
    assert denied.value.data['reason'] == 'TOOL_CALL_LIMIT_EXCEEDED'
    row, = durable.store.rows(budgets)
    assert row['tool_calls'] == 2 and row['calls'] == 0 and row['limits']['max_tool_calls'] == '2'
    assert row['stopped_reason'] == 'TOOL_CALL_LIMIT_EXCEEDED'
    expire_lease(durable)
    assert await DetachedChatWorker(durable.factory, None).claim() is None
    async with durable.factory() as db:
        replay = await read_workflow_events(db, durable.user_id, workflow_id, 0, 10)
    assert replay['status'] == 'failed' and len(replay['events']) == 1
    assert all(replay['events'][0][key] == value for key, value in denied.value.stream_event().items())


@pytest.mark.asyncio
async def test_missing_persistent_budget_fails_closed(durable):
    workflow_id, _ = await start(durable, TaskModelBudget(2, None, {}))
    durable.store.sql(budgets.delete())
    expire_lease(durable)
    assert await DetachedChatWorker(durable.factory, None).claim() is None
    assert (await read(durable, workflow_id))['error_code'] == 'MODEL_BUDGET_STORE_UNAVAILABLE'


@pytest.mark.asyncio
async def test_usage_over_the_declared_bound_persists_stop_and_does_not_refund(durable):
    _, job = await start(durable, TaskModelBudget(5, 30, {}))
    reserved = await job.budget.reserve({'context_window': 10}, price_time(), durable.factory,
                                       output_limit=2, input_count=1)
    with pytest.raises(ModelBudgetDenied) as denied:
        await reserved.settle(1, 3)
    reason = 'MODEL_USAGE_EXCEEDS_DECLARED_BOUND'
    assert denied.value.data['reason'] == reason and denied.value.reported_usage == (1, 3)
    assert durable.store.rows(reservations)[0]['status'] == 'over_bound'
    assert durable.store.rows(budgets)[0]['tokens'] == 12
    reloaded = DurableTaskModelBudget(job.workflow, durable.store.rows(budgets)[0])
    with pytest.raises(ModelBudgetDenied) as stopped:
        reloaded.check_open()
    assert stopped.value.data['reason'] == reason
