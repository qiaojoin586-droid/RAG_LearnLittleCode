"""Config publication gate with a private catalog fixture, no live embedding API."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import copy
import json
from pathlib import Path
import sqlite3
import tempfile

import httpx
import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_vector_gate(workspace,engine,Database,actor,control,report):
    from app.core.failed_response import BusinessError
    from app.model_config import tables as t
    from app.model_config.embedding_status import EmbeddingStatusService
    from app.model_config.index_attestation import IndexAttestationService,prepare_attestation
    from app.model_config.index_inventory import inspect_indexes
    from app.model_config.provider import ProviderGateway
    from app.model_config.publishing import PublishingService
    from app.model_config.validation_service import ValidationService
    from app.model_config.security import issue_proof
    from app.model_config.contracts import SCHEMA
    @asynccontextmanager
    async def database():
        with Session(engine) as session:yield Database(session)
    root=(workspace/'.cache/vector-binding-gate').resolve();root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='catalog-',dir=root) as directory:
        directory=Path(directory).resolve()
        if not directory.is_relative_to(root):raise RuntimeError('Invalid temporary catalog directory')
        with sqlite3.connect(directory/'chroma.sqlite3') as catalog:
            catalog.executescript('''CREATE TABLE databases(id TEXT,name TEXT,tenant_id TEXT);
                CREATE TABLE collections(id TEXT,name TEXT,dimension INTEGER,database_id TEXT);
                CREATE TABLE segments(id TEXT,collection TEXT,scope TEXT);
                CREATE TABLE embeddings(id INTEGER,segment_id TEXT);
                INSERT INTO databases VALUES('db','default_database','default_tenant');
                INSERT INTO collections VALUES('rag-id','rag_collection',2,'db'),('notes-id','notes_collection',2,'db');''')
        catalog.close()
        inventory=inspect_indexes(directory,workspace=workspace)
        with Session(engine) as session:
            registered,_=asyncio.run(EmbeddingStatusService(control).register(Database(session),actor.user_id,inventory,'vector-baselines'))
        with Session(engine) as session:
            head=session.execute(sa.select(t.ai_config_heads)).mappings().one();session.rollback()
            draft,_=asyncio.run(control.create_draft(Database(session),actor,{'base_release_id':head['active_release_id'],
                'expected_head_version':head['head_version']},'vector-draft'))
        with engine.connect() as connection:policy=connection.execute(sa.select(t.ai_endpoint_policies)).mappings().one()
        profile={'display_name':'isolated-vector','model_kind':'embedding','adapter_type':'ollama_native','api_protocol':'ollama_embed',
            'vendor':'ollama','base_url':policy['origin'],'model':'fixture-embedding','model_revision':None,'auth_mode':'none',
            'endpoint_policy_revision_id':policy['id'],'context_window':None,'tokenizer_id':None,'tokenizer_revision':None,
            'token_counting_policy':{},'max_output_tokens':None,'connect_timeout_ms':1000,'request_timeout_ms':3000,
            'embedding_dimension':2,'parameters':{}}
        with Session(engine) as session:
            draft,_=asyncio.run(control.patch_draft(Database(session),actor,draft['id'],{'operations':[{
                'op':'replace_profile','role_key':'embedding','profile':profile,'credential':{'action':'keep'},'index_keys':[]}]},
                'vector-profile',draft['etag']))
        identity={'schema_version':1,'vendor':'ollama','model':'fixture-embedding','weights_revision':'sha256:'+'a'*64,
            'dimension':2,'normalization':'provider','input_preprocessing':'utf8-verbatim-v1','document_prefix':'',
            'query_prefix':'','pooling':'provider','parameters':{}}
        artifact=directory/'build-record.json'
        artifact.write_text(json.dumps({'schema_version':1,'semantic_identity':identity,'provider_origin':policy['origin'],
            'api_protocol':'ollama_embed','build_reference':'mysql-fixture','parser_version':'parser-v1','chunker_version':'chunks-v1',
            'source_watermark':'fixture:1','collections':{item['index_key']:{key:item[key] for key in ('collection_name','collection_id','dimension')}
                for item in inventory['collections']}}),encoding='utf-8')
        plan=prepare_attestation(artifact,directory,{row['index_key']:row['id'] for row in registered['items']},workspace=workspace)
        def attach():
            with Session(engine) as session:return asyncio.run(IndexAttestationService(control).attach(
                Database(session),actor.user_id,draft['id'],plan,'attest-vector',draft['etag']))
        first=attach();assert attach()==first
        with Session(engine) as session:current=asyncio.run(control.get_draft(Database(session),actor,draft['id']))
        assert current['embedding_diff'][0]['status']=='compatible'
        with engine.connect() as connection:
            assert connection.execute(sa.select(t.ai_config_heads.c.head_version)).scalar_one()==2
        report['checks']['vector_mysql_attestation_replay_and_draft_only_scope']=True
        raw=copy.deepcopy(current['payload']['role_bindings_raw']['embedding']);raw.update(enabled=True,failure_mode='error')
        with Session(engine) as session:
            current,_=asyncio.run(control.patch_draft(Database(session),actor,current['id'],{
                'operations':[{'op':'replace_binding','role_key':'embedding','binding':raw}]},'vector-enable',current['etag']))
        def gateway(profile,policy,key,*,permit):
            async def reply(request):
                await permit()
                if request.method=='GET':return httpx.Response(200,json={'models':[{'name':'fixture-embedding','digest':'a'*64}]})
                if request.url.path.endswith('/api/embed'):return httpx.Response(200,json={'embeddings':[[1.,0.],[0.,1.]]})
                return httpx.Response(200,text='{"message":{"content":"OK"},"done":false}\n{"message":{},"done":true}\n')
            return ProviderGateway(profile,policy,key,permit=permit,transport=httpx.MockTransport(reply))
        validation=ValidationService(control,database,gateway_factory=gateway)
        async def prove(value,prefix):
            for role in ('embedding','session_title'):
                with Session(engine) as session:
                    result,_=await validation.test_draft(Database(session),actor,value['id'],{
                        'role_key':role,'expected_draft_version':value['draft_version']},prefix+'-'+role,value['etag'])
                    assert result['status']=='passed',result
        asyncio.run(prove(current,'first-vector-proof'))
        proof=issue_proof(control.keyring,actor,1)['proof']
        def publish_once(index):
            try:
                with Session(engine) as session:return asyncio.run(PublishingService(control,database).publish(Database(session),actor,
                    current['id'],{'expected_draft_version':current['draft_version'],'expected_head_version':2},'vector-publish-'+str(index),proof,current['etag']))
            except BusinessError as error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(publish_once,[1,2]))
        assert sorted(value[1] if isinstance(value,tuple) else value for value in results)==[200,40920],results
        published=next(value[0] for value in results if isinstance(value,tuple))
        with engine.connect() as connection:
            assert connection.execute(sa.select(sa.func.count()).select_from(t.ai_config_release_refs).where(
                t.ai_config_release_refs.c.release_id==published['release_id'],t.ai_config_release_refs.c.entity_kind=='index')).scalar_one()==2
            assert connection.execute(sa.select(t.ai_config_heads.c.head_version)).scalar_one()==3
        report['checks']['vector_mysql_publication_cas_pins_both_index_references']=True
        with Session(engine) as session:
            fresh,_=asyncio.run(control.create_draft(Database(session),actor,{
                'base_release_id':published['release_id'],'expected_head_version':3},'vector-rotate-draft'))
        selected=fresh['payload']['role_bindings_raw']['embedding']['profile_revision_id']
        old=next(value for value in fresh['profiles'] if value['id']==selected)
        replacement={key:old[key] for key in SCHEMA['$defs']['ProfileInput']['properties']};replacement['request_timeout_ms']+=1
        with Session(engine) as session:
            rotated,_=asyncio.run(control.patch_draft(Database(session),actor,fresh['id'],{'operations':[{
                'op':'replace_profile','role_key':'embedding','profile':replacement,'credential':{'action':'keep'},
                'index_keys':['rag','notes']}]},'vector-rotate',fresh['etag']))
        asyncio.run(prove(rotated,'rotate-proof'))
        with Session(engine) as session:
            switched,_=asyncio.run(PublishingService(control,database).publish(Database(session),actor,rotated['id'],{
                'expected_draft_version':rotated['draft_version'],'expected_head_version':3},'vector-rotate-publish',proof,rotated['etag']))
        assert switched['embedding_state']=='connection_updated'
        assert all(current['payload']['index_bindings'][key]['index_version_id']==rotated['payload']['index_bindings'][key]['index_version_id'] for key in ('rag','notes'))
        report['checks']['vector_mysql_same_space_connection_update_keeps_index_versions']=True
        with Session(engine) as session:
            restored,_=asyncio.run(PublishingService(control,database).restore(Database(session),actor,published['release_id'],{
                'expected_head_version':4},'vector-restore-offline',proof))
        assert restored['historical_offline'] and restored['head_version']==5
        report['checks']['vector_mysql_historical_restore_reuses_original_offline_evidence']=True
