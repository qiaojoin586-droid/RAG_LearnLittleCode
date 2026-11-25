"""A backup needs source-role evidence and follows the pinned dispatch boundary."""
import copy
import json

import httpx
import pytest
from sqlalchemy.orm import Session

from app.core.failed_response import BusinessError
from app.model_config import tables as tables
from app.model_config.provider import ProviderFailure
from app.model_config.prompt_contracts import default_template
from app.model_config.runtime import model_for
from test_credential_revocation import revoke
from test_model_config_control_plane import AsyncFacade, admin, profile_op, prompt_op, store  # noqa: F401
from test_model_config_publication import mock_runtime, publish, selected, snapshot
from test_model_config_validation import validation_service


async def configure(admin, *, local=False, mismatch=None, cross_service=False):
    draft = await admin.draft()
    policy = await admin.policy(local=local, allow_cross_service_fallback=cross_service)
    backup_policy = policy
    if cross_service:
        backup_policy = await admin.policy(key='backup-cloud', origin='https://backup.example.com',
                                           allow_cross_service_fallback=mismatch != 'opt-in')
    primary = profile_op(policy, local=local)
    alternate = profile_op(backup_policy, local=local, key='different-account-credential' if mismatch == 'credential' or cross_service else
                           'fictional-secret-long-key-123456')
    primary['profile']['context_window'] = 8192
    alternate['role_key'] = 'session_title'
    alternate['profile'].update(model='backup-model', context_window=4096 if mismatch == 'context' else 16384)
    if mismatch == 'egress':
        alternate['profile']['base_url'] += '/another-project'
    alternate_prompt = prompt_op('session_title', 'Backup role system prompt')
    alternate_prompt['prompt']['template_text'] = default_template('session_title')
    draft, _ = await admin.call('patch_draft', draft['id'], {'operations': [
        primary, alternate, prompt_op(text='Source system prompt'), alternate_prompt,
    ]}, 'models', draft['etag'], None if local else admin.proof())
    operations = []
    for role, backups in [('chat', ['session_title']), ('session_title', [])]:
        binding = copy.deepcopy(draft['payload']['role_bindings_raw'][role])
        binding.update(enabled=True, fallback=backups, failure_mode='error')
        operations.append({'op': 'replace_binding', 'role_key': role, 'binding': binding})
    draft, _ = await admin.call('patch_draft', draft['id'], {'operations': operations}, 'roles', draft['etag'])
    return draft


async def probe(admin, draft, role, backup=None):
    validation, requests = validation_service(admin)
    body = {'role_key': role, 'expected_draft_version': draft['draft_version']}
    if backup:
        body['fallback_role_key'] = backup
    with Session(admin.engine) as session:
        result, _ = await validation.test_draft(AsyncFacade(session), admin.actor, draft['id'], body,
                                                f'probe-{role}-{backup}', draft['etag'])
    assert result['status'] == 'passed', result
    return result, requests


async def verified(admin, *, local=False, cross_service=False):
    draft = await configure(admin, local=local, cross_service=cross_service)
    await probe(admin, draft, 'chat')
    await probe(admin, draft, 'session_title')
    await probe(admin, draft, 'chat', 'session_title')
    await publish(admin, draft)
    return await snapshot(admin)


@pytest.mark.asyncio
async def test_cross_service_requires_permission_on_both_endpoint_policies(admin):
    draft = await configure(admin, cross_service=True, mismatch='opt-in')
    with pytest.raises(BusinessError) as error:
        await probe(admin, draft, 'chat', 'session_title')
    assert error.value.data['reason'] == 'FALLBACK_EGRESS_MISMATCH'
    assert admin.count(tables.ai_config_validations) == 0


@pytest.mark.asyncio
async def test_cross_service_fallback_uses_each_allowed_origin_and_credential(admin, monkeypatch):
    pinned = await verified(admin, cross_service=True)

    def respond(request):
        if request.url.host == 'models.example.com':
            return httpx.Response(503, headers={'Retry-After': '0'})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'backup'}}]})

    requests = mock_runtime(admin, monkeypatch, respond)
    with selected(admin, pinned):
        reply = await model_for('chat').ainvoke('question')
    assert reply.content == 'backup'
    assert [request.url.host for request in requests] == ['models.example.com', 'backup.example.com']
    assert [request.headers['Authorization'] for request in requests] == [
        'Bearer fictional-secret-long-key-123456', 'Bearer different-account-credential']
    assert 'Source system prompt' in [item['content'] for item in json.loads(requests[1].content)['messages']]


