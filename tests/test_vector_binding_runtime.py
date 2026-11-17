"""Published vector requests use real Chroma plus SQL/HTTP fixtures, never live models."""
from contextlib import asynccontextmanager
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.contracts import content_hash,SCHEMA
from app.model_config.embedding_status import EmbeddingStatusService
from app.model_config.index_attestation import IndexAttestationService,prepare_attestation
from app.model_config.index_inventory import inspect_indexes
from app.model_config.snapshots import thaw,freeze
from app.model_config import vector_runtime as vr
from app.model_config.publishing import PublishingService
from app.rag.vector_store import VectorStoreService
from test_model_config_control_plane import admin,store,profile,AsyncFacade  # noqa: F401
from test_model_config_publication import publish,snapshot,selected,mock_runtime,database_factory
from test_model_config_validation import validation_service,execute,fixture_handler


def probe_response(request):
    if request.method=='GET':return httpx.Response(200,json={'models':[{'name':'local-model','digest':'a'*64}]})
    return fixture_handler(request)


@pytest_asyncio.fixture(params=['openai','ollama','dashscope'])
async def vectors(admin,tmp_path,monkeypatch,request):
    native=request.param=='ollama';dashscope=request.param=='dashscope'
    for name in ('TEMP','TMP','SQLITE_TMPDIR'):monkeypatch.setenv(name,str(tmp_path))
    import chromadb
    from chromadb.config import Settings
    directory=tmp_path/'chroma';directory.mkdir()
    with chromadb.PersistentClient(path=os.path.relpath(directory,Path.cwd()).replace('\\','/'),settings=Settings(anonymized_telemetry=False)) as client:
        collections={}
        for key in ('rag','notes'):
            collection=client.create_collection(key+'_collection',embedding_function=None,metadata={'hnsw:space':'cosine'})
            collection.upsert(ids=['a','foreign'],documents=['owned document','other user private'],embeddings=[[1.,0.],[1.,0.]],
                metadatas=[{'user_id':admin.actor.user_id,'document_id':'doc','note_id':'note'},
                           {'user_id':admin.other.user_id,'document_id':'foreign','note_id':'foreign'}])
            collections[key]={'collection_name':collection.name,'collection_id':str(collection.id),'dimension':2}
    inventory=inspect_indexes(directory,workspace=tmp_path)
    with Session(admin.engine) as session:
        registered,_=await EmbeddingStatusService(admin.service).register(AsyncFacade(session),admin.actor.user_id,inventory,'register')
    initial=await admin.draft('text-bootstrap')
    text_published,_=await publish(admin,initial,'text-bootstrap')
    draft,_=await admin.call('create_draft',{'base_release_id':text_published['release_id'],'expected_head_version':text_published['head_version']},'vector-draft')
    policy=await admin.policy(local=native)
    value=profile(policy,local=native);value.update(model_kind='embedding',api_protocol='ollama_embed' if native else 'openai_embeddings',
        embedding_dimension=2,max_output_tokens=None,parameters={})
    if dashscope:value.update(adapter_type='dashscope_native',api_protocol='dashscope_embed',vendor='dashscope',auth_mode='dashscope_key')
    draft,_=await admin.call('patch_draft',draft['id'],{'operations':[{'op':'replace_profile','role_key':'embedding','profile':value,
        'credential':{'action':'keep'} if native else {'action':'replace','api_key':'fictional-vector-key-one'},'index_keys':[]}]},'profile',draft['etag'],None if native else admin.proof())
    identity={'schema_version':1,'vendor':value['vendor'],'model':value['model'],
        'weights_revision':'sha256:'+'a'*64 if native else 'immutable-fixture-v1','dimension':2,'normalization':'provider',
        'input_preprocessing':'utf8-verbatim-v1','document_prefix':'doc: ','query_prefix':'query: ','pooling':'provider','parameters':{}}
    record={'schema_version':1,'semantic_identity':identity,'provider_origin':policy['origin'],'api_protocol':value['api_protocol'],
        'build_reference':'isolated-build-fixture','parser_version':'parser-v1','chunker_version':'chunks-v1',
        'source_watermark':'fixture:1','collections':collections}
    artifact=tmp_path/'build-record.json';artifact.write_text(json.dumps(record),encoding='utf-8')
    plan=prepare_attestation(artifact,directory,{row['index_key']:row['id'] for row in registered['items']},workspace=tmp_path)
    with Session(admin.engine) as session:
        result,_=await IndexAttestationService(admin.service).attach(AsyncFacade(session),admin.actor.user_id,draft['id'],plan,'attest',draft['etag'])
    assert not result['published']
    draft=await admin.call('get_draft',draft['id'])
    binding=copy.deepcopy(draft['payload']['role_bindings_raw']['embedding']);binding.update(enabled=True,failure_mode='error')
    draft,_=await admin.call('patch_draft',draft['id'],{'operations':[{'op':'replace_binding','role_key':'embedding','binding':binding}]},'enable',draft['etag'])
    validator,_=validation_service(admin,probe_response)
    assert (await execute(admin,validator,draft,'embedding'))[0]['status']=='passed'
    published,_=await publish(admin,draft)
    pinned=await snapshot(admin)
    monkeypatch.setattr('app.db.database.async_session_factory',database_factory(admin))
    vr._cache.clear()
    state={'digest':'a'*64,'bad':False}
    def handler(request):
        if request.method=='GET':return httpx.Response(200,json={'models':[{'name':value['model'],'digest':state['digest']}]})
        body=json.loads(request.content);texts=body['input']
        if dashscope:texts=texts['texts']
        rows=[[1.,0.] if not state['bad'] else [1.] for _ in texts]
        if native:
            if state.get('drift_after_embed'):state['digest']='b'*64
            return httpx.Response(200,json={'embeddings':rows,'prompt_eval_count':len(texts)})
        if dashscope:return httpx.Response(200,json={'output':{'embeddings':[{'text_index':index,'embedding':row} for index,row in enumerate(rows)]}})
        return httpx.Response(200,json={'data':[{'index':index,'embedding':row} for index,row in reversed(list(enumerate(rows)))],
            'usage':{'prompt_tokens':len(texts),'total_tokens':len(texts)}})
    requests=mock_runtime(admin,monkeypatch,handler)
    return SimpleNamespace(admin=admin,draft=draft,published=published,snapshot=pinned,requests=requests,state=state,
        service=VectorStoreService(),plan=plan,artifact=artifact,directory=directory,native=native,dashscope=dashscope,
        monkeypatch=monkeypatch,text_published=text_published)


