"""Exercise independent stream and execution lifetimes against committed chat rows."""

import asyncio
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.ai_service import detached_chat
from app.ai_service.agent_tools import create_agent_tools
from app.ai_service.plan_execute_agent import _build_plan_tool_list
from app.ai_service.workflow_events import WorkflowJournal, read_workflow_events
from app.core.failed_response import BusinessError
from app.model_config.runtime import dispatch_fence
from app.model_config.snapshots import current_snapshot
from app.models.chat import ChatMessage
from app.routers import chat
from test_chat_workflows import fixture as checkpoint_fixture, read, start
from test_model_config_control_plane import admin, store  # noqa: F401
from test_model_config_publication import publish, ready, selected, snapshot
from test_transactional_tool_operations import as_turn, local_tools  # noqa: F401
from test_workflow_event_replay import journal_fixture  # noqa: F401


@pytest.mark.asyncio
async def test_transport_disconnect_does_not_cancel_execution_and_reply_is_replayable(journal_fixture, admin):
    fixture = journal_fixture
    await publish(admin, await ready(admin))
    pinned = await snapshot(admin)
    workflow = await start(fixture, release_id=pinned.release_id, detached=True)
    advance = asyncio.Event()
    release = AsyncMock()
    invocations = 0

    async def produce(workflow):
        nonlocal invocations
        invocations += 1
        journal = WorkflowJournal(workflow)
        yield await journal.live_frame({'type': 'thinking', 'stage': 'processing'})
        await advance.wait()
        assert current_snapshot().release_id == pinned.release_id
        await dispatch_fence.get()()
        async with fixture.factory() as db:
            async with db.begin():
                reply = await db.execute(ChatMessage.__table__.insert().values(
                    session_id='session', role='assistant', content='finished after disconnect',
                ))
                await workflow.succeed_in(db, reply.inserted_primary_key[0])
        yield await journal.live_frame({'type': 'done', 'session_id': 'session'})

    with selected(admin, pinned):
        run = detached_chat.start_detached_chat(workflow, fixture.user_id, produce, release)
    assert current_snapshot() is None
    stream = run.frames()
    try:
        assert (await anext(stream)).startswith('id: 1')
        await stream.aclose()
        assert not run.task.done() and detached_chat.has_detached_chat(fixture.user_id)
    finally:
        advance.set()
        await asyncio.wait_for(run.task, 2)
    async with fixture.factory() as db:
        replay = await read_workflow_events(db, fixture.user_id, workflow.id, 1, 50)
    assert replay['events'][0]['replay_content'] == 'finished after disconnect'
    assert replay['status'] == 'succeeded' and invocations == 1
    assert not detached_chat.has_detached_chat(fixture.user_id)
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_owner_cancel_commits_first_and_stops_active_worker(checkpoint_fixture, monkeypatch):
    fixture = checkpoint_fixture
    workflow = await start(fixture, detached=True)
    entered, exited = asyncio.Event(), asyncio.Event()
    release = AsyncMock()

    async def produce(_workflow):
        try:
            entered.set()
            await asyncio.Future()
            yield 'unreachable'
        finally:
            exited.set()

    run = detached_chat.start_detached_chat(workflow, fixture.user_id, produce, release)
    await entered.wait()
    try:
        with as_turn(fixture.store.other.user_id), pytest.raises(BusinessError) as denied:
            await detached_chat.cancel_detached_chat(fixture.factory, workflow.id, fixture.store.other.user_id)
        assert denied.value.http_status == 404 and not run.task.done()

        monkeypatch.setattr(chat, 'async_session_factory', fixture.factory)
        app = FastAPI()
        app.include_router(chat.router, prefix='/api/v1')
        app.dependency_overrides[chat.get_current_user_id] = lambda: fixture.user_id
        endpoint = next(route for route in chat.router.routes if route.path.endswith('/{workflow_id}/cancel'))
        limit = next(item.call for item in endpoint.dependant.dependencies if item.name == 'limited')
        app.dependency_overrides[limit] = lambda: None
        with as_turn(fixture.user_id):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                for _ in range(2):
                    response = await client.post(f'/api/v1/chat/workflows/{workflow.id}/cancel')
                    assert response.status_code == 200
                    view = response.json()['data']
                    assert view['status'] == 'cancelled' and view['error_code'] == 'USER_CANCELLED'
        await asyncio.wait_for(exited.wait(), 2)
        with pytest.raises(BusinessError):
            await workflow.assert_active()
    finally:
        run.stop('TEST_CLEANUP')
        await asyncio.gather(run.task, return_exceptions=True)
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_before_first_step_releases_capacity_without_starting(checkpoint_fixture):
    fixture = checkpoint_fixture
    workflow = await start(fixture, detached=True)
    other = await start(fixture, message_id=2, detached=True)
    release = AsyncMock()

    async def produce(_workflow):
        pytest.fail('The worker was stopped before its first step')
        yield ''

    run = detached_chat.start_detached_chat(workflow, fixture.user_id, produce, release)
    assert detached_chat.start_detached_chat(other, fixture.user_id, produce, release) is None
    await detached_chat.stop_detached_chats()
    assert run.task.done() and not detached_chat.has_detached_chat(fixture.user_id)
    assert (await read(fixture, workflow.id))['error_code'] == 'SERVICE_SHUTDOWN'
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_reader_does_not_block_deadline_or_expand_buffer(checkpoint_fixture, monkeypatch):
    fixture = checkpoint_fixture
    workflow = await start(fixture, detached=True)
    release = AsyncMock()
    monkeypatch.setattr(detached_chat, 'MAX_RELAY_FRAMES', 2)
    monkeypatch.setattr(detached_chat, 'RUN_TIMEOUT_SECONDS', 0.03)

    async def produce(_workflow):
        for chunk in range(8):
            yield str(chunk)
        await asyncio.Future()

    run = detached_chat.start_detached_chat(workflow, fixture.user_id, produce, release)
    await asyncio.wait_for(run.task, 2)
    assert [frame async for frame in run.frames()] == ['0', '1']
    view = await read(fixture, workflow.id)
    assert view['status'] == 'failed' and view['error_code'] == 'DETACHED_DEADLINE_EXCEEDED'
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_policy_reaches_planner_and_executor_without_changing_normal_chat(checkpoint_fixture):
    fixture = checkpoint_fixture
    workflow = await start(fixture, detached=True)
    release = AsyncMock()
    assert 'create_note_tool' in {tool.name for tool in create_agent_tools(fixture.user_id)}

    async def produce(workflow):
        tools = {tool.name for tool in create_agent_tools(fixture.user_id)}
        assert 'get_note_content_tool' in tools and tools <= detached_chat.READ_ONLY_TOOLS
        menu = _build_plan_tool_list()
        assert 'create_note_tool' not in menu and 'get_note_content_tool' in menu
        async with fixture.factory() as db:
            async with db.begin():
                await workflow.succeed_in(db)
        yield 'done'

    run = detached_chat.start_detached_chat(workflow, fixture.user_id, produce, release)
    await asyncio.wait_for(run.task, 2)
    assert (await read(fixture, workflow.id))['status'] == 'succeeded'
    assert 'create_note_tool' in {tool.name for tool in create_agent_tools(fixture.user_id)}


