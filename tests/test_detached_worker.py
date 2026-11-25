"""Durable chat admission, ownership transfer and authorization checks."""

import asyncio
from datetime import datetime
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from app.ai_service import detached_worker
from app.ai_service.detached_chat import cancel_detached_chat, find_detached_chat, read_only_chat
from app.ai_service.tool_operations import ToolTurn
from app.ai_service.workflow_events import WorkflowJournal, read_workflow_events
from app.core.failed_response import BusinessError
from app.model_config import tables as config_tables
from app.model_config.snapshots import current_snapshot, inference_actor, use_snapshot
from app.models.chat import ChatAttachment, ChatMessage, ChatSession, ChatSummary
from app.models.user import User
from app.models.workflow_run import workflow_runs
from app.routers import chat
from app.schemas.chat import QueryRequest
from test_chat_workflows import fixture as checkpoint_fixture, read
from test_model_config_control_plane import admin, store  # noqa: F401
from test_model_config_publication import publish, ready
from test_transactional_tool_operations import ToolSession, as_turn, local_tools  # noqa: F401
from test_workflow_event_replay import journal_fixture  # noqa: F401


async def enqueue(fixture, release_id, message_id=1, *, user_id=None, session_id='session'):
    user_id = user_id or fixture.user_id
    with as_turn(user_id, message_id):
        async with fixture.factory() as db:
            async with db.begin():
                await detached_worker.lock_chat_admission(db, user_id)
                return await detached_worker.enqueue_chat(db, user_id, ToolTurn(session_id, message_id), release_id)


def expire_lease(fixture):
    fixture.store.sql(workflow_runs.update().values(lease_expires_at=datetime(2000, 1, 1)))


@pytest.mark.asyncio
@pytest.mark.parametrize('release_slot', ['finish', 'expired_lease', 'deadline'])
async def test_workers_share_claim_capacity(journal_fixture, admin, monkeypatch, release_slot):
    fixture = journal_fixture
    published, _ = await publish(admin, await ready(admin))
    release_id = published['release_id']
    second_user = fixture.store.other.user_id
    fixture.store.sql(ChatSession.__table__.insert().values(id='other-session', user_id=second_user))
    fixture.store.sql(ChatMessage.__table__.insert().values(id=4, session_id='other-session', role='user', content='other'))
    monkeypatch.setattr(detached_worker, 'MAX_ACTIVE_JOBS', 1)

    first_id = await enqueue(fixture, release_id)
    first = await detached_worker.DetachedChatWorker(fixture.factory, None).claim()
    assert first.workflow.id == first_id and first.workflow.attempt == 1
    second_id = await enqueue(fixture, release_id, 4, user_id=second_user, session_id='other-session')
    worker = detached_worker.DetachedChatWorker(fixture.factory, None)
    assert await worker.claim() is None
    rows = {row['id']: row for row in fixture.store.rows(workflow_runs)}
    assert rows[second_id]['status'] == 'running' and rows[second_id]['execution_attempt'] == 0

    if release_slot == 'expired_lease':
        fixture.store.sql(workflow_runs.update().where(workflow_runs.c.id == first_id).values(
            lease_expires_at=datetime(2000, 1, 1)))
        recovered = await worker.claim()
        assert recovered.workflow.id == first_id and recovered.workflow.attempt == 2
        with pytest.raises(BusinessError):
            await first.workflow.assert_active()
        assert await worker.claim() is None
        await recovered.workflow.finish('succeeded')
    elif release_slot == 'deadline':
        fixture.store.sql(workflow_runs.update().where(workflow_runs.c.id == first_id).values(
            job_deadline_at=datetime(2000, 1, 1)))
    else:
        await first.workflow.finish('succeeded')

    second = await worker.claim()
    assert second.workflow.id == second_id and second.workflow.attempt == 1
    assert await worker.claim() is None
    if release_slot == 'deadline':
        rows = {row['id']: row for row in fixture.store.rows(workflow_runs)}
        assert rows[first_id]['error_code'] == 'DETACHED_DEADLINE_EXCEEDED'


