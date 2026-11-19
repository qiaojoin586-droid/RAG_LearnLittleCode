"""Check write receipts through the typed tools with isolated SQL transactions."""

from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from langchain_core.messages import ToolMessage
from sqlalchemy.orm import Session

from app.ai_service.agent_tools import create_agent_tools
from app.ai_service.chat_graph import stream_chat_graph
from app.ai_service.tool_operations import ToolTurn, tool_turn
from app.core.failed_response import BusinessError
from app.model_config.snapshots import inference_actor
from app.models.category import NoteCategory
from app.models.chat import ChatMessage, ChatSession
from app.models.note import Note
from app.models.note_index import note_index_jobs, note_search_chunks
from app.models.review import ReviewRecord
from app.models.tool_operation import tool_operations
from app.models.user import User
from app.services.note_service import NoteService
from app.services.review_service import ReviewService
from test_model_config_control_plane import AsyncFacade, store  # noqa: F401


class ToolSession(AsyncFacade):
    def add(self, entity):
        self.session.add(entity)

    async def flush(self):
        self.session.flush()

    async def refresh(self, entity):
        self.session.refresh(entity)

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def delete(self, entity):
        self.session.delete(entity)

    async def commit(self):
        self.session.commit()

    @asynccontextmanager
    async def begin_nested(self):
        with self.session.begin_nested():
            yield self


@pytest.fixture
def local_tools(store):
    user_id = store.actor.user_id
    for table in (ChatSession.__table__, ChatMessage.__table__, NoteCategory.__table__,
                  Note.__table__, ReviewRecord.__table__, tool_operations, note_index_jobs, note_search_chunks):
        table.create(store.engine)
    store.sql(ChatSession.__table__.insert().values(id='session', user_id=user_id))
    store.sql(ChatMessage.__table__.insert().values([
        {'id': value, 'session_id': 'session', 'role': 'user', 'content': 'fixture'} for value in (1, 2, 3)
    ]))

    @asynccontextmanager
    async def factory():
        with Session(store.engine, expire_on_commit=False) as session:
            yield ToolSession(session)

    note_service = NoteService()
    tools = {tool.name: tool for tool in create_agent_tools(user_id, note_service=note_service,
             review_service=ReviewService(), db_session_factory=factory, groups=['note_write', 'review'])}
    return SimpleNamespace(store=store, user_id=user_id, tools=tools, note_service=note_service, factory=factory)


@contextmanager
def as_turn(user_id, message_id=1):
    token = inference_actor.set({'user_id': user_id, 'expires': None, 'jti': None})
    turn = tool_turn.set(ToolTurn('session', message_id))
    try:
        yield
    finally:
        tool_turn.reset(turn)
        inference_actor.reset(token)


async def call(local_tools, name, call_id, **arguments):
    result = await local_tools.tools[name].ainvoke({'type': 'tool_call', 'name': name, 'id': call_id, 'args': arguments})
    assert isinstance(result, ToolMessage)
    return result


@pytest.mark.asyncio
async def test_create_note_replays_after_commit_with_a_different_model_call_id(local_tools, monkeypatch):
    fx = local_tools

    async def committed(note):
        assert fx.store.rows(tool_operations)[0]['result_ref'] == {'note_id': note.id}
        assert fx.store.count(Note.__table__) == 1
        assert fx.store.rows(note_index_jobs)[0]['note_id'] == note.id

    schedule = AsyncMock(side_effect=committed)
    monkeypatch.setattr(fx.note_service, '_schedule_created_note', schedule)
    with as_turn(fx.user_id):
        first = await call(fx, 'create_note_tool', 'first', title='private title', content='private body', tags='a, b')
        repeated = await call(fx, 'create_note_tool', 'new-id', title='private title', content='private body', tags='a,b')
    assert first.content == repeated.content
    assert repeated.tool_call_id == 'new-id' and repeated.artifact == {**first.artifact, 'replayed': True}
    row = fx.store.rows(tool_operations)[0]
    assert row['operation_id'] == first.artifact['operation_id'] and first.artifact['replayed'] is False
    assert 'private title' not in str(row) and 'private body' not in str(row)
    assert fx.store.count(ReviewRecord.__table__) == 1
    schedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_replayed_note_update_does_not_undo_later_update(local_tools, monkeypatch):
    fx = local_tools
    monkeypatch.setattr(fx.note_service, '_schedule_created_note', AsyncMock())
    with as_turn(fx.user_id):
        await call(fx, 'create_note_tool', 'create', title='original', content='body')
        note = fx.store.rows(Note.__table__)[0]
        first = await call(fx, 'update_note_tool', 'update', note_id=note['id'], title='first')
        first_revision = fx.store.rows(note_index_jobs)[0]['revision']
    with as_turn(fx.user_id, 2):
        await call(fx, 'update_note_tool', 'new-turn', note_id=note['id'], title='later')
        current_revision = fx.store.rows(note_index_jobs)[0]['revision']
    with as_turn(fx.user_id):
        replay = await call(fx, 'update_note_tool', 'retry', note_id=note['id'], title='first')
    assert replay.artifact == {**first.artifact, 'replayed': True}
    assert fx.store.rows(Note.__table__)[0]['title'] == 'later'
    assert current_revision != first_revision
    assert fx.store.rows(note_index_jobs)[0]['revision'] == current_revision


