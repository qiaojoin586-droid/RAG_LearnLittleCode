"""Candidate GC keeps immutable history, leases and genuine dependency references."""
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.credential_calls import registry
from app.model_config.lifecycle import CandidateLifecycleWorker,SYSTEM_ACTOR
from app.model_config.lifecycle import configured_worker
from app.model_config.outbox_service import OutboxService
from app.model_config.security import SCOPE
from app.model_config.service import utcnow,uid
from test_model_config_control_plane import store,admin,AsyncFacade,profile_op,prompt_op  # noqa: F401
from test_model_config_publication import ready,publish,database_factory,snapshot
from test_credential_revocation import revoke


async def closed(admin):
    draft=await admin.draft();policy=await admin.policy()
    draft,_=await admin.call('patch_draft',draft['id'],{'operations':[profile_op(policy),prompt_op()]},
        'candidates',draft['etag'],admin.proof())
    await admin.call('abandon_draft',draft['id'],'abandon',draft['etag'])
    return draft


def aged(admin):
    for table in (t.ai_profile_revisions,t.ai_prompt_revisions,t.ai_secrets):
        admin.sql(table.update().where(table.c.retired_at.is_not(None)).values(retired_at=utcnow()-timedelta(days=8)))


def worker(admin,**options):return CandidateLifecycleWorker(admin.service,database_factory(admin),**options)


@pytest.mark.asyncio
async def test_retire_grace_purge_dependency_order_tombstone_and_monotone_versions(admin):
    draft=await closed(admin);requests=admin.count(t.ai_config_requests)
    collector=worker(admin,purge=True)
    assert (await collector.tick())['retired']==3
    assert all(row['status']=='retired' for table in (t.ai_secrets,t.ai_profile_revisions,t.ai_prompt_revisions) for row in admin.rows(table))
    assert (await collector.tick())['purged']==0
    aged(admin)
    assert (await worker(admin).tick())['purged']==0  # purge is a separate opt-in
    assert (await collector.tick())['purged']==3
    assert admin.count(t.ai_secrets)==admin.count(t.ai_profile_revisions)==admin.count(t.ai_prompt_revisions)==0
    assert admin.count(t.ai_config_draft_items)==1  # endpoint policy is not a GC target
    assert admin.count(t.ai_config_requests)==requests
    audits=[row for row in admin.rows(t.ai_config_audits) if row['action'].startswith('candidate.')]
    assert len(audits)==6 and all(row['actor_id']==SYSTEM_ACTOR for row in audits)
    assert 'fictional-secret' not in str(audits) and 'ciphertext' not in str(audits)
    with pytest.raises(BusinessError) as error:await admin.call('get_draft',draft['id'])
    assert error.value.http_status==410 and error.value.data['reason']=='DRAFT_REFERENCES_PURGED'
    new=await admin.draft('new-draft');policy=dict(admin.rows(t.ai_endpoint_policies)[0])
    await admin.call('patch_draft',new['id'],{'operations':[profile_op(policy)]},'new-profile',new['etag'],admin.proof())
    assert admin.rows(t.ai_secrets)[0]['credential_version']==2 and admin.rows(t.ai_profile_revisions)[0]['revision']==2


@pytest.mark.asyncio
async def test_expired_draft_is_terminal_and_active_draft_is_untouched(admin):
    draft=await closed(admin)
    admin.sql(t.ai_config_drafts.update().values(status='editing',expires_at=utcnow()-timedelta(seconds=1)))
    result=await worker(admin).tick()
    assert result['expired_drafts']==1 and result['retired']==3
    assert admin.rows(t.ai_config_drafts)[0]['status']=='expired'
    live=await admin.draft('live');assert (await worker(admin,purge=True).tick())['expired_drafts']==0
    assert next(row for row in admin.rows(t.ai_config_drafts) if row['id']==live['id'])['status']=='editing'


@pytest.mark.asyncio
async def test_published_history_and_index_references_are_never_collected_even_without_inventory(admin):
    draft=await ready(admin,local=False);result,_=await publish(admin,draft)
    before=await snapshot(admin)
    admin.sql(t.ai_config_release_refs.delete())
    admin.sql(t.ai_config_draft_items.delete())
    admin.sql(t.ai_config_validations.update().values(valid_until=utcnow()-timedelta(days=1),
        lease_expires_at=utcnow()-timedelta(days=1),deadline_at=utcnow()-timedelta(days=1)))
    # Simulate old retired published credentials; manifest is the second guard.
    admin.sql(t.ai_secrets.update().values(status='retired',retired_at=utcnow()-timedelta(days=10)))
    collector=worker(admin,purge=True);assert (await collector.tick())['purged']==0
    assert (await snapshot(admin)).release_id==before.release_id==result['release_id']
    assert admin.count(t.ai_secrets)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('reference',['inventory','payload','index'])
