"""M1 configuration control routes with explicit permission mapping."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from app.core.failed_response import BusinessError
from app.core.rate_limit import check_rate_limit
from app.core.success_response import success_response
from app.db.database import async_session_factory
from app.model_config import tables as t
from app.model_config.contracts import validate
from app.model_config.errors import fail
from app.model_config.permissions import required_config_permission
from app.model_config.registry import role_views
from app.model_config.security import Principal, authorize, issue_proof
from app.model_config.service import ConfigService
from app.utils import auth_utils
from app.model_config.validation_service import ValidationService
from app.model_config.publishing import PublishingService
from app.model_config.rebase import RebaseService
from app.model_config.outbox_service import OutboxService

async def private_headers(response: Response):
    response.headers['Cache-Control'] = 'no-store'


service = ConfigService(reauth_guard=auth_utils.check_login_attempts)
router = APIRouter(prefix='/model-config',tags=['Model Configuration'],dependencies=[Depends(private_headers)])
auth_router = APIRouter(prefix='/auth',tags=['User & Auth'],dependencies=[Depends(private_headers)])


async def validation_token_guard(actor):
    if await auth_utils.is_token_blacklisted(actor.access_jti):
        fail(40102,'TOKEN_INVALID',401)


def validator():
    return ValidationService(service,async_session_factory,token_guard=validation_token_guard)


async def config_db():
    # Explicit service transaction commits are required before a success response.
    # Never use the legacy dependency's best-effort post-response auto-commit.
    try:
        async with async_session_factory() as session:
            try:
                yield session
            finally:
                await session.rollback()
    except SQLAlchemyError:
        # SQLAlchemy exceptions may render bound values; do not log/re-raise them.
        fail(50303,'CONTROL_STORE_UNAVAILABLE',503)


async def principal(request: Request, user_id=Depends(auth_utils.get_current_user_id)):
    claims = getattr(request.state,'auth_claims',{})
    try:
        jti = claims['jti']
        if not isinstance(jti,str) or not 1<=len(jti)<=64:
            raise ValueError
        expires = datetime.fromtimestamp(claims['exp'],timezone.utc).replace(tzinfo=None)
        if claims['sub'] != user_id:
            raise ValueError
    except (KeyError,ValueError,TypeError,OverflowError,OSError):
        fail(40102,'TOKEN_INVALID',401)
    redis = request.app.state.redis
    endpoint_limit = 3 if request.method=='POST' and request.url.path.endswith('/test') else 30
    if not await check_rate_limit(redis,user_id,request.url.path,endpoint_limit=endpoint_limit):
        fail(42902,'ENDPOINT_RATE_LIMIT',429)
    if request.url.path.startswith('/api/v1/model-config') and not required_config_permission(
        request.method,request.url.path):
        fail(40312,'CONFIG_PERMISSION_DENIED',403)
    return Principal(user_id,jti,expires)


def write_response(response, result):
    data,status = result
    response.status_code = status
    if isinstance(data,dict) and data.get('etag'):
        response.headers['ETag'] = data['etag']
    response.headers['Cache-Control'] = 'no-store'
    return success_response(data)


@router.get('')
async def root(actor=Depends(principal), db=Depends(config_db)):
    return success_response(await service.summary(db,actor))


@router.get('/roles')
async def roles(actor=Depends(principal), db=Depends(config_db)):
    await authorize(db,actor,'config.read')
    return success_response({'items':role_views()})


@router.get('/outbox')
async def outbox_events(status: str | None=Query(None,max_length=24),cursor: str | None=Query(None,max_length=36),
        limit: int=Query(20,ge=1,le=100),actor=Depends(principal),db=Depends(config_db)):
    return success_response(await OutboxService(service).list(db,actor,status=status,cursor=cursor,limit=limit))


@router.get('/outbox/{event_id}')
async def outbox_event(event_id: str,actor=Depends(principal),db=Depends(config_db)):
    return success_response(await OutboxService(service).get(db,actor,event_id))


@router.post('/outbox/{event_id}/redrive')
async def redrive_outbox(event_id: str,response: Response,body: dict=Body(...),
        request_key: str | None=Header(None,alias='Idempotency-Key',max_length=128),
        proof: str | None=Header(None,alias='X-Admin-Reauth',max_length=4096),
        actor=Depends(principal),db=Depends(config_db)):
    return write_response(response,await OutboxService(service).redrive(db,actor,event_id,body,request_key,proof))


@router.post('/drafts/{draft_id}/publish')
async def publish(draft_id: str,response: Response,body: dict=Body(...),
        request_key: str | None=Header(None,alias='Idempotency-Key',max_length=128),
        match: str | None=Header(None,alias='If-Match',max_length=128),
        proof: str | None=Header(None,alias='X-Admin-Reauth',max_length=4096),
        actor=Depends(principal),db=Depends(config_db)):
    return write_response(response,await PublishingService(service,async_session_factory).publish(
        db,actor,draft_id,body,request_key,proof,match))


@router.post('/drafts/{draft_id}/rebase')
async def rebase(draft_id: str,response: Response,body: dict=Body(...),
        request_key: str | None=Header(None,alias='Idempotency-Key',max_length=128),
        match: str | None=Header(None,alias='If-Match',max_length=128),
        actor=Depends(principal),db=Depends(config_db)):
    return write_response(response,await RebaseService(service).rebase(db,actor,draft_id,body,request_key,match))


@router.get('/releases')
async def releases(limit: int=Query(20,ge=1,le=100),cursor: str | None=Query(None,max_length=36),
        actor=Depends(principal),db=Depends(config_db)):
    return success_response(await service.list_rows(db,actor,t.ai_config_releases,limit=limit,cursor=cursor))


@router.get('/releases/{release_id}')
async def release(release_id: str,actor=Depends(principal),db=Depends(config_db)):
    await authorize(db,actor,'config.read')
    from app.model_config.service import json_view
    return success_response(json_view(await service._entity(db,t.ai_config_releases,release_id)))


@router.post('/releases/{release_id}/restore')
async def restore(release_id: str,response: Response,body: dict=Body(...),
        request_key: str | None=Header(None,alias='Idempotency-Key',max_length=128),
        proof: str | None=Header(None,alias='X-Admin-Reauth',max_length=4096),
        actor=Depends(principal),db=Depends(config_db)):
    return write_response(response,await PublishingService(service,async_session_factory).restore(
        db,actor,release_id,body,request_key,proof))


@router.post('/releases/{release_id}/index-catch-up')
async def catch_up_release_indexes(release_id: str, response: Response, body: dict = Body(...),
        request_key: str | None = Header(None, alias='Idempotency-Key', max_length=128),
        proof: str | None = Header(None, alias='X-Admin-Reauth', max_length=4096),
        actor=Depends(principal), db=Depends(config_db)):
    from app.model_config.index_build import IndexBuildService

    return write_response(response, await IndexBuildService(service, async_session_factory).enqueue_restore(
        db, actor, release_id, body, request_key, proof))


@router.get('/drafts')
async def drafts(limit: int=Query(20,ge=1,le=100),cursor: str | None=Query(None,max_length=36),
        status: str | None=Query(None,max_length=24),actor=Depends(principal), db=Depends(config_db)):
    return success_response(await service.list_rows(db,actor,t.ai_config_drafts,limit=limit,cursor=cursor,status=status))


@router.post('/drafts',status_code=201)
async def create_draft(response: Response, body: dict=Body(...),
        request_key: str | None=Header(None,alias='Idempotency-Key',max_length=128),
        actor=Depends(principal),db=Depends(config_db)):
    return write_response(response,await service.create_draft(db,actor,body,request_key))


@router.get('/drafts/{draft_id}')
async def get_draft(draft_id: str,response: Response,actor=Depends(principal),db=Depends(config_db)):
    view = await service.get_draft(db,actor,draft_id)
    response.headers['ETag'] = view['etag']
    response.headers['Cache-Control'] = 'no-store'
    return success_response(view)


@router.patch('/drafts/{draft_id}')
async def patch_draft(draft_id: str,response: Response,body: dict=Body(...),
        request_key: str | None=Header(None,alias='Idempotency-Key',max_length=128),
        match: str | None=Header(None,alias='If-Match',max_length=128),
        proof: str | None=Header(None,alias='X-Admin-Reauth',max_length=4096),
        actor=Depends(principal),db=Depends(config_db)):
    return write_response(response,await service.patch_draft(db,actor,draft_id,body,request_key,match,proof))


@router.delete('/drafts/{draft_id}')
async def abandon_draft(draft_id: str,response: Response,
        request_key: str | None=Header(None,alias='Idempotency-Key',max_length=128),
        match: str | None=Header(None,alias='If-Match',max_length=128),
        actor=Depends(principal),db=Depends(config_db)):
    return write_response(response,await service.abandon_draft(db,actor,draft_id,request_key,match))


@router.get('/drafts/{draft_id}/resolution')
async def resolution(draft_id: str,actor=Depends(principal),db=Depends(config_db)):
    view = await service.get_draft(db,actor,draft_id)
    return success_response({key:view[key] for key in ('id','draft_version','resolved_static_bindings',
        'follow_execution_roles','reverse_refs','affected_roles','embedding_diff',
        'binding_issues','validation_coverage','publication_blockers')})


@router.post('/drafts/{draft_id}/test')
async def test_draft(draft_id: str,response: Response,body: dict=Body(...),
        request_key: str | None=Header(None,alias='Idempotency-Key',max_length=128),
        match: str | None=Header(None,alias='If-Match',max_length=128),
        actor=Depends(principal),db=Depends(config_db)):
    return write_response(response,await validator().test_draft(db,actor,draft_id,body,request_key,match))


@router.get('/validations/{validation_id}')
async def validation(validation_id: str,actor=Depends(principal),db=Depends(config_db)):
    return success_response(await validator().get(db,actor,validation_id))


@router.get('/endpoint-policies')
async def policies(limit: int=Query(20,ge=1,le=100),cursor: str | None=Query(None,max_length=36),
        actor=Depends(principal),db=Depends(config_db)):
    return success_response(await service.list_rows(db,actor,t.ai_endpoint_policies,limit=limit,cursor=cursor))


@router.get('/endpoint-policies/{policy_id}')
async def policy(policy_id: str,actor=Depends(principal),db=Depends(config_db)):
    await authorize(db,actor,'config.read')
    from app.model_config.service import json_view
    return success_response(json_view(await service._entity(db,t.ai_endpoint_policies,policy_id)))


@router.post('/endpoint-policies',status_code=201)
async def create_policy(response: Response,body: dict=Body(...),
        request_key: str | None=Header(None,alias='Idempotency-Key',max_length=128),
        proof: str | None=Header(None,alias='X-Admin-Reauth',max_length=4096),
        actor=Depends(principal),db=Depends(config_db)):
    return write_response(response,await service.create_policy(db,actor,body,request_key,proof))


@router.get('/credentials')
async def credentials(limit: int=Query(20,ge=1,le=100),cursor: str | None=Query(None,max_length=36),
        actor=Depends(principal),db=Depends(config_db)):
    return success_response(await service.list_rows(db,actor,t.ai_secrets,limit=limit,cursor=cursor))


@router.get('/credentials/{credential_id}/impact')
async def credential_impact(credential_id: str,actor=Depends(principal),db=Depends(config_db)):
    from app.model_config.credential_service import CredentialService
    return success_response(await CredentialService(service).impact(db,actor,credential_id))


@router.post('/credentials/{credential_id}/revoke')
async def revoke_credential(credential_id: str,response: Response,body: dict=Body(...),
        request_key: str | None=Header(None,alias='Idempotency-Key',max_length=128),
        proof: str | None=Header(None,alias='X-Admin-Reauth',max_length=4096),
        actor=Depends(principal),db=Depends(config_db)):
    from app.model_config.credential_service import CredentialService
    return write_response(response,await CredentialService(service).revoke(db,actor,credential_id,body,request_key,proof))


@router.get('/embedding-status')
async def embedding_status(draft_id: str | None=Query(None,max_length=36),actor=Depends(principal),db=Depends(config_db)):
    from app.model_config.embedding_status import EmbeddingStatusService
    return success_response(await EmbeddingStatusService(service).status(db,actor,draft_id=draft_id))


@router.post('/embedding-migrations')
async def start_embedding_migration(response: Response, body: dict = Body(...),
        request_key: str | None = Header(None, alias='Idempotency-Key', max_length=128),
        match: str | None = Header(None, alias='If-Match', max_length=128),
        proof: str | None = Header(None, alias='X-Admin-Reauth', max_length=4096),
        actor=Depends(principal), db=Depends(config_db)):
    from app.model_config.index_build import IndexBuildService

    return write_response(response, await IndexBuildService(service, async_session_factory).enqueue(
        db, actor, body, request_key, proof, match))


@router.get('/embedding-migrations/{job_id}')
async def embedding_migration(job_id: str, actor=Depends(principal), db=Depends(config_db)):
    from app.model_config.index_build import IndexBuildService

    return success_response(await IndexBuildService(service, async_session_factory).status(db, actor, job_id))


@router.get('/audits')
async def audits(limit: int=Query(20,ge=1,le=100),cursor: str | None=Query(None,max_length=36),
        actor=Depends(principal),db=Depends(config_db)):
    return success_response(await service.list_rows(db,actor,t.ai_config_audits,permission='config.audit.read',
                                                   limit=limit,cursor=cursor))


@router.get('/grants')
async def grants(limit: int=Query(20,ge=1,le=100),cursor: str | None=Query(None,max_length=36),
        actor=Depends(principal),db=Depends(config_db)):
    return success_response(await service.list_rows(db,actor,t.ai_config_admin_grants,permission='grant.manage',
                                                   limit=limit,cursor=cursor))


@router.put('/grants/{user_id}')
async def update_grant(user_id: str,response: Response,body: dict=Body(...),
        request_key: str | None=Header(None,alias='Idempotency-Key',max_length=128),
        proof: str | None=Header(None,alias='X-Admin-Reauth',max_length=4096),
        actor=Depends(principal),db=Depends(config_db)):
    return write_response(response,await service.change_grant(db,actor,user_id,body,request_key,proof))


@router.delete('/grants/{user_id}')
async def revoke_grant(user_id: str,response: Response,body: dict=Body(...),
        request_key: str | None=Header(None,alias='Idempotency-Key',max_length=128),
        proof: str | None=Header(None,alias='X-Admin-Reauth',max_length=4096),
        actor=Depends(principal),db=Depends(config_db)):
    return write_response(response,await service.change_grant(db,actor,user_id,body,request_key,proof,revoke=True))


@auth_router.post('/reauth')
async def reauth(request: Request,body: dict=Body(...),actor=Depends(principal),db=Depends(config_db)):
    validate('ReauthInput',body)
    user,_,policy = await authorize(db,actor,'config.read')
    # Both actor and source IP have a separate five-per-minute budget.
    redis = request.app.state.redis
    ip = request.client.host if request.client else 'unknown'
    window_script = "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n"
    for key in ('config_reauth:actor:'+actor.user_id,'config_reauth:ip:'+ip):
        if int(await redis.eval(window_script,1,key))>5:
            fail(42902,'ENDPOINT_RATE_LIMIT',429)
    await auth_utils.check_login_attempts(user['username'])
    if not await asyncio.to_thread(auth_utils.verify_password,body['password'],user['password']):
        await auth_utils.record_login_failure(user['username'])
        raise BusinessError(40103,http_status=401)
    await auth_utils.clear_login_attempts(user['username'])
    # Re-read live authorization after the expensive password hash check. End the
    # snapshot first so a concurrent revocation is not hidden by REPEATABLE READ.
    await db.rollback()
    _,_,current_policy = await authorize(db,actor,'config.read')
    if current_policy != policy:
        fail(40111,'REAUTH_INVALID',401)
    return success_response(issue_proof(service.keyring,actor,current_policy))