@pytest.mark.asyncio
async def test_two_collections_share_query_embedding_and_recheck_each_index(vectors, monkeypatch):
    from app.rag.rag_service import RagService

    v = vectors
    async def visible(rows, owner):
        return rows
    service = RagService(v.service, enable_keyword=False, visibility_checker=visible)
    real_permit = vr.permit_index
    checked = []
    async def permit(snapshot, index, profile, session_factory=None):
        checked.append(index['index_key'])
        return await real_permit(snapshot, index, profile, session_factory)
    monkeypatch.setattr(vr, 'permit_index', permit)
    with selected(v.admin, v.snapshot):
        hit = await service.query('shared query', v.admin.actor.user_id, use_hyde=False)
        assert {r['source'] for r in hit['sources']} == {'knowledge','notes'}
        assert len([r for r in v.requests if r.method == 'POST']) == 1
        assert checked.count('rag') >= 2 and checked.count('notes') >= 2
        async def revoke(snapshot, index, profile, session_factory=None):
            if index['index_key'] == 'notes':
                raise RuntimeError('revoked')
            return await real_permit(snapshot, index, profile, session_factory)
        monkeypatch.setattr(vr, 'permit_index', revoke)
        hit = await service.query('shared query', v.admin.actor.user_id, use_hyde=False)
        assert hit['failed_sources'] == ['notes'] and all(r['source']=='knowledge' for r in hit['sources'])


