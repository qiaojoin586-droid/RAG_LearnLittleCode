"""Actual memory Alembic and owner-row lock checks on the disposable server only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
from types import SimpleNamespace
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_memory_gate(workspace,engine,Database,actor,keyring,report):
    from app.core.failed_response import BusinessError
    from app.models.user import User
    from app.user_memory import tables as t
    from app.user_memory.service import MemoryService,etag
    path=workspace/'alembic/versions/b235d6e7f8a9_personal_memory.py'
    spec=importlib.util.spec_from_file_location('personal_memory_migration_gate',path)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    migration.context=SimpleNamespace(is_offline_mode=lambda:False)
    report['memory_migration_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    report['memory_code_sha256']={name:hashlib.sha256((workspace/'app/user_memory'/name).read_bytes()).hexdigest()
        for name in ('tables.py','service.py','jobs.py','router.py')}
    with engine.begin() as bind:
        migration.op=Operations(MigrationContext.configure(bind))
        migration.upgrade();migration.upgrade()
        for name in migration.NAMES:
            inspector=sa.inspect(bind)
            expected={constraint.name for constraint in t.metadata.tables[name].foreign_key_constraints}
            actual={item['name'] for item in inspector.get_foreign_keys(name)}
            assert expected==actual,(name,expected,actual)
            assert inspector.get_table_options(name)['mysql_collate']=='utf8mb4_0900_ai_ci'
        assert set(migration.SOURCE_FIELDS)<={item['name'] for item in sa.inspect(bind).get_columns('user_memory_heads')}
        migration.downgrade();migration.upgrade()
    report['checks']['memory_actual_migration_up_repeat_down_up_owner_fks_and_collation']=True
    memory=MemoryService(keyring)
    with Session(engine) as session:initial=asyncio.run(memory.snapshot(Database(session),actor.user_id))
    assert initial['head_version']==1
    def save(index):
        try:
            with Session(engine) as session:
                return asyncio.run(memory.save(Database(session),actor.user_id,{'content_md':'偏好 '+str(index)},
                    'parallel-'+str(index),etag(1)))['head_version']
        except BusinessError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:versions=list(pool.map(save,[1,2]))
    assert sorted(versions)==[2,40940],versions
    with engine.connect() as bind:
        assert bind.execute(sa.select(sa.func.count()).select_from(t.user_memory_revisions)).scalar_one()==2
    report['checks']['memory_mysql_concurrent_cas_loser_leaves_no_revision']=True
    other=str(uuid.uuid4())
    with engine.begin() as bind:bind.execute(User.__table__.insert().values(uuid=other,username='memory-other',password='unused',status='active'))
    with Session(engine) as session:asyncio.run(memory.snapshot(Database(session),other))
    with engine.begin() as bind:
        try:
            with bind.begin_nested():
                bind.execute(t.user_memory_heads.update().where(t.user_memory_heads.c.user_id==other).values(active_revision_id=initial['revision_id']))
        except sa.exc.IntegrityError:pass
        else:raise AssertionError('Composite owner FK accepted another user revision')
    report['checks']['memory_mysql_composite_fk_rejects_cross_owner_head']=True
    with Session(engine) as session:
        cleared=asyncio.run(memory.clear(Database(session),actor.user_id,{'purge_history':True},'purge',etag(2)))
    assert cleared['revision_number']==3 and cleared['content_md']=='' and not cleared['settings']['enabled']
    with engine.begin() as bind:
        rows=bind.execute(sa.select(t.user_memory_revisions).where(t.user_memory_revisions.c.user_id==actor.user_id)).mappings().all()
        assert len(rows)==1 and rows[0]['source_kind']=='clear'
        migration.op=Operations(MigrationContext.configure(bind))
        try:migration.downgrade()
        except RuntimeError as error:assert 'Personal memory exists' in str(error)
        else:raise AssertionError('Populated memory downgrade must refuse before DDL')
        assert all(sa.inspect(bind).has_table(name) for name in migration.NAMES)
    report['checks']['memory_mysql_purge_fk_order_and_monotone_counter']=True
    report['checks']['memory_populated_downgrade_refuses_before_ddl']=True
