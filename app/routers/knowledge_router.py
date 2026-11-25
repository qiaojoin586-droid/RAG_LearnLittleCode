"""Knowledge upload, durable progress, and owner-scoped document access."""

import asyncio
import json
import os
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.failed_response import BusinessError, ErrorCode
from app.core.rate_limit import rate_limit
from app.core.success_response import success_response
from app.db.database import async_session_factory, get_db
from app.model_config.snapshots import current_snapshot
from app.models.knowledge import KnowledgeDocument
from app.rag.ingest import UPLOAD_ROOT, list_upload_jobs, read_upload_job, submit_deletion, submit_upload
from app.rag.source_access import read_document_source
from app.schemas.knowledge import KnowledgeDocumentListResponse, KnowledgeDocumentResponse
from app.utils.auth_utils import get_current_user_id
from app.utils.file_handler import get_safe_filename, read_upload_limited, validate_upload_file


router = APIRouter()


def _sse(data):
    return f'data: {json.dumps(data, ensure_ascii=False)}\n\n'


async def _upload_events(user_id, filename, document_id, job_id):
    if job_id is None:
        yield _sse({'event_type': 'completed', 'filename': filename, 'progress': 100,
                    'document_id': document_id, 'message': '文档已存在，已跳过上传'})
        yield _sse({'event_type': 'finish'})
        return

    previous = None
    next_keepalive = asyncio.get_running_loop().time() + 15
    while True:
        try:
            state = await read_upload_job(async_session_factory, user_id, job_id)
        except BusinessError:
            yield _sse({'event_type': 'error', 'job_id': job_id, 'message': '上传任务已不可用'})
            return
        if state['status'] == 'succeeded':
            yield _sse({'event_type': 'completed', **state, 'message': '文档处理完成'})
            yield _sse({'event_type': 'finish'})
            return
        if state['status'] in ('failed', 'cancelled'):
            yield _sse({'event_type': 'error', **state})
            return
        if state != previous:
            yield _sse({'event_type': 'processing', **state})
            previous = state
        elif asyncio.get_running_loop().time() >= next_keepalive:
            yield ': waiting\n\n'
            next_keepalive = asyncio.get_running_loop().time() + 15
        await asyncio.sleep(1)


@router.post('/knowledge/upload', summary='上传知识库文档', dependencies=[Depends(rate_limit(endpoint_limit=20))])
async def upload_document(file: UploadFile = File(...), user_id: str = Depends(get_current_user_id)):
    max_size = int(os.getenv('MAX_UPLOAD_SIZE_MB', '50'))
    content = await read_upload_limited(file, max_size)
    extension = validate_upload_file(file.filename or '', len(content), max_size)[1:]
    filename = get_safe_filename(file.filename)
    snapshot = current_snapshot()
    document_id, job_id = await submit_upload(async_session_factory, user_id, filename, extension, content,
                                              snapshot.release_id if snapshot is not None else None)
    headers = {'Cache-Control': 'private, no-store', 'X-Accel-Buffering': 'no'}
    if job_id is not None:
        headers['X-Knowledge-Job-Id'] = job_id
    return StreamingResponse(_upload_events(user_id, filename, document_id, job_id),
                             media_type='text/event-stream', headers=headers)


@router.get('/knowledge/upload-jobs', summary='恢复知识库上传进度')
async def get_upload_jobs(
    response: Response, after: UUID | None = None, limit: int = Query(20, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
):
    response.headers['Cache-Control'] = 'private, no-store'
    return success_response(data=await list_upload_jobs(
        async_session_factory, user_id, after=str(after) if after is not None else None, limit=limit,
    ))


@router.get('/knowledge/upload-jobs/{job_id}', summary='查询知识库上传任务')
async def get_upload_job(job_id: UUID, response: Response, user_id: str = Depends(get_current_user_id)):
    response.headers['Cache-Control'] = 'private, no-store'
    return success_response(data=await read_upload_job(async_session_factory, user_id, str(job_id)))


@router.get('/knowledge/documents', summary='文档列表')
async def list_documents(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(KnowledgeDocument).where(
        KnowledgeDocument.user_id == user_id, KnowledgeDocument.ready_for_retrieval.is_(True),
    ).order_by(KnowledgeDocument.created_at.desc()))
    documents = result.scalars().all()
    return success_response(data=KnowledgeDocumentListResponse(
        documents=[KnowledgeDocumentResponse.model_validate(document) for document in documents],
        total=len(documents),
    ).model_dump())


@router.get('/knowledge/documents/{doc_id}', summary='文档详情')
async def get_document(doc_id: int, user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    document = await db.scalar(select(KnowledgeDocument).where(
        KnowledgeDocument.id == doc_id, KnowledgeDocument.user_id == user_id, KnowledgeDocument.ready_for_retrieval.is_(True),
    ))
    if document is None:
        raise BusinessError(ErrorCode.DOCUMENT_NOT_FOUND, http_status=404)
    return success_response(data=KnowledgeDocumentResponse.model_validate(document).model_dump())


@router.get('/knowledge/documents/{doc_id}/chunks/{ordinal}', summary='按原文版本读取知识库切片')
async def get_document_chunk(
    doc_id: int, ordinal: int, response: Response,
    version: str = Query(pattern=r'^[0-9a-f]{64}$'),
    user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db),
):
    response.headers['Cache-Control'] = 'private, no-store'
    source = await read_document_source(db, user_id, doc_id, ordinal, version, UPLOAD_ROOT)
    return success_response(data={key: source[key] for key in ('source_locator', 'content')})


@router.delete('/knowledge/documents/{doc_id}', summary='删除文档')
async def delete_document(doc_id: int, user_id: str = Depends(get_current_user_id)):
    snapshot = current_snapshot()
    await submit_deletion(async_session_factory, user_id, doc_id, snapshot.release_id if snapshot is not None else None)
    return success_response(message='文档已删除')
