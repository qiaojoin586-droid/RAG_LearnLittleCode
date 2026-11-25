"""Context admission exercises the generated wire request before transport."""

from dataclasses import replace
import json
from unittest.mock import Mock

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
import pytest

from app.ai_service.context_composer import assistant_data, compose_history, set_assistant_data
from app.core.failed_response import BusinessError
from app.model_config.context_budget import IMAGE_RESERVE, ModelContextExceeded, message_cost, text_counter
from app.model_config.runtime import model_for
from app.model_config.snapshots import freeze
from app.model_config.task_budget import TaskModelBudget, current_model_budget
from test_model_config_control_plane import admin, profile_op, store  # noqa: F401
from test_model_config_publication import mock_runtime, selected
from test_model_fallback import verified


def resized(pinned, window):
    primary = pinned.manifest['resolved_static_bindings']['chat']['profile_revision_id']
    roles = pinned.manifest['role_bindings_raw']
    return replace(pinned, profiles=freeze({**pinned.profiles, primary: {**pinned.profiles[primary], 'context_window': window}}),
                   manifest=freeze({**pinned.manifest, 'role_bindings_raw': {**roles, 'chat': {**roles['chat'], 'fallback': ()}}}))


def test_tokenizer_cache_miss_and_revision_mismatch_never_fetch(monkeypatch, tmp_path):
    monkeypatch.setenv('TIKTOKEN_CACHE_DIR', str(tmp_path))
    encoding = Mock(side_effect=AssertionError('unexpected vocabulary fetch'))
    monkeypatch.setattr('tiktoken.get_encoding', encoding)
    profile = {'tokenizer_id': 'cl100k_base', 'token_counting_policy': {'mode': 'exact'}}
    assert text_counter(profile)('汉字🙂') == 10
    assert text_counter({**profile, 'tokenizer_revision': 'wrong'})('汉字🙂') == 10
    encoding.assert_not_called()


def test_exact_counter_uses_verified_encoder_and_treats_special_markers_as_text(monkeypatch):
    import tiktoken
    encoder = tiktoken.Encoding(name='fixture-byte-vocab', pat_str=r'.+', special_tokens={'<marker>': 300},
                                mergeable_ranks={bytes([i]): i for i in range(256)} | {b'ab': 256})
    monkeypatch.setattr('app.model_config.context_budget.local_encoder', lambda *_: encoder)
    counter = text_counter({'tokenizer_id': 'o200k_base', 'token_counting_policy': {'mode': 'exact'}})
    assert counter('ab') == 1
    assert counter('<marker>') == len('<marker>')


@pytest.mark.parametrize('native', [False, True])
def test_media_is_counted_per_image_without_serializing_binary(native):
    urls = ['A' * 100000, 'B' * 10]
    content = {'role': 'user', 'content': '看图', 'images': urls} if native else {
        'role': 'user', 'content': [{'type': 'text', 'text': '看图'}, *[
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + blob}} for blob in urls]]}
    tokens = message_cost(content, lambda text: len(text.encode()))
    assert IMAGE_RESERVE * 2 < tokens < IMAGE_RESERVE * 2 + 100


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True])
async def test_answer_citations_require_delivery_after_context_fitting(admin, monkeypatch, native):
    from app.rag.citations import CitationTurn, current_citations

    pinned = await verified(admin, local=native)
    answer = {'content': 'Claim [E1]'}
    requests = mock_runtime(admin, monkeypatch, lambda _: httpx.Response(200, json=(
        {'message': answer} if native else {'choices': [{'message': answer}]})))
    retrieval = 'fixture evidence ' * 240
    data_token = assistant_data.set(None)
    turn = CitationTurn(retrieval, [{'citation_id': 'E1'}])
    citation_token = current_citations.set(turn)
    try:
        set_assistant_data(rag=retrieval)
        for snapshot, expected in [(pinned, True), (resized(pinned, 1650), False)]:
            with selected(admin, snapshot):
                await model_for('chat').ainvoke([HumanMessage(content='Give the conclusion')])
            assert ('fixture evidence' in requests[-1].content.decode()) is expected
            assert turn.delivered is expected
    finally:
        current_citations.reset(citation_token)
        assistant_data.reset(data_token)


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True])
async def test_smaller_window_recomputes_history_and_preserves_question_tools_and_rules(admin, monkeypatch, native):
    pinned = await verified(admin, local=native)
    body_result = {'message': {'content': 'ok'}} if native else {'choices': [{'message': {'content': 'ok'}}]}
    requests = mock_runtime(admin, monkeypatch, lambda _: httpx.Response(200, json=body_result))
    token = assistant_data.set(None)
    try:
        set_assistant_data(rag='old retrieval ' * 160)
        messages = [*compose_history([HumanMessage(content='old question ' * 65),
                    AIMessage(content='old answer ' * 65)]),
                    HumanMessage(content='complete current question'),
                    AIMessage(content='', tool_calls=[{'name': 'fixture', 'args': {}, 'id': 'tool-1'}]),
                    ToolMessage(content='active tool result', tool_call_id='tool-1')]
        with selected(admin, pinned):
            await model_for('chat').ainvoke(messages)
        with selected(admin, resized(pinned, 1500)):
            await model_for('chat').ainvoke(messages)
    finally:
        assistant_data.reset(token)
    large, small = [json.loads(request.content) for request in requests]
    assert 'old answer' in str(large) and 'old retrieval' in str(large)
    assert 'old answer' not in str(small) and 'old question' not in str(small) and 'old retrieval' not in str(small)
    assert small['messages'][:2] == large['messages'][:2]
    assert small['messages'][-3:] == large['messages'][-3:]
    if native:
        assert small['options']['num_ctx'] == 1500


