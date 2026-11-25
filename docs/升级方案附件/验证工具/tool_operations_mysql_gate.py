"""Validate transactional tool receipts on the disposable MySQL server."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
import importlib.util
from threading import Barrier
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_tool_operations_gate(workspace, engine, Database, actor, report):
    from app.ai_service.tool_operations import ToolTurn, run_tool_write, tool_turn
    from app.model_config.snapshots import inference_actor
    from app.models.chat import ChatMessage, ChatSession
    from app.models.tool_operation import tool_operations

    path = workspace / 'alembic/versions/d457f809b1c2_transactional_tool_operations.py'
    spec = importlib.util.spec_from_file_location('transactional_tool_operations_gate', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.context = SimpleNamespace(is_offline_mode=lambda: False)
    report['tool_operation_migration_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    report['tool_operation_code_sha256'] = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
        for name in ('app/ai_service/tool_operations.py', 'app/models/tool_operation.py',
                     'app/ai_service/agent_tools.py', 'app/services/note_service.py')
    }
    table = sa.Table('m2_effect_fixture', sa.MetaData(), sa.Column('id', sa.Integer, primary_key=True))
    session_id = 'tool-operation-mysql-session'
    with engine.begin() as bind:
        for fixture in (ChatSession.__table__, ChatMessage.__table__, table):
            fixture.create(bind)
        bind.execute(ChatSession.__table__.insert().values(id=session_id, user_id=actor.user_id))
        bind.execute(ChatMessage.__table__.insert().values(id=1, session_id=session_id, role='user', content='fixture'))
        with Operations.context(MigrationContext.configure(bind)):
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
    report['checks']['tool_operation_mysql_migration_replay_and_empty_downgrade'] = True

    class ToolDatabase(Database):
        async def scalar(self, statement):
            return self.session.scalar(statement)

    @asynccontextmanager
    async def factory():
        with Session(engine, expire_on_commit=False) as session:
            yield ToolDatabase(session)

    async def write(db, _after_commit):
        result = await db.execute(table.insert().values())
        key = result.inserted_primary_key[0]
        return str(key), {'effect_id': key}

    async def read(db, ref):
        result = await db.scalar(sa.select(table.c.id).where(table.c.id == ref['effect_id']))
        return str(result)

    def invoke(write_action, arguments):
        actor_token = inference_actor.set({'user_id': actor.user_id, 'expires': None, 'jti': None})
        turn_token = tool_turn.set(ToolTurn(session_id, 1))
        try:
            return asyncio.run(run_tool_write(factory, actor.user_id, 'fixture_tool', arguments, write_action, read))
        finally:
            tool_turn.reset(turn_token)
            inference_actor.reset(actor_token)

    barrier = Barrier(2, timeout=5)

    def simultaneous():
        barrier.wait()
        return invoke(write, {'text': 'identical fixture'})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result(timeout=20) for future in [pool.submit(simultaneous), pool.submit(simultaneous)]]
    assert first[0] == second[0]
    assert first[1]['operation_id'] == second[1]['operation_id']
    assert {first[1]['replayed'], second[1]['replayed']} == {True, False}

    async def interrupted(db, after_commit):
        await write(db, after_commit)
        raise RuntimeError('interrupted test transaction')

    try:
        invoke(interrupted, {'text': 'rolled back fixture'})
    except RuntimeError as error:
        assert str(error) == 'interrupted test transaction'
    else:
        raise AssertionError('Interrupted transaction reported success')
    with engine.begin() as bind:
        assert bind.scalar(sa.select(sa.func.count()).select_from(tool_operations)) == 1
        assert bind.scalar(sa.select(sa.func.count()).select_from(table)) == 1
        with Operations.context(MigrationContext.configure(bind)):
            try:
                migration.downgrade()
            except RuntimeError as error:
                assert 'Tool receipts exist' in str(error)
            else:
                raise AssertionError('Populated operation receipts were not protected')
        bind.execute(ChatMessage.__table__.delete().where(ChatMessage.id == 1))
        assert bind.scalar(sa.select(sa.func.count()).select_from(tool_operations)) == 0
    report['checks']['tool_operations_mysql_concurrent_replay_and_interrupted_write_atomic'] = True
    report['checks']['tool_operation_mysql_populated_downgrade_and_turn_delete'] = True
