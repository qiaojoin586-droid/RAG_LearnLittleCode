"""Verify note index admission and fencing on isolated MySQL."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
import importlib.util
from threading import Barrier
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.orm import Session


def run_note_index_gate(workspace, engine, Database, actor, report):
    from app.models.category import NoteCategory
    from app.models.note import Note
    from app.models.note_index import note_index_jobs as jobs
    from app.models.review import ReviewRecord
    from app.rag.note_index_worker import LeaseLost, NoteIndexWorker
    from app.rag.keyword_search import KeywordSearch
    from app.rag.visibility import CandidateVisibilityService
    from app.schemas.note import NoteCreate, NoteUpdate
    from app.services.note_service import NoteService

    path = workspace / 'alembic/versions/4da62fb3e891_note_index_jobs.py'
    spec = importlib.util.spec_from_file_location('note_index_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.context = SimpleNamespace(is_offline_mode=lambda: False)
    span_spec = importlib.util.spec_from_file_location('note_span_migration',
        workspace / 'alembic/versions/ce28a435b197_structural_note_chunks.py')
    span_migration = importlib.util.module_from_spec(span_spec)
    span_spec.loader.exec_module(span_migration)
    span_migration.context = migration.context
    owner = actor.user_id
    notes = Note.__table__
    with engine.begin() as bind:
        for table in (NoteCategory.__table__, notes, ReviewRecord.__table__):
            table.create(bind, checkfirst=True)
        bind.execute(notes.insert().values(id='legacy-note', user_id=owner, title='legacy', content='legacy'))
        with Operations.context(MigrationContext.configure(bind)):
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
            span_migration.upgrade()
            span_migration.upgrade()
            span_migration.downgrade()
            # Resume MySQL's committed ADD COLUMN after a failed CREATE TABLE.
            bind.exec_driver_sql('ALTER TABLE note_index_jobs ADD COLUMN source_manifest JSON')
            original = bind.exec_driver_sql('SELECT @@session.default_collation_for_utf8mb4').scalar_one()
            bind.exec_driver_sql("SET SESSION default_collation_for_utf8mb4='utf8mb4_0900_ai_ci'")
            try:
                span_migration.upgrade()
            finally:
                bind.execute(sa.text('SET SESSION default_collation_for_utf8mb4 = :original'), {'original': original})
            collations = bind.exec_driver_sql(
                "SELECT COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND "
                "((TABLE_NAME='notes' AND COLUMN_NAME='id') OR (TABLE_NAME='note_search_chunks' AND COLUMN_NAME='note_id'))"
            ).scalars().all()
            assert len(collations) == 2 and collations[0] == collations[1]
            report['checks']['note_chunks_mysql_resume_partial_ddl_inherits_note_id_collation'] = True
            span_migration.upgrade()
        assert bind.scalar(sa.select(notes.c.content).where(notes.c.id == 'legacy-note')) == 'legacy'
        assert bind.scalar(sa.select(sa.func.count()).select_from(jobs)) == 0

    class NoteDatabase(Database):
        async def scalar(self, statement):
            return self.session.scalar(statement)

        def add(self, entity):
            self.session.add(entity)

        async def flush(self):
            self.session.flush()

        async def refresh(self, entity):
            self.session.refresh(entity)

    @asynccontextmanager
    async def factory():
        with Session(engine, expire_on_commit=False) as session:
            yield NoteDatabase(session)

    class Vectors:
        def __init__(self):
            self.rows = []

        async def upsert_document(self, texts, metadatas, ids, *, collection, write_guard):
            assert collection == 'notes' and ids
            async with write_guard():
                self.rows.extend(dict(source='notes', content=text, metadata=meta, chunk_id=identifier)
                                 for text, meta, identifier in zip(texts, metadatas, ids, strict=True))

        async def delete_note_vectors(self, note_id, *, user_id, write_guard):
            assert user_id == owner and all(row['metadata']['note_id'] == note_id for row in self.rows)
            async with write_guard():
                self.rows = []

    service = NoteService()
    visibility = CandidateVisibilityService(factory)
    vectors = Vectors()
    worker = NoteIndexWorker(factory, vectors=vectors)

    async def admit():
        async with factory() as db:
            rolled_back = await service.create_note(db, owner, NoteCreate(title='rollback', content='rollback'), after_commit=[])
            rolled_back_id = rolled_back.id
            await db.rollback()
            assert await db.scalar(sa.select(jobs.c.note_id).where(jobs.c.note_id == rolled_back_id)) is None
            assert await db.scalar(sa.select(notes.c.id).where(notes.c.id == rolled_back_id)) is None
        async with factory() as db, db.begin():
            content = 'original paragraph ' * 160 + '\n\n## Appendix\n尾段精确匹配 XZ-947'
            note = await service.create_note(db, owner, NoteCreate(title='fixture', content=content), after_commit=[])
            return note.id

    note_id = asyncio.run(admit())
    barrier = Barrier(2, timeout=5)

    def claim():
        barrier.wait()
        return asyncio.run(worker.claim())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        claimed = [job for outcome in futures if (job := outcome.result(timeout=15)) is not None]
    assert len(claimed) == 1 and claimed[0]['note_id'] == note_id
    with engine.begin() as bind:
        bind.execute(jobs.update().where(jobs.c.note_id == note_id).values(lease_expires_at=datetime(2000, 1, 1)))

    async def exercise():
        restarted = NoteIndexWorker(factory, vectors=vectors)
        assert await restarted.tick()
        try:
            async with worker._publish(claimed[0]):
                raise AssertionError('Expired index job obtained the write fence')
        except LeaseLost:
            pass
        original = vectors.rows.copy()
        authorized = await visibility.filter(original, owner)
        assert len(authorized) == len(original) > 1
        keyword = KeywordSearch(factory)
        matches = await keyword.search_notes('XZ-947', owner, 1)
        assert not matches.incomplete and '/mysql-ngram-' in matches.revision
        [tail] = matches
        [checked] = await visibility.filter([{**tail, 'source': 'notes'}], owner)
        assert checked['source_locator'] == authorized[-1]['source_locator']
        assert checked['source_locator']['section_title'] == '## Appendix'
        assert any(hit['algorithm'] == 'mysql_fulltext_ngram' and hit['chunk_id'] == tail['chunk_id']
                   for hit in await keyword.search_notes('尾段 检验', owner, 10))
        with engine.begin() as bind, Operations.context(MigrationContext.configure(bind)):
            try:
                span_migration.downgrade()
            except RuntimeError as error:
                assert 'Published note chunks' in str(error)
            else:
                raise AssertionError('Expected populated downgrade to refuse')
        async with factory() as db, db.begin():
            await service.update_note(db, note_id, owner, NoteUpdate(content='next'))
        assert await visibility.filter(original, owner) == []
        superseded = await restarted.claim()
        async with factory() as db, db.begin():
            await service.delete_note(db, note_id, owner)
        try:
            async with restarted._publish(superseded):
                raise AssertionError('Superseded index revision obtained the write fence')
        except LeaseLost:
            pass
        assert await restarted.tick() and not vectors.rows
        assert not await keyword.search_notes('XZ-947', owner, 1)

    asyncio.run(exercise())
    report['checks']['note_index_mysql_additive_and_retryable_migration'] = True
    report['checks']['note_index_mysql_atomic_admission_and_exclusive_claim'] = True
    report['checks']['note_index_mysql_lease_recovery_supersession_and_cleanup'] = True
    report['checks']['note_chunks_mysql_migration_replay_and_populated_downgrade'] = True
    report['checks']['note_chunks_mysql_versioned_tail_ngram_literal_and_delete'] = True
    report['checks']['dual_index_mysql_active_note_coverage_and_analyzer'] = True