@pytest.mark.asyncio
async def test_route_commits_turn_and_queue_before_worker_or_sse(journal_fixture, admin, monkeypatch):
    fixture = journal_fixture
    published, _ = await publish(admin, await ready(admin))
    ChatAttachment.__table__.create(fixture.store.engine)
    monkeypatch.setattr(chat, 'async_session_factory', fixture.factory)
    monkeypatch.setattr(chat, '_acquire_sse_slot', AsyncMock(return_value=True))
    monkeypatch.setattr(chat, '_release_sse_slot', AsyncMock())
    monkeypatch.setattr(chat.chat_service.session_manager, 'cache_message', AsyncMock())

    async def relay(workflow_id, owner):
        assert owner == fixture.user_id
        state = await read(fixture, workflow_id)
        assert state['execution_attempt'] == 0 and state['status'] == 'running'
        yield ': queued\n\n'

    monkeypatch.setattr(chat, '_queued_chat_frames', relay)
    app = FastAPI()
    app.include_router(chat.router, prefix='/api/v1')
    app.dependency_overrides[chat.get_current_user_id] = lambda: fixture.user_id
    endpoint = next(route for route in chat.router.routes if route.path == '/chat/query')
    app.dependency_overrides[endpoint.dependencies[0].dependency] = lambda: None
    data = {'session_id': 'session', 'message': 'durable question', 'continue_on_disconnect': True,
            'allow_background_actions': True,
            'enable_thinking': True}
    with as_turn(fixture.user_id), use_snapshot(SimpleNamespace(release_id=published['release_id'])):
        request = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=request, base_url='http://fixture') as client:
            response = await client.post('/api/v1/chat/query', json=data)
        assert response.status_code == 200 and response.text == ': queued\n\n'
        workflow_id = response.headers['x-workflow-id']
        assert find_detached_chat(workflow_id, fixture.user_id) is None
        assert chat._release_sse_slot.await_count == 1
        row = fixture.store.rows(workflow_runs)[0]
        assert row['job_payload']['allow_actions'] is True
        assert row['job_payload']['thinking'] is True and 'durable question' not in str(row['job_payload'])
        assert fixture.store.rows(ChatMessage.__table__)[-1]['content'] == 'durable question'
        with pytest.raises(BusinessError):
            await chat._persist_chat_turn(QueryRequest(**data), fixture.user_id, published['release_id'])
        assert fixture.store.count(ChatMessage.__table__) == 4

        cancelled = await cancel_detached_chat(fixture.factory, workflow_id, fixture.user_id)
        assert cancelled['status'] == 'cancelled' and 'job_payload' not in cancelled
        assert await detached_worker.DetachedChatWorker(fixture.factory, None).claim() is None

        # An enqueue error rolls back the new message and attachment association transaction.
        monkeypatch.setattr(chat, 'enqueue_chat', AsyncMock(side_effect=RuntimeError('enqueue failed')))
        with pytest.raises(RuntimeError):
            await chat._persist_chat_turn(QueryRequest(**data), fixture.user_id, published['release_id'])
        assert fixture.store.count(ChatMessage.__table__) == 4


