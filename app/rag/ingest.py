"""Commit knowledge upload and deletion intent before any index mutations."""

import asyncio
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

from app.core.failed_response import BusinessError, ErrorCode
from app.models.knowledge import KnowledgeDocument
from app.models.knowledge_ingest import knowledge_ingest_jobs as jobs
from app.models.user import User
from app.utils.file_handler import calculate_md5_bytes


UPLOAD_ROOT = Path('data/uploads')
DOCUMENTS = KnowledgeDocument.__table__
FILE_TYPES = {'txt': 'text/plain', 'md': 'text/markdown', 'pdf': 'application/pdf'}
STAGE_PROGRESS = {'queued': 10, 'parsing': 30, 'splitting': 60, 'vectorizing': 85, 'complete': 100}
UPLOAD_ERRORS = {
    'PDF_TEXT_REQUIRED': 'PDF 没有可提取的文本，请先进行 OCR 后重新上传。',
    'PDF_PARSE_FAILED': 'PDF 无法解析，请检查文件是否完整。',
    'DOCUMENT_EMPTY': '文档没有可入库的文本。',
}


def scoped_upload_path(root, user_id, filename):
    directory = (root / user_id).resolve()
    path = Path(filename).resolve()
    if not directory.is_relative_to(root.resolve()) or not path.is_relative_to(directory) or path == directory:
        raise ValueError('Knowledge source path is outside its owner directory')
    return path


