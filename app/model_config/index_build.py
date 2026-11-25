"""Authorize candidate index builds and attach completed collections to a draft."""

import copy

import sqlalchemy as sa

from app.model_config import tables
from app.model_config.contracts import content_hash, validate
from app.model_config.embedding_space import semantic_fingerprint
from app.model_config.errors import fail
from app.model_config.index_attestation import bind_embedding_identity
from app.model_config.index_binding import BUILD_PROVENANCE, SUPPORTED_INDEXES, check_binding
from app.model_config.security import SCOPE, authorize, scoped
from app.model_config.service import etag, json_view, uid
from app.model_config.validation_service import ValidationService
from app.models.index_build import index_builds as jobs


def job_view(job):
    fields = ('id', 'draft_id', 'draft_version', 'status', 'index_ids', 'source_watermark', 'error_code', 'created_at')
    result = {field: job[field] for field in fields}
    if job.get('restore_release_id'):
        result['restore_release_id'] = job['restore_release_id']
    return json_view(result)


async def save_draft(control, db, draft):
    validate('DraftPayload', draft['payload'])
    draft.update(draft_version=draft['draft_version'] + 1, status='editing',
                 payload_hash=content_hash(draft['payload']), updated_at=await control._now(db))
    fields = ('draft_version', 'status', 'payload', 'payload_hash', 'updated_at')
    await db.execute(tables.ai_config_drafts.update().where(scoped(tables.ai_config_drafts),
        tables.ai_config_drafts.c.id == draft['id']).values(**{field: draft[field] for field in fields}))


