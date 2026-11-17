"""LangChain chat model on the same guarded wire protocol used by probes."""
from __future__ import annotations

import asyncio
from contextlib import aclosing, asynccontextmanager
import json
import uuid
from app.core.failed_response import BusinessError

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage,AIMessageChunk,SystemMessage
from langchain_core.outputs import ChatResult,ChatGeneration,ChatGenerationChunk
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field,ConfigDict

from app.model_config.prompt_contracts import FIXED_SAFETY
from app.model_config.provider import BackupEligibleFailure,ProviderFailure,ROLE_THINKING,parse_json
from app.model_config.responses import decode_response, stream_parts
from app.model_config.snapshots import ReleaseSnapshot
from app.model_config.task_budget import ModelBudgetDenied
from app.model_config.context_budget import ModelContextExceeded, fit_chat_context
from app.rag.citations import ANSWER_ROLES, POLICY as CITATION_POLICY, current_citations


class BoundTrace(BaseCallbackHandler):
    def __init__(self,provenance,backup=None):
        self.provenance=provenance
        self.backup = backup

    def _call(self,method,*args,profile_id=None,price_at=None,memory=None,**kwargs):
        from app.core.model_trace import TRACE_CALLBACK,trace_scope
        context = self.provenance
        if self.backup and profile_id == self.backup['profile_revision_id']:
            context = {**context, **self.backup}
        if memory is not None:
            context = {**context, 'memory_context': memory}
        with trace_scope(**context, price_at=price_at):
            getattr(TRACE_CALLBACK,method)(*args,**kwargs)
    def on_chat_model_start(self,serialized,messages,**kwargs):
        self._call('on_llm_start',serialized,messages,**kwargs)
    def on_llm_end(self,response,**kwargs):
        metadata = {}
        if response.generations and response.generations[0]:
            message = getattr(response.generations[0][0], 'message', None)
            if message is not None:
                metadata = message.response_metadata
        self._call('on_llm_end',response,profile_id=metadata.get('raglearn_profile_revision_id'),
                   price_at=metadata.get('raglearn_price_at'),
                   memory=metadata.get('raglearn_memory_context'),**kwargs)
    def on_llm_error(self,error,**kwargs):
        if isinstance(error, ModelContextExceeded) or (isinstance(error, ModelBudgetDenied) and error.reported_usage is None):
            # LangChain starts the logical call before provider admission.
            from app.core.model_trace import TRACE_CALLBACK
            TRACE_CALLBACK.discard_start(**kwargs)
            return
        usage = None
        trace_error = ProviderFailure('PROVIDER_UNAVAILABLE')
        if isinstance(error, ModelBudgetDenied):
            prompt, completion = error.reported_usage
            usage = {'prompt_tokens': prompt, 'completion_tokens': completion, 'total_tokens': prompt + completion}
            trace_error = error
        self._call('on_llm_error',trace_error,usage=usage,
                   profile_id=getattr(error, 'raglearn_profile_revision_id', None),
                   memory=getattr(error, 'raglearn_memory_context', None),
                   price_at=getattr(error, 'raglearn_price_at', None), **kwargs)


def usage_metadata(value,native=False):
    usage={'prompt_tokens':value.get('prompt_eval_count'),'completion_tokens':value.get('eval_count')} if native else value.get('usage')
    if not isinstance(usage,dict):
        return None
    a,b=usage.get('prompt_tokens'),usage.get('completion_tokens')
    if any(type(number) is not int or number<0 for number in (a,b)):
        return None
    return {'input_tokens':a,'output_tokens':b,'total_tokens':a+b}


