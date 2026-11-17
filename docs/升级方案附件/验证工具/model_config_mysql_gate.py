"""Called only by the self-owned disposable MySQL harness; no .env or app DB."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import importlib.util
from types import SimpleNamespace
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet
import pymysql
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool


def run_gate(workspace,port,password,report):
    import sys
    if str(workspace) not in sys.path:
        sys.path.insert(0,str(workspace))
    from app.core.failed_response import BusinessError
    from app.model_config import tables as t
    from app.model_config.crypto import Keyring
    from app.model_config.security import Principal
    from app.model_config.service import ConfigService,bootstrap_admin,utcnow
    from app.models.user import User

    def connection(database=None):
        return pymysql.connect(host='127.0.0.1',port=port,user='gate_root',password=password,
                               database=database,charset='utf8mb4')
    with connection() as root:
        with root.cursor() as cursor:
            cursor.execute('CREATE DATABASE m1_fixture CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci')
        root.commit()
    engine = sa.create_engine('mysql+pymysql://',creator=lambda:connection('m1_fixture'),poolclass=NullPool)
    class Database:
        def __init__(self,session):
            self.session = session
        def get_bind(self):
            return self.session.get_bind()
        async def execute(self,query):
            return self.session.execute(query)
        async def rollback(self):
            self.session.rollback()
        @asynccontextmanager
        async def begin(self):
            with self.session.begin():
                yield self
    path = workspace/'alembic/versions/a124c5d6e7f8_model_config_control_plane.py'
    spec = importlib.util.spec_from_file_location('model_config_migration_gate',path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    report['config_migration_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    storage_path=workspace/'alembic/versions/c346e7f8a9b0_index_storage_binding.py'
    storage_spec=importlib.util.spec_from_file_location('index_storage_migration_gate',storage_path)
    storage_migration=importlib.util.module_from_spec(storage_spec);storage_spec.loader.exec_module(storage_migration)
    storage_migration.context=SimpleNamespace(is_offline_mode=lambda:False)
    report['index_storage_migration_sha256']=hashlib.sha256(storage_path.read_bytes()).hexdigest()
    report['config_code_sha256'] = {name:hashlib.sha256((workspace/'app/model_config'/name).read_bytes()).hexdigest()
        for name in ('tables.py','contracts.schema.json','crypto.py','security.py','service.py','resolution.py','rebase.py','legacy_import.py',
                     'index_inventory.py','embedding_status.py','embedding_space.py','cli.py','index_binding.py','index_attestation.py','vector_runtime.py',
                     'credential_calls.py','credential_service.py','outbox_service.py','lifecycle.py','key_rotation.py','pricing.py','task_budget.py',
                     'context_budget.py',
                     'provider.py','probes.py','transport.py','validation_service.py','publishing.py','snapshots.py',
                     'runtime.py','runtime_model.py','prompt_contracts.py','outbox.py')}
    report['fixture_scope'] += '; M1 migration and service on isolated MySQL, not full legacy migration chain'
    try:
        with engine.begin() as bind:
            User.__table__.create(bind)
            bind.exec_driver_sql('CREATE TABLE model_traces (id BIGINT PRIMARY KEY, request_id VARCHAR(32))')
            bind.exec_driver_sql("INSERT INTO model_traces VALUES (1,'legacy-trace')")
            migration.context = SimpleNamespace(is_offline_mode=lambda:False)
            migration.op = Operations(MigrationContext.configure(bind))
            migration.upgrade()
            migration.upgrade()
            assert bind.exec_driver_sql('SELECT request_id,pricing_status,is_test FROM model_traces').one()==('legacy-trace','legacy_estimate',0)
            for table in t.CONFIG_TABLES:
                inspector = sa.inspect(bind)
                expected = {constraint.name for constraint in table.foreign_key_constraints}
                actual = {item['name'] for item in inspector.get_foreign_keys(table.name)}
                assert expected==actual,(table.name,expected,actual)
                assert inspector.get_table_options(table.name)['mysql_collate']=='utf8mb4_0900_ai_ci'
            report['checks']['config_migration_18_tables_all_scope_fks_and_legacy_collation'] = True
            migration.downgrade()
            assert bind.exec_driver_sql('SELECT request_id FROM model_traces').scalar_one()=='legacy-trace'
            migration.upgrade()
            report['checks']['config_migration_up_repeat_down_up_preserves_legacy_trace'] = True
            storage_migration.op=Operations(MigrationContext.configure(bind))
            storage_migration.upgrade();storage_migration.upgrade();storage_migration.downgrade();storage_migration.upgrade()
            assert bind.exec_driver_sql('SELECT index_context FROM model_traces').scalar_one() is None
            assert 'storage_binding' in {item['name'] for item in sa.inspect(bind).get_columns('rag_index_versions')}
            report['checks']['index_storage_migration_up_repeat_down_up_preserves_legacy_trace']=True
        actor = Principal(str(uuid.uuid4()),'isolated-access',utcnow()+timedelta(hours=1))
        with engine.begin() as bind:
            bind.execute(User.__table__.insert().values(uuid=actor.user_id,username='isolated-actor',password='unused',status='active'))
        with Session(engine) as session:
            asyncio.run(bootstrap_admin(Database(session),actor.user_id))
        service = ConfigService(Keyring('test-only',{'test-only':Fernet.generate_key()}),owner_id='mysql-test-manager')
        def create(key):
            with Session(engine) as session:
                return asyncio.run(service.create_draft(Database(session),actor,
                    {'base_release_id':None,'expected_head_version':0},key))
        # Unique request + authorization-row lock must converge to one draft.
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create,['same-key','same-key']))
        assert results[0]==results[1]
        draft = results[0][0]
        with engine.connect() as bind:
            assert bind.execute(sa.select(sa.func.count()).select_from(t.ai_config_drafts)).scalar_one()==1
            assert bind.execute(sa.select(sa.func.count()).select_from(t.ai_config_requests)).scalar_one()==1
        report['checks']['config_mysql_concurrent_idempotent_create_commits_one_result'] = True
        def patch(index):
            body = {'operations':[{'op':'replace_prompt','role_key':'chat',
                    'prompt':{'system_text':'version-'+str(index),'template_text':'question'}}]}
            try:
                with Session(engine) as session:
                    result = asyncio.run(service.patch_draft(Database(session),actor,draft['id'],body,
                        'patch-'+str(index),draft['etag']))
                    return result[0]['draft_version']
            except BusinessError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            versions = list(pool.map(patch,[1,2]))
        assert sorted(versions)==[2,40921],versions
        with engine.connect() as bind:
            assert bind.execute(sa.select(sa.func.count()).select_from(t.ai_prompt_revisions)).scalar_one()==1
            assert bind.execute(sa.select(t.ai_version_counters.c.last_num)).scalar_one()==1
        report['checks']['config_mysql_concurrent_patch_cas_loser_leaves_no_version'] = True
        # Exercise the real durable probe transactions with an isolated HTTP
        # fixture. No provider, socket to an untrusted address, or paid call.
        import httpx
        from app.model_config.provider import ProviderGateway
        from app.model_config.security import issue_proof
        from app.model_config.validation_service import ValidationService
        proof = issue_proof(service.keyring,actor,1)['proof']
        with Session(engine) as session:
            policy,_ = asyncio.run(service.create_policy(Database(session),actor,{
                'policy_key':'local-fixture','origin':'http://127.0.0.1:11434','allowed_path_prefix':'/',
                'address_mode':'trusted_private','connection_rules':{'allowed_cidrs':['127.0.0.1/32']},
                'environment':'development'},'endpoint-fixture',proof))
        with Session(engine) as session:
            current = asyncio.run(service.get_draft(Database(session),actor,draft['id']))
        candidate = {'display_name':'isolated','model_kind':'chat','adapter_type':'ollama_native',
            'api_protocol':'ollama_chat','vendor':'ollama','base_url':policy['origin'],'model':'fixture-model',
            'model_revision':None,'auth_mode':'none','endpoint_policy_revision_id':policy['id'],
            'context_window':None,'tokenizer_id':None,'tokenizer_revision':None,'token_counting_policy':{},
            'max_output_tokens':96,'connect_timeout_ms':1000,'request_timeout_ms':3000,
            'embedding_dimension':None,'parameters':{}}
        with Session(engine) as session:
            current,_ = asyncio.run(service.patch_draft(Database(session),actor,draft['id'],{
                'operations':[{'op':'replace_profile','role_key':'session_title','profile':candidate,
                               'credential':{'action':'keep'}}]},'probe-profile',current['etag']))
        @asynccontextmanager
        async def database():
            with Session(engine) as session:
                yield Database(session)
        calls=[]
        def gateway(profile,policy,key,*,permit):
            async def reply(request):
                await permit();calls.append(request.url.path)
                return httpx.Response(200,text='{"message":{"content":"OK"},"done":false}\n{"message":{},"done":true}\n')
            return ProviderGateway(profile,policy,key,permit=permit,transport=httpx.MockTransport(reply))
        validation = ValidationService(service,database,gateway_factory=gateway)
        async def run_probe():
            with Session(engine) as session:
                return await validation.test_draft(Database(session),actor,draft['id'],{
                    'role_key':'session_title','expected_draft_version':current['draft_version']},
                    'probe-fixture',current['etag'])
        first=asyncio.run(run_probe());second=asyncio.run(run_probe())
        assert first==second and first[0]['status']=='passed' and len(calls)==1,(first,second,len(calls))
        with engine.connect() as bind:
            row=bind.execute(sa.select(t.ai_config_validations)).mappings().one()
            request=bind.execute(sa.select(t.ai_config_requests).where(t.ai_config_requests.c.id==row['request_record_id'])).mappings().one()
            assert request['status']=='succeeded' and row['valid_until']>utcnow()
            assert row['target_spec']['profile']['model']=='fixture-model'
        report['checks']['config_mysql_probe_prepare_run_finish_request_validation_fk'] = True
        report['checks']['config_mysql_completed_probe_replay_does_not_repeat_http'] = True
        from app.model_config.prompt_contracts import default_template
        from app.model_config.publishing import PublishingService
        with Session(engine) as session:
            current = asyncio.run(service.get_draft(Database(session),actor,draft['id']))
        binding = dict(current['payload']['role_bindings_raw']['session_title'])
        binding.update(enabled=True,failure_mode='error')
        with Session(engine) as session:
            current,_ = asyncio.run(service.patch_draft(Database(session),actor,draft['id'],{'operations':[
                {'op':'replace_binding','role_key':'session_title','binding':binding},
                {'op':'replace_prompt','role_key':'session_title','prompt':{'system_text':'title only',
                    'template_text':default_template('session_title')}}]},'publish-prepare',current['etag']))
        with Session(engine) as session:
            result=asyncio.run(validation.test_draft(Database(session),actor,draft['id'],{
                'role_key':'session_title','expected_draft_version':current['draft_version']},'publish-proof',current['etag']))
            assert result[0]['status']=='passed'
        def publish_once(index):
            try:
                with Session(engine) as session:
                    return asyncio.run(PublishingService(service,database).publish(Database(session),actor,draft['id'],{
                        'expected_draft_version':current['draft_version'],'expected_head_version':0},
                        'publication-'+str(index),proof,current['etag']))[1]
            except BusinessError as error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:statuses=list(pool.map(publish_once,[1,2]))
        assert sorted(statuses)==[200,40920],statuses
        with engine.connect() as bind:
            assert bind.execute(sa.select(sa.func.count()).select_from(t.ai_config_releases)).scalar_one()==1
            assert bind.execute(sa.select(sa.func.count()).select_from(t.outbox_events)).scalar_one()==1
            assert bind.execute(sa.select(t.ai_config_heads.c.head_version)).scalar_one()==1
        report['checks']['config_mysql_concurrent_publication_cas_one_release_one_outbox'] = True
        from app.model_config.rebase import RebaseService
        from app.model_config.legacy_import import LegacyImporter,build_plan
        def fork(key):
            with Session(engine) as session:
                live=session.execute(sa.select(t.ai_config_heads)).mappings().one()
                session.rollback()
                return asyncio.run(service.create_draft(Database(session),actor,{
                    'base_release_id':live['active_release_id'],'expected_head_version':live['head_version']},key))[0]
        def edit(value,text,key):
            with Session(engine) as session:
                return asyncio.run(service.patch_draft(Database(session),actor,value['id'],{'operations':[
                    {'op':'replace_prompt','role_key':'chat','prompt':{'system_text':text,'template_text':'{question}'}}]},key,value['etag']))[0]
        ours=edit(fork('merge-ours'),'ours','merge-local')
        theirs=edit(fork('merge-theirs'),'theirs','merge-remote')
        async def move_head():
            with Session(engine) as session:
                await validation.test_draft(Database(session),actor,theirs['id'],{
                    'role_key':'session_title','expected_draft_version':theirs['draft_version']},'remote-proof',theirs['etag'])
            with Session(engine) as session:
                return await PublishingService(service,database).publish(Database(session),actor,theirs['id'],{
                    'expected_draft_version':theirs['draft_version'],'expected_head_version':1},'remote-publish',proof,theirs['etag'])
        asyncio.run(move_head())
        def merge_once(index,resolve=False):
            try:
                with Session(engine) as session:
                    return asyncio.run(RebaseService(service).rebase(Database(session),actor,ours['id'],{
                        'expected_head_version':2,'resolutions':[{'path':'/role_bindings_raw/chat','choice':'ours'}] if resolve else []},
                        'merge-'+str(index),ours['etag']))[1]
            except BusinessError as error:return error.code
        assert merge_once(0)==40925
        with engine.connect() as bind:
            assert bind.execute(sa.select(t.ai_config_drafts.c.draft_version).where(t.ai_config_drafts.c.id==ours['id'])).scalar_one()==ours['draft_version']
        report['checks']['config_mysql_rebase_conflict_rolls_back_original_draft'] = True
        with ThreadPoolExecutor(max_workers=2) as pool:statuses=list(pool.map(lambda i:merge_once(i,True),[1,2]))
        assert sorted(statuses)==[200,40921],statuses
        with Session(engine) as session:
            merged=asyncio.run(service.get_draft(Database(session),actor,ours['id']))
        assert merged['base_head_version']==2 and merged['draft_version']==ours['draft_version']+1
        report['checks']['config_mysql_concurrent_rebase_cas_has_one_winner'] = True
        async def untested_publish():
            with Session(engine) as session:
                return await PublishingService(service,database).publish(Database(session),actor,merged['id'],{
                    'expected_draft_version':merged['draft_version'],'expected_head_version':2},'untested-publish',proof,merged['etag'])
        try:asyncio.run(untested_publish())
        except BusinessError as error:assert error.code==42222 and error.data['reason']=='CAPABILITY_UNVERIFIED'
        else:raise AssertionError('A rebased draft must have its own fresh capability proof')
        report['checks']['config_mysql_rebase_requires_own_version_validation'] = True
        plan=build_plan({'MODEL_PROVIDER':'ollama','OLLAMA_BASE_URL':policy['origin']})
        def import_once(_):
            with Session(engine) as session:
                return asyncio.run(LegacyImporter(service).apply(Database(session),actor.user_id,plan,{policy['origin']:policy['id']}))
        with ThreadPoolExecutor(max_workers=2) as pool:imports=list(pool.map(import_once,[1,2]))
        assert imports[0]==imports[1] and imports[0][1]==201
        with engine.connect() as bind:
            request=bind.execute(sa.select(t.ai_config_requests).where(t.ai_config_requests.c.action=='bootstrap.import')).mappings().one()
            assert request['actor_access_jti'] is None and request['execution_state']['authority_kind']=='controlled_cli'
            assert bind.execute(sa.select(t.ai_config_heads.c.head_version)).scalar_one()==2
            assert bind.execute(sa.select(sa.func.count()).select_from(t.ai_config_releases)).scalar_one()==2
            assert bind.execute(sa.select(sa.func.count()).select_from(t.outbox_events)).scalar_one()==2
        report['checks']['config_mysql_concurrent_cli_import_one_draft_no_head_move'] = True
        from embedding_baselines_mysql_gate import run_baseline_gate
        run_baseline_gate(engine,Database,actor,service,report)
        from vector_bindings_mysql_gate import run_vector_gate
        run_vector_gate(workspace,engine,Database,actor,service,report)
        from credential_revocation_mysql_gate import run_credential_gate
        run_credential_gate(engine,Database,actor,service,report)
        from outbox_management_mysql_gate import run_outbox_gate
        run_outbox_gate(engine,Database,actor,service,report)
        from candidate_lifecycle_mysql_gate import run_lifecycle_gate
        run_lifecycle_gate(engine,Database,actor,service,report)
        with engine.begin() as bind:
            storage_migration.op=Operations(MigrationContext.configure(bind))
            try:storage_migration.downgrade()
            except RuntimeError as error:assert 'Pinned index storage exists' in str(error)
            else:raise AssertionError('Pinned storage downgrade must refuse')
            assert 'index_context' in {item['name'] for item in sa.inspect(bind).get_columns('model_traces')}
            report['checks']['index_storage_populated_downgrade_refuses_before_ddl']=True
        from user_memory_mysql_gate import run_memory_gate
        run_memory_gate(workspace,engine,Database,actor,service.keyring,report)
        from key_rotation_mysql_gate import run_key_rotation_gate
        run_key_rotation_gate(workspace,engine,Database,actor,service,report)
        from usage_pricing_mysql_gate import run_pricing_gate
        run_pricing_gate(engine,Database,actor,service,report)
        from tool_operations_mysql_gate import run_tool_operations_gate
        run_tool_operations_gate(workspace,engine,Database,actor,report)
        from workflow_mysql_gate import run_workflow_gate
        run_workflow_gate(workspace,engine,Database,actor,port,password,report)
        from workflow_events_mysql_gate import run_workflow_events_gate
        run_workflow_events_gate(workspace,engine,Database,actor,report)
        from detached_chat_mysql_gate import run_detached_chat_gate
        run_detached_chat_gate(workspace,engine,Database,actor,report)
        from workflow_cancel_mysql_gate import run_workflow_cancel_gate
        run_workflow_cancel_gate(workspace,engine,Database,actor,port,password,report)
        from detached_worker_mysql_gate import run_detached_worker_gate
        run_detached_worker_gate(workspace,engine,Database,actor,port,password,report)
        from note_index_mysql_gate import run_note_index_gate
        run_note_index_gate(workspace,engine,Database,actor,report)
        from tool_approval_mysql_gate import run_tool_approval_gate
        run_tool_approval_gate(workspace,engine,Database,actor,report)
        from email_dispatch_mysql_gate import run_email_dispatch_gate
        run_email_dispatch_gate(workspace,engine,Database,actor,report)
        from workflow_budget_mysql_gate import run_workflow_budget_gate
        run_workflow_budget_gate(workspace,engine,Database,actor,port,password,report)
        from m2_recovery_mysql_gate import run_m2_recovery_gate
        run_m2_recovery_gate(workspace,engine,Database,actor,port,password,report)
        from knowledge_ingest_mysql_gate import run_knowledge_ingest_gate
        async_url = sa.URL.create('mysql+aiomysql', username='gate_root', password=password,
                                  host='127.0.0.1', port=port, database='m1_fixture', query={'charset': 'utf8mb4'})
        run_knowledge_ingest_gate(workspace, engine, Database, actor, report, async_url)
        from app.models.index_build import index_builds
        from app.model_config.index_restore import lock_catchups_for_switch
        from app.rag.index_build_worker import IndexBuildWorker

        build_path = workspace / 'alembic/versions/ea03d94b72c1_rag_index_builds.py'
        build_spec = importlib.util.spec_from_file_location('rag_build_migration', build_path)
        build_migration = importlib.util.module_from_spec(build_spec)
        build_spec.loader.exec_module(build_migration)
        build_migration.context = SimpleNamespace(is_offline_mode=lambda: False)
        catch_up_path = workspace / 'alembic/versions/c72d19e85a4b_index_restore_catchup.py'
        catch_up_spec = importlib.util.spec_from_file_location('rag_index_catch_up_migration', catch_up_path)
        catch_up_migration = importlib.util.module_from_spec(catch_up_spec)
        catch_up_spec.loader.exec_module(catch_up_migration)
        catch_up_migration.context = build_migration.context
        with engine.begin() as bind:
            with Operations.context(MigrationContext.configure(bind)):
                build_migration.upgrade()
                build_migration.upgrade()
                build_migration.downgrade()
                build_migration.upgrade()
            draft_id = bind.execute(sa.select(t.ai_config_drafts.c.id).limit(1)).scalar_one()
            now = utcnow() - timedelta(seconds=2)
            bind.execute(index_builds.insert().values(id=str(uuid.uuid4()), draft_id=draft_id, draft_version=1,
                actor_id=actor.user_id, policy_version=1, target_hash='a' * 64, status='queued', next_run_at=now,
                attempts=0, created_at=now))
            with Operations.context(MigrationContext.configure(bind)):
                catch_up_migration.upgrade()
                catch_up_migration.upgrade()
                catch_up_migration.downgrade()
                catch_up_migration.upgrade()
            assert bind.execute(sa.select(index_builds.c.restore_release_id)).scalar_one() is None

        def claim_index_build():
            return asyncio.run(IndexBuildWorker(service, database).claim())

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: claim_index_build(), range(2)))
        owned = [claim for claim in claims if claim is not None]
        assert len(owned) == 1
        with engine.begin() as bind:
            bind.execute(index_builds.update().values(lease_expires_at=now))
        reclaimed = claim_index_build()
        assert reclaimed['id'] == owned[0]['id'] and reclaimed['lease_token'] != owned[0]['lease_token']
        report['checks']['rag_index_build_mysql_migration_and_exclusive_reclaim'] = True
        with engine.begin() as bind:
            historical = bind.execute(sa.select(t.ai_config_releases.c.id).limit(1)).scalar_one()
            bind.execute(index_builds.update().values(restore_release_id=historical,
                index_ids={'rag': 'historical-index'}, collections={'rag': 'historical-collection'}))
        target = {'rag': {'id': 'switch-target', 'storage_binding': {'collection_id': 'historical-collection'}}}
        with Session(engine) as session, session.begin():
            try:
                asyncio.run(lock_catchups_for_switch(Database(session), target))
            except BusinessError as error:
                assert error.data['reason'] == 'INDEX_RESTORE_CATCH_UP_RUNNING'
            else:
                raise AssertionError('An aliased historical collection is still leased')
        with engine.begin() as bind:
            bind.execute(index_builds.update().values(status='ready_to_switch', lease_token=None, lease_expires_at=None))
            with Operations.context(MigrationContext.configure(bind)):
                try:
                    catch_up_migration.downgrade()
                except RuntimeError as error:
                    assert str(error) == 'Historical index catch-up jobs exist'
                else:
                    raise AssertionError('Historical catch-up records must survive a downgrade attempt')
        with Session(engine) as session, session.begin():
            assert asyncio.run(lock_catchups_for_switch(Database(session), target)) == [reclaimed['id']]
        report['checks']['rag_index_restore_mysql_upgrade_and_storage_lease_fence'] = True
        with engine.begin() as bind:
            migration.op = Operations(MigrationContext.configure(bind))
            try:
                migration.downgrade()
            except RuntimeError as error:
                assert 'configuration data exists' in str(error)
            else:
                raise AssertionError('Populated control plane must refuse destructive downgrade')
            assert sa.inspect(bind).has_table('ai_secrets')
            report['checks']['config_migration_populated_downgrade_refuses_before_ddl'] = True
    finally:
        engine.dispose()
