"""Exercise the optional v3 role through release publication and the real RAG entry."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.orm import Session

from app.ai_service.context_composer import assistant_data, set_assistant_data
from app.core.failed_response import BusinessError
from app.model_config import tables as t
from app.model_config.contracts import content_hash, validate
from app.model_config.prompt_contracts import FIXED_SAFETY, default_template, output_contract
from app.model_config.publishing import PublishingService
from app.model_config.rebase import merge
from app.model_config.snapshots import thaw
from app.model_config.task_budget import ModelBudgetDenied, TaskModelBudget
from app.rag.query_expansion import published_generator
from app.rag.rag_service import RagService
from test_model_config_control_plane import admin, store, AsyncFacade, profile_op  # noqa: F401
from test_model_config_publication import database_factory, mock_runtime, publish, ready, selected, snapshot
from test_model_config_validation import execute, fixture_handler, validation_service
from test_model_responses import message, probe_reply, result
from test_query_expansion import QUESTION, REWRITES, reply
from test_task_model_budget import use_budget


ROLE = 'rag_query_expand'
SYSTEM = '仅做有限查询改写'


async def patch(admin, draft, operations, key, proof=None):
    view, _ = await admin.call('patch_draft', draft['id'], {'operations':operations},
                               key, draft['etag'], proof)
    return view


async def configured(admin, draft, protocol):
    local = protocol == 'ollama_chat'
    policy = await admin.policy(local=local)
    connection = profile_op(policy, local=local)
    connection['role_key'] = ROLE
    connection['profile'].update(api_protocol=protocol, model='expansion-only', max_output_tokens=4096)
    prompt = {'op':'replace_prompt', 'role_key':ROLE,
              'prompt':{'system_text':SYSTEM, 'template_text':default_template(ROLE)}}
    view = await patch(admin, draft, [connection, prompt], 'expansion-config', None if local else admin.proof())
    binding = {**view['payload']['role_bindings_raw'][ROLE], 'enabled':True}
    view = await patch(admin, view, [{'op':'replace_binding', 'role_key':ROLE, 'binding':binding}], 'enable')
    validator, probes = validation_service(admin, probe_reply if protocol == 'responses' else fixture_handler)
    validation, _ = await execute(admin, validator, view, ROLE)
    assert validation['status'] == 'passed' and validation['result']['role_schema_verified']
    assert validation['max_calls'] == len(probes) == 1
    return view


def retrieval(admin, enabled):
    search = AsyncMock(return_value=[])
    source = SimpleNamespace(search_documents=search, search_notes=search)
    return RagService(source, enable_keyword=True, keyword_search=source,
                      db_session_factory=database_factory(admin), enable_published_expansion=enabled), search


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['chat_completions','responses','ollama_chat'])
async def test_published_protocol_payload_limits_and_gates(admin, monkeypatch, protocol):
    view = await configured(admin, await admin.draft(), protocol)
    await publish(admin, view)
    pinned = await snapshot(admin)
    response_text = reply(REWRITES[0])
    def handler(_):
        if protocol == 'responses':
            return httpx.Response(200, json=result(message(response_text)))
        payload = {'content':response_text}
        return httpx.Response(200, json={'message':payload, 'done':True} if protocol == 'ollama_chat'
                              else {'choices':[{'message':payload}]})
    calls = mock_runtime(admin, monkeypatch, handler)
    service, search = retrieval(admin, True)
    token = assistant_data.set(None)
    try:
        set_assistant_data(rag='private-doc', references='private-note', memory={'content_md':'private-memory'})
        with selected(admin, pinned), use_budget(TaskModelBudget(1, None, {})) as budget:
            disabled, _ = retrieval(admin, False)
            assert (await disabled.query(QUESTION, admin.actor.user_id))['query_expansion']['status'] == 'off'
            assert (await service.query(QUESTION + ' <referenced_notes>private-note</referenced_notes>',
                                        admin.actor.user_id))['query_expansion']['status'] == 'skipped'
            assert not calls
            response = await service.query(QUESTION, admin.actor.user_id)
            assert response['query_expansion']['status'] == 'searched'
            assert search.await_args_list[-1].args == (REWRITES[0], admin.actor.user_id)
            assert budget.calls == 1
            with pytest.raises(ModelBudgetDenied):
                await service.query(QUESTION, admin.actor.user_id)
    finally:
        assistant_data.reset(token)
    call, = calls
    body = json.loads(call.content)
    assert body['model'] == 'expansion-only' and body['stream'] is False and 'tools' not in body
    assert call.url.path == {'responses':'/v1/responses','chat_completions':'/v1/chat/completions',
                             'ollama_chat':'/api/chat'}[protocol]
    assert ('authorization' in call.headers) == (protocol != 'ollama_chat')
    assert body.get('messages', body.get('input')) == [
        {'role':'system','content':FIXED_SAFETY}, {'role':'system','content':SYSTEM},
        {'role':'user','content':default_template(ROLE).replace('{query}',QUESTION)}]
    if protocol == 'ollama_chat':
        assert body['format'] == output_contract(ROLE) and body['options']['num_predict'] == 384
        assert body['think'] is False
    elif protocol == 'responses':
        assert body['text']['format']['schema'] == output_contract(ROLE) and body['max_output_tokens'] == 384
    else:
        assert body['response_format']['json_schema']['schema'] == output_contract(ROLE) and body['max_tokens'] == 384
        assert 'reasoning_effort' not in body  # Generic compatible providers retain their own controls.


@pytest.mark.asyncio
async def test_invalid_output_transient_failure_and_revocation_never_retry(admin, monkeypatch):
    await publish(admin, await configured(admin, await admin.draft(), 'chat_completions'))
    pinned = await snapshot(admin)
    responses = iter([httpx.Response(503), httpx.Response(200,json={'choices':[{'message':{
        'content':'{"queries":[],"filters":{}}'}}]})])
    calls = mock_runtime(admin, monkeypatch, lambda _:next(responses))
    service, search = retrieval(admin, True)
    with selected(admin, pinned):
        for i in (1, 2):
            response = await service.query(QUESTION, admin.actor.user_id)
            assert response['query_expansion']['status'] == 'fallback'
            assert len(calls) == i and search.await_count == i * 4
        admin.sql(t.ai_secrets.update().values(status='revoked',revocation_epoch=1))
        response = await service.query(QUESTION, admin.actor.user_id)
        assert response['query_expansion']['status'] == 'fallback' and len(calls) == 2
        assert all(call.args == (QUESTION, admin.actor.user_id) for call in search.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('old_version', [1,2])
async def test_historical_read_clone_v3_publish_and_restore(admin, old_version):
    first, _ = await publish(admin, await ready(admin))
    if old_version == 1:
        # Seed an authentic pre-memory manifest and digest in the isolated fixture.
        manifest = thaw((await snapshot(admin)).manifest)
        for key in ('role_bindings_raw', 'resolved_static_bindings'):
            del manifest[key]['memory_extraction']
        manifest['schema_version'] = 1
        admin.sql(t.ai_config_releases.update().values(**manifest, content_hash=content_hash(manifest)))
    old = await snapshot(admin)
    original = thaw(old.manifest)
    validate('ReleaseManifestRead', original)
    with selected(admin, old):
        assert published_generator() is None
    clone, _ = await admin.call('create_draft', {'base_release_id':first['release_id'],
                                'expected_head_version':1}, 'clone')
    assert clone['payload']['schema_version'] == 2 and ROLE not in clone['payload']['role_bindings_raw']
    view = await patch(admin, clone, [{'op':'replace_prompt','role_key':ROLE,
        'prompt':{'template_text':'{query}','system_text':SYSTEM}}], 'add-role')
    assert view['payload']['schema_version'] == 3 and not view['payload']['role_bindings_raw'][ROLE]['enabled']
    merged, conflicts = merge(clone['payload'], view['payload'], clone['payload'])
    assert not conflicts and validate('DraftPayload',merged) == view['payload']
    validator, _ = validation_service(admin)
    assert (await execute(admin,validator,view,key='revalidate'))[0]['status'] == 'passed'
    await publish(admin,view,key='v3')
    middle = await snapshot(admin)
    validate('ReleaseManifestRead',thaw(middle.manifest))
    with selected(admin, middle):
        assert published_generator() is None
    clone3, _ = await admin.call('create_draft', {'base_release_id':middle.release_id,
                                 'expected_head_version':2}, 'clone-v3')
    assert clone3['payload'] == view['payload']
    with Session(admin.engine) as session:
        await PublishingService(admin.service,database_factory(admin)).restore(AsyncFacade(session),admin.actor,
            old.release_id,{'expected_head_version':2},'restore-old',admin.proof())
    restored = await snapshot(admin)
    assert restored.release_id != old.release_id and restored.manifest['schema_version'] == 2
    assert thaw(restored.manifest['role_bindings_raw']) == clone['payload']['role_bindings_raw']
    assert admin.rows(t.ai_config_releases)[0]['content_hash'] == content_hash(original)
    assert thaw(old.manifest) == original
    with Session(admin.engine) as session:
        await PublishingService(admin.service,database_factory(admin)).restore(AsyncFacade(session),admin.actor,
            middle.release_id,{'expected_head_version':3},'restore-v3',admin.proof())
    assert thaw((await snapshot(admin)).manifest) == thaw(middle.manifest)


@pytest.mark.asyncio
async def test_expansion_requires_dedicated_binding_and_prompt(admin):
    draft = await ready(admin)
    draft = await patch(admin,draft,[{'op':'replace_prompt','role_key':ROLE,
                         'prompt':{'system_text':SYSTEM,'template_text':'{query}'}}],'dedicated-prompt')
    prompt = draft['payload']['role_bindings_raw'][ROLE]['prompt_revision_id']
    binding = {**draft['payload']['role_bindings_raw']['chat'], 'failure_mode':'disabled','prompt_revision_id':prompt}
    with pytest.raises(BusinessError) as denied:
        await patch(admin,draft,[{'op':'replace_binding','role_key':ROLE,'binding':binding}],'borrow')
    assert denied.value.data['reason'] == 'EXPANSION_DEDICATED_PROFILE_REQUIRED'
    for updates in ({'selection':'follow_role','source_role':'chat'}, {'fallback':['chat']}):
        bad = {**draft['payload']['role_bindings_raw']['session_title'], **updates}
        with pytest.raises(BusinessError):
            await patch(admin,draft,[{'op':'replace_binding','role_key':ROLE,'binding':bad}],'bad-'+next(iter(updates)))
    from app.model_config.prompt_contracts import validate_prompt
    with pytest.raises(BusinessError):
        validate_prompt(ROLE, {'template_text':'{query} {content_md}'})
