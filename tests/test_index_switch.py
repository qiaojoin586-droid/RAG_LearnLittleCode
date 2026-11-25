"""Real Chroma cutover and restore through normal publication with isolated model transports."""

import copy
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config import tables as config
from app.model_config.embedding_status import EmbeddingStatusService
from app.model_config.index_attestation import IndexAttestationService, prepare_attestation
from app.model_config.index_build import IndexBuildService
from app.model_config.index_inventory import inspect_indexes
from app.model_config.publishing import PublishingService
from app.model_config.provider import ProviderGateway
from app.model_config.service import utcnow
from app.models.category import NoteCategory
from app.models.index_build import index_builds
from app.models.knowledge import KnowledgeDocument, knowledge_search_chunks
from app.models.knowledge_ingest import knowledge_ingest_jobs
from app.models.note import Note
from app.models.note_index import note_index_jobs
from app.rag.ingest_worker import KnowledgeIngestWorker
from app.rag.index_build_worker import IndexBuildWorker
from test_model_config_control_plane import AsyncFacade, admin, store  # noqa: F401
from test_model_config_publication import database_factory, publish, selected, snapshot
from test_model_config_validation import execute, validation_service
from test_transactional_tool_operations import ToolSession
from test_vector_binding_runtime import probe_response, vectors  # noqa: F401


def seed_collection(directory, owner, note_id, title, *, setup=False, extra_doc=False):
    from chromadb import PersistentClient
    from chromadb.config import Settings

    with PersistentClient(path=str(directory), settings=Settings(anonymized_telemetry=False)) as client:
        for key in ('rag', 'notes'):
            name = key + '_collection'
            if setup:
                collection = client.create_collection(name, metadata={'hnsw:space': 'cosine'}, embedding_function=None)
            else:
                collection = client.get_collection(name, embedding_function=None)
                old_ids = collection.get(include=[])['ids']
                if old_ids:
                    collection.delete(ids=old_ids)
            if key == 'rag':
                ids = ['1_0', '2_0'] if extra_doc else ['1_0']
                collection.upsert(ids=ids, documents=['document body'] * len(ids), embeddings=[[1., 0.]] * len(ids),
                    metadatas=[{'user_id': owner, 'document_id': identifier[0], 'chunk_index': 0} for identifier in ids])
            else:
                collection.upsert(ids=[f'note_{note_id}'], documents=[title], embeddings=[[1., 0.]],
                                  metadatas=[{'user_id': owner, 'note_id': note_id}])


async def stage(v, directory, draft, tmp_path):
    admin = v.admin
    inventory = inspect_indexes(directory, workspace=tmp_path)
    with Session(admin.engine) as session:
        baseline, _ = await EmbeddingStatusService(admin.service).register(
            AsyncFacade(session), admin.actor.user_id, inventory, 'migration-observation')
    record = copy.deepcopy(v.plan['record'])
    record['semantic_identity']['query_prefix'] = 'new query: '
    record['semantic_identity']['document_prefix'] = 'new document: '
    record['collections'] = {item['index_key']: {key: item[key] for key in ('collection_name', 'collection_id', 'dimension')}
                             for item in inventory['collections']}
    artifact = Path(tmp_path) / 'migration-record.json'
    artifact.write_text(json.dumps(record), encoding='utf-8')
    plan = prepare_attestation(artifact, directory, {item['index_key']: item['id'] for item in baseline['items']}, workspace=tmp_path)
    with Session(admin.engine) as session:
        result, _ = await IndexAttestationService(admin.service).attach(AsyncFacade(session), admin.actor.user_id,
            draft['id'], plan, 'migration-stage', draft['etag'], migration=True)
    assert not result['published'] and result['provenance'] == 'operator_attested_migration_v1'
    draft = await admin.call('get_draft', draft['id'])
    validator, _ = validation_service(admin, probe_response)
    verification, _ = await execute(admin, validator, draft, 'embedding', key='migration-probe')
    assert verification['status'] == 'passed'
    return draft


async def restore(admin, release, head, key):
    with Session(admin.engine) as session:
        return await PublishingService(admin.service, database_factory(admin)).restore(
            AsyncFacade(session), admin.actor, release, {'expected_head_version': head}, key, admin.proof())


