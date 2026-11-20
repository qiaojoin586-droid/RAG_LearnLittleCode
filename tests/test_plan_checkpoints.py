"""A recovered Plan resumes only safe, completed, input-matched work."""

import json
from contextlib import aclosing
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import HumanMessage
import pytest

from app.ai_service import chat_graph, plan_execute_agent as plans
from app.ai_service.chat_route import ChatRouteContext, collect_events, plan_events
from app.ai_service.context_composer import assistant_data, set_assistant_data
from app.ai_service.detached_chat import cancel_detached_chat, read_only_chat
from app.ai_service.detached_worker import DetachedChatWorker
from app.ai_service.plan_checkpoints import PlanCheckpoint
from app.core.failed_response import BusinessError
from app.models.workflow_run import workflow_runs
from test_chat_workflows import fixture as checkpoint_fixture, read  # noqa: F401
from test_detached_worker import enqueue, expire_lease
from test_model_config_control_plane import store  # noqa: F401
from test_transactional_tool_operations import as_turn, local_tools  # noqa: F401
from test_workflow_event_replay import journal_fixture  # noqa: F401


class StepModel:
    def __init__(self):
        self.calls = []

    async def astream(self, messages):
        content = messages[-1].content
        if '当前任务步骤：prepare' in content:
            self.calls.append('prepare')
            yield SimpleNamespace(content='the prepared value')
        elif '当前任务步骤：finish' in content:
            self.calls.append('finish')
            assert 'the prepared value' in content
            yield SimpleNamespace(content='the finished value')


def plan_context(workflow, model, planner, history=()):
    return ChatRouteContext(agent_model=model, plan_model=planner, user_id='owner', user_message='complex question',
                            system_prompt='', compressed_messages=list(history),
                            db_session_factory=workflow.session_factory, workflow=workflow)


@pytest.mark.asyncio
async def test_reclaimed_plan_skips_completed_step_on_real_plan_route(journal_fixture, monkeypatch):
    fixture = journal_fixture
    workflow_id = await enqueue(fixture, None)
    worker = DetachedChatWorker(fixture.factory, None)
    first = await worker.claim()
    model = StepModel()
    planner = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps({
        'goal': 'goal', 'steps': [{'step': 1, 'action': 'prepare', 'tool': 'none'},
                                  {'step': 2, 'action': 'finish', 'tool': 'none', 'depends_on': [1]}],
    }))))

    async def synthesize(**kwargs):
        assert kwargs['results'] == {1: 'the prepared value', 2: 'the finished value'}
        yield {'type': 'response', 'content': 'complete answer'}

    monkeypatch.setattr(plans, 'get_reflection_config', lambda: {})
    monkeypatch.setattr(plans, 'get_plan_execute_config', lambda: {'enable_parallel': False})
    monkeypatch.setattr(plans, '_synthesize', synthesize)
    readonly = read_only_chat.set(True)
    sources = assistant_data.set(None)
    try:
        ctx = plan_context(first.workflow, model, planner)
        ctx.plan_checkpoint = await PlanCheckpoint.for_context(ctx)
        async with aclosing(plan_events(ctx, timeout=30)) as events:
            async for event in events:
                if event.get('type') == 'plan_step_end':
                    assert event['step'] == 1
                    break  # same durable boundary as losing the process after its first step
        assert model.calls == ['prepare']
        raw = fixture.store.rows(workflow_runs)[0]
        assert raw['plan_checkpoint']['results']['1']['text'] == 'the prepared value'
        assert 'plan_checkpoint' not in await read(fixture, workflow_id)

        expire_lease(fixture)
        second = await worker.claim()
        with pytest.raises(BusinessError):
            await ctx.plan_checkpoint.store_result(1, 'stale write', {})
        ctx = plan_context(second.workflow, model, planner)
        state = await chat_graph.classify_node({'ctx': ctx})
        assert state['classification']['source'] == 'checkpoint'
        events, answer = await collect_events(plan_events(ctx, timeout=30))
        assert model.calls == ['prepare', 'finish'] and planner.ainvoke.await_count == 1
        assert answer == 'complete answer'
        assert [event['step'] for event in events if event['type'] == 'plan_step_end'] == [1, 2]
        assert events[-1]['completed_steps'] == 2
        async with fixture.factory() as db:
            async with db.begin():
                await second.workflow.succeed_in(db)
        assert fixture.store.rows(workflow_runs)[0]['plan_checkpoint'] is None
    finally:
        assistant_data.reset(sources)
        read_only_chat.reset(readonly)


