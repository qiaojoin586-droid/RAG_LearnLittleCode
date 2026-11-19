"""Concurrent confirmation and missing SMTP acknowledgement on disposable MySQL."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
import importlib.util
from threading import Barrier
from types import SimpleNamespace

import aiosmtplib
from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_email_dispatch_gate(workspace, engine, Database, actor, report):
    from app.ai_service.email_dispatch import list_email_operations, mark_email_received, maintain_email_dispatches
    from app.ai_service.tool_approvals import approval_workflow, decide_approval, list_approvals
    from app.ai_service.tool_operations import ToolTurn, request_email_approval, tool_turn
    from app.ai_service.workflows import ChatWorkflow
    from app.model_config.snapshots import inference_actor
    from app.models.chat import ChatMessage, ChatSession
    from app.models.email_dispatch import email_dispatches as dispatches
    from app.models.operation_approval import operation_approvals as approvals

    path = workspace / 'alembic/versions/e124af7bc938_email_dispatches.py'
    spec = importlib.util.spec_from_file_location('email_dispatch_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.context = SimpleNamespace(is_offline_mode=lambda: False)
    report['email_dispatch_migration_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    report['email_dispatch_code_sha256'] = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
        for name in ('app/ai_service/email_dispatch.py', 'app/ai_service/tool_approvals.py',
                     'app/ai_service/tool_operations.py', 'app/ai_service/agent_tools.py',
                     'app/services/email_service.py', 'app/routers/chat.py', 'app/models/email_dispatch.py')
    }

    session_id = 'email-operation-fixture'
    with engine.begin() as bind:
        with Operations.context(MigrationContext.configure(bind)):
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
        bind.execute(ChatSession.__table__.insert().values(id=session_id, user_id=actor.user_id))
        def make_turn():
            message = bind.execute(ChatMessage.__table__.insert().values(session_id=session_id, role='user', content='fixture'))
            return ToolTurn(session_id, message.inserted_primary_key[0])
        turn = make_turn()
        interrupted_turn = make_turn()

    class ToolDatabase(Database):
        async def scalar(self, statement):
            return self.session.scalar(statement)

    @asynccontextmanager
    async def factory():
        with Session(engine, expire_on_commit=False) as session:
            yield ToolDatabase(session)

    arguments = {'to': 'fixture@example.invalid', 'subject': 'Fixture mail', 'body': 'isolated content', 'format': 'text'}

    async def propose(source, payload):
        token = inference_actor.set({'user_id': actor.user_id})
        turn_token = tool_turn.set(source)
        workflow_token = None
        try:
            workflow = await ChatWorkflow.start(factory, actor.user_id, source, None)
            workflow_token = approval_workflow.set(workflow)
            _, result = await request_email_approval(factory, actor.user_id, payload)
            async with factory() as db:
                pending = await list_approvals(db, actor.user_id, session_id)
            return next(item for item in pending if item['operation_id'] == result['operation_id'])
        finally:
            if workflow_token is not None:
                approval_workflow.reset(workflow_token)
            tool_turn.reset(turn_token)
            inference_actor.reset(token)

    item = asyncio.run(propose(turn, arguments))
    calls = []

    async def mock_smtp(to, subject, body, *, attachments, retries, message_id):
        assert (to, subject, body) == (arguments['to'], arguments['subject'], arguments['body'])
        assert attachments is None and retries == 0
        with engine.connect() as bind:
            assert bind.scalar(sa.select(dispatches.c.status).where(dispatches.c.operation_id == item['operation_id'])) == 'dispatching'
            assert bind.scalar(sa.select(approvals.c.arguments).where(approvals.c.operation_id == item['operation_id'])) is None
        calls.append(message_id)
        await asyncio.sleep(0.05)
        raise aiosmtplib.SMTPTimeoutError('isolated lost acknowledgement')

    service = SimpleNamespace(available=True, send_email=mock_smtp)
    barrier = Barrier(2, timeout=5)

    async def approve(candidate):
        return await decide_approval(factory, actor.user_id, candidate['operation_id'], candidate['preview_hash'],
                                     'approve', None, None, email_service_factory=lambda: service)

    def confirm():
        token = inference_actor.set({'user_id': actor.user_id})
        try:
            barrier.wait()
            return asyncio.run(approve(item))
        finally:
            inference_actor.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(confirm) for _ in range(2)]
        first, second = [task.result(timeout=15) for task in tasks]
    assert first['result'] == second['result'] and len(calls) == 1
    assert all(result['status'] in ('dispatching', 'unknown') for result in (first, second))

    interrupted = asyncio.run(propose(interrupted_turn, {**arguments, 'subject': 'Reservation recovery'}))

    async def verify_recovery():
        from unittest.mock import patch

        token = inference_actor.set({'user_id': actor.user_id})
        try:
            assert (await approve(item))['status'] == 'unknown'
            assert (await mark_email_received(factory, actor.user_id, item['operation_id']))['status'] == 'received'
            with patch('app.ai_service.tool_approvals.send_reserved_email', side_effect=asyncio.CancelledError):
                try:
                    await approve(interrupted)
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError('The simulated interruption did not occur')
            with engine.begin() as bind:
                bind.execute(dispatches.update().where(dispatches.c.operation_id == interrupted['operation_id']).values(
                    deadline_at=sa.func.current_timestamp(),
                ))
            await maintain_email_dispatches(factory)
            assert (await approve(interrupted))['status'] == 'unknown' and len(calls) == 1
            async with factory() as db:
                rows = await list_email_operations(db, actor.user_id, session_id)
            assert {row['status'] for row in rows} == {'received', 'unknown'}
            assert all('body' not in row['result'] for row in rows)
        finally:
            inference_actor.reset(token)

    asyncio.run(verify_recovery())
    with engine.begin() as bind:
        with Operations.context(MigrationContext.configure(bind)):
            try:
                migration.downgrade()
            except RuntimeError as error:
                assert 'Email dispatch receipts exist' in str(error)
            else:
                raise AssertionError('Populated dispatch table was not protected')
        bind.execute(ChatSession.__table__.delete().where(ChatSession.id == session_id))
        assert bind.scalar(sa.select(sa.func.count()).select_from(dispatches)) == 2
    report['checks']['email_dispatch_mysql_migration_replay_and_guarded_downgrade'] = True
    report['checks']['email_dispatch_mysql_concurrent_confirmation_has_one_smtp_attempt'] = True
    report['checks']['email_dispatch_mysql_lost_reservation_reconciliation_no_resend'] = True
