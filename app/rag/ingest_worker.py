"""Consume durable knowledge jobs in the process that owns the vector store."""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

from app.core.logger_handler import logger
from app.core.model_trace import trace_scope
from app.model_config.snapshots import inference_actor, load_snapshot, use_snapshot
from app.models.knowledge_ingest import knowledge_ingest_jobs as jobs
from app.models.user import User
from app.rag.ingest import DOCUMENTS, UPLOAD_ROOT, lock_owner, scoped_upload_path
from app.rag.keyword_index import replace_chunks
from app.rag.source_spans import make_document_manifest


LEASE = timedelta(seconds=45)
MAX_ATTEMPTS = 3
BATCH_SIZE = 32


class IngestFailure(Exception):
    """A source error that repeating the same job cannot repair."""


class LeaseLost(Exception):
    """A stale attempt without permission to publish."""


def _read_pages(path, digest, *, legacy_md5=None):
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        raise IngestFailure('SOURCE_MISSING') from None
    if (hashlib.sha256(content).hexdigest() != digest if digest is not None
            else hashlib.md5(content, usedforsecurity=False).hexdigest() != legacy_md5):
        raise IngestFailure('SOURCE_CHANGED')
    extension = path.suffix.lower()[1:]
    if extension != 'pdf':
        encoding = 'utf-8' if digest is None else 'utf-8-sig'
        return extension, [(None, content.decode(encoding, errors='replace'))]
    try:
        import fitz

        with fitz.open(stream=content, filetype='pdf') as pdf:
            if not pdf.is_pdf or pdf.needs_pass:
                raise IngestFailure('PDF_PARSE_FAILED')
            pages = [(number, page.get_text()) for number, page in enumerate(pdf, start=1)]
    except ImportError:
        raise IngestFailure('PDF_PARSER_UNAVAILABLE') from None
    except (RuntimeError, ValueError, OSError):
        raise IngestFailure('PDF_PARSE_FAILED') from None
    if digest is None:
        # Pre-job uploads concatenated PDF pages before applying the chunker.
        return extension, [(None, '\n\n'.join(text for _, text in pages if text))]
    return extension, pages


def _split_pages(extension, pages, *, legacy=False):
    from app.rag.text_spliter import TextSplitter

    splitter = TextSplitter()
    split = splitter.split_legacy_positions if legacy else splitter.split_with_positions
    chunks = []
    for page, text in pages:
        if not text.strip():
            continue
        if extension == 'pdf' and not any(c.isprintable() and not c.isspace() and c != '\ufffd' for c in text):
            continue
        for content, span in split(text, extension):
            if page is not None:
                span['page_number'] = page
            chunks.append((content, span))
    if not chunks:
        raise IngestFailure('PDF_TEXT_REQUIRED' if extension == 'pdf' else 'DOCUMENT_EMPTY')
    return chunks


