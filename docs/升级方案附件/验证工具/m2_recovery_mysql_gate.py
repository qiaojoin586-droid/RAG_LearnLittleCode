"""Kill a Worker after it reserves a file operation, then recover without redispatch."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import importlib.util
import json
import subprocess
import sys

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.orm import Session

from workflow_mysql_gate import WORKER_BOOTSTRAP


WORKER = WORKER_BOOTSTRAP + '''
from app.ai_service.chat_graph import stream_chat_graph
from app.ai_service.chat_route import ChatRouteContext, collect_events
from app.ai_service.detached_chat import find_detached_chat
from app.ai_service.detached_worker import DetachedChatWorker
from app.ai_service.tool_operations import run_artifact_tool
from app.model_config.snapshots import current_snapshot

async def produce(job, user_id, session_id):
    assert job.workflow.attempt == fixture['attempt']
    assert current_snapshot().release_id == fixture['release_id']
    turn = ToolTurn(session_id, job.turn_message_id)
    if job.workflow.attempt == 1:
        async def simulate_external_acceptance():
            await job.workflow.record_route('react')
            await asyncio.Future()
        call = {'name': 'text_to_speech', 'id': 'fixture', 'args': {'text': 'isolated utterance'}}
        await run_artifact_tool(job.workflow, user_id, turn, call, simulate_external_acceptance)
        raise AssertionError('Crash fixture returned unexpectedly')
    ctx = ChatRouteContext(None, user_id, 'isolated recovery question', '', [], factory,
                           tool_turn=turn, workflow=job.workflow)
    events, answer = await collect_events(stream_chat_graph(ctx))
    assert '待核对' in answer and all(event['type'] != 'tool_file' for event in events)
    async with factory() as db:
        async with db.begin():
            await job.workflow.succeed_in(db)
    yield 'complete'

async def main():
    worker = DetachedChatWorker(factory, produce)
    assert await worker.tick()
    run = find_detached_chat(fixture['workflow_id'], fixture['user_id'])
    await asyncio.wait_for(run.task, 20)
    engine.dispose()

asyncio.run(main())
print(json.dumps({'recovered': True}))
'''


def run_m2_recovery_gate(workspace, engine, Database, actor, port, password, report):
    from app.ai_service.detached_worker import enqueue_chat, lock_chat_admission
    from app.ai_service.tool_operations import ToolTurn
    from app.ai_service.workflows import read_workflow
    from app.model_config.snapshots import inference_actor
    from app.model_config.tables import ai_config_heads
    from app.models.chat import ChatMessage, ChatSession
    from app.models.tool_operation import tool_operation_steps, tool_operations
    from app.models.workflow_run import workflow_runs

    path = workspace / 'alembic/versions/e5312db748ce_tool_operation_steps.py'
    spec = importlib.util.spec_from_file_location('m2_operation_step_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    from types import SimpleNamespace
    migration.context = SimpleNamespace(is_offline_mode=lambda: False)

    class WorkflowDatabase(Database):
        async def scalar(self, statement):
            return self.session.scalar(statement)

    @asynccontextmanager
    async def factory():
        with Session(engine) as session:
            yield WorkflowDatabase(session)

    session_id = 'detached-m2-operation-recovery'
    with engine.begin() as bind:
        with Operations.context(MigrationContext.configure(bind)):
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
        release_id = bind.scalar(sa.select(ai_config_heads.c.active_release_id))
        bind.execute(ChatSession.__table__.insert().values(id=session_id, user_id=actor.user_id))
        source = bind.execute(ChatMessage.__table__.insert().values(session_id=session_id, role='user', content='fixture'))
        turn = ToolTurn(session_id, source.inserted_primary_key[0])

    async def enqueue():
        token = inference_actor.set({'user_id': actor.user_id})
        try:
            async with factory() as db:
                async with db.begin():
                    await lock_chat_admission(db, actor.user_id)
                    return await enqueue_chat(db, actor.user_id, turn, release_id, allow_actions=True)
        finally:
            inference_actor.reset(token)

    workflow_id = asyncio.run(enqueue())
    worker_input = {'port': port, 'password': password, 'workflow_id': workflow_id, 'user_id': actor.user_id,
                    'release_id': release_id, 'attempt': 1}
    command = [sys.executable, '-X', 'utf8', '-c', WORKER]

    async def wait_for_effect():
        async with asyncio.timeout(15):
            while first.poll() is None:
                async with factory() as db:
                    view = await read_workflow(db, actor.user_id, workflow_id)
                if view['route'] == 'react':
                    return
                await asyncio.sleep(0.05)
        raise RuntimeError('First recovery fixture Worker exited before reserving the operation')

    with subprocess.Popen(command, cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, encoding='utf8') as first:
        try:
            first.stdin.write(json.dumps(worker_input))
            first.stdin.close()
            first.stdin = None
            asyncio.run(wait_for_effect())
        finally:
            if first.poll() is None:
                first.kill()
            first.communicate(timeout=5)

    receipt_query = sa.select(tool_operations.c.operation_id, tool_operations.c.result_ref).join(
        tool_operation_steps, tool_operations.c.operation_id == tool_operation_steps.c.operation_id,
    ).where(tool_operation_steps.c.workflow_id == workflow_id)
    with engine.begin() as bind:
        original = bind.execute(receipt_query).one()
        assert original.result_ref == {'state': 'unknown'}
        bind.execute(workflow_runs.update().where(workflow_runs.c.id == workflow_id)
                     .values(lease_expires_at=datetime(2000, 1, 1)))
    worker_input['attempt'] = 2
    second = subprocess.run(command, cwd=workspace, input=json.dumps(worker_input), encoding='utf8',
                            capture_output=True, timeout=25)
    if second.returncode:
        raise RuntimeError(f'Operation recovery fixture Worker exited with status {second.returncode}')
    assert json.loads(second.stdout.splitlines()[-1])['recovered']
    with engine.begin() as bind:
        assert bind.execute(receipt_query).one() == original
        assert bind.scalar(sa.select(workflow_runs.c.status).where(workflow_runs.c.id == workflow_id)) == 'succeeded'
        with Operations.context(MigrationContext.configure(bind)):
            try:
                migration.downgrade()
            except RuntimeError as error:
                assert 'Tool operation step receipts exist' in str(error)
            else:
                raise AssertionError('Populated operation receipts were not protected')
        bind.execute(ChatSession.__table__.delete().where(ChatSession.id == session_id))
        assert bind.execute(receipt_query).first() is None
    report['checks']['workflow_m2_mysql_crash_recovers_unknown_without_redispatch'] = True
    report['checks']['workflow_m2_operation_step_migration_and_cascade'] = True
