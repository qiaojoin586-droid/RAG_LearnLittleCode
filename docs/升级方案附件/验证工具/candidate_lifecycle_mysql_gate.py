"""Candidate retirement/purge, FK order and concurrent maintenance on MySQL."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_lifecycle_gate(engine,Database,actor,control,report):
    from app.model_config import tables as t
    from app.model_config.lifecycle import CandidateLifecycleWorker
    from app.model_config.security import scoped,issue_proof
    from app.model_config.service import utcnow
    @asynccontextmanager
    async def database():
        with Session(engine) as session:yield Database(session)
    proof=issue_proof(control.keyring,actor,1)['proof']
    with Session(engine) as session:
        head=dict(session.execute(sa.select(t.ai_config_heads)).mappings().one())
        binding=session.execute(sa.select(t.ai_config_releases.c.resolved_static_bindings).where(
            t.ai_config_releases.c.id==head['active_release_id'])).scalar_one()['chat']
        profile=dict(session.execute(sa.select(t.ai_profile_revisions).where(
            t.ai_profile_revisions.c.id==binding['profile_revision_id'])).mappings().one())
        session.rollback()
        draft,_=asyncio.run(control.create_draft(Database(session),actor,{'base_release_id':head['active_release_id'],
            'expected_head_version':head['head_version']},'gc-draft'))
    from app.model_config.contracts import SCHEMA
    fields=SCHEMA['$defs']['ProfileInput']['properties']
    profile={key:profile[key] for key in fields};profile['display_name']='gc fixture'
    with Session(engine) as session:
        draft,_=asyncio.run(control.patch_draft(Database(session),actor,draft['id'],{'operations':[{
            'op':'replace_profile','role_key':'chat','profile':profile,
            'credential':{'action':'replace','api_key':'fictional-gc-key'}}]},'gc-profile',draft['etag'],proof))
    target_profile=draft['payload']['role_bindings_raw']['chat']['profile_revision_id']
    target_secret=draft['payload']['role_bindings_raw']['chat']['credential_revision_id']
    with Session(engine) as session:
        asyncio.run(control.abandon_draft(Database(session),actor,draft['id'],'gc-abandon',draft['etag']))
    collector=CandidateLifecycleWorker(control,database,batch_size=100,purge=True)
    asyncio.run(collector.tick())
    with engine.begin() as connection:
        for table,identifier in ((t.ai_profile_revisions,target_profile),(t.ai_secrets,target_secret)):
            row=connection.execute(sa.select(table).where(table.c.id==identifier)).mappings().one()
            assert row['status']=='retired'
            connection.execute(table.update().where(table.c.id==identifier).values(retired_at=utcnow()-timedelta(days=8)))
    old_audit=control._audit
    async def broken(*args,**kwargs):raise RuntimeError('simulated maintenance audit failure')
    control._audit=broken
    try:
        try:asyncio.run(collector.tick())
        except RuntimeError:pass
        else:raise AssertionError('Maintenance audit failure must roll back')
    finally:control._audit=old_audit
    with engine.connect() as connection:
        assert connection.execute(sa.select(t.ai_profile_revisions.c.id).where(t.ai_profile_revisions.c.id==target_profile)).first()
        assert connection.execute(sa.select(t.ai_secrets.c.id).where(t.ai_secrets.c.id==target_secret)).first()
        assert connection.execute(sa.select(t.ai_config_draft_items.c.entity_id).where(t.ai_config_draft_items.c.entity_id==target_profile)).first()
    report['checks']['lifecycle_mysql_audit_failure_rolls_back_ciphertext_profiles_and_inventory']=True
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks=[pool.submit(lambda:asyncio.run(CandidateLifecycleWorker(control,database,batch_size=100,purge=True).tick())) for _ in range(2)]
        for task in tasks:task.result()
    with engine.connect() as connection:
        for table,identifier in ((t.ai_profile_revisions,target_profile),(t.ai_secrets,target_secret)):
            assert not connection.execute(sa.select(table.c.id).where(table.c.id==identifier)).first()
            assert connection.execute(sa.select(sa.func.count()).select_from(t.ai_config_audits).where(
                t.ai_config_audits.c.action=='candidate.purge',t.ai_config_audits.c.target_key==identifier)).scalar_one()==1
        assert connection.execute(sa.select(t.ai_config_heads.c.head_version)).scalar_one()==head['head_version']
    report['checks']['lifecycle_mysql_retire_seven_day_purge_orders_profile_before_secret_fk']=True
    report['checks']['lifecycle_mysql_concurrent_gc_has_one_purge_audit_and_no_head_move']=True
    # Remove the registry row deliberately: immutable release JSON is a second
    # guard. This is only an isolated corruption fixture, never a business write.
    protected=binding['credential_revision_id']
    with engine.begin() as connection:
        connection.execute(t.ai_config_release_refs.delete().where(scoped(t.ai_config_release_refs),
            t.ai_config_release_refs.c.entity_kind=='credential',t.ai_config_release_refs.c.entity_id==protected))
        connection.execute(t.ai_secrets.update().where(t.ai_secrets.c.id==protected).values(retired_at=utcnow()-timedelta(days=8)))
    asyncio.run(CandidateLifecycleWorker(control,database,batch_size=100,purge=True).tick())
    with engine.connect() as connection:
        assert connection.execute(sa.select(t.ai_secrets.c.id).where(t.ai_secrets.c.id==protected)).first()
    report['checks']['lifecycle_mysql_immutable_manifest_protects_missing_reference_registry_entry']=True
