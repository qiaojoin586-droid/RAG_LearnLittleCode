"""Publication, old/new requests and actual LangChain wire behavior with SQL/HTTP fixtures."""
from contextlib import asynccontextmanager,contextmanager
import copy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
from langchain_core.messages import HumanMessage,AIMessage,ToolMessage
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.publishing import PublishingService
from app.model_config.runtime import model_for,template_for
from app.model_config.snapshots import _current,inference_actor,load_snapshot,SnapshotMiddleware
from app.model_config.service import utcnow
from app.model_config.provider import ProviderGateway
from test_model_config_control_plane import admin,store,AsyncFacade,profile_op,prompt_op  # noqa: F401
from test_model_config_validation import validation_service,execute,fixture_handler


def database_factory(admin):
    @asynccontextmanager
    async def factory():
        with Session(admin.engine) as session:yield AsyncFacade(session)
    return factory


async def ready(admin,*,local=True):
    draft,policy=await admin.draft(),await admin.policy(local=local)
    view,_=await admin.call('patch_draft',draft['id'],{'operations':[profile_op(policy,local=local),prompt_op()]},
        'profile',draft['etag'],None if local else admin.proof())
    binding=copy.deepcopy(view['payload']['role_bindings_raw']['chat']);binding.update(enabled=True,failure_mode='error')
    view,_=await admin.call('patch_draft',draft['id'],{'operations':[{'op':'replace_binding','role_key':'chat','binding':binding}]},
        'enable',view['etag'])
    validator,_=validation_service(admin)
    result,_=await execute(admin,validator,view)
    assert result['status']=='passed'
    return view


async def publish(admin,draft,key='publish'):
    with Session(admin.engine) as session:
        return await PublishingService(admin.service,database_factory(admin)).publish(AsyncFacade(session),admin.actor,
            draft['id'],{'expected_draft_version':draft['draft_version'],'expected_head_version':draft['base_head_version']},
            key,admin.proof(),draft['etag'])


async def snapshot(admin):
    with Session(admin.engine) as session:return await load_snapshot(AsyncFacade(session))


@contextmanager
def selected(admin,value):
    token=_current.set(value);actor=inference_actor.set({'user_id':admin.actor.user_id,'jti':None,'expires':None})
    try:yield
    finally:_current.reset(token);inference_actor.reset(actor)


def mock_runtime(admin,monkeypatch,handler=fixture_handler,*,patch_keyring_loader=True):
    from app.model_config import runtime
    from app.model_config.crypto import Keyring
    if patch_keyring_loader:monkeypatch.setattr(Keyring,'load',classmethod(lambda cls,path=None:admin.keyring))
    requests=[]
    @asynccontextmanager
    async def gateway(snapshot,profile_id,**authorization):
        from app.model_config.snapshots import thaw
        profile=thaw(snapshot.profiles[profile_id]);policy=thaw(snapshot.policies[profile['endpoint_policy_revision_id']])
        credential=snapshot.credentials.get(profile['credential_revision_id'])
        key=admin.keyring.decrypt(credential['ciphertext'],credential['key_version'],{
            'scope_type':'system','scope_id':'system','id':credential['id'],'origin':credential['allowed_origin'],
            'version':credential['credential_version']}) if credential else None
        async def reply(request):
            await runtime.permit(snapshot,profile_id,database_factory(admin),**authorization)
            requests.append(request)
            result=handler(request)
            return await result if hasattr(result,'__await__') else result
        async with ProviderGateway(profile,policy,key,permit=AsyncMock(),probe_mode=False,
            transport=httpx.MockTransport(reply)) as client:
            client.budget_session_factory=database_factory(admin)
            yield client
    monkeypatch.setattr(runtime,'gateway_for',gateway)
    return requests