@pytest.mark.asyncio
async def test_real_chroma_queries_and_writes_use_pinned_models_and_owned_metadata(vectors):
    v=vectors

    @asynccontextmanager
    async def stale_attempt():
        raise RuntimeError('fence rejected')
        yield

    with selected(v.admin,v.snapshot):
        docs=await v.service.search_documents('find',v.admin.actor.user_id)
        notes=await v.service.search_notes('find',v.admin.actor.user_id)
        assert [row['content'] for row in docs]==['owned document']
        assert [row['content'] for row in notes]==['owned document']
        with pytest.raises(RuntimeError, match='fence rejected'):
            await v.service.upsert_document(['obsolete'],[{'user_id':v.admin.actor.user_id,'note_id':'old'}],
                                            ['old-id'],'notes',write_guard=stale_attempt)
        assert len(await v.service.search_notes('find',v.admin.actor.user_id))==1
        await v.service.upsert_document(['new note'],[{'user_id':v.admin.actor.user_id,'note_id':'new'}],['new-id'],'notes')
        assert len(await v.service.search_notes('find',v.admin.actor.user_id))==2
        with pytest.raises(RuntimeError, match='fence rejected'):
            await v.service.delete_note_vectors('new', user_id=v.admin.actor.user_id, write_guard=stale_attempt)
        assert len(await v.service.search_notes('find',v.admin.actor.user_id))==2
        await v.service.delete_note_vectors('new')
        assert len(await v.service.search_notes('find',v.admin.actor.user_id))==1
    payloads=[json.loads(r.content) for r in v.requests if r.method=='POST']
    inputs=[body['input']['texts'] if v.dashscope else body['input'] for body in payloads]
    assert ['query: find'] in inputs and ['doc: new note'] in inputs
    if v.dashscope:
        assert {body['parameters']['text_type'] for body in payloads}=={'query','document'}
    assert len(v.snapshot.indexes)==2
    assert set(row['index_version_id'] for row in thaw(v.snapshot.manifest)['index_bindings'].values())==set(v.snapshot.indexes)


@pytest.mark.asyncio
async def test_cache_separates_query_document_and_space_and_rejects_bad_vectors(vectors):
    v=vectors
    with selected(v.admin,v.snapshot):
        await vr.embeddings(['same'],'rag',purpose='query')
        calls=len([r for r in v.requests if r.method=='POST'])
        await vr.embeddings(['same'],'rag',purpose='query')
        assert len([r for r in v.requests if r.method=='POST'])==calls
        await vr.embeddings(['same'],'rag',purpose='document')
        assert len([r for r in v.requests if r.method=='POST'])==calls+1
        previous=len(vr._cache);v.state['bad']=True
        with pytest.raises(BusinessError):await vr.embeddings(['bad new input'],'rag')
        assert len(vr._cache)==previous
    assert all('same' not in key for key in vr._cache)


@pytest.mark.asyncio
async def test_warm_cache_still_checks_revocation_and_native_model_digest(vectors):
    v=vectors
    with selected(v.admin,v.snapshot):
        await vr.embeddings(['cached'],'rag')
        if v.native:
            v.state['digest']='b'*64
            with pytest.raises(BusinessError) as error:await vr.embeddings(['cached'],'rag')
            assert error.value.data['reason']=='OLLAMA_MODEL_DIGEST_CHANGED'
            v.state.update(digest='a'*64,drift_after_embed=True)
            before=len(vr._cache)
            with pytest.raises(BusinessError) as error:await vr.embeddings(['changed while embedding'],'rag')
            assert error.value.data['reason']=='OLLAMA_MODEL_DIGEST_CHANGED' and len(vr._cache)==before
        else:
            v.admin.sql(t.ai_secrets.update().values(status='revoked',revocation_epoch=1))
            with pytest.raises(BusinessError) as error:await vr.embeddings(['cached'],'rag')
            assert error.value.code==40926