@pytest.mark.asyncio
async def test_context_change_or_retrieval_material_prevents_reuse(journal_fixture):
    fixture = journal_fixture
    workflow_id = await enqueue(fixture, None)
    worker = DetachedChatWorker(fixture.factory, None)
    first = await worker.claim()
    readonly = read_only_chat.set(True)
    sources = assistant_data.set(None)
    try:
        ctx = plan_context(first.workflow, None, None, [HumanMessage(content='original history')])
        cp = await PlanCheckpoint.for_context(ctx)
        await cp.store_plan({'goal': 'goal', 'steps': []})
        await cp.store_result(2, 'old dependent output', {1: 'initial input'})
        assert cp.read_result(2, {1: 'initial input'}) == 'old dependent output'
        assert cp.read_result(2, {1: 'different input'}) is None
        changed = plan_context(first.workflow, None, None, [HumanMessage(content='updated history')])
        assert not (await PlanCheckpoint.for_context(changed)).data
        set_assistant_data(rag='note content that requires current access')
        assert await PlanCheckpoint.for_context(ctx) is None
        with as_turn(fixture.user_id):
            assert (await cancel_detached_chat(fixture.factory, workflow_id, fixture.user_id))['status'] == 'cancelled'
        assert fixture.store.rows(workflow_runs)[0]['plan_checkpoint'] is None
    finally:
        assistant_data.reset(sources)
        read_only_chat.reset(readonly)


@pytest.mark.asyncio
async def test_tool_steps_and_their_dependents_are_never_reused(journal_fixture, monkeypatch):
    fixture = journal_fixture
    await enqueue(fixture, None)
    worker = DetachedChatWorker(fixture.factory, None)
    first = await worker.claim()
    readonly = read_only_chat.set(True)
    sources = assistant_data.set(None)
    generated = plans.ExecutionPlan('goal', [plans.PlanStep(1, 'fetch', 'get_note_content_tool'),
                                            plans.PlanStep(2, 'reason', 'none', [1]),
                                            plans.PlanStep(3, 'prepare', 'none')])
    calls = []

    async def step_stream(step, *args, **kwargs):
        calls.append(step.step)
        step.result = f'output {step.step}'
        step.result_ready = True
        yield {'type': 'plan_step_end', 'step': step.step}

    async def synthesize(**kwargs):
        yield {'type': 'response', 'content': 'answer'}

    monkeypatch.setattr(plans, '_generate_plan', AsyncMock(return_value=generated))
    monkeypatch.setattr(plans, 'get_plan_execute_config', lambda: {'enable_parallel': False})
    monkeypatch.setattr(plans, 'get_reflection_config', lambda: {})
    monkeypatch.setattr(plans, '_execute_step', step_stream)
    monkeypatch.setattr(plans, '_synthesize', synthesize)
    try:
        ctx = plan_context(first.workflow, None, None)
        ctx.plan_checkpoint = await PlanCheckpoint.for_context(ctx)
        await collect_events(plan_events(ctx, timeout=30))
        assert set(ctx.plan_checkpoint.data['results']) == {'3'}
        expire_lease(fixture)
        second = await worker.claim()
        ctx = plan_context(second.workflow, None, None)
        ctx.plan_checkpoint = await PlanCheckpoint.for_context(ctx)
        await collect_events(plan_events(ctx, timeout=30))
        assert calls == [1, 3, 2, 1, 2]
        fixture.store.sql(workflow_runs.update().values(job_deadline_at=datetime(2000, 1, 1)))
        assert await worker.claim() is None
        assert fixture.store.rows(workflow_runs)[0]['plan_checkpoint'] is None
    finally:
        assistant_data.reset(sources)
        read_only_chat.reset(readonly)
