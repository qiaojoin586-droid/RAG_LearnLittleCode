"""Authenticated replay uses committed content and safe, ordered metadata."""

import json

from fastapi import FastAPI
import httpx
import pytest
import sqlalchemy as sa

from app.ai_service import workflow_events as replay
from app.core.failed_response import BusinessError
from app.model_config.security import SCOPE
from app.model_config.tables import ai_config_heads
from app.models.chat import ChatMessage
from app.models.workflow_event import workflow_events
from app.routers import chat
from test_chat_workflows import fixture as checkpoint_fixture, start
from test_model_config_control_plane import store  # noqa: F401
from test_transactional_tool_operations import local_tools  # noqa: F401


@pytest.fixture
def journal_fixture(checkpoint_fixture):
    workflow_events.create(checkpoint_fixture.store.engine)
    # The real detached entry point already has a configuration scope before enqueue.
    checkpoint_fixture.store.sql(ai_config_heads.insert().values(
        **SCOPE, head_version=0, last_release_number=0, manager_generation=0, updated_at=sa.func.current_timestamp(),
    ))
    return checkpoint_fixture


def parse(frame):
    return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith('data: ')))


async def read(fixture, run, after=0, limit=50):
    async with fixture.factory() as db:
        return await replay.read_workflow_events(db, fixture.user_id, run.id, after, limit)


@pytest.mark.asyncio
async def test_progress_is_committed_in_order_and_strips_model_generated_text(journal_fixture):
    fixture = journal_fixture
    run = await start(fixture)
    journal = replay.WorkflowJournal(run)
    info = {'type': 'plan_step_end', 'step': 3, 'result': 'private tool result', 'payload': {'api_key': 'secret'}}
    frame = await journal.live_frame(info)
    assert frame.startswith('id: 1\n') and parse(frame)['result'] == info['result']
    for event in ({'type': 'thinking', 'stage': 'thinking', 'content': 'hidden reasoning'},
                  {'type': 'response', 'content': 'unconfirmed reply'},
                  {'type': 'tool_file', 'download_url': 'temporary private URL'}):
        frame = await journal.live_frame(event)
        assert not frame.startswith('id:') and parse(frame)['type'] == event['type']
    await journal.live_frame({'type': 'tool_end', 'name': 'create_note_tool', 'duration_ms': 20, 'error': False})
    rows = fixture.store.rows(workflow_events)
    assert [row['sequence'] for row in rows] == [1, 2]
    assert rows[0]['payload'] == {'type': 'plan_step_end', 'step': 3}
    assert rows[1]['payload'] == {'type': 'tool_end', 'name': 'create_note_tool', 'duration_ms': 20, 'error': False}
    assert (await read(fixture, run, after=1))['events'] == [
        {**rows[1]['payload'], 'sequence': 2, 'workflow_id': run.id}
    ]


@pytest.mark.asyncio
async def test_rag_degradation_replays_only_the_safe_status(journal_fixture):
    run = await start(journal_fixture)
    journal = replay.WorkflowJournal(run)
    await journal.live_frame({'type': 'thinking', 'stage': 'rag', 'status': 'partial',
                              'content': 'internal details', 'sources': ['private']})
    [event] = (await read(journal_fixture, run))['events']
    assert event['status'] == 'partial' and event['content'] == replay.RAG_NOTICES['partial']
    assert 'private' not in str(event) and 'internal details' not in str(event)


@pytest.mark.asyncio
async def test_lost_done_frame_replays_committed_reply_and_never_duplicates_after_ack(journal_fixture):
    fixture = journal_fixture
    run = await start(fixture)
    journal = replay.WorkflowJournal(run)
    await journal.live_frame({'type': 'thinking', 'stage': 'processing', 'content': 'live status'})
    db_table = ChatMessage.__table__
    async with fixture.factory() as db:
        async with db.begin():
            result = await db.execute(db_table.insert().values(
                session_id='session', role='assistant', content='complete committed answer'))
            await run.succeed_in(db, result.inserted_primary_key[0])
    last_page = await read(fixture, run, after=1)
    assert last_page['status'] == 'succeeded' and last_page['cursor'] == 2 and last_page['has_more'] is False
    terminal = last_page['events'][0]
    assert terminal == {'type': 'done', 'sequence': 2, 'session_id': 'session',
                        'workflow_id': run.id, 'replay_content': 'complete committed answer'}
    live_done = parse(await journal.live_frame({'type': 'done', 'session_id': 'session'}))
    assert live_done['sequence'] == terminal['sequence'] and 'replay_content' not in live_done
    assert (await read(fixture, run, after=last_page['cursor']))['events'] == []


@pytest.mark.asyncio
async def test_cancelled_run_replays_progress_without_uncommitted_partial_reply(journal_fixture, monkeypatch):
    fixture = journal_fixture
    run = await start(fixture)
    journal = replay.WorkflowJournal(run)
    monkeypatch.setattr(replay, 'MAX_PROGRESS_EVENTS', 2)
    for step in (1, 2, 3):
        await journal.live_frame({'type': 'plan_step_start', 'step': step, 'action': 'private text'})
    await journal.live_frame({'type': 'response', 'content': 'partial answer'})
    await run.finish('cancelled', 'STREAM_CLOSED')
    first = await read(fixture, run, limit=2)
    assert first['has_more'] is True and first['cursor'] == 2
    second = await read(fixture, run, after=2, limit=2)
    assert second['events'][0]['type'] == 'error' and second['events'][0]['reason'] == 'STREAM_CLOSED'
    assert second['cursor'] == 3 and 'partial answer' not in str(second)
    with pytest.raises(BusinessError):
        await replay.WorkflowJournal(run).live_frame({'type': 'plan_step_start', 'step': 1})


@pytest.mark.asyncio
async def test_event_read_is_owner_scoped_and_http_response_is_not_cacheable(journal_fixture, monkeypatch):
    fixture = journal_fixture
    run = await start(fixture)
    with pytest.raises(BusinessError) as denied:
        async with fixture.factory() as db:
            await replay.read_workflow_events(db, fixture.store.other.user_id, run.id, 0, 50)
    assert denied.value.http_status == 404
    monkeypatch.setattr(chat, 'async_session_factory', fixture.factory)
    app = FastAPI()
    app.include_router(chat.router, prefix='/api/v1')
    app.dependency_overrides[chat.get_current_user_id] = lambda: fixture.user_id
    endpoint = next(route for route in chat.router.routes if route.path.endswith('/{workflow_id}/events'))
    app.dependency_overrides[endpoint.dependencies[0].dependency] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        response = await client.get(f'/api/v1/chat/workflows/{run.id}/events?after=0')
    assert response.status_code == 200 and response.headers['cache-control'] == 'private, no-store'
    assert response.json()['data'] == {'cursor': 0, 'events': [], 'has_more': False, 'status': 'running'}
