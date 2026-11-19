"""Verify the chat-only approval and SMTP boundary without contacting a mail host."""

import asyncio
from datetime import datetime
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import aiosmtplib
from fastapi import FastAPI
import httpx
import pytest

from app.ai_service import email_dispatch, tool_approvals
from app.ai_service.agent_tools import create_agent_tools
from app.ai_service.tool_operations import ToolTurn, tool_turn
from app.ai_service.workflows import ChatWorkflow
from app.core.failed_response import BusinessError
from app.core.failed_response_register import register_exception_handlers
from app.models.chat import ChatMessage, ChatSession
from app.models.email_dispatch import email_dispatches
from app.models.operation_approval import operation_approvals
from app.models.tool_operation import tool_operations
from app.routers import chat, note_router
from app.services.email_service import EmailService
from app.services.note_service import NoteService
from test_chat_workflows import fixture as checkpoint_fixture  # noqa: F401
from test_model_config_control_plane import store  # noqa: F401
from test_tool_approvals import fixture as approval_fixture  # noqa: F401
from test_transactional_tool_operations import as_turn, local_tools  # noqa: F401


@pytest.fixture
def fixture(approval_fixture):
    email_dispatches.create(approval_fixture.store.engine)
    return approval_fixture


async def propose(fixture, service, session_id='session', message_id=1, **values):
    arguments = {'to': 'fixture@example.invalid', 'subject': 'Fixture subject', 'body': 'Private test content',
                 'format': 'md', **values}
    with as_turn(fixture.user_id):
        turn = ToolTurn(session_id, message_id)
        workflow = await ChatWorkflow.start(fixture.factory, fixture.user_id, turn, None)
        turn_token = tool_turn.set(turn)
        workflow_token = tool_approvals.approval_workflow.set(workflow)
        try:
            tool = next(tool for tool in create_agent_tools(fixture.user_id, db_session_factory=fixture.factory,
                        email_service=service, groups=['email']) if tool.name == 'send_email')
            result = await tool.ainvoke({'type': 'tool_call', 'name': 'send_email', 'id': 'one', 'args': arguments})
            repeated = await tool.ainvoke({'type': 'tool_call', 'name': 'send_email', 'id': 'two', 'args': arguments})
            assert repeated.artifact == result.artifact and result.artifact['approval_status'] == 'pending'
        finally:
            tool_approvals.approval_workflow.reset(workflow_token)
            tool_turn.reset(turn_token)
    return next(row for row in fixture.store.rows(operation_approvals) if row['operation_id'] == result.artifact['operation_id'])


async def confirm(fixture, row, service, decision='approve'):
    with as_turn(fixture.user_id):
        return await tool_approvals.decide_approval(fixture.factory, fixture.user_id, row['operation_id'],
            row['preview_hash'], decision, None, None, email_service_factory=lambda: service)


@pytest.mark.asyncio
async def test_http_confirms_exact_email_and_reads_receipt(fixture, monkeypatch):
    service = SimpleNamespace(available=True, send_email=AsyncMock())
    session_id = str(uuid4())
    fixture.store.sql(ChatSession.__table__.insert().values(id=session_id, user_id=fixture.user_id))
    fixture.store.sql(ChatMessage.__table__.insert().values(id=4, session_id=session_id, role='user', content='fixture'))
    row = await propose(fixture, service, session_id, 4)
    assert not service.send_email.called
    monkeypatch.setattr(chat, 'async_session_factory', fixture.factory)
    monkeypatch.setattr(note_router, '_get_note_service', NoteService)
    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(init_manager=SimpleNamespace(email_service=service)))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(chat.router, prefix='/api/v1')
    app.dependency_overrides[chat.get_current_user_id] = lambda: fixture.user_id
    for route in chat.router.routes:
        if route.path.startswith('/chat/approvals/'):
            for dependency in route.dependencies:
                app.dependency_overrides[dependency.dependency] = lambda: None
    with as_turn(fixture.user_id):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as api:
            path = f'/api/v1/chat/sessions/{session_id}'
            items = (await api.get(f'{path}/approvals')).json()['data']
            assert items[0]['arguments'] == row['arguments']
            url = f"/api/v1/chat/approvals/{row['operation_id']}"
            decision = {'decision': 'approve', 'preview_hash': row['preview_hash']}
            tampered = await api.post(url, json={**decision, 'preview_hash': '0' * 64})
            assert tampered.status_code == 409 and not service.send_email.called
            first = (await api.post(url, json=decision)).json()['data']
            assert first['status'] == 'accepted'
            assert first == (await api.post(url, json=decision)).json()['data']
            listing = await api.get(f'{path}/email-operations')
            assert listing.headers['cache-control'] == 'private, no-store'
            assert listing.json()['data'][0]['result'] == first['result']
            assert 'Private test content' not in listing.text
            receipt = (await api.post(f'{url}/email-received')).json()['data']
            assert receipt['status'] == 'received'
    service.send_email.assert_awaited_once_with('fixture@example.invalid', 'Fixture subject', 'Private test content',
        attachments=[{'filename': 'Fixture subject.md', 'data': b'Private test content', 'mime': 'text/markdown'}],
        retries=0, message_id=f"<{row['operation_id']}@raglearn.local>")
    assert fixture.store.rows(operation_approvals)[0]['arguments'] is None


