"""M1 draft control plane. No provider I/O or runtime cutover occurs here.

Every mutation commits its versions, reference index, audit and idempotency result
in one transaction. M1 serializes writes on the system authorization head.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hmac
import os
import re
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.request_context import current_request_id
from app.model_config import tables as t
from app.model_config.contracts import content_hash, disabled_binding, empty_payload, upgrade_payload, validate
from app.model_config.crypto import Keyring
from app.model_config.endpoints import check_policy, check_profile
from app.model_config.errors import fail
from app.core.failed_response import BusinessError
from app.model_config.permissions import allows_config_action
from app.model_config.registry import PROMPT_KEYS
from app.model_config.security import Principal, SCOPE, authorize, scoped, verify_proof
from app.models.user import User


def uid():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def json_view(value):
    if isinstance(value,datetime):
        return value.isoformat(timespec='microseconds')+'Z'
    if isinstance(value,dict) or hasattr(value,'keys'):
        return {key:json_view(item) for key,item in value.items()}
    if isinstance(value,(list,tuple)):
        return [json_view(item) for item in value]
    return value


def etag(draft):
    return '"draft:'+draft['id']+':'+str(draft['draft_version'])+'"'


async def ensure_row(db, table, values):
    dialect = db.get_bind().dialect.name
    if dialect=='mysql':
        statement = mysql_insert(table).values(**values)
        key = next(iter(table.primary_key.columns)).name
        statement = statement.on_duplicate_key_update(**{key:table.c[key]})
    elif dialect=='sqlite':
        statement = sqlite_insert(table).values(**values).on_conflict_do_nothing()
    else:
        fail(50303,'UNSUPPORTED_CONTROL_STORE',503)
    await db.execute(statement)


class ConfigService:
    def __init__(self, keyring: Keyring | None = None, *, owner_id: str | None = None, reauth_guard=None):
        self._keyring = keyring
        self.owner_id = owner_id or uid()
        self.reauth_guard = reauth_guard

    @property
    def keyring(self):
        if self._keyring is None:
            self._keyring = Keyring.load()
        return self._keyring

    async def _now(self, db):
        if db.get_bind().dialect.name == 'mysql':
            return (await db.execute(sa.text('SELECT UTC_TIMESTAMP(6)'))).scalar_one()
        return utcnow()

    async def _head(self, db, *, lock=False):
        query = sa.select(t.ai_config_heads).where(scoped(t.ai_config_heads))
        return (await db.execute(query.with_for_update() if lock else query)).mappings().first()

    async def _manager(self, db):
        try:
            workers = max(int(os.getenv(name,'1')) for name in ('WEB_CONCURRENCY','UVICORN_WORKERS'))
        except ValueError:
            workers = 0
        if workers != 1:
            fail(50304,'MULTI_WORKER_UNSUPPORTED',503)
        now = await self._now(db)
        await ensure_row(db,t.ai_config_heads,{**SCOPE,'head_version':0,'last_release_number':0,
                                               'manager_generation':0,'updated_at':now})
        head = await self._head(db,lock=True)
        if (head['manager_owner_id'] not in (None,self.owner_id)
                and head['manager_lease_expires_at'] and head['manager_lease_expires_at']>now):
            fail(50304,'MULTI_WORKER_UNSUPPORTED',503)
        generation = head['manager_generation'] + int(head['manager_owner_id'] != self.owner_id)
        deadline = now+timedelta(seconds=30)
        await db.execute(t.ai_config_heads.update().where(scoped(t.ai_config_heads)).values(
            manager_owner_id=self.owner_id,manager_generation=generation,
            manager_lease_expires_at=deadline,updated_at=now))
        return {**head,'manager_owner_id':self.owner_id,'manager_generation':generation,
                'manager_lease_expires_at':deadline}

    async def _counter(self, db, kind, key):
        table = t.ai_version_counters
        now = await self._now(db)
        await ensure_row(db,table,{**SCOPE,'entity_kind':kind,'entity_key':key,'last_num':0,'updated_at':now})
        where = sa.and_(scoped(table),table.c.entity_kind==kind,table.c.entity_key==key)
        last = (await db.execute(sa.select(table.c.last_num).where(where).with_for_update())).scalar_one()
        await db.execute(table.update().where(where).values(last_num=last+1,updated_at=now))
        return last+1

    async def _audit(self, db, actor, action, target, diff, *, head=None):
        await db.execute(t.ai_config_audits.insert().values(id=uid(),**SCOPE,actor_id=actor,
            action=action,target_key=target,result='succeeded',redacted_diff=diff,
            request_id=current_request_id(),created_at=await self._now(db),
            release_id=diff.get('release_id'),restore_from_release_id=diff.get('restore_from_release_id'),
            credential_revision_id=diff.get('credential_revision_id'),credential_version=diff.get('credential_version'),
            head_version_before=diff.get('head_version_before',head),head_version_after=diff.get('head_version_after',head)))

    async def _mutate(self, db, principal, action, permission, target, body, request_key,
                      handler, *, match=None, proof=None, sensitive=False, http_status=200, deferred=False):
        if not request_key or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',request_key):
            fail(40002,'IDEMPOTENCY_KEY_REQUIRED',422)
        # Do not include Bearer/proof in the fingerprint or any persistent payload.
        fingerprint_input = {'action':action,'target':target,'body':body,'if_match':match}
        async with db.begin():
            user,grant,policy_version = await authorize(db,principal,'config.read',lock=True)
            table = t.ai_config_requests
            request = (await db.execute(sa.select(table).where(scoped(table),
                table.c.actor_id==principal.user_id,table.c.action==action,
                table.c.request_key==request_key).with_for_update())).mappings().first()
            now = await self._now(db)
            if request:
                calculated = self.keyring.fingerprint(fingerprint_input,request['payload_hash_key_version'])
                if not hmac.compare_digest(calculated,request['payload_hash']):
                    fail(40922,'IDEMPOTENCY_PAYLOAD_MISMATCH')
                if now>=request['replay_until']:
                    fail(40929,'REQUEST_REPLAY_EXPIRED',410)
                if deferred and request['status'] in ('pending','running') and request['response_json']:
                    return request['response_json'],202,False
                if request['status'] != 'succeeded':
                    fail(40923,'RESOURCE_ACTION_IN_PROGRESS')
                if deferred:
                    return request['response_json'],request['response_http_status'],False
                return request['response_json'],request['response_http_status']
            if not allows_config_action(grant['config_role_key'],permission):
                fail(40312,'CONFIG_PERMISSION_DENIED',403)
            if sensitive and self.reauth_guard:
                await self.reauth_guard(user['username'])
            proof_deadline = verify_proof(self.keyring,proof,principal,policy_version) if sensitive else None
            manager = await self._manager(db)
            deadline = min(now+timedelta(seconds=30),manager['manager_lease_expires_at'],
                           principal.expires_at,proof_deadline or principal.expires_at)
            record_id = uid()
            # Persist IDs/operation names only; API keys and prompt text are kept
            # out of requests/audits. The encrypted credential row is the boundary.
            action_payload = {'target':target,'if_match':match,
                'role_keys':sorted({item['role_key'] for item in body.get('operations',[]) if 'role_key' in item}),
                'operations':[item['op'] for item in body.get('operations',[])]}
            for key in ('expected_head_version','expected_policy_version','base_release_id'):
                if key in body:
                    action_payload[key] = body[key]
            await db.execute(table.insert().values(id=record_id,**SCOPE,actor_id=principal.user_id,
                action=action,request_key=request_key,actor_access_jti=principal.access_jti,
                authorization_policy_version=policy_version,
                reauth_verified_at=now if sensitive else None,
                authorization_valid_until=min(principal.expires_at,proof_deadline or principal.expires_at),
                action_payload=action_payload,execution_state={},
                payload_hash=self.keyring.fingerprint(fingerprint_input),
                payload_hash_key_version=self.keyring.active_version,status='running',
                deadline_at=deadline,lease_owner=self.owner_id,lease_expires_at=deadline,
                created_at=now,replay_until=now+timedelta(days=7),attempt_count=1))
            result,result_ref,diff = await handler({**manager,'request_record_id':record_id,
                                                   'authorization_policy_version':policy_version})
            result = json_view(result)
            now = await self._now(db)
            if now>=deadline:
                fail(50410,'ACTION_DEADLINE_EXCEEDED',504)
            await self._audit(db,principal.user_id,action,target,diff,head=manager['head_version'])
            await db.execute(table.update().where(table.c.id==record_id,scoped(table)).values(
                status='running' if deferred else 'succeeded',result_ref=result_ref,response_http_status=http_status,
                response_json=result,completed_at=None if deferred else now))
        # Explicit commit happens before the caller constructs its HTTP response.
        return (result,http_status,True) if deferred else (result,http_status)

    async def summary(self, db, principal):
        _,grant,policy = await authorize(db,principal,'config.read')
        head = await self._head(db)
        drafts = (await db.execute(sa.select(t.ai_config_drafts.c.id,t.ai_config_drafts.c.draft_version,
            t.ai_config_drafts.c.status).where(scoped(t.ai_config_drafts),t.ai_config_drafts.c.status.in_(
            ['editing','validating','ready']),t.ai_config_drafts.c.expires_at>await self._now(db)).order_by(
            t.ai_config_drafts.c.updated_at.desc()).limit(20))).mappings().all()
        return {'scope':SCOPE,'config_role_key':grant['config_role_key'],'policy_version':policy,
            'head_version':head['head_version'] if head else 0,
            'active_release_id':head['active_release_id'] if head else None,
            'release_id':head['active_release_id'] if head else None,
            'setup_required':not head or not head['active_release_id'],'current_drafts':[dict(row) for row in drafts],
            'status':'setup_required' if not head or not head['active_release_id'] else 'active',
            'effective_for':'new_requests' if head and head['active_release_id'] else 'none',
            'runtime_source':'database' if head and head['active_release_id'] else 'legacy_environment',
            'features':{'draft_edit':True,'endpoint_policy_edit':True,
                'capability_probe':True,'publish':True,'hot_reload':True,'personal_memory':True,'fallback_model':True}}

    async def list_rows(self, db, principal, table, *, permission='config.read', limit=20, cursor=None, status=None):
        await authorize(db,principal,permission)
        if not 1<=limit<=100:
            fail(40002,'FORMAT_VALIDATION_FAILED',422)
        if table is t.ai_secrets:
            query = sa.select(*[table.c[key] for key in ('id','credential_key','credential_version',
                'allowed_origin','masked_suffix','status','origin_draft_id','created_at','revocation_epoch','revoked_at')])
        else:
            query = sa.select(table)
        where = [scoped(table)]
        id_column = table.c.id if 'id' in table.c else table.c.subject_user_id
        time_column = table.c.created_at if 'created_at' in table.c else table.c.granted_at
        if status is not None:
            if table is not t.ai_config_drafts or status not in ('editing','validating','ready','published','abandoned','expired'):
                fail(40002,'FORMAT_VALIDATION_FAILED',422)
            where.append(table.c.status==status)
        if cursor:
            previous = (await db.execute(sa.select(time_column).where(scoped(table),id_column==cursor))).first()
            if previous is None:
                fail(40002,'INVALID_CURSOR',422)
            where.append(sa.or_(time_column<previous[0],sa.and_(time_column==previous[0],id_column<cursor)))
        order = [time_column.desc(),id_column.desc()]
        rows = (await db.execute(query.where(*where).order_by(*order).limit(limit+1))).mappings().all()
        next_cursor = rows[limit-1][id_column.name] if len(rows)>limit else None
        return json_view({'items':rows[:limit],'limit':limit,'next_cursor':next_cursor})

    async def _entity(self, db, table, entity_id, draft_id=None, *, allow_invalid=False):
        row = (await db.execute(sa.select(table).where(scoped(table),table.c.id==entity_id))).mappings().first()
        if not row:
            fail(40401,'CONFIG_RESOURCE_NOT_FOUND',404)
        if table in (t.ai_profile_revisions,t.ai_prompt_revisions,t.ai_secrets):
            source = 'origin_draft_id' if table is t.ai_secrets else 'source_draft_id'
            unpublished_retired=False
            if row['status']=='retired':
                kind={t.ai_profile_revisions:'profile',t.ai_prompt_revisions:'prompt',t.ai_secrets:'credential'}[table]
                unpublished_retired=not (await db.execute(sa.select(t.ai_config_release_refs.c.release_id).where(
                    scoped(t.ai_config_release_refs),t.ai_config_release_refs.c.entity_kind==kind,
                    t.ai_config_release_refs.c.entity_id==entity_id).limit(1))).first()
            if (row['status']=='candidate' or unpublished_retired) and row[source] != draft_id:
                fail(40312,'CANDIDATE_DRAFT_MISMATCH',403)
            if unpublished_retired and not allow_invalid:fail(40923,'CANDIDATE_RETIRED')
            if not allow_invalid and table is t.ai_secrets and row['status']=='revoked':
                fail(40926,'CREDENTIAL_REVOKED')
            if not allow_invalid and table is t.ai_secrets and row['status']=='candidate' and (not row['candidate_expires_at'] or row['candidate_expires_at'] <= await self._now(db)):
                fail(40926,'CREDENTIAL_EXPIRED')
        return dict(row)

    async def _draft(self, db, draft_id, *, match=None, edit=False):
        query = sa.select(t.ai_config_drafts).where(scoped(t.ai_config_drafts),t.ai_config_drafts.c.id==draft_id)
        row = (await db.execute(query.with_for_update() if edit else query)).mappings().first()
        if not row:
            fail(40401,'CONFIG_RESOURCE_NOT_FOUND',404)
        if row['expires_at']<=await self._now(db) or row['status']=='expired':
            fail(40924,'DRAFT_EXPIRED',409 if edit else 410)
        if edit:
            if not match:
                fail(42801,'IF_MATCH_REQUIRED',428)
            if match != etag(row):
                fail(40921,'DRAFT_VERSION_CONFLICT',expected=match,actual=etag(row))
            if row['status'] not in ('editing','validating','ready'):
                fail(40923,'DRAFT_NOT_EDITABLE')
        return dict(row)

    async def create_draft(self, db, principal, body, request_key):
        validate('DraftCreate',body)
        async def operation(head):
            if body['expected_head_version'] != head['head_version']:
                fail(40920,'HEAD_VERSION_CONFLICT',expected=body['expected_head_version'],actual=head['head_version'])
            if body['base_release_id'] != head['active_release_id']:
                fail(40927,'REBASE_REQUIRED')
            payload = empty_payload()
            if body['base_release_id']:
                release = await self._entity(db,t.ai_config_releases,body['base_release_id'])
                payload = upgrade_payload({'schema_version':release['schema_version'],
                    'role_bindings_raw':release['role_bindings_raw'],'index_bindings':release['index_bindings']})
            now = await self._now(db)
            draft = dict(id=uid(),**SCOPE,base_release_id=body['base_release_id'],
                base_head_version=head['head_version'],draft_version=1,payload=payload,
                payload_hash=content_hash(payload),status='editing',expires_at=now+timedelta(days=7),
                created_by=principal.user_id,created_at=now,updated_at=now)
            await db.execute(t.ai_config_drafts.insert().values(**draft))
            view = await self._view(db,draft,register=True,allow_unusable=True)
            return view,draft['id'],{'draft_id':draft['id'],'draft_version':1}
        return await self._mutate(db,principal,'draft.create','config.edit','drafts',body,request_key,
                                  operation,http_status=201)

    async def get_draft(self, db, principal, draft_id):
        await authorize(db,principal,'config.read')
        draft=await self._draft(db,draft_id)
        try:return json_view(await self._view(db,draft))
        except BusinessError as error:
            if draft['status'] in ('abandoned','expired') and error.code==40401:
                fail(40924,'DRAFT_REFERENCES_PURGED',410)
            raise

    async def abandon_draft(self, db, principal, draft_id, request_key, match):
        async def operation(head):
            draft = await self._draft(db,draft_id,match=match,edit=True)
            await db.execute(t.ai_config_drafts.update().where(scoped(t.ai_config_drafts),
                t.ai_config_drafts.c.id==draft_id).values(status='abandoned',updated_at=await self._now(db)))
            return {'id':draft_id,'status':'abandoned'},draft_id,{'draft_id':draft_id}
        return await self._mutate(db,principal,'draft.abandon','config.edit',draft_id,{},request_key,
                                  operation,match=match)

    async def create_policy(self, db, principal, body, request_key, proof):
        validate('EndpointPolicyInput',body)
        normalized = check_policy(body)
        async def operation(head):
            row = {**normalized,'id':uid(),**SCOPE,'revision':await self._counter(db,'endpoint_policy',body['policy_key']),
                'origin_hash':content_hash(normalized['origin']),'status':'active',
                'created_by':principal.user_id,'created_at':await self._now(db)}
            await db.execute(t.ai_endpoint_policies.insert().values(**row))
            return row,row['id'],{'endpoint_policy_revision_id':row['id'],'revision':row['revision']}
        return await self._mutate(db,principal,'endpoint.create','endpoint.manage',body['policy_key'],body,
                                  request_key,operation,proof=proof,sensitive=True,http_status=201)

    async def change_grant(self, db, principal, user_id, body, request_key, proof, *, revoke=False):
        definition = 'GrantDeleteInput' if revoke else 'GrantUpdateInput'
        validate(definition,body)
        async def operation(head):
            auth_head = t.ai_config_authorization_heads
            current = (await db.execute(sa.select(auth_head.c.policy_version).where(scoped(auth_head)))).scalar_one()
            if current != body['expected_policy_version']:
                fail(40920,'AUTHORIZATION_POLICY_VERSION_CONFLICT',expected=body['expected_policy_version'],actual=current)
            user = (await db.execute(sa.select(User.__table__).where(User.uuid==user_id).with_for_update())).mappings().first()
            if not user or not revoke and user['status']!='active':
                fail(40314,'ACCOUNT_INACTIVE',403)
            table = t.ai_config_admin_grants
            where = sa.and_(scoped(table),table.c.subject_user_id==user_id)
            old = (await db.execute(sa.select(table).where(where).with_for_update())).mappings().first()
            if not old and revoke:
                fail(40401,'CONFIG_RESOURCE_NOT_FOUND',404)
            demotes = old and old['status']=='active' and old['config_role_key']=='config_admin' and (
                revoke or body['config_role_key']!='config_admin')
            if demotes:
                admins = (await db.execute(sa.select(sa.func.count()).select_from(table.join(
                    User.__table__,User.uuid==table.c.subject_user_id)).where(scoped(table),
                    table.c.status=='active',table.c.config_role_key=='config_admin',User.status=='active'))).scalar_one()
                if admins<=1:
                    fail(40928,'LAST_ADMIN_PROTECTED')
            now = await self._now(db)
            values = {'status':'revoked' if revoke else 'active','revoked_at':now if revoke else None}
            if not revoke:
                values.update(config_role_key=body['config_role_key'],granted_by=principal.user_id,granted_at=now)
            if old:
                await db.execute(table.update().where(where).values(**values))
            else:
                await db.execute(table.insert().values(**SCOPE,subject_user_id=user_id,**values))
            await db.execute(auth_head.update().where(scoped(auth_head)).values(policy_version=current+1,updated_at=now))
            result = {'subject_user_id':user_id,'status':values['status'],
                      'config_role_key':old['config_role_key'] if revoke else body['config_role_key'],
                      'policy_version':current+1}
            return result,user_id,result
        return await self._mutate(db,principal,'grant.revoke' if revoke else 'grant.update','grant.manage',
            user_id,body,request_key,operation,proof=proof,sensitive=True)

    async def _profile(self, db, principal, draft, role, profile, action):
        if profile['model_kind'] != ('embedding' if role=='embedding' else 'chat'):
            fail(40002,'MODEL_KIND_MISMATCH',422)
        policy = await self._entity(db,t.ai_endpoint_policies,profile['endpoint_policy_revision_id'])
        origin = check_profile(profile,policy)
        if (profile['model_kind'] == 'chat' and profile['context_window'] is not None and
                profile['context_window'] <= (profile['max_output_tokens'] or 2048)):
            fail(40002,'MODEL_OUTPUT_EXCEEDS_CONTEXT',422)
        old_id = draft['payload']['role_bindings_raw'][role].get('profile_revision_id')
        old = await self._entity(db,t.ai_profile_revisions,old_id,draft['id']) if old_id else None
        credential_id = None
        now = await self._now(db)
        if action['action']=='keep':
            if profile['auth_mode']!='none':
                if not old or not old['credential_revision_id']:
                    fail(40002,'CREDENTIAL_REQUIRED',422)
                credential = await self._entity(db,t.ai_secrets,old['credential_revision_id'],draft['id'])
                if credential['allowed_origin'] != origin:
                    fail(40310,'CREDENTIAL_ORIGIN_MISMATCH',403)
                credential_id = credential['id']
            elif old and old['credential_revision_id']:
                fail(40002,'CREDENTIAL_CLEAR_REQUIRED',422)
        elif action['action']=='replace':
            if profile['auth_mode']=='none':
                fail(40002,'UNAUTHENTICATED_PROFILE_HAS_CREDENTIAL',422)
            credential_id = uid()
            version = await self._counter(db,'credential',role)
            identity = {**SCOPE,'id':credential_id,'origin':origin,'version':version}
            encrypted,key_version = self.keyring.encrypt(action['api_key'],identity)
            await db.execute(t.ai_secrets.insert().values(id=credential_id,**SCOPE,credential_key=role,
                credential_version=version,allowed_origin=origin,ciphertext=encrypted,key_version=key_version,
                masked_suffix=action['api_key'][-4:] if len(action['api_key'])>8 else None,status='candidate',
                origin_draft_id=draft['id'],candidate_expires_at=draft['expires_at'],revocation_epoch=0,
                created_by=principal.user_id,created_at=now))
            await self._inventory(db,draft['id'],'credential',credential_id)
        elif profile['auth_mode']!='none':
            fail(40002,'CREDENTIAL_REQUIRED',422)
        row = {**profile,'id':uid(),**SCOPE,'profile_key':role,'revision':await self._counter(db,'profile',role),
            'adapter_version':'control-draft-v1','credential_revision_id':credential_id,
            'semantic_fingerprint':None,'semantic_identity':None,'capabilities':{},
            'source_draft_id':draft['id'],'status':'candidate','created_by':principal.user_id,'created_at':now}
        row['content_hash'] = content_hash({**profile,'credential_revision_id':credential_id,
                                            'adapter_version':row['adapter_version']})
        if (role=='embedding' and old and old['semantic_fingerprint']
                and all(profile[name]==old[name] for name in ('model','vendor','model_revision','embedding_dimension',
                    'base_url','adapter_type','api_protocol','parameters'))):
            from app.model_config.embedding_space import compatibility
            if compatibility({'status':'baseline_verified','dimension':old['embedding_dimension'],
                    'semantic_identity':old['semantic_identity'],'semantic_fingerprint':old['semantic_fingerprint']},old)=='COMPATIBLE':
                row.update(semantic_identity=copy.deepcopy(old['semantic_identity']),semantic_fingerprint=old['semantic_fingerprint'],
                    capabilities=copy.deepcopy(old['capabilities']))
                row['content_hash']=content_hash({'profile_hash':row['content_hash'],'semantic_fingerprint':row['semantic_fingerprint']})
        await db.execute(t.ai_profile_revisions.insert().values(**row))
        await self._inventory(db,draft['id'],'profile',row['id'])
        return row

    async def patch_draft(self, db, principal, draft_id, body, request_key, match, proof=None):
        validate('DraftPatch',body)
        sensitive = any(op['op']=='replace_profile' and op['credential']['action'] in ('replace','clear')
                        for op in body['operations'])
        async def operation(head):
            draft = await self._draft(db,draft_id,match=match,edit=True)
            payload = copy.deepcopy(draft['payload'])
            draft['payload'] = payload
            for operation in body['operations']:
                kind = operation['op']
                role = operation.get('role_key')
                if role == 'rag_query_expand' and payload['schema_version'] == 2:
                    payload['schema_version'] = 3
                    payload['role_bindings_raw'][role] = disabled_binding()
                if kind=='replace_profile':
                    profile = await self._profile(db,principal,draft,role,operation['profile'],operation['credential'])
                    payload['role_bindings_raw'][role].update(selection='explicit',source_role=None,
                        profile_revision_id=profile['id'],credential_revision_id=profile['credential_revision_id'])
                    for index_key in operation.get('index_keys',[]):
                        if index_key not in payload['index_bindings']:
                            fail(40930,'INDEX_BASELINE_REQUIRED')
                        payload['index_bindings'][index_key].update(query_profile_revision_id=profile['id'],
                            credential_revision_id=profile['credential_revision_id'])
                elif kind=='replace_prompt':
                    prompt = operation['prompt']
                    from app.model_config.prompt_contracts import prompt_metadata
                    row = dict(id=uid(),**SCOPE,prompt_key=PROMPT_KEYS[role],
                        revision=await self._counter(db,'prompt',PROMPT_KEYS[role]),**prompt,
                        **prompt_metadata(role),content_hash=content_hash(prompt),
                        source='user',status='candidate',source_draft_id=draft_id,
                        created_by=principal.user_id,created_at=await self._now(db))
                    await db.execute(t.ai_prompt_revisions.insert().values(**row))
                    await self._inventory(db,draft_id,'prompt',row['id'])
                    payload['role_bindings_raw'][role]['prompt_revision_id'] = row['id']
                elif kind=='replace_binding':
                    payload['role_bindings_raw'][role] = copy.deepcopy(operation['binding'])
                elif kind=='replace_index_binding':
                    # No M1 index creation or vector-space substitution.
                    existing = payload['index_bindings'].get(operation['index_key'])
                    if not existing or existing['index_version_id'] != operation['binding']['index_version_id']:
                        fail(40930,'INDEX_BASELINE_REQUIRED')
                    payload['index_bindings'][operation['index_key']] = copy.deepcopy(operation['binding'])
            validate('DraftPayload',payload)
            draft.update(draft_version=draft['draft_version']+1,payload_hash=content_hash(payload),
                         status='editing',updated_at=await self._now(db))
            view = await self._view(db,draft,register=True,allow_unusable=True)
            await db.execute(t.ai_config_drafts.update().where(scoped(t.ai_config_drafts),
                t.ai_config_drafts.c.id==draft_id).values(**{key:draft[key] for key in
                ('draft_version','payload','payload_hash','status','updated_at')}))
            # Retain the original validation rows as evidence; their draft_version
            # no longer matches and they cannot authorize a future publication.
            return view,draft_id,{'draft_id':draft_id,'draft_version':draft['draft_version'],
                                  'operations':[op['op'] for op in body['operations']]}
        return await self._mutate(db,principal,'draft.patch','config.edit',draft_id,body,request_key,
                                  operation,match=match,proof=proof,sensitive=sensitive)

    async def _inventory(self, db, draft_id, kind, entity_id):
        await ensure_row(db,t.ai_config_draft_items,{**SCOPE,'draft_id':draft_id,
            'entity_kind':kind,'entity_id':entity_id,'created_at':await self._now(db)})

    async def _view(self, db, draft, *, register=False, allow_unusable=False):
        from app.model_config.resolution import resolve_bindings
        profiles,prompts,credentials,policies,indexes = {},{},{},{},{}
        refs = set()
        async def load(kind, entity_id):
            if not entity_id:
                return
            table,container = {'profile':(t.ai_profile_revisions,profiles),'prompt':(t.ai_prompt_revisions,prompts),
                'credential':(t.ai_secrets,credentials),'endpoint_policy':(t.ai_endpoint_policies,policies),
                'index':(t.rag_index_versions,indexes)}[kind]
            if entity_id in container:
                return
            container[entity_id] = await self._entity(db,table,entity_id,draft['id'],allow_invalid=not register or allow_unusable)
            refs.add((kind,entity_id))
            if kind=='profile':
                await load('credential',container[entity_id]['credential_revision_id'])
                await load('endpoint_policy',container[entity_id]['endpoint_policy_revision_id'])
                if register and not allow_unusable:
                    check_profile(container[entity_id],policies[container[entity_id]['endpoint_policy_revision_id']])
        for binding in draft['payload']['role_bindings_raw'].values():
            for kind in ('profile','credential','prompt'):
                await load(kind,binding.get(kind+'_revision_id'))
        for binding in draft['payload']['index_bindings'].values():
            await load('index',binding['index_version_id'])
            await load('profile',binding['query_profile_revision_id'])
            await load('credential',binding['credential_revision_id'])
        resolution = resolve_bindings(draft['payload'],profiles,prompts,credentials,indexes)
        validations = (await db.execute(sa.select(t.ai_config_validations.c.id,t.ai_config_validations.c.request_record_id,
            t.ai_config_validations.c.role_key,t.ai_config_validations.c.draft_version,
            t.ai_config_validations.c.status,t.ai_config_validations.c.result,
            t.ai_config_validations.c.tested_at,t.ai_config_validations.c.valid_until,
            t.ai_config_validations.c.target_hash,t.ai_config_validations.c.target_spec).where(scoped(t.ai_config_validations),
                t.ai_config_validations.c.draft_id==draft['id'],
                t.ai_config_validations.c.draft_version==draft['draft_version']).order_by(
                t.ai_config_validations.c.created_at.desc(),t.ai_config_validations.c.id.desc()).limit(170))).mappings().all()
        if register:
            # Keep created/unreferenced candidates in this inventory too. They
            # remain attributable to their origin draft for later retirement.
            for kind,entity_id in sorted(refs):
                await self._inventory(db,draft['id'],kind,entity_id)
        blockers=set()
        now=await self._now(db)
        for credential in credentials.values():
            if credential['status']=='revoked':blockers.add('CREDENTIAL_REVOKED')
            elif credential['status']=='candidate' and (not credential['candidate_expires_at'] or credential['candidate_expires_at']<=now):blockers.add('CREDENTIAL_EXPIRED')
        if any(policy['status']!='active' for policy in policies.values()):blockers.add('ENDPOINT_DENIED')
        if draft['payload']['role_bindings_raw']['embedding']['enabled'] and (
                set(draft['payload']['index_bindings'])!={'rag','notes'} or any(item['status']!='compatible' for item in resolution['embedding_diff'])):
            blockers.add('INDEX_BASELINE_NOT_VERIFIED')
        return {**draft,'etag':etag(draft),'profiles':list(profiles.values()),'prompts':list(prompts.values()),
                **resolution,'validation_coverage':[
                    {**{key: value for key, value in row.items() if key != 'target_spec'},
                     'fallback_role_key': row['target_spec'].get('fallback_role_key')} for row in validations],
                'publication_blockers':sorted(blockers)}


async def bootstrap_admin(db, user_id: str):
    """Local CLI only: authorize the first existing active user, never a username."""
    async with db.begin():
        await ensure_row(db,t.ai_config_authorization_heads,{**SCOPE,'policy_version':1,'updated_at':utcnow()})
        await db.execute(sa.select(t.ai_config_authorization_heads).where(
            scoped(t.ai_config_authorization_heads)).with_for_update())
        user = (await db.execute(sa.select(User.__table__).where(User.uuid==user_id).with_for_update())).mappings().first()
        if not user or user['status']!='active':
            fail(40314,'ACCOUNT_INACTIVE',403)
        grants = (await db.execute(sa.select(t.ai_config_admin_grants).where(
            scoped(t.ai_config_admin_grants),t.ai_config_admin_grants.c.status=='active').with_for_update())).mappings().all()
        if grants:
            if len(grants)==1 and grants[0]['subject_user_id']==user_id and grants[0]['config_role_key']=='config_admin':
                return {'user_id':user_id,'status':'already_bootstrapped'}
            fail(40312,'BOOTSTRAP_ALREADY_COMPLETED',403)
        # A previously bootstrapped/revoked deployment must use its recovery flow.
        if (await db.execute(sa.select(t.ai_config_admin_grants).where(scoped(t.ai_config_admin_grants)).limit(1))).first():
            fail(40312,'BOOTSTRAP_ALREADY_COMPLETED',403)
        await db.execute(t.ai_config_admin_grants.insert().values(**SCOPE,subject_user_id=user_id,
            config_role_key='config_admin',status='active',granted_by=user_id,granted_at=utcnow()))
        await ConfigService()._audit(db,user_id,'grant.bootstrap',user_id,{'config_role_key':'config_admin'})
    return {'user_id':user_id,'status':'bootstrapped'}
