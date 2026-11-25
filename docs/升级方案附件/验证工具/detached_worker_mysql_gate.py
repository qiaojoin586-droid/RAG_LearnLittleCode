"""Kill a claimed fixture Worker and resume its completed Plan steps in a new process."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import json
import subprocess
import sys
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from workflow_mysql_gate import WORKER_BOOTSTRAP


WORKER = WORKER_BOOTSTRAP + '''
from types import SimpleNamespace

from app.ai_service import plan_execute_agent as plans
from app.ai_service.chat_graph import stream_chat_graph
from app.ai_service.chat_route import ChatRouteContext
from app.ai_service.detached_chat import find_detached_chat, read_only_chat
from app.ai_service.detached_worker import DetachedChatWorker
from app.ai_service.query_classifier import QueryClassifier
from app.ai_service.workflow_events import WorkflowJournal
from app.model_config.snapshots import current_snapshot
from app.models.chat import ChatMessage

fixture_calls = []

async def classify(self, question):
    assert fixture['attempt'] == 1
    return SimpleNamespace(complexity='complex', source='fixture', reason='worker crash probe')

async def generate(*args, **kwargs):
    assert fixture['attempt'] == 1
    return plans.ExecutionPlan('goal', [plans.PlanStep(1, 'prepare', 'none'), plans.PlanStep(2, 'finish', 'none', [1])])

async def synthesize(**kwargs):
    assert kwargs['results'] == {1: 'first step result', 2: 'second step result'}
    yield {'type': 'response', 'content': 'isolated recovered answer'}

class Model:
    async def astream(self, messages):
        prompt = messages[-1].content
        if '当前任务步骤：prepare' in prompt:
            fixture_calls.append('prepare')
            assert fixture['attempt'] == 1
            yield SimpleNamespace(content='first step result')
        else:
            assert 'first step result' in prompt
            if fixture['attempt'] == 1:
                await asyncio.Future()
            fixture_calls.append('finish')
            yield SimpleNamespace(content='second step result')

QueryClassifier.classify = classify
plans._generate_plan = generate
plans._synthesize = synthesize
plans.get_reflection_config = lambda: {}

async def produce(job, user_id, session_id):
    assert current_snapshot().release_id == fixture['release_id'] and read_only_chat.get()
    assert job.workflow.attempt == fixture['attempt']
    journal = await WorkflowJournal.resume(job.workflow)
    ctx = ChatRouteContext(agent_model=Model(), plan_model=object(), user_id=user_id,
                           user_message='isolated source question', system_prompt='', compressed_messages=[],
                           db_session_factory=factory, workflow=job.workflow)
    answer = ''
    async for event in stream_chat_graph(ctx):
        assert event['type'] != 'error'
        if event['type'] == 'response':
            answer += event['content']
        yield await journal.live_frame(event)
    assert answer == 'isolated recovered answer'
    async with factory() as db:
        async with db.begin():
            result = await db.execute(ChatMessage.__table__.insert().values(
                session_id=session_id, role='assistant', content=answer,
            ))
            await job.workflow.succeed_in(db, result.inserted_primary_key[0])

async def main():
    worker = DetachedChatWorker(factory, produce)
    assert await worker.tick()
    run = find_detached_chat(fixture['workflow_id'], fixture['user_id'])
    await asyncio.wait_for(run.task, 20)
    assert fixture_calls == ['finish']
    engine.dispose()

asyncio.run(main())
print(json.dumps({'completed': True}))
'''


def run_detached_worker_gate(workspace, engine, Database, actor, port, password, report):
    from app.ai_service import detached_worker
    from app.ai_service.detached_worker import DetachedChatWorker, enqueue_chat, lock_chat_admission
    from app.ai_service.tool_operations import ToolTurn
    from app.ai_service.workflow_events import read_workflow_events
    from app.ai_service.workflows import ChatWorkflow, read_workflow
    from app.core.failed_response import BusinessError
    from app.model_config.snapshots import inference_actor
    from app.model_config.tables import ai_config_heads, ai_config_releases
    from app.models.chat import ChatMessage, ChatSession
    from app.models.user import User
    from app.models.workflow_run import workflow_runs

    session_id, message_id = 'durable-chat-worker', 12
    with engine.begin() as bind:
        head = bind.scalar(sa.select(ai_config_heads.c.active_release_id))
        release_id = bind.execute(sa.select(ai_config_releases.c.id).where(ai_config_releases.c.id != head)
                                  .limit(1)).scalar_one()
        bind.execute(ChatSession.__table__.insert().values(id=session_id, user_id=actor.user_id))
        bind.execute(ChatMessage.__table__.insert().values(id=message_id, session_id=session_id, role='user',
                                                          content='isolated source question'))

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
            async with factory() as db:
                async with db.begin():
                    await lock_chat_admission(db, actor.user_id)
                    workflow_id = await enqueue_chat(db, actor.user_id, ToolTurn(session_id, message_id), release_id)
        finally:
            inference_actor.reset(token)
        worker_input = {'port': port, 'password': password, 'release_id': release_id,
                        'user_id': actor.user_id, 'workflow_id': workflow_id, 'attempt': 1}
        command = [sys.executable, '-X', 'utf8', '-c', WORKER]
        with subprocess.Popen(command, cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding='utf8') as process:
            try:
                process.stdin.write(json.dumps(worker_input))
                process.stdin.close()
                process.stdin = None
                async with asyncio.timeout(12):
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError('First durable fixture Worker exited before the crash probe')
                        async with factory() as db:
                            view = await read_workflow(db, actor.user_id, workflow_id)
                        if view['finished_steps'] == [1]:
                            break
                        await asyncio.sleep(0.05)
                assert view['status'] == 'running' and view['execution_attempt'] == 1
                with engine.connect() as bind:
                    checkpoint = bind.scalar(sa.select(workflow_runs.c.plan_checkpoint).where(workflow_runs.c.id == workflow_id))
                    assert checkpoint['results']['1']['text'] == 'first step result'
                assert await DetachedChatWorker(factory, None).claim() is None
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

        # Move just the stopped process's lease into the past to avoid a 45-second test wait.
        with engine.begin() as bind:
            bind.execute(workflow_runs.update().where(workflow_runs.c.id == workflow_id)
                         .values(lease_expires_at=datetime(2000, 1, 1)))
        async with factory() as db:
            queued = await read_workflow(db, actor.user_id, workflow_id)
        assert queued['status'] == 'running'
        worker_input['attempt'] = 2
        second = subprocess.run(command, cwd=workspace, input=json.dumps(worker_input), encoding='utf8',
                                capture_output=True, timeout=25)
        if second.returncode:
            raise RuntimeError(f'Recovery fixture Worker exited with status {second.returncode}')
        assert json.loads(second.stdout.splitlines()[-1])['completed']

        stale = ChatWorkflow(workflow_id, factory, 1)
        try:
            await stale.record_route('react')
        except BusinessError:
            pass
        else:
            raise AssertionError('Terminated Worker attempt retained ownership')
        async with factory() as db:
            view = await read_workflow(db, actor.user_id, workflow_id)
            replay = await read_workflow_events(db, actor.user_id, workflow_id, 0, 50)
        assert view['execution_attempt'] == 2 and view['status'] == 'succeeded' and view['release_id'] == release_id
        assert view['finished_steps'] == [1, 2] and 'plan_checkpoint' not in view
        assert [event['sequence'] for event in replay['events']] == list(range(1, len(replay['events']) + 1))
        assert replay['events'][-1]['replay_content'] == 'isolated recovered answer'
        with engine.connect() as bind:
            assert bind.scalar(sa.select(workflow_runs.c.job_payload).where(workflow_runs.c.id == workflow_id)) is None
            assert bind.scalar(sa.select(workflow_runs.c.plan_checkpoint).where(workflow_runs.c.id == workflow_id)) is None
            assert bind.scalar(sa.select(sa.func.count()).select_from(ChatMessage).where(
                ChatMessage.session_id == session_id, ChatMessage.role == 'assistant',
            )) == 1

    asyncio.run(exercise())

    other_user = str(uuid4())
    with engine.begin() as bind:
        bind.execute(User.__table__.insert().values(uuid=other_user, username='detached-capacity-fixture',
                                                   password='unused-fixture-password', status='active'))
        sources = []
        for number, owner in enumerate((actor.user_id, other_user), start=1):
            session = f'detached-capacity-{number}'
            bind.execute(ChatSession.__table__.insert().values(id=session, user_id=owner))
            message = bind.execute(ChatMessage.__table__.insert().values(session_id=session, role='user', content='fixture'))
            sources.append((owner, ToolTurn(session, message.inserted_primary_key[0])))

    async def enqueue_sources():
        identifiers = set()
        for owner, source in sources:
            token = inference_actor.set({'user_id': owner})
            try:
                async with factory() as db:
                    async with db.begin():
                        await lock_chat_admission(db, owner)
                        identifiers.add(await enqueue_chat(db, owner, source, release_id))
            finally:
                inference_actor.reset(token)
        return identifiers

    pending = asyncio.run(enqueue_sources())
    barrier = Barrier(2)

    def compete(_):
        barrier.wait(timeout=5)
        return asyncio.run(DetachedChatWorker(factory, None).claim())

    with patch.object(detached_worker, 'MAX_ACTIVE_JOBS', 1):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = [job for job in pool.map(compete, range(2)) if job is not None]
        assert len(claimed) == 1
        first = claimed[0].workflow
        assert first.id in pending and first.attempt == 1
        with engine.connect() as bind:
            attempts = bind.execute(sa.select(workflow_runs.c.execution_attempt).where(workflow_runs.c.id.in_(pending)))
            assert sorted(attempts.scalars()) == [0, 1]
        asyncio.run(first.finish('cancelled', 'FIXTURE_CANCELLED'))
        second = asyncio.run(DetachedChatWorker(factory, None).claim())
        assert {first.id, second.workflow.id} == pending and second.workflow.attempt == 1
        asyncio.run(second.workflow.finish('succeeded'))
    report['checks']['workflow_chat_job_mysql_concurrent_workers_share_capacity'] = True

    report['workflow_chat_job_code_sha256'] = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
        for name in ('app/ai_service/detached_worker.py', 'app/ai_service/detached_chat.py',
                     'app/ai_service/workflows.py', 'app/ai_service/workflow_events.py', 'app/routers/chat.py', 'main.py')
    }
    report['checks']['workflow_job_mysql_recovers_after_worker_process_is_killed'] = True
    report['checks']['workflow_plan_mysql_reuses_completed_step_after_worker_process_is_killed'] = True
    report['workflow_plan_code_sha256'] = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
        for name in ('app/ai_service/plan_checkpoints.py', 'app/ai_service/plan_execute_agent.py',
                     'app/ai_service/chat_graph.py', 'app/ai_service/chat_route.py', 'app/models/workflow_run.py')
    }