@pytest.mark.asyncio
async def test_missing_or_replaced_collection_never_falls_back_to_legacy(vectors):
    v=vectors
    def forbidden():raise AssertionError('legacy store must not be initialized')
    v.monkeypatch.setattr(v.service,'_ensure_initialized',forbidden)
    from chromadb import PersistentClient
    from chromadb.config import Settings
    with PersistentClient(path=str(v.directory),settings=Settings(anonymized_telemetry=False)) as client:
        client.delete_collection('rag_collection')
        client.create_collection('rag_collection',embedding_function=None)
    with selected(v.admin,v.snapshot):
        with pytest.raises(BusinessError) as error:await v.service.search_documents('find',v.admin.actor.user_id)
        assert error.value.data['reason']=='INDEX_COLLECTION_REPLACED'


@pytest.mark.asyncio
async def test_vector_owner_and_index_revocation_are_checked_before_cached_results(vectors):
    v=vectors
    with selected(v.admin,v.snapshot):
        with pytest.raises(BusinessError):await v.service.search_documents('find',v.admin.other.user_id)
        with pytest.raises(BusinessError):await v.service.upsert_document(['wrong'],[{'user_id':v.admin.other.user_id}],['x'])
        await vr.embeddings(['cached'],'rag')
        v.admin.sql(t.rag_index_versions.update().values(status='retired'))
        with pytest.raises(BusinessError) as error:await vr.embeddings(['cached'],'rag')
        assert error.value.data['reason']=='INDEX_BASELINE_UNVERIFIED'


@pytest.mark.asyncio
async def test_key_or_timeout_change_preserves_space_and_old_request_snapshot(vectors):
    v=vectors;admin=v.admin
    fresh,_=await admin.call('create_draft',{'base_release_id':v.published['release_id'],'expected_head_version':v.published['head_version']},'rotate-draft')
    old_profile=next(p for p in fresh['profiles'] if p['profile_key']=='embedding')
    value={key:old_profile[key] for key in SCHEMA['$defs']['ProfileInput']['properties']};value['request_timeout_ms']+=1
    credential={'action':'keep'} if v.native else {'action':'replace','api_key':'fictional-vector-key-two'}
    updated,_=await admin.call('patch_draft',fresh['id'],{'operations':[{'op':'replace_profile','role_key':'embedding','profile':value,
        'credential':credential,'index_keys':['rag','notes']}]},'rotate',fresh['etag'],None if v.native else admin.proof())
    assert updated['embedding_diff'][0]['status']=='compatible'
    validator,_=validation_service(admin,probe_response)
    assert (await execute(admin,validator,updated,'embedding',key='rotate-test'))[0]['status']=='passed'
    result,_=await publish(admin,updated,'rotate-publish')
    assert result['embedding_enabled'] and result['embedding_state']=='connection_updated'
    latest=await snapshot(admin)
    for pinned in (v.snapshot,latest):
        with selected(admin,pinned):await vr.embeddings([pinned.release_id],'rag')
    posts=[r for r in v.requests if r.method=='POST']
    if not v.native:assert [r.headers['Authorization'] for r in posts[-2:]]==['Bearer fictional-vector-key-one','Bearer fictional-vector-key-two']
    assert thaw(latest.manifest)['index_bindings']['rag']['index_version_id']==thaw(v.snapshot.manifest)['index_bindings']['rag']['index_version_id']


@pytest.mark.asyncio
async def test_changed_model_connection_requires_new_evidence_and_new_space_cannot_switch(vectors):
    v=vectors;admin=v.admin
    fresh,_=await admin.call('create_draft',{'base_release_id':v.published['release_id'],'expected_head_version':v.published['head_version']},'change-draft')
    old=next(p for p in fresh['profiles'] if p['profile_key']=='embedding')
    value={key:old[key] for key in SCHEMA['$defs']['ProfileInput']['properties']};value['model']='different-model-same-dimension'
    updated,_=await admin.call('patch_draft',fresh['id'],{'operations':[{'op':'replace_profile','role_key':'embedding','profile':value,
        'credential':{'action':'keep'},'index_keys':['rag','notes']}]},'change',fresh['etag'])
    assert updated['embedding_diff'][0]['status']=='pending_rebuild'
    assert next(p for p in updated['profiles'] if p['id']==updated['payload']['role_bindings_raw']['embedding']['profile_revision_id'])['semantic_fingerprint'] is None
    with pytest.raises(BusinessError):await publish(admin,updated,'bad-change')
    assert admin.count(t.ai_config_releases)==2


