"""Read an application checkpoint after the creating process has exited."""

import asyncio
from datetime import datetime
import hashlib
import importlib.util
import json
import subprocess
import sys
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.orm import Session


WORKER_BOOTSTRAP = '''
import asyncio
from contextlib import asynccontextmanager
from importlib.metadata import version
import json
import sys

import pymysql
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.ai_service.tool_operations import ToolTurn
from app.ai_service.workflows import ChatWorkflow
from app.model_config.snapshots import inference_actor

fixture = json.load(sys.stdin)
engine = sa.create_engine('mysql+pymysql://', creator=lambda: pymysql.connect(
    host='127.0.0.1', port=fixture['port'], user='gate_root', password=fixture['password'],
    database='m1_fixture', charset='utf8mb4',
))

class Database:
    def __init__(self, session):
        self.session = session

    def get_bind(self):
        return self.session.get_bind()

    @asynccontextmanager
    async def begin(self):
        with self.session.begin():
            yield

    async def execute(self, statement):
        return self.session.execute(statement)

    async def scalar(self, statement):
        return self.session.scalar(statement)

@asynccontextmanager
async def factory():
    with Session(engine) as session:
        yield Database(session)
'''

WORKER = WORKER_BOOTSTRAP + '''
async def main():
    token = inference_actor.set({'user_id': fixture['user_id']})
    try:
        run = await ChatWorkflow.start(factory, fixture['user_id'], ToolTurn(fixture['session_id'], 2), fixture['release_id'])
        await run.record_route('plan_execute')
        await run.record_step_end(3)
        return run.id
    finally:
        inference_actor.reset(token)
        engine.dispose()

print(json.dumps({'id': asyncio.run(main()), 'langgraph_version': version('langgraph')}))
'''


