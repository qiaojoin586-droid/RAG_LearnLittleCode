"""Explicit operator review of original build records, never inferred provenance.

Only a controlled local CLI can attach this evidence. Artifact hashing detects
changes; the active administrator remains responsible for its truth/source.
"""
import copy
from datetime import timedelta
import json
from pathlib import Path
import re

from app.model_config import tables as t
from app.model_config.contracts import SCHEMA,canonical,content_hash,validate
from app.model_config.embedding_space import semantic_fingerprint
from app.model_config.endpoints import canonical_url
from app.model_config.errors import fail
from app.model_config.index_binding import (
    BASELINE_PROVENANCE, MIGRATION_PROVENANCE, SUPPORTED_INDEXES, check_binding, check_embedding_semantics,
    published_index_ids,
)
from app.model_config.index_inventory import inspect_indexes
from app.model_config.legacy_import import _CLIActor
from app.model_config.security import SCOPE,scoped
from app.model_config.service import uid,utcnow,etag


def prepare_attestation(record_path,directory,index_ids,*,tenant='default_tenant',database='default_database',workspace=None):
    def pairs(items):
        result={}
        for name,value in items:
            if name in result:raise ValueError
            result[name]=value
        return result
    def invalid(_):raise ValueError
    try:
        path=Path(record_path)
        if path.stat().st_size>1048576:raise ValueError
        record=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=pairs,parse_constant=invalid)
    except (OSError,ValueError,UnicodeError):fail(40002,'BUILD_RECORD_INVALID',422)
    fields={'schema_version','semantic_identity','provider_origin','api_protocol','build_reference',
            'parser_version','chunker_version','source_watermark','collections'}
    if (not isinstance(record,dict) or set(record)!=fields or type(record['schema_version']) is not int or record['schema_version']!=1
            or set(index_ids)!=SUPPORTED_INDEXES or len(set(index_ids.values()))!=len(index_ids)
            or any(not re.fullmatch(r'[a-fA-F0-9-]{36}',value) for value in index_ids.values())):
        fail(40002,'BUILD_RECORD_INVALID',422)
    semantic_fingerprint(record['semantic_identity'])
    for name in ('build_reference','parser_version','chunker_version','source_watermark'):
        if (not isinstance(record[name],str) or not 1<=len(record[name])<=(64 if name in ('parser_version','chunker_version') else 128)
                or record[name].strip().lower() in ('unknown','unverified','latest','')):fail(40002,'BUILD_RECORD_INCOMPLETE',422)
    origin,_=canonical_url(record['provider_origin'])
    if record['provider_origin']!=origin:fail(40002,'BUILD_RECORD_INVALID',422)
    collections=record['collections']
    if not isinstance(collections,dict) or set(collections)!=SUPPORTED_INDEXES:fail(40002,'BUILD_RECORD_INVALID',422)
    for item in collections.values():
        if (not isinstance(item,dict) or set(item)!={'collection_name','collection_id','dimension'}
                or not isinstance(item['collection_id'],str) or not item['collection_id']
                or type(item['dimension']) is not int or item['dimension']!=record['semantic_identity']['dimension']):
            fail(40002,'BUILD_RECORD_INVALID',422)
    inventory=inspect_indexes(directory,{key:item['collection_name'] for key,item in collections.items()},
        tenant=tenant,database=database,workspace=workspace)
    for item in inventory['collections']:
        expected=collections[item['index_key']]
        if any(item[name]!=expected[name] for name in expected):fail(42220,'BUILD_RECORD_CATALOG_MISMATCH',422)
    return {'record':record,'record_hash':content_hash(record),'inventory':inventory,
            'index_ids':dict(index_ids),'directory':str(Path(directory).resolve())}


async def bind_embedding_identity(control, db, actor, draft, identity, evidence_hash, provenance,
                                  *, provider_origin=None, api_protocol=None):
    raw = draft['payload']['role_bindings_raw']['embedding']
    if raw['selection'] != 'explicit' or not raw['profile_revision_id']:
        fail(40002, 'EMBEDDING_PROFILE_REQUIRED', 422)
    old = await control._entity(db, t.ai_profile_revisions, raw['profile_revision_id'], draft['id'])
    fingerprint = semantic_fingerprint(identity)
    if (old['model_kind'] != 'embedding' or old['model'] != identity['model'] or old['vendor'] != identity['vendor']
            or (provider_origin is not None and canonical_url(old['base_url'])[0] != provider_origin)
            or (api_protocol is not None and old['api_protocol'] != api_protocol)
            or old['model_revision'] not in (None, identity['weights_revision'])
            or old['embedding_dimension'] not in (None, identity['dimension'])):
        fail(42220, 'BUILD_RECORD_PROFILE_MISMATCH', 422)
    profile = {name: old[name] for name in SCHEMA['$defs']['ProfileInput']['properties']}
    profile.update(model_revision=identity['weights_revision'], embedding_dimension=identity['dimension'])
    validate('ProfileInput', profile)
    check_embedding_semantics({**profile, 'semantic_identity': identity})
    created = await control._profile(db, actor, draft, 'embedding', profile, {'action': 'keep'})
    changes = {'semantic_fingerprint': fingerprint, 'semantic_identity': identity,
               'capabilities': {'semantic_provenance': provenance, 'evidence_hash': evidence_hash},
               'content_hash': content_hash({'profile_hash': created['content_hash'], 'semantic_fingerprint': fingerprint})}
    await db.execute(t.ai_profile_revisions.update().where(scoped(t.ai_profile_revisions),
        t.ai_profile_revisions.c.id == created['id']).values(**changes))
    raw.update(profile_revision_id=created['id'], credential_revision_id=created['credential_revision_id'])
    return {**created, **changes}


