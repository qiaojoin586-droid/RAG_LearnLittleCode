"""Reconcile real, isolated Chroma catalogs with committed SQL state."""

from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from app.model_config.security import SCOPE
from app.model_config.tables import rag_index_versions
from app.models.knowledge import KnowledgeDocument
from app.models.knowledge_ingest import knowledge_ingest_jobs
from app.models.note import Note
from app.models.note_index import note_index_jobs
from app.rag import reconcile as reconciliation
from app.rag.maintenance_lock import VectorStoreBusy, local_vector_owner
from app.rag.note_index import rebuild_note_index
from test_model_config_control_plane import store  # noqa: F401
from test_transactional_tool_operations import local_tools  # noqa: F401


@pytest.fixture
def indexes(tmp_path, local_tools, monkeypatch):
    from chromadb import PersistentClient
    from chromadb.config import Settings

    for name in ('TEMP', 'TMP', 'SQLITE_TMPDIR'):
        monkeypatch.setenv(name, str(tmp_path))
    fx = local_tools
    other = fx.store.other.user_id
    owner = fx.user_id
    note, deleted, stale, absent, foreign, unmatched = (str(uuid4()) for _ in range(6))
    for table in (KnowledgeDocument.__table__, knowledge_ingest_jobs):
        table.create(fx.store.engine)
    fx.store.sql(Note.__table__.insert().values([
        dict(id=identifier, user_id=user, title='probe', content='confidential note', deleted_at=deleted_at)
        for identifier, user, deleted_at in [(note, owner, None), (deleted, owner, datetime(2026, 1, 1)),
                                              (stale, owner, None), (foreign, other, None)]
    ]))
    fx.store.sql(note_index_jobs.insert().values(note_id=unmatched, user_id=owner, revision=str(uuid4()),
        operation='delete', status='failed', attempts=3, next_attempt_at=datetime(2026, 1, 1), error_code='CLEANUP_FAILED'))
    fx.store.sql(note_index_jobs.insert().values(note_id=stale, user_id=owner, revision=str(uuid4()),
        operation='upsert', status='succeeded', attempts=1, next_attempt_at=datetime(2026, 1, 1)))
    fx.store.sql(KnowledgeDocument.__table__.insert().values([
        dict(id=number, user_id=user, filename='probe.md', file_path='probe.md', file_size=8, file_type='text/markdown',
             md5_hash='hash', chunk_count=chunks, ready_for_retrieval=published)
        for number, user, chunks, published in [(1, owner, 2, True), (2, owner, 1, True),
                                                 (3, owner, 1, False), (4, other, 1, True)]
    ]))
    directory = tmp_path / 'chroma'
    with PersistentClient(path=str(directory), settings=Settings(anonymized_telemetry=False)) as client:
        notes = client.create_collection('notes_collection', embedding_function=None)
        rag = client.create_collection('rag_collection', embedding_function=None)
        archive = client.create_collection('notes_archive', embedding_function=None)
        replaced = client.create_collection('notes_replaced', embedding_function=None)

        def add(collection, identifier, text, **metadata):
            collection.upsert(ids=[identifier], documents=[text], embeddings=[[1., 0.]], metadatas=[metadata])

        for identifier, source, user, text in [
            ('valid', note, owner, 'confidential note'), ('deleted', deleted, owner, 'old'),
            ('stale', stale, owner, 'confidential note'),
            ('orphan', absent, owner, 'old'), ('owner-mismatch', foreign, owner, 'old'), ('foreign', absent, other, 'other'),
        ]:
            add(notes, identifier, text, note_id=source, user_id=user)
        for identifier, document, user, chunk in [
            ('doc-valid', 1, owner, 0), ('doc-duplicate', 1, owner, 0), ('doc-pending', 3, owner, 0),
            ('doc-foreign', 4, other, 0), ('doc-orphan', 900, owner, 0), ('doc-invalid-chunk', 1, owner, 3),
        ]:
            add(rag, identifier, 'confidential knowledge', document_id=str(document), chunk_index=chunk, user_id=user)
        add(archive, 'archive-orphan', 'old', note_id=absent, user_id=owner)
        add(replaced, 'do-not-remove', 'old', note_id=absent, user_id=owner)
        for version, collection, collection_id in [(1, archive, str(archive.id)), (2, archive, str(archive.id)),
                                                     (3, replaced, str(uuid4()))]:
            fx.store.sql(rag_index_versions.insert().values(id=str(uuid4()), **SCOPE, index_key='notes',
                index_version=version, collection_name=collection.name, dimension=2, semantic_identity={},
                parser_version='p', chunker_version='c', status='retired', created_at=datetime(2026, 1, 1),
                storage_binding={'adapter': 'chroma_local', 'directory': str(directory), 'tenant': 'default_tenant',
                                 'database': 'default_database', 'collection_id': collection_id, 'evidence_hash': 'a' * 64,
                                 'provenance': 'operator_attested_build_record_v1'}))
    return fx, directory, deleted


