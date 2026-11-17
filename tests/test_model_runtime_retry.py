"""Retries are confined to a single model dispatch before response consumption."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock

import httpcore
import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from app.core.failed_response import BusinessError
from app.model_config.provider import ProviderFailure, ProviderGateway, retry_delay
from app.model_config.runtime import model_for
from test_credential_revocation import revoke
from test_model_config_control_plane import admin, store  # noqa: F401
from test_model_config_publication import mock_runtime, publish, ready, selected, snapshot


@pytest.mark.parametrize('value,expected', [
    (None, 0.2), (' 0 ', 0), ('2', 2), ('-1', None), ('0.1', None), ('garbage', None),
])
def test_retry_after_delay_seconds_and_invalid_values(value, expected):
    assert retry_delay(value) == expected


def test_retry_after_accepts_http_date():
    now = datetime.now(timezone.utc)
    assert retry_delay(format_datetime(now - timedelta(seconds=20), usegmt=True)) == 0
    assert 55 < retry_delay(format_datetime(now + timedelta(seconds=60), usegmt=True)) <= 60


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True])
async def test_published_chat_retries_after_explicit_status_with_same_tool_history(admin, monkeypatch, native):
    await publish(admin, await ready(admin, local=native))
    pinned = await snapshot(admin)
    observed = []
    monkeypatch.setattr('app.core.model_trace._emit', observed.append)
    responses = iter([
        httpx.Response(503, headers={'Retry-After': '0'}, content=b'private upstream error'),
        httpx.Response(200, json={'message': {'content': 'OK'}} if native else
                       {'choices': [{'message': {'content': 'OK'}}]}),
    ])
    requests = mock_runtime(admin, monkeypatch, lambda _: next(responses))
    messages = [HumanMessage(content='private user input'), AIMessage(content='', tool_calls=[{
        'name': 'fixture_tool', 'args': {}, 'id': 'call-id', 'type': 'tool_call',
    }]), ToolMessage(content='already saved', tool_call_id='call-id', name='fixture_tool')]
    with selected(admin, pinned):
        assert (await model_for('chat').ainvoke(messages)).content == 'OK'
    assert len(requests) == 2 and requests[0].url == requests[1].url
    assert requests[0].content == requests[1].content and b'already saved' in requests[1].content
    assert requests[0].headers.get('authorization') == requests[1].headers.get('authorization')
    retry = [event for event in observed if event['stage'] == 'model_retry']
    assert len(retry) == 1 and retry[0]['usage_status'] == retry[0]['pricing_status'] == 'unknown'
    assert 'private user input' not in str(retry) and 'private upstream error' not in str(retry)


@pytest.mark.asyncio
async def test_stream_retries_only_before_successful_headers(admin, monkeypatch):
    await publish(admin, await ready(admin, local=False))
    pinned = await snapshot(admin)
    closed = []

    class Unconsumed(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail('An HTTP error body must never be consumed')
            yield b''

        async def aclose(self):
            closed.append(True)

    responses = iter([
        httpx.Response(429, headers={'Retry-After': '0'}, stream=Unconsumed()),
        httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n'),
    ])
    requests = mock_runtime(admin, monkeypatch, lambda _: next(responses))
    with selected(admin, pinned):
        content = ''.join([part.content async for part in model_for('chat').astream('stream')])
    assert content == 'hello' and len(requests) == 2 and closed == [True]


@pytest.mark.asyncio
async def test_stream_failure_after_first_chunk_does_not_repeat_model_call(admin, monkeypatch):
    await publish(admin, await ready(admin, local=False))
    pinned = await snapshot(admin)

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            raise httpx.ReadTimeout('private upstream error')

    requests = mock_runtime(admin, monkeypatch, lambda _: httpx.Response(200, stream=BrokenStream()))
    chunks = []
    with selected(admin, pinned), pytest.raises(ProviderFailure) as error:
        async for part in model_for('chat').astream('stream'):
            chunks.append(part.content)
    assert chunks == ['first'] and len(requests) == 1
    assert error.value.reason == 'PROVIDER_UNAVAILABLE'


def gateway(handler, *, probe=False, timeout_ms=3000):
    profile = {'api_protocol': 'chat_completions', 'base_url': 'https://models.example.com/v1',
               'model': 'fixture', 'adapter_type': 'openai_compatible', 'auth_mode': 'none',
               'request_timeout_ms': timeout_ms, 'connect_timeout_ms': 10}
    return ProviderGateway(profile, {}, None, permit=AsyncMock(),
                           transport=httpx.MockTransport(handler), probe_mode=probe)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,header,timeout_ms,attempts', [
    (500, '0', 3000, 2), (502, '0', 3000, 2), (504, '0', 3000, 2),
    (503, '3', 3000, 1), (429, '1', 500, 1), (429, 'malformed', 3000, 1),
    (400, '0', 3000, 1), (401, '0', 3000, 1), (501, '0', 3000, 1),
])
async def test_retry_budget_and_non_retryable_status(status, header, timeout_ms, attempts):
    handler = AsyncMock(side_effect=lambda _: httpx.Response(status, headers={'Retry-After': header}))
    async with gateway(handler, timeout_ms=timeout_ms) as client:
        with pytest.raises(ProviderFailure):
            async with client.chat_response({'model': 'fixture'}):
                pytest.fail('Error response accepted')
    assert handler.await_count == attempts


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type,attempts', [(httpcore.ConnectTimeout, 2), (httpx.ConnectTimeout, 2),
                                                 (httpcore.ReadTimeout, 1), (httpx.WriteError, 1)])
async def test_only_connect_timeouts_retry_before_headers(error_type, attempts):
    handler = AsyncMock(side_effect=error_type('network detail'))
    async with gateway(handler) as client:
        with pytest.raises(ProviderFailure):
            async with client.chat_response({'model': 'fixture'}):
                pytest.fail('Failed request accepted')
    assert handler.await_count == attempts


@pytest.mark.asyncio
async def test_probe_keeps_one_attempt_for_429():
    handler = AsyncMock(return_value=httpx.Response(429, headers={'Retry-After': '0'}))
    async with gateway(handler, probe=True) as client:
        with pytest.raises(ProviderFailure, match='Provider call did not complete'):
            await client.request('chat/completions', {'model': 'fixture'})
    assert handler.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_retry_wait_never_dispatches_again(monkeypatch):
    retry_admitted = asyncio.Event()
    monkeypatch.setattr('app.core.model_trace.record_discarded_chat_attempt', lambda *_: retry_admitted.set())
    handler = AsyncMock(return_value=httpx.Response(503, headers={'Retry-After': '2'}))
    async with gateway(handler, timeout_ms=10000) as client:
        async def invoke():
            async with client.chat_response({'model': 'fixture'}):
                pytest.fail('Unexpected successful response')

        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(retry_admitted.wait(), 5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert handler.await_count == 1


@pytest.mark.asyncio
async def test_retry_rechecks_revocation_before_second_dispatch(admin, monkeypatch):
    from app.model_config import runtime

    await publish(admin, await ready(admin, local=False))
    pinned = await snapshot(admin)
    credential_id = next(iter(pinned.credentials))

    class RevokeOnClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail('An HTTP error body must never be consumed')
            yield b''

        async def aclose(self):
            await revoke(admin, credential_id)

    permit = AsyncMock(wraps=runtime.permit)
    monkeypatch.setattr(runtime, 'permit', permit)
    requests = mock_runtime(admin, monkeypatch,
                            lambda _: httpx.Response(503, headers={'Retry-After': '0'}, stream=RevokeOnClose()))
    with selected(admin, pinned), pytest.raises(BusinessError) as error:
        await model_for('chat').ainvoke('fixture')
    assert error.value.code == 40926 and len(requests) == 1
    assert permit.await_count == 2