class IndexBuildService:
    def __init__(self, control, session_factory):
        self.control = control
        self.validation = ValidationService(control, session_factory)

    async def enqueue(self, db, actor, body, key, proof, match):
        if (set(body) != {'draft_id', 'semantic_identity'} or not isinstance(body['draft_id'], str)
                or not 1 <= len(body['draft_id']) <= 36):
            fail(40002, 'INDEX_BUILD_INPUT_INVALID', 422)
        draft_id = body['draft_id']
        identity = body['semantic_identity']
        evidence = semantic_fingerprint(identity)

        async def operation(head):
            draft = await self.control._draft(db, draft_id, edit=True, match=match)
            if draft['base_head_version'] != head['head_version']:
                fail(40927, 'REBASE_REQUIRED')
            active = (await db.execute(sa.select(jobs.c.id).where(jobs.c.draft_id == draft_id,
                jobs.c.draft_version == draft['draft_version'], jobs.c.status.in_(
                    ('queued', 'building', 'catching_up', 'ready_to_switch'))))).first()
            if active:
                fail(40923, 'INDEX_BUILD_ALREADY_EXISTS')

            draft['payload'] = copy.deepcopy(draft['payload'])
            # Reserve this draft revision for the build. An earlier pair must not
            # become publishable through an unrelated edit during registration.
            draft['payload']['index_bindings'] = {}
            await bind_embedding_identity(self.control, db, actor, draft, identity, evidence, BUILD_PROVENANCE)
            await save_draft(self.control, db, draft)
            target, *_ = await self.validation._target(db, draft, 'embedding')
            now = await self.control._now(db)
            job = dict(id=uid(), draft_id=draft_id, draft_version=draft['draft_version'], actor_id=actor.user_id,
                       policy_version=head['authorization_policy_version'], target_hash=content_hash(target),
                       status='queued', next_run_at=now, attempts=0, created_at=now, index_ids=None,
                       source_watermark=None, error_code=None)
            await db.execute(jobs.insert().values(**job))
            result = {**job_view(job), 'etag': etag(draft)}
            return result, job['id'], {'draft_id': draft_id, 'index_build_id': job['id'], 'profile_target_hash': job['target_hash']}

        return await self.control._mutate(db, actor, 'index.build.enqueue', 'config.publish', draft_id, body, key,
                                          operation, proof=proof, match=match, sensitive=True, http_status=202)

    async def enqueue_restore(self, db, actor, release_id, body, key, proof):
        from app.model_config.index_restore import overlapping_catchups, restore_target

        validate('RestoreInput', body)

        async def operation(head):
            if body['expected_head_version'] != head['head_version']:
                fail(40920, 'HEAD_VERSION_CONFLICT', actual=head['head_version'])
            release, indexes, _, _, _, target_hash = await restore_target(self.control, db, release_id, head['active_release_id'])
            for existing in await overlapping_catchups(db, indexes):
                if existing['restore_release_id'] == release_id:
                    return job_view(existing), existing['id'], {'index_build_id': existing['id'], 'restore_release_id': release_id}
                fail(40923, 'INDEX_RESTORE_CATCH_UP_CONFLICT', job_id=existing['id'])
            now = await self.control._now(db)
            job = dict(id=uid(), draft_id=release['draft_id'], draft_version=release['draft_version'],
                       restore_release_id=release_id, actor_id=actor.user_id, policy_version=head['authorization_policy_version'],
                       target_hash=target_hash, status='queued', next_run_at=now, attempts=0, created_at=now,
                       collections={name: index['storage_binding']['collection_id'] for name, index in indexes.items()},
                       index_ids={name: index['id'] for name, index in indexes.items()}, source_watermark=None, error_code=None)
            await db.execute(jobs.insert().values(**job))
            return job_view(job), job['id'], {'index_build_id': job['id'], 'restore_release_id': release_id}

        return await self.control._mutate(db, actor, 'index.restore.catch_up', 'config.restore', release_id, body, key,
                                          operation, proof=proof, sensitive=True, http_status=202)

    async def status(self, db, actor, job_id):
        await authorize(db, actor, 'config.read')
        row = (await db.execute(sa.select(jobs).where(jobs.c.id == job_id))).mappings().first()
        if not row:
            fail(40401, 'CONFIG_RESOURCE_NOT_FOUND', 404)
        return job_view(row)

    async def attach(self, db, draft, job, profile, directory, collections, watermark):
        indexes, bindings = {}, {}
        now = await self.control._now(db)
        for key in sorted(SUPPORTED_INDEXES):
            storage = {'adapter': 'chroma_local', 'directory': str(directory), 'tenant': 'default_tenant',
                       'database': 'default_database', 'collection_id': collections[key], 'provenance': BUILD_PROVENANCE,
                       'evidence_hash': content_hash({'target': job['target_hash'], 'collection_id': collections[key]})}
            index = dict(id=uid(), **SCOPE, index_key=key, index_version=await self.control._counter(db, 'index', key),
                collection_name=key + '_collection', storage_binding=storage, build_profile_revision_id=profile['id'],
                semantic_fingerprint=profile['semantic_fingerprint'], semantic_identity=profile['semantic_identity'],
                dimension=profile['embedding_dimension'], parser_version='published-source-or-legacy-v2',
                chunker_version='published-spans-or-legacy-v2', source_watermark=watermark, status='ready', created_at=now)
            binding = {'index_version_id': index['id'], 'query_profile_revision_id': profile['id'],
                       'credential_revision_id': profile['credential_revision_id']}
            check_binding(index, profile, binding)
            await db.execute(tables.rag_index_versions.insert().values(**index))
            await self.control._inventory(db, draft['id'], 'index', index['id'])
            bindings[key] = binding
            indexes[key] = index['id']
        draft['payload']['index_bindings'] = bindings
        await save_draft(self.control, db, draft)
        await self.control._audit(db, job['actor_id'], 'index.build.ready', draft['id'],
            {'index_build_id': job['id'], 'index_ids': indexes, 'source_watermark': watermark})
        return indexes


async def lock_build_for_publish(db, draft_id, indexes):
    expected = {key: index['id'] for key, index in indexes.items()}
    rows = (await db.execute(sa.select(jobs).where(jobs.c.draft_id == draft_id, jobs.c.restore_release_id.is_(None),
        jobs.c.status != 'switched').order_by(jobs.c.id).with_for_update())).mappings().all()
    job = next((row for row in rows if row['index_ids'] == expected), None)
    if job is None or job['status'] != 'ready_to_switch' or job['lease_token'] is not None:
        fail(42220, 'INDEX_BUILD_NOT_READY', 422)
    return job['id']
