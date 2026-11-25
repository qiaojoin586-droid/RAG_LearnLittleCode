"""Credential impact views and irreversible, audited local revocation."""
import sqlalchemy as sa

from app.model_config import tables as t
from app.model_config.contracts import validate
from app.model_config.credential_calls import registry
from app.model_config.errors import fail
from app.model_config.security import authorize,scoped,SCOPE
from app.model_config.service import uid,json_view

REASONS=frozenset(('suspected_leak','provider_disabled','administrator_action'))
PUBLIC_FIELDS=('id','credential_key','credential_version','allowed_origin','masked_suffix','status','revocation_epoch','revoked_at')


class CredentialService:
    def __init__(self,control):self.control=control

    async def _credential(self,db,credential_id,*,lock=False):
        query=sa.select(t.ai_secrets).where(scoped(t.ai_secrets),t.ai_secrets.c.id==credential_id)
        row=(await db.execute(query.with_for_update() if lock else query)).mappings().first()
        if not row:fail(40401,'CONFIG_RESOURCE_NOT_FOUND',404)
        return dict(row)

    async def impact(self,db,actor,credential_id):
        await authorize(db,actor,'config.read')
        credential=await self._credential(db,credential_id)
        release_count=(await db.execute(sa.select(sa.func.count(sa.distinct(t.ai_config_release_refs.c.release_id))).where(scoped(t.ai_config_release_refs),
            t.ai_config_release_refs.c.entity_kind=='credential',t.ai_config_release_refs.c.entity_id==credential_id))).scalar_one()
        drafts=(await db.execute(sa.select(sa.func.count(sa.distinct(t.ai_config_draft_items.c.draft_id))).where(scoped(t.ai_config_draft_items),
            t.ai_config_draft_items.c.entity_kind=='credential',t.ai_config_draft_items.c.entity_id==credential_id))).scalar_one()
        head=await self.control._head(db);roles=[];indexes=[];dynamic=[]
        active_id=head['active_release_id'] if head else None;active_reference=False
        if active_id:
            release=await self.control._entity(db,t.ai_config_releases,active_id)
            active_reference=bool((await db.execute(sa.select(sa.func.count()).select_from(t.ai_config_release_refs).where(
                scoped(t.ai_config_release_refs),t.ai_config_release_refs.c.release_id==active_id,
                t.ai_config_release_refs.c.entity_kind=='credential',t.ai_config_release_refs.c.entity_id==credential_id))).scalar_one())
            roles=[key for key,binding in release['resolved_static_bindings'].items()
                if binding['enabled'] and binding.get('credential_revision_id')==credential_id]
            indexes=[key for key,binding in release['index_bindings'].items() if binding['credential_revision_id']==credential_id]
            if roles:dynamic=release['follow_execution_roles']
        return json_view({'credential':{name:credential[name] for name in PUBLIC_FIELDS},'active_release_id':active_id,
            'active_role_keys':sorted(roles),'possible_dynamic_role_keys':sorted(dynamic),'active_index_keys':sorted(indexes),
            'historical_release_count':release_count-int(active_reference),'referenced_draft_count':drafts,
            'scope':'all_references_to_this_credential_revision','provider_key_revocation_supported':False})

    async def revoke(self,db,actor,credential_id,body,key,proof):
        validate('CredentialRevocationInput',body)
        if not body['reason'].strip():fail(40002,'REVOCATION_REASON_REQUIRED',422)
        async def operation(head):
            credential=await self._credential(db,credential_id,lock=True)
            already=credential['status']=='revoked';epoch=credential['revocation_epoch']+int(not already)
            now=await self.control._now(db)
            if not already:
                await db.execute(t.ai_secrets.update().where(scoped(t.ai_secrets),t.ai_secrets.c.id==credential_id)
                    .values(status='revoked',revocation_epoch=epoch,revoked_at=now))
                await db.execute(t.outbox_events.insert().values(id=uid(),**SCOPE,
                    event_key=f'credential-revoke:{credential_id}:{epoch}',event_type='credential.revoked',
                    aggregate_id=credential_id,aggregate_version=epoch,
                    payload={'credential_revision_id':credential_id,'revocation_epoch':epoch},status='pending',attempts=0,
                    next_attempt_at=now,created_at=now))
                # Historical passed evidence stays immutable; running tests are
                # terminal now and late network results cannot restore them.
                from app.model_config.validation_service import ValidationService
                rows=(await db.execute(sa.select(t.ai_config_validations).where(scoped(t.ai_config_validations),
                    t.ai_config_validations.c.credential_revision_id==credential_id,
                    t.ai_config_validations.c.status.in_(['pending','running'])).with_for_update())).mappings().all()
                for item in rows:
                    row=dict(item);row.update(status='cancelled',valid_until=None,
                        result={'status':'cancelled','reason':'CREDENTIAL_REVOKED','usage':None})
                    await db.execute(t.ai_config_validations.update().where(scoped(t.ai_config_validations),t.ai_config_validations.c.id==row['id'])
                        .values(status='cancelled',valid_until=None,result=row['result']))
                    await db.execute(t.ai_config_requests.update().where(scoped(t.ai_config_requests),t.ai_config_requests.c.id==row['request_record_id'])
                        .values(status='succeeded',response_http_status=200,response_json=ValidationService.view(row),completed_at=now))
            result={'credential_revision_id':credential_id,'credential_version':credential['credential_version'],
                'status':'revoked','revocation_epoch':epoch,'dispatch_blocked':True,'cancel_requested':True,'already_revoked':already}
            validate('CredentialRevocationResult',result)
            diff={**result,'reason_code':body['reason'] if body['reason'] in REASONS else 'operator_supplied',
                'reason_fingerprint':self.control.keyring.fingerprint({'credential_id':credential_id,'reason':body['reason']})}
            return result,credential_id,diff
        result,status=await self.control._mutate(db,actor,'credential.revoke','config.credential.revoke',credential_id,body,key,
            operation,proof=proof,sensitive=True)
        # The durable revoke is committed before signalling. Failed delivery is
        # retried by Outbox; replay also requests local cancellation again.
        try:registry.request_cancel(credential_id)
        except Exception:pass
        return result,status