@pytest.mark.asyncio
async def test_batches_have_correct_order_and_gateway_budgets(vectors):
    v=vectors
    with selected(v.admin,v.snapshot):
        values=await vr.embeddings([f'batch-{i}' for i in range(27)],'rag',purpose='document')
    assert values==[[1.,0.]]*27
    posts=[json.loads(r.content) for r in v.requests if r.method=='POST']
    inputs=[body['input']['texts'] if v.dashscope else body['input'] for body in posts]
    assert [len(value) for value in inputs]==[10,10,7]
    assert [text for value in inputs for text in value]==[f'doc: batch-{i}' for i in range(27)]


@pytest.mark.asyncio
async def test_cache_cannot_mix_two_semantic_spaces(vectors):
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from app.model_config.embedding_space import semantic_fingerprint
    v=vectors
    with selected(v.admin,v.snapshot):await vr.embeddings(['same literal'],'rag')
    profiles=thaw(v.snapshot.profiles);indexes=thaw(v.snapshot.indexes)
    profile_id=thaw(v.snapshot.manifest)['index_bindings']['rag']['query_profile_revision_id']
    profiles[profile_id]['semantic_identity']['query_prefix']='second-space: '
    digest=semantic_fingerprint(profiles[profile_id]['semantic_identity']);profiles[profile_id]['semantic_fingerprint']=digest
    for index in indexes.values():index.update(semantic_identity=copy.deepcopy(profiles[profile_id]['semantic_identity']),semantic_fingerprint=digest)
    # The two snapshots model distinct, already-authorized spaces. The SQL
    # immutability/authorization boundary has separate integration assertions.
    v.monkeypatch.setattr(vr,'permit_index',AsyncMock())
    other=replace(v.snapshot,profiles=freeze(profiles),indexes=freeze(indexes))
    with selected(v.admin,other):await vr.embeddings(['same literal'],'rag')
    posts=[json.loads(r.content) for r in v.requests if r.method=='POST']
    inputs=[body['input']['texts'] if v.dashscope else body['input'] for body in posts]
    assert inputs==[['query: same literal'],['second-space: same literal']]


@pytest.mark.asyncio
async def test_attestation_replay_and_partial_failure_do_not_create_orphan_versions(vectors):
    v=vectors;admin=v.admin
    original=next(row for row in admin.rows(t.ai_config_requests) if row['action']=='index.attest')
    # Exact stored result is replayable even after the original draft published.
    first_draft=admin.rows(t.ai_config_drafts)[1]
    with Session(admin.engine) as session:
        result,_=await IndexAttestationService(admin.service).attach(AsyncFacade(session),admin.actor.user_id,v.draft['id'],
            v.plan,'attest',original['action_payload']['if_match'])
    assert result['draft_id']==v.draft['id'] and first_draft['id']==v.draft['id']
    fresh,_=await admin.call('create_draft',{'base_release_id':v.published['release_id'],
        'expected_head_version':v.published['head_version']},'bad-attest-draft')
    bad=copy.deepcopy(v.plan);bad['index_ids']['rag']=bad['index_ids']['notes']
    before={table.name:admin.count(table) for table in (t.ai_profile_revisions,t.rag_index_versions,t.ai_config_requests,t.ai_config_draft_items)}
    with Session(admin.engine) as session:
        with pytest.raises(BusinessError):await IndexAttestationService(admin.service).attach(AsyncFacade(session),admin.actor.user_id,
            fresh['id'],bad,'bad-attach',fresh['etag'])
    assert before=={table.name:admin.count(table) for table in (t.ai_profile_revisions,t.rag_index_versions,t.ai_config_requests,t.ai_config_draft_items)}
    assert (await admin.call('get_draft',fresh['id']))['etag']==fresh['etag']