async def test_live_draft_inventory_actual_payload_and_build_profile_protect_assets(admin,reference):
    draft=await closed(admin);profile=dict(admin.rows(t.ai_profile_revisions)[0])
    if reference=='index':
        admin.sql(t.rag_index_versions.insert().values(id=uid(),**SCOPE,index_key='rag',index_version=1,collection_name='fixture',
            build_profile_revision_id=profile['id'],semantic_identity={},dimension=3,parser_version='p',chunker_version='c',
            status='baseline_unverified',created_at=utcnow()))
    else:
        live=await admin.draft('live')
        if reference=='inventory':
            admin.sql(t.ai_config_draft_items.insert().values(**SCOPE,draft_id=live['id'],entity_kind='profile',
                entity_id=profile['id'],created_at=utcnow()))
        else:admin.sql(t.ai_config_drafts.update().where(t.ai_config_drafts.c.id==live['id']).values(payload=draft['payload']))
    result=await worker(admin,purge=True).tick()
    assert result['protected']>=1 and admin.rows(t.ai_profile_revisions)[0]['status']=='candidate'
    assert admin.rows(t.ai_secrets)[0]['status']=='retired' or reference=='payload'


@pytest.mark.asyncio
async def test_validation_lease_and_passed_evidence_block_retirement_until_expiry(admin):
    draft=await ready(admin,local=False)
    await admin.call('abandon_draft',draft['id'],'abandon',draft['etag'])
    assert (await worker(admin,purge=True).tick())['retired']==0
    admin.sql(t.ai_config_validations.update().values(valid_until=utcnow()-timedelta(seconds=1),
        lease_expires_at=utcnow()+timedelta(minutes=1)))
    assert (await worker(admin,purge=True).tick())['retired']==0
    admin.sql(t.ai_config_validations.update().values(lease_expires_at=utcnow()-timedelta(seconds=1)))
    assert (await worker(admin).tick())['retired']==3
    assert admin.rows(t.ai_config_validations)[0]['status']=='passed'  # evidence not rewritten


@pytest.mark.asyncio
async def test_local_inflight_credential_reference_survives_purge(admin):
    await closed(admin);collector=worker(admin,purge=True);await collector.tick();aged(admin)
    identifier=admin.rows(t.ai_secrets)[0]['id'];lease=registry.open(identifier)
    try:
        assert (await collector.tick())['purged']==2 and admin.count(t.ai_secrets)==1
    finally:lease.close()
    assert (await collector.tick())['purged']==1


@pytest.mark.asyncio
async def test_transaction_failure_restores_inventory_rows_ciphertext_and_cursor(admin,monkeypatch):
    await closed(admin);collector=worker(admin,purge=True);await collector.tick();aged(admin)
    before=[dict(row) for row in admin.rows(t.ai_secrets)];count=admin.count(t.ai_config_draft_items)
    async def broken(*args,**kwargs):raise RuntimeError('simulated audit failure')
    monkeypatch.setattr(admin.service,'_audit',broken)
    with pytest.raises(RuntimeError):await collector.tick()
    assert [dict(row) for row in admin.rows(t.ai_secrets)]==before and admin.count(t.ai_config_draft_items)==count
    assert admin.count(t.ai_profile_revisions)==1 and collector.cursor is None


@pytest.mark.asyncio
async def test_retired_unpublished_versions_cannot_cross_drafts_or_be_published(admin):
    original=await closed(admin);await worker(admin).tick();other=await admin.draft('other')
    for table in (t.ai_secrets,t.ai_profile_revisions,t.ai_prompt_revisions):
        identifier=admin.rows(table)[0]['id']
        with Session(admin.engine) as session:
            with pytest.raises(BusinessError) as error:
                await admin.service._entity(AsyncFacade(session),table,identifier,other['id'],allow_invalid=True)
            assert error.value.code==40312
        with Session(admin.engine) as session:
            with pytest.raises(BusinessError) as error:await admin.service._entity(AsyncFacade(session),table,identifier,original['id'])
            assert error.value.data['reason']=='CANDIDATE_RETIRED'


@pytest.mark.asyncio
async def test_revocation_notifications_block_cleanup_until_terminal_and_purged_target_cannot_redrive(admin):
    await closed(admin);identifier=admin.rows(t.ai_secrets)[0]['id'];await revoke(admin,identifier)
    collector=worker(admin,purge=True);await collector.tick()
    assert admin.rows(t.ai_secrets)[0]['retired_at'] is None
    admin.sql(t.outbox_events.update().values(status='dead_letter',attempts=5))
    await collector.tick();assert admin.rows(t.ai_secrets)[0]['status']=='revoked'
    aged(admin);await collector.tick();assert admin.count(t.ai_secrets)==0
    with Session(admin.engine) as session:
        with pytest.raises(BusinessError) as error:await OutboxService(admin.service).redrive(AsyncFacade(session),admin.actor,
            admin.rows(t.outbox_events)[0]['id'],{'expected_attempts':5,'reason':'operator_retry'},'redrive-purged',admin.proof())
    assert error.value.data['reason']=='OUTBOX_TARGET_UNAVAILABLE'