@pytest.mark.asyncio
async def test_published_switch_and_rollback_require_current_source_coverage(vectors, tmp_path):
    v, owner = vectors, vectors.admin.actor.user_id
    admin = v.admin
    documents, notes = KnowledgeDocument.__table__, Note.__table__
    for table in (NoteCategory.__table__, documents, knowledge_search_chunks, notes, knowledge_ingest_jobs,
                  note_index_jobs, index_builds):
        table.create(admin.engine)
    note_id = str(uuid4())
    admin.sql(notes.insert().values(id=note_id, user_id=owner, title='note title', content='original note'))
    root = tmp_path / 'uploads'
    source = root / owner / 'fixture.md'
    source.parent.mkdir(parents=True)
    content = b'document body'
    source.write_bytes(content)
    admin.sql(documents.insert().values([dict(id=doc_id, user_id=owner, filename='fixture.md', file_path=str(source),
        file_size=len(content), file_type='text/markdown', md5_hash=hashlib.md5(content, usedforsecurity=False).hexdigest(),
        chunk_count=1) for doc_id in (1, 2)]))

    draft, _ = await admin.call('create_draft', {'base_release_id': v.snapshot.release_id,
        'expected_head_version': v.published['head_version']}, 'switch-draft')
    version_count = admin.count(config.rag_index_versions)
    with Session(admin.engine) as session:
        with pytest.raises(BusinessError) as alias:
            await IndexAttestationService(admin.service).attach(AsyncFacade(session), owner, draft['id'], v.plan,
                                                               'alias', draft['etag'], migration=True)
    assert alias.value.data['reason'] == 'INDEX_MIGRATION_REQUIRES_ISOLATED_COLLECTION'
    assert admin.count(config.rag_index_versions) == version_count

    seed_collection(v.directory, owner, note_id, 'original note', extra_doc=True)
    target = tmp_path / 'candidate'
    seed_collection(target, owner, note_id, 'original note', setup=True, extra_doc=True)
    draft = await stage(v, target, draft, tmp_path)
    count = admin.count(config.ai_config_releases)
    # A job using the active target cannot straddle a migration cutover.
    job_id = str(uuid4())
    admin.sql(knowledge_ingest_jobs.insert().values(id=job_id, user_id=owner, document_id=100,
        release_id=v.snapshot.release_id, filename='fixture.md', operation='delete', status='running',
        stage='queued', attempts=1, next_attempt_at=utcnow()))
    with pytest.raises(BusinessError) as pending:
        await publish(admin, draft, 'migration-publish')
    assert pending.value.data['reason'] == 'INDEX_SWITCH_JOBS_PENDING'
    admin.sql(knowledge_ingest_jobs.update().where(knowledge_ingest_jobs.c.id == job_id).values(status='succeeded'))

    admin.sql(documents.delete().where(documents.c.id == 2))
    with pytest.raises(BusinessError) as stale:
        await publish(admin, draft, 'migration-publish')
    assert stale.value.data['reason'] == 'INDEX_SWITCH_NOT_CAUGHT_UP'
    assert stale.value.data['counts']['orphan_vector'] == 1
    assert admin.count(config.ai_config_releases) == count
    assert (await snapshot(admin)).release_id == v.snapshot.release_id

    # A stale note also blocks switching when document and deletion checks pass.
    admin.sql(notes.update().where(notes.c.id == note_id).values(content='edited after build'))
    seed_collection(target, owner, note_id, 'original note')
    with pytest.raises(BusinessError) as old_note:
        await publish(admin, draft, 'migration-publish')
    assert old_note.value.data['counts']['stale_vector'] == 1
    seed_collection(target, owner, note_id, 'edited after build')
    receipt, _ = await publish(admin, draft, 'migration-publish')
    assert (await publish(admin, draft, 'migration-publish'))[0] == receipt
    latest = await snapshot(admin)
    assert latest.release_id == receipt['release_id'] and latest.indexes.keys().isdisjoint(v.snapshot.indexes)
    with selected(admin, v.snapshot):
        assert (await v.service.search_notes('previous read', owner))[0]['content'] == 'original note'
    with selected(admin, latest):
        assert (await v.service.search_notes('new read', owner))[0]['content'] == 'edited after build'
    target_audit = next(row['redacted_diff']['index_switch'] for row in admin.rows(config.ai_config_audits)
                        if row['release_id'] == receipt['release_id'])
    assert len(target_audit['source_watermark']) == 64 and target_audit['targets']['rag']['vector_count'] == 1

    from chromadb import PersistentClient
    from chromadb.config import Settings

    # Simulate cleanup admitted by a request that started on the earlier release.
    def orphan_in(directory, *, add=False):
        with PersistentClient(path=str(directory), settings=Settings(anonymized_telemetry=False)) as client:
            collection = client.get_collection('rag_collection', embedding_function=None)
            if add:
                collection.upsert(ids=['99_0'], documents=['deleted'], embeddings=[[1., 0.]],
                                  metadatas=[{'user_id': owner, 'document_id': '99', 'chunk_index': 0}])
            return bool(collection.get(ids=['99_0'], include=[])['ids'])

    assert orphan_in(v.directory, add=True) and orphan_in(target, add=True)
    admin.sql(knowledge_ingest_jobs.insert().values(id=str(uuid4()), user_id=owner, document_id=99,
        release_id=v.snapshot.release_id, filename='fixture.md', file_path=str(root / owner / 'deleted.md'),
        operation='delete', status='queued', stage='queued', attempts=0, next_attempt_at=utcnow().replace(microsecond=0)))

    @asynccontextmanager
    async def factory():
        with Session(admin.engine) as session:
            yield ToolSession(session)

    assert await KnowledgeIngestWorker(factory, root=root, vectors=v.service).tick()
    assert orphan_in(v.directory) and not orphan_in(target)

    with pytest.raises(BusinessError) as stale_restore:
        await restore(admin, v.snapshot.release_id, receipt['head_version'], 'migration-restore')
    assert stale_restore.value.data['reason'] == 'INDEX_SWITCH_NOT_CAUGHT_UP'
    assert (await snapshot(admin)).release_id == latest.release_id
    service = IndexBuildService(admin.service, factory)
    head = {'expected_head_version': receipt['head_version']}
    async with factory() as db:
        with pytest.raises(BusinessError) as active:
            await service.enqueue_restore(db, admin.actor, latest.release_id, head, 'currently-active', admin.proof())
    assert active.value.data['reason'] == 'INDEX_RESTORE_TARGET_ACTIVE'
    async with factory() as db:
        queued, status = await service.enqueue_restore(db, admin.actor, v.snapshot.release_id, head, 'catch-up', admin.proof())
    async with factory() as db:
        repeated, _ = await service.enqueue_restore(db, admin.actor, v.snapshot.release_id, head, 'catch-up-retry', admin.proof())
    assert status == 202 and queued['restore_release_id'] == v.snapshot.release_id and repeated['id'] == queued['id']

    sent = []

    def gateway_factory(profile, policy, key, *, permit, probe_mode=False):
        async def transport(request):
            await permit()
            if request.method == 'GET':
                return probe_response(request)
            payload = json.loads(request.content)['input']
            texts = payload['texts'] if v.dashscope else payload
            sent.extend(texts)
            if v.native:
                return httpx.Response(200, json={'embeddings': [[1., 0.]] * len(texts)})
            if v.dashscope:
                return httpx.Response(200, json={'output': {'embeddings': [
                    {'text_index': number, 'embedding': [1., 0.]} for number in range(len(texts))]}})
            return httpx.Response(200, json={'data': [
                {'index': number, 'embedding': [1., 0.]} for number in range(len(texts))]})

        return ProviderGateway(profile, policy, key, permit=permit, probe_mode=probe_mode,
                               transport=httpx.MockTransport(transport))

    worker = IndexBuildWorker(admin.service, factory, uploads=root, gateway_factory=gateway_factory)
    assert (await worker.claim())['id'] == queued['id']
    with pytest.raises(BusinessError) as writing:
        await restore(admin, v.snapshot.release_id, receipt['head_version'], 'migration-restore')
    assert writing.value.data['reason'] == 'INDEX_RESTORE_CATCH_UP_RUNNING'
    past = utcnow() - timedelta(minutes=5)
    admin.sql(index_builds.update().values(lease_expires_at=past))
    assert await worker.tick()
    row = admin.rows(index_builds)[0]
    assert row['status'] == 'ready_to_switch', row['error_code']
    assert not orphan_in(v.directory)
    prefix = v.plan['record']['semantic_identity']['document_prefix']
    assert sent == [prefix + 'edited after build']
    assert (await snapshot(admin)).release_id == latest.release_id

    added = str(uuid4())
    admin.sql(notes.insert().values(id=added, user_id=owner, title='new note', content='added while waiting'))
    with pytest.raises(BusinessError) as advanced:
        await restore(admin, v.snapshot.release_id, receipt['head_version'], 'migration-restore')
    assert advanced.value.data['counts']['missing_source_vectors'] == 1
    admin.sql(index_builds.update().values(next_run_at=past))
    assert await worker.tick()
    assert sent == [prefix + 'edited after build', prefix + 'added while waiting']
    completed, _ = await restore(admin, v.snapshot.release_id, receipt['head_version'], 'migration-restore')
    assert completed['restore_from_release_id'] == v.snapshot.release_id
    assert (await snapshot(admin)).indexes.keys() == v.snapshot.indexes.keys()
    assert admin.rows(index_builds)[0]['status'] == 'switched'
    assert await worker.tick() is False