class IndexAttestationService:
    def __init__(self,control):self.control=control

    async def attach(self,db,user_id,draft_id,plan,key,match,*,migration=False):
        actor=_CLIActor(user_id,utcnow()+timedelta(seconds=30))
        body={name:value for name,value in plan.items() if name!='directory'}
        body['directory_hash']=content_hash(plan['directory'])
        provenance = MIGRATION_PROVENANCE if migration else BASELINE_PROVENANCE
        if migration:
            body['migration'] = True
        async def operation(head):
            draft=await self.control._draft(db,draft_id,edit=True,match=match)
            if draft['base_head_version']!=head['head_version']:fail(40927,'REBASE_REQUIRED')
            payload=copy.deepcopy(draft['payload']);draft['payload']=payload
            record=plan['record'];identity=record['semantic_identity']
            created = await bind_embedding_identity(self.control, db, actor, draft, identity, plan['record_hash'], provenance,
                provider_origin=record['provider_origin'], api_protocol=record['api_protocol'])
            fingerprint = created['semantic_fingerprint']
            live={}
            if head['active_release_id']:
                release=await self.control._entity(db,t.ai_config_releases,head['active_release_id']);live=release['index_bindings']
            candidates = []
            for observation in plan['inventory']['collections']:
                index_key=observation['index_key']
                baseline=await self.control._entity(db,t.rag_index_versions,plan['index_ids'][index_key])
                baseline_identity=baseline['semantic_identity'] or {}
                if (baseline['status']!='baseline_unverified' or baseline['index_key']!=index_key
                        or baseline_identity.get('store_id')!=plan['inventory']['store_id']
                        or baseline_identity.get('store_snapshot')!=plan['inventory']['store_snapshot']
                        or baseline_identity.get('observation')!=observation):
                    fail(42220,'BASELINE_OBSERVATION_STALE',422)
                storage={'adapter':'chroma_local','directory':plan['directory'],'tenant':plan['inventory']['tenant'],
                    'database':plan['inventory']['database'],'collection_id':observation['collection_id'],
                    'evidence_hash':plan['record_hash'],'provenance':provenance}
                prior_id = None
                if not migration:
                    previous=await published_index_ids(db,index_key)
                    prior_id=live.get(index_key,{}).get('index_version_id')
                    if prior_id is None:
                        if len(previous)>1:fail(42220,'INDEX_MIGRATION_REQUIRED',422)
                        prior_id=previous[0] if previous else None
                if prior_id:
                    index=await self.control._entity(db,t.rag_index_versions,prior_id)
                    prior=index.get('storage_binding') or {}
                    if (index['semantic_fingerprint']!=fingerprint or any(prior.get(name)!=storage[name]
                            for name in ('adapter','directory','tenant','database','collection_id'))):
                        fail(42220,'INDEX_MIGRATION_REQUIRED',422,migration_required=True)
                else:
                    index={**{name:baseline[name] for name in ('scope_type','scope_id','index_key','collection_name','dimension')},
                        'id':uid(),'index_version':await self.control._counter(db,'index',index_key),
                        'build_profile_revision_id':created['id'],'semantic_fingerprint':fingerprint,'semantic_identity':identity,
                        'storage_binding':storage,'parser_version':record['parser_version'],'chunker_version':record['chunker_version'],
                        'source_watermark':record['source_watermark'],'status':'baseline_verified','created_at':await self.control._now(db)}
                    await db.execute(t.rag_index_versions.insert().values(**index))
                    candidates.append(index)
                binding={'index_version_id':index['id'],'query_profile_revision_id':created['id'],
                         'credential_revision_id':created['credential_revision_id']}
                check_binding(index,created,binding)
                payload['index_bindings'][index_key]=binding
            if migration:
                from app.model_config.index_switch import assert_isolated_candidates
                from app.utils.config import get_chroma_config

                await assert_isolated_candidates(db, candidates, get_chroma_config())
            validate('DraftPayload',payload)
            draft.update(draft_version=draft['draft_version']+1,status='editing',payload_hash=content_hash(payload),updated_at=await self.control._now(db))
            await self.control._view(db,draft,register=True)
            await db.execute(t.ai_config_drafts.update().where(scoped(t.ai_config_drafts),t.ai_config_drafts.c.id==draft_id)
                .values(**{name:draft[name] for name in ('payload','payload_hash','draft_version','status','updated_at')}))
            await db.execute(t.ai_config_requests.update().where(scoped(t.ai_config_requests),t.ai_config_requests.c.id==head['request_record_id'])
                .values(execution_state={'authority_kind':'controlled_cli','provenance':provenance}))
            result={'draft_id':draft_id,'draft_version':draft['draft_version'],'etag':etag(draft),'published':False,
                'requires_capability_test':True,'provenance':provenance}
            return result,draft_id,{'draft_id':draft_id,'evidence_hash':plan['record_hash'],'semantic_fingerprint':fingerprint,
                'authority_kind':'controlled_cli','provenance':provenance}
        return await self.control._mutate(db,actor,'index.attest','config.publish',draft_id,body,key,operation,match=match)