@pytest.mark.asyncio
async def test_publish_needs_source_role_probe_against_backup(admin):
    draft = await configure(admin, local=True)
    await probe(admin, draft, 'chat')
    await probe(admin, draft, 'session_title')
    with pytest.raises(BusinessError) as error:
        await publish(admin, draft)
    assert error.value.data['reason'] == 'FALLBACK_CAPABILITY_UNVERIFIED'
    assert admin.count(tables.ai_config_releases) == 0
    result, requests = await probe(admin, draft, 'chat', 'session_title')
    assert result['fallback_role_key'] == 'session_title'
    assert result['target_spec']['fallback_role_key'] == 'session_title'
    assert result['target_spec']['role_key'] == 'chat' and len(requests) == 3
    assert all(json.loads(request.content)['model'] == 'backup-model' for request in requests)
    await publish(admin, draft)
    assert result['id'] in admin.rows(tables.ai_config_releases)[0]['validation_refs']


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch,reason', [
    ('egress', 'FALLBACK_EGRESS_MISMATCH'), ('credential', 'FALLBACK_CREDENTIAL_MISMATCH'),
    ('context', 'FALLBACK_CONTEXT_MISMATCH'),
])
async def test_backup_probe_rejects_incompatible_target_before_network(admin, mismatch, reason):
    draft = await configure(admin, mismatch=mismatch)
    with pytest.raises(BusinessError) as error:
        await probe(admin, draft, 'chat', 'session_title')
    assert error.value.data['reason'] == reason and admin.count(tables.ai_config_validations) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_two_dispatches_keep_source_prompt_and_trace_real_backup(admin, monkeypatch, stream):
    pinned = await verified(admin, local=stream)
    trace_events = []
    monkeypatch.setattr('app.core.model_trace._emit', trace_events.append)

    def response(request):
        body = json.loads(request.content)
        if body['model'] != 'backup-model':
            return httpx.Response(503, headers={'Retry-After': '0'})
        if stream:
            return httpx.Response(200, text='{"message":{"content":"backup"},"done":false}\n'
                                           '{"message":{"content":" worked"},"done":true}\n')
        return httpx.Response(200, json={'choices': [{'message': {'content': 'backup worked'}}]})

    requests = mock_runtime(admin, monkeypatch, response)
    with selected(admin, pinned):
        model = model_for('chat')
        if stream:
            content = ''.join([part.content async for part in model.astream('payload')])
        else:
            content = (await model.ainvoke('payload')).content
    assert content == 'backup worked' and len(requests) == 2
    primary, backup = [json.loads(request.content) for request in requests]
    assert primary['model'] != backup['model'] == 'backup-model'
    assert primary['messages'] == backup['messages']
    assert backup['messages'][1]['content'] == 'Source system prompt'
    assert requests[0].headers.get('authorization') == requests[1].headers.get('authorization')
    successes = [event for event in trace_events if event['success']]
    assert successes[-1]['model'] == 'backup-model'
    assert successes[-1]['profile_revision_id'] == model.backup_profile_id


@pytest.mark.asyncio
@pytest.mark.parametrize('status,wait', [(401, '0'), (429, '5'), (200, '0')])
async def test_denied_or_accepted_response_cannot_trigger_backup(admin, monkeypatch, status, wait):
    pinned = await verified(admin, local=True)
    requests = mock_runtime(admin, monkeypatch, lambda _: httpx.Response(status, headers={'Retry-After': wait},
                                                                         content=b'{"unexpected":true}'))
    with selected(admin, pinned), pytest.raises(ProviderFailure):
        await model_for('chat').ainvoke('payload')
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_revoking_primary_between_attempts_prevents_backup_dispatch(admin, monkeypatch):
    pinned = await verified(admin)
    original_id = pinned.manifest['resolved_static_bindings']['chat']['credential_revision_id']

    class RevokeAfterHeaders(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'error'

        async def aclose(self):
            await revoke(admin, original_id)

    requests = mock_runtime(admin, monkeypatch,
                            lambda _: httpx.Response(503, headers={'Retry-After': '0'}, stream=RevokeAfterHeaders()))
    with selected(admin, pinned), pytest.raises(BusinessError) as error:
        await model_for('chat').ainvoke('payload')
    assert error.value.code == 40926 and len(requests) == 1
