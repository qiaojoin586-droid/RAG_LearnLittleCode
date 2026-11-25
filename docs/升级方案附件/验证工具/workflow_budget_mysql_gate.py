"""Crash a budgeted Worker after reservation and verify independent MySQL recovery."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import importlib.util
import json
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.orm import Session

from workflow_mysql_gate import WORKER_BOOTSTRAP


WORKER = WORKER_BOOTSTRAP + '''
import httpx

from app.ai_service.detached_chat import find_detached_chat
from app.ai_service.detached_worker import DetachedChatWorker
from app.model_config.durable_task_budget import DurableTaskModelBudget
from app.model_config.provider import ProviderGateway
from app.model_config.task_budget import current_model_budget

PROFILE = {'api_protocol': 'ollama_embed', 'adapter_type': 'ollama_native', 'auth_mode': 'none',
           'base_url': 'http://localhost:11434', 'model': 'fixture-vector', 'request_timeout_ms': 30000,
           'connect_timeout_ms': 100, 'embedding_dimension': 1, 'context_window': 10}

async def produce(job, user_id, session_id):
    assert isinstance(current_model_budget.get(), DurableTaskModelBudget)
    assert job.workflow.attempt == fixture['attempt']
    await job.budget.reserve_tool()

    async def reply(request):
        if job.workflow.attempt == 1:
            await job.workflow.record_route('react')
            await asyncio.Future()
        return httpx.Response(200, json={'embeddings': [[1]], 'prompt_eval_count': 3})

    async with ProviderGateway(PROFILE, {}, None, permit=None, transport=httpx.MockTransport(reply), probe_mode=False) as gateway:
        await gateway.embed(['x'])
    async with factory() as db:
        async with db.begin():
            await job.workflow.succeed_in(db)
    yield 'complete'

async def main():
    worker = DetachedChatWorker(factory, produce)
    assert await worker.tick()
    run = find_detached_chat(fixture['workflow_id'], fixture['user_id'])
    await asyncio.wait_for(run.task, 22)
    engine.dispose()

asyncio.run(main())
print(json.dumps({'completed': True}))
'''


def run_workflow_budget_gate(workspace, engine, Database, actor, port, password, report):
    from app.ai_service.detached_worker import DetachedChatWorker, enqueue_chat, lock_chat_admission
    from app.ai_service.tool_operations import ToolTurn
    from app.ai_service.workflows import read_workflow
    from app.core.model_trace import price_time
    from app.model_config.durable_task_budget import DurableTaskModelBudget
    from app.model_config.snapshots import inference_actor
    from app.model_config.tables import ai_config_heads
    from app.model_config.task_budget import ModelBudgetDenied, TaskModelBudget, current_model_budget
    from app.models.chat import ChatMessage, ChatSession
    from app.models.workflow_model_budget import workflow_model_budgets as budgets, workflow_model_reservations as reservations
    from app.models.workflow_run import workflow_runs

    path = workspace / 'alembic/versions/f235b08cda49_workflow_model_budgets.py'
    spec = importlib.util.spec_from_file_location('workflow_budget_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.context = SimpleNamespace(is_offline_mode=lambda: False)
    report['workflow_budget_migration_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    tool_path = workspace / 'alembic/versions/d76487ba19e3_workflow_tool_call_budget.py'
    tool_spec = importlib.util.spec_from_file_location('workflow_tool_budget_migration', tool_path)
    tool_migration = importlib.util.module_from_spec(tool_spec)
    tool_spec.loader.exec_module(tool_migration)
    tool_migration.context = SimpleNamespace(is_offline_mode=lambda: False)
    report['workflow_tool_budget_migration_sha256'] = hashlib.sha256(tool_path.read_bytes()).hexdigest()
    report['workflow_budget_code_sha256'] = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
        for name in ('app/model_config/durable_task_budget.py', 'app/model_config/task_budget.py',
                     'app/ai_service/detached_worker.py', 'app/models/workflow_model_budget.py',
                     'app/model_config/provider.py', 'app/model_config/runtime_model.py', 'app/ai_service/agent.py')
    }

    class WorkflowDatabase(Database):
        async def scalar(self, statement):
            return self.session.scalar(statement)

    @asynccontextmanager
    async def factory():
        with Session(engine) as session:
            yield WorkflowDatabase(session)

    session_id = 'workflow-budget-session'
    with engine.begin() as bind:
        with Operations.context(MigrationContext.configure(bind)):
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
            tool_migration.upgrade()
            tool_migration.upgrade()
            tool_migration.downgrade()
            tool_migration.upgrade()
        release_id = bind.execute(sa.select(ai_config_heads.c.active_release_id)).scalar_one()
        bind.execute(ChatSession.__table__.insert().values(id=session_id, user_id=actor.user_id))

    async def enqueue(policy):
        actor_token = inference_actor.set({'user_id': actor.user_id})
        budget_token = current_model_budget.set(policy)
        try:
            async with factory() as db:
                async with db.begin():
                    await lock_chat_admission(db, actor.user_id)
                    result = await db.execute(ChatMessage.__table__.insert().values(session_id=session_id, role='user',
                                                                                   content='isolated budget fixture'))
                    return await enqueue_chat(db, actor.user_id, ToolTurn(session_id, result.inserted_primary_key[0]), release_id)
        finally:
            current_model_budget.reset(budget_token)
            inference_actor.reset(actor_token)

    async def exercise_restart():
        workflow_id = await enqueue(TaskModelBudget(2, 25, {}, max_tool_calls=2))
        worker_input = {'port': port, 'password': password, 'workflow_id': workflow_id,
                        'user_id': actor.user_id, 'attempt': 1}
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
                            raise RuntimeError('Budget Worker stopped before outbound admission')
                        async with factory() as db:
                            view = await read_workflow(db, actor.user_id, workflow_id)
                        if view['route'] == 'react':
                            break
                        await asyncio.sleep(0.05)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
        with engine.begin() as bind:
            assert bind.scalar(sa.select(budgets.c.tokens).where(budgets.c.workflow_id == workflow_id)) == 10
            assert bind.scalar(sa.select(budgets.c.tool_calls).where(budgets.c.workflow_id == workflow_id)) == 1
            bind.execute(workflow_runs.update().where(workflow_runs.c.id == workflow_id)
                         .values(lease_expires_at=datetime(2000, 1, 1)))
        worker_input['attempt'] = 2
        second = subprocess.run(command, cwd=workspace, input=json.dumps(worker_input), encoding='utf8',
                                capture_output=True, timeout=25)
        if second.returncode:
            raise RuntimeError(f'Resumed budget Worker exited with status {second.returncode}')
        assert json.loads(second.stdout.splitlines()[-1])['completed']
        async with factory() as db:
            view = await read_workflow(db, actor.user_id, workflow_id)
        assert view['status'] == 'succeeded' and view['execution_attempt'] == 2
        with engine.connect() as bind:
            ledger = bind.execute(sa.select(budgets).where(budgets.c.workflow_id == workflow_id)).mappings().one()
            assert ledger['tokens'] == 13 and ledger['calls'] == 2 and ledger['tool_calls'] == 2
            attempts = dict(bind.execute(sa.select(reservations.c.execution_attempt, reservations.c.status)
                            .where(reservations.c.workflow_id == workflow_id)).all())
            assert attempts == {1: 'reserved', 2: 'settled'}

    asyncio.run(exercise_restart())
    trial_id = asyncio.run(enqueue(TaskModelBudget(None, 15, {})))
    job = asyncio.run(DetachedChatWorker(factory, None).claim())
    barrier = Barrier(2, timeout=5)

    def reserve():
        ledger = DurableTaskModelBudget(job.workflow, {'stopped_reason': None})
        barrier.wait()
        try:
            asyncio.run(ledger.reserve({'context_window': 10}, price_time(), factory, output_limit=0, input_count=1))
        except ModelBudgetDenied as error:
            return error.data['reason']
        return 'reserved'

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(reserve) for _ in range(2)]
        assert {item.result(timeout=15) for item in outcomes} == {'reserved', 'MODEL_TOKEN_LIMIT_EXCEEDED'}
    asyncio.run(job.workflow.finish('failed', 'MODEL_TOKEN_LIMIT_EXCEEDED'))

    tool_job_id = asyncio.run(enqueue(TaskModelBudget(None, None, {}, max_tool_calls=1)))
    first_tool_job = asyncio.run(DetachedChatWorker(factory, None).claim())
    asyncio.run(first_tool_job.workflow.relinquish())
    tool_job = asyncio.run(DetachedChatWorker(factory, None).claim())
    assert tool_job.workflow.id == tool_job_id and tool_job.workflow.attempt == 2
    with engine.connect() as bind:
        tool_config = bind.execute(sa.select(budgets).where(budgets.c.workflow_id == tool_job_id)).mappings().one()

    def reserve_tool():
        ledger = DurableTaskModelBudget(tool_job.workflow, tool_config)
        barrier.wait()
        try:
            asyncio.run(ledger.reserve_tool())
        except ModelBudgetDenied as error:
            return error.data['reason']
        return 'reserved'

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(reserve_tool) for _ in range(2)]
        assert {item.result(timeout=15) for item in outcomes} == {'reserved', 'TOOL_CALL_LIMIT_EXCEEDED'}
    asyncio.run(tool_job.workflow.relinquish())
    assert asyncio.run(DetachedChatWorker(factory, None).claim()) is None
    with engine.begin() as bind:
        ledger = bind.execute(sa.select(budgets).where(budgets.c.workflow_id == trial_id)).mappings().one()
        assert ledger['tokens'] == 10 and ledger['calls'] == 1
        assert bind.scalar(sa.select(budgets.c.tool_calls).where(budgets.c.workflow_id == tool_job_id)) == 1
        assert bind.scalar(sa.select(workflow_runs.c.error_code).where(workflow_runs.c.id == tool_job_id)) == 'TOOL_CALL_LIMIT_EXCEEDED'
        with Operations.context(MigrationContext.configure(bind)):
            try:
                tool_migration.downgrade()
            except RuntimeError as error:
                assert 'Workflow tool budgets exist' in str(error)
            else:
                raise AssertionError('Populated tool budget ledger was not protected')
            try:
                migration.downgrade()
            except RuntimeError as error:
                assert 'Workflow model budget receipts exist' in str(error)
            else:
                raise AssertionError('Populated budget ledger was not protected')
        bind.execute(ChatSession.__table__.delete().where(ChatSession.id == session_id))
        assert bind.scalar(sa.select(sa.func.count()).select_from(budgets)) == 0
        assert bind.scalar(sa.select(sa.func.count()).select_from(reservations)) == 0
    report['checks']['workflow_budget_mysql_original_limits_and_reservations_survive_worker_crash'] = True
    report['checks']['workflow_budget_mysql_parallel_reservations_are_atomic'] = True
    report['checks']['workflow_tool_budget_mysql_survives_worker_crash'] = True
    report['checks']['workflow_tool_budget_mysql_parallel_reservations_are_atomic'] = True
    report['checks']['workflow_budget_mysql_migration_retry_guarded_downgrade_and_cascade'] = True
