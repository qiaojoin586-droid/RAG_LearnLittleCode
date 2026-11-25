"""Check collection isolation and freshness at the release transaction boundary."""

import asyncio
from collections import defaultdict
import os
from pathlib import Path
import sqlite3

import sqlalchemy as sa

from app.core.failed_response import BusinessError
from app.model_config import tables
from app.model_config.contracts import content_hash
from app.model_config.errors import fail
from app.model_config.index_binding import BUILD_PROVENANCE, check_storage, published_index_ids
from app.model_config.index_inventory import open_catalog_snapshot
from app.model_config.security import scoped
from app.model_config.service import json_view
from app.models.knowledge_ingest import knowledge_ingest_jobs
from app.models.note_index import note_index_jobs
from app.rag.chroma_transport import ChromaTransportError, http_chroma, route_port
from app.rag.maintenance_lock import WORKSPACE
from app.rag.reconcile import Binding, INDEXES, Location, _inspect, _inspect_service, _read_sources


def _location(index):
    storage = check_storage(index)
    return Location(Path(storage['directory']).resolve(), storage['tenant'], storage['database'], index['collection_name'])


def _slot(location):
    return (os.path.normcase(str(location.directory)), location.tenant, location.database, location.name)


async def assert_isolated_candidates(db, candidates, legacy):
    """Never attest a replacement over any currently or formerly published collection."""
    slots, collection_ids = set(), set()
    directory = Path(legacy.get('persist_directory', 'data/chroma')).resolve()
    for key, (name, _) in INDEXES.items():
        slots.add(_slot(Location(directory, 'default_tenant', 'default_database', legacy.get('collections', {}).get(key, name))))
    previously_published = {index_id for key in INDEXES for index_id in await published_index_ids(db, key)}
    table = tables.rag_index_versions
    if previously_published:
        rows = await db.execute(sa.select(table).where(scoped(table), table.c.id.in_(previously_published)))
        for row in rows.mappings():
            slots.add(_slot(_location(row)))
            collection_ids.add(row['storage_binding']['collection_id'])
    for index in candidates:
        slot = _slot(_location(index))
        collection_id = index['storage_binding']['collection_id']
        if slot in slots or collection_id in collection_ids:
            fail(42220, 'INDEX_MIGRATION_REQUIRES_ISOLATED_COLLECTION', 422)
        slots.add(slot)
        collection_ids.add(collection_id)


async def _pending(db, table, identifier):
    rows = await db.execute(sa.select(identifier, table.c.status).order_by(identifier).with_for_update())
    return sum(row.status in ('queued', 'running') for row in rows)


def _verify_collection_report(key, index, report, observed):
    if report['status'] != 'checked':
        fail(42220, 'INDEX_SWITCH_COLLECTION_UNVERIFIED', 422, index_key=key, status=report['status'])
    empty_managed = (observed == (None, 'cosine') and not report['counts']
                     and index['storage_binding']['provenance'] == BUILD_PROVENANCE)
    if observed != (index['dimension'], 'cosine') and not empty_managed:
        fail(42220, 'INDEX_SWITCH_COLLECTION_UNVERIFIED', 422, index_key=key, status='dimension_or_metric')
    differences = {reason: count for reason, count in report['counts'].items() if reason != 'valid_vector'}
    if differences:
        fail(42220, 'INDEX_SWITCH_NOT_CAUGHT_UP', 422, index_key=key, counts=differences)
    return {'index_version_id': index['id'], 'vector_count': report['counts'].get('valid_vector', 0)}


def _verify_service_collection(port, key, index, sources):
    location = _location(index)
    binding = Binding(keys={key}, versions={index['id']: index['storage_binding']['collection_id']})
    with http_chroma(port, location.tenant, location.database) as client:
        collection = client.get_collection(location.name)
        report, _ = _inspect_service(collection, location, binding, sources, None, collect_repairs=False)
        observed = (collection.dimension, (collection.metadata or {}).get('hnsw:space'))
        return {**_verify_collection_report(key, index, report, observed), 'transport': 'chroma_http'}


def _verify_collections(indexes, sources, workspace):
    directories = defaultdict(list)
    for key, index in indexes.items():
        directories[_location(index).directory].append((key, index))
    result = {}
    for directory, entries in directories.items():
        try:
            port = route_port(directory)
            if port is not None:
                for key, index in entries:
                    result[key] = _verify_service_collection(port, key, index, sources[key])
                continue
            with open_catalog_snapshot(directory, workspace) as (catalog, stamp):
                for key, index in entries:
                    location = _location(index)
                    identity = index['storage_binding']['collection_id']
                    binding = Binding(keys={key}, versions={index['id']: identity})
                    report, _ = _inspect(catalog, location, binding, sources[key], None, collect_repairs=False)
                    observed = catalog.execute('''SELECT c.dimension, m.str_value FROM collections c
                        LEFT JOIN collection_metadata m ON m.collection_id=c.id AND m.key='hnsw:space'
                        WHERE c.id=?''', (identity,)).fetchone()
                    result[key] = {**_verify_collection_report(key, index, report, observed), 'catalog_snapshot': stamp}
        except ChromaTransportError:
            fail(42220, 'INDEX_SWITCH_COLLECTION_UNVERIFIED', 422, status='service_unavailable')
        except (OSError, sqlite3.Error):
            fail(42220, 'INDEX_SWITCH_COLLECTION_UNVERIFIED', 422, status='catalog_unavailable')
    return result


async def lock_switch_sources(db):
    if db.get_bind().dialect.name == 'mysql':
        isolation = (await db.execute(sa.text('SELECT @@transaction_isolation'))).scalar_one()
        if isolation not in ('REPEATABLE-READ', 'SERIALIZABLE'):
            fail(50303, 'INDEX_SWITCH_ISOLATION_UNSUPPORTED', 503)
    # The full ranges include gaps for new resources and tombstones. Use locking
    # reads so the gate sees changes committed after the earlier draft lookup.
    knowledge_pending = await _pending(db, knowledge_ingest_jobs, knowledge_ingest_jobs.c.id)
    if knowledge_pending:
        fail(42220, 'INDEX_SWITCH_JOBS_PENDING', 422, index_key='rag', count=knowledge_pending)
    sources = await _read_sources(db, lock=True)
    notes_pending = await _pending(db, note_index_jobs, note_index_jobs.c.note_id)
    if notes_pending:
        fail(42220, 'INDEX_SWITCH_JOBS_PENDING', 422, index_key='notes', count=notes_pending)
    return sources


async def verify_index_switch(db, indexes, *, workspace=WORKSPACE):
    # HTTP deployments still use one application writer per workspace.
    sources = await lock_switch_sources(db)
    from app.rag.keyword_publication import verify_keyword_publication
    keyword = await verify_keyword_publication(db, sources)
    try:
        targets = await asyncio.to_thread(_verify_collections, indexes, sources, Path(workspace))
    except BusinessError as error:
        if error.http_status == 404:
            fail(42220, 'INDEX_SWITCH_COLLECTION_UNVERIFIED', 422, status='catalog_missing')
        raise
    return {'source_watermark': content_hash(json_view(sources)), 'targets': targets, 'keyword': keyword}
