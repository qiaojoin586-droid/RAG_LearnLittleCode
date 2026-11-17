"""Immutable release snapshots. No process-global mutable active model objects."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass,field
import json
import os
import re
from types import MappingProxyType

import sqlalchemy as sa

from app.model_config import tables as t
from app.model_config.contracts import content_hash
from app.model_config.errors import fail
from app.model_config.security import scoped

_current = ContextVar('ai_release_snapshot',default=None)
inference_actor = ContextVar('ai_inference_actor',default=None)


def current_snapshot():
    return _current.get()


@contextmanager
def use_snapshot(snapshot):
    token = _current.set(snapshot)
    try:
        yield
    finally:
        _current.reset(token)


def freeze(value):
    if isinstance(value,dict):
        return MappingProxyType({key:freeze(item) for key,item in value.items()})
    if isinstance(value,list):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value):
    if hasattr(value,'items'):
        return {key:thaw(item) for key,item in value.items()}
    if isinstance(value,(tuple,list)):
        return [thaw(item) for item in value]
    return value


@dataclass(frozen=True,repr=False)
class ReleaseSnapshot:
    release_id: str
    head_version: int
    manifest: object
    profiles: object
    prompts: object
    policies: object
    credentials: object
    indexes: object = field(default_factory=lambda:freeze({}))

    def __repr__(self):
        return f'<ReleaseSnapshot {self.release_id}>'


async def load_snapshot(db,release_id=None):
    head = (await db.execute(sa.select(t.ai_config_heads).where(scoped(t.ai_config_heads)))).mappings().first()
    if release_id is None and (not head or not head['active_release_id']):
        return None
    release = (await db.execute(sa.select(t.ai_config_releases).where(scoped(t.ai_config_releases),
        t.ai_config_releases.c.id==(release_id or head['active_release_id'])))).mappings().one()
    manifest = {key:release[key] for key in ('schema_version','role_bindings_raw','resolved_static_bindings',
                                            'follow_execution_roles','index_bindings')}
    if content_hash(manifest)!=release['content_hash']:
        fail(50303,'RELEASE_INTEGRITY_FAILED',503)
    if manifest['schema_version'] not in (1, 2, 3):
        fail(50303,'RELEASE_SCHEMA_UNSUPPORTED',503)
    refs = (await db.execute(sa.select(t.ai_config_release_refs).where(scoped(t.ai_config_release_refs),
        t.ai_config_release_refs.c.release_id==release['id']))).mappings().all()
    containers = {}
    for kind,table in [('profile',t.ai_profile_revisions),('prompt',t.ai_prompt_revisions),
                       ('endpoint_policy',t.ai_endpoint_policies),('credential',t.ai_secrets),('index',t.rag_index_versions)]:
        ids = [row['entity_id'] for row in refs if row['entity_kind']==kind]
        rows = (await db.execute(sa.select(table).where(scoped(table),table.c.id.in_(ids)))).mappings().all() if ids else []
        if len(rows)!=len(set(ids)):
            fail(50303,'RELEASE_REFERENCE_MISSING',503)
        containers[kind] = freeze({row['id']:dict(row) for row in rows})
    return ReleaseSnapshot(release['id'],head['head_version'] if head else release['release_number'],freeze(manifest),containers['profile'],
        containers['prompt'],containers['endpoint_policy'],containers['credential'],containers['index'])


class SnapshotMiddleware:
    """ASGI lifetime includes streamed response bodies; tasks inherit the snapshot."""
    def __init__(self,app,session_factory=None):
        self.app=app
        if session_factory is None:
            from app.db.database import async_session_factory
            session_factory=async_session_factory
        self.session_factory=session_factory

    async def __call__(self,scope,receive,send):
        path=scope.get('path','')
        # Management/auth/health do not require an active release during setup.
        email_receipt = (path.startswith('/api/v1/chat/sessions/') and path.endswith('/email-operations')) or (
            path.startswith('/api/v1/chat/approvals/') and path.endswith('/email-received'))
        upload_progress = scope.get('method') == 'GET' and (
            path == '/api/v1/knowledge/upload-jobs' or path.startswith('/api/v1/knowledge/upload-jobs/'))
        chat_history = scope.get('method') == 'GET' and re.fullmatch(
            r'/api/v1/chat/[^/]+/messages(?:/\d+/citations/[^/]+)?', path)
        if scope['type']!='http' or not path.startswith('/api/v1/') or path.startswith((
            '/api/v1/model-config','/api/v1/auth','/api/v1/user','/api/v1/usage','/api/v1/chat/workflows/')) or email_receipt or upload_progress or chat_history:
            await self.app(scope,receive,send);return
        from app.core.failed_response import BusinessError,failed_response
        from fastapi.responses import JSONResponse
        from app.model_config.task_budget import TaskModelBudget, current_model_budget
        try:
            try:
                budget = TaskModelBudget.configured()
            except ValueError:
                fail(50303,'MODEL_BUDGET_CONFIG_INVALID',503)
            async with self.session_factory() as db:
                snapshot=await load_snapshot(db)
            if snapshot is None and ((budget is not None and budget.has_model_limits)
                    or os.getenv('DB_CONFIG_REQUIRED','false').lower() in ('true','1','yes')):
                fail(50305,'MODEL_CONFIG_NOT_READY',503)
        except BusinessError as error:
            await JSONResponse(failed_response(error.code,message=error.message,data=error.data),
                               status_code=error.http_status)(scope,receive,send);return
        except Exception:
            await JSONResponse(failed_response(50303,message='模型配置存储不可用',
                data={'reason':'CONTROL_STORE_UNAVAILABLE'}),status_code=503)(scope,receive,send);return
        token=_current.set(snapshot)
        from app.ai_service.context_composer import assistant_data
        data_token=assistant_data.set(None)
        actor_token=inference_actor.set(None)
        from app.model_config.runtime import dispatch_fence
        fence_token=dispatch_fence.set(None)
        budget_token=current_model_budget.set(budget)
        try:
            await self.app(scope,receive,send)
        finally:
            current_model_budget.reset(budget_token)
            assistant_data.reset(data_token)
            dispatch_fence.reset(fence_token)
            inference_actor.reset(actor_token)
            _current.reset(token)
