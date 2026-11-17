"""Exercise admission on actual adapter calls and the ASGI request boundary."""

import asyncio
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
import pytest

from app.ai_service.agent import AgentFactory
from app.ai_service.chat_route import ChatRouteContext, react_events
from app.model_config.provider import ProviderFailure, ProviderGateway
from app.model_config.runtime import model_for
from app.model_config.snapshots import SnapshotMiddleware, inference_actor
from app.model_config.task_budget import ModelBudgetDenied, TaskModelBudget, current_model_budget
from test_model_config_control_plane import admin, store  # noqa: F401
from test_model_config_publication import database_factory, mock_runtime, publish, ready, selected, snapshot
from test_model_fallback import verified
from test_usage_pricing import rate


@contextmanager
def use_budget(budget):
    token = current_model_budget.set(budget)
    try:
        yield budget
    finally:
        current_model_budget.reset(token)


@pytest.mark.parametrize('config', [{'max_tokens': True}, {'max_calls': 0}, {'max_tokes': 10}, {'max_tool_calls': False},
                                   {'cost_limits': {'USD': 0.1}}, {'cost_limits': {'USD': 'NaN'}}])
def test_invalid_budget_config_does_not_silently_disable_limits(monkeypatch, config):
    monkeypatch.setattr('app.utils.config.load_yaml', lambda _: {'task_model_budget': config})
    with pytest.raises(ValueError):
        TaskModelBudget.configured()


class ToolCallingModel(BaseChatModel):
    names: list[str]

    @property
    def _llm_type(self):
        return 'tool-budget-fixture'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if isinstance(messages[-1], ToolMessage):
            message = AIMessage(content='complete')
        else:
            message = AIMessage(content='', tool_calls=[{'name': name, 'args': {}, 'id': f'call-{index}'}
                                                        for index, name in enumerate(self.names)])
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.asyncio
@pytest.mark.parametrize('names, exhausted', [(['unknown_tool', 'read_record'], False),
                                             (['read_record', 'read_record'], True)])
async def test_tool_only_budget_limits_actual_agent_dispatch(monkeypatch, names, exhausted):
    monkeypatch.setattr('app.utils.config.load_yaml', lambda _: {'task_model_budget': {'max_tool_calls': 1}})
    calls = []

    @tool
    async def read_record() -> str:
        """Return a fixture value."""
        calls.append('started')
        return 'value'

    with use_budget(TaskModelBudget.configured()) as ledger:
        assert not ledger.has_model_limits
        model = ToolCallingModel(names=names)
        if exhausted:
            monkeypatch.setattr('app.ai_service.agent_runner.create_agent_tools', lambda **_: [read_record])
            monkeypatch.setattr('app.ai_service.chat_route.get_reflection_config', lambda: {'l2_repair': {'enable': True}})
            monkeypatch.setattr('app.ai_service.stream.default_middleware.trigger', AsyncMock())
            ctx = ChatRouteContext(model, 'fixture-user', 'query', '', [], None)
            events = [event async for event in react_events(ctx, timeout=5)]
            errors = [event for event in events if event['type'] == 'error']
            assert len(errors) == 1 and errors[0]['reason'] == 'TOOL_CALL_LIMIT_EXCEEDED'
            assert errors[0]['retryable'] is False and len(calls) <= 1
            assert all(event.get('stage') != 'repairing' for event in events)
        else:
            agent, _ = AgentFactory.create_agent(model, [read_record])
            result = await agent.ainvoke({'messages': [HumanMessage(content='query')]})
            assert result['messages'][-1].content == 'complete' and calls == ['started']
        assert ledger.tool_calls == 1


@pytest.mark.asyncio
async def test_tool_only_limit_allows_legacy_requests_without_release(admin, monkeypatch):
    monkeypatch.delenv('DB_CONFIG_REQUIRED', raising=False)
    monkeypatch.setattr('app.utils.config.load_yaml', lambda _: {'task_model_budget': {'max_tool_calls': 1}})
    app = FastAPI()
    app.add_middleware(SnapshotMiddleware, session_factory=database_factory(admin))

    @app.get('/api/v1/tool-budget-fixture')
    async def budget_probe():
        return {'tool_limit': current_model_budget.get().max_tool_calls}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        response = await client.get('/api/v1/tool-budget-fixture')
    assert response.status_code == 200 and response.json() == {'tool_limit': 1}
    assert current_model_budget.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('backup', [False, True])
