"""Exercise cursor replay and privacy bounds on disposable MySQL."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import importlib.util
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_workflow_events_gate(workspace, engine, Database, actor, report):
    from app.ai_service.tool_operations import ToolTurn
    from app.ai_service.workflow_events import WorkflowJournal, read_workflow_events
    from app.ai_service.workflows import ChatWorkflow
    from app.model_config.snapshots import inference_actor
    from app.models.chat import ChatMessage, ChatSession
    from app.models.workflow_event import workflow_events

    path = workspace / 'alembic/versions/f679ba2dc3e4_workflow_events.py'
    spec = importlib.util.spec_from_file_location('workflow_events_gate', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.context = SimpleNamespace(is_offline_mode=lambda: False)
    report['workflow_event_migration_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    report['workflow_event_code_sha256'] = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
        for name in ('app/ai_service/workflow_events.py', 'app/models/workflow_event.py', 'app/routers/chat.py')
    }
    messages = ChatMessage.__table__
    session_id = 'mysql-workflow-events'
    with engine.begin() as bind:
        with Operations.context(MigrationContext.configure(bind)):
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
        bind.execute(ChatSession.__table__.insert().values(id=session_id, user_id=actor.user_id))
        bind.execute(messages.insert().values(id=5, session_id=session_id, role='user', content='fixture'))
    report['checks']['workflow_event_mysql_migration_replay_and_empty_downgrade'] = True

    class EventsDatabase(Database):
        async def scalar(self, statement):
            return self.session.scalar(statement)

    @asynccontextmanager
    async def factory():
        with Session(engine) as session:
            yield EventsDatabase(session)

    async def commit():
        token = inference_actor.set({'user_id': actor.user_id})
        try:
            run = await ChatWorkflow.start(factory, actor.user_id, ToolTurn(session_id, 5), None)
            journal = WorkflowJournal(run)
            for step in (1, 2):
                await journal.live_frame({'type': 'plan_step_end', 'step': step, 'result': 'private intermediate result'})
            await journal.live_frame({'type': 'thinking', 'stage': 'thinking', 'content': 'private reasoning'})
            async with factory() as db:
                async with db.begin():
                    reply = await db.execute(messages.insert().values(
                        session_id=session_id, role='assistant', content='committed recovery fixture'))
                    await run.succeed_in(db, reply.inserted_primary_key[0])
            return run.id
        finally:
            inference_actor.reset(token)

    run_id = asyncio.run(commit())
    with Session(engine) as session:
        first = asyncio.run(read_workflow_events(EventsDatabase(session), actor.user_id, run_id, 1, 1))
    assert first['cursor'] == 2 and first['has_more'] and 'private' not in str(first)
    with Session(engine) as session:
        second = asyncio.run(read_workflow_events(EventsDatabase(session), actor.user_id, run_id, 2, 1))
    assert second['events'] == [{'type': 'done', 'sequence': 3, 'workflow_id': run_id, 'session_id': session_id,
                                 'replay_content': 'committed recovery fixture'}]
    assert not second['has_more']
    report['checks']['workflow_event_mysql_ordered_paging_and_committed_reply_replay'] = True

    with engine.begin() as bind:
        with Operations.context(MigrationContext.configure(bind)):
            try:
                migration.downgrade()
            except RuntimeError as error:
                assert 'Workflow events exist' in str(error)
            else:
                raise AssertionError('Populated event journal was not protected')
        bind.execute(messages.delete().where(messages.c.id == 5))
        assert bind.scalar(sa.select(sa.func.count()).select_from(workflow_events)) == 0
    report['checks']['workflow_event_mysql_populated_downgrade_and_turn_cleanup'] = True
