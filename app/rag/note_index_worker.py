"""Apply committed note revisions to the embedded vector store."""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
import os
from uuid import uuid4

import sqlalchemy as sa

from app.core.logger_handler import logger
from app.core.model_trace import trace_scope
from app.model_config.snapshots import inference_actor, load_snapshot, use_snapshot
from app.models.note import Note
from app.models.note_index import note_index_jobs as jobs, note_search_chunks
from app.models.user import User
from app.rag.source_spans import hash_text, make_document_manifest, note_chunk_id
from app.rag.text_spliter import TextSplitter


ATTEMPT_TIMEOUT = 120
LEASE = timedelta(seconds=ATTEMPT_TIMEOUT + 30)
MAX_ATTEMPTS = 3
BATCH_SIZE = 32


class LeaseLost(Exception):
    pass


class SourceUnavailable(Exception):
    pass


class NoteIndexWorker:
    def __init__(self, factory, *, vectors=None):
        self.factory = factory
        self.vectors = vectors
        self.task = None

    async def claim(self):
        async with self.factory() as db, db.begin():
            now = await db.scalar(sa.select(sa.func.current_timestamp()))
            row = (await db.execute(sa.select(jobs).where(sa.or_(
                sa.and_(jobs.c.status == 'queued', jobs.c.next_attempt_at <= now),
                sa.and_(jobs.c.status == 'running', jobs.c.lease_expires_at <= now),
            )).order_by(jobs.c.next_attempt_at, jobs.c.note_id).limit(1).with_for_update(skip_locked=True))).mappings().first()
            if row is None:
                return None
            update = jobs.update().where(jobs.c.note_id == row['note_id'])
            if row['attempts'] >= MAX_ATTEMPTS:
                await db.execute(update.values(status='failed', lease_token=None, lease_expires_at=None,
                                               error_code='NOTE_INDEX_ATTEMPTS_EXHAUSTED'))
                return None
            values = dict(status='running', attempts=row['attempts'] + 1, lease_token=str(uuid4()),
                          lease_expires_at=now + LEASE, error_code=None)
            await db.execute(update.values(**values))
            return {**row, **values}

    @staticmethod
    def _attempt(job):
        return (jobs.c.note_id == job['note_id'], jobs.c.revision == job['revision'],
                jobs.c.status == 'running', jobs.c.lease_token == job['lease_token'])

    @staticmethod
    async def _source(db, job, *, lock=False):
        query = sa.select(Note.title, Note.content, Note.format, Note.category, Note.is_indexed, Note.deleted_at).where(
            Note.id == job['note_id'], Note.user_id == job['user_id'],
        )
        row = (await db.execute(query.with_for_update() if lock else query)).one_or_none()
        return row if row is not None and row.is_indexed and row.deleted_at is None else None

    @asynccontextmanager
    async def _publish(self, job, source=None):
        """Fence the short Chroma mutation after embedding has finished."""
        async with self.factory() as db, db.begin():
            # Writers lock the note before its outbox entry.
            current = await self._source(db, job, lock=True)
            attempt = self._attempt(job)
            owned = await db.scalar(sa.select(jobs.c.note_id).where(
                *attempt, jobs.c.lease_expires_at > sa.func.current_timestamp(),
            ).with_for_update())
            if owned is None:
                raise LeaseLost
            if (job['operation'] == 'delete' and current is not None) or (
                job['operation'] == 'upsert' and (current is None or source is not None
                    and current != source)
            ):
                raise SourceUnavailable
            yield db

    async def _replace(self, job, source):
        note_id, owner = job['note_id'], job['user_id']
        def guard():
            return self._publish(job, source)

        manifest, chunks = None, []
        if source is not None:
            text = source.content or source.title
            splitter = TextSplitter()
            chunks = await asyncio.to_thread(splitter.split_with_positions, text, source.format)
            if not chunks and text and text.isspace():
                # Existing notes may have a title and an all-whitespace body.
                chunks = [(text[i:i + splitter.chunk_size], {
                    'section_title': '', 'block_kind': 'text', 'char_start': i,
                    'char_end': min(len(text), i + splitter.chunk_size),
                }) for i in range(0, len(text), splitter.chunk_size)]
            if not chunks:
                raise SourceUnavailable
            manifest = make_document_manifest(source.format, hash_text(text), chunks, parser_version='note-unicode-v1')

        await self.vectors.delete_note_vectors(note_id, user_id=owner, write_guard=guard)
        for offset in range(0, len(chunks), BATCH_SIZE):
            texts, metadata, ids = [], [], []
            for ordinal, (excerpt, span) in enumerate(chunks[offset:offset + BATCH_SIZE], offset):
                texts.append(excerpt)
                ids.append(note_chunk_id(note_id, ordinal))
                metadata.append({**span, 'chunk_index': ordinal, 'user_id': owner, 'note_id': note_id,
                                 'title': source.title, 'category': source.category or '', 'index_revision': job['revision']})
            await self.vectors.upsert_document(texts, metadata, ids, collection='notes', write_guard=guard)

        async with guard() as db:
            if manifest is not None:
                from app.rag.keyword_publication import publication_manifest
                manifest = await publication_manifest(db, manifest)
            await db.execute(note_search_chunks.delete().where(note_search_chunks.c.note_id == note_id))
            for offset in range(0, len(chunks), BATCH_SIZE):
                rows = [dict(note_id=note_id, ordinal=i, content=text, revision=job['revision'],
                             title=source.title if i == 0 else '')
                        for i, (text, _) in enumerate(chunks[offset:offset + BATCH_SIZE], offset)]
                await db.execute(note_search_chunks.insert().values(rows))
            await db.execute(jobs.update().where(*self._attempt(job)).values(source_manifest=manifest,
                status='succeeded', lease_token=None, lease_expires_at=None, error_code=None,
            ))

    async def _fail(self, job, reason, *, retryable):
        async with self.factory() as db, db.begin():
            now = await db.scalar(sa.select(sa.func.current_timestamp()))
            retry = retryable and job['attempts'] < MAX_ATTEMPTS
            await db.execute(jobs.update().where(*self._attempt(job), jobs.c.lease_expires_at > now).values(
                status='queued' if retry else 'failed', lease_token=None, lease_expires_at=None, error_code=reason,
                next_attempt_at=now + timedelta(seconds=2 ** job['attempts']),
            ))

    async def _execute(self, job):
        user_id = job['user_id']
        deleting = job['operation'] == 'delete'
        async with self.factory() as db:
            current = await db.scalar(sa.select(jobs.c.note_id).where(*self._attempt(job)))
            if current is None:
                raise LeaseLost
            source = await self._source(db, job)
            if deleting == (source is not None):
                raise SourceUnavailable
            if not deleting and await db.scalar(sa.select(User.status).where(User.uuid == user_id)) != 'active':
                raise SourceUnavailable
            snapshot = await load_snapshot(db)
            if snapshot is None and os.getenv('DB_CONFIG_REQUIRED', '').lower() in ('true', '1', 'yes'):
                raise RuntimeError('Note index configuration unavailable')

        if self.vectors is None:
            from app.rag.vector_store import VectorStoreService

            self.vectors = VectorStoreService()
        actor = inference_actor.set({'user_id': user_id})
        try:
            with use_snapshot(snapshot), trace_scope(user_id=user_id, request_id=job['revision'].replace('-', ''),
                                                     stage='note_index'):
                await self._replace(job, source)
        finally:
            inference_actor.reset(actor)

    async def tick(self):
        job = await self.claim()
        if job is None:
            return False
        try:
            async with asyncio.timeout(ATTEMPT_TIMEOUT):
                await self._execute(job)
        except LeaseLost:
            pass
        except SourceUnavailable:
            await self._fail(job, 'NOTE_INDEX_SOURCE_UNAVAILABLE', retryable=False)
        except Exception as error:
            logger.warning(f'笔记索引任务执行失败: note_id={job["note_id"]}, error_type={type(error).__name__}')
            await self._fail(job, 'NOTE_INDEX_CLEANUP_FAILED' if job['operation'] == 'delete'
                             else 'NOTE_INDEX_UPSERT_FAILED', retryable=True)
        return True

    async def _run(self):
        while True:
            try:
                if await self.tick():
                    continue
            except Exception as error:
                logger.warning(f'笔记索引 Worker 轮询失败: error_type={type(error).__name__}')
            await asyncio.sleep(0.5)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name='note_index_worker')

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