async def test_retry_and_backup_need_separate_admission_without_phantom_usage(admin, monkeypatch, backup):
    if backup:
        pinned = await verified(admin)
    else:
        await publish(admin, await ready(admin, local=False))
        pinned = await snapshot(admin)
    events = []
    monkeypatch.setattr('app.core.model_trace._emit', events.append)
    requests = mock_runtime(admin, monkeypatch, lambda _: httpx.Response(503, headers={'Retry-After': '0'}))
    with selected(admin, pinned), use_budget(TaskModelBudget(1, None, {})) as ledger:
        with pytest.raises(ModelBudgetDenied) as denied:
            await model_for('chat').ainvoke('budgeted question')
    assert denied.value.data['reason'] == 'MODEL_CALL_LIMIT_EXCEEDED'
    assert len(requests) == ledger.calls == 1
    assert [event['stage'] for event in events] == ['model_retry']


@pytest.mark.asyncio
async def test_cost_refund_uses_dispatch_price_version_and_preserves_currencies(admin, monkeypatch):
    pinned = await verified(admin)
    await rate(admin, key='usd', model='example-model', value='1')
    await rate(admin, key='cny', model='backup-model', currency='CNY', value='2')
    arrived = asyncio.Event()
    complete = asyncio.Event()

    async def reply(request):
        if b'example-model' in request.content:
            arrived.set()
            await complete.wait()
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50}})

    requests = mock_runtime(admin, monkeypatch, reply)
    with selected(admin, pinned), use_budget(TaskModelBudget(5, None, {'USD': Decimal('30'), 'CNY': Decimal('40')})) as ledger:
        running = asyncio.create_task(model_for('chat').ainvoke('first'))
        await asyncio.wait_for(arrived.wait(), timeout=2)
        try:
            assert ledger.costs == {'USD': Decimal('8.448')}
            await rate(admin, key='usd-new', model='example-model', value='90')
        finally:
            complete.set()
        assert (await running).content == 'ok'
        assert ledger.costs['USD'] == Decimal('0.15') and ledger.tokens == 150
        await model_for('session_title').ainvoke('other currency')
        assert ledger.costs == {'USD': Decimal('0.15'), 'CNY': Decimal('0.30')}
        with pytest.raises(ModelBudgetDenied) as denied:
            await model_for('chat').ainvoke('uses newer price')
    assert denied.value.data['reason'] == 'MODEL_COST_LIMIT_EXCEEDED'
    assert len(requests) == ledger.calls == 2


@pytest.mark.asyncio
async def test_parallel_attempts_cannot_reserve_the_same_token_room(admin, monkeypatch):
    pinned = await verified(admin)
    arrived, complete = asyncio.Event(), asyncio.Event()

    async def reply(_):
        arrived.set()
        await complete.wait()
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50}})

    requests = mock_runtime(admin, monkeypatch, reply)
    with selected(admin, pinned), use_budget(TaskModelBudget(None, 8448, {})) as ledger:
        running = asyncio.create_task(model_for('chat').ainvoke('first'))
        await asyncio.wait_for(arrived.wait(), timeout=2)
        try:
            with pytest.raises(ModelBudgetDenied) as denied:
                await model_for('chat').ainvoke('parallel')
        finally:
            complete.set()
        await running
    assert denied.value.data['reason'] == 'MODEL_TOKEN_LIMIT_EXCEEDED'
    assert len(requests) == ledger.calls == 1 and ledger.tokens == 150