@pytest.mark.asyncio
async def test_publication_atomic_manifest_refs_outbox_replay_and_snapshot_immutability(admin):
    draft=await ready(admin)
    result,status=await publish(admin,draft)
    assert status==200 and result['effective_for']=='new_requests' and result['head_version']==1
    assert (await publish(admin,draft))[0]==result
    assert admin.count(t.ai_config_releases)==admin.count(t.outbox_events)==1
    assert admin.rows(t.ai_profile_revisions)[0]['status']=='published'
    pinned=await snapshot(admin)
    assert pinned.release_id==result['release_id'] and 'ciphertext' not in repr(pinned)
    with pytest.raises(TypeError):pinned.manifest['role_bindings_raw']['chat']['enabled']=False
    assert admin.count(t.ai_config_release_refs)==3
    audit=next(row for row in admin.rows(t.ai_config_audits) if row['action']=='draft.publish')
    assert audit['head_version_before']==0 and audit['head_version_after']==1 and audit['release_id']==result['release_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('change,code',[
    (lambda admin:admin.sql(t.ai_config_validations.update().values(status='unverified')),42222),
    (lambda admin:admin.sql(t.ai_config_validations.update().values(valid_until=utcnow()-timedelta(seconds=1))),42222),
    (lambda admin:admin.sql(t.ai_endpoint_policies.update().values(status='revoked')),40310),
    (lambda admin:admin.sql(t.ai_config_heads.update().values(head_version=1)),40920),
])
async def test_failed_publication_leaves_no_release_head_move_or_outbox(admin,change,code):
    draft=await ready(admin);change(admin)
    with pytest.raises(BusinessError) as error:await publish(admin,draft)
    assert error.value.code==code
    assert admin.count(t.ai_config_releases)==0 and admin.count(t.outbox_events)==0
    assert admin.rows(t.ai_config_drafts)[0]['status']=='editing'


@pytest.mark.asyncio
async def test_unknown_variables_and_unverified_embedding_are_not_publishable(admin):
    draft=await ready(admin)
    view,_=await admin.call('patch_draft',draft['id'],{'operations':[prompt_op(text='same')]},'known',draft['etag'])
    body={'operations':[{'op':'replace_prompt','role_key':'chat','prompt':{'system_text':'system','template_text':'{not_registered}'}}]}
    view,_=await admin.call('patch_draft',draft['id'],body,'unknown',view['etag'])
    with pytest.raises(BusinessError) as error:await publish(admin,view)
    assert error.value.code==42221


@pytest.mark.asyncio
async def test_real_langchain_nonstream_and_tools_use_pinned_profile_and_fixed_safety(admin,monkeypatch):
    draft=await ready(admin);await publish(admin,draft);pinned=await snapshot(admin)
    def handler(request):
        body=json.loads(request.content)
        if body.get('tools'):
            return httpx.Response(200,json={'message':{'content':'','tool_calls':[{
                'function':{'name':'echo','arguments':{'value':'ok'}}}]},'done':True})
        return httpx.Response(200,json={'message':{'content':'done'},'done':True,'prompt_eval_count':8,'eval_count':1})
    requests=mock_runtime(admin,monkeypatch,handler)
    with selected(admin,pinned):
        model=model_for('chat')
        answer=await model.ainvoke([HumanMessage(content='hello')])
        assert answer.content=='done' and answer.usage_metadata['total_tokens']==9
        bound=model.bind_tools([{'name':'echo','description':'local','parameters':{'type':'object','properties':{'value':{'type':'string'}}}}])
        tool=await bound.ainvoke([HumanMessage(content='echo')])
        assert tool.tool_calls[0]['name']=='echo'
        assert model_for('auto_tag',SimpleNamespace()) is None
        assert template_for('chat','legacy',question='hello',user_message='hello',current_time='now')=='hello'
    body=json.loads(requests[0].content)
    assert body['messages'][0]['role']=='system' and '低信任' in body['messages'][0]['content']
    assert body['messages'][1]['content']=='help' and body['options']['num_predict']==256