class KnowledgeIngestWorker:
    def __init__(self, factory, *, root=UPLOAD_ROOT, vectors=None):
        self.factory = factory
        self.root = Path(root)
        self.vectors = vectors
        self.task = None

    @staticmethod
    def _lease_filter(job):
        return (jobs.c.id == job['id'], jobs.c.status == 'running', jobs.c.lease_token == job['lease_token'],
                jobs.c.lease_expires_at > sa.func.current_timestamp())

    async def claim(self):
        async with self.factory() as db:
            async with db.begin():
                now = await db.scalar(sa.select(sa.func.current_timestamp()))
                row = (await db.execute(sa.select(jobs).where(sa.or_(
                    sa.and_(jobs.c.status == 'queued', jobs.c.next_attempt_at <= now),
                    sa.and_(jobs.c.status == 'running', jobs.c.lease_expires_at <= now),
                )).order_by(jobs.c.next_attempt_at, jobs.c.id).limit(1).with_for_update(skip_locked=True))).mappings().first()
                if row is None:
                    return None
                update = jobs.update().where(jobs.c.id == row['id'])
                if row['attempts'] >= MAX_ATTEMPTS:
                    await db.execute(update.values(status='failed', lease_token=None, lease_expires_at=None,
                                                   error_code='INGEST_ATTEMPTS_EXHAUSTED'))
                    return None
                values = {'status': 'running', 'stage': 'queued', 'attempts': row['attempts'] + 1, 'lease_token': str(uuid4()),
                          'lease_expires_at': now + LEASE, 'error_code': None}
                await db.execute(update.values(**values))
                return {**row, **values}

    @asynccontextmanager
    async def _write_guard(self, job):
        async with self.factory() as db, db.begin():
            if await db.scalar(sa.select(jobs.c.id).where(*self._lease_filter(job)).with_for_update()) is None:
                raise LeaseLost
            yield db

    async def _update(self, job, **values):
        async with self._write_guard(job) as db:
            await db.execute(jobs.update().where(*self._lease_filter(job)).values(**values))

    async def _heartbeat(self, job):
        while True:
            await asyncio.sleep(10)
            try:
                async with self.factory() as db:
                    async with db.begin():
                        now = await db.scalar(sa.select(sa.func.current_timestamp()))
                        result = await db.execute(jobs.update().where(*self._lease_filter(job)).values(lease_expires_at=now + LEASE))
                        if result.rowcount != 1:
                            return
            except sa.exc.SQLAlchemyError as error:
                logger.warning(f'知识库任务续租失败: error_type={type(error).__name__}')

    async def _publish(self, job, manifest, excerpts):
        async with self.factory() as db:
            async with db.begin():
                await lock_owner(db, job['user_id'])
                # Lock the job before the document, as deletion does.
                changed = await db.execute(jobs.update().where(*self._lease_filter(job)).values(
                    status='succeeded', stage='complete', lease_token=None, lease_expires_at=None, error_code=None,
                ))
                if changed.rowcount != 1:
                    raise LeaseLost
                from app.rag.keyword_publication import publication_manifest
                manifest = await publication_manifest(db, manifest)
                document = await db.execute(DOCUMENTS.update().where(
                    DOCUMENTS.c.id == job['document_id'], DOCUMENTS.c.user_id == job['user_id'],
                    DOCUMENTS.c.ready_for_retrieval.is_(False),
                ).values(ready_for_retrieval=True, chunk_count=len(manifest['chunks']), source_manifest=manifest))
                if document.rowcount != 1:
                    raise LeaseLost
                await replace_chunks(db, job['document_id'], manifest['resource_version_id'], excerpts)

    async def _fail(self, job, reason, *, retryable):
        async with self.factory() as db:
            async with db.begin():
                now = await db.scalar(sa.select(sa.func.current_timestamp()))
                retry = retryable and job['attempts'] < MAX_ATTEMPTS
                await db.execute(jobs.update().where(*self._lease_filter(job)).values(
                    status='queued' if retry else 'failed', error_code=reason, lease_token=None, lease_expires_at=None,
                    stage='queued' if retry else jobs.c.stage,
                    next_attempt_at=now + timedelta(seconds=2 ** job['attempts']),
                ))

    async def _execute(self, job):
        async with self.factory() as db:
            status = await db.scalar(sa.select(User.status).where(User.uuid == job['user_id']))
            if status != 'active':
                raise IngestFailure('INGEST_OWNER_INACTIVE')
            if job['operation'] == 'ingest' and await db.scalar(sa.select(DOCUMENTS.c.id).where(
                DOCUMENTS.c.id == job['document_id'], DOCUMENTS.c.user_id == job['user_id'],
                DOCUMENTS.c.ready_for_retrieval.is_(False),
            )) is None:
                raise IngestFailure('INGEST_DOCUMENT_UNAVAILABLE')
            # Resolve the write target once per attempt, after the durable claim.
            # A request admitted after cutover may still hold its earlier read snapshot.
            snapshot = await load_snapshot(db)
            if snapshot is None and os.getenv('DB_CONFIG_REQUIRED', '').lower() in ('true', '1', 'yes'):
                raise RuntimeError('Knowledge index configuration unavailable')

        actor = inference_actor.set({'user_id': job['user_id']})
        try:
            with use_snapshot(snapshot), trace_scope(user_id=job['user_id'], request_id=job['id'].replace('-', ''),
                                                     stage='knowledge_ingest'):
                if self.vectors is None:
                    from app.rag.vector_store import VectorStoreService

                    self.vectors = VectorStoreService()
                try:
                    path = scoped_upload_path(self.root, job['user_id'], job['file_path'])
                except ValueError:
                    raise IngestFailure('SOURCE_PATH_INVALID') from None
                if job['operation'] == 'delete':
                    await self.vectors.delete_document_vectors(str(job['document_id']), user_id=job['user_id'],
                                                               write_guard=lambda: self._write_guard(job))
                    await asyncio.to_thread(path.unlink, missing_ok=True)
                    await self._update(job, status='succeeded', stage='complete', file_path=None,
                                       lease_token=None, lease_expires_at=None, error_code=None)
                    return
                await self._ingest(job, path)
        finally:
            inference_actor.reset(actor)

    async def _ingest(self, job, path):
        await self._update(job, stage='parsing')
        extension, pages = await asyncio.to_thread(_read_pages, path, job['source_sha256'])
        await self._update(job, stage='splitting')
        chunks = await asyncio.to_thread(_split_pages, extension, pages)
        manifest = await asyncio.to_thread(make_document_manifest, extension, job['source_sha256'], chunks)
        await self._update(job, stage='vectorizing')
        def guard():
            return self._write_guard(job)

        await self.vectors.delete_document_vectors(str(job['document_id']), user_id=job['user_id'], write_guard=guard)
        for offset in range(0, len(chunks), BATCH_SIZE):
            await self._update(job, stage='vectorizing')
            batch = chunks[offset:offset + BATCH_SIZE]
            metadata = [{**item, 'user_id': job['user_id'], 'document_id': str(job['document_id']),
                         'filename': job['filename'], 'chunk_index': offset + index, 'source_sha256': job['source_sha256']}
                        for index, (_, item) in enumerate(batch)]
            ids = [f"{job['document_id']}_{item['chunk_index']}" for item in metadata]
            await self.vectors.upsert_document([text for text, _ in batch], metadata, ids, collection='rag', write_guard=guard)
        await self._publish(job, manifest, [text for text, _ in chunks])

    async def tick(self):
        job = await self.claim()
        if job is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            async with asyncio.timeout(300):
                await self._execute(job)
        except LeaseLost:
            pass
        except IngestFailure as error:
            await self._fail(job, str(error), retryable=False)
        except Exception as error:
            logger.warning(f'知识库任务执行失败: job_id={job["id"]}, error_type={type(error).__name__}')
            await self._fail(job, 'INDEXING_FAILED' if job['operation'] == 'ingest' else 'CLEANUP_FAILED', retryable=True)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def _run(self):
        while True:
            try:
                if await self.tick():
                    continue
            except Exception as error:
                logger.warning(f'知识库 Worker 轮询失败: error_type={type(error).__name__}')
            await asyncio.sleep(0.5)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name='knowledge_ingest_worker')

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