@pytest.mark.asyncio
async def test_recovery_pins_release_and_fences_old_worker_and_keeps_event_cursor(journal_fixture, admin):
    fixture = journal_fixture
    published, _ = await publish(admin, await ready(admin))
    release_id = published['release_id']
    workflow_id = await enqueue(fixture, release_id)
    first = await detached_worker.DetachedChatWorker(fixture.factory, None).claim()
    await first.workflow.record_route('plan_execute')
    await first.workflow.record_step_end(1)
    journal = await WorkflowJournal.resume(first.workflow)
    await journal.live_frame({'type': 'plan_step_end', 'step': 1})
    fixture.store.sql(config_tables.ai_config_heads.update().values(active_release_id=None))
    expire_lease(fixture)
    assert (await read(fixture, workflow_id))['status'] == 'running'

    async def produce(job, user_id, session_id):
        assert (user_id, session_id) == (fixture.user_id, 'session')
        assert inference_actor.get()['user_id'] == user_id
        assert read_only_chat.get() and current_snapshot().release_id == release_id
        assert job.workflow.attempt == 2
        assert await first.workflow.execution_state() == 'interrupted'
        with pytest.raises(BusinessError):
            await first.workflow.record_step_end(9)
        await first.workflow.finish('failed', 'STALE_WORKER')
        view = await read(fixture, workflow_id)
        assert view['finished_steps'] == [] and view['status'] == 'running'
        resumed = await WorkflowJournal.resume(job.workflow)
        yield await resumed.live_frame({'type': 'thinking', 'stage': 'processing'})
        async with fixture.factory() as db:
            async with db.begin():
                result = await db.execute(ChatMessage.__table__.insert().values(
                    session_id='session', role='assistant', content='recovered reply',
                ))
                await job.workflow.succeed_in(db, result.inserted_primary_key[0])

    worker = detached_worker.DetachedChatWorker(fixture.factory, produce)
    assert await worker.tick()
    run = find_detached_chat(workflow_id, fixture.user_id)
    await asyncio.wait_for(run.task, 2)
    assert fixture.store.rows(workflow_runs)[0]['job_payload'] is None
    async with fixture.factory() as db:
        page = await read_workflow_events(db, fixture.user_id, workflow_id, 1, 10)
    assert [event['sequence'] for event in page['events']] == [2, 3]
    assert page['events'][-1]['replay_content'] == 'recovered reply'
    assert await worker.claim() is None


@pytest.mark.asyncio
async def test_pending_job_rechecks_revoked_account_before_entering_producer(journal_fixture, admin):
    fixture = journal_fixture
    published, _ = await publish(admin, await ready(admin))
    workflow_id = await enqueue(fixture, published['release_id'])
    fixture.store.sql(User.__table__.update().where(User.uuid == fixture.user_id).values(status='disabled'))

    async def produce(*args):
        raise AssertionError('Revoked account must not reach the producer')
        yield

    worker = detached_worker.DetachedChatWorker(fixture.factory, produce)
    assert await worker.tick()
    await asyncio.wait_for(find_detached_chat(workflow_id, fixture.user_id).task, 2)
    assert (await read(fixture, workflow_id))['error_code'] == 'DETACHED_AUTHORIZATION_DENIED'


