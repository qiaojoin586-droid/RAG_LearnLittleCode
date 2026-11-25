"""Recover persisted effects without starting a second operation."""

import asyncio
from contextlib import aclosing, contextmanager
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.tools import tool
import pytest

from app.ai_service import chat_graph, plan_execute_agent as plans
from app.ai_service.chat_route import ChatRouteContext, collect_events
from app.ai_service.detached_chat import background_actions, read_only_chat
from app.ai_service.detached_worker import DetachedChatWorker, enqueue_chat, lock_chat_admission
from app.ai_service.tool_approvals import approval_workflow
from app.ai_service.tool_operations import ToolTurn, request_email_approval, run_artifact_tool
from app.ai_service.tool_results import artifact_output_id
from app.ai_service.workflow_events import WorkflowJournal, read_workflow_events
from app.models.email_dispatch import email_dispatches
from app.models.note import Note
from app.models.operation_approval import operation_approvals
from app.models.tool_operation import tool_operation_steps, tool_operations
from test_chat_workflows import fixture as checkpoint_fixture  # noqa: F401
from test_detached_worker import expire_lease
from test_model_config_control_plane import store  # noqa: F401
from test_task_model_budget import ToolCallingModel
from test_tool_approvals import decide
from test_transactional_tool_operations import as_turn, call, local_tools  # noqa: F401
from test_workflow_event_replay import journal_fixture  # noqa: F401


TURN = ToolTurn('session', 1)


@pytest.fixture
def recovery(journal_fixture, monkeypatch):
    fixture = journal_fixture
    for table in (operation_approvals, tool_operation_steps, email_dispatches):
        table.create(fixture.store.engine)
    monkeypatch.setattr('app.services.note_service.NoteService._schedule_created_note', AsyncMock())
    monkeypatch.setattr(chat_graph, '_compiled_graph', None)
    return fixture


@contextmanager
def allow_background_actions():
    flags = (read_only_chat.set(True), background_actions.set(True))
    try:
        yield
    finally:
        background_actions.reset(flags[1])
        read_only_chat.reset(flags[0])


async def start_job(fixture):
    async with fixture.factory() as db:
        async with db.begin():
            await lock_chat_admission(db, fixture.user_id)
            await enqueue_chat(db, fixture.user_id, TURN, None, allow_actions=True)
    return (await DetachedChatWorker(fixture.factory, None).claim()).workflow


def context(fixture, workflow, *, model=None, planner=None):
    return ChatRouteContext(model, fixture.user_id, '请完成这次任务', '', [], fixture.factory,
                            plan_model=planner, workflow=workflow, tool_turn=TURN)


@pytest.mark.asyncio
async def test_plan_recovers_confirmed_write_before_step_end(recovery, monkeypatch):
    fx = recovery
    calls = []

    async def classify(_):
        return SimpleNamespace(complexity='complex', source='fixture', reason='fixture')

    async def generate(*args, **kwargs):
        return plans.ExecutionPlan('complete the task', [
            plans.PlanStep(1, '申请创建笔记', 'create_note_tool'),
            plans.PlanStep(2, '总结回执', 'none', [1]),
        ])

    async def agent(**_):
        calls.append('propose')
        response = await call(fx, 'create_note_tool', 'first-call', title='原始标题', content='原始内容')
        assert response.artifact['approval_status'] == 'pending'
        yield {'type': 'tool_end', 'name': 'create_note_tool'}
        await asyncio.Future()

    class Model:
        async def astream(self, messages):
            assert '操作已确认并完成' in messages[-1].content
            yield SimpleNamespace(content='根据回执完成总结')

    async def synthesize(**kwargs):
        assert '操作已确认并完成' in kwargs['results'][1]
        yield {'type': 'response', 'content': kwargs['results'][2]}

    monkeypatch.setattr('app.ai_service.query_classifier.QueryClassifier.classify', lambda self, question: classify(question))
    monkeypatch.setattr(plans, '_generate_plan', generate)
    monkeypatch.setattr(plans, '_synthesize', synthesize)
    monkeypatch.setattr(plans, 'get_reflection_config', lambda: {})
    monkeypatch.setattr('app.ai_service.agent_runner.execute_agent', agent)

    with as_turn(fx.user_id), allow_background_actions():
        first = await start_job(fx)
        ctx = context(fx, first, model=Model(), planner=object())
        async with aclosing(chat_graph.stream_chat_graph(ctx)) as events:
            async for event in events:
                if event['type'] == 'tool_end':
                    break
        proposal, = fx.store.rows(operation_approvals)
        assert fx.store.count(Note.__table__) == 0
        assert (await decide(fx, proposal))['status'] == 'executed'
        expire_lease(fx)
        second = (await DetachedChatWorker(fx.factory, None).claim()).workflow
        events, reply = await collect_events(chat_graph.stream_chat_graph(context(fx, second, model=Model(), planner=object())))
        assert reply == '根据回执完成总结' and calls == ['propose']
        assert [event['step'] for event in events if event['type'] == 'plan_step_end'] == [1, 2]
        assert fx.store.count(Note.__table__) == fx.store.count(tool_operations) == fx.store.count(tool_operation_steps) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('completed_file', [False, True])