def wire_messages(messages,native):
    result=[]
    names={}
    for message in messages:
        role={'human':'user','ai':'assistant','system':'system','tool':'tool'}.get(message.type)
        if role is None:
            raise ProviderFailure('MESSAGE_ROLE_UNSUPPORTED')
        item={'role':role,'content':message.content}
        if native and isinstance(message.content,list):
            texts,images=[],[]
            for block in message.content:
                if block.get('type')=='text':
                    texts.append(block['text'])
                elif block.get('type')=='image_url':
                    image=block['image_url']['url']
                    if not image.startswith('data:image/') or ';base64,' not in image:
                        raise ProviderFailure('NATIVE_IMAGE_FORMAT_UNSUPPORTED')
                    images.append(image.split(';base64,',1)[1])
                else:
                    raise ProviderFailure('NATIVE_CONTENT_UNSUPPORTED')
            item['content']='\n'.join(texts)
            if images:item['images']=images
        if role=='assistant' and message.tool_calls:
            calls=[]
            for call in message.tool_calls:
                names[call['id']]=call['name']
                function={'name':call['name'],'arguments':call['args'] if native else json.dumps(call['args'],ensure_ascii=False)}
                calls.append({'function':function} if native else {'id':call['id'],'type':'function','function':function})
            item['tool_calls']=calls
        if role=='tool':
            item.update({'tool_name':getattr(message,'name',None) or names.get(message.tool_call_id,'')}
                        if native else {'tool_call_id':message.tool_call_id})
        result.append(item)
    return result