def run_workflow_gate(workspace, engine, Database, actor, port, password, report):
    from app.ai_service.workflows import read_workflow
    from app.model_config.tables import ai_config_heads, ai_config_releases
    from app.models.chat import ChatMessage, ChatSession
    from app.models.workflow_run import workflow_runs

    path = workspace / 'alembic/versions/e568a91cb2d3_workflow_checkpoints.py'
    spec = importlib.util.spec_from_file_location('workflow_checkpoint_gate', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.context = SimpleNamespace(is_offline_mode=lambda: False)
    report['workflow_migration_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    mode_path = workspace / 'alembic/versions/a780cb3ed4f5_workflow_detached_mode.py'
    mode_spec = importlib.util.spec_from_file_location('workflow_mode_gate', mode_path)
    mode_migration = importlib.util.module_from_spec(mode_spec)
    mode_spec.loader.exec_module(mode_migration)
    mode_migration.context = SimpleNamespace(is_offline_mode=lambda: False)
    report['workflow_detached_mode_migration_sha256'] = hashlib.sha256(mode_path.read_bytes()).hexdigest()
    jobs_path = workspace / 'alembic/versions/b891dc4fe605_workflow_chat_jobs.py'
    jobs_spec = importlib.util.spec_from_file_location('workflow_jobs_gate', jobs_path)
    jobs_migration = importlib.util.module_from_spec(jobs_spec)
    jobs_spec.loader.exec_module(jobs_migration)
    jobs_migration.context = SimpleNamespace(is_offline_mode=lambda: False)
    report['workflow_chat_jobs_migration_sha256'] = hashlib.sha256(jobs_path.read_bytes()).hexdigest()
    plan_path = workspace / 'alembic/versions/c902ed5fa716_workflow_plan_checkpoint.py'
    plan_spec = importlib.util.spec_from_file_location('workflow_plan_gate', plan_path)
    plan_migration = importlib.util.module_from_spec(plan_spec)
    plan_spec.loader.exec_module(plan_migration)
    plan_migration.context = SimpleNamespace(is_offline_mode=lambda: False)
    report['workflow_plan_checkpoint_migration_sha256'] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    report['workflow_code_sha256'] = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
        for name in ('app/models/workflow_run.py', 'app/ai_service/workflows.py',
                     'app/routers/chat.py', 'app/ai_service/chat_graph.py')
    }
    session_id = 'mysql-workflow-fixture'
    with engine.begin() as bind:
        with Operations.context(MigrationContext.configure(bind)):
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
        head = bind.execute(sa.select(ai_config_heads.c.active_release_id)).scalar_one()
        release_id = bind.execute(sa.select(ai_config_releases.c.id).where(
            ai_config_releases.c.id != head).limit(1)).scalar_one()
        bind.execute(ChatSession.__table__.insert().values(id=session_id, user_id=actor.user_id))
        bind.execute(ChatMessage.__table__.insert().values(id=2, session_id=session_id, role='user', content='fixture'))
        bind.execute(ChatMessage.__table__.insert().values(id=3, session_id=session_id, role='user', content='old fixture'))
        bind.exec_driver_sql(
            "INSERT INTO workflow_runs (id, turn_message_id, status, finished_steps, state_version, created_at, updated_at, lease_expires_at) "
            "VALUES ('old-workflow-fixture', 3, 'succeeded', '[]', 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP + INTERVAL 45 SECOND)"
        )
        mode = workflow_runs.c.is_detached
        with Operations.context(MigrationContext.configure(bind)):
            mode_migration.upgrade()
            mode_migration.upgrade()
            assert bind.scalar(sa.select(mode)) is False
            bind.execute(workflow_runs.update().values(status='running', is_detached=True))
            try:
                mode_migration.downgrade()
            except RuntimeError as error:
                assert 'Active detached workflows exist' in str(error)
            else:
                raise AssertionError('Active detached workflow mode was not protected')
            bind.execute(workflow_runs.update().values(status='succeeded'))
            mode_migration.downgrade()
            mode_migration.upgrade()
            assert bind.scalar(sa.select(mode)) is False
            assert bind.scalar(sa.select(workflow_runs.c.state_version)) == 2
            jobs_migration.upgrade()
            jobs_migration.upgrade()
            assert bind.scalar(sa.select(workflow_runs.c.execution_attempt)) is None
            plan_migration.upgrade()
            plan_migration.upgrade()
            assert bind.scalar(sa.select(workflow_runs.c.plan_checkpoint)) is None
            bind.execute(workflow_runs.update().values(status='running', plan_checkpoint={'fixture': True}))
            try:
                plan_migration.downgrade()
            except RuntimeError as error:
                assert 'Active plan checkpoints exist' in str(error)
            else:
                raise AssertionError('Active plan checkpoint was not protected')
            bind.execute(workflow_runs.update().values(status='succeeded', plan_checkpoint=sa.null()))
            plan_migration.downgrade()
            plan_migration.upgrade()
            assert bind.scalar(sa.select(workflow_runs.c.plan_checkpoint)) is None
            bind.execute(workflow_runs.update().values(status='running', execution_attempt=0))
            try:
                jobs_migration.downgrade()
            except RuntimeError as error:
                assert 'Pending chat jobs exist' in str(error)
            else:
                raise AssertionError('Queued chat jobs were not protected')
            bind.execute(workflow_runs.update().values(status='succeeded'))
            jobs_migration.downgrade()
            jobs_migration.upgrade()
            assert bind.scalar(sa.select(workflow_runs.c.execution_attempt)) is None
        bind.execute(ChatMessage.__table__.delete().where(ChatMessage.id == 3))
    report['checks']['workflow_mysql_migration_replay_and_empty_downgrade'] = True
    report['checks']['workflow_mode_mysql_legacy_default_and_active_downgrade_guard'] = True
    report['checks']['workflow_jobs_mysql_legacy_default_and_queued_downgrade_guard'] = True
    report['checks']['workflow_plan_mysql_legacy_default_and_active_downgrade_guard'] = True
    worker_input = {'port': port, 'password': password, 'user_id': actor.user_id,
                    'session_id': session_id, 'release_id': release_id}
    worker = subprocess.run([sys.executable, '-X', 'utf8', '-c', WORKER], cwd=workspace,
                            input=json.dumps(worker_input), encoding='utf8', capture_output=True, timeout=30)
    if worker.returncode:
        # Keep fixture credentials and any driver diagnostics out of report output.
        raise RuntimeError(f'Workflow checkpoint fixture process failed (exit {worker.returncode})')
    result = json.loads(worker.stdout.splitlines()[-1])

    class WorkflowDatabase(Database):
        async def scalar(self, statement):
            return self.session.scalar(statement)

    with Session(engine) as session:
        checkpoint = asyncio.run(read_workflow(WorkflowDatabase(session), actor.user_id, result['id']))
    assert checkpoint['release_id'] == release_id and release_id != head
    assert checkpoint['route'] == 'plan_execute' and checkpoint['finished_steps'] == [3]
    assert checkpoint['state_version'] == 3 and checkpoint['status'] == 'running'
    report['workflow_checkpoint_probe'] = {'langgraph_version': result['langgraph_version'],
                                           'storage': 'mysql', 'writer_process_exited': True}
    report['checks']['workflow_mysql_distinct_process_reads_pinned_checkpoint'] = True

    with engine.begin() as bind:
        with Operations.context(MigrationContext.configure(bind)):
            try:
                migration.downgrade()
            except RuntimeError as error:
                assert 'Workflow checkpoints exist' in str(error)
            else:
                raise AssertionError('Stored workflow checkpoints were not protected')
        bind.execute(workflow_runs.update().values(lease_expires_at=datetime(2000, 1, 1)))
    with Session(engine) as session:
        interrupted = asyncio.run(read_workflow(WorkflowDatabase(session), actor.user_id, result['id']))
        assert interrupted['status'] == 'interrupted' and interrupted['finished_steps'] == [3]
    with engine.begin() as bind:
        bind.execute(ChatMessage.__table__.delete().where(ChatMessage.id == 2))
        assert bind.scalar(sa.select(sa.func.count()).select_from(workflow_runs)) == 0
    report['checks']['workflow_mysql_expired_lease_downgrade_guard_and_turn_cleanup'] = True
