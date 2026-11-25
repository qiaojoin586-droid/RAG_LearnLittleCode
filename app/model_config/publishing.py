"""Atomic publication/restore: manifest, head CAS, references, audit and outbox."""
from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa

from app.model_config import tables as t
from app.model_config.contracts import content_hash,validate,upgrade_payload
from app.model_config.errors import fail
from app.model_config.prompt_contracts import prompt_metadata,validate_prompt
from app.model_config.probes import STRUCTURED_ROLES
from app.model_config.security import SCOPE,scoped
from app.model_config.service import uid,json_view
from app.model_config.validation_service import ValidationService


class PublishingService:
    def __init__(self,control,session_factory):
        self.control=control
        self.session_factory=session_factory

    async def _commit(self,db,actor,draft,head,*,historical=None):
        if draft['base_head_version']!=head['head_version']:
            fail(40927,'REBASE_REQUIRED')
        view=await self.control._view(db,draft,register=True)
        if view['binding_issues']:
            fail(42222,'ROLE_BINDINGS_INCOMPLETE',422,issues=view['binding_issues'])
        raw=draft['payload']['role_bindings_raw']
        from app.model_config.index_binding import BUILD_PROVENANCE, MIGRATION_PROVENANCE, SUPPORTED_INDEXES, check_binding, published_index_ids
        index_bindings=draft['payload']['index_bindings']
        active = None
        if head['active_release_id']:
            active = await self.control._entity(db,t.ai_config_releases,head['active_release_id'])
        active_bindings = active['index_bindings'] if active else {}
        switch_required, target_indexes, new_candidates = False, {}, []
        if raw['embedding']['enabled']:
            if set(index_bindings)!=SUPPORTED_INDEXES:
                fail(42220,'INDEX_BASELINE_NOT_VERIFIED',422,migration_required=True)
        if index_bindings:
            if set(index_bindings)!=SUPPORTED_INDEXES:fail(42220,'INDEX_BASELINE_NOT_VERIFIED',422)
            embedding=view['resolved_static_bindings']['embedding']
            for key,binding in index_bindings.items():
                index=await self.control._entity(db,t.rag_index_versions,binding['index_version_id'])
                profile=await self.control._entity(db,t.ai_profile_revisions,binding['query_profile_revision_id'],draft['id'])
                if binding['query_profile_revision_id']!=embedding['profile_revision_id']:
                    fail(42220,'INDEX_QUERY_PROFILE_NOT_SELECTED',422)
                check_binding(index,profile,binding)
                target_indexes[key] = index
                live = active_bindings.get(key, {}).get('index_version_id')
                if live == index['id']:
                    continue
                history = set(await published_index_ids(db,key))
                staged = index['storage_binding']['provenance'] in (MIGRATION_PROVENANCE, BUILD_PROVENANCE)
                if live is None and history <= {index['id']} and not staged:
                    continue  # Existing M1 bootstrap and unchanged baseline after a text-only release.
                if not staged and index['id'] not in history:
                    fail(42220,'INDEX_MIGRATION_REQUIRED',422,migration_required=True)
                if staged and index['id'] not in history:
                    new_candidates.append(index)
                switch_required = True
        validations=[]
        now=await self.control._now(db)
        probe=ValidationService(self.control,self.session_factory)
        for role,binding in raw.items():
            if not binding['enabled']:
                continue
            backups = binding['fallback']
            if backups and (len(backups) != 1 or role == 'embedding' or binding['selection'] == 'follow_execution'):
                fail(42222, 'FALLBACK_SINGLE_CHAT_MODEL_REQUIRED', 422, role_key=role)
            targets = [(None, None)]
            if backups:
                backup = backups[0]
                targets.append((backup, (await probe._target(db, draft, role, backup))[0]))
            if role!='embedding':
                prompt=next((row for row in view['prompts'] if row['id']==binding['prompt_revision_id']),None)
                if not prompt:
                    fail(42222,'PROMPT_REQUIRED',422,role_key=role)
                metadata=validate_prompt(role,prompt)
                if any(prompt[key]!=value for key,value in metadata.items()):
                    fail(42222,'PROMPT_REVISION_REQUIRES_RESAVE',422,role_key=role)
            if binding['selection']=='follow_execution':
                continue  # Actual execution profile is checked against this same snapshot.
            for backup, target in targets:
                if target is None:
                    target,*_ = await probe._target(db,draft,role)
                where=[scoped(t.ai_config_validations),t.ai_config_validations.c.role_key==role,
                    t.ai_config_validations.c.target_hash==content_hash(target),t.ai_config_validations.c.status=='passed']
                if historical:
                    where.append(t.ai_config_validations.c.id.in_(historical['validation_refs']))
                else:
                    where.append(t.ai_config_validations.c.valid_until>now)
                    where.extend([t.ai_config_validations.c.draft_id==draft['id'],
                                  t.ai_config_validations.c.draft_version==draft['draft_version']])
                validation=(await db.execute(sa.select(t.ai_config_validations).where(*where).order_by(
                    t.ai_config_validations.c.tested_at.desc()).limit(1))).mappings().first()
                if not validation or role in STRUCTURED_ROLES and not (validation['result'] or {}).get('role_schema_verified'):
                    if backup:
                        fail(42222, 'FALLBACK_CAPABILITY_UNVERIFIED', 422, role_key=role, fallback_role_key=backup)
                    fail(42222,'CAPABILITY_UNVERIFIED',422,role_key=role)
                validations.append(validation['id'])
        static={}
        for role,binding in view['resolved_static_bindings'].items():
            credential_id=binding.get('credential_revision_id')
            credential=await self.control._entity(db,t.ai_secrets,credential_id,draft['id']) if credential_id else None
            static[role]={key:binding.get(key) for key in ('enabled','profile_revision_id','credential_revision_id',
                                                           'prompt_revision_id','fallback','failure_mode')}
            static[role]['credential_version']=credential['credential_version'] if credential else None
        for identifier in sorted({row['credential_revision_id'] for row in view['profiles'] if row['credential_revision_id']}):
            secret=await self.control._entity(db,t.ai_secrets,identifier,draft['id'])
            self.control.keyring.decrypt(secret['ciphertext'],secret['key_version'],{
                **SCOPE,'id':secret['id'],'origin':secret['allowed_origin'],'version':secret['credential_version']})
        manifest={'schema_version':draft['payload']['schema_version'],'role_bindings_raw':raw,'resolved_static_bindings':static,
                  'follow_execution_roles':view['follow_execution_roles'],'index_bindings':draft['payload']['index_bindings']}
        validate('ReleaseManifestV3' if manifest['schema_version'] == 3 else 'ReleaseManifest',manifest)
        switch_evidence = None
        build_id = None
        catch_up_ids = []
        if switch_required:
            from app.model_config.index_restore import lock_catchups_for_switch
            from app.model_config.index_switch import assert_isolated_candidates, verify_index_switch

            catch_up_ids = await lock_catchups_for_switch(db, target_indexes)
            if any(index['storage_binding']['provenance'] == BUILD_PROVENANCE for index in new_candidates):
                from app.model_config.index_build import lock_build_for_publish

                build_id = await lock_build_for_publish(db, draft['id'], target_indexes)
            if new_candidates:
                from app.utils.config import get_chroma_config

                await assert_isolated_candidates(db, new_candidates, get_chroma_config())
            switch_evidence = await verify_index_switch(db, target_indexes)
        release_id=uid()
        number=head['last_release_number']+1
        await db.execute(t.ai_config_releases.insert().values(id=release_id,**SCOPE,release_number=number,
            draft_id=draft['id'],draft_version=draft['draft_version'],restore_from_release_id=historical['id'] if historical else None,
            request_record_id=head['request_record_id'],**manifest,validation_refs=validations,
            content_hash=content_hash(manifest),created_by=actor.user_id,created_at=now))
        references=set()
        for binding in list(raw.values())+list(static.values()):
            for kind in ('profile','prompt','credential'):
                if binding.get(kind+'_revision_id'):references.add((kind,binding[kind+'_revision_id']))
        for profile in view['profiles']:
            references.add(('endpoint_policy',profile['endpoint_policy_revision_id']))
            if profile['credential_revision_id']:references.add(('credential',profile['credential_revision_id']))
        references.update(('index',binding['index_version_id']) for binding in index_bindings.values())
        for kind,entity_id in sorted(references):
            await db.execute(t.ai_config_release_refs.insert().values(**SCOPE,release_id=release_id,entity_kind=kind,entity_id=entity_id))
            table={'credential':t.ai_secrets,'profile':t.ai_profile_revisions,'prompt':t.ai_prompt_revisions}.get(kind)
            if table is not None:
                await db.execute(table.update().where(scoped(table),table.c.id==entity_id,table.c.status=='candidate').values(
                    status='active' if kind=='credential' else 'published'))
        changed=await db.execute(t.ai_config_heads.update().where(scoped(t.ai_config_heads),
            t.ai_config_heads.c.head_version==head['head_version'],
            t.ai_config_heads.c.manager_generation==head['manager_generation'],
            t.ai_config_heads.c.manager_owner_id==self.control.owner_id).values(active_release_id=release_id,
                head_version=head['head_version']+1,last_release_number=number,updated_at=now))
        if changed.rowcount!=1:fail(40920,'HEAD_VERSION_CONFLICT')
        switched_jobs = catch_up_ids + ([build_id] if build_id is not None else [])
        if switched_jobs:
            from app.models.index_build import index_builds

            await db.execute(index_builds.update().where(index_builds.c.id.in_(switched_jobs))
                             .values(status='switched', error_code=None))
        await db.execute(t.ai_config_drafts.update().where(scoped(t.ai_config_drafts),
            t.ai_config_drafts.c.id==draft['id']).values(status='published',updated_at=now))
        await db.execute(t.outbox_events.insert().values(id=uid(),**SCOPE,event_key='release:'+release_id,
            event_type='model_config.published',aggregate_id=release_id,aggregate_version=head['head_version']+1,
            payload={'release_id':release_id,'head_version':head['head_version']+1},status='pending',attempts=0,
            next_attempt_at=now,created_at=now))
        result={'release_id':release_id,'release_number':number,'head_version':head['head_version']+1,
            'draft_id':draft['id'],'published_at':json_view(now),'effective_for':'new_requests',
            'embedding_state':'connection_updated' if index_bindings and any(
                not head['active_release_id'] or active['index_bindings'].get(key)!=binding for key,binding in index_bindings.items()) else 'unchanged',
            'embedding_enabled':raw['embedding']['enabled'],
            'historical_offline':bool(historical),'restore_from_release_id':historical['id'] if historical else None}
        audit = {'release_id':release_id,'head_version_before':head['head_version'],
            'head_version_after':head['head_version']+1,'restore_from_release_id':result['restore_from_release_id']}
        if switch_evidence is not None:
            audit['index_switch'] = switch_evidence
        if catch_up_ids:
            audit['index_catch_up_jobs'] = catch_up_ids
        return result,release_id,audit

    async def publish(self,db,actor,draft_id,body,key,proof,match):
        validate('PublishInput',body)
        async def operation(head):
            if body['expected_head_version']!=head['head_version']:
                fail(40920,'HEAD_VERSION_CONFLICT',actual=head['head_version'])
            draft=await self.control._draft(db,draft_id,edit=True,match=match)
            if draft['draft_version']!=body['expected_draft_version']:fail(40921,'DRAFT_VERSION_CONFLICT')
            return await self._commit(db,actor,draft,head)
        return await self.control._mutate(db,actor,'draft.publish','config.publish',draft_id,body,key,
            operation,proof=proof,sensitive=True,match=match)

    async def restore(self,db,actor,release_id,body,key,proof):
        validate('RestoreInput',body)
        async def operation(head):
            if body['expected_head_version']!=head['head_version']:fail(40920,'HEAD_VERSION_CONFLICT')
            release=await self.control._entity(db,t.ai_config_releases,release_id)
            payload=upgrade_payload({key:release[key] for key in ('schema_version','role_bindings_raw','index_bindings')})
            now=await self.control._now(db)
            draft=dict(id=uid(),**SCOPE,base_release_id=head['active_release_id'],base_head_version=head['head_version'],
                draft_version=1,payload=payload,payload_hash=content_hash(payload),status='editing',
                expires_at=now+timedelta(days=7),created_by=actor.user_id,created_at=now,updated_at=now)
            await db.execute(t.ai_config_drafts.insert().values(**draft))
            return await self._commit(db,actor,draft,head,historical=release)
        return await self.control._mutate(db,actor,'release.restore','config.restore',release_id,body,key,
            operation,proof=proof,sensitive=True)
