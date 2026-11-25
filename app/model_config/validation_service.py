"""Durable bounded probes. Prepare/finish transactions never span provider I/O."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import sqlalchemy as sa

from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.contracts import content_hash, validate
from app.model_config.endpoints import check_profile
from app.model_config.errors import fail
from app.model_config.fallback import backup_profile, require_same_credential, same_service
from app.model_config.probes import PROBE_SUITE_VERSION, run_suite
from app.model_config.provider import ADAPTER_VERSION, ProviderGateway
from app.model_config.security import SCOPE, authorize, scoped
from app.model_config.service import json_view, uid


class ValidationService:
    def __init__(self,control,session_factory,*,gateway_factory=ProviderGateway,token_guard=None):
        self.control = control
        self.session_factory = session_factory
        self.gateway_factory = gateway_factory
        self.token_guard = token_guard

    @staticmethod
    def view(row):
        keys = ('id','request_record_id','draft_id','draft_version','role_key','status','target_hash',
                'target_spec','hash_algorithm','result','max_calls','max_output_tokens','deadline_at',
                'tested_at','valid_until','created_at')
        return json_view({**{key:row[key] for key in keys},
                          'fallback_role_key': row['target_spec'].get('fallback_role_key')})

    async def _row(self,db,validation_id,*,lock=False):
        table = t.ai_config_validations
        query = sa.select(table).where(scoped(table),table.c.id==validation_id)
        row = (await db.execute(query.with_for_update() if lock else query)).mappings().first()
        if not row:
            fail(40401,'CONFIG_RESOURCE_NOT_FOUND',404)
        return dict(row)

    async def _target(self,db,draft,role,fallback_role=None):
        view = await self.control._view(db,draft)
        static = view['resolved_static_bindings']
        binding = static.get(fallback_role or role)
        if fallback_role and not all(item and item.get('enabled') and item.get('profile_revision_id')
                                     for item in (static.get(role), binding)):
            fail(42222, 'FALLBACK_STATIC_ROLE_REQUIRED', 422, role_key=role, fallback_role_key=fallback_role)
        if not binding or not binding.get('profile_revision_id'):
            fail(40002,'PROBE_STATIC_PROFILE_REQUIRED',422)
        profile = await self.control._entity(db,t.ai_profile_revisions,binding['profile_revision_id'],draft['id'])
        policy = await self.control._entity(db,t.ai_endpoint_policies,profile['endpoint_policy_revision_id'])
        origin = check_profile(profile,policy)
        credential = await self.control._entity(db,t.ai_secrets,profile['credential_revision_id'],draft['id']) if profile['credential_revision_id'] else None
        if credential and credential['allowed_origin']!=origin:
            fail(40310,'CREDENTIAL_ORIGIN_MISMATCH',403)
        if profile['credential_revision_id'] != binding.get('credential_revision_id'):
            fail(40002,'CREDENTIAL_BINDING_MISMATCH',422)
        if fallback_role:
            primary = await self.control._entity(db,t.ai_profile_revisions,static[role]['profile_revision_id'],draft['id'])
            primary_policy = await self.control._entity(db,t.ai_endpoint_policies,primary['endpoint_policy_revision_id'])
            backup_profile(role, fallback_role, draft['payload']['role_bindings_raw'], static,
                           {item['id']: item for item in view['profiles']},
                           {item['id']: item for item in (policy, primary_policy)})
            primary_origin = check_profile(primary, primary_policy)
            primary_id = static[role]['credential_revision_id']
            primary_credential = await self.control._entity(db,t.ai_secrets,primary_id,draft['id']) if primary_id else None
            if primary['credential_revision_id'] != primary_id:
                fail(40002,'CREDENTIAL_BINDING_MISMATCH',422)
            if primary_credential and primary_credential['allowed_origin'] != primary_origin:
                fail(40310,'CREDENTIAL_ORIGIN_MISMATCH',403)
            if same_service(primary, profile) and (primary_credential or credential):
                require_same_credential(self.control.keyring, primary_credential, credential)
        # Neither secret material nor mutable provider-returned data enters this target.
        from app.model_config.prompt_contracts import CONTRACT_VERSION
        target = {'role_key':role,'role_contract_version':CONTRACT_VERSION,
            'profile':{key:profile[key] for key in ('id','content_hash','model_kind','adapter_type','api_protocol',
                'vendor','base_url','model','model_revision','auth_mode','parameters','context_window','tokenizer_id',
                'tokenizer_revision','token_counting_policy','max_output_tokens','connect_timeout_ms',
                'request_timeout_ms','embedding_dimension')},
            'credential_revision_id':credential['id'] if credential else None,
            'credential_version':credential['credential_version'] if credential else None,
            'credential_revocation_epoch':credential['revocation_epoch'] if credential else None,
            'endpoint_policy_revision_id':policy['id'],'adapter_version':ADAPTER_VERSION,
            'safety_policy_version':'fixed-nonpersonal-probes-v1','probe_suite_version':PROBE_SUITE_VERSION,
            'canonicalization_version':'rfc8785-v1'}
        if fallback_role:
            target['fallback_role_key'] = fallback_role
            target['primary_profile_revision_id'] = static[role]['profile_revision_id']
            target['primary_credential_revision_id'] = primary_id
            target['primary_credential_revocation_epoch'] = primary_credential['revocation_epoch'] if primary_credential else None
        if role=='embedding':
            from app.model_config.probes import EMBEDDING_PROBE_SUITE_VERSION
            target['profile'].update(semantic_fingerprint=profile['semantic_fingerprint'],semantic_identity=profile['semantic_identity'])
            target['probe_suite_version']=EMBEDDING_PROBE_SUITE_VERSION
        return target,profile,policy,credential

    async def _permit(self,actor,validation_id):
        if self.token_guard:
            await self.token_guard(actor)
        async with self.session_factory() as db:
            await authorize(db,actor,'config.test')
            row = await self._row(db,validation_id)
            now = await self.control._now(db)
            if row['status']!='running' or row['deadline_at']<=now or row['lease_owner']!=self.control.owner_id:
                fail(40923,'PROBE_NO_LONGER_RUNNING')
            head = await self.control._head(db)
            request = (await db.execute(sa.select(t.ai_config_requests.c.execution_state).where(
                scoped(t.ai_config_requests),t.ai_config_requests.c.id==row['request_record_id']))).scalar_one()
            if (head['manager_owner_id']!=self.control.owner_id or head['manager_lease_expires_at']<=now
                    or head['manager_generation']!=request['manager_generation']):
                fail(50304,'MANAGER_LEASE_LOST',503)
            draft = await self.control._draft(db,row['draft_id'])
            if draft['draft_version']!=row['draft_version'] or draft['status'] not in ('editing','validating','ready'):
                fail(40921,'DRAFT_VERSION_CONFLICT')
            target,profile,policy,credential = await self._target(db,draft,row['role_key'],row['target_spec'].get('fallback_role_key'))
            if content_hash(target)!=row['target_hash']:
                fail(40921,'VALIDATION_TARGET_CHANGED')
            credential_epochs = {}
            for prefix in ('', 'primary_'):
                identifier = target.get(prefix + 'credential_revision_id')
                if identifier:
                    credential_epochs[identifier] = target[prefix + 'credential_revocation_epoch']
            for identifier, epoch in sorted(credential_epochs.items()):
                live=(await db.execute(sa.select(t.ai_secrets.c.status,t.ai_secrets.c.revocation_epoch).where(scoped(t.ai_secrets),
                    t.ai_secrets.c.id==identifier).with_for_update(read=True))).mappings().first()
                if not live or live['status']=='revoked' or live['revocation_epoch']!=epoch:
                    fail(40926,'CREDENTIAL_REVOKED')
            return row,profile,policy,credential

    async def _heartbeat(self,actor,validation_id):
        while True:
            await asyncio.sleep(5)
            async with self.session_factory() as db:
                async with db.begin():
                    await authorize(db,actor,'config.test',lock=True)
                    head = await self.control._manager(db)
                    row = await self._row(db,validation_id)
                    request = (await db.execute(sa.select(t.ai_config_requests.c.execution_state).where(
                        scoped(t.ai_config_requests),t.ai_config_requests.c.id==row['request_record_id']))).scalar_one()
                    if head['manager_generation']!=request['manager_generation']:
                        fail(50304,'MANAGER_LEASE_LOST',503)

    async def _run(self,actor,validation_id,gateway,role):
        probe = asyncio.create_task(run_suite(gateway,role))
        heartbeat = asyncio.create_task(self._heartbeat(actor,validation_id))
        try:
            done,_ = await asyncio.wait((probe,heartbeat),return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                heartbeat.result()  # Lease/authorization failure cancels only this probe.
            return probe.result()
        finally:
            for task in (probe,heartbeat):
                if not task.done():
                    task.cancel()
            await asyncio.gather(probe,heartbeat,return_exceptions=True)

    async def test_draft(self,db,actor,draft_id,body,request_key,match):
        validate('DraftTestInput',body)
        async def prepare(head):
            draft = await self.control._draft(db,draft_id,match=match,edit=True)
            if draft['draft_version']!=body['expected_draft_version']:
                fail(40921,'DRAFT_VERSION_CONFLICT')
            now = await self.control._now(db)
            active = (await db.execute(sa.select(t.ai_config_validations.c.id).where(
                scoped(t.ai_config_validations),t.ai_config_validations.c.draft_id==draft_id,
                t.ai_config_validations.c.role_key==body['role_key'],t.ai_config_validations.c.status=='running',
                t.ai_config_validations.c.deadline_at>now).limit(1))).first()
            if active:
                fail(40923,'RESOURCE_ACTION_IN_PROGRESS')
            target,profile,policy,credential = await self._target(db,draft,body['role_key'],body.get('fallback_role_key'))
            budget = 90 if body.get('budget','standard')=='local_cold' else 30
            if budget==90 and policy['address_mode']!='trusted_private':
                fail(40002,'LOCAL_COLD_BUDGET_REQUIRES_PRIVATE_ENDPOINT',422)
            now = await self.control._now(db)
            deadline = min(now+timedelta(seconds=budget),actor.expires_at)
            row = dict(id=uid(),**SCOPE,request_record_id=head['request_record_id'],created_by=actor.user_id,
                draft_id=draft_id,draft_version=draft['draft_version'],role_key=body['role_key'],
                target_hash=content_hash(target),target_spec=target,hash_algorithm='jcs-sha256-v1',
                endpoint_policy_revision_id=policy['id'],safety_policy_version=target['safety_policy_version'],
                adapter_version=ADAPTER_VERSION,credential_revision_id=target['credential_revision_id'],
                credential_version=target['credential_version'],validation_kind='capability',status='running',
                result=None,max_calls=1 if body['role_key']=='rag_query_expand' else 3,
                max_output_tokens=96,attempt_count=1,lease_owner=self.control.owner_id,
                deadline_at=deadline,lease_expires_at=deadline,tested_at=None,valid_until=None,created_at=now)
            await db.execute(t.ai_config_validations.insert().values(**row))
            await db.execute(t.ai_config_requests.update().where(scoped(t.ai_config_requests),
                t.ai_config_requests.c.id==head['request_record_id']).values(deadline_at=deadline,
                    lease_expires_at=deadline,authorization_valid_until=actor.expires_at,
                    execution_state={'validation_id':row['id'],'manager_generation':head['manager_generation']}))
            return self.view(row),row['id'],{'validation_id':row['id'],'role_key':body['role_key'],
                                           'draft_id':draft_id,'draft_version':draft['draft_version']}
        prepared,status,created = await self.control._mutate(db,actor,'draft.test','config.test',draft_id,
            body,request_key,prepare,match=match,http_status=202,deferred=True)
        if not created:
            return prepared,status
        validation_id = prepared['id']
        try:
            row,profile,policy,credential = await self._permit(actor,validation_id)
            api_key = self.control.keyring.decrypt(credential['ciphertext'],credential['key_version'],
                {**SCOPE,'id':credential['id'],'origin':credential['allowed_origin'],
                 'version':credential['credential_version']}) if credential else None
            remaining = max(0,(row['deadline_at']-await self.control._now(db)).total_seconds())
            # _now may open a read transaction; close it before any provider I/O.
            await db.rollback()
            async with asyncio.timeout(remaining):
                async with self.gateway_factory(profile,policy,api_key,
                    permit=lambda:self._permit(actor,validation_id)) as gateway:
                    gateway.trace_context = {'user_id':actor.user_id,'profile_revision_id':profile['id'],
                        'credential_revision_id':profile['credential_revision_id'],
                        'credential_version':credential['credential_version'] if credential else None,
                        'step_id':validation_id}
                    result = await self._run(actor,validation_id,gateway,row['role_key'])
            result['adapter_version'] = ADAPTER_VERSION
            result['business_prompt_contract_verified'] = False
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._finish(actor,validation_id,{
                'status':'unverified','reason':'PROBE_INTERRUPTED','usage':None}))
            try:
                await asyncio.wait_for(asyncio.shield(cleanup),5)
            finally:
                if not cleanup.done():
                    cleanup.cancel()
                    await asyncio.gather(cleanup,return_exceptions=True)
            raise
        except TimeoutError:
            result = {'status':'unverified','reason':'PROBE_DEADLINE_EXCEEDED','usage':None}
        except BusinessError as error:
            result = {'status':'unverified','reason':(error.data or {}).get('reason','PROBE_UNAVAILABLE'),'usage':None}
        except Exception:
            # Never persist or log arbitrary upstream exception/response text.
            result = {'status':'unverified','reason':'PROVIDER_UNAVAILABLE','usage':None}
        return await self._finish(actor,validation_id,result),200

    async def _finish(self,actor,validation_id,result):
        async with self.session_factory() as db:
            async with db.begin():
                try:
                    await authorize(db,actor,'config.test',lock=True)
                    if self.token_guard:
                        await self.token_guard(actor)
                    authorized = True
                except BusinessError:
                    authorized = False
                    # Preserve evidence but never turn a revoked/expired action into passed.
                    result = {'status':'unverified','reason':'AUTHORIZATION_CHANGED','usage':None}
                if authorized:
                    head = await self.control._manager(db)
                row = await self._row(db,validation_id,lock=True)
                if row['status']!='running' or row['lease_owner']!=self.control.owner_id:
                    return self.view(row)
                now = await self.control._now(db)
                request = (await db.execute(sa.select(t.ai_config_requests.c.execution_state).where(
                    scoped(t.ai_config_requests),t.ai_config_requests.c.id==row['request_record_id']))).scalar_one()
                if authorized and head['manager_generation']!=request['manager_generation']:
                    result = {'status':'unverified','reason':'MANAGER_GENERATION_CHANGED','usage':None}
                if now>=row['deadline_at']:
                    result = {'status':'unverified','reason':'PROBE_DEADLINE_EXCEEDED','usage':None}
                if result['status']=='passed':
                    try:
                        draft = await self.control._draft(db,row['draft_id'])
                        if draft['draft_version']!=row['draft_version'] or draft['status'] not in ('editing','validating','ready'):
                            raise ValueError
                        target,*_ = await self._target(db,draft,row['role_key'],row['target_spec'].get('fallback_role_key'))
                        if content_hash(target)!=row['target_hash']:
                            raise ValueError
                    except (BusinessError,ValueError):
                        result = {'status':'unverified','reason':'VALIDATION_TARGET_CHANGED','usage':None}
                row.update(status=result['status'],result=result,tested_at=now,
                           valid_until=now+timedelta(hours=24) if result['status']=='passed' else None)
                await db.execute(t.ai_config_validations.update().where(scoped(t.ai_config_validations),
                    t.ai_config_validations.c.id==validation_id).values(**{key:row[key] for key in
                    ('status','result','tested_at','valid_until')}))
                view = self.view(row)
                await db.execute(t.ai_config_requests.update().where(scoped(t.ai_config_requests),
                    t.ai_config_requests.c.id==row['request_record_id']).values(status='succeeded',
                        response_http_status=200,response_json=view,completed_at=now))
                await self.control._audit(db,row['created_by'],'validation.complete',validation_id,
                                         {'status':row['status'],'target_hash':row['target_hash']})
            return view

    async def get(self,db,actor,validation_id):
        await authorize(db,actor,'config.read')
        row = await self._row(db,validation_id)
        if row['status']=='running' and row['deadline_at']<=await self.control._now(db):
            # A crashed process must not replay unknown network work. Retire the
            # evidence after the lease expires and require an explicit new test.
            await db.rollback()
            async with db.begin():
                await authorize(db,actor,'config.read',lock=True)
                await self.control._manager(db)
                row = await self._row(db,validation_id,lock=True)
                if row['status']=='running' and row['deadline_at']<=await self.control._now(db):
                    row.update(status='unverified',result={'reason':'PROBE_RESULT_UNKNOWN','usage':None})
                    await db.execute(t.ai_config_validations.update().where(scoped(t.ai_config_validations),
                        t.ai_config_validations.c.id==validation_id).values(status='unverified',result=row['result']))
                    await db.execute(t.ai_config_requests.update().where(scoped(t.ai_config_requests),
                        t.ai_config_requests.c.id==row['request_record_id']).values(status='succeeded',
                            response_http_status=200,response_json=self.view(row),completed_at=await self.control._now(db)))
        return self.view(row)