@pytest.mark.asyncio
@pytest.mark.parametrize('local',[True,False])
async def test_langchain_streams_text_usage_and_tool_call_ids(admin,monkeypatch,local):
    draft=await ready(admin,local=local);await publish(admin,draft);pinned=await snapshot(admin)
    def handler(request):
        body=json.loads(request.content)
        if body.get('tools'):
            if local:
                return httpx.Response(200,text='{"message":{"tool_calls":[{"function":{"name":"echo","arguments":{"value":"ok"}}}]},"done":false}\n{"message":{},"done":true,"prompt_eval_count":1,"eval_count":2}\n')
            return httpx.Response(200,text='data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"echo","arguments":"{\\"value\\":"}}]}}]}\n\ndata: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ok\\"}"}}]}}]}\n\ndata: [DONE]\n\n')
        return fixture_handler(request)
    mock_runtime(admin,monkeypatch,handler)
    with selected(admin,pinned):
        model=model_for('chat');text=''
        async for chunk in model.astream([HumanMessage(content='hello')]):text+=chunk.content
        assert text=='fixture response'
        bound=model.bind_tools([{'name':'echo','parameters':{'type':'object','properties':{'value':{'type':'string'}}}}])
        aggregate=None
        async for chunk in bound.astream([HumanMessage(content='use tool')]):aggregate=chunk if aggregate is None else aggregate+chunk
        assert aggregate.tool_calls[0]['args']=={'value':'ok'}
        assert aggregate.tool_calls[0]['id']


@pytest.mark.asyncio
async def test_old_request_keeps_old_key_after_new_release_and_restore_creates_new_head(admin,monkeypatch):
    draft=await ready(admin,local=False);first,_=await publish(admin,draft);old=await snapshot(admin)
    clone,_=await admin.call('create_draft',{'base_release_id':first['release_id'],'expected_head_version':1},'clone')
    profile=clone['profiles'][0]
    from test_model_config_control_plane import profile as input_profile
    policy=admin.rows(t.ai_endpoint_policies)[0]
    operation=profile_op(policy,key='second-fictional-long-key-654321');operation['profile']['model']='second-model'
    clone,_=await admin.call('patch_draft',clone['id'],{'operations':[operation]},'second-profile',clone['etag'],admin.proof())
    validator,_=validation_service(admin);assert (await execute(admin,validator,clone,key='test2'))[0]['status']=='passed'
    second,_=await publish(admin,clone,key='publish2');new=await snapshot(admin)
    captured=mock_runtime(admin,monkeypatch,lambda _:httpx.Response(200,json={'choices':[{'message':{'content':'OK'}}]}))
    with selected(admin,old):await model_for('chat').ainvoke('first')
    with selected(admin,new):await model_for('chat').ainvoke('second')
    assert captured[0].headers['authorization'].endswith('123456') and captured[1].headers['authorization'].endswith('654321')
    assert json.loads(captured[0].content)['model']=='example-model'
    assert json.loads(captured[1].content)['model']=='second-model'
    admin.sql(t.ai_config_validations.update().values(valid_until=utcnow()-timedelta(days=2)))
    with Session(admin.engine) as session:
        restored,_=await PublishingService(admin.service,database_factory(admin)).restore(AsyncFacade(session),admin.actor,
            first['release_id'],{'expected_head_version':2},'restore',admin.proof())
    assert restored['head_version']==3 and restored['release_number']==3 and restored['historical_offline']
    assert restored['release_id'] not in (first['release_id'],second['release_id'])
    assert (await snapshot(admin)).profiles[next(iter(old.profiles))]['model']=='example-model'


@pytest.mark.asyncio
async def test_revocation_blocks_dispatch_even_for_old_pinned_snapshot(admin,monkeypatch):
    draft=await ready(admin,local=False);await publish(admin,draft);old=await snapshot(admin)
    requests=mock_runtime(admin,monkeypatch)
    admin.sql(t.ai_secrets.update().values(status='revoked',revocation_epoch=1))
    with selected(admin,old):
        with pytest.raises(BusinessError):await model_for('chat').ainvoke('blocked')
    assert requests==[]


@pytest.mark.asyncio
async def test_snapshot_middleware_freezes_stream_and_reports_store_failures(admin):
    draft=await ready(admin);await publish(admin,draft)
    app=FastAPI()
    @app.get('/api/v1/test')
    async def endpoint():return {'release':_current.get().release_id}
    app.add_middleware(SnapshotMiddleware,session_factory=database_factory(admin))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.get('/api/v1/test')
        assert response.json()['release']==admin.rows(t.ai_config_releases)[0]['id']
    assert _current.get() is None


