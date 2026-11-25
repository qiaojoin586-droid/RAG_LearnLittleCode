"""Validate durable knowledge admission and lease recovery with real MySQL."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.orm import Session


async def _check_retrieval_timeout(url, owner, document_id, report):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.rag.keyword_search import KeywordSearch
    from app.rag.rag_service import RagService

    engine = create_async_engine(url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine)
    started, closed = asyncio.Event(), asyncio.Event()

    class SlowNotes(KeywordSearch):
        async def search_notes(self, query, user_id, top_k):
            try:
                async with factory() as db:
                    await db.connection()
                    started.set()
                    await db.execute(sa.text('SELECT SLEEP(3)'))
            finally:
                closed.set()
            return await super().search_notes(query, user_id, top_k)

    try:
        search = SlowNotes(factory)
        service = RagService(db_session_factory=factory, keyword_search=search, enable_keyword=True)
        result = await service.query('AX-900', owner, deadline=asyncio.get_running_loop().time() + 1.25)
        assert started.is_set() and 'keyword_notes' in result['failed_sources']
        assert result['status'] == 'partial' and any(row['chunk_id'] == f'{document_id}_0' for row in result['sources'])
        assert result['timings_ms']['total'] < 1250
        await asyncio.wait_for(closed.wait(), 2)
        assert engine.sync_engine.pool.checkedout() == 0
        assert await search.search_documents('AX-900', owner, 3)
        report['retrieval_timeout_probe'] = {'parent_ms': 1250, 'slow_sql_ms': 3000,
            'status': result['status'], 'failed_sources': result['failed_sources'], 'timings_ms': result['timings_ms'],
            'connection_returned': True, 'subsequent_keyword_read': True}
    finally:
        await engine.dispose()


def run_knowledge_ingest_gate(workspace, engine, Database, actor, report, async_url):
    from app.models.knowledge import KnowledgeDocument, knowledge_search_chunks
    from app.models.knowledge_ingest import knowledge_ingest_jobs as jobs
    from app.models.note import Note
    from app.rag.ingest import read_upload_job, submit_deletion, submit_upload
    from app.rag.ingest_worker import KnowledgeIngestWorker, LeaseLost
    from app.rag.keyword_search import KeywordSearch
    from app.rag.reconcile import _read_job_counts, _read_sources

    documents = KnowledgeDocument.__table__
    path = workspace / 'alembic/versions/f64173ab9d20_knowledge_ingest_jobs.py'
    spec = importlib.util.spec_from_file_location('knowledge_ingest_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.context = SimpleNamespace(is_offline_mode=lambda: False)
    evidence_path = workspace / 'alembic/versions/d26b913f7e5c_rag_source_spans_and_note_eligibility.py'
    evidence_spec = importlib.util.spec_from_file_location('rag_source_spans_migration', evidence_path)
    evidence_migration = importlib.util.module_from_spec(evidence_spec)
    evidence_spec.loader.exec_module(evidence_migration)
    evidence_migration.context = migration.context
    keyword_path = workspace / 'alembic/versions/e51a93f8c4b7_knowledge_keyword_chunks.py'
    keyword_spec = importlib.util.spec_from_file_location('knowledge_keyword_migration', keyword_path)
    keyword_migration = importlib.util.module_from_spec(keyword_spec)
    keyword_spec.loader.exec_module(keyword_migration)
    keyword_migration.context = migration.context

    with engine.begin() as bind:
        documents.create(bind)
        bind.exec_driver_sql('ALTER TABLE knowledge_documents DROP COLUMN ready_for_retrieval')
        bind.exec_driver_sql('ALTER TABLE knowledge_documents DROP COLUMN source_manifest')
        bind.exec_driver_sql('ALTER TABLE notes DROP COLUMN is_indexed')
        bind.execute(sa.text('''INSERT INTO knowledge_documents
            (user_id,filename,file_path,file_size,file_type,md5_hash,chunk_count)
            VALUES (:owner,'legacy.txt','legacy.txt',6,'text/plain','legacy',1)'''), {'owner': actor.user_id})
        with Operations.context(MigrationContext.configure(bind)):
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
            evidence_migration.upgrade()
            evidence_migration.upgrade()
            evidence_migration.downgrade()
            evidence_migration.upgrade()
            keyword_migration.upgrade()
            keyword_migration.upgrade()
            keyword_migration.downgrade()
            keyword_migration.upgrade()
        assert bind.scalar(sa.select(documents.c.ready_for_retrieval).where(documents.c.filename == 'legacy.txt')) is True
        assert bind.scalar(sa.select(documents.c.source_manifest).where(documents.c.filename == 'legacy.txt')) is None
        assert not bind.execute(sa.select(Note.id).where(Note.is_indexed.is_(False))).first()

    class IngestDatabase(Database):
        async def scalar(self, statement):
            return self.session.scalar(statement)

    @asynccontextmanager
    async def factory():
        with Session(engine) as session:
            yield IngestDatabase(session)

    class Vectors:
        def __init__(self):
            self.ids = set()

        async def upsert_document(self, texts, metadata, ids, *, collection, write_guard):
            assert collection == 'rag' and all(item['user_id'] == actor.user_id for item in metadata)
            async with write_guard():
                self.ids.update(ids)

        async def delete_document_vectors(self, document_id, *, user_id, write_guard):
            assert user_id == actor.user_id
            async with write_guard():
                self.ids = {identifier for identifier in self.ids if not identifier.startswith(f'{document_id}_')}

    with TemporaryDirectory(prefix='m3-knowledge-fixture-', dir=workspace / '.cache') as directory:
        root = Path(directory)
        barrier = Barrier(2, timeout=5)

        def upload():
            barrier.wait()
            return asyncio.run(submit_upload(factory, actor.user_id, 'isolated.txt', 'txt',
                                             '轴承冷却液型号 AX-900，参数 ref_code_v2'.encode(), None, root=root))

        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [pool.submit(upload) for _ in range(2)]
            first, duplicate = [request.result(timeout=15) for request in requests]
        assert first == duplicate and len(list((root / actor.user_id).iterdir())) == 1

        async def exercise():
            document_id, job_id = first
            vectors = Vectors()
            old_worker = KnowledgeIngestWorker(factory, root=root, vectors=vectors)
            old = await old_worker.claim()
            assert old['id'] == job_id
            with engine.begin() as bind:
                assert bind.scalar(sa.select(documents.c.ready_for_retrieval).where(documents.c.id == document_id)) is False
                bind.execute(jobs.update().where(jobs.c.id == job_id).values(lease_expires_at=datetime(2000, 1, 1)))
            try:
                async with old_worker._write_guard(old):
                    raise AssertionError('Expired ingestion attempt obtained the vector write fence')
            except LeaseLost:
                pass
            try:
                await old_worker._publish(old, {'chunks': [{}]}, ['expired'])
            except LeaseLost:
                pass
            else:
                raise AssertionError('Expired ingestion attempt was able to publish')
            worker = KnowledgeIngestWorker(factory, root=root, vectors=vectors)
            assert await worker.tick() and vectors.ids == {f'{document_id}_0'}
            assert (await read_upload_job(factory, actor.user_id, job_id))['status'] == 'succeeded'
            with engine.connect() as bind:
                manifest = bind.scalar(sa.select(documents.c.source_manifest).where(documents.c.id == document_id))
                assert manifest['chunks'][0]['char_end'] == len('轴承冷却液型号 AX-900，参数 ref_code_v2')
                with Operations.context(MigrationContext.configure(bind)):
                    try:
                        evidence_migration.downgrade()
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError('Downgrade discarded a committed source manifest')

            another, _ = await submit_upload(factory, actor.user_id, 'another.txt', 'txt',
                                              '故障调压方式 ZX-42'.encode(), None, root=root)
            assert await worker.tick()
            search = KeywordSearch(factory)
            [literal] = await search.search_documents('AX-900 如何配置', actor.user_id, 1)
            assert literal['chunk_id'] == f'{document_id}_0' and literal['algorithm'] == 'sql_literal'
            assert literal['keyword_score'] > 0
            fulltext = await search.search_documents('高温 冷却液', actor.user_id, 3)
            assert any(hit['chunk_id'] == f'{document_id}_0' and hit['algorithm'] == 'mysql_fulltext_ngram'
                       and hit['keyword_score'] > 0 for hit in fulltext)
            await _check_retrieval_timeout(async_url, actor.user_id, document_id, report)
            assert '/mysql-ngram-' in fulltext.revision
            assert manifest['keyword_index_version_id'] == fulltext.revision
            with engine.begin() as bind:
                bind.execute(documents.update().where(documents.c.id == document_id).values(
                    source_manifest={**manifest, 'keyword_index_version_id': 'previous-ngram-analyzer'}))
            drift = await search.search_documents('高温 冷却液', actor.user_id, 3)
            assert drift.incomplete and not any(hit['chunk_id'] == f'{document_id}_0' for hit in drift)
            exact = await search.search_documents('AX-900', actor.user_id, 3)
            assert exact.incomplete and any(hit['algorithm'] == 'sql_literal' for hit in exact)
            with engine.begin() as bind:
                bind.execute(documents.update().where(documents.c.id == document_id).values(source_manifest=manifest))
                bind.execute(knowledge_search_chunks.update().where(knowledge_search_chunks.c.document_id == document_id)
                             .values(resource_version_id='obsolete'))
            stale = await search.search_documents('AX-900', actor.user_id, 3)
            assert stale.incomplete and not any(hit['chunk_id'] == f'{document_id}_0' for hit in stale)
            with engine.begin() as bind:
                bind.execute(knowledge_search_chunks.update().where(knowledge_search_chunks.c.document_id == document_id)
                             .values(resource_version_id=manifest['resource_version_id']))
            assert not await search.search_documents('AX-900', str(uuid4()), 3)
            await submit_deletion(factory, actor.user_id, document_id, None)
            with engine.connect() as bind:
                assert bind.scalar(sa.select(documents.c.id).where(documents.c.id == document_id)) is None
                assert bind.scalar(sa.select(sa.func.count()).select_from(knowledge_search_chunks).where(
                    knowledge_search_chunks.c.document_id == document_id)) == 0
            assert not await search.search_documents('AX-900', actor.user_id, 3)
            await submit_deletion(factory, actor.user_id, another, None)
            assert await worker.tick()
            assert await worker.tick() and not vectors.ids and not any((root / actor.user_id).iterdir())

            async with factory() as db, db.begin():
                sources = await _read_sources(db)
                counts = await _read_job_counts(db, actor.user_id)
            assert str(document_id) not in sources['rag']
            assert any(row['ready_for_retrieval'] and row['status'] is None for row in sources['rag'].values())
            assert {'operation': 'delete', 'status': 'succeeded', 'count': 2} in counts['rag']

        asyncio.run(exercise())

    def range_is_locked(statement):
        with engine.connect() as connection:
            connection.exec_driver_sql('SET SESSION innodb_lock_wait_timeout=1')
            try:
                connection.execute(statement)
            except sa.exc.OperationalError as error:
                return error.orig.args[0] == 1205
            return False

    with Session(engine) as session, session.begin():
        # Keep the same locking reads used by cutover open while a concurrent
        # transaction tries to add fresh source identifiers to either range.
        asyncio.run(_read_sources(IngestDatabase(session), lock=True))
        note_insert = Note.__table__.insert().values(id=str(uuid4()), user_id=actor.user_id,
                                                     title='write fence fixture', content='isolated')
        document_insert = documents.insert().values(user_id=actor.user_id, filename='write-fence.txt',
            file_path='write-fence.txt', file_size=1, file_type='text/plain', md5_hash='fixture', chunk_count=1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            writers = [pool.submit(range_is_locked, statement) for statement in (note_insert, document_insert)]
            assert all(writer.result(timeout=10) for writer in writers)

    report['checks']['knowledge_ingest_mysql_legacy_visibility_and_migration_retry'] = True
    report['checks']['knowledge_ingest_mysql_concurrent_same_file_has_one_job'] = True
    report['checks']['knowledge_ingest_mysql_lease_recovery_and_durable_cleanup'] = True
    report['checks']['rag_reconcile_mysql_legacy_sources_and_completed_deletions'] = True
    report['checks']['rag_index_switch_mysql_source_ranges_block_new_writes'] = True
    report['checks']['rag_source_span_mysql_migration_and_atomic_manifest'] = True
    report['checks']['rag_keyword_mysql_ngram_exact_match_and_owner_delete'] = True
    report['checks']['dual_index_mysql_analyzer_and_stale_document_projection'] = True
    report['checks']['retrieval_budget_mysql_async_timeout_keeps_keyword_and_reuses_pool'] = True
    report['checks']['knowledge_ingest_mysql_expired_vector_write_fence'] = True
