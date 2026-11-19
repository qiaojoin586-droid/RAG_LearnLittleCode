"""Check user approval at the real chat tool and decision entry points."""

from datetime import datetime
from uuid import uuid4
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from app.ai_service import chat_graph, tool_approvals
from app.ai_service.chat_route import ChatRouteContext, collect_events
from app.ai_service.tool_operations import ToolTurn
from app.ai_service.workflows import ChatWorkflow
from app.core.failed_response import BusinessError
from app.models.chat import ChatMessage, ChatSession
from app.models.note import Note
from app.models.operation_approval import operation_approvals
from app.models.review import ReviewRecord
from app.models.tool_operation import tool_operations
from app.routers import chat, note_router
from app.services.review_service import ReviewService
from test_chat_workflows import fixture as checkpoint_fixture, start  # noqa: F401
from test_model_config_control_plane import store  # noqa: F401
from test_transactional_tool_operations import as_turn, call, local_tools  # noqa: F401


@pytest.fixture
def fixture(checkpoint_fixture, monkeypatch):
    fx = checkpoint_fixture
    operation_approvals.create(fx.store.engine)
    monkeypatch.setattr('app.services.note_service.NoteService._schedule_created_note', AsyncMock())
    return fx


async def propose(fx, name, **arguments):
    workflow = await start(fx)
    token = tool_approvals.approval_workflow.set(workflow)
    try:
        with as_turn(fx.user_id):
            receipt = await call(fx, name, 'proposal', **arguments)
    finally:
        tool_approvals.approval_workflow.reset(token)
    assert receipt.artifact['approval_status'] == 'pending'
    return fx.store.rows(operation_approvals)[0]


async def decide(fx, row, decision='approve', *, actor=None, preview_hash=None, note_service=None):
    from app.services.note_service import NoteService
    user_id = actor or fx.user_id
    with as_turn(user_id):
        return await tool_approvals.decide_approval(
            fx.factory, user_id, row['operation_id'], preview_hash or row['preview_hash'], decision,
            note_service or NoteService(), ReviewService(),
        )


@pytest.mark.asyncio
async def test_graph_proposes_and_only_authenticated_api_confirm_executes(fixture, monkeypatch):
    fx = fixture
    session_id = str(uuid4())
    fx.store.sql(ChatSession.__table__.insert().values(id=session_id, user_id=fx.user_id))
    fx.store.sql(ChatMessage.__table__.insert().values(id=4, session_id=session_id, role='user', content='create a note'))
    turn = ToolTurn(session_id, 4)
    payload = {'title': 'Proposed title', 'content': 'Exact proposed body', 'tags': 'alpha,beta'}

    async def react(ctx, timeout):
        proposal = await call(fx, 'create_note_tool', 'tool-call', **payload)
        again = await call(fx, 'create_note_tool', 'other-call', **payload)
        assert proposal.artifact == again.artifact and '尚未写入' in proposal.content
        yield {'type': 'response', 'content': proposal.content}

    async def classify(state):
        return {'classification': {'complexity': 'simple'}}

    monkeypatch.setattr(chat_graph, 'react_events', react)
    monkeypatch.setattr(chat_graph, 'classify_node', classify)
    monkeypatch.setattr(chat_graph, '_compiled_graph', None)
    with as_turn(fx.user_id):
        workflow = await ChatWorkflow.start(fx.factory, fx.user_id, turn, None)
        ctx = ChatRouteContext(None, fx.user_id, 'create a note', '', [], fx.factory, tool_turn=turn, workflow=workflow)
        _, text = await collect_events(chat_graph.stream_chat_graph(ctx))
        assert '尚未写入' in text and fx.store.count(Note.__table__) == 0
        assert fx.store.count(operation_approvals) == 1

        from app.services.note_service import NoteService
        monkeypatch.setattr(chat, 'async_session_factory', fx.factory)
        monkeypatch.setattr(note_router, '_get_note_service', NoteService)
        app = FastAPI()
        app.include_router(chat.router, prefix='/api/v1')
        app.dependency_overrides[chat.get_current_user_id] = lambda: fx.user_id
        route = next(route for route in chat.router.routes if route.path == '/chat/approvals/{operation_id}')
        app.dependency_overrides[route.dependencies[0].dependency] = lambda: None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as api:
            listing = await api.get(f'/api/v1/chat/sessions/{session_id}/approvals')
            assert listing.headers['cache-control'] == 'private, no-store'
            item, = listing.json()['data']
            assert item['arguments']['content'] == 'Exact proposed body'
            decision = {'decision': 'approve', 'preview_hash': item['preview_hash']}
            url = f"/api/v1/chat/approvals/{item['operation_id']}"
            injected = await api.post(url, json={**decision, 'arguments': {'content': 'unconfirmed replacement'}})
            assert injected.status_code == 422
            first = await api.post(url, json=decision)
            second = await api.post(url, json=decision)
            assert first.status_code == second.status_code == 200
            assert first.json()['data'] == second.json()['data'] and first.json()['data']['status'] == 'executed'
            assert (await api.get(f'/api/v1/chat/sessions/{session_id}/approvals')).json()['data'] == []
    note, = fx.store.rows(Note.__table__)
    assert note['content'] == 'Exact proposed body' and note['tags'] == ['alpha', 'beta']
    assert fx.store.rows(tool_operations)[0]['result_ref'] == {'note_id': note['id']}
    assert fx.store.rows(operation_approvals)[0]['arguments'] is None