@pytest.mark.asyncio
@pytest.mark.parametrize('component', ['question', 'image', 'tool-description', 'output'])
async def test_required_content_rejects_before_network_and_budget_admission(admin, monkeypatch, component):
    pinned = resized(await verified(admin), 1250 if component != 'output' else 200)
    requests = mock_runtime(admin, monkeypatch)
    traces = []
    monkeypatch.setattr('app.core.model_trace._emit', traces.append)
    prompt = 'large' * 1000 if component == 'question' else 'keep current question'
    if component == 'image':
        prompt = [{'type': 'text', 'text': prompt}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,a'}}]
    options = {'tools': [{'type': 'function', 'function': {'name': 'fixture',
                 'description': 'large' * 1000, 'parameters': {'type': 'object'}}}]} if component == 'tool-description' else {}
    ledger = TaskModelBudget(1, None, {})
    token = current_model_budget.set(ledger)
    try:
        with selected(admin, pinned), pytest.raises(ModelContextExceeded) as denied:
            await model_for('chat').ainvoke([HumanMessage(content=prompt)], **options)
    finally:
        current_model_budget.reset(token)
    assert not requests and not traces and ledger.calls == 0
    assert denied.value.http_status == 413 and denied.value.stream_event()['code'] == 40008


@pytest.mark.asyncio
async def test_memory_omission_is_reflected_in_actual_call_trace(admin, monkeypatch):
    pinned = resized(await verified(admin), 1100)
    events = []
    monkeypatch.setattr('app.core.model_trace._emit', events.append)
    requests = mock_runtime(admin, monkeypatch, lambda _: httpx.Response(200, json={'choices': [{
        'message': {'content': 'ok'}}]}))
    token = assistant_data.set(None)
    try:
        with selected(admin, pinned):
            model = model_for('chat')
            set_assistant_data(memory={'revision_id': 'r1', 'head_version': 1, 'memory_epoch': 1,
                'content_hash': 'base', 'selected_hash': 'slice', 'truncated': False, 'content_md': 'old preference ' * 200})
            await model.ainvoke([*compose_history([SystemMessage(content='old summary ' * 200)]), HumanMessage(content='new question')])
    finally:
        assistant_data.reset(token)
    assert len(events) == len(requests) == 1
    assert 'old preference' not in str(requests[0].content) and 'new question' in str(requests[0].content)
    assert events[0]['memory_context']['omitted'] is True and events[0]['memory_context']['selected_hash'] is None


@pytest.mark.asyncio
async def test_profile_save_rejects_window_without_input_room(admin):
    draft = await admin.draft()
    profile = profile_op(await admin.policy(local=True), local=True)
    profile['profile']['context_window'] = profile['profile']['max_output_tokens']
    with pytest.raises(BusinessError) as denied:
        await admin.call('patch_draft', draft['id'], {'operations': [profile]}, 'bad-window', draft['etag'])
    assert denied.value.data['reason'] == 'MODEL_OUTPUT_EXCEEDS_CONTEXT'