def test_local_store_lock_excludes_other_owners_and_releases(tmp_path):
    with local_vector_owner(tmp_path):
        with pytest.raises(VectorStoreBusy), local_vector_owner(tmp_path):
            pytest.fail('a second owner acquired the local store')
    with local_vector_owner(tmp_path):
        pass


@pytest.mark.asyncio
async def test_report_and_orphan_cleanup_across_collections_preserve_other_rows(indexes, tmp_path):
    from chromadb import PersistentClient
    from chromadb.config import Settings

    fx, directory, _ = indexes
    legacy = {'persist_directory': str(directory)}

    def catalog_digest():
        return sha256((directory / 'chroma.sqlite3').read_bytes()).digest()

    initial_digest = catalog_digest()
    audit = await reconciliation.reconcile(fx.factory, legacy, workspace=tmp_path)
    assert catalog_digest() == initial_digest and 'confidential' not in str(audit)
    findings = {item['collection']: item for item in audit['collections']}
    assert len(findings) == len(audit['collections']) == 4
    assert findings['notes_replaced']['status'] == 'collection_identity_mismatch'
    assert len(findings['notes_archive']['index_version_ids']) == 2
    assert findings['notes_collection']['counts'] == {
        'deleted_note_vector': 1, 'missing_source_vectors': 2, 'orphan_vector': 2, 'owner_mismatch': 1,
        'stale_vector': 1, 'valid_vector': 1,
    }
    assert findings['rag_collection']['counts'] == {
        'duplicate_chunk': 1, 'missing_source_vectors': 1, 'orphan_vector': 1, 'partial_source_vectors': 1,
        'pending_or_failed_vector': 1, 'unexpected_chunk': 1, 'unpublished_source': 1, 'valid_vector': 2,
    }
    assert audit['jobs']['notes'] == [{'operation': 'delete', 'status': 'failed', 'count': 1},
                                     {'operation': 'upsert', 'status': 'succeeded', 'count': 1}]
    with pytest.raises(ValueError):
        await reconciliation.reconcile(fx.factory, legacy, apply=True, workspace=tmp_path)

    repaired = await reconciliation.reconcile(fx.factory, legacy, apply=True, owner=fx.user_id, workspace=tmp_path)
    assert {item['collection']: item.get('purged_vectors') for item in repaired['collections']} == {
        'notes_collection': 2, 'rag_collection': 1, 'notes_archive': 1, 'notes_replaced': None,
    }
    with PersistentClient(path=str(directory), settings=Settings(anonymized_telemetry=False)) as client:
        assert set(client.get_collection('notes_collection').get(include=[])['ids']) == {
            'valid', 'stale', 'foreign', 'owner-mismatch',
        }
        assert set(client.get_collection('rag_collection').get(include=[])['ids']) == {
            'doc-valid', 'doc-duplicate', 'doc-pending', 'doc-foreign', 'doc-invalid-chunk',
        }
        assert client.get_collection('notes_replaced').get(include=[])['ids'] == ['do-not-remove']
    repeated = await reconciliation.reconcile(fx.factory, legacy, apply=True, owner=fx.user_id, workspace=tmp_path)
    assert all(item.get('purged_vectors', 0) == 0 for item in repeated['collections'])


@pytest.mark.asyncio
async def test_cleanup_rechecks_source_before_deleting(indexes, tmp_path, monkeypatch):
    fx, directory, deleted = indexes
    purge = reconciliation._purge

    async def restore_before_purge(factory, location, key, collection_id, rows):
        if location.name == 'notes_collection':
            fx.store.sql(Note.__table__.update().where(Note.id == deleted).values(deleted_at=None))
        return await purge(factory, location, key, collection_id, rows)

    monkeypatch.setattr(reconciliation, '_purge', restore_before_purge)
    report = await reconciliation.reconcile(fx.factory, {'persist_directory': str(directory)}, apply=True,
                                            owner=fx.user_id, workspace=Path(tmp_path))
    notes = next(item for item in report['collections'] if item['collection'] == 'notes_collection')
    assert notes['counts']['deleted_note_vector'] == 1 and notes['purged_vectors'] == 1


@pytest.mark.asyncio
async def test_note_rebuild_queues_once_for_the_current_owner(indexes):
    fx, _, deleted = indexes
    old = next(job for job in fx.store.rows(note_index_jobs) if job['operation'] == 'upsert')
    async with fx.factory() as db, db.begin():
        assert not await rebuild_note_index(db, fx.user_id, deleted)
        assert not await rebuild_note_index(db, fx.store.other.user_id, old['note_id'])
        assert await rebuild_note_index(db, fx.user_id, old['note_id'])
        assert not await rebuild_note_index(db, fx.user_id, old['note_id'])
    job = next(job for job in fx.store.rows(note_index_jobs) if job['note_id'] == old['note_id'])
    assert job['status'] == 'queued' and job['revision'] != old['revision'] and job['attempts'] == 0
