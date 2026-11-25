"""Probes use local HTTP fixtures only. No live providers, keys or model downloads."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.provider import ProviderGateway
from app.model_config.service import utcnow
from app.model_config.transport import GuardedTransport, LimitedStream, PolicyNetworkBackend, allowed_address
from app.model_config.validation_service import ValidationService
from test_model_config_control_plane import admin,store,profile_op,prompt_op,AsyncFacade  # noqa: F401


def fixture_handler(request):
    body = json.loads(request.content)
    native = request.url.path.endswith('/api/chat')
    if request.url.path.endswith(('/embeddings','/api/embed','/text-embedding')):
        if request.url.path.endswith('/api/embed'):
            value = {'embeddings':[[1.,2.],[3.,4.]]}
        elif request.url.path.endswith('/text-embedding'):
            value = {'output':{'embeddings':[{'text_index':1,'embedding':[3.,4.]},{'text_index':0,'embedding':[1.,2.]}]}}
        else:
            value = {'data':[{'index':1,'embedding':[3.,4.]},{'index':0,'embedding':[1.,2.]}]}
        return httpx.Response(200,json=value)
    if body.get('stream'):
        return httpx.Response(200,text='{"message":{"content":"fixture response"},"done":false}\n{"message":{},"done":true}\n' if native
            else 'data: {"choices":[{"delta":{"content":"fixture response"}}]}\n\ndata: [DONE]\n\n')
    if body.get('tools'):
        nonce = re.search(r'nonce ([0-9a-f]+)',body['messages'][0]['content'])[1]
        call = {'function':{'name':'echo_test','arguments':{'nonce':nonce} if native else json.dumps({'nonce':nonce})}}
        if not native:
            call.update(id='call-probe',type='function')
        message = {'content':'','tool_calls':[call]}
    elif body['messages'][-1]['role']=='tool':
        message = {'content':body['messages'][-1]['content']}
    else:
        content=body['messages'][0]['content']
        prefix='Return this fixed test JSON exactly: '
        message = {'content':content[len(prefix):] if content.startswith(prefix) else '{"ok":true,"label":"probe"}'}
    return httpx.Response(200,json={'message':message,'done':True} if native else {'choices':[{'message':message}]})


def validation_service(admin,handler=fixture_handler):
    @asynccontextmanager
    async def database():
        with Session(admin.engine) as session:
            yield AsyncFacade(session)
    requests = []
    def gateway(profile,policy,key,*,permit):
        async def request(request):
            await permit()
            requests.append(request)
            result = handler(request)
            return await result if hasattr(result,'__await__') else result
        return ProviderGateway(profile,policy,key,permit=permit,transport=httpx.MockTransport(request))
    return ValidationService(admin.service,database,gateway_factory=gateway),requests


async def prepare(admin,role='chat',*,local=False,protocol=None):
    draft,policy = await admin.draft(),await admin.policy(local=local)
    operation = profile_op(policy,local=local)
    operation['role_key'] = role
    if protocol:
        operation['profile']['api_protocol'] = protocol
    if role=='embedding':
        operation['profile'].update(model_kind='embedding',api_protocol='ollama_embed' if local else 'openai_embeddings',
                                    embedding_dimension=2,parameters={})
        operation['index_keys'] = []
    view,_ = await admin.call('patch_draft',draft['id'],{'operations':[operation]},'prepare',draft['etag'],
                              None if local else admin.proof())
    return view


async def execute(admin,service,draft,role='chat',key='test',budget='standard'):
    with Session(admin.engine) as session:
        return await service.test_draft(AsyncFacade(session),admin.actor,draft['id'],{
            'role_key':role,'expected_draft_version':draft['draft_version'],'budget':budget},key,draft['etag'])


@pytest.mark.asyncio
@pytest.mark.parametrize('role,local,expected_calls',[
    ('chat',False,3),('chat',True,3),('vision',False,3),('classifier',False,1),
    ('memory_extraction',True,1),('session_title',False,1),('embedding',False,1),('embedding',True,1),
])
async def test_role_probes_real_payloads_budgets_and_durable_replay(admin,role,local,expected_calls):
    draft = await prepare(admin,role,local=local)
    service,requests = validation_service(admin)
    result,status = await execute(admin,service,draft,role)
    assert status==200 and result['status']=='passed',result
    assert len(requests)==expected_calls and result['result']['call_count']==expected_calls
    assert result['result']['business_prompt_contract_verified'] is False
    assert result['result']['usage'] is None
    replay,status = await execute(admin,service,draft,role)
    assert replay==result and status==200 and len(requests)==expected_calls
    assert admin.rows(t.ai_config_validations)[0]['valid_until']>utcnow()
    view = await admin.call('get_draft',draft['id'])
    assert len(view['validation_coverage'])==1
    secret = 'fictional-secret-long-key-123456'
    for value in (result,admin.rows(t.ai_config_validations),admin.rows(t.ai_config_requests),admin.rows(t.ai_config_audits)):
        assert secret not in str(value) and 'ciphertext' not in str(value)
    for request in requests:
        body = json.loads(request.content)
        if role!='embedding':
            assert body.get('max_tokens',body.get('options',{}).get('num_predict'))<=96
        assert 'help' not in request.content.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,reason',[(401,'PROVIDER_AUTH_FAILED'),(429,'PROVIDER_RATE_LIMITED'),(302,'PROVIDER_HTTP_ERROR')])
async def test_no_retry_redirect_or_provider_error_echo(admin,status,reason):
    draft = await prepare(admin)
    service,requests = validation_service(admin,lambda _:httpx.Response(status,text='fictional-secret-do-not-echo',headers={'Location':'http://169.254.169.254'}))
    result,_ = await execute(admin,service,draft)
    assert result['status']=='unverified' and result['result']['reason']==reason
    assert len(requests)==1 and 'fictional-secret-do-not-echo' not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('body',[
    'data: {"choices":[{"delta":{"content":"prefix"}}]}\n\n',
    'data: [DONE]\n\n',
    'data: {"error":{"message":"private-provider-message"}}\n\ndata: [DONE]\n\n',
])
async def test_partial_empty_and_error_stream_never_pass(admin,body):
    draft = await prepare(admin)
    service,_ = validation_service(admin,lambda _:httpx.Response(200,text=body))
    result,_ = await execute(admin,service,draft)
    assert result['status']=='unverified'
    assert 'private-provider-message' not in str(result)


@pytest.mark.asyncio
async def test_patch_while_probe_is_running_invalidates_result(admin):
    draft = await prepare(admin,'session_title')
    async def changed(request):
        await admin.call('patch_draft',draft['id'],{'operations':[prompt_op()]},'concurrent-patch',draft['etag'])
        return fixture_handler(request)
    service,_ = validation_service(admin,changed)
    result,_ = await execute(admin,service,draft,'session_title')
    assert result['status']=='unverified' and result['result']['reason']=='VALIDATION_TARGET_CHANGED'
    assert (await admin.call('get_draft',draft['id']))['validation_coverage']==[]


@pytest.mark.asyncio
async def test_revoke_mid_probe_blocks_next_send_and_cannot_leave_passed_evidence(admin):
    draft = await prepare(admin)
    first = True
    def revoke(request):
        nonlocal first
        if first:
            admin.sql(t.ai_secrets.update().values(status='revoked',revocation_epoch=1))
            first = False
        return fixture_handler(request)
    service,requests = validation_service(admin,revoke)
    result,_ = await execute(admin,service,draft)
    assert result['status']=='unverified' and len(requests)==1
    assert result['valid_until'] is None


@pytest.mark.asyncio
async def test_lease_lost_and_current_permission_changes_cannot_pass(admin):
    draft = await prepare(admin,'session_title')
    def changed(request):
        admin.sql(t.ai_config_admin_grants.update().values(config_role_key='config_reader'))
        return fixture_handler(request)
    service,_ = validation_service(admin,changed)
    result,_ = await execute(admin,service,draft,'session_title')
    assert result['status']=='unverified' and result['result']['reason']=='AUTHORIZATION_CHANGED'


@pytest.mark.asyncio
async def test_concurrent_same_key_running_returns_same_202_without_second_network_run(admin):
    draft = await prepare(admin,'session_title')
    entered,release = asyncio.Event(),asyncio.Event()
    async def delayed(request):
        entered.set(); await release.wait(); return fixture_handler(request)
    service,requests = validation_service(admin,delayed)
    task = asyncio.create_task(execute(admin,service,draft,'session_title'))
    await entered.wait()
    running,status = await execute(admin,service,draft,'session_title')
    assert status==202 and running['status']=='running' and len(requests)==1
    release.set()
    result,_ = await task
    assert result['id']==running['id'] and result['status']=='passed'


@pytest.mark.asyncio
async def test_crashed_probe_expiry_becomes_unverified_never_auto_replayed(admin):
    draft = await prepare(admin,'session_title')
    service,requests = validation_service(admin)
    original_finish = service._finish
    async def crash(*args):
        raise RuntimeError('simulated process loss after network')
    service._finish = crash
    with pytest.raises(RuntimeError):
        await execute(admin,service,draft,'session_title')
    row = admin.rows(t.ai_config_validations)[0]
    assert row['status']=='running'
    admin.sql(t.ai_config_validations.update().values(deadline_at=utcnow()-timedelta(seconds=1)))
    with Session(admin.engine) as session:
        result = await service.get(AsyncFacade(session),admin.actor,row['id'])
    assert result['status']=='unverified' and result['result']['reason']=='PROBE_RESULT_UNKNOWN'
    service._finish = original_finish
    again,_ = await execute(admin,service,draft,'session_title')
    assert again==result and len(requests)==1


@pytest.mark.asyncio
async def test_provider_timeout_and_cancellation_are_finalized_and_release_tasks(admin):
    draft = await prepare(admin,'session_title')
    entered = asyncio.Event()
    async def delayed(_):
        entered.set(); await asyncio.Event().wait()
    service,_ = validation_service(admin,delayed)
    task = asyncio.create_task(execute(admin,service,draft,'session_title'))
    await entered.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = admin.rows(t.ai_config_validations)[0]
    assert row['status']=='unverified' and row['result']['reason']=='PROBE_INTERRUPTED'
    assert not any(not task.done() and '_heartbeat' in str(task.get_coro()) for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_local_cold_budget_is_not_accepted_for_cloud(admin):
    draft = await prepare(admin,'session_title')
    service,requests = validation_service(admin)
    with pytest.raises(BusinessError) as error:
        await execute(admin,service,draft,'session_title',budget='local_cold')
    assert error.value.code==40002 and not requests


def policy(private=False):
    return {'policy_key':'fixture','origin':'http://localhost:11434' if private else 'https://models.example.com',
        'allowed_path_prefix':'/' if private else '/v1','address_mode':'trusted_private' if private else 'public_https',
        'connection_rules':{'allowed_cidrs':['127.0.0.1/32','::1/128']} if private else {'allowed_cidrs':[]},
        'environment':'development'}


@pytest.mark.parametrize('address',['127.0.0.1','169.254.169.254','100.100.100.200','168.63.129.16',
                                    '::ffff:127.0.0.1','::1','224.0.0.1','0.0.0.0'])
def test_public_policy_denies_internal_metadata_and_mapped_addresses(address):
    assert not allowed_address(address,policy())


def test_private_policy_only_allows_explicit_ranges_including_ipv6_loopback():
    assert allowed_address('127.0.0.1',policy(True)) and allowed_address('::1',policy(True))
    assert not allowed_address('127.0.0.2',policy(True))


@pytest.mark.asyncio
async def test_network_backend_dials_vetted_ip_once_and_leaves_tls_hostname_to_httpcore():
    stream = SimpleNamespace(get_extra_info=lambda _:('8.8.8.8',443),aclose=AsyncMock())
    backend = SimpleNamespace(connect_tcp=AsyncMock(return_value=stream))
    resolver = AsyncMock(return_value=['8.8.8.8'])
    guarded = PolicyNetworkBackend(policy(),resolver=resolver,backend=backend)
    assert await guarded.connect_tcp('models.example.com',443) is stream
    assert backend.connect_tcp.call_args.args==('8.8.8.8',443)
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_mixed_dns_rebinding_and_peer_mismatch_fail_closed():
    backend = SimpleNamespace(connect_tcp=AsyncMock())
    guarded = PolicyNetworkBackend(policy(),resolver=AsyncMock(return_value=['8.8.8.8','127.0.0.1']),backend=backend)
    with pytest.raises(BusinessError):
        await guarded.connect_tcp('models.example.com',443)
    backend.connect_tcp.assert_not_awaited()
    stream = SimpleNamespace(get_extra_info=lambda _:('127.0.0.1',443),aclose=AsyncMock())
    backend.connect_tcp.return_value=stream
    guarded.resolver=AsyncMock(return_value=['8.8.8.8'])
    with pytest.raises(BusinessError):
        await guarded.connect_tcp('models.example.com',443)
    stream.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_transport_rechecks_permission_and_refuses_redirect_targets_before_network():
    permit = AsyncMock()
    transport = GuardedTransport(policy(),permit=permit)
    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{}'
    transport.pool.handle_async_request = AsyncMock(return_value=httpcore.Response(200,content=Bytes()))
    async with httpx.AsyncClient(transport=transport,follow_redirects=False) as client:
        response = await client.post('https://models.example.com/v1/chat/completions',json={})
        assert response.status_code==200 and permit.await_count==1
        with pytest.raises(BusinessError):
            await client.post('http://169.254.169.254/latest',json={})
        assert transport.pool.handle_async_request.await_count==1


@pytest.mark.asyncio
async def test_response_byte_budget_is_enforced_before_json_or_line_buffering():
    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'x'*(2*1024*1024+1)
    stream = LimitedStream(Bytes())
    with pytest.raises(BusinessError):
        async for _ in stream:
            pass


@pytest.mark.asyncio
async def test_each_provider_call_emits_redacted_test_trace_with_unknown_cost(admin,monkeypatch):
    from app.core import model_trace
    events = []
    monkeypatch.setattr(model_trace,'_emit',events.append)
    draft = await prepare(admin)
    service,_ = validation_service(admin)
    await execute(admin,service,draft)
    assert len(events)==3
    for event in events:
        assert event['is_test'] is True and event['stage']=='config_test'
        assert event['user_id']==admin.actor.user_id
        assert event['profile_revision_id'] and event['credential_revision_id'] and event['step_id']
        assert event['usage_status']=='unknown' and event['total_tokens'] is None
        assert event['pricing_status']=='unknown' and event['cost_amount'] is None
        assert 'fictional-secret' not in str(event) and 'Authorization' not in str(event)


@pytest.mark.asyncio
async def test_another_key_cannot_start_duplicate_role_probe(admin):
    draft = await prepare(admin,'session_title')
    entered,release = asyncio.Event(),asyncio.Event()
    async def delayed(request):
        entered.set(); await release.wait(); return fixture_handler(request)
    service,requests = validation_service(admin,delayed)
    task = asyncio.create_task(execute(admin,service,draft,'session_title'))
    await entered.wait()
    try:
        with pytest.raises(BusinessError) as error:
            await execute(admin,service,draft,'session_title',key='other-key')
        assert error.value.code==40923 and len(requests)==1
    finally:
        release.set(); await task


@pytest.mark.asyncio
async def test_test_usage_is_separate_from_business_and_never_estimated_as_free(admin):
    from app.models.model_trace import ModelTrace, ModelPricing
    from app.services.usage_service import get_usage_summary
    ModelTrace.__table__.create(admin.engine)
    ModelPricing.__table__.create(admin.engine)
    with admin.engine.begin() as connection:
        connection.execute(ModelTrace.__table__.insert(),[
            dict(id=1,user_id=admin.actor.user_id,stage='agent',model='fixture',is_test=False,
                 prompt_tokens=10,completion_tokens=5,usage_status='complete',latency_ms=100),
            dict(id=2,user_id=admin.actor.user_id,stage='config_test',model='fixture',is_test=True,
                 prompt_tokens=None,completion_tokens=None,usage_status='unknown',latency_ms=200),
        ])
    with Session(admin.engine) as session:
        data = await get_usage_summary(AsyncFacade(session),admin.actor.user_id)
    assert data['total_calls']==1 and data['total_tokens']==15
    assert data['config_test_usage']['calls']==1 and data['config_test_usage']['unknown_usage_count']==1
    assert data['config_test_usage']['cost_amount'] is None


@pytest.mark.asyncio
async def test_tls_receives_original_hostname_after_numeric_connect():
    events = []
    class Stream(httpcore.AsyncNetworkStream):
        async def read(self,max_bytes,timeout=None):
            return b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}'
        async def write(self,buffer,timeout=None):
            events.append(('write',buffer))
        async def aclose(self):
            pass
        async def start_tls(self,ssl_context,server_hostname=None,timeout=None):
            events.append(('sni',server_hostname)); return self
        def get_extra_info(self,info):
            return ('8.8.8.8',443) if info=='server_addr' else None
    backend = SimpleNamespace(connect_tcp=AsyncMock(return_value=Stream()))
    transport = GuardedTransport(policy(),permit=AsyncMock(),resolver=AsyncMock(return_value=['8.8.8.8']),backend=backend)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post('https://models.example.com/v1/chat/completions',json={})
        assert response.json()=={}
    assert backend.connect_tcp.call_args.args[0]=='8.8.8.8'
    assert ('sni','models.example.com') in events


@pytest.mark.asyncio
async def test_http_test_route_and_validation_query_use_real_control_permission(admin,monkeypatch):
    from fastapi import FastAPI
    from app.core.failed_response_register import register_exception_handlers
    from app.model_config import router as module
    draft = await prepare(admin,'session_title')
    validator,_ = validation_service(admin)
    application = FastAPI(); register_exception_handlers(application)
    application.include_router(module.router,prefix='/api/v1')
    async def database():
        with Session(admin.engine) as session:
            yield AsyncFacade(session)
    application.dependency_overrides[module.principal]=lambda:admin.actor
    application.dependency_overrides[module.config_db]=database
    monkeypatch.setattr(module,'validator',lambda:validator)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url='http://test') as client:
        response = await client.post('/api/v1/model-config/drafts/'+draft['id']+'/test',
            json={'role_key':'session_title','expected_draft_version':draft['draft_version']},
            headers={'If-Match':draft['etag'],'Idempotency-Key':'http-probe'})
        assert response.status_code==200 and response.json()['data']['status']=='passed'
        identifier = response.json()['data']['id']
        assert (await client.get('/api/v1/model-config/validations/'+identifier)).json()['data']['status']=='passed'
        admin.sql(t.ai_config_admin_grants.update().values(config_role_key='config_reader'))
        rejected = await client.post('/api/v1/model-config/drafts/'+draft['id']+'/test',
            json={'role_key':'session_title','expected_draft_version':draft['draft_version']},
            headers={'If-Match':draft['etag'],'Idempotency-Key':'different-probe'})
        assert rejected.status_code==403


@pytest.mark.asyncio
async def test_boolean_embedding_indices_cannot_pass_as_integer_positions(admin):
    draft = await prepare(admin,'embedding')
    service,_ = validation_service(admin,lambda _:httpx.Response(200,json={
        'data':[{'index':False,'embedding':[1.,2.]},{'index':True,'embedding':[3.,4.]}]}))
    result,_ = await execute(admin,service,draft,'embedding')
    assert result['status']=='unverified' and result['result']['reason']=='EMBEDDING_ORDER_INVALID'
