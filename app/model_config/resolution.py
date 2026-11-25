"""Offline binding compiler; capability and index compatibility are never guessed."""
from __future__ import annotations

import copy

from app.model_config.errors import fail
from app.model_config.registry import PROMPT_KEYS


def resolve_bindings(payload, profiles, prompts, credentials, indexes):
    bindings = payload['role_bindings_raw']
    visiting,visited = set(),set()
    def walk(role):
        if role in visiting:
            fail(40002,'ROLE_BINDING_CYCLE',422,role_key=role)
        if role in visited:
            return
        visiting.add(role)
        binding = bindings[role]
        edges = binding['fallback'][:]
        if binding['selection']=='follow_role':
            edges.append(binding['source_role'])
        for target in edges:
            if (role=='embedding') != (target=='embedding'):
                fail(40002,'MODEL_KIND_MISMATCH',422,role_key=role)
            walk(target)
        visiting.remove(role)
        visited.add(role)
    for role in bindings:
        walk(role)

    resolved,dynamic,reverse,issues = {},[],{},[]
    allowed_failure = {'rule_only':'classifier','react_fallback':'planner','default_title':'session_title'}
    def issue(role,reason):
        issues.append({'role_key':role,'reason':reason})
    def record(entity,role):
        if entity:
            reverse.setdefault(entity,set()).add(role)
    def resolve(role):
        if role in resolved:
            return resolved[role]
        raw = bindings[role]
        if raw['failure_mode'] in allowed_failure and role != allowed_failure[raw['failure_mode']]:
            fail(40002,'ROLE_FAILURE_MODE_MISMATCH',422,role_key=role)
        prompt = prompts.get(raw.get('prompt_revision_id'))
        if prompt and prompt['prompt_key'] != PROMPT_KEYS[role]:
            fail(40002,'PROMPT_ROLE_MISMATCH',422,role_key=role)
        if role=='embedding' and raw.get('prompt_revision_id') is not None:
            fail(40002,'EMBEDDING_HAS_PROMPT',422)
        if raw['selection']=='follow_execution':
            if role!='synthesize':
                fail(40002,'DYNAMIC_ROLE_NOT_ALLOWED',422,role_key=role)
            if raw['enabled']:
                dynamic.append(role)
                if not prompt:
                    issue(role,'PROMPT_REQUIRED')
            record(raw.get('prompt_revision_id'),role)
            return None
        result = copy.deepcopy(raw)
        if raw['selection']=='follow_role':
            parent = resolve(raw['source_role'])
            if parent is None:
                fail(40002,'DYNAMIC_SOURCE_NOT_ALLOWED',422,role_key=role)
            for key in ('profile_revision_id','credential_revision_id'):
                result[key] = parent.get(key)
            if raw['enabled'] and not bindings[raw['source_role']]['enabled']:
                issue(role,'SOURCE_ROLE_DISABLED')
        profile = profiles.get(result.get('profile_revision_id'))
        if profile:
            if role == 'rag_query_expand' and profile['profile_key'] != role:
                fail(40002,'EXPANSION_DEDICATED_PROFILE_REQUIRED',422,role_key=role)
            if profile['model_kind'] != ('embedding' if role=='embedding' else 'chat'):
                fail(40002,'MODEL_KIND_MISMATCH',422,role_key=role)
            if profile['credential_revision_id'] != result.get('credential_revision_id'):
                fail(40002,'CREDENTIAL_BINDING_MISMATCH',422,role_key=role)
            credential = credentials.get(result.get('credential_revision_id'))
            if profile['auth_mode']=='none' and credential or profile['auth_mode']!='none' and not credential:
                fail(40002,'CREDENTIAL_BINDING_MISMATCH',422,role_key=role)
        elif result.get('credential_revision_id'):
            fail(40002,'CREDENTIAL_BINDING_MISMATCH',422,role_key=role)
        if raw['enabled']:
            if not profile:
                issue(role,'PROFILE_REQUIRED')
            if role!='embedding' and not prompt:
                issue(role,'PROMPT_REQUIRED')
            for target in raw['fallback']:
                if not bindings[target]['enabled']:
                    issue(role,'FALLBACK_ROLE_DISABLED')
        resolved[role] = result
        for key in ('profile_revision_id','credential_revision_id','prompt_revision_id'):
            record(result.get(key),role)
        if profile:
            record(profile['endpoint_policy_revision_id'],role)
        return result

    states = []
    for role in bindings:
        result = resolve(role)
        if result is None:
            states.append({'role_key':role,'status':'dynamic' if bindings[role]['enabled'] else 'disabled',
                'has_api_key':None,'masked_suffix':None,'credential_revision_id':None,
                'credential_version':None,'auth_mode':None})
            continue
        profile = profiles.get(result.get('profile_revision_id'))
        credential = credentials.get(result.get('credential_revision_id'))
        status = 'disabled' if not result['enabled'] else ('unconfigured' if not profile else
            'unauthenticated' if profile['auth_mode']=='none' else credential['status'])
        states.append({'role_key':role,'status':status,'has_api_key':bool(credential),
            'masked_suffix':credential['masked_suffix'] if credential else None,
            'credential_revision_id':credential['id'] if credential else None,
            'credential_version':credential['credential_version'] if credential else None,
            'auth_mode':profile['auth_mode'] if profile else None})
    embedding_diff,index_states = [],[]
    for key,binding in payload['index_bindings'].items():
        index = indexes[binding['index_version_id']]
        profile = profiles[binding['query_profile_revision_id']]
        if profile['model_kind']!='embedding' or profile['credential_revision_id'] != binding['credential_revision_id']:
            fail(40002,'INDEX_BINDING_MISMATCH',422)
        from app.model_config.embedding_space import compatibility
        reason=compatibility(index,profile)
        embedding_diff.append({'index_key':key,'status':'compatible' if reason=='COMPATIBLE' else 'pending_rebuild',
                               'reason':reason,'migration_available':False})
        credential = credentials.get(binding['credential_revision_id'])
        index_states.append({'index_key':key,'credential_revision_id':binding['credential_revision_id'],
            'has_api_key':bool(credential),'masked_suffix':credential['masked_suffix'] if credential else None,
            'credential_version':credential['credential_version'] if credential else None})
    return {'resolved_static_bindings':resolved,'follow_execution_roles':dynamic,
        'reverse_refs':{key:sorted(value) for key,value in sorted(reverse.items())},
        'affected_roles':sorted({role for roles in reverse.values() for role in roles}),
        'binding_issues':issues,'credential_states':states,'index_credential_states':index_states,
        'embedding_diff':embedding_diff}
