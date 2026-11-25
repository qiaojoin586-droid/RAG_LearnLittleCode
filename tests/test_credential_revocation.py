"""Revocation transaction, replay, impact and actual credential-bound streams."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.credential_calls import CredentialCallRegistry,CredentialTransport,registry
from app.model_config.credential_service import CredentialService
from app.model_config.outbox import CONFIG_CHANNEL, ConfigOutboxWorker, CredentialRevocationWatcher
from test_model_config_control_plane import admin,store,api,AsyncFacade,profile_op,prompt_op  # noqa: F401
from test_model_config_publication import ready,publish,snapshot,selected,mock_runtime,database_factory
from test_model_config_validation import validation_service,execute,prepare,fixture_handler
from test_vector_binding_runtime import vectors  # noqa: F401


async def revoke(admin,credential_id,*,key='revoke',reason='suspected_leak',proof=True,actor=None):
    with Session(admin.engine) as session:
        return await CredentialService(admin.service).revoke(AsyncFacade(session),actor or admin.actor,credential_id,
            {'reason':reason},key,admin.proof() if proof is True else proof or None)


async def impact(admin,credential_id):
    with Session(admin.engine) as session:return await CredentialService(admin.service).impact(AsyncFacade(session),admin.actor,credential_id)


async def candidate(admin):
    draft=await admin.draft();policy=await admin.policy()
    draft,_=await admin.call('patch_draft',draft['id'],{'operations':[profile_op(policy)]},'candidate',draft['etag'],admin.proof())
    return draft,admin.rows(t.ai_secrets)[0]['id']


@pytest.mark.asyncio
async def test_candidate_revoke_atomic_audit_outbox_and_stable_epoch(admin):
    _,credential_id=await candidate(admin)
    reason='do not persist this free text fictional-secret-123456'
    result,status=await revoke(admin,credential_id,reason=reason)
    assert status==200 and result['dispatch_blocked'] and result['cancel_requested']
    assert result['revocation_epoch']==1 and not result['already_revoked']
    assert (await revoke(admin,credential_id,reason=reason,proof=False))[0]==result
    again,_=await revoke(admin,credential_id,key='again',reason='administrator_action')
    assert again['already_revoked'] and again['revocation_epoch']==1
    assert admin.count(t.outbox_events)==1 and admin.count(t.ai_config_releases)==0
    secret=admin.rows(t.ai_secrets)[0]
    assert secret['status']=='revoked' and secret['revoked_at'] is not None
    audit=next(row for row in admin.rows(t.ai_config_audits) if row['action']=='credential.revoke')
    assert audit['credential_revision_id']==credential_id and audit['credential_version']==1
    assert audit['redacted_diff']['reason_code']=='operator_supplied'
    for table in (t.ai_config_requests,t.ai_config_audits,t.outbox_events):
        assert reason not in str(admin.rows(table)) and 'fictional-secret-long-key' not in str(admin.rows(table))
    with pytest.raises(BusinessError) as error:await revoke(admin,credential_id,reason='different')
    assert error.value.code==40922


@pytest.mark.asyncio
async def test_revoke_requires_permission_and_token_bound_reauth_without_mutation(admin):
    _,credential_id=await candidate(admin)
    before=admin.count(t.ai_config_requests)
    with pytest.raises(BusinessError) as error:await revoke(admin,credential_id,proof=False)
    assert error.value.code==40110
    with pytest.raises(BusinessError) as error:await revoke(admin,credential_id,proof=admin.proof(admin.other))
    assert error.value.code==40111
    admin.sql(t.ai_config_admin_grants.update().values(config_role_key='config_operator'))
    with pytest.raises(BusinessError) as error:await revoke(admin,credential_id)
    assert error.value.code==40312
    assert admin.count(t.ai_config_requests)==before and admin.count(t.outbox_events)==0
    assert admin.rows(t.ai_secrets)[0]['status']=='candidate'


@pytest.mark.asyncio
async def test_failed_commit_boundary_does_not_cancel_or_revoke(admin,monkeypatch):
    _,credential_id=await candidate(admin)
    original=admin.service._audit
    async def fail_audit(*args,**kwargs):raise RuntimeError('simulated audit store failure')
    monkeypatch.setattr(admin.service,'_audit',fail_audit)
    cancel=AsyncMock();signals=[]
    monkeypatch.setattr(registry,'request_cancel',signals.append)
    with pytest.raises(RuntimeError):await revoke(admin,credential_id)
    assert admin.rows(t.ai_secrets)[0]['status']=='candidate' and not signals
    assert admin.count(t.outbox_events)==0
    monkeypatch.setattr(admin.service,'_audit',original)


@pytest.mark.asyncio
async def test_impact_and_historical_restore_do_not_bypass_revocation(admin):
    draft=await ready(admin,local=False);published,_=await publish(admin,draft)
    credential_id=admin.rows(t.ai_secrets)[0]['id']
    view=await impact(admin,credential_id)
    assert view['active_role_keys']==['chat'] and view['active_release_id']==published['release_id']
    assert view['referenced_draft_count']==1 and not view['provider_key_revocation_supported']
    assert 'ciphertext' not in str(view) and 'fictional-secret' not in str(view)
    pinned=await snapshot(admin)
    await revoke(admin,credential_id)
    from app.model_config.runtime import permit
    with selected(admin,pinned):
        with pytest.raises(BusinessError) as error:await permit(pinned,next(iter(pinned.profiles)),database_factory(admin))
    assert error.value.code==40926
    from app.model_config.publishing import PublishingService
    with Session(admin.engine) as session:
        with pytest.raises(BusinessError):await PublishingService(admin.service,database_factory(admin)).restore(AsyncFacade(session),admin.actor,
            published['release_id'],{'expected_head_version':1},'restore',admin.proof())
    assert admin.count(t.ai_config_releases)==1


@pytest.mark.asyncio
async def test_revocation_cancels_running_probe_and_late_result_is_terminal(admin):
    draft=await prepare(admin,'chat');credential_id=admin.rows(t.ai_secrets)[0]['id']
    async def reply(request):
        await revoke(admin,credential_id)
        return fixture_handler(request)
    service,requests=validation_service(admin,reply)
    result,_=await execute(admin,service,draft)
    assert result['status']=='cancelled' and result['result']['reason']=='CREDENTIAL_REVOKED'
    assert len(requests)==1 and (await execute(admin,service,draft))[0]==result
    assert not registry.count(credential_id)


class WaitingStream(httpx.AsyncByteStream):
    def __init__(self):self.entered=asyncio.Event();self.closed=False
    async def __aiter__(self):
        yield b'first'
        self.entered.set()
        await asyncio.Event().wait()
        yield b'unreachable'
    async def aclose(self):self.closed=True


@pytest.mark.asyncio
@pytest.mark.parametrize('broker_available', [True, False])
async def test_remote_inflight_call_recovers_when_outbox_was_consumed_elsewhere(admin, broker_available):
    _, credential_id = await candidate(admin)
    sessions = database_factory(admin)
    calls = CredentialCallRegistry()
    watcher = CredentialRevocationWatcher(sessions, calls=calls)
    stream = WaitingStream()
    transport = CredentialTransport(httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
                                    credential_id, calls=calls)

    async def read():
        async with httpx.AsyncClient(transport=transport) as client:
            async with client.stream('GET', 'https://example.invalid') as response:
                await response.aread()

    task = asyncio.create_task(read())
    try:
        await asyncio.wait_for(stream.entered.wait(), 2)
        await watcher.reconcile()
        assert not task.done() and calls.active_ids() == (credential_id,)
        await revoke(admin, credential_id)
        broker = SimpleNamespace(publish=AsyncMock(side_effect=None if broker_available else ConnectionError()))
        assert await ConfigOutboxWorker(admin.service, sessions, redis=broker).tick()
        broker.publish.assert_awaited_once_with(CONFIG_CHANNEL, 'changed')
        assert admin.rows(t.outbox_events)[0]['status'] == ('succeeded' if broker_available else 'retry')
        assert not task.done()
        watcher.start()
        with pytest.raises(BusinessError) as revoked:
            await asyncio.wait_for(task, 2)
        assert revoked.value.code == 40926
        assert stream.closed and not calls.active_ids()
    finally:
        await watcher.stop()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pubsub_wakes_local_reconciliation(admin):
    messages = asyncio.Queue()
    subscribed = asyncio.Event()

    class Subscriber:
        async def subscribe(self, channel):
            assert channel == CONFIG_CHANNEL
            subscribed.set()

        async def listen(self):
            yield await messages.get()
            await asyncio.Future()

    @asynccontextmanager
    async def pubsub(**_):
        yield Subscriber()

    watcher = CredentialRevocationWatcher(database_factory(admin), redis=SimpleNamespace(pubsub=pubsub))
    listener = asyncio.create_task(watcher._listen())
    try:
        await asyncio.wait_for(subscribed.wait(), 1)
        await asyncio.wait_for(watcher.wakeup.wait(), 1)
        watcher.wakeup.clear()
        await messages.put({'type': 'message', 'data': 'untrusted input is only a wake-up'})
        await asyncio.wait_for(watcher.wakeup.wait(), 1)
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


@pytest.mark.asyncio
async def test_local_cancel_stops_only_target_http_stream_without_cancelling_caller():
    calls=CredentialCallRegistry();stream=WaitingStream()
    transport=CredentialTransport(httpx.MockTransport(lambda _:httpx.Response(200,stream=stream)),'credential-a',calls=calls)
    async def read():
        async with httpx.AsyncClient(transport=transport) as client:
            async with client.stream('GET','https://example.invalid') as response:
                chunks=[]
                try:
                    async for chunk in response.aiter_bytes():chunks.append(chunk)
                except BusinessError as error:
                    assert error.code==40926
                    return chunks
    task=asyncio.create_task(read());await asyncio.wait_for(stream.entered.wait(),2)
    assert calls.count('credential-a')==1
    assert calls.request_cancel('different-credential')==0
    calls.request_cancel('credential-a')
    assert await asyncio.wait_for(task,2)==[b'first']
    assert stream.closed and not task.cancelled() and calls.count('credential-a')==0


@pytest.mark.asyncio
async def test_cancel_during_headers_and_external_cancellation_release_registrations():
    calls=CredentialCallRegistry();entered=asyncio.Event();finished=asyncio.Event()
    async def delayed(_):
        entered.set()
        try:await asyncio.Event().wait()
        finally:finished.set()
    async def request():
        async with httpx.AsyncClient(transport=CredentialTransport(httpx.MockTransport(delayed),'target',calls=calls)) as client:
            return await client.get('https://example.invalid')
    task=asyncio.create_task(request());await asyncio.wait_for(entered.wait(),2)
    calls.request_cancel('target')
    with pytest.raises(BusinessError):await asyncio.wait_for(task,2)
    assert finished.is_set() and calls.count('target')==0
    entered.clear();finished.clear()
    task=asyncio.create_task(request());await asyncio.wait_for(entered.wait(),2);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert finished.is_set() and calls.count('target')==0


@pytest.mark.asyncio
async def test_outbox_retry_can_deliver_revoke_without_any_active_release(admin):
    _,credential_id=await candidate(admin);await revoke(admin,credential_id)
    worker=ConfigOutboxWorker(admin.service,database_factory(admin))
    assert await worker.tick()
    assert admin.rows(t.outbox_events)[0]['status']=='succeeded'
    assert admin.rows(t.ai_config_heads)[0]['active_release_id'] is None


@pytest.mark.asyncio
async def test_http_impact_revoke_cache_control_reauth_and_no_secret_echo(api,admin):
    _,credential_id=await candidate(admin);client,_=api
    response=await client.get('/api/v1/model-config/credentials/'+credential_id+'/impact')
    assert response.status_code==200 and response.headers['Cache-Control']=='no-store'
    assert 'ciphertext' not in response.text
    response=await client.post('/api/v1/model-config/credentials/'+credential_id+'/revoke',json={'reason':'suspected_leak'},
        headers={'Idempotency-Key':'http-revoke','X-Admin-Reauth':admin.proof()})
    assert response.status_code==200 and response.json()['data']['dispatch_blocked']
    assert response.headers['Cache-Control']=='no-store'
    bad=await client.post('/api/v1/model-config/credentials/'+credential_id+'/revoke',json={'reason':'secret-invalid','api_key':'do-not-echo'},
        headers={'Idempotency-Key':'bad','X-Admin-Reauth':admin.proof()})
    assert bad.status_code==422 and 'do-not-echo' not in bad.text


@pytest.mark.asyncio
async def test_real_langchain_stream_stops_with_revocation_error_and_closes_http(admin,monkeypatch):
    from app.model_config.runtime import model_for
    from langchain_core.messages import HumanMessage
    draft=await ready(admin,local=False);await publish(admin,draft)
    pinned=await snapshot(admin);credential_id=admin.rows(t.ai_secrets)[0]['id']
    entered=asyncio.Event();closed=[]
    class ModelStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            entered.set();await asyncio.Event().wait()
            yield b'data: [DONE]\n\n'
        async def aclose(self):closed.append(True)
    mock_runtime(admin,monkeypatch,lambda _:httpx.Response(200,stream=ModelStream()))
    async def read():
        chunks=[]
        with selected(admin,pinned):
            try:
                async for part in model_for('chat').astream([HumanMessage(content='private message')]):chunks.append(part.content)
            except BusinessError as error:
                assert error.code==40926;return chunks
        raise AssertionError('A revoked in-flight model must not report success')
    task=asyncio.create_task(read());waiting=asyncio.create_task(entered.wait())
    try:
        await asyncio.wait_for(asyncio.wait((task,waiting),return_when=asyncio.FIRST_COMPLETED),10)
        if task.done():await task
        assert entered.is_set()
        await revoke(admin,credential_id)
        assert await asyncio.wait_for(task,3)==['first']
    finally:
        for running in (task,waiting):
            if not running.done():running.cancel()
        await asyncio.gather(task,waiting,return_exceptions=True)
    assert closed and not registry.count(credential_id)


@pytest.mark.asyncio
async def test_actual_revoke_blocks_vector_warm_cache_and_does_not_target_no_key_ollama(vectors):
    from app.model_config import vector_runtime as vr
    v=vectors
    profile_id=v.snapshot.manifest['index_bindings']['rag']['query_profile_revision_id']
    credential_id=v.snapshot.profiles[profile_id]['credential_revision_id']
    with selected(v.admin,v.snapshot):await vr.embeddings(['cached'],'rag')
    if credential_id is None:
        with pytest.raises(BusinessError) as error:await revoke(v.admin,profile_id)
        assert error.value.http_status==404
        with selected(v.admin,v.snapshot):assert await vr.embeddings(['cached'],'rag')
    else:
        sent=len(v.requests);await revoke(v.admin,credential_id)
        with selected(v.admin,v.snapshot):
            with pytest.raises(BusinessError) as error:await vr.embeddings(['cached'],'rag')
        assert error.value.code==40926 and len(v.requests)==sent


@pytest.mark.asyncio
async def test_legacy_revoked_epoch_zero_is_already_blocked_and_idempotent(admin):
    _,credential_id=await candidate(admin)
    admin.sql(t.ai_secrets.update().values(status='revoked',revocation_epoch=0))
    result,_=await revoke(admin,credential_id)
    assert result['already_revoked'] and result['revocation_epoch']==0 and result['dispatch_blocked']
    assert admin.count(t.outbox_events)==0


@pytest.mark.asyncio
async def test_missing_target_or_reason_do_not_create_request_audit_or_event(admin):
    _,credential_id=await candidate(admin)
    count=admin.count(t.ai_config_requests)
    with pytest.raises(BusinessError) as error:await revoke(admin,'missing')
    assert error.value.http_status==404
    with pytest.raises(BusinessError) as error:await revoke(admin,credential_id,reason='   ')
    assert error.value.http_status==422
    assert admin.count(t.ai_config_requests)==count and admin.count(t.outbox_events)==0


@pytest.mark.asyncio
async def test_revoked_active_config_can_be_cloned_repaired_tested_and_published(admin):
    draft=await ready(admin,local=False);published,_=await publish(admin,draft)
    credential_id=admin.rows(t.ai_secrets)[0]['id'];await revoke(admin,credential_id)
    repair,_=await admin.call('create_draft',{'base_release_id':published['release_id'],'expected_head_version':1},'repair-draft')
    assert 'CREDENTIAL_REVOKED' in repair['publication_blockers']
    # Partial work is retained without making the broken configuration usable.
    repair,_=await admin.call('patch_draft',repair['id'],{'operations':[prompt_op(text='repaired prompt')]},'repair-prompt',repair['etag'])
    with pytest.raises(BusinessError):await publish(admin,repair,'bad-repair')
    policy=admin.rows(t.ai_endpoint_policies)[0]
    repaired,_=await admin.call('patch_draft',repair['id'],{'operations':[profile_op(policy,key='fictional-new-key-repaired')]},
        'repair-key',repair['etag'],admin.proof())
    assert 'CREDENTIAL_REVOKED' not in repaired['publication_blockers']
    service,_=validation_service(admin)
    assert (await execute(admin,service,repaired,key='repair-test'))[0]['status']=='passed'
    assert (await publish(admin,repaired,'repair-publish'))[0]['head_version']==2
    assert next(row for row in admin.rows(t.ai_secrets) if row['id']==credential_id)['status']=='revoked'
