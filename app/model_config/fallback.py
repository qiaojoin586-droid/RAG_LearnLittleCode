"""Compatibility checks shared by probes, publication and pinned runtime."""
import hmac

from app.model_config.endpoints import canonical_url
from app.model_config.errors import fail
from app.model_config.provider import CHAT_PATHS
from app.model_config.security import SCOPE


def same_service(primary, target):
    return (all(primary[field] == target[field] for field in (
        'vendor', 'endpoint_policy_revision_id', 'auth_mode'))
        and canonical_url(primary['base_url']) == canonical_url(target['base_url']))


def backup_profile(role, backup, raw, static, profiles, policies):
    def reject(reason):
        fail(42222, reason, 422, role_key=role, fallback_role_key=backup)

    if role == 'embedding' or role == backup or tuple(raw[role]['fallback']) != (backup,):
        reject('FALLBACK_SINGLE_CHAT_MODEL_REQUIRED')
    primary_binding = static.get(role) or {}
    backup_binding = static.get(backup) or {}
    if not primary_binding.get('enabled') or not backup_binding.get('enabled') or raw[backup]['fallback']:
        reject('FALLBACK_STATIC_ROLE_REQUIRED')
    primary = profiles.get(primary_binding.get('profile_revision_id'))
    target = profiles.get(backup_binding.get('profile_revision_id'))
    if not primary or not target or primary['id'] == target['id'] or (
            primary['model'] == target['model'] and canonical_url(primary['base_url']) == canonical_url(target['base_url'])):
        reject('FALLBACK_DISTINCT_MODEL_REQUIRED')
    if (target['model_kind'] != 'chat' or primary['api_protocol'] not in CHAT_PATHS
            or any(primary[field] != target[field] for field in ('adapter_type', 'api_protocol'))):
        reject('FALLBACK_EGRESS_MISMATCH')
    if not same_service(primary, target):
        source = policies.get(primary['endpoint_policy_revision_id'])
        destination = policies.get(target['endpoint_policy_revision_id'])
        if (not source or not destination or source['id'] == destination['id']
                or any(source[field] != destination[field] for field in ('address_mode', 'environment'))
                or not all(policy['connection_rules'].get('allow_cross_service_fallback') is True
                           for policy in (source, destination))):
            reject('FALLBACK_EGRESS_MISMATCH')
    output_limit = primary['max_output_tokens'] or 2048
    if (not primary['context_window'] or not target['context_window'] or primary['context_window'] <= output_limit
            or target['context_window'] < primary['context_window']
            or (target['max_output_tokens'] or 2048) != output_limit
            or any(primary[field] != target[field] for field in ('tokenizer_id', 'tokenizer_revision', 'token_counting_policy'))):
        reject('FALLBACK_CONTEXT_MISMATCH')
    return target


def require_same_credential(keyring, primary, backup):
    if (primary is None and backup is None) or (primary and backup and primary['id'] == backup['id']):
        return

    def decrypt(secret):
        return keyring.decrypt(secret['ciphertext'], secret['key_version'], {
            **SCOPE, 'id': secret['id'], 'origin': secret['allowed_origin'],
            'version': secret['credential_version'],
        }).encode('utf-8')

    if not primary or not backup or not hmac.compare_digest(decrypt(primary), decrypt(backup)):
        fail(42222, 'FALLBACK_CREDENTIAL_MISMATCH', 422)
