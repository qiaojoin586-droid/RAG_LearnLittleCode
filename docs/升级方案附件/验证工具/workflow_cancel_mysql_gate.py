"""Commit cancellation in the parent while a distinct process owns execution."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import subprocess
import sys

import sqlalchemy as sa
from sqlalchemy.orm import Session

from workflow_mysql_gate import WORKER_BOOTSTRAP


WORKER = WORKER_BOOTSTRAP + '''
from app.ai_service.detached_chat import start_detached_chat
from app.ai_service.workflows import read_workflow

async def main():
    workflow = ChatWorkflow(fixture['workflow_id'], factory)
    released = []
    producer_exited = False

    async def release():
        released.append(True)

    async def produce(workflow):
        nonlocal producer_exited
        try:
            await workflow.record_route('react')
            await asyncio.Future()
            yield 'unexpected'
        finally:
            producer_exited = True

    run = start_detached_chat(workflow, fixture['user_id'], produce, release)
    try:
        await asyncio.wait_for(asyncio.gather(run.task, return_exceptions=True), 25)
        async with factory() as db:
            state = await read_workflow(db, fixture['user_id'], workflow.id)
        assert state['status'] == 'cancelled' and state['error_code'] == 'USER_CANCELLED'
        assert producer_exited and released == [True]
        return {'stopped': True, 'released': True, 'state_version': state['state_version']}
    finally:
        engine.dispose()

print(json.dumps(asyncio.run(main())))
'''


def run_workflow_cancel_gate(workspace, engine, Database, actor, port, password, report):
    from app.ai_service.detached_chat import cancel_detached_chat, find_detached_chat
    from app.ai_service.tool_operations import ToolTurn
    from app.ai_service.workflows import ChatWorkflow, read_workflow
    from app.model_config.snapshots import inference_actor
    from app.models.chat import ChatMessage, ChatSession

    report['workflow_cancel_code_sha256'] = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
        for name in ('app/ai_service/detached_chat.py', 'app/ai_service/workflows.py',
                     'app/models/workflow_run.py', 'app/routers/chat.py')
    }
    session_id = 'cross-process-cancel'
    with engine.begin() as bind:
        bind.execute(ChatSession.__table__.insert().values(id=session_id, user_id=actor.user_id))
        bind.execute(ChatMessage.__table__.insert().values(id=10, session_id=session_id, role='user', content='fixture'))

    class WorkflowDatabase(Database):
        async def scalar(self, statement):
            return self.session.scalar(statement)

    @asynccontextmanager
    async def factory():
        with Session(engine) as session:
            yield WorkflowDatabase(session)

    async def exercise():
        token = inference_actor.set({'user_id': actor.user_id})
        try:
            workflow = await ChatWorkflow.start(factory, actor.user_id, ToolTurn(session_id, 10), None, detached=True)
            worker_input = {'port': port, 'password': password, 'user_id': actor.user_id, 'workflow_id': workflow.id}
            with subprocess.Popen(
                [sys.executable, '-X', 'utf8', '-c', WORKER], cwd=workspace,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf8',
            ) as worker:
                try:
                    worker.stdin.write(json.dumps(worker_input))
                    worker.stdin.close()
                    worker.stdin = None
                    async with asyncio.timeout(15):
                        while True:
                            if worker.poll() is not None:
                                raise RuntimeError('Detached fixture process stopped before cancellation')
                            async with factory() as db:
                                state = await read_workflow(db, actor.user_id, workflow.id)
                            if state['route'] == 'react':
                                break
                            await asyncio.sleep(0.05)
                    assert find_detached_chat(workflow.id, actor.user_id) is None
                    cancelled = await cancel_detached_chat(factory, workflow.id, actor.user_id)
                    assert cancelled['status'] == 'cancelled'
                    stdout, _ = worker.communicate(timeout=10)
                    if worker.returncode:
                        raise RuntimeError(f'Detached cancellation fixture exited with status {worker.returncode}')
                    result = json.loads(stdout.splitlines()[-1])
                    assert result['stopped'] and result['released']
                    assert result['state_version'] == cancelled['state_version']
                finally:
                    if worker.poll() is None:
                        worker.kill()
                        worker.communicate(timeout=5)
        finally:
            inference_actor.reset(token)

    asyncio.run(exercise())
    report['checks']['detached_chat_mysql_cancelled_by_separate_process_without_notification'] = True
