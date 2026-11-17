"""Resolve and fence the physical targets of historical index catch-up."""

import sqlalchemy as sa

from app.model_config import tables
from app.model_config.contracts import content_hash
from app.model_config.endpoints import check_profile
from app.model_config.errors import fail
from app.model_config.index_binding import SUPPORTED_INDEXES
from app.model_config.index_switch import _location, _slot
from app.model_config.snapshots import load_snapshot, thaw, use_snapshot
from app.model_config.vector_runtime import selected_index
from app.models.index_build import index_builds as jobs


CATCH_UP_STATES = ('queued', 'building', 'catching_up', 'ready_to_switch')


async def restore_target(control, db, release_id, active_release_id):
    release = await control._entity(db, tables.ai_config_releases, release_id)
    if not active_release_id or active_release_id == release_id:
        fail(40923, 'INDEX_RESTORE_TARGET_ACTIVE')
    snapshot = await load_snapshot(db, release_id=release_id)
    active = await load_snapshot(db, release_id=active_release_id)
    if (not snapshot.manifest['role_bindings_raw']['embedding']['enabled']
            or set(snapshot.manifest['index_bindings']) != SUPPORTED_INDEXES):
        fail(42220, 'INDEX_RESTORE_EMBEDDING_REQUIRED', 422)

    indexes, profiles = {}, {}
    with use_snapshot(snapshot):
        for key in sorted(SUPPORTED_INDEXES):
            _, index, profile = selected_index(key)
            if index['index_key'] != key:
                fail(42220, 'INDEX_RESTORE_BINDING_INVALID', 422)
            indexes[key], profiles[profile['id']] = index, profile

    if set(profiles) != {snapshot.manifest['resolved_static_bindings']['embedding']['profile_revision_id']}:
        fail(42220, 'INDEX_RESTORE_BINDING_INVALID', 422)
    profile = next(iter(profiles.values()))
    active_indexes = [thaw(active.indexes[binding['index_version_id']])
                      for binding in active.manifest['index_bindings'].values()]
    active_slots = {_slot(_location(index)) for index in active_indexes}
    active_collections = {index['storage_binding']['collection_id'] for index in active_indexes}
    for index in indexes.values():
        slot, collection_id = _slot(_location(index)), index['storage_binding']['collection_id']
        if slot in active_slots or collection_id in active_collections:
            fail(40923, 'INDEX_RESTORE_TARGET_ACTIVE')
        active_slots.add(slot)
        active_collections.add(collection_id)

    policy = await control._entity(db, tables.ai_endpoint_policies, profile['endpoint_policy_revision_id'])
    origin = check_profile(profile, policy)
    credential_id = profile['credential_revision_id']
    credential = await control._entity(db, tables.ai_secrets, credential_id) if credential_id else None
    if credential and (credential['allowed_origin'] != origin or credential['status'] not in ('active', 'retired')):
        fail(40310, 'INDEX_RESTORE_CREDENTIAL_UNAVAILABLE', 403)
    fingerprint = content_hash({
        'release': release['content_hash'], 'profile': profile['content_hash'],
        'storage': {key: index['storage_binding'] for key, index in indexes.items()},
        'credential_epoch': credential['revocation_epoch'] if credential else None,
    })
    return release, indexes, profile, policy, credential, fingerprint


async def overlapping_catchups(db, indexes):
    index_ids = {index['id'] for index in indexes.values()}
    collections = {index['storage_binding']['collection_id'] for index in indexes.values()}
    rows = (await db.execute(sa.select(jobs).where(jobs.c.restore_release_id.is_not(None),
        jobs.c.status.in_(CATCH_UP_STATES)).order_by(jobs.c.id).with_for_update())).mappings().all()
    return [row for row in rows if (index_ids.intersection(row['index_ids'].values())
                                    or collections.intersection(row['collections'].values()))]


async def lock_catchups_for_switch(db, indexes):
    rows = await overlapping_catchups(db, indexes)
    if any(row['lease_token'] is not None for row in rows):
        fail(42220, 'INDEX_RESTORE_CATCH_UP_RUNNING', 422)
    return [row['id'] for row in rows]