@pytest.mark.asyncio
@pytest.mark.parametrize('case,reason', [
    ('unknown', 'MODEL_PRICE_UNAVAILABLE'), ('other-currency', 'MODEL_BUDGET_CURRENCY_UNAVAILABLE'),
    ('free', None),
])
async def test_money_budget_distinguishes_unknown_other_currency_and_free(admin, monkeypatch, case, reason):
    pinned = await verified(admin, local=False)
    if case != 'unknown':
        await rate(admin, key='zero', model='example-model', currency='CNY' if case == 'free' else 'USD', value='0')
    requests = mock_runtime(admin, monkeypatch)
    with selected(admin, pinned), use_budget(TaskModelBudget(None, None, {'CNY': Decimal(0)})):
        if reason is None:
            await model_for('chat').ainvoke('free')
        else:
            with pytest.raises(ModelBudgetDenied) as denied:
                await model_for('chat').ainvoke('denied')
            assert denied.value.data['reason'] == reason
    assert len(requests) == (1 if case == 'free' else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('done', [True, False])
async def test_native_stream_releases_room_only_after_terminal_usage(admin, monkeypatch, done):
    pinned = await verified(admin, local=True)
    delivered = '{"message":{"content":"ok"},"done":false}\n'
    usage = '{"done":true,"prompt_eval_count":30,"eval_count":12}\n' if done else ''
    requests = mock_runtime(admin, monkeypatch, lambda _: httpx.Response(200, text=delivered + usage))
    with selected(admin, pinned), use_budget(TaskModelBudget(None, 15000, {})) as ledger:
        if done:
            chunks = [chunk async for chunk in model_for('chat').astream('stream')]
            assert [chunk.usage_metadata['total_tokens'] for chunk in chunks if chunk.usage_metadata] == [42]
            assert ledger.tokens == 42
        else:
            with pytest.raises(ProviderFailure, match='Provider call did not complete'):
                _ = [chunk async for chunk in model_for('chat').astream('short stream')]
            assert ledger.tokens == 8448
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_cancellation_retains_in_flight_reservation(admin, monkeypatch):
    pinned = await verified(admin)
    arrived = asyncio.Event()

    async def reply(_):
        arrived.set()
        await asyncio.Future()

    mock_runtime(admin, monkeypatch, reply)
    with selected(admin, pinned), use_budget(TaskModelBudget(None, 10000, {})) as ledger:
        running = asyncio.create_task(model_for('chat').ainvoke('cancelled'))
        await asyncio.wait_for(arrived.wait(), timeout=2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert ledger.calls == 1 and ledger.tokens == 8448


@pytest.mark.asyncio
async def test_usage_above_declared_output_stops_further_calls_but_remains_billable(admin, monkeypatch):
    pinned = await verified(admin)
    events = []
    monkeypatch.setattr('app.core.model_trace._emit', events.append)
    requests = mock_runtime(admin, monkeypatch, lambda _: httpx.Response(200, json={
        'choices': [{'message': {'content': 'fixture'}}], 'usage': {'prompt_tokens': 60, 'completion_tokens': 300},
    }))
    with selected(admin, pinned), use_budget(TaskModelBudget(None, 20000, {})):
        for _ in range(2):
            with pytest.raises(ModelBudgetDenied) as denied:
                await model_for('chat').ainvoke('fixture')
            assert denied.value.data['reason'] == 'MODEL_USAGE_EXCEEDS_DECLARED_BOUND'
    assert len(requests) == len(events) == 1
    assert not events[0]['success'] and events[0]['total_tokens'] == 360 and events[0]['usage_status'] == 'complete'


@pytest.mark.asyncio
async def test_embedding_bound_per_item_and_malformed_usage_does_not_refund():
    attempted = []

    def reply(request):
        attempted.append(request.url.path)
        return httpx.Response(200, json={'embeddings': [[1]], 'prompt_eval_count': True})

    profile = {'api_protocol': 'ollama_embed', 'adapter_type': 'ollama_native', 'auth_mode': 'none',
        'base_url': 'http://localhost:11434', 'model': 'test-embedding', 'request_timeout_ms': 1000,
        'connect_timeout_ms': 100, 'embedding_dimension': 1, 'context_window': 20}
    with use_budget(TaskModelBudget(None, 59, {})) as ledger:
        async with ProviderGateway(profile, {}, None, permit=None, transport=httpx.MockTransport(reply), probe_mode=False) as gateway:
            await gateway.embed(['a', 'b'])
            with pytest.raises(ModelBudgetDenied) as denied:
                await gateway.embed(['c'])
    assert denied.value.data['reason'] == 'MODEL_TOKEN_LIMIT_EXCEEDED'
    assert attempted == ['/api/embed'] and ledger.tokens == 40


@pytest.mark.asyncio
async def test_snapshot_middleware_budget_lives_through_sse_and_is_isolated_per_request(admin, monkeypatch):
    pinned = await verified(admin)
    requests = mock_runtime(admin, monkeypatch)
    monkeypatch.setattr(TaskModelBudget, 'configured', classmethod(lambda cls: cls(1, None, {})))
    app = FastAPI()
    app.add_middleware(SnapshotMiddleware, session_factory=database_factory(admin))

    @app.get('/api/v1/budget-fixture')
    async def fixture():
        inference_actor.set({'user_id': admin.actor.user_id})

        async def stream():
            assert model_for('chat').snapshot.release_id == pinned.release_id
            await model_for('chat').ainvoke('first')
            yield 'first\n'
            with pytest.raises(ModelBudgetDenied) as denied:
                await model_for('chat').ainvoke('second')
            yield str(denied.value.stream_event()['code'])

        return StreamingResponse(stream())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        responses = await asyncio.gather(*[client.get('/api/v1/budget-fixture') for _ in range(2)])
    assert [response.text for response in responses] == ['first\n42903', 'first\n42903']
    assert len(requests) == 2 and current_model_budget.get() is None
