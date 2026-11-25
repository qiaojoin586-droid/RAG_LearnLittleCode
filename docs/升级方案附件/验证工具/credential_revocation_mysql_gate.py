"""Disposable MySQL locking/transaction checks for credential revocation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import copy
import threading

import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_credential_gate(engine,Database,actor,control,report):
    from app.core.failed_response import BusinessError
    from app.model_config import tables as t
    from app.model_config.contracts import SCHEMA
    from app.model_config.credential_service import CredentialService
    from app.model_config.outbox import ConfigOutboxWorker
    from app.model_config.publishing import PublishingService
    from app.model_config.runtime import permit
    from app.model_config.security import issue_proof
    from app.model_config.snapshots import load_snapshot,inference_actor
    @asynccontextmanager
    async def database():
        with Session(engine) as session:yield Database(session)
    proof=issue_proof(control.keyring,actor,1)['proof']
    with Session(engine) as session:
        policy,_=asyncio.run(control.create_policy(Database(session),actor,{'policy_key':'revocable-cloud',
            'origin':'https://models.example.com','allowed_path_prefix':'/v1','address_mode':'public_https',
            'connection_rules':{},'environment':'development'},'revoke-policy',proof))
    profile={'display_name':'revocable','model_kind':'chat','adapter_type':'openai_compatible','api_protocol':'chat_completions',
        'vendor':'custom','base_url':policy['origin']+'/v1','model':'fixture-model','model_revision':None,'auth_mode':'bearer',
        'endpoint_policy_revision_id':policy['id'],'context_window':None,'tokenizer_id':None,'tokenizer_revision':None,
        'token_counting_policy':{},'max_output_tokens':96,'connect_timeout_ms':1000,'request_timeout_ms':3000,
        'embedding_dimension':None,'parameters':{}}
    def new_draft(key):
        with Session(engine) as session:
            head=session.execute(sa.select(t.ai_config_heads)).mappings().one();session.rollback()
            return asyncio.run(control.create_draft(Database(session),actor,{'base_release_id':head['active_release_id'],
                'expected_head_version':head['head_version']},key))[0]
    draft=new_draft('revocable-draft')
    operations=[]
    for role,binding in draft['payload']['role_bindings_raw'].items():
        if binding['enabled']:
            disabled=copy.deepcopy(binding);disabled.update(enabled=False,failure_mode='disabled')
            operations.append({'op':'replace_binding','role_key':role,'binding':disabled})
    operations.append({'op':'replace_profile','role_key':'chat','profile':profile,
        'credential':{'action':'replace','api_key':'fictional-mysql-revocable-key'}})
    with Session(engine) as session:
        current,_=asyncio.run(control.patch_draft(Database(session),actor,draft['id'],{'operations':operations},
            'revocable-profile',draft['etag'],proof))
    def publish(value,key):
        with Session(engine) as session:return asyncio.run(PublishingService(control,database).publish(Database(session),actor,value['id'],{
            'expected_draft_version':value['draft_version'],'expected_head_version':value['base_head_version']},key,proof,value['etag']))
    published,_=publish(current,'revocable-publish')
    with Session(engine) as session:pinned=asyncio.run(load_snapshot(Database(session)))
    profile_id=current['payload']['role_bindings_raw']['chat']['profile_revision_id']
    credential_id=current['payload']['role_bindings_raw']['chat']['credential_revision_id']
    entered=threading.Event();resume=threading.Event()
    class Paused(Database):
        async def execute(self,statement):
            if getattr(statement,'is_select',False) and t.ai_secrets in statement.get_final_froms():
                entered.set()
                if not resume.wait(5):raise RuntimeError('Credential locking gate timed out')
            return await super().execute(statement)
    @asynccontextmanager
    async def paused_database():
        with Session(engine) as session:yield Paused(session)
    def old_snapshot_permit():
        async def run():
            token=inference_actor.set({'user_id':actor.user_id,'jti':None,'expires':None})
            try:
                await permit(pinned,profile_id,paused_database)
                return None
            except BusinessError as error:return error.code
            finally:inference_actor.reset(token)
        return asyncio.run(run())
    def revoke(target,key='mysql-revoke',reason='suspected_leak'):
        with Session(engine) as session:return asyncio.run(CredentialService(control).revoke(Database(session),actor,target,{'reason':reason},key,proof))
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=pool.submit(old_snapshot_permit)
        try:
            assert entered.wait(5)
            first=revoke(credential_id)
        finally:resume.set()
        assert pending.result(timeout=5)==40926
    report['checks']['credential_mysql_current_read_blocks_stale_repeatable_read_permit']=True
    with ThreadPoolExecutor(max_workers=2) as pool:replays=list(pool.map(lambda _:revoke(credential_id),[1,2]))
    assert all(value==first for value in replays)
    with engine.connect() as connection:
        secret=connection.execute(sa.select(t.ai_secrets).where(t.ai_secrets.c.id==credential_id)).mappings().one()
        assert secret['status']=='revoked' and secret['revocation_epoch']==1
        assert connection.execute(sa.select(sa.func.count()).select_from(t.outbox_events).where(
            t.outbox_events.c.event_type=='credential.revoked',t.outbox_events.c.aggregate_id==credential_id)).scalar_one()==1
    report['checks']['credential_mysql_concurrent_replay_has_one_epoch_and_outbox_event']=True
    newer,_=revoke(credential_id,'mysql-revoke-again','freeform-must-not-persist')
    assert newer['already_revoked'] and newer['revocation_epoch']==1
    with engine.connect() as connection:
        requests=connection.execute(sa.select(t.ai_config_requests).where(t.ai_config_requests.c.action=='credential.revoke')).mappings().all()
        audits=connection.execute(sa.select(t.ai_config_audits).where(t.ai_config_audits.c.action=='credential.revoke')).mappings().all()
        assert 'freeform-must-not-persist' not in str(requests)+str(audits)
        assert 'fictional-mysql-revocable-key' not in str(requests)+str(audits)
        assert connection.execute(sa.select(t.ai_config_heads.c.head_version)).scalar_one()==published['head_version']
    report['checks']['credential_mysql_revoke_has_no_plaintext_or_head_change']=True
    for index in range(2):
        repair=new_draft('repair-race-'+str(index))
        assert 'CREDENTIAL_REVOKED' in repair['publication_blockers']
        with Session(engine) as session:
            repaired,_=asyncio.run(control.patch_draft(Database(session),actor,repair['id'],{'operations':[{
                'op':'replace_profile','role_key':'chat','profile':profile,
                'credential':{'action':'replace','api_key':'fictional-race-key-'+str(index)}}]},'repair-race-profile-'+str(index),repair['etag'],proof))
        new_id=repaired['payload']['role_bindings_raw']['chat']['credential_revision_id']
        def racing_publish():
            try:return publish(repaired,'repair-race-publish-'+str(index))[1]
            except BusinessError as error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outgoing=pool.submit(racing_publish);revoking=pool.submit(revoke,new_id,'repair-race-revoke-'+str(index))
            status=outgoing.result();revoking.result()
        assert status in (200,40926),status
        with engine.connect() as connection:
            assert connection.execute(sa.select(t.ai_secrets.c.status).where(t.ai_secrets.c.id==new_id)).scalar_one()=='revoked'
        # If publication lost, the active release still holds the earlier revoked
        # credential; if it won, the new active credential has now been revoked.
    report['checks']['credential_mysql_revoke_publish_race_cannot_reactivate_secret']=True
    worker=ConfigOutboxWorker(control,database)
    for _ in range(30):
        if not asyncio.run(worker.tick()):break
    with engine.connect() as connection:
        rows=connection.execute(sa.select(t.outbox_events.c.status).where(t.outbox_events.c.event_type=='credential.revoked')).scalars().all()
        assert rows and all(status=='succeeded' for status in rows)
    report['checks']['credential_mysql_outbox_delivery_rechecks_committed_revocation']=True
