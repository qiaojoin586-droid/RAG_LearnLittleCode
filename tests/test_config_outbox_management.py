"""Notification redrive preserves history and cannot replay configuration writes."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.outbox import ConfigOutboxWorker
from app.model_config.outbox_service import OutboxService
from app.model_config.service import utcnow, uid
from test_model_config_control_plane import store, admin, api, AsyncFacade  # noqa: F401
from test_model_config_publication import ready, publish, database_factory
from test_credential_revocation import revoke


async def event(admin,kind='model_config.published'):
    await publish(admin,await ready(admin,local=False))
    if kind=='credential.revoked':await revoke(admin,admin.rows(t.ai_secrets)[0]['id'])
    row=next(dict(row) for row in admin.rows(t.outbox_events) if row['event_type']==kind)
    admin.sql(t.outbox_events.update().where(t.outbox_events.c.id==row['id']).values(
        status='dead_letter',attempts=5,error_code='CONFIG_NOTIFY_FAILED'))
    return row['id']


async def call(admin,method,*args,**kwargs):
    with Session(admin.engine) as session:
        return await getattr(OutboxService(admin.service),method)(AsyncFacade(session),admin.actor,*args,**kwargs)


async def redrive(admin,event_id,*,key='redrive',body=None,proof=True):
    return await call(admin,'redrive',event_id,body or {'expected_attempts':5,'reason':'operator_retry'},key,
                      admin.proof() if proof is True else proof)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['model_config.published','credential.revoked'])
async def test_append_only_redrive_replay_delivers_without_republishing_or_unrevoking(admin,kind):
    original=await event(admin,kind);before=admin.rows(t.outbox_events)
    head=dict(admin.rows(t.ai_config_heads)[0]);secrets=[dict(row) for row in admin.rows(t.ai_secrets)]
    result,status=await redrive(admin,original)
    assert status==200 and result['status']=='pending' and not result['config_head_changed']
    assert result['source_event_id']==original and result['event_id']!=original
    assert (await redrive(admin,original,proof=None))[0]==result
    assert admin.count(t.outbox_events)==len(before)+1
    parent=next(row for row in admin.rows(t.outbox_events) if row['id']==original)
    assert parent['status']=='dead_letter' and parent['attempts']==5
    with pytest.raises(BusinessError) as error:await redrive(admin,original,key='duplicate-new-key')
    assert error.value.data['reason']=='OUTBOX_ALREADY_REDRIVEN'
    worker=ConfigOutboxWorker(admin.service,database_factory(admin))
    for _ in range(4):
        if not await worker.tick():break
    assert (await call(admin,'get',result['event_id']))['status']=='succeeded'
    assert (await call(admin,'get',original))['redrive_event_id']==result['event_id']
    assert admin.rows(t.ai_config_heads)[0]['head_version']==head['head_version']
    assert admin.count(t.ai_config_releases)==1 and [dict(row) for row in admin.rows(t.ai_secrets)]==secrets
    assert (await redrive(admin,original,proof=None))[0]==result  # receipt is not live status
    with pytest.raises(BusinessError) as error:
        await redrive(admin,original,body={'expected_attempts':4,'reason':'operator_retry'})
    assert error.value.code==40922


@pytest.mark.asyncio
async def test_redrive_and_audit_roll_back_together(admin,monkeypatch):
    original=await event(admin);count=admin.count(t.ai_config_requests)
    async def broken(*args,**kwargs):raise RuntimeError('simulated audit outage')
    monkeypatch.setattr(admin.service,'_audit',broken)
    with pytest.raises(RuntimeError):await redrive(admin,original)
    assert admin.count(t.outbox_events)==1 and admin.count(t.ai_config_requests)==count


@pytest.mark.asyncio
@pytest.mark.parametrize('role',['config_reader','config_operator'])
async def test_outbox_management_requires_admin_and_new_redrive_requires_reauth(admin,role):
    original=await event(admin)
    with pytest.raises(BusinessError) as error:await redrive(admin,original,proof=None)
    assert error.value.code==40110
    with pytest.raises(BusinessError) as error:await redrive(admin,original,proof=admin.proof(admin.other))
    assert error.value.code==40111
    admin.sql(t.ai_config_admin_grants.update().values(config_role_key=role))
    for method,args in [('list',()),('get',(original,))]:
        with pytest.raises(BusinessError) as error:await call(admin,method,*args)
        assert error.value.code==40312
    with pytest.raises(BusinessError) as error:await redrive(admin,original)
    assert error.value.code==40312 and admin.count(t.outbox_events)==1


@pytest.mark.asyncio
async def test_redacted_views_paginate_filter_scope_and_hide_payload_lease_and_unknown_errors(admin):
    original=await event(admin)
    parent=next(dict(row) for row in admin.rows(t.outbox_events) if row['id']==original)
    for scope in ['system','another']:
        admin.sql(t.outbox_events.insert().values(**{**parent,'id':uid(),'scope_id':scope,'event_key':uid(),
            'error_code':'fictional-sensitive-provider-message','payload':{'secret':'must-not-echo'}}))
    page=await call(admin,'list',status='dead_letter',limit=1)
    second=await call(admin,'list',status='dead_letter',cursor=page['next_cursor'],limit=1)
    assert len(page['items'])==len(second['items'])==1 and not second['next_cursor']
    assert page['items'][0]['id']!=second['items'][0]['id']
    assert not (await call(admin,'list',status='pending'))['items']
    text=str(page)+str(second)
    assert all(value not in text for value in ['must-not-echo','fictional-sensitive','payload','lease_owner','event_key'])
    foreign=next(row['id'] for row in admin.rows(t.outbox_events) if row['scope_id']=='another')
    with pytest.raises(BusinessError) as error:await call(admin,'get',foreign)
    assert error.value.http_status==404
    with pytest.raises(BusinessError):await call(admin,'list',status='unknown')


@pytest.mark.asyncio
async def test_redrive_rejects_changed_state_mismatched_attempts_invalid_payload_and_reused_key(admin):
    original=await event(admin)
    with pytest.raises(BusinessError) as error:await redrive(admin,original,body={'expected_attempts':4,'reason':'operator_retry'})
    assert error.value.data['reason']=='OUTBOX_STATE_CHANGED'
    admin.sql(t.outbox_events.update().values(status='succeeded'))
    with pytest.raises(BusinessError):await redrive(admin,original)
    admin.sql(t.outbox_events.update().values(status='dead_letter',payload={'unknown':'do-not-republish'}))
    with pytest.raises(BusinessError) as error:await redrive(admin,original)
    assert error.value.data['reason']=='OUTBOX_EVENT_INVALID'
    assert admin.count(t.outbox_events)==1


@pytest.mark.asyncio
async def test_worker_retries_only_when_due_then_stops_at_five_and_redrive_has_separate_budget(admin):
    await publish(admin,await ready(admin));callback=AsyncMock(side_effect=RuntimeError('never persisted provider text'))
    worker=ConfigOutboxWorker(admin.service,database_factory(admin),callback)
    for number in range(1,6):
        admin.sql(t.outbox_events.update().values(next_attempt_at=utcnow()-timedelta(seconds=1)))
        assert await worker.tick()
        row=dict(admin.rows(t.outbox_events)[0]);assert row['attempts']==number
        if number<5:assert not await worker.tick()
    assert row['status']=='dead_letter' and callback.await_count==5 and not await worker.tick()
    result,_=await redrive(admin,row['id'])
    assert await worker.tick() and callback.await_count==6
    assert (await call(admin,'get',result['event_id']))['attempts']==1
    assert (await call(admin,'get',row['id']))['attempts']==5


@pytest.mark.asyncio
async def test_cancelled_worker_recovers_expired_lease_and_final_crashed_attempt_becomes_dead_letter(admin):
    await publish(admin,await ready(admin));entered=asyncio.Event()
    async def block(_):entered.set();await asyncio.Event().wait()
    worker=ConfigOutboxWorker(admin.service,database_factory(admin),block)
    task=asyncio.create_task(worker.tick());await asyncio.wait_for(entered.wait(),3)
    task.cancel();await asyncio.gather(task,return_exceptions=True)
    row=dict(admin.rows(t.outbox_events)[0]);assert row['status']=='inflight' and row['attempts']==1
    admin.sql(t.outbox_events.update().values(lease_expires_at=utcnow()-timedelta(seconds=1)))
    assert await ConfigOutboxWorker(admin.service,database_factory(admin)).tick()
    assert (await call(admin,'get',row['id']))['attempts']==2
    admin.sql(t.outbox_events.update().values(status='inflight',attempts=5,lease_owner='crashed',
        lease_expires_at=utcnow()-timedelta(seconds=1)))
    assert await worker.tick()
    result=await call(admin,'get',row['id'])
    assert result['status']=='dead_letter' and result['error_code']=='CONFIG_NOTIFY_LEASE_EXPIRED' and result['lease_expires_at'] is None


@pytest.mark.asyncio
async def test_late_completion_cannot_override_new_attempt_or_expired_lease(admin):
    await publish(admin,await ready(admin))
    async def advanced(_):
        admin.sql(t.outbox_events.update().values(attempts=2,lease_owner='replacement-worker',
            lease_expires_at=utcnow()+timedelta(seconds=10)))
    assert await ConfigOutboxWorker(admin.service,database_factory(admin),advanced).tick()
    row=dict(admin.rows(t.outbox_events)[0]);assert row['attempts']==2 and row['status']=='inflight'
    admin.sql(t.outbox_events.update().values(lease_expires_at=utcnow()-timedelta(seconds=1)))
    async def expired(_):admin.sql(t.outbox_events.update().values(lease_expires_at=utcnow()-timedelta(seconds=1)))
    assert await ConfigOutboxWorker(admin.service,database_factory(admin),expired).tick()
    assert admin.rows(t.outbox_events)[0]['status']=='inflight'


@pytest.mark.asyncio
@pytest.mark.parametrize('loss',['owner','generation','expired'])
async def test_manager_loss_prevents_old_worker_completion(admin,loss):
    await publish(admin,await ready(admin))
    async def lost(_):
        changes=({'manager_owner_id':'another-manager'} if loss=='owner' else
            {'manager_generation':admin.rows(t.ai_config_heads)[0]['manager_generation']+1} if loss=='generation' else
            {'manager_lease_expires_at':utcnow()-timedelta(seconds=1)})
        admin.sql(t.ai_config_heads.update().values(**changes))
    with pytest.raises(BusinessError) as error:await ConfigOutboxWorker(admin.service,database_factory(admin),lost).tick()
    assert error.value.code==50304 and admin.rows(t.outbox_events)[0]['status']=='inflight'


@pytest.mark.asyncio
async def test_http_routes_require_jwt_reauth_and_strict_body_and_return_no_store(api,admin):
    client,_=api;original=await event(admin)
    response=await client.get('/api/v1/model-config/outbox',params={'status':'dead_letter'})
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    assert len(response.json()['data']['items'])==1
    path='/api/v1/model-config/outbox/'+original
    assert (await client.get(path)).json()['data']['id']==original
    body={'expected_attempts':5,'reason':'operator_retry'}
    headers={'Idempotency-Key':'http-redrive','X-Admin-Reauth':admin.proof()}
    invalid=await client.post(path+'/redrive',json={**body,'api_key':'must-not-echo'},headers=headers)
    assert invalid.status_code==422 and 'must-not-echo' not in invalid.text
    assert (await client.post(path+'/redrive',json=body,headers={'Idempotency-Key':'http-redrive'})).json()['code']==40110
    result=await client.post(path+'/redrive',json=body,headers=headers)
    assert result.status_code==200 and result.headers['cache-control']=='no-store'
    assert 'payload' not in result.text and admin.count(t.outbox_events)==2
