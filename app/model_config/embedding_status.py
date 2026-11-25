"""Unverified baseline registration and database-only status views.

No import or read operation grants a verified semantic identity, binds an index,
starts rebuilding, alters Chroma, or changes the active release.
"""
from datetime import timedelta

import sqlalchemy as sa

from app.model_config import tables as t
from app.model_config.contracts import content_hash
from app.model_config.embedding_space import compatibility
from app.model_config.embedding_space import semantic_fingerprint
from app.core.failed_response import BusinessError
from app.model_config.errors import fail
from app.model_config.index_inventory import INVENTORY_VERSION,storage_identity
from app.model_config.index_binding import check_binding,check_storage
from app.model_config.legacy_import import _CLIActor
from app.model_config.security import SCOPE,authorize,scoped
from app.model_config.service import json_view,uid,utcnow


class EmbeddingStatusService:
    def __init__(self,control):self.control=control

    async def register(self,db,user_id,inventory,key='index-baseline:system:system:v1'):
        if inventory['inventory_version']!=INVENTORY_VERSION:fail(40002,'INDEX_INVENTORY_VERSION_UNSUPPORTED',422)
        actor=_CLIActor(user_id,utcnow()+timedelta(seconds=30))
        async def operation(head):
            items=[];skipped=[]
            for item in inventory['collections']:
                if not item['collection_id'] or item['dimension'] is None:
                    skipped.append({'index_key':item['index_key'],'reason':item['reason']});continue
                identity={'kind':'catalog_observation_v1','store_id':inventory['store_id'],
                    'store_snapshot':inventory['store_snapshot'],'observation':item}
                existing=(await db.execute(sa.select(t.rag_index_versions).where(scoped(t.rag_index_versions),
                    t.rag_index_versions.c.index_key==item['index_key']).order_by(t.rag_index_versions.c.index_version.desc())
                    .limit(1001))).mappings().all()
                if len(existing)>1000:fail(40930,'INDEX_BASELINE_HISTORY_LIMIT',409)
                same=next((row for row in existing if row['status']=='baseline_unverified' and row['semantic_identity']==identity),None)
                if same:
                    items.append({'index_key':item['index_key'],'id':same['id'],'index_version':same['index_version']});continue
                row={'id':uid(),**SCOPE,'index_key':item['index_key'],
                    'index_version':await self.control._counter(db,'index',item['index_key']),
                    'collection_name':item['collection_name'],'build_profile_revision_id':None,
                    'semantic_fingerprint':None,'semantic_identity':identity,'dimension':item['dimension'],
                    'parser_version':'unknown','chunker_version':'unknown','source_watermark':None,
                    'status':'baseline_unverified','created_at':await self.control._now(db)}
                await db.execute(t.rag_index_versions.insert().values(**row))
                items.append({name:row[name] for name in ('index_key','id','index_version')})
            if not items:fail(42220,'INDEX_BASELINE_NO_OBSERVED_DIMENSION',422)
            await db.execute(t.ai_config_requests.update().where(scoped(t.ai_config_requests),t.ai_config_requests.c.id==head['request_record_id'])
                .values(execution_state={'authority_kind':'controlled_cli','inventory_version':INVENTORY_VERSION}))
            result={'items':items,'skipped':skipped,'status':'baseline_unverified','published':False}
            return result,items[0]['id'],{'store_id':inventory['store_id'],'inventory_hash':content_hash(inventory),
                'index_version_ids':[item['id'] for item in items],'authority_kind':'controlled_cli'}
        return await self.control._mutate(db,actor,'index.baseline.register','config.publish',inventory['store_id'],
            inventory,key,operation,http_status=201)

    async def status(self,db,actor,*,draft_id=None):
        await authorize(db,actor,'config.read')
        rows=(await db.execute(sa.select(t.rag_index_versions).where(scoped(t.rag_index_versions))
            .order_by(t.rag_index_versions.c.index_key,t.rag_index_versions.c.index_version.desc()).limit(1001))).mappings().all()
        if len(rows)>1000:fail(40930,'INDEX_BASELINE_HISTORY_LIMIT',409)
        latest={}
        for row in rows:
            identity=row['semantic_identity'] or {}
            store=identity.get('store_id') if isinstance(identity,dict) else None
            location=row.get('storage_binding')
            if location and all(name in location for name in ('directory','tenant','database')):
                store=storage_identity(location['directory'],location['tenant'],location['database'])
            latest.setdefault((row['index_key'],store),row)
        items=[]
        for (_,store),row in latest.items():
            identity=row['semantic_identity'] or {}
            observation=identity.get('observation',{}) if isinstance(identity,dict) else {}
            try:
                verified=(row['status'] in ('baseline_verified','ready') and semantic_fingerprint(identity)==row['semantic_fingerprint']
                    and identity['dimension']==row['dimension'])
            except BusinessError:verified=False
            try:check_storage(row);storage_verified=True
            except BusinessError:storage_verified=False
            items.append({name:row[name] for name in ('id','index_key','index_version','collection_name','dimension','status','created_at')}
                | {'store_id':store,'metadata_record_count':observation.get('metadata_record_count'),
                   'semantic_provenance_verified':verified,
                   'runtime_usable':False,'reason':'AWAITING_PUBLICATION' if verified and storage_verified else 'SEMANTIC_PROVENANCE_MISSING',
                   'provenance':(row.get('storage_binding') or {}).get('provenance')})
        head=await self.control._head(db)
        bindings=[]
        if head and head['active_release_id']:
            release=await self.control._entity(db,t.ai_config_releases,head['active_release_id'])
            for key,binding in release['index_bindings'].items():
                index=await self.control._entity(db,t.rag_index_versions,binding['index_version_id'])
                profile=await self.control._entity(db,t.ai_profile_revisions,binding['query_profile_revision_id'])
                try:check_binding(index,profile,binding);usable=release['role_bindings_raw']['embedding']['enabled']
                except BusinessError:usable=False
                bindings.append({'index_key':key,'index_version_id':index['id'],'query_profile_revision_id':profile['id'],
                    'reason':compatibility(index,profile),'runtime_usable':usable})
                for item in items:
                    if item['id']==index['id']:item.update(runtime_usable=usable,reason='ACTIVE_BINDING' if usable else 'EMBEDDING_DISABLED')
        draft=None
        if draft_id:
            view=await self.control.get_draft(db,actor,draft_id)
            draft={'id':view['id'],'draft_version':view['draft_version'],
                'embedding_enabled':view['payload']['role_bindings_raw']['embedding']['enabled'],
                'embedding_diff':view['embedding_diff'],'publication_blockers':view['publication_blockers']}
        return json_view({'inventory_version':INVENTORY_VERSION,'items':items,'active_bindings':bindings,'draft':draft,
            'head_version':head['head_version'] if head else 0,'migration_supported':True,
            'runtime_binding_supported':True,'registration_status':'registered' if items else 'not_registered'})