class GuardedChatModel(BaseChatModel):
    model_config=ConfigDict(arbitrary_types_allowed=True)
    snapshot: ReleaseSnapshot=Field(exclude=True,repr=False)
    role_key: str
    profile_id: str
    model_name: str
    backup_profile_id: str | None = None

    @property
    def _llm_type(self):return 'guarded-compatible'
    @property
    def _identifying_params(self):return {'model_name':self.model_name,'role_key':self.role_key}
    def _generate(self,*args,**kwargs):
        raise RuntimeError('Configured chat models require async invocation')
    def bind_tools(self,tools,*,tool_choice=None,**kwargs):
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools],
                         **({'tool_choice':tool_choice} if tool_choice is not None else {}))

    def _messages(self,messages):
        raw=self.snapshot.manifest['role_bindings_raw'][self.role_key]
        prompt=self.snapshot.prompts[raw['prompt_revision_id']]
        if self.role_key in ('chat','chat_thinking','vision','synthesize','planner','reflection') and not any(
            message.additional_kwargs.get('raglearn_context') for message in messages):
            from app.ai_service.context_composer import with_data
            messages=with_data(messages)
        turn = current_citations.get()
        policy = FIXED_SAFETY
        if turn and turn.sources and self.role_key in ANSWER_ROLES:
            policy += '\n' + CITATION_POLICY
        return [SystemMessage(content=policy),SystemMessage(content=prompt['system_text']),*messages]

    def _options(self,body,kwargs,native):
        if self.role_key == 'rag_query_expand':
            from app.rag.query_expansion import MAX_OUTPUT_TOKENS
            options, key = (body['options'], 'num_predict') if native else (body, 'max_tokens')
            options[key] = min(options[key], kwargs.get('max_output_tokens', MAX_OUTPUT_TOKENS), MAX_OUTPUT_TOKENS)
        if not native and kwargs.get('tool_choice') is not None:
            body['tool_choice']=kwargs['tool_choice']
        return body

    @asynccontextmanager
    async def _reply(self, messages, kwargs, *, stream=False):
        from app.model_config.runtime import gateway_for
        from app.model_config.probes import STRUCTURED_ROLES
        from app.model_config.prompt_contracts import output_contract
        from app.ai_service.context_composer import memory_provenance, unpack_data

        primary = self.snapshot.profiles[self.profile_id]
        expansion = self.role_key == 'rag_query_expand'
        if expansion and (stream or kwargs.get('tools') or self.backup_profile_id or len(messages) != 1
                          or messages[0].type != 'human' or messages[0].additional_kwargs
                          or not isinstance(messages[0].content, str)):
            raise ProviderFailure('EXPANSION_INPUT_INVALID')
        assembled = self._messages(messages)
        data_message = next((message for message in assembled if message.additional_kwargs.get('raglearn_context')), None)
        memory = memory_provenance(data_message) if data_message is not None else None
        schema = output_contract(self.role_key) if not stream and self.role_key in STRUCTURED_ROLES and not kwargs.get('tools') else None
        deadline = None
        if self.backup_profile_id:
            deadline = asyncio.get_running_loop().time() + primary['request_timeout_ms'] / 1000
        for identifier in (self.profile_id, self.backup_profile_id):
            profile = self.snapshot.profiles[identifier]
            native = profile['api_protocol'] == 'ollama_chat'
            backup = identifier != self.profile_id
            authorization = {'primary_profile_id': self.profile_id} if backup else {}
            delivered = False
            gateway = None
            fitted_memory = memory
            try:
                async with gateway_for(self.snapshot, identifier, **authorization) as gateway:
                    body = gateway.chat_body(wire_messages(assembled, native), stream=stream, tools=kwargs.get('tools'),
                                             thinking=ROLE_THINKING.get(self.role_key), structured=schema)
                    self._options(body, kwargs, native)
                    turn = current_citations.get()
                    evidence_wire = None
                    if (turn and turn.sources and data_message is not None
                            and (unpack_data(data_message) or {}).get('retrieval') == turn.context):
                        index = next(i for i, message in enumerate(assembled) if message is data_message)
                        evidence_wire = body['messages'][index]
                    memory_omitted = fit_chat_context(profile, body, assembled)
                    included = evidence_wire is not None and any(item is evidence_wire for item in body['messages'])
                    if memory_omitted and memory:
                        fitted_memory = {**memory, 'omitted': True, 'truncated': True, 'selected_hash': None}
                    async with gateway.chat_response(body, deadline=deadline, retry=not expansion and self.backup_profile_id is None,
                                                     switch_to_backup=bool(self.backup_profile_id and not backup)) as response:
                        delivered = True
                        if turn is not None and self.role_key in ANSWER_ROLES:
                            turn.delivered = included
                        yield response, profile['api_protocol'], {'raglearn_profile_revision_id': identifier,
                            'raglearn_price_at': gateway.chat_price_at,
                            'raglearn_memory_context': fitted_memory}, gateway.chat_reservation
                        return
            except BackupEligibleFailure:
                if delivered or backup or not self.backup_profile_id:
                    raise
            except (ProviderFailure, BusinessError) as error:
                error.raglearn_profile_revision_id = identifier
                error.raglearn_price_at = getattr(gateway, 'chat_price_at', None)
                error.raglearn_memory_context = fitted_memory
                raise

    async def _agenerate(self,messages,stop=None,run_manager=None,**kwargs):
        async with self._reply(messages, kwargs) as (wire_response, protocol, metadata, reservation):
            response = parse_json(await wire_response.aread())
            native = protocol == 'ollama_chat'
            if protocol == 'responses':
                response = decode_response(response, tools_allowed=bool(kwargs.get('tools')))
            usage = usage_metadata(response,native) if isinstance(response,dict) else None
            if reservation is not None and usage is not None:
                await reservation.settle(usage['input_tokens'],usage['output_tokens'])
            try:
                message=response['message'] if native else response['choices'][0]['message']
                calls=[]
                for call in message.get('tool_calls',[]):
                    function=call['function']
                    arguments=function['arguments'] if native else parse_json(function['arguments'])
                    if not isinstance(arguments,dict):raise ValueError
                    calls.append({'id':call.get('id') or str(uuid.uuid4()),'name':function['name'],
                                  'args':arguments,'type':'tool_call'})
                ai=AIMessage(content=message.get('content') or '',tool_calls=calls,
                             usage_metadata=usage,
                             response_metadata=metadata)
                if calls and not kwargs.get('tools'):
                    raise ValueError
                if not calls:
                    from app.model_config.probes import STRUCTURED_ROLES
                    if self.role_key in STRUCTURED_ROLES:
                        from app.model_config.prompt_contracts import output_contract
                        from jsonschema import Draft202012Validator
                        value=parse_json(ai.content)
                        if not Draft202012Validator(output_contract(self.role_key)).is_valid(value):
                            raise ValueError
                return ChatResult(generations=[ChatGeneration(message=ai)])
            except (KeyError,IndexError,TypeError,ValueError):
                raise ProviderFailure('PROVIDER_RESPONSE_INVALID') from None

    async def _astream(self,messages,stop=None,run_manager=None,**kwargs):
        try:
            async with self._reply(messages, kwargs, stream=True) as (response, protocol, metadata, reservation):
                chunks = (self._consume_responses(response, metadata, reservation, tools_allowed=bool(kwargs.get('tools')))
                          if protocol == 'responses' else self._consume_stream(response, protocol == 'ollama_chat', metadata, reservation))
                async with aclosing(chunks):
                    async for chunk in chunks:
                        yield chunk
        except (ProviderFailure,BusinessError):
            raise
        except Exception:
            raise ProviderFailure('PROVIDER_UNAVAILABLE') from None

    async def _consume_responses(self, response, metadata, reservation, *, tools_allowed):
        text_seen = False
        async with aclosing(stream_parts(response, tools_allowed=tools_allowed)) as parts:
            async for text, final in parts:
                if final is None:
                    if text:
                        yield ChatGenerationChunk(message=AIMessageChunk(content=text, response_metadata=metadata))
                        text_seen, metadata = True, {}
                    continue
                usage = usage_metadata(final)
                if usage is not None and reservation is not None:
                    await reservation.settle(usage['input_tokens'], usage['output_tokens'])
                message = final['choices'][0]['message']
                calls = [{'index': index, 'id': call['id'], 'name': call['function']['name'],
                          'args': call['function']['arguments'], 'type': 'tool_call_chunk'}
                         for index, call in enumerate(message['tool_calls'])]
                yield ChatGenerationChunk(message=AIMessageChunk(content='' if text_seen else message['content'],
                                           tool_call_chunks=calls, response_metadata=metadata, usage_metadata=usage))

    async def _consume_stream(self, response, native, metadata, reservation):
        done = seen = False
        tool_ids = {}
        final_usage = None
        async for line in response.aiter_lines():
            if not line.strip() or line.startswith((':','event:','id:','retry:')):
                continue
            if not native:
                if not line.startswith('data:'):
                    raise ProviderFailure('PROVIDER_STREAM_INVALID')
                line = line[5:].strip()
                if line == '[DONE]':
                    done = True
                    break
            item = parse_json(line)
            if not isinstance(item, dict) or item.get('error'):
                raise ProviderFailure('PROVIDER_STREAM_INVALID')
            choices = item.get('choices', [])
            delta = item.get('message', {}) if native else choices[0].get('delta', {}) if choices else {}
            chunks = []
            for index, call in enumerate(delta.get('tool_calls', [])):
                function = call.get('function', {})
                position = call.get('index', index)
                ident = tool_ids.setdefault(position, str(uuid.uuid4())) if native else call.get('id')
                arguments = function.get('arguments', {})
                chunks.append({'index': position, 'id': ident, 'name': function.get('name'),
                               'args': json.dumps(arguments, ensure_ascii=False) if native else function.get('arguments', ''),
                               'type': 'tool_call_chunk'})
            content = delta.get('content') or ''
            usage = usage_metadata(item, native)
            if usage is not None:
                final_usage = usage
            if content or chunks:
                seen = True
            if content or chunks:
                yield ChatGenerationChunk(message=AIMessageChunk(content=content, tool_call_chunks=chunks,
                                           response_metadata=metadata))
                metadata = {}
            if native and item.get('done') is True:
                done = True
                break
        if done and final_usage is not None and reservation is not None:
            await reservation.settle(final_usage['input_tokens'],final_usage['output_tokens'])
        if not done or not seen:
            raise ProviderFailure('PROVIDER_STREAM_INCOMPLETE')
        if final_usage is not None:
            yield ChatGenerationChunk(message=AIMessageChunk(content='', usage_metadata=final_usage))
