"""Run a detached worker through progress, reply and cancellation on MySQL."""

import asyncio
from contextlib import asynccontextmanager, aclosing
import hashlib
import importlib.util
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations


def run_detached_chat_gate(workspace, engine, Database, actor, report):
    from app.ai_service.detached_chat import cancel_detached_chat, start_detached_chat
    from app.ai_service.tool_operations import ToolTurn
    from app.ai_service.workflow_events import WorkflowJournal, read_workflow_events
    from app.ai_service.workflows import ChatWorkflow, read_workflow
    from app.model_config.snapshots import inference_actor
    from app.models.chat import ChatMessage, ChatSession

    report['detached_chat_code_sha256'] = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
        for name in ('app/ai_service/detached_chat.py', 'app/ai_service/workflows.py', 'app/routers/chat.py',
                     'app/ai_service/agent_tools.py', 'app/ai_service/plan_execute_agent.py')
    }
    session_id = 'detached-async-mysql'
    messages = ChatMessage.__table__
    with engine.begin() as bind:
        bind.execute(ChatSession.__table__.insert().values(id=session_id, user_id=actor.user_id))
        bind.execute(messages.insert(), [
            {'id': index, 'session_id': session_id, 'role': 'user', 'content': 'fixture'}
            for index in (7, 8)
        ])
        path = workspace / 'alembic/versions/b74e11c832af_chat_citations.py'
        spec = importlib.util.spec_from_file_location('chat_citation_migration', path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.context = SimpleNamespace(is_offline_mode=lambda: False)
        with Operations.context(MigrationContext.configure(bind)):
            migration.downgrade()
            migration.upgrade()
            migration.upgrade()
            assert bind.scalar(sa.select(sa.func.count()).select_from(messages).where(
                messages.c.session_id == session_id, messages.c.citations_json.is_(None))) == 2
        report['checks']['chat_citation_migration_preserves_legacy_messages'] = True

    class DetachedDatabase(Database):
        async def scalar(self, statement):
            return self.session.scalar(statement)

    @asynccontextmanager
    async def factory():
        with Session(engine) as session:
            yield DetachedDatabase(session)

    async def exercise():
        token = inference_actor.set({'user_id': actor.user_id})
        releases = []

        async def release():
            releases.append(True)

        try:
            acknowledged = asyncio.Event()
            workflow = await ChatWorkflow.start(factory, actor.user_id, ToolTurn(session_id, 7), None, detached=True)

            async def produce(workflow):
                journal = WorkflowJournal(workflow)
                yield await journal.live_frame({'type': 'thinking', 'stage': 'processing'})
                await acknowledged.wait()
                async with factory() as db:
                    async with db.begin():
                        reply = await db.execute(messages.insert().values(
                            session_id=session_id, role='assistant', content='completed while detached',
                            citations_json={'E1': {'locator_digest': 'mysql-fixture'}}))
                        await workflow.succeed_in(db, reply.inserted_primary_key[0])
                yield await journal.live_frame({'type': 'done', 'session_id': session_id})

            run = start_detached_chat(workflow, actor.user_id, produce, release)
            async with aclosing(run.frames()) as frames:
                assert (await anext(frames)).startswith('id: 1')
            assert not run.task.done()
            acknowledged.set()
            await asyncio.wait_for(run.task, 5)
            async with factory() as db:
                page = await read_workflow_events(db, actor.user_id, workflow.id, 1, 50)
            assert page['status'] == 'succeeded' and page['events'][0]['replay_content'] == 'completed while detached'
            assert page['events'][0]['citation_ids'] == ['E1'] and 'locator_digest' not in str(page)
            with engine.begin() as bind, Operations.context(MigrationContext.configure(bind)):
                try:
                    migration.downgrade()
                except RuntimeError as error:
                    assert str(error) == 'Saved citations exist'
                else:
                    raise AssertionError('Expected populated citation downgrade to refuse')
            report['checks']['chat_citation_mysql_replay_and_populated_downgrade'] = True

            workflow = await ChatWorkflow.start(factory, actor.user_id, ToolTurn(session_id, 8), None, detached=True)
            running = asyncio.Event()

            async def suspended(_workflow):
                running.set()
                await asyncio.Future()
                yield 'unexpected result'

            run = start_detached_chat(workflow, actor.user_id, suspended, release)
            await running.wait()
            cancelled = await cancel_detached_chat(factory, workflow.id, actor.user_id)
            await asyncio.gather(run.task, return_exceptions=True)
            assert cancelled['status'] == 'cancelled' and cancelled['error_code'] == 'USER_CANCELLED'
            async with factory() as db:
                after_cleanup = await read_workflow(db, actor.user_id, workflow.id)
            assert after_cleanup['reply_message_id'] is None and len(releases) == 2
        finally:
            inference_actor.reset(token)

    asyncio.run(asyncio.wait_for(exercise(), 20))
    report['checks']['detached_chat_mysql_disconnect_commit_and_replay'] = True
    report['checks']['detached_chat_mysql_explicit_cancel_and_slot_release'] = True