@pytest.mark.asyncio
async def test_process_without_local_registry_cancels_and_worker_observes_after_transient_failure(checkpoint_fixture, monkeypatch):
    fixture = checkpoint_fixture
    workflow = await start(fixture, detached=True)
    entered, exited = asyncio.Event(), asyncio.Event()
    release = AsyncMock()
    monkeypatch.setattr(detached_chat, 'find_detached_chat', lambda *_: None)
    monkeypatch.setattr(detached_chat, 'CANCEL_POLL_SECONDS', 0.005)
    original_state = type(workflow).execution_state
    attempts = 0

    async def state(self):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise SQLAlchemyError('fixture temporary read error')
        return await original_state(self)

    monkeypatch.setattr(type(workflow), 'execution_state', state)

    async def produce(_workflow):
        entered.set()
        try:
            await asyncio.Future()
            yield 'unexpected'
        finally:
            exited.set()

    run = detached_chat.start_detached_chat(workflow, fixture.user_id, produce, release)
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            with as_turn(fixture.user_id):
                first = await detached_chat.cancel_detached_chat(fixture.factory, workflow.id, fixture.user_id)
                second = await detached_chat.cancel_detached_chat(fixture.factory, workflow.id, fixture.user_id)
            assert first['status'] == 'cancelled' and first['state_version'] == second['state_version']
            with pytest.raises(BusinessError):
                await workflow.assert_active()
            await exited.wait()
    finally:
        run.stop('TEST_CLEANUP')
        await asyncio.gather(run.task, return_exceptions=True)
    assert attempts >= 3 and not detached_chat.has_detached_chat(fixture.user_id)
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_stopped_before_worker_handoff_never_enters_the_producer(checkpoint_fixture):
    fixture = checkpoint_fixture
    workflow = await start(fixture, detached=True)
    with as_turn(fixture.user_id):
        stopped = await detached_chat.cancel_detached_chat(fixture.factory, workflow.id, fixture.user_id)
    assert stopped['status'] == 'cancelled'
    release = AsyncMock()

    async def produce(_workflow):
        pytest.fail('A cancelled workflow entered the producer')
        yield ''

    run = detached_chat.start_detached_chat(workflow, fixture.user_id, produce, release)
    await asyncio.wait_for(run.task, 2)
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_inline_chat_cannot_be_cancelled_by_the_background_endpoint(checkpoint_fixture):
    fixture = checkpoint_fixture
    workflow = await start(fixture)
    with as_turn(fixture.user_id), pytest.raises(BusinessError) as error:
        await detached_chat.cancel_detached_chat(fixture.factory, workflow.id, fixture.user_id)
    assert error.value.http_status == 409 and error.value.data['reason'] == 'WORKFLOW_NOT_DETACHED'
    assert (await read(fixture, workflow.id))['status'] == 'running'


@pytest.mark.asyncio
async def test_late_cancellation_preserves_completed_work_and_final_frame(checkpoint_fixture, monkeypatch):
    fixture = checkpoint_fixture
    workflow = await start(fixture, detached=True)
    observed_success, drain = asyncio.Event(), asyncio.Event()
    release = AsyncMock()
    original_state = type(workflow).execution_state
    monkeypatch.setattr(detached_chat, 'CANCEL_POLL_SECONDS', 0.005)

    async def state(self):
        value = await original_state(self)
        if value == 'succeeded':
            observed_success.set()
        return value

    monkeypatch.setattr(type(workflow), 'execution_state', state)

    async def produce(workflow):
        async with fixture.factory() as db:
            async with db.begin():
                await workflow.succeed_in(db)
        await drain.wait()
        yield 'done'

    run = detached_chat.start_detached_chat(workflow, fixture.user_id, produce, release)
    try:
        await asyncio.wait_for(observed_success.wait(), 2)
        with as_turn(fixture.user_id):
            view = await detached_chat.cancel_detached_chat(fixture.factory, workflow.id, fixture.user_id)
        assert view['status'] == 'succeeded' and not run.task.done()
    finally:
        drain.set()
        await asyncio.wait_for(run.task, 2)
    assert [frame async for frame in run.frames()] == ['done']
    release.assert_awaited_once()