def _store_source(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


async def lock_owner(db, user_id):
    status = await db.scalar(sa.select(User.status).where(User.uuid == user_id).with_for_update())
    if status != 'active':
        raise BusinessError(ErrorCode.NO_NOTE_ACCESS, http_status=403)


def _queued(operation, now):
    return {'operation': operation, 'status': 'queued', 'stage': 'queued', 'attempts': 0,
            'lease_token': None, 'lease_expires_at': None, 'error_code': None, 'next_attempt_at': now}


async def submit_upload(factory, user_id, filename, extension, content, release_id, *, root=UPLOAD_ROOT):
    digest = hashlib.sha256(content).hexdigest()
    md5 = calculate_md5_bytes(content)
    async with factory() as db:
        async with db.begin():
            await lock_owner(db, user_id)
            matches = (await db.execute(sa.select(DOCUMENTS.c.id, jobs.c.id.label('job_id'), jobs.c.source_sha256,
                jobs.c.status, jobs.c.error_code)
                .outerjoin(jobs, jobs.c.document_id == DOCUMENTS.c.id)
                .where(DOCUMENTS.c.user_id == user_id, DOCUMENTS.c.md5_hash == md5,
                       DOCUMENTS.c.file_size == len(content)).order_by(DOCUMENTS.c.id))).mappings().all()
            for existing in matches:
                if existing['job_id'] is None or existing['source_sha256'] == digest:
                    if existing['status'] == 'failed' and existing['error_code'] in ('INDEXING_FAILED', 'INGEST_ATTEMPTS_EXHAUSTED'):
                        now = await db.scalar(sa.select(sa.func.current_timestamp()))
                        await db.execute(jobs.update().where(jobs.c.id == existing['job_id']).values(**_queued('ingest', now)))
                    return existing['id'], existing['job_id']

            job_id = str(uuid4())
            path = scoped_upload_path(root, user_id, root / user_id / f'{job_id}.{extension}')
            await asyncio.to_thread(_store_source, path, content)
            result = await db.execute(DOCUMENTS.insert().values(
                user_id=user_id, filename=filename, file_path=str(path), file_size=len(content),
                file_type=FILE_TYPES[extension], md5_hash=md5, chunk_count=0, ready_for_retrieval=False,
            ))
            document_id = result.inserted_primary_key[0]
            now = await db.scalar(sa.select(sa.func.current_timestamp()))
            await db.execute(jobs.insert().values(id=job_id, user_id=user_id, document_id=document_id,
                release_id=release_id, filename=filename, file_path=str(path), source_sha256=digest, **_queued('ingest', now)))
    return document_id, job_id


async def submit_deletion(factory, user_id, document_id, release_id):
    async with factory() as db:
        async with db.begin():
            await lock_owner(db, user_id)
            # Match the Worker lock order when invalidating a leased upload.
            job = (await db.execute(sa.select(jobs).where(jobs.c.document_id == document_id,
                jobs.c.user_id == user_id).with_for_update())).mappings().one_or_none()
            document = (await db.execute(sa.select(DOCUMENTS).where(DOCUMENTS.c.id == document_id,
                DOCUMENTS.c.user_id == user_id).with_for_update())).mappings().one_or_none()
            if document is None:
                if job and job['operation'] == 'delete':
                    if job['status'] == 'failed' and job['error_code'] in ('CLEANUP_FAILED', 'INGEST_ATTEMPTS_EXHAUSTED'):
                        now = await db.scalar(sa.select(sa.func.current_timestamp()))
                        await db.execute(jobs.update().where(jobs.c.id == job['id']).values(**_queued('delete', now)))
                    return
                raise BusinessError(ErrorCode.DOCUMENT_NOT_FOUND, http_status=404)
            now = await db.scalar(sa.select(sa.func.current_timestamp()))
            if job is None:
                await db.execute(jobs.insert().values(id=str(uuid4()), user_id=user_id, document_id=document_id,
                    release_id=release_id, filename=document['filename'], file_path=document['file_path'], **_queued('delete', now)))
            else:
                await db.execute(jobs.update().where(jobs.c.id == job['id']).values(**_queued('delete', now)))
            await db.execute(DOCUMENTS.delete().where(DOCUMENTS.c.id == document_id, DOCUMENTS.c.user_id == user_id))


async def queue_source_manifest(factory, user_id, document_id, *, root=UPLOAD_ROOT):
    """Reindex a legacy upload from its verified original file using the durable worker."""
    async with factory() as db, db.begin():
        await lock_owner(db, user_id)
        owner_document = (DOCUMENTS.c.id == document_id, DOCUMENTS.c.user_id == user_id)
        job = (await db.execute(sa.select(jobs).where(jobs.c.document_id == document_id,
            jobs.c.user_id == user_id).with_for_update())).mappings().one_or_none()
        document = (await db.execute(sa.select(DOCUMENTS).where(*owner_document).with_for_update())).mappings().one_or_none()
        if document is None:
            raise BusinessError(ErrorCode.DOCUMENT_NOT_FOUND, http_status=404)
        if document['source_manifest'] is not None or job and (
            job['operation'] != 'ingest' or job['status'] in ('queued', 'running')
        ):
            return None
        path = scoped_upload_path(root, user_id, document['file_path'])
        content = await asyncio.to_thread(path.read_bytes)
        digest = hashlib.sha256(content).hexdigest()
        if (len(content) != document['file_size'] or calculate_md5_bytes(content) != document['md5_hash']
                or job and job['source_sha256'] != digest):
            raise ValueError('Legacy upload no longer matches its recorded source')
        now = await db.scalar(sa.select(sa.func.current_timestamp()))
        values = {'filename': document['filename'], 'file_path': str(path), 'source_sha256': digest,
                  **_queued('ingest', now)}
        job_id = job['id'] if job else str(uuid4())
        if job:
            await db.execute(jobs.update().where(jobs.c.id == job_id).values(**values))
        else:
            await db.execute(jobs.insert().values(id=job_id, user_id=user_id, document_id=document_id, **values))
        await db.execute(DOCUMENTS.update().where(*owner_document).values(ready_for_retrieval=False))
        return job_id


def _upload_view(row):
    status = 'cancelled' if row['operation'] == 'delete' else row['status']
    message = None
    if status == 'cancelled':
        message = '文档已删除'
    elif status == 'failed':
        message = UPLOAD_ERRORS.get(row['error_code'], '文档入库失败，可通过上传任务查看状态。')
    return {'job_id': row['id'], 'document_id': row['document_id'], 'filename': row['filename'],
            'status': status, 'stage': row['stage'], 'progress': STAGE_PROGRESS[row['stage']],
            'error_code': row['error_code'], 'attempts': row['attempts'], 'message': message}


async def read_upload_job(factory, user_id, job_id):
    async with factory() as db:
        row = (await db.execute(sa.select(jobs).where(jobs.c.id == job_id, jobs.c.user_id == user_id))).mappings().one_or_none()
    if row is None:
        raise BusinessError(ErrorCode.DOCUMENT_NOT_FOUND, http_status=404)
    return _upload_view(row)


async def list_upload_jobs(factory, user_id, *, after=None, limit=20):
    """List outstanding uploads and the latest failures for the current account."""
    base = sa.select(jobs).where(jobs.c.user_id == user_id, jobs.c.operation == 'ingest')
    active = base.where(jobs.c.status.in_(('queued', 'running')))
    if after is not None:
        active = active.where(jobs.c.id > after)
    async with factory() as db:
        rows = (await db.execute(active.order_by(jobs.c.id).limit(limit + 1))).mappings().all()
        failed = []
        if after is None:
            failed = (await db.execute(base.where(jobs.c.status == 'failed')
                .order_by(jobs.c.next_attempt_at.desc(), jobs.c.id.desc()).limit(20))).mappings().all()
    return {'jobs': [_upload_view(row) for row in [*rows[:limit], *failed]],
            'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None}