@pytest.mark.asyncio
async def test_smtp_timeout_after_attempt_is_unknown_and_never_retried(fixture, monkeypatch):
    messages = []

    class SMTP:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def login(self, *args):
            pass

        async def send_message(self, message):
            messages.append(message)
            raise aiosmtplib.SMTPTimeoutError('SMTP fixture lost the final response')

    monkeypatch.setattr(aiosmtplib, 'SMTP', SMTP)
    service = EmailService({'SMTP_HOST': 'fixture.invalid', 'SMTP_USERNAME': 'sender@example.invalid',
                            'SMTP_PASSWORD': 'fixture-only'})
    row = await propose(fixture, service, format='text')
    receipt = await confirm(fixture, row, service)
    assert receipt['status'] == 'unknown' and await confirm(fixture, row, service) == receipt
    message, = messages
    assert message['Message-ID'] == receipt['result']['message_id'] and message['To'] == 'fixture@example.invalid'
    other = fixture.store.other.user_id
    with as_turn(other), pytest.raises(BusinessError) as denied:
        await email_dispatch.mark_email_received(fixture.factory, other, row['operation_id'])
    assert denied.value.http_status == 404
    with as_turn(fixture.user_id):
        assert (await email_dispatch.mark_email_received(fixture.factory, fixture.user_id, row['operation_id']))['status'] == 'received'
    assert (await confirm(fixture, row, service))['status'] == 'received' and len(messages) == 1


@pytest.mark.asyncio
async def test_lost_holder_converges_to_unknown_without_sending(fixture, monkeypatch):
    service = SimpleNamespace(available=True, send_email=AsyncMock())
    row = await propose(fixture, service)
    original = tool_approvals.send_reserved_email

    async def lost_after_commit(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(tool_approvals, 'send_reserved_email', lost_after_commit)
    with pytest.raises(asyncio.CancelledError):
        await confirm(fixture, row, service)
    assert fixture.store.rows(operation_approvals)[0]['status'] == 'executed'
    assert (await confirm(fixture, row, service))['status'] == 'dispatching'
    fixture.store.sql(email_dispatches.update().values(deadline_at=datetime(2000, 1, 1)))
    monkeypatch.setattr(tool_approvals, 'send_reserved_email', original)
    assert (await confirm(fixture, row, service))['status'] == 'unknown'
    await email_dispatch.maintain_email_dispatches(fixture.factory)
    assert fixture.store.rows(email_dispatches)[0]['status'] == 'unknown'
    assert not service.send_email.called


@pytest.mark.asyncio
async def test_rejected_and_unrenderable_pdf_never_contact_smtp(fixture):
    def render(*args):
        raise ValueError('rendering failure')

    service = SimpleNamespace(available=True, send_email=AsyncMock(), render_markdown_pdf=render)
    row = await propose(fixture, service)
    assert (await confirm(fixture, row, service, 'reject'))['status'] == 'rejected'
    assert fixture.store.count(email_dispatches) == 0
    row = await propose(fixture, service, message_id=2, format='pdf')
    assert (await confirm(fixture, row, service))['status'] == 'failed'
    assert not service.send_email.called


@pytest.mark.asyncio
async def test_rate_limit_survives_chat_deletion(fixture, monkeypatch):
    monkeypatch.setattr('app.utils.config.get_security_config', lambda: {'email_rate_limit_per_hour': 1})
    service = SimpleNamespace(available=True, send_email=AsyncMock())
    confirmed = await propose(fixture, service)
    assert (await confirm(fixture, confirmed, service))['status'] == 'accepted'
    fixture.store.sql(ChatMessage.__table__.delete().where(ChatMessage.id == 1))
    assert fixture.store.count(tool_operations) == 0
    pending = await propose(fixture, service, message_id=2, subject='Another email')
    with pytest.raises(BusinessError) as denied:
        await confirm(fixture, pending, service)
    assert denied.value.http_status == 429 and service.send_email.await_count == 1
