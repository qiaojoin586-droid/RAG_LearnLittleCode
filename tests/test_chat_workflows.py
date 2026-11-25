"""Check durable workflow state at the graph, reply and read boundaries."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy.orm import Session

from app.ai_service import chat_graph, workflows
from app.ai_service.tool_operations import ToolTurn
from app.core.failed_response import BusinessError
from app.model_config.snapshots import SnapshotMiddleware
from app.models.chat import ChatMessage
from app.models.workflow_run import workflow_runs
from app.routers import chat
from app.services.chat_service import ChatService
from test_model_config_control_plane import admin, store  # noqa: F401
from test_model_config_publication import publish, ready
from test_transactional_tool_operations import ToolSession, as_turn, local_tools  # noqa: F401


@pytest.fixture
def fixture(local_tools):
    workflow_runs.create(local_tools.store.engine)
    return local_tools


async def start(fixture, message_id=1, release_id=None, *, detached=False):
    with as_turn(fixture.user_id, message_id):
        return await workflows.ChatWorkflow.start(
            fixture.factory, fixture.user_id, ToolTurn('session', message_id), release_id, detached=detached,
        )


async def read(fixture, run_id):
    async with fixture.factory() as db:
        return await workflows.read_workflow(db, fixture.user_id, run_id)


@pytest.mark.asyncio
async def test_plan_boundaries_persist_only_route_and_step_numbers(fixture, monkeypatch):
    run = await start(fixture)

    async def plan_events(ctx, timeout):
        assert (await read(fixture, run.id))['route'] == 'plan_execute'
        for step in (2, 1, 2):
            yield {'type': 'plan_step_end', 'step': step, 'result': 'private step content'}
        yield {'type': 'response', 'content': 'private answer'}

    monkeypatch.setattr(chat_graph, 'plan_events', plan_events)
    await chat_graph.plan_node({'ctx': SimpleNamespace(workflow=run, plan_timeout=5), 'event_queue': asyncio.Queue()})
    view = await read(fixture, run.id)
    assert view['state_version'] == 4 and view['finished_steps'] == [1, 2]
    assert view['status'] == 'running' and 'private' not in str(view)


@pytest.mark.asyncio
async def test_owned_status_uses_pinned_release_after_head_changes(fixture, admin):
    published, _ = await publish(admin, await ready(admin))
    run = await start(fixture, release_id=published['release_id'])
    from app.model_config import tables as t
    admin.sql(t.ai_config_heads.update().values(active_release_id=None))
    assert (await read(fixture, run.id))['release_id'] == published['release_id']
    async with fixture.factory() as db:
        with pytest.raises(BusinessError) as denied:
            await workflows.read_workflow(db, fixture.store.other.user_id, run.id)
    assert denied.value.http_status == 404


@pytest.mark.asyncio
async def test_expired_lease_is_interrupted_and_cannot_be_revived(fixture):
    run = await start(fixture)
    fixture.store.sql(workflow_runs.update().values(lease_expires_at=datetime(2000, 1, 1)))
    await run.finish('cancelled')
    with pytest.raises(BusinessError):
        await run.record_route('react')
    view = await read(fixture, run.id)
    assert view['status'] == 'interrupted' and view['error_code'] == 'WORKFLOW_LEASE_EXPIRED'
    assert fixture.store.rows(workflow_runs)[0]['status'] == 'running'


@pytest.mark.asyncio
async def test_heartbeat_extends_lease_without_creating_a_checkpoint(fixture, monkeypatch):
    run = await start(fixture)
    initial = fixture.store.rows(workflow_runs)[0]['lease_expires_at'] - timedelta(seconds=15)
    fixture.store.sql(workflow_runs.update().values(lease_expires_at=initial))
    monkeypatch.setattr(workflows, 'HEARTBEAT_SECONDS', 0.001)
    task = asyncio.create_task(run.heartbeat())
    try:
        async with asyncio.timeout(1):
            while fixture.store.rows(workflow_runs)[0]['lease_expires_at'] <= initial:
                await asyncio.sleep(0.005)
        assert (await read(fixture, run.id))['state_version'] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reply_and_success_checkpoint_commit_and_roll_back_together(fixture, monkeypatch):
    run = await start(fixture)

    class ChatDb(ToolSession):
        async def commit(self):
            self.session.commit()

    @asynccontextmanager
    async def factory():
        with Session(fixture.store.engine, expire_on_commit=False) as session:
            yield ChatDb(session)

    async def add_message(db, session_id, user_id, role, content, *_args, **_kwargs):
        assert _kwargs['cache_immediately'] is False
        message = ChatMessage(session_id=session_id, role=role, content=content)
        db.add(message)
        await db.flush()
        return message

    monkeypatch.setattr('app.services.chat_service.async_session_factory', factory)
    async def check_committed_cache(session_id, message):
        view = await read(fixture, run.id)
        assert view['status'] == 'succeeded' and view['reply_message_id'] == message.id

    cache = AsyncMock(side_effect=check_committed_cache)
    service = ChatService(SimpleNamespace(add_message=add_message, cache_message=cache))

    async def fail_after_checkpoint(db, message):
        await run.succeed_in(db, message.id)
        raise RuntimeError('fixture interrupted before commit')

    with pytest.raises(BusinessError):
        await service.save_message_with_commit('session', fixture.user_id, 'assistant', 'fixture reply',
            require_commit=True, before_message_commit=fail_after_checkpoint)
    assert (await read(fixture, run.id))['status'] == 'running'
    assert fixture.store.count(ChatMessage.__table__) == 3
    cache.assert_not_awaited()

    async def finalize_ok(db, message):
        await run.succeed_in(db, message.id)

    message_id = await service.save_message_with_commit('session', fixture.user_id, 'assistant', 'fixture reply',
        require_commit=True, before_message_commit=finalize_ok)
    await run.finish('cancelled')
    view = await read(fixture, run.id)
    assert view['status'] == 'succeeded' and view['reply_message_id'] == message_id
    cache.assert_awaited_once()


@pytest.mark.asyncio
async def test_checkpoint_status_http_does_not_require_current_model_config(fixture, monkeypatch):
    run = await start(fixture)
    await run.finish('cancelled', 'STREAM_CLOSED')
    monkeypatch.setattr(chat, 'async_session_factory', fixture.factory)

    def no_snapshot_reads():
        raise AssertionError('Status route should not load active model configuration')

    app = FastAPI()
    app.include_router(chat.router, prefix='/api/v1')
    app.add_middleware(SnapshotMiddleware, session_factory=no_snapshot_reads)
    app.dependency_overrides[chat.get_current_user_id] = lambda: fixture.user_id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        response = await client.get(f'/api/v1/chat/workflows/{run.id}')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    status = response.json()['data']
    assert status['id'] == run.id and status['status'] == 'cancelled' and status['reply_message_id'] is None
