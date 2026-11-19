"""Test the approval migration and concurrent decisions in isolated MySQL."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
import importlib.util
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_tool_approval_gate(workspace, engine, Database, actor, report):
    from app.ai_service.agent_tools import create_agent_tools
    from app.ai_service.tool_approvals import approval_workflow, decide_approval, list_approvals
    from app.ai_service.tool_operations import ToolTurn, tool_turn
    from app.ai_service.workflows import ChatWorkflow
    from app.model_config.snapshots import inference_actor
    from app.models.category import NoteCategory
    from app.models.chat import ChatMessage, ChatSession
    from app.models.note import Note
    from app.models.operation_approval import operation_approvals as approvals
    from app.models.review import ReviewRecord
    from app.models.tool_operation import tool_operations
    from app.services.note_service import NoteService
    from app.services.review_service import ReviewService

    path = workspace / 'alembic/versions/d013fe6ab827_operation_approvals.py'
    spec = importlib.util.spec_from_file_location('tool_approval_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.context = SimpleNamespace(is_offline_mode=lambda: False)
    report['tool_approval_migration_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    report['tool_approval_code_sha256'] = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
        for name in ('app/ai_service/tool_approvals.py', 'app/ai_service/note_tool_writes.py', 'app/ai_service/tool_operations.py',
                     'app/ai_service/agent_tools.py', 'app/ai_service/chat_graph.py', 'app/routers/chat.py', 'app/models/operation_approval.py')
    }

    session_id = 'approval-fixture-session'
    with engine.begin() as bind:
        for table in (NoteCategory.__table__, Note.__table__, ReviewRecord.__table__):
            table.create(bind, checkfirst=True)
        with Operations.context(MigrationContext.configure(bind)):
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
        bind.execute(ChatSession.__table__.insert().values(id=session_id, user_id=actor.user_id))
        message = bind.execute(ChatMessage.__table__.insert().values(session_id=session_id, role='user', content='fixture'))
        turn = ToolTurn(session_id, message.inserted_primary_key[0])

    class ToolDatabase(Database):
        async def scalar(self, statement):
            return self.session.scalar(statement)

        def add(self, entity):
            self.session.add(entity)

        async def flush(self):
            self.session.flush()

        async def refresh(self, entity):
            self.session.refresh(entity)

    @asynccontextmanager
    async def factory():
        with Session(engine, expire_on_commit=False) as session:
            yield ToolDatabase(session)

    service = NoteService()
    service._schedule_created_note = AsyncMock()

    async def propose():
        actor_token = inference_actor.set({'user_id': actor.user_id})
        turn_token = tool_turn.set(turn)
        workflow_token = None
        try:
            workflow = await ChatWorkflow.start(factory, actor.user_id, turn, None)
            workflow_token = approval_workflow.set(workflow)
            tool = next(tool for tool in create_agent_tools(actor.user_id, note_service=service,
                                                           db_session_factory=factory, groups=['note_write'])
                        if tool.name == 'create_note_tool')
            proposed = await tool.ainvoke({'type': 'tool_call', 'name': tool.name, 'id': 'mysql-approval',
                                           'args': {'title': 'guarded fixture', 'content': 'approved exact body'}})
            assert proposed.artifact['approval_status'] == 'pending'
            async with factory() as db:
                item, = await list_approvals(db, actor.user_id, session_id)
                assert item['operation_id'] == proposed.artifact['operation_id']
                assert await db.scalar(sa.select(sa.func.count()).select_from(Note).where(Note.title == 'guarded fixture')) == 0
            return item
        finally:
            if workflow_token is not None:
                approval_workflow.reset(workflow_token)
            tool_turn.reset(turn_token)
            inference_actor.reset(actor_token)

    item = asyncio.run(propose())
    barrier = Barrier(2, timeout=5)

    def confirm():
        token = inference_actor.set({'user_id': actor.user_id})
        try:
            barrier.wait()
            return asyncio.run(decide_approval(factory, actor.user_id, item['operation_id'], item['preview_hash'],
                                               'approve', service, ReviewService()))
        finally:
            inference_actor.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(confirm) for _ in range(2)]
        first, second = [task.result(timeout=15) for task in tasks]
    assert first == second and first['status'] == 'executed'
    assert service._schedule_created_note.await_count == 1
    with engine.begin() as bind:
        assert bind.exec_driver_sql("SELECT COUNT(*) FROM notes WHERE title = 'guarded fixture'").scalar_one() == 1
        saved = bind.scalar(sa.select(Note.content).where(Note.id == first['result']['note_id']))
        assert saved == item['arguments']['content']
        assert bind.scalar(sa.select(approvals.c.arguments).where(approvals.c.operation_id == item['operation_id'])) is None
        with Operations.context(MigrationContext.configure(bind)):
            try:
                migration.downgrade()
            except RuntimeError as error:
                assert 'Approval receipts exist' in str(error)
            else:
                raise AssertionError('Populated approval table was not protected')
        bind.execute(ChatMessage.__table__.delete().where(ChatMessage.id == turn.message_id))
        assert bind.scalar(sa.select(sa.func.count()).select_from(approvals)) == 0
        assert bind.scalar(sa.select(tool_operations.c.operation_id).where(
            tool_operations.c.operation_id == item['operation_id'])) is None
    report['checks']['tool_approval_mysql_migration_retry_replay_and_safe_downgrade'] = True
    report['checks']['tool_approval_mysql_concurrent_confirm_commits_one_exact_write'] = True
