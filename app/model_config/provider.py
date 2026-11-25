"""Credential-local provider gateway with explicit retry rules for runtime chat."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
import time

import httpcore
import httpx

from app.model_config.errors import fail
from app.model_config.transport import GuardedTransport
from app.model_config.credential_calls import CredentialTransport
from app.model_config.task_budget import current_model_budget
from app.core.failed_response import BusinessError

ADAPTER_VERSION = 'guarded-http-v1'
RETRYABLE_CHAT_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_WAIT_SECONDS = 2
CONNECT_TIMEOUTS = (httpcore.ConnectTimeout, httpx.ConnectTimeout)
NETWORK_ERRORS = (httpcore.NetworkError, httpcore.TimeoutException, httpcore.ProtocolError,
                  httpx.HTTPError, TimeoutError, OSError)
CHAT_PATHS = {'chat_completions': 'chat/completions', 'ollama_chat': 'api/chat', 'responses': 'responses'}
ROLE_THINKING = {'chat_thinking': True, 'rag_query_expand': False}


class ProviderFailure(Exception):
    def __init__(self,reason):
        self.reason = reason
        super().__init__('Provider call did not complete')


class BackupEligibleFailure(ProviderFailure):
    """The retry window is open and no successful response body was consumed."""


def parse_json(value):
    def invalid(_):
        raise ValueError
    try:
        return json.loads(value,parse_constant=invalid)
    except (ValueError,UnicodeError,TypeError):
        raise ProviderFailure('PROVIDER_RESPONSE_INVALID') from None


def retry_delay(value: str | None) -> float | None:
    if value is None:
        return 0.2
    value = value.strip()
    if not value or len(value) > 64:
        return None
    if re.fullmatch(r'[0-9]+', value):
        return float(int(value))
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def require_success(response):
    if response.status_code != 200:
        reason = {401:'PROVIDER_AUTH_FAILED',403:'PROVIDER_AUTH_FAILED',
                  429:'PROVIDER_RATE_LIMITED'}.get(response.status_code,'PROVIDER_HTTP_ERROR')
        raise ProviderFailure(reason)
    if response.headers.get('content-encoding','identity').lower() not in ('identity',''):
        raise ProviderFailure('PROVIDER_CONTENT_ENCODING_DENIED')


class ProviderGateway:
    def __init__(self,profile,policy,api_key,*,permit,transport=None,probe_mode=True):
        self.profile = profile
        self.probe_mode = probe_mode
        headers = {'Accept':'application/json','Content-Type':'application/json',
                   'Accept-Encoding':'identity'}
        if profile['auth_mode']!='none':
            if not api_key or any(char in api_key for char in ('\r','\n')):
                fail(40002,'INVALID_CREDENTIAL_FORMAT',422)
            headers['Authorization'] = 'Bearer '+api_key
        inner=transport or GuardedTransport(policy,permit=permit)
        credential_id=profile.get('credential_revision_id')
        if credential_id:inner=CredentialTransport(inner,credential_id)
        self.client = httpx.AsyncClient(transport=inner,
            headers=headers,follow_redirects=False,trust_env=False,
            timeout=httpx.Timeout(profile['request_timeout_ms']/1000,
                                  connect=profile['connect_timeout_ms']/1000))
        self.calls = 0
        self.trace_context = {}
        self.budget_session_factory = None
        self.chat_reservation = None

    async def _reserve_attempt(self, price_at, *, output_limit, input_count):
        budget = current_model_budget.get()
        if self.probe_mode or budget is None:
            return None
        session_factory = self.budget_session_factory
        if session_factory is None:
            from app.db.database import async_session_factory
            session_factory = async_session_factory
        return await budget.reserve(self.profile, price_at, session_factory, output_limit=output_limit,
                                    input_count=input_count)

    def url(self,path):
        return self.profile['base_url'].rstrip('/')+'/'+path

    async def __aenter__(self):
        return self

    async def __aexit__(self,*args):
        await self.client.aclose()

    async def _open_chat(self, path, body, *, deadline, retry, switch_to_backup):
        from app.core.model_trace import price_time, record_discarded_chat_attempt

        loop = asyncio.get_running_loop()
        retry_allowed = not self.probe_mode and (retry or switch_to_backup)
        while True:
            if loop.time() >= deadline:
                raise ProviderFailure('PROVIDER_UNAVAILABLE')
            started = loop.time()
            self.chat_price_at = None
            self.chat_reservation = None
            response = None
            try:
                async with asyncio.timeout_at(deadline):
                    attempt_at = price_time()
                    output_limit = (body['options']['num_predict'] if self.profile['api_protocol'] == 'ollama_chat'
                                    else body.get('max_output_tokens', body.get('max_tokens', 2048)))
                    self.chat_reservation = await self._reserve_attempt(attempt_at, output_limit=output_limit, input_count=1)
                    self.chat_price_at = attempt_at
                    request = self.client.build_request('POST', self.url(path), json=body)
                    response = await self.client.send(request, stream=True)
            except CONNECT_TIMEOUTS:
                reason, delay = 'PROVIDER_CONNECT_TIMEOUT', retry_delay(None)
            else:
                if response.status_code not in RETRYABLE_CHAT_STATUS:
                    return response
                reason = 'PROVIDER_RATE_LIMITED' if response.status_code == 429 else 'PROVIDER_HTTP_ERROR'
                delay = retry_delay(response.headers.get('retry-after'))

            if (not retry_allowed or delay is None or delay > MAX_RETRY_WAIT_SECONDS
                    or loop.time() + delay >= deadline):
                if response is not None:
                    return response
                raise ProviderFailure(reason) from None
            if response is not None:
                await response.aclose()
            record_discarded_chat_attempt(self.trace_context, self.profile, reason,
                                          round((loop.time() - started) * 1000), self.chat_price_at)
            retry_allowed = False
            async with asyncio.timeout_at(deadline):
                await asyncio.sleep(delay)
            if switch_to_backup:
                raise BackupEligibleFailure(reason) from None

    @asynccontextmanager
    async def chat_response(self, body, *, deadline=None, retry=True, switch_to_backup=False):
        """Select a response before parsing its body. The caller consumes it once."""
        protocol = self.profile['api_protocol']
        if protocol not in CHAT_PATHS:
            raise ProviderFailure('PROTOCOL_NOT_IMPLEMENTED')
        if protocol == 'responses':
            from app.model_config.responses import request_body
            body = request_body(body)
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + self.profile['request_timeout_ms'] / 1000
        response = None
        try:
            response = await self._open_chat(CHAT_PATHS[protocol], body, deadline=deadline, retry=retry,
                                             switch_to_backup=switch_to_backup)
            require_success(response)
            yield response
        except NETWORK_ERRORS:
            raise ProviderFailure('PROVIDER_UNAVAILABLE') from None
        finally:
            if response is not None:
                await response.aclose()

    async def request(self,path,body,*,stream=False,method='POST',embedding_count=None):
        from app.core.model_trace import embedding_prompt_tokens, price_time

        self.calls += 1
        if self.calls>3:
            raise ProviderFailure('PROBE_CALL_BUDGET_EXCEEDED')
        started = time.monotonic()
        self.request_price_at = None
        attempt_at = price_time()
        reservation = None
        if embedding_count is not None:
            reservation = await self._reserve_attempt(attempt_at, output_limit=0, input_count=embedding_count)
        self.request_price_at = attempt_at
        outcome,usage,error = False,None,'PROVIDER_UNAVAILABLE'
        try:
            async with self.client.stream(method,self.url(path),json=body) as response:
                require_success(response)
                if path == 'responses' and self.profile['api_protocol'] == 'responses':
                    from app.model_config.responses import decode_response, stream_parts
                    tools_allowed = bool(body.get('tools'))
                    if stream:
                        async for _, result in stream_parts(response, tools_allowed=tools_allowed):
                            if result is not None:
                                value = {'text': result['choices'][0]['message']['content'], 'usage': result['usage']}
                        if not value['text'].strip():
                            raise ProviderFailure('PROVIDER_STREAM_INCOMPLETE')
                    else:
                        value = decode_response(parse_json(await response.aread()), tools_allowed=tools_allowed)
                    usage = value.get('usage')
                elif stream:
                    value = await self._stream(response)
                    usage = value.get('usage')
                else:
                    value = parse_json(await response.aread())
                    if isinstance(value,dict):
                        usage = {'prompt_tokens':value.get('prompt_eval_count'),
                            'completion_tokens':value.get('eval_count')} if self.profile['adapter_type']=='ollama_native' else value.get('usage')
                if reservation is not None:
                    await reservation.settle(embedding_prompt_tokens(value,self.profile['api_protocol']),0)
                outcome,error = True,None
                return value
        except NETWORK_ERRORS:
            raise ProviderFailure('PROVIDER_UNAVAILABLE') from None
        except ProviderFailure as failure:
            error = failure.reason
            raise
        except BusinessError as failure:
            error=(failure.data or {}).get('reason','DISPATCH_DENIED')
            raise
        finally:
            if self.probe_mode:
                from app.core.model_trace import record_config_test_call
                record_config_test_call(self.trace_context,self.profile,success=outcome,usage=usage,
                    latency_ms=round((time.monotonic()-started)*1000),error=error,price_at=self.request_price_at)

    async def _stream(self,response):
        ollama = self.profile['api_protocol']=='ollama_chat'
        text,done,usage = '',False,None
        async for line in response.aiter_lines():
            if not line.strip():
                continue
            if not ollama:
                if line.startswith(':') or line.startswith(('event:','id:','retry:')):
                    continue
                if not line.startswith('data:'):
                    raise ProviderFailure('PROVIDER_STREAM_INVALID')
                line = line[5:].strip()
                if line=='[DONE]':
                    done = True
                    break
            item = parse_json(line)
            if not isinstance(item,dict) or item.get('error'):
                raise ProviderFailure('PROVIDER_STREAM_INVALID')
            if ollama:
                message = item.get('message',{})
                text += message.get('content','')
                done = item.get('done') is True
                if done:
                    usage = {'prompt_tokens':item.get('prompt_eval_count'),
                             'completion_tokens':item.get('eval_count')}
                    break
            else:
                choices = item.get('choices',[])
                if choices:
                    text += choices[0].get('delta',{}).get('content') or ''
                if item.get('usage'):
                    usage = item['usage']
        if not done or not text.strip():
            raise ProviderFailure('PROVIDER_STREAM_INCOMPLETE')
        return {'text':text,'usage':usage}

    def chat_body(self,messages,*,stream=False,tools=None,structured=None,thinking=None):
        profile = self.profile
        native = profile['api_protocol']=='ollama_chat'
        parameters = dict(profile['parameters'])
        body = {'model':profile['model'],'messages':messages,'stream':stream}
        limit = min(96,profile['max_output_tokens'] or 96) if self.probe_mode else profile['max_output_tokens'] or 2048
        if native:
            body['options'] = {**parameters,'num_predict':limit}
            if thinking is not None:
                body['think'] = thinking
        else:
            body.update(parameters,max_tokens=limit)
            if stream:
                body['stream_options'] = {'include_usage':True}
            if thinking:
                if profile['vendor']!='dashscope' or profile['api_protocol'] == 'responses':
                    raise ProviderFailure('THINKING_CONTROL_UNVERIFIED')
                body['enable_thinking'] = True
            elif thinking is False and profile['vendor'] == 'ollama':
                body['reasoning_effort'] = 'none'
        if tools:
            body['tools'] = tools
            if not native and self.probe_mode:
                body['tool_choice'] = {'type':'function','function':{'name':'echo_test'}}
        if structured:
            if native:
                body['format'] = structured
            else:
                body['response_format'] = {'type':'json_schema','json_schema':{
                    'name':'probe_result','strict':True,'schema':structured}}
        return body

    async def chat(self,messages,*,stream=False,tools=None,structured=None,thinking=None):
        protocol = self.profile['api_protocol']
        if protocol not in CHAT_PATHS:
            raise ProviderFailure('PROTOCOL_PROBE_NOT_IMPLEMENTED')
        body = self.chat_body(messages,stream=stream,tools=tools,structured=structured,thinking=thinking)
        if protocol == 'responses':
            from app.model_config.responses import request_body
            body = request_body(body)
        return await self.request(CHAT_PATHS[protocol], body, stream=stream)

    async def embed(self,texts,*,purpose='query'):
        profile = self.profile
        if not self.probe_mode:
            from app.model_config.context_budget import check_embedding_context
            check_embedding_context(profile,texts)
        protocol = profile['api_protocol']
        if protocol=='openai_embeddings':
            body = {'model':profile['model'],'input':texts,'encoding_format':'float'}
            if profile['embedding_dimension']:
                body['dimensions'] = profile['embedding_dimension']
            return await self.request('embeddings',body,embedding_count=len(texts))
        if protocol=='ollama_embed':
            body = {'model':profile['model'],'input':texts,'truncate':(profile.get('semantic_identity') or {}).get('parameters',{}).get('truncate',False)}
            if profile['embedding_dimension']:
                body['dimensions'] = profile['embedding_dimension']
            return await self.request('api/embed',body,embedding_count=len(texts))
        if protocol=='dashscope_embed':
            # Keep the native protocol and explicit query semantic. No SDK global
            # api_key or base address mutation, and no cross-protocol index swap.
            body = {'model':profile['model'],'input':{'texts':texts},'parameters':{'text_type':'query' if purpose=='query' else 'document'}}
            if profile['embedding_dimension']:
                body['parameters']['dimension'] = profile['embedding_dimension']
            from urllib.parse import urlsplit
            prefix='api/v1/' if not urlsplit(profile['base_url']).path.strip('/') else ''
            return await self.request(prefix+'services/embeddings/text-embedding/text-embedding',body,embedding_count=len(texts))
        raise ProviderFailure('PROTOCOL_PROBE_NOT_IMPLEMENTED')

    async def verify_embedding_revision(self):
        if self.profile['api_protocol']!='ollama_embed':return
        expected=(self.profile.get('model_revision') or '').removeprefix('sha256:')
        if not re.fullmatch(r'[a-f0-9]{64}',expected):raise ProviderFailure('OLLAMA_DIGEST_UNVERIFIED')
        response=await self.request('api/tags',None,method='GET')
        names={self.profile['model'],self.profile['model']+':latest'}
        try:
            if not isinstance(response['models'],list) or any(not isinstance(item,dict) for item in response['models']):raise ValueError
            matches=[item for item in response['models'] if item.get('name') in names or item.get('model') in names]
            if len(matches)!=1 or matches[0].get('digest')!=expected:raise ValueError
        except (KeyError,TypeError,ValueError):raise ProviderFailure('OLLAMA_MODEL_DIGEST_CHANGED') from None