@pytest.mark.asyncio
async def test_restoring_text_only_release_does_not_bypass_m1_index_switch_guard(vectors):
    v=vectors;admin=v.admin
    with Session(admin.engine) as session:
        restored,_=await PublishingService(admin.service,database_factory(admin)).restore(AsyncFacade(session),admin.actor,
            v.text_published['release_id'],{'expected_head_version':v.published['head_version']},'restore-text',admin.proof())
    fresh,_=await admin.call('create_draft',{'base_release_id':restored['release_id'],
        'expected_head_version':restored['head_version']},'after-restore')
    old=next(row for row in v.draft['profiles'] if row['profile_key']=='embedding')
    value={key:old[key] for key in SCHEMA['$defs']['ProfileInput']['properties']};value['model_revision']=None
    fresh,_=await admin.call('patch_draft',fresh['id'],{'operations':[{'op':'replace_profile','role_key':'embedding',
        'profile':value,'credential':{'action':'keep'} if v.native else {'action':'replace','api_key':'fictional-new-key'},'index_keys':[]}]},
        'after-restore-profile',fresh['etag'],None if v.native else admin.proof())
    bad=copy.deepcopy(v.plan)
    bad['record']['semantic_identity']['weights_revision']='sha256:'+'b'*64 if v.native else 'immutable-other-revision'
    bad['record_hash']=content_hash(bad['record'])
    with Session(admin.engine) as session:
        with pytest.raises(BusinessError) as error:await IndexAttestationService(admin.service).attach(AsyncFacade(session),admin.actor.user_id,
            fresh['id'],bad,'switch-after-restore',fresh['etag'])
    assert error.value.data['reason']=='INDEX_MIGRATION_REQUIRED'
    with Session(admin.engine) as session:
        result,_=await IndexAttestationService(admin.service).attach(AsyncFacade(session),admin.actor.user_id,
            fresh['id'],v.plan,'same-space-after-restore',fresh['etag'])
    current=await admin.call('get_draft',result['draft_id'])
    assert {k:b['index_version_id'] for k,b in current['payload']['index_bindings'].items()}=={
        k:b['index_version_id'] for k,b in v.draft['payload']['index_bindings'].items()}


@pytest.mark.asyncio
async def test_vector_trace_has_only_metadata_and_unknown_price(vectors):
    from app.core import model_trace
    v=vectors;events=[]
    v.monkeypatch.setattr(model_trace,'_emit',events.append)
    with selected(v.admin,v.snapshot):await vr.embeddings(['private query body'],'rag')
    assert len(events)==1
    event=events[0]
    assert event['release_id']==v.snapshot.release_id and event['index_context']['purpose']=='query'
    assert event['is_test'] is False and event['cost_amount'] is None and event['pricing_status']=='unknown'
    assert 'private query body' not in json.dumps(event) and 'fictional-vector-key' not in json.dumps(event)


@pytest.mark.asyncio
async def test_cache_is_bounded_by_vector_size_as_well_as_entry_count(vectors):
    v=vectors;v.monkeypatch.setattr(vr,'CACHE_FLOAT_LIMIT',4)
    with selected(v.admin,v.snapshot):await vr.embeddings(['one','two','three'],'rag')
    assert len(vr._cache)==2 and sum(len(value[0]) for value in vr._cache.values())==4


@pytest.mark.parametrize('vector',[[True,0.],['1',0.],[float('nan'),0.],[float('inf'),0.],[1.],[],[0.,0.]])
def test_invalid_embedding_numbers_or_shape_never_pass_the_response_boundary(vector):
    from app.model_config.provider import ProviderFailure
    with pytest.raises(ProviderFailure):vr.parse_vectors({'data':[{'index':0,'embedding':vector}]},'openai_embeddings',1,2)


def test_duplicate_build_record_fields_are_rejected_before_catalog_access(tmp_path):
    path=tmp_path/'record.json';path.write_text('{"schema_version":1,"schema_version":2}',encoding='utf-8')
    with pytest.raises(BusinessError) as error:prepare_attestation(path,tmp_path,{'rag':'a'*36,'notes':'b'*36})
    assert error.value.data['reason']=='BUILD_RECORD_INVALID'
