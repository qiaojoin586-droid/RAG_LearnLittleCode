"""Kill an isolated worker after its Chroma mutation and recover its durable upload."""

import asyncio
from contextlib import aclosing, asynccontextmanager, closing
from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi import UploadFile
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeDocument, knowledge_search_chunks
from app.models.knowledge_ingest import knowledge_ingest_jobs as jobs
from app.rag.ingest import read_upload_job, submit_upload
from app.routers import knowledge_router
from test_knowledge_ingest import knowledge, store  # noqa: F401
from test_transactional_tool_operations import ToolSession


WORKER = """
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys

import sqlalchemy as sa
from sqlalchemy.orm import Session
from app.rag.ingest_worker import KnowledgeIngestWorker
from app.rag.vector_store import VectorStoreService

database, uploads, chroma, marker, phase, tests = sys.argv[1:]
sys.path.insert(0, tests)
from test_transactional_tool_operations import ToolSession

engine = sa.create_engine(sa.URL.create('sqlite', database=database))

@asynccontextmanager
async def sessions():
    with Session(engine) as session:
        yield ToolSession(session)

async def embed(texts, **kwargs):
    return [[1.0, 0.0] for _ in texts]

class IsolatedWorker(KnowledgeIngestWorker):
    async def _publish(self, job, manifest, excerpts):
        if phase == 'interrupt':
            assert self.vectors._rag_collection.count() == len(excerpts)
            ready = Path(marker)
            staging = ready.with_suffix('.tmp')
            staging.write_text(job['id'], encoding='utf-8')
            staging.rename(ready)
            await asyncio.Event().wait()
        await super()._publish(job, manifest, excerpts)

async def run():
    vectors = VectorStoreService()
    vectors.config = {'persist_directory': chroma}
    vectors._generate_embeddings = embed
    try:
        assert await IsolatedWorker(sessions, root=uploads, vectors=vectors).tick()
        assert vectors._rag_collection.count() == 1
    finally:
        if vectors._chroma_client:
            vectors._chroma_client.close()
        engine.dispose()

asyncio.run(run())
"""


@pytest.mark.asyncio
async def test_upload_survives_stream_close_worker_process_loss_and_retry(knowledge, monkeypatch):
    store, root, _ = knowledge
    owner = store.actor.user_id
    disk, marker = root / 'jobs.sqlite3', root / 'publication-window.txt'
    with closing(store.engine.raw_connection()) as source, closing(sqlite3.connect(disk)) as target:
        source.driver_connection.backup(target)
    engine = sa.create_engine(sa.URL.create('sqlite', database=str(disk)))

    @asynccontextmanager
    async def factory():
        with Session(engine) as session:
            yield ToolSession(session)

    monkeypatch.setattr(knowledge_router, 'async_session_factory', factory)
    payload = b'persisted across a process restart'
    response = await knowledge_router.upload_document(UploadFile(BytesIO(payload), filename='source.txt'), owner)
    job_id = response.headers['X-Knowledge-Job-Id']
    async with aclosing(response.body_iterator) as stream:
        assert 'queued' in await anext(stream)

    def start(phase, output):
        env = {**os.environ, 'DB_CONFIG_REQUIRED': 'false', 'RAG_CHROMA_HTTP_ROUTES': '{}',
               'TEMP': str(root), 'TMP': str(root), 'SQLITE_TMPDIR': str(root)}
        return subprocess.Popen(
            [sys.executable, '-c', WORKER, str(disk), str(root), str(root / 'chroma'), str(marker),
             phase, str(Path(__file__).resolve().parent)],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )

    try:
        first_log = root / 'first-worker.log'
        with first_log.open('wb') as log, start('interrupt', log) as first:
            try:
                async with asyncio.timeout(30):
                    while not marker.exists():
                        assert first.poll() is None, first_log.read_text(encoding='utf-8', errors='replace')
                        await asyncio.sleep(0.05)
                assert marker.read_text(encoding='utf-8') == job_id
                state = await read_upload_job(factory, owner, job_id)
                assert (state['status'], state['stage'], state['attempts']) == ('running', 'vectorizing', 1)
                assert await submit_upload(factory, owner, 'duplicate.txt', 'txt', payload, None, root=root) == (
                    state['document_id'], job_id,
                )
            finally:
                if first.poll() is None:
                    first.kill()
                await asyncio.to_thread(first.wait, 10)

        with engine.begin() as db:
            row = db.execute(sa.select(KnowledgeDocument.__table__)).mappings().one()
            assert not row['ready_for_retrieval']
            assert db.execute(knowledge_search_chunks.select()).first() is None
            db.execute(jobs.update().where(jobs.c.id == job_id).values(lease_expires_at=datetime(2000, 1, 1)))

        recovered_log = root / 'recovered-worker.log'
        with recovered_log.open('wb') as log, start('recover', log) as recovered:
            try:
                assert await asyncio.to_thread(recovered.wait, 30) == 0, recovered_log.read_text(
                    encoding='utf-8', errors='replace',
                )
            finally:
                if recovered.poll() is None:
                    recovered.kill()
                    await asyncio.to_thread(recovered.wait, 10)
        final = await read_upload_job(factory, owner, job_id)
        assert (final['status'], final['stage'], final['attempts'], final['progress']) == ('succeeded', 'complete', 2, 100)
        with engine.connect() as db:
            assert db.execute(sa.select(jobs.c.id)).scalar_one() == job_id
            published = db.execute(sa.select(KnowledgeDocument.__table__)).mappings().one()
            assert published['ready_for_retrieval'] and published['chunk_count'] == 1
            assert db.execute(sa.select(knowledge_search_chunks.c.content)).scalar_one() == payload.decode()
    finally:
        engine.dispose()
