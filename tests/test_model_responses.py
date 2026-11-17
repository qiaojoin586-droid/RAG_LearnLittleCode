import copy
import json
import re

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage, message_chunk_to_message

from app.model_config.provider import ProviderFailure
from app.model_config.runtime import model_for
from test_model_config_control_plane import admin, prompt_op, store  # noqa: F401
from test_model_config_publication import mock_runtime, publish, selected, snapshot
from test_model_config_validation import execute, prepare, validation_service


def message(text):
    return {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}


def result(*items):
    return {'status': 'completed', 'usage': {'input_tokens': 9, 'output_tokens': 4}, 'output': list(items)}


def sse(*events):
    return httpx.Response(200, text=''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events))


def complete(value):
    return {'type': 'response.completed', 'response': value}


def probe_reply(request):
    body = json.loads(request.content)
    assert request.url.path == '/v1/responses' and body['store'] is False
    assert not {'messages', 'max_tokens', 'stream_options'} & body.keys()
    if body['stream']:
        return sse({'type': 'response.output_text.delta', 'delta': 'fixture'}, complete(result(message('fixture'))))
    if body.get('tools'):
        ident = re.search(r'nonce ([a-f0-9]+)', body['input'][0]['content'])[1]
        assert body['tool_choice'] == {'type': 'function', 'name': 'echo_test'}
        return httpx.Response(200, json=result({'type': 'function_call', 'id': 'fc_internal', 'call_id': 'call_probe',
                                              'name': 'echo_test', 'arguments': json.dumps({'nonce': ident})}))
    if body['input'][-1].get('type') == 'function_call_output':
        assert body['input'][-1]['call_id'] == body['input'][-2]['call_id'] == 'call_probe'
        return httpx.Response(200, json=result(message(body['input'][-1]['output'])))
    assert body['text']['format']['type'] == 'json_schema'
    value = body['input'][0]['content'].removeprefix('Return this fixed test JSON exactly: ')
    return httpx.Response(200, json=result(message(value)))


@pytest.mark.asyncio
@pytest.mark.parametrize('role, expected_calls', [('classifier', 1), ('vision', 3)])
async def test_explicit_responses_probe_checks_schema_and_vision(admin, role, expected_calls):
    draft = await prepare(admin, role, protocol='responses')
    validator, requests = validation_service(admin, probe_reply)
    validation, _ = await execute(admin, validator, draft, role=role)
    assert validation['status'] == 'passed' and len(requests) == expected_calls, validation
    if role == 'vision':
        assert json.loads(requests[0].content)['input'][0]['content'][1]['type'] == 'input_image'


async def configured(admin):
    draft = await prepare(admin, protocol='responses')
    draft, _ = await admin.call('patch_draft', draft['id'], {'operations': [prompt_op()]}, 'prompt', draft['etag'])
    binding = copy.deepcopy(draft['payload']['role_bindings_raw']['chat'])
    binding.update(enabled=True, failure_mode='error')
    draft, _ = await admin.call('patch_draft', draft['id'], {'operations': [
        {'op': 'replace_binding', 'role_key': 'chat', 'binding': binding}]}, 'enable', draft['etag'])
    validator, requests = validation_service(admin, probe_reply)
    validation, _ = await execute(admin, validator, draft)
    assert validation['status'] == 'passed' and len(requests) == 3
    await publish(admin, draft)
    return await snapshot(admin)


@pytest.mark.asyncio
async def test_responses_published_runtime_preserves_function_ids_usage_and_text(admin, monkeypatch):
    release = await configured(admin)
    function = {'type': 'function_call', 'id': 'fc_internal', 'call_id': 'call_runtime',
                'name': 'echo_test', 'arguments': '{"nonce":"ping"}'}
    schema = {'name': 'echo_test', 'description': 'Echo', 'parameters': {'type': 'object'}}

    def reply(request):
        body = json.loads(request.content)
        assert request.url.path == '/v1/responses' and body['max_output_tokens'] == 256
        if any(item.get('type') == 'function_call_output' for item in body['input']):
            assert [item['call_id'] for item in body['input'] if item.get('type', '').startswith('function_call')] == [
                'call_runtime', 'call_runtime']
            return httpx.Response(200, json=result(message('tool result')))
        return sse({'type': 'response.reasoning_summary_text.delta', 'delta': 'private'},
                   {'type': 'response.output_text.delta', 'delta': 'checking'},
                   {'type': 'response.function_call_arguments.delta', 'delta': '{"nonce"'},
                   complete(result(message('checking'), function)))

    mock_runtime(admin, monkeypatch, reply)
    with selected(admin, release):
        model = model_for('chat')
        prompt = [HumanMessage(content='hello')]
        chunks = [chunk async for chunk in model.bind_tools([schema]).astream(prompt)]
        combined = sum(chunks[1:], chunks[0])
        assert combined.content == 'checking'
        assert combined.tool_calls[0]['id'] == 'call_runtime'
        assert combined.tool_calls[0]['args'] == {'nonce': 'ping'}
        reply = await model.ainvoke([*prompt, message_chunk_to_message(combined),
                                    ToolMessage(content='pong', tool_call_id='call_runtime')])
    assert reply.content == 'tool result'
    assert reply.usage_metadata == combined.usage_metadata == {'input_tokens': 9, 'output_tokens': 4, 'total_tokens': 13}


@pytest.mark.asyncio
@pytest.mark.parametrize('event, reason', [
    ({'type': 'response.output_item.added', 'item': {'type': 'function_call'}}, 'PROVIDER_STREAM_INCOMPLETE'),
    ({'type': 'response.failed', 'response': {'error': {'message': 'private'}}}, 'PROVIDER_STREAM_INVALID'),
    (complete(result({'type': 'web_search_call'})), 'PROVIDER_RESPONSE_INVALID'),
])
async def test_responses_incomplete_or_unsupported_stream_is_not_success(admin, monkeypatch, event, reason):
    release = await configured(admin)
    requests = mock_runtime(admin, monkeypatch, lambda _: sse(event))
    with selected(admin, release), pytest.raises(ProviderFailure) as failure:
        _ = [chunk async for chunk in model_for('chat').astream('hello')]
    assert failure.value.reason == reason and len(requests) == 1