async def test_recovery_checks_artifact_without_generating_again(recovery, monkeypatch, tmp_path, completed_file):
    fx = recovery
    monkeypatch.setattr('app.routers.tts_router.TTS_FILE_ROOT', str(tmp_path))

    async def interrupted_call():
        if completed_file:
            directory = tmp_path / fx.user_id
            directory.mkdir()
            (directory / f'{artifact_output_id.get()}.mp3').write_bytes(b'completed fixture')
        raise asyncio.CancelledError

    with as_turn(fx.user_id), allow_background_actions():
        first = await start_job(fx)
        call_spec = {'name': 'text_to_speech', 'id': 'original', 'args': {'text': 'fixture'}}
        external = AsyncMock(side_effect=interrupted_call)
        with pytest.raises(asyncio.CancelledError):
            await run_artifact_tool(first, fx.user_id, TURN, call_spec, external)
        expire_lease(fx)
        second = (await DetachedChatWorker(fx.factory, None).claim()).workflow
        retry = await run_artifact_tool(second, fx.user_id, TURN, {**call_spec, 'args': {'text': 'changed'}}, external)
        events, text = await collect_events(chat_graph.stream_chat_graph(context(fx, second)))
    expected = '已生成' if completed_file else '待核对'
    assert expected in retry.content and expected in text
    assert any(event['type'] == 'tool_file' for event in events) == completed_file
    assert fx.store.count(tool_operations) == 1 and external.await_count == 1


@pytest.mark.asyncio
async def test_recovery_reads_existing_unknown_smtp_without_new_approval(recovery):
    fx = recovery
    arguments = {'to': 'fixture@example.invalid', 'subject': 'fixture', 'body': 'fixture', 'format': 'text'}
    with as_turn(fx.user_id), allow_background_actions():
        first = await start_job(fx)
        owner = approval_workflow.set(first)
        try:
            _, receipt = await request_email_approval(fx.factory, fx.user_id, arguments)
        finally:
            approval_workflow.reset(owner)
        operation_id = receipt['operation_id']
        fx.store.sql(operation_approvals.update().values(status='executed'))
        fx.store.sql(email_dispatches.insert().values(
            operation_id=operation_id, user_id=fx.user_id, status='dispatching',
            started_at=datetime(2000, 1, 1), deadline_at=datetime(2000, 1, 2), updated_at=datetime(2000, 1, 1),
        ))
        expire_lease(fx)
        second = (await DetachedChatWorker(fx.factory, None).claim()).workflow
        _, text = await collect_events(chat_graph.stream_chat_graph(context(fx, second)))
        owner = approval_workflow.set(second)
        try:
            _, retried = await request_email_approval(fx.factory, fx.user_id, {**arguments, 'subject': 'changed'})
        finally:
            approval_workflow.reset(owner)
    assert '发送结果待核对' in text and retried['operation_id'] == operation_id
    assert retried['approval_status'] == 'unknown' and fx.store.count(tool_operations) == 1


@pytest.mark.asyncio
async def test_real_agent_only_streams_committed_artifact_reference(recovery, monkeypatch):
    fx = recovery
    @tool('generate_ppt_tool')
    async def generated_file() -> str:
        """Generate a fixture file."""
        return json.dumps({'file_id': artifact_output_id.get(), 'download_url': 'https://example.invalid/private', 'slide_count': 3})

    async def classify(self, question):
        return SimpleNamespace(complexity='simple', source='fixture', reason='fixture')

    monkeypatch.setattr('app.ai_service.query_classifier.QueryClassifier.classify', classify)
    monkeypatch.setattr('app.ai_service.agent_runner.create_agent_tools', lambda **_: [generated_file])
    monkeypatch.setattr('app.ai_service.stream.default_middleware.trigger', AsyncMock())
    with as_turn(fx.user_id), allow_background_actions():
        workflow = await start_job(fx)
        events, _ = await collect_events(chat_graph.stream_chat_graph(context(
            fx, workflow, model=ToolCallingModel(names=['generate_ppt_tool']))))
        file_event, = [event for event in events if event['type'] == 'tool_file']
        file_id = file_event['operation_id'].replace('-', '')
        assert file_event['download_url'] == f'/api/v1/ppt/{file_id}'
        assert fx.store.rows(tool_operations)[0]['result_ref'] == {'file_id': file_id, 'slide_count': 3}
        journal = await WorkflowJournal.resume(workflow)
        await journal.live_frame(file_event)
        async with fx.factory() as db:
            replay = await read_workflow_events(db, fx.user_id, workflow.id, 0, 10)
        assert replay['events'][0]['download_url'] == file_event['download_url']