@pytest.mark.asyncio
async def test_outbox_failure_retries_bounded_and_stale_event_cannot_move_head(admin):
    from app.model_config.outbox import ConfigOutboxWorker
    draft=await ready(admin);await publish(admin,draft)
    callback=AsyncMock(side_effect=RuntimeError('test failure'))
    worker=ConfigOutboxWorker(admin.service,database_factory(admin),callback)
    for _ in range(5):
        admin.sql(t.outbox_events.update().values(next_attempt_at=utcnow()-timedelta(seconds=1)))
        assert await worker.tick()
    row=admin.rows(t.outbox_events)[0]
    assert row['attempts']==5 and row['status']=='dead_letter'
    assert admin.rows(t.ai_config_heads)[0]['head_version']==1


@pytest.mark.asyncio
async def test_nonempty_cross_role_fallback_is_blocked_until_original_role_evidence_exists(admin):
    draft=await ready(admin)
    binding=copy.deepcopy(draft['payload']['role_bindings_raw']['session_title'])
    binding.update(enabled=True,selection='explicit',profile_revision_id=draft['payload']['role_bindings_raw']['chat']['profile_revision_id'],
        credential_revision_id=draft['payload']['role_bindings_raw']['chat']['credential_revision_id'],failure_mode='default_title')
    primary=copy.deepcopy(draft['payload']['role_bindings_raw']['chat']);primary['fallback']=['session_title']
    from test_model_config_control_plane import prompt_op
    draft,_=await admin.call('patch_draft',draft['id'],{'operations':[
        {'op':'replace_binding','role_key':'session_title','binding':binding},
        prompt_op('session_title'),{'op':'replace_binding','role_key':'chat','binding':primary}]},'fallback',draft['etag'])
    with pytest.raises(BusinessError) as error:await publish(admin,draft)
    assert error.value.data['reason']=='FALLBACK_DISTINCT_MODEL_REQUIRED'
    assert admin.count(t.ai_config_releases)==0


@pytest.mark.asyncio
async def test_published_failure_policy_and_legacy_rule_behavior_are_distinct(admin):
    from app.model_config.runtime import require_failure_policy
    draft=await ready(admin);await publish(admin,draft)
    with selected(admin,await snapshot(admin)):
        with pytest.raises(BusinessError):require_failure_policy('chat','disabled')
        require_failure_policy('classifier','rule_only')  # explicitly disabled classifier
    require_failure_policy('chat','disabled')  # legacy behavior before first publication


@pytest.mark.asyncio
async def test_published_snapshot_does_not_fall_back_to_legacy_embedding_cache_or_model(admin):
    import hashlib
    import time
    from app.rag.vector_store import VectorStoreService
    from unittest.mock import Mock
    await publish(admin,await ready(admin))
    vector=object.__new__(VectorStoreService)
    vector.embed_model=Mock()
    vector._embedding_cache={hashlib.md5(b'cached document').hexdigest(): ([0.1], time.time())}
    vector._embedding_cache_ttl=300
    with selected(admin,await snapshot(admin)):
        with pytest.raises(BusinessError):await vector._generate_embeddings(['cached document'])
    vector.embed_model.aembed_documents.assert_not_called()


@pytest.mark.asyncio
async def test_configured_provider_is_not_priced_by_arbitrary_model_name(admin):
    from app.models.model_trace import ModelTrace,ModelPricing
    from app.services.usage_service import get_usage_summary
    ModelTrace.__table__.create(admin.engine);ModelPricing.__table__.create(admin.engine)
    with admin.engine.begin() as db:
        db.execute(ModelPricing.__table__.insert().values(model='same-name',input_price_per_1k=1,output_price_per_1k=2))
        for ident,release in [(1,None),(2,'new-release')]:
            db.execute(ModelTrace.__table__.insert().values(id=ident,user_id=admin.actor.user_id,stage='agent',
                model='same-name',release_id=release,is_test=False,prompt_tokens=1000,completion_tokens=1000))
    with Session(admin.engine) as session:data=await get_usage_summary(AsyncFacade(session),admin.actor.user_id)
    assert data['total_calls']==2 and data['total_cost_cny'] is None and data['unknown_cost_count']==1
    estimate = next(row for row in data['currency_totals'] if row['currency'] == 'CNY')
    assert estimate['legacy_estimated_amount'] == '3.0' and estimate['priced_amount'] is None
