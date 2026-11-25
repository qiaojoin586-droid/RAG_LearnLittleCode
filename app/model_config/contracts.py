"""Packaged field contracts, kept identical to reviewed attachment E by tests."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
import rfc8785

from app.core.failed_response import BusinessError

SCHEMA = json.loads(Path(__file__).with_name('contracts.schema.json').read_text(encoding='utf-8'))
ROLE_KEYS = tuple(SCHEMA['$defs']['RoleKey']['enum'])
VALIDATORS = {key: Draft202012Validator({'$ref': '#/$defs/'+key, '$defs': SCHEMA['$defs']},
             format_checker=FormatChecker()) for key in SCHEMA['$defs']}


def validate(definition: str, value):
    error = next(VALIDATORS[definition].iter_errors(value), None)
    if error is not None:
        # ValidationError.message can embed an API key: never emit its text.
        field = '/'+'/'.join(str(part) for part in error.absolute_path)
        raise BusinessError(40002, message='配置字段不符合契约', http_status=422,
                            data={'reason':'FORMAT_VALIDATION_FAILED','field':field})
    try:
        encoded = canonical(value)
    except (ValueError, TypeError, OverflowError):
        raise BusinessError(40002, message='配置包含不可规范化的数值或文本', http_status=422) from None
    if len(encoded) > 1048576:
        raise BusinessError(40002, message='配置超过 1 MiB 限制', http_status=422)
    return value


def canonical(value) -> bytes:
    return rfc8785.dumps(value)


def content_hash(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def disabled_binding() -> dict:
    return {'enabled':False,'selection':'explicit','profile_revision_id':None,
                'credential_revision_id':None,'prompt_revision_id':None,'source_role':None,
                'fallback':[],'failure_mode':'disabled'}


def empty_payload() -> dict:
    return {'schema_version':2,'role_bindings_raw':{
        role:disabled_binding() for role in ROLE_KEYS},'index_bindings':{}}


def upgrade_payload(payload: dict) -> dict:
    result = copy.deepcopy(payload)
    if result['schema_version'] == 1:
        result['schema_version'] = 2
        result['role_bindings_raw']['memory_extraction'] = empty_payload()['role_bindings_raw']['memory_extraction']
    return validate('DraftPayload', result)