@pytest.mark.asyncio
async def test_repeated_small_batches_progress_and_manager_loss_does_not_delete(admin):
    await closed(admin)
    for number in range(3):
        draft=await admin.draft('extra-'+str(number))
        draft,_=await admin.call('patch_draft',draft['id'],{'operations':[prompt_op()]},'extra-prompt-'+str(number),draft['etag'])
        await admin.call('abandon_draft',draft['id'],'extra-abandon-'+str(number),draft['etag'])
    collector=worker(admin,batch_size=1)
    for _ in range(8):await collector.tick()
    assert all(row['status']=='retired' for row in admin.rows(t.ai_prompt_revisions))
    aged(admin);admin.sql(t.ai_config_heads.update().values(manager_owner_id='another-manager',manager_lease_expires_at=utcnow()+timedelta(minutes=1)))
    with pytest.raises(BusinessError):await worker(admin,purge=True).tick()
    assert admin.count(t.ai_prompt_revisions)==4


@pytest.mark.asyncio
async def test_foreign_scope_candidate_rows_are_not_touched(admin):
    draft=await closed(admin)
    source=dict(admin.rows(t.ai_config_drafts)[0]);source.update(id=uid(),scope_id='another')
    admin.sql(t.ai_config_drafts.insert().values(**source))
    prompt=dict(admin.rows(t.ai_prompt_revisions)[0]);prompt.update(id=uid(),scope_id='another',source_draft_id=source['id'])
    admin.sql(t.ai_prompt_revisions.insert().values(**prompt))
    await worker(admin,purge=True).tick()
    assert next(row for row in admin.rows(t.ai_prompt_revisions) if row['id']==prompt['id'])['status']=='candidate'


@pytest.mark.asyncio
async def test_more_than_one_asset_page_progresses_without_purging_early(admin):
    await closed(admin);template=dict(admin.rows(t.ai_prompt_revisions)[0])
    for number in range(103):
        admin.sql(t.ai_prompt_revisions.insert().values(**{**template,'id':uid(),'revision':number+200}))
    collector=worker(admin,purge=True)
    await collector.tick();assert collector.asset_cursors
    assert sum(row['status']=='retired' for row in admin.rows(t.ai_prompt_revisions))==100
    await collector.tick()
    assert all(row['status']=='retired' for row in admin.rows(t.ai_prompt_revisions))
    assert admin.count(t.ai_prompt_revisions)==104 and not collector.asset_cursors


def test_maintenance_disabled_and_development_retirement_have_no_extra_engine(admin,monkeypatch):
    monkeypatch.setenv('AI_CONFIG_CANDIDATE_GC_ENABLED','false')
    assert configured_worker(admin.service,database_factory(admin))==(None,None)
    monkeypatch.setenv('AI_CONFIG_CANDIDATE_GC_ENABLED','true')
    monkeypatch.setenv('AI_CONFIG_CANDIDATE_GC_PURGE','false')
    monkeypatch.setenv('APP_ENV','development');monkeypatch.delenv('AI_CONFIG_MAINTENANCE_DATABASE_URL',raising=False)
    collector,engine=configured_worker(admin.service,database_factory(admin))
    assert not collector.purge and engine is None


@pytest.mark.asyncio
async def test_production_purge_requires_distinct_same_target_maintenance_role(admin,monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy.engine import make_url
    ordinary=SimpleNamespace(kw={'bind':SimpleNamespace(url=make_url('mysql+aiomysql://app:fictional@localhost:3306/fixture'))})
    monkeypatch.setenv('AI_CONFIG_CANDIDATE_GC_ENABLED','true');monkeypatch.setenv('AI_CONFIG_CANDIDATE_GC_PURGE','true')
    monkeypatch.setenv('APP_ENV','production # comment');monkeypatch.delenv('AI_CONFIG_MAINTENANCE_DATABASE_URL',raising=False)
    with pytest.raises(BusinessError) as error:configured_worker(admin.service,ordinary)
    assert error.value.data['reason']=='MAINTENANCE_STORE_REQUIRED'
    for target in ('mysql+aiomysql://gc:fictional@localhost:3306/another','mysql+aiomysql://app:fictional@localhost:3306/fixture'):
        monkeypatch.setenv('AI_CONFIG_MAINTENANCE_DATABASE_URL',target)
        with pytest.raises(BusinessError) as error:configured_worker(admin.service,ordinary)
        assert error.value.data['reason']=='MAINTENANCE_STORE_INVALID' and error.value.__context__ is None
    monkeypatch.setenv('AI_CONFIG_MAINTENANCE_DATABASE_URL','mysql+aiomysql://gc:fictional@localhost:3306/fixture')
    collector,engine=configured_worker(admin.service,ordinary)
    assert collector.purge and collector.session_factory is not ordinary
    await engine.dispose()