@pytest.mark.asyncio
@pytest.mark.parametrize(('message', 'rag_status'), [
    ('original question', 'empty'), ('你好！', None), ('你好，帮我查笔记', 'partial'), ('查知识库', 'error'),
])
async def test_worker_runs_real_chat_producer_from_original_message(journal_fixture, admin, monkeypatch, message, rag_status):
    fixture = journal_fixture
    published, _ = await publish(admin, await ready(admin))
    workflow_id = await enqueue(fixture, published['release_id'], message_id=2)
    fixture.store.sql(ChatMessage.__table__.update().where(ChatMessage.id == 2).values(content=message))
    fixture.store.sql(ChatMessage.__table__.update().where(ChatMessage.id == 3).values(content='future question'))
    ChatSummary.__table__.create(fixture.store.engine)
    from app.services import chat_service as chat_service_module
    from app.services import memory_compressor, token_budget

    first_stage = asyncio.Event()
    first_stage.set()
    docs = AsyncMock(return_value=[], side_effect=ConnectionError if rag_status in ('partial', 'error') else None)
    notes = AsyncMock(return_value=[], side_effect=ConnectionError if rag_status == 'error' else None)
    vectors = SimpleNamespace(search_documents=docs, search_notes=notes)
    rag = chat.RagService(vectors, enable_keyword=False, db_session_factory=fixture.factory)
    query = AsyncMock(wraps=rag.query)
    fake_manager = SimpleNamespace(stage1_complete=first_stage, stage2_complete=first_stage, chat_model=None,
                                   email_service=None, note_service=None,
                                   rag_service=SimpleNamespace(query=query))
    # Avoid the main module's heavyweight initialization while exercising its queued producer.
    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(init_manager=fake_manager))
    monkeypatch.setattr('app.rag.vector_store.VectorStoreService', lambda: None)
    monkeypatch.setattr(chat, 'async_session_factory', fixture.factory)
    monkeypatch.setattr(chat_service_module, 'async_session_factory', fixture.factory)

    async def commit(db):
        db.session.commit()

    monkeypatch.setattr(ToolSession, 'commit', commit, raising=False)
    monkeypatch.setattr(chat.chat_service.session_manager, 'cache_message', AsyncMock())
    monkeypatch.setattr(chat, 'model_for', lambda *args: SimpleNamespace(role_key='chat'))
    monkeypatch.setattr(chat, 'template_for', lambda *args, **kwargs: '')
    monkeypatch.setattr(token_budget, 'TokenBudget', lambda **kwargs: SimpleNamespace(allocate=lambda _: 1000))

    history = []
    def build_history(**kwargs):
        history.extend(item['content'] for item in kwargs['all_messages'])
        return [], 0

    monkeypatch.setattr(memory_compressor, 'MemoryCompressor', lambda _: SimpleNamespace(build_context=build_history))

    async def graph(ctx):
        assert ctx.tool_turn.message_id == 2 and ctx.user_message == message
        assert read_only_chat.get() and current_snapshot().release_id == published['release_id']
        yield {'type': 'response', 'content': 'original answer'}

    monkeypatch.setattr(chat, 'stream_chat_graph', graph)
    worker = detached_worker.DetachedChatWorker(fixture.factory, chat.stream_queued_chat)
    assert await worker.tick()
    await asyncio.wait_for(find_detached_chat(workflow_id, fixture.user_id).task, 2)
    state = await read(fixture, workflow_id)
    assert state['status'] == 'succeeded'
    assert history == ['fixture']
    assert fixture.store.rows(ChatMessage.__table__)[-1]['content'] == 'original answer'
    assert query.await_count == int(rag_status is not None)
    async with fixture.factory() as db:
        page = await read_workflow_events(db, fixture.user_id, workflow_id, 0, 10)
    events = [event for event in page['events'] if event.get('stage') == 'rag']
    if rag_status is None:
        assert events == []
    else:
        assert query.await_args.kwargs['query_text'] == message
        assert query.await_args.kwargs['deadline'] is not None
        [notice] = events
        assert notice['status'] == rag_status and notice['content'] == chat.RAG_NOTICES[rag_status]


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_limited', [False, True])
async def test_retry_is_bounded_and_legacy_limited_runs_cannot_reset_budget(journal_fixture, admin, legacy_limited):
    fixture = journal_fixture
    published, _ = await publish(admin, await ready(admin))
    workflow_id = await enqueue(fixture, published['release_id'])
    if legacy_limited:
        options = fixture.store.rows(workflow_runs)[0]['job_payload']
        options.pop('budget_mode')
        fixture.store.sql(workflow_runs.update().values(job_payload={**options, 'retry': False}))
    worker = detached_worker.DetachedChatWorker(fixture.factory, None)
    first = await worker.claim()
    assert first.workflow.attempt == 1
    expire_lease(fixture)
    if not legacy_limited:
        second = await worker.claim()
        assert second.workflow.attempt == 2
        expire_lease(fixture)
    assert await worker.claim() is None
    view = await read(fixture, workflow_id)
    assert view['status'] == 'failed' and view['error_code'] == 'DETACHED_RECOVERY_EXHAUSTED'
