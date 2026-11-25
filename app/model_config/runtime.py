"""Request-local role resolution and live dispatch permission checks."""
from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar

import sqlalchemy as sa

from app.model_config import tables as t
from app.model_config.crypto import Keyring
from app.model_config.endpoints import check_profile
from app.model_config.errors import fail
from app.model_config.prompt_contracts import render
from app.model_config.provider import ProviderGateway
from app.model_config.snapshots import current_snapshot, thaw
from app.model_config.security import SCOPE,scoped
dispatch_fence=ContextVar('runtime_dispatch_fence',default=None)


def require_failure_policy(role,mode):
    """An explicitly enabled role may degrade only as its published policy says."""
    snapshot=current_snapshot()
    if snapshot is not None:
        raw=snapshot.manifest['role_bindings_raw'][role]
        if raw['enabled'] and raw['failure_mode']!=mode:
            fail(50305,'ROLE_INFERENCE_FAILED',503,role_key=role)


def model_for(role,legacy=None,*,execution_model=None):
    snapshot=current_snapshot()
    if snapshot is None:
        return None if role == 'rag_query_expand' else legacy
    if role == 'rag_query_expand' and snapshot.manifest.get('schema_version') != 3:
        return None
    raw=snapshot.manifest['role_bindings_raw'][role]
    if not raw['enabled']:
        return None
    binding=snapshot.manifest['resolved_static_bindings'].get(role)
    if binding is None:
        if role not in snapshot.manifest['follow_execution_roles'] or execution_model is None:
            fail(50305,'EXECUTION_MODEL_REQUIRED',503)
        if not hasattr(execution_model,'snapshot') or execution_model.snapshot.release_id!=snapshot.release_id:
            fail(50303,'EXECUTION_SNAPSHOT_MISMATCH',503)
        profile_id=execution_model.profile_id
    else:
        profile_id=binding['profile_revision_id']
    if not profile_id or profile_id not in snapshot.profiles:
        fail(50305,'ROLE_PROFILE_UNAVAILABLE',503)
    from app.model_config.runtime_model import GuardedChatModel,BoundTrace
    profile=snapshot.profiles[profile_id]
    prompt_id=raw.get('prompt_revision_id')
    context={**profile_provenance(snapshot, profile_id),'prompt_revision_id':prompt_id, 'model_override':profile['model']}
    from app.model_config.snapshots import inference_actor
    context['user_id']=(inference_actor.get() or {}).get('user_id','')
    if role in ('memory_extraction', 'rag_query_expand'):
        context['stage'] = role
    backup_id = None
    if raw.get('fallback'):
        from app.model_config.fallback import backup_profile
        alternate = backup_profile(role, raw['fallback'][0], snapshot.manifest['role_bindings_raw'],
                                   snapshot.manifest['resolved_static_bindings'], snapshot.profiles, snapshot.policies)
        backup_id = alternate['id']
    backup_context = {**profile_provenance(snapshot, backup_id), 'model_override': snapshot.profiles[backup_id]['model']} if backup_id else None
    return GuardedChatModel(snapshot=snapshot,role_key=role,profile_id=profile_id,model_name=profile['model'],
                            **({'disable_streaming':True, 'cache':False} if role == 'rag_query_expand' else {}),
                            backup_profile_id=backup_id, callbacks=[BoundTrace(context, backup_context)])


def profile_provenance(snapshot, profile_id):
    from app.model_config.endpoints import canonical_url
    profile = snapshot.profiles[profile_id]
    credential_id = profile['credential_revision_id']
    return {'release_id': snapshot.release_id, 'profile_revision_id': profile_id,
            'credential_revision_id': credential_id, 'adapter_type': profile['adapter_type'],
            'credential_version': snapshot.credentials[credential_id]['credential_version'] if credential_id else None,
            'provider_origin': canonical_url(profile['base_url'])[0]}


def template_for(role,legacy,**values):
    snapshot=current_snapshot()
    if snapshot is None:
        return legacy
    raw=snapshot.manifest['role_bindings_raw'][role]
    prompt=snapshot.prompts.get(raw.get('prompt_revision_id'))
    if prompt is None:
        return ''
    return render(role,prompt['template_text'],values) if values else prompt['template_text']


async def verify_inference_actor(db, actor):
    from app.models.user import User
    from app.model_config.service import utcnow
    from datetime import datetime,timezone
    if not actor:fail(40102,'INFERENCE_IDENTITY_REQUIRED',401)
    if actor.get('expires') is not None and datetime.fromtimestamp(actor['expires'],timezone.utc).replace(tzinfo=None)<=utcnow():
        fail(40101,'TOKEN_EXPIRED',401)
    if actor.get('jti'):
        from app.utils.auth_utils import is_token_blacklisted
        if await is_token_blacklisted(actor['jti']):fail(40102,'TOKEN_INVALID',401)
    status=(await db.execute(sa.select(User.status).where(User.uuid==actor['user_id']))).scalar_one_or_none()
    if status!='active':fail(40314,'ACCOUNT_INACTIVE',403)


async def permit(snapshot,profile_id,session_factory,*,primary_profile_id=None):
    """Authorization linearization before each outbound call, including old releases."""
    from app.model_config.snapshots import inference_actor
    async with session_factory() as db:
        await verify_inference_actor(db, inference_actor.get())
        profile_ids = {profile_id, primary_profile_id} if primary_profile_id else {profile_id}
        for identifier in sorted(profile_ids):
            profile = thaw(snapshot.profiles[identifier])
            policy=(await db.execute(sa.select(t.ai_endpoint_policies).where(scoped(t.ai_endpoint_policies),
                t.ai_endpoint_policies.c.id==profile['endpoint_policy_revision_id']).with_for_update(read=True))).mappings().first()
            if not policy:
                fail(40310,'ENDPOINT_DENIED',403)
            origin=check_profile(profile,policy)
            if profile['credential_revision_id']:
                credential=(await db.execute(sa.select(t.ai_secrets).where(scoped(t.ai_secrets),
                    t.ai_secrets.c.id==profile['credential_revision_id']).with_for_update(read=True))).mappings().first()
                pinned=snapshot.credentials[profile['credential_revision_id']]
                if (not credential or credential['status'] not in ('active','retired')
                        or credential['allowed_origin']!=origin or credential['revocation_epoch']!=pinned['revocation_epoch']):
                    fail(40926,'CREDENTIAL_REVOKED')
    fence=dispatch_fence.get()
    if fence:await fence()
    from app.rag.citations import authorize_turn_evidence
    await authorize_turn_evidence()


@asynccontextmanager
async def gateway_for(snapshot,profile_id,*,session_factory=None,gateway_factory=ProviderGateway,primary_profile_id=None):
    if session_factory is None:
        from app.db.database import async_session_factory
        session_factory=async_session_factory
    profile=thaw(snapshot.profiles[profile_id])
    policy=thaw(snapshot.policies[profile['endpoint_policy_revision_id']])
    credential=snapshot.credentials.get(profile['credential_revision_id'])
    value=Keyring.load().decrypt(credential['ciphertext'],credential['key_version'],
        {**SCOPE,'id':credential['id'],'origin':credential['allowed_origin'],
         'version':credential['credential_version']}) if credential else None
    authorization = {'primary_profile_id': primary_profile_id} if primary_profile_id else {}
    async with gateway_factory(profile,policy,value,permit=lambda:permit(snapshot,profile_id,session_factory,**authorization),
                               probe_mode=False) as gateway:
        gateway.trace_context = profile_provenance(snapshot, profile_id)
        gateway.budget_session_factory = session_factory
        yield gateway
