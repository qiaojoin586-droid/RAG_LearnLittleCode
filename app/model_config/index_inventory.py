"""Read Chroma's SQLite catalog from a private copy, without starting Chroma.

Only collection identity, observed dimension and metadata row counts leave the
reader. The source is never opened by SQLite or an SDK, including its WAL/SHM.
This observes an existing catalog; it cannot attest model weights or HNSW health.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from app.model_config.contracts import content_hash
from app.model_config.errors import fail

INVENTORY_VERSION = 1


def storage_identity(directory,tenant,database):
    return content_hash({'directory':os.path.normcase(str(directory)),'tenant':tenant,'database':database})


def _digest(path):
    if not path.exists():return None
    before=path.stat()
    if not path.is_file():fail(42220,'INDEX_STORE_INVALID',422)
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1048576),b''):digest.update(block)
    after=path.stat()
    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
        fail(40920,'INDEX_STORE_CHANGED',409)
    return digest.hexdigest()


@contextmanager
def open_catalog_snapshot(directory,workspace):
    source=directory/'chroma.sqlite3'
    paths=[source,source.with_name(source.name+'-wal')]
    baseline=[_digest(path) for path in paths]
    if baseline[0] is None:fail(40401,'INDEX_STORE_NOT_FOUND',404)
    task_root=(workspace/'.cache/index-inventory').resolve()
    task_root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='catalog-',dir=task_root) as temporary:
        target=Path(temporary).resolve()
        # TemporaryDirectory's recursive cleanup is confined to our workspace.
        if not target.is_relative_to(task_root):raise RuntimeError('Invalid inventory temporary directory')
        for path,expected in zip(paths,baseline):
            if expected is not None:
                copied=target/path.name
                shutil.copyfile(path,copied)
                if _digest(copied)!=expected:fail(40920,'INDEX_STORE_CHANGED',409)
        if [_digest(path) for path in paths]!=baseline:fail(40920,'INDEX_STORE_CHANGED',409)
        connection=sqlite3.connect((target/source.name).as_uri()+'?mode=ro',uri=True,timeout=2)
        try:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            yield connection,content_hash({'sqlite':baseline[0],'wal':baseline[1]})
        finally:connection.close()


def inspect_indexes(directory,collections=None,*,tenant='default_tenant',database='default_database',workspace=None):
    """Inspect a quiescent local store. Never infer provenance from .env/model probes."""
    directory=Path(directory).resolve()
    from app.rag.chroma_transport import route_port

    if route_port(directory) is not None:
        fail(42220,'INDEX_INVENTORY_REQUIRES_OFFLINE_STORE',422)
    workspace=Path(workspace or Path(__file__).resolve().parents[2]).resolve()
    collections=collections or {'rag':'rag_collection','notes':'notes_collection'}
    if (not 1<=len(collections)<=100 or len(set(collections.values()))!=len(collections)
            or any(not re.fullmatch(r'[a-z][a-z0-9_.:-]{0,63}',key)
                   or not isinstance(name,str) or not 1<=len(name)<=128 for key,name in collections.items())
            or not isinstance(tenant,str) or not 1<=len(tenant)<=128
            or not isinstance(database,str) or not 1<=len(database)<=128):
        fail(40002,'INDEX_INVENTORY_INPUT_INVALID',422)
    store_id=storage_identity(directory,tenant,database)
    try:
        with open_catalog_snapshot(directory,workspace) as (connection,snapshot):
            required={'collections':{'id','name','dimension','database_id'},'databases':{'id','name','tenant_id'},
                      'segments':{'id','collection','scope'},'embeddings':{'segment_id'}}
            for table,columns in required.items():
                actual={row[1] for row in connection.execute('PRAGMA table_info('+table+')')}
                if not columns.issubset(actual):fail(42220,'INDEX_CATALOG_LAYOUT_UNSUPPORTED',422)
            if connection.execute('PRAGMA quick_check(1)').fetchone()!=('ok',):
                fail(42220,'INDEX_CATALOG_CORRUPT',422)
            items=[]
            for key,name in sorted(collections.items()):
                rows=connection.execute('SELECT c.id,c.dimension FROM collections c JOIN databases d ON c.database_id=d.id '
                    'WHERE c.name=? AND d.name=? AND d.tenant_id=?',(name,database,tenant)).fetchall()
                if len(rows)>1:fail(42220,'INDEX_CATALOG_AMBIGUOUS',422)
                collection_id,dimension=rows[0] if rows else (None,None)
                if dimension is not None and (not isinstance(dimension,int) or dimension<=0):
                    fail(42220,'INDEX_CATALOG_DIMENSION_INVALID',422)
                count=connection.execute('SELECT COUNT(*) FROM embeddings e JOIN segments s ON e.segment_id=s.id '
                    "WHERE s.collection=? AND s.scope='METADATA'",(collection_id,)).fetchone()[0] if rows else None
                item={'index_key':key,'collection_name':name,'collection_id':collection_id,
                    'dimension':dimension,'metadata_record_count':count,'status':'baseline_unverified',
                    'reason':'COLLECTION_MISSING' if not rows else 'DIMENSION_UNKNOWN' if dimension is None else 'SEMANTIC_PROVENANCE_MISSING'}
                items.append(item)
            return {'inventory_version':INVENTORY_VERSION,'store_id':store_id,'store_snapshot':snapshot,
                    'tenant':tenant,'database':database,'collections':items}
    except (OSError,sqlite3.Error,ValueError):
        # OS/SQLite messages may disclose local paths or raw catalog values.
        fail(42220,'INDEX_CATALOG_UNREADABLE',422)
