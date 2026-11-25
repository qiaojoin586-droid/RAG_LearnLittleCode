"""Conservative semantic identity comparison, separate from connection identity."""
from app.model_config.contracts import canonical,content_hash
from app.model_config.errors import fail

FIELDS={'schema_version','vendor','model','weights_revision','dimension','normalization',
        'input_preprocessing','document_prefix','query_prefix','pooling','parameters'}


def semantic_fingerprint(identity):
    if not isinstance(identity,dict) or set(identity)!=FIELDS:fail(42220,'SEMANTIC_IDENTITY_UNVERIFIED',422)
    if type(identity['schema_version']) is not int or identity['schema_version']!=1 or type(identity['dimension']) is not int or identity['dimension']<=0:
        fail(42220,'SEMANTIC_IDENTITY_UNVERIFIED',422)
    for field in ('vendor','model','weights_revision','normalization','input_preprocessing','pooling'):
        value=identity[field]
        if (not isinstance(value,str) or not 1<=len(value)<=255
                or value.strip().lower() in ('unknown','unverified','latest','default')):
            fail(42220,'SEMANTIC_IDENTITY_UNVERIFIED',422)
    for field in ('document_prefix','query_prefix'):
        if not isinstance(identity[field],str) or len(identity[field])>4096:fail(42220,'SEMANTIC_IDENTITY_UNVERIFIED',422)
    parameters=identity['parameters']
    if not isinstance(parameters,dict) or set(parameters)-{'truncate','input_type','dimensions'}:
        fail(42220,'SEMANTIC_IDENTITY_UNVERIFIED',422)
    if ('truncate' in parameters and type(parameters['truncate']) is not bool
            or 'input_type' in parameters and parameters['input_type'] not in ('query','document','symmetric')
            or 'dimensions' in parameters and (type(parameters['dimensions']) is not int or parameters['dimensions']!=identity['dimension'])):
        fail(42220,'SEMANTIC_IDENTITY_UNVERIFIED',422)
    if len(canonical(identity))>16384:fail(42220,'SEMANTIC_IDENTITY_UNVERIFIED',422)
    return content_hash(identity)


def compatibility(index,profile):
    """Equality of names/dimensions alone never authorizes reuse of an index."""
    from app.core.failed_response import BusinessError
    if index['status'] not in ('baseline_verified','ready'):return 'INDEX_BASELINE_UNVERIFIED'
    if profile.get('model_kind')!='embedding':return 'QUERY_PROFILE_NOT_EMBEDDING'
    if not index.get('semantic_fingerprint'):return 'INDEX_SPACE_UNVERIFIED'
    if not profile.get('semantic_fingerprint'):return 'QUERY_SPACE_UNVERIFIED'
    try:
        left=semantic_fingerprint(index.get('semantic_identity'))
        right=semantic_fingerprint(profile.get('semantic_identity'))
    except BusinessError:return 'SEMANTIC_IDENTITY_UNVERIFIED'
    if left!=index['semantic_fingerprint'] or right!=profile['semantic_fingerprint']:
        return 'SEMANTIC_FINGERPRINT_INVALID'
    if (profile.get('model')!=profile['semantic_identity']['model']
            or profile.get('vendor')!=profile['semantic_identity']['vendor']
            or profile.get('model_revision')!=profile['semantic_identity']['weights_revision']):
        return 'QUERY_IDENTITY_PROFILE_MISMATCH'
    if (index['dimension']!=profile.get('embedding_dimension')
            or index['dimension']!=index['semantic_identity']['dimension']
            or profile.get('embedding_dimension')!=profile['semantic_identity']['dimension']):
        return 'EMBEDDING_DIMENSION_MISMATCH'
    if left!=right:return 'EMBEDDING_SPACE_MISMATCH'
    return 'COMPATIBLE'