@pytest.mark.asyncio
async def test_update_approval_denies_another_owner_and_detects_same_timestamp_edit(fixture):
    fx = fixture
    fx.store.sql(Note.__table__.insert().values(id='note', user_id=fx.user_id, title='Original', content='old'))
    row = await propose(fx, 'update_note_tool', note_id='note', content='proposed replace')
    with pytest.raises(BusinessError) as another:
        await decide(fx, row, actor=fx.store.other.user_id)
    assert another.value.http_status == 404
    with pytest.raises(BusinessError) as wrong_hash:
        await decide(fx, row, preview_hash='0' * 64)
    assert wrong_hash.value.http_status == 409
    timestamp = fx.store.rows(Note.__table__)[0]['updated_at']
    fx.store.sql(Note.__table__.update().where(Note.id == 'note').values(content='concurrent edit', updated_at=timestamp))
    assert (await decide(fx, row))['status'] == 'conflict'
    assert fx.store.rows(Note.__table__)[0]['content'] == 'concurrent edit'
    assert fx.store.rows(tool_operations)[0]['result_ref'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('expected', ['executed', 'rejected', 'expired'])
async def test_review_decision_is_terminal_and_clears_preview(fixture, expected):
    fx = fixture
    fx.store.sql(Note.__table__.insert().values(id='note', user_id=fx.user_id, title='Review me', content='body'))
    fx.store.sql(ReviewRecord.__table__.insert().values(id=42, note_id='note', user_id=fx.user_id,
                                                       next_review_at=datetime(2026, 1, 1)))
    row = await propose(fx, 'mark_reviewed_tool', review_id=42)
    if expected == 'expired':
        fx.store.sql(operation_approvals.update().values(expires_at=datetime(2000, 1, 1)))
        await tool_approvals.expire_approvals(fx.factory)
    result = await decide(fx, row, 'reject' if expected == 'rejected' else 'approve')
    assert result['status'] == expected
    assert await decide(fx, row) == result
    assert fx.store.rows(ReviewRecord.__table__)[0]['review_count'] == int(expected == 'executed')
    assert fx.store.rows(operation_approvals)[0]['target'] is None


@pytest.mark.asyncio
async def test_confirm_failure_rolls_back_note_and_receipt_then_can_retry(fixture):
    from app.services.note_service import NoteService
    fx = fixture
    row = await propose(fx, 'create_note_tool', title='new', content='proposed')

    class BrokenNoteService(NoteService):
        async def create_note(self, *args, **kwargs):
            await super().create_note(*args, **kwargs)
            raise RuntimeError('simulated failure after flush')

    with pytest.raises(RuntimeError):
        await decide(fx, row, note_service=BrokenNoteService())
    assert fx.store.count(Note.__table__) == 0
    assert fx.store.rows(operation_approvals)[0]['status'] == 'pending'
    assert fx.store.rows(tool_operations)[0]['result_ref'] is None
    assert (await decide(fx, row))['status'] == 'executed'
    assert fx.store.count(Note.__table__) == 1
