"""Owned loopback services and synthetic SQL/Chroma data for the four-arm evaluation."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
import os
from pathlib import Path
import socket
import subprocess
import time
from uuid import uuid4

import httpx
import pymysql
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, KnowledgeDocument, Note, NoteCategory, User
from app.models.knowledge import knowledge_search_chunks
from app.models.note_index import note_index_jobs, note_search_chunks
from app.rag.keyword_publication import publication_manifest
from app.rag.source_spans import hash_text, make_document_manifest
from app.rag.vector_store import VectorStoreService


OWNER = 'local-eval'
FOREIGN = 'other-eval'
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@contextmanager
def owned_process(command, log, env=None):
    with log.open('wb') as output:
        proc = subprocess.Popen(command, env=env, stdout=output, stderr=subprocess.STDOUT, creationflags=FLAGS)
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


@contextmanager
def local_mysql(binary, directory):
    port, password = free_port(), uuid4().hex
    base = [str(binary), '--no-defaults', '--console', f'--basedir={binary.parent.parent}',
            f'--datadir={directory / "mysql"}', f'--tmpdir={directory}']
    init = subprocess.run([*base, '--initialize-insecure'], capture_output=True, timeout=90, creationflags=FLAGS)
    if init.returncode:
        raise RuntimeError('MySQL initialization failed: ' + init.stderr.decode(errors='replace')[-1500:])
    setup = directory / 'bootstrap.sql'
    setup.write_text("CREATE DATABASE ablation CHARACTER SET utf8mb4;\n"
        f"CREATE USER 'evaluator'@'127.0.0.1' IDENTIFIED BY '{password}';\n"
        "GRANT ALL ON ablation.* TO 'evaluator'@'127.0.0.1';\n"
        "GRANT SHUTDOWN ON *.* TO 'evaluator'@'127.0.0.1';\n", encoding='ascii')
    with owned_process([*base, '--bind-address=127.0.0.1', '--skip-name-resolve', '--mysqlx=OFF',
                        f'--port={port}', f'--init-file={setup}', '--innodb-buffer-pool-size=64M'],
                       directory / 'mysql.log') as proc:
        end = time.monotonic() + 60
        while time.monotonic() < end and proc.poll() is None:
            try:
                with pymysql.connect(host='127.0.0.1', port=port, user='evaluator', password=password,
                                     database='ablation', connect_timeout=2):
                    break
            except pymysql.OperationalError:
                time.sleep(.25)
        else:
            raise RuntimeError('Disposable MySQL failed to start')
        setup.unlink()
        try:
            yield sa.URL.create('mysql+aiomysql', username='evaluator', password=password, host='127.0.0.1',
                                port=port, database='ablation', query={'charset':'utf8mb4'})
        finally:
            with pymysql.connect(host='127.0.0.1', port=port, user='evaluator', password=password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute('SHUTDOWN')
            proc.wait(timeout=30)


@asynccontextmanager
async def local_ollama(executable, models, directory):
    port = free_port()
    env = {**os.environ, 'OLLAMA_HOST': f'127.0.0.1:{port}', 'OLLAMA_MODELS': str(models),
           'OLLAMA_NO_CLOUD': '1', 'OLLAMA_NUM_PARALLEL': '4', 'OLLAMA_MAX_LOADED_MODELS': '2',
           'OLLAMA_CONTEXT_LENGTH': '2048', 'OLLAMA_KEEP_ALIVE': '30m'}
    with owned_process([executable, 'serve'], directory / 'ollama.log', env) as proc:
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=90) as http:
            for _ in range(120):
                if proc.poll() is not None:
                    raise RuntimeError('Owned Ollama exited before readiness')
                try:
                    response = await http.get('/api/tags', timeout=2)
                    response.raise_for_status()
                    break
                except httpx.TransportError:
                    await asyncio.sleep(.5)
            else:
                raise RuntimeError('Owned Ollama did not become ready')
            yield http, {m['name']:m['digest'] for m in response.json()['models']}


@asynccontextmanager
async def seeded_indexes(url, client, corpus, embed):
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine)
    tables = [User.__table__, NoteCategory.__table__, KnowledgeDocument.__table__, Note.__table__,
              note_index_jobs, knowledge_search_chunks, note_search_chunks]
    vectors = object.__new__(VectorStoreService)
    options = {'embedding_function': None, 'metadata': {'hnsw:space': 'cosine'}}
    vectors._rag_collection = client.create_collection('eval_documents', **options)
    vectors._notes_collection = client.create_collection('eval_notes', **options)
    vectors._generate_embeddings = embed
    try:
        async with engine.begin() as db:
            await db.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))
        async with factory.begin() as db:
            await db.execute(User.__table__.insert(), [dict(uuid=u, username=u, password='unused') for u in (OWNER, FOREIGN)])
            for source, collection in [('knowledge', vectors._rag_collection), ('notes', vectors._notes_collection)]:
                rows = [(k, r) for k, r in corpus.items() if r['source'] == source]
                for offset in range(0, len(rows), 12):
                    batch = rows[offset:offset+12]
                    texts = [row['text'] for _, row in batch]
                    embeddings = await embed(texts)
                    metadata = []
                    for _, row in batch:
                        key, text = row['id'], row['text']
                        owner = FOREIGN if row['state'] == 'foreign' else OWNER
                        if source == 'knowledge':
                            version = hash_text(text)
                            span = {'char_start':0,'char_end':len(text),'page_number':None,'section_title':''}
                            manifest = await publication_manifest(db, make_document_manifest('txt', version, [(text, span)]))
                            await db.execute(KnowledgeDocument.__table__.insert().values(id=int(key), user_id=owner,
                                filename=key+'.txt', file_path=key+'.txt', file_type='txt', file_size=len(text.encode()),
                                md5_hash='fixture', chunk_count=1, ready_for_retrieval=True, source_manifest=manifest))
                            await db.execute(knowledge_search_chunks.insert().values(document_id=int(key), ordinal=0,
                                content=text, resource_version_id=version))
                            metadata.append({'document_id':key,'chunk_index':0,'user_id':owner,'source_sha256':version})
                        else:
                            await db.execute(Note.__table__.insert().values(id=key, user_id=owner, title=key, content=text,
                                is_indexed=row['state'] != 'revoked', deleted_at=datetime(2025,1,1) if row['state']=='deleted' else None))
                            metadata.append({'note_id':key,'user_id':owner})
                    collection.add(ids=[k for k,_ in batch], documents=texts, embeddings=embeddings, metadatas=metadata)
            mysql = (await db.execute(sa.text('SELECT VERSION(), @@ngram_token_size'))).one()
        yield factory, vectors, {'mysql': mysql[0], 'ngram_size': mysql[1]}
    finally:
        await engine.dispose()