@pytest.mark.asyncio
async def test_review_is_advanced_once_per_turn_and_replays_original_date(local_tools, monkeypatch):
    fx = local_tools
    monkeypatch.setattr(fx.note_service, '_schedule_created_note', AsyncMock())
    with as_turn(fx.user_id):
        await call(fx, 'create_note_tool', 'note', title='fixture', content='fixture')
        review_id = fx.store.rows(ReviewRecord.__table__)[0]['id']
        first = await call(fx, 'mark_reviewed_tool', 'review', review_id=review_id)
    with as_turn(fx.user_id, 2):
        await call(fx, 'mark_reviewed_tool', 'another-turn', review_id=review_id)
    with as_turn(fx.user_id):
        replay = await call(fx, 'mark_reviewed_tool', 'retry', review_id=review_id)
    assert replay.artifact == {**first.artifact, 'replayed': True} and replay.content == first.content
    assert fx.store.rows(ReviewRecord.__table__)[0]['review_count'] == 2


@pytest.mark.asyncio
async def test_roll_back_write_and_receipt_and_retry_without_launching_post_commit_work(local_tools, monkeypatch):
    fx = local_tools
    schedule = AsyncMock()
    monkeypatch.setattr(fx.note_service, '_schedule_created_note', schedule)
    original = fx.note_service.create_note

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('interrupted after row flush')

    monkeypatch.setattr(fx.note_service, 'create_note', interrupted)
    with as_turn(fx.user_id), pytest.raises(RuntimeError, match='interrupted after row flush'):
        await call(fx, 'create_note_tool', 'first', title='fixture', content='fixture')
    assert all(fx.store.count(table) == 0 for table in (Note.__table__, ReviewRecord.__table__, tool_operations))
    schedule.assert_not_awaited()
    monkeypatch.setattr(fx.note_service, 'create_note', original)
    with as_turn(fx.user_id):
        result = await call(fx, 'create_note_tool', 'retry', title='fixture', content='fixture')
    assert result.artifact['replayed'] is False
    schedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_account_revocation_and_mismatched_turn_deny_replay(local_tools, monkeypatch):
    fx = local_tools
    monkeypatch.setattr(fx.note_service, '_schedule_created_note', AsyncMock())
    with as_turn(fx.user_id, 99), pytest.raises(BusinessError) as missing:
        await call(fx, 'create_note_tool', 'unowned', title='fixture', content='fixture')
    assert missing.value.code == 40402
    with as_turn(fx.user_id):
        await call(fx, 'create_note_tool', 'allowed', title='fixture', content='fixture')
    fx.store.sql(User.__table__.update().where(User.uuid == fx.user_id).values(status='banned'))
    with as_turn(fx.user_id), pytest.raises(BusinessError) as denied:
        await call(fx, 'create_note_tool', 'revoked', title='fixture', content='fixture')
    assert denied.value.http_status == 403 and fx.store.count(Note.__table__) == 1


@pytest.mark.asyncio
async def test_commit_response_loss_is_recovered_from_receipt(local_tools, monkeypatch):
    fx = local_tools
    schedule = AsyncMock()
    monkeypatch.setattr(fx.note_service, '_schedule_created_note', schedule)
    original_begin = ToolSession.begin

    @asynccontextmanager
    async def lose_result(self):
        async with original_begin(self):
            yield self
        raise sa.exc.OperationalError('fixture commit', {}, TimeoutError('response lost'))

    monkeypatch.setattr(ToolSession, 'begin', lose_result)
    with as_turn(fx.user_id), pytest.raises(BusinessError) as unknown:
        await call(fx, 'create_note_tool', 'uncertain', title='fixture', content='fixture')
    assert unknown.value.http_status == 503
    assert fx.store.count(Note.__table__) == fx.store.count(tool_operations) == 1
    monkeypatch.setattr(ToolSession, 'begin', original_begin)
    with as_turn(fx.user_id):
        repeated = await call(fx, 'create_note_tool', 'recovered', title='fixture', content='fixture')
    assert repeated.artifact['replayed'] is True and fx.store.count(Note.__table__) == 1
    schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_worker_uses_the_committed_turn_and_resets_its_scope(local_tools, monkeypatch):
    fx = local_tools
    monkeypatch.setattr(fx.note_service, '_schedule_created_note', AsyncMock())

    async def run(state):
        result = await call(fx, 'create_note_tool', 'graph', title='from graph', content='fixture')
        await state['event_queue'].put({'type': 'tool_end', **result.artifact})

    monkeypatch.setattr('app.ai_service.chat_graph.build_chat_graph', lambda: SimpleNamespace(ainvoke=run))
    with as_turn(fx.user_id, 999):
        events = [event async for event in stream_chat_graph(SimpleNamespace(tool_turn=ToolTurn('session', 1)))]
        assert tool_turn.get() == ToolTurn('session', 999)
    assert events[0]['operation_id'] == fx.store.rows(tool_operations)[0]['operation_id']
