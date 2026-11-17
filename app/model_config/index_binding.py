"""Static binding checks shared by publication and inference."""
from pathlib import Path
import re

from app.model_config.embedding_space import compatibility
from app.model_config.errors import fail

SUPPORTED_INDEXES=frozenset(('rag','notes'))
BASELINE_PROVENANCE = 'operator_attested_build_record_v1'
MIGRATION_PROVENANCE = 'operator_attested_migration_v1'
BUILD_PROVENANCE = 'managed_index_build_v1'


async def published_index_ids(db,key):
    import sqlalchemy as sa
    from app.model_config import tables as t
    from app.model_config.security import scoped
    return (await db.execute(sa.select(t.ai_config_release_refs.c.entity_id).join(t.rag_index_versions,
        t.rag_index_versions.c.id==t.ai_config_release_refs.c.entity_id).where(scoped(t.ai_config_release_refs),
        scoped(t.rag_index_versions),t.ai_config_release_refs.c.entity_kind=='index',
        t.rag_index_versions.c.index_key==key).distinct())).scalars().all()


def check_storage(index):
    value=index.get('storage_binding')
    if (not isinstance(value,dict) or value.get('adapter')!='chroma_local'
            or set(value)!={'adapter','directory','tenant','database','collection_id','evidence_hash','provenance'}
            or value.get('provenance') not in (BASELINE_PROVENANCE, MIGRATION_PROVENANCE, BUILD_PROVENANCE)
            or not all(isinstance(value.get(key),str) and value[key] for key in ('directory','tenant','database','collection_id'))
            or not Path(value['directory']).is_absolute()
            or not re.fullmatch(r'[a-f0-9]{64}',value.get('evidence_hash',''))):
        fail(42220,'INDEX_STORAGE_UNVERIFIED',422)
    return value


def check_binding(index,profile,binding):
    reason=compatibility(index,profile)
    if reason!='COMPATIBLE':fail(42220,reason,422,migration_required=True)
    if binding['credential_revision_id']!=profile['credential_revision_id']:
        fail(40002,'INDEX_BINDING_MISMATCH',422)
    check_storage(index)
    check_embedding_semantics(profile)
    return True


def check_embedding_semantics(profile):
    identity=profile['semantic_identity']
    if profile['api_protocol']=='ollama_embed' and not re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}',identity['weights_revision']):
        fail(42220,'OLLAMA_DIGEST_UNVERIFIED',422)
    if (identity['input_preprocessing']!='utf8-verbatim-v1' or identity['normalization'] not in ('provider','none','l2')
            or set(identity['parameters'])-{'truncate','dimensions'}):
        fail(42220,'EMBEDDING_SEMANTICS_UNSUPPORTED',422)
