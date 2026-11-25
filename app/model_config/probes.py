"""Role-specific capability suite, at most three provider requests per run."""
from __future__ import annotations

import json
import math
import secrets

from jsonschema import Draft202012Validator

from app.model_config.provider import ProviderFailure, ROLE_THINKING

PROBE_SUITE_VERSION = 'role-probes-v2'
EMBEDDING_PROBE_SUITE_VERSION='embedding-probes-v3'
STRUCTURED_ROLES = frozenset(('classifier','planner','ppt_outline','auto_tag','review_question',
                             'reflection','memory_extraction','rag_query_expand'))
TOOL_ROLES = frozenset(('chat','chat_thinking','vision'))
STRUCTURED_SCHEMA = {'type':'object','properties':{'ok':{'const':True},'label':{'const':'probe'}},
                     'required':['ok','label'],'additionalProperties':False}
PROBE_IMAGE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6dZsAAAAASUVORK5CYII='


def assistant_message(response, native):
    try:
        message = response['message'] if native else response['choices'][0]['message']
        if not isinstance(message,dict):
            raise ValueError
        return message
    except (KeyError,IndexError,TypeError,ValueError):
        raise ProviderFailure('PROVIDER_RESPONSE_INVALID') from None


def bounded_usage(usage):
    if not isinstance(usage,dict):
        return None
    names = ('prompt_tokens','completion_tokens')
    if any(isinstance(usage.get(key),bool) or not isinstance(usage.get(key),int) or usage[key]<0 for key in names):
        return None
    return {key:usage[key] for key in names}


async def run_suite(gateway,role):
    checks,usage = [],[]
    native = gateway.profile['adapter_type']=='ollama_native'
    def supported(name):
        checks.append({'capability':name,'status':'supported'})
    try:
        if role=='embedding':
            if gateway.profile.get('semantic_fingerprint') and gateway.profile['api_protocol']=='ollama_embed':
                await gateway.verify_embedding_revision()
            response = await gateway.embed(['Capability probe first.','Capability probe second.'])
            if gateway.profile.get('semantic_fingerprint') and gateway.profile['api_protocol']=='ollama_embed':
                await gateway.verify_embedding_revision()
            protocol = gateway.profile['api_protocol']
            if protocol=='ollama_embed':
                vectors = response['embeddings']
            else:
                data = response['output']['embeddings'] if protocol=='dashscope_embed' else response['data']
                key = 'text_index' if protocol=='dashscope_embed' else 'index'
                if len(data)!=2 or any(type(item[key]) is not int for item in data) or sorted(item[key] for item in data) != [0,1]:
                    raise ProviderFailure('EMBEDDING_ORDER_INVALID')
                vectors = [item['embedding'] for item in sorted(data,key=lambda item:item[key])]
            if (not isinstance(vectors,list) or len(vectors)!=2 or not vectors[0]
                    or len(vectors[0])!=len(vectors[1])):
                raise ProviderFailure('EMBEDDING_SHAPE_INVALID')
            dimension = len(vectors[0])
            if gateway.profile['embedding_dimension'] and dimension!=gateway.profile['embedding_dimension']:
                raise ProviderFailure('EMBEDDING_DIMENSION_MISMATCH')
            if any(isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number)
                   for vector in vectors for number in vector):
                raise ProviderFailure('EMBEDDING_VALUES_INVALID')
            if any(not any(number!=0 for number in vector) for vector in vectors):
                raise ProviderFailure('EMBEDDING_VALUES_INVALID')
            supported('embedding')
            return {'status':'passed','checks':checks,'dimension':dimension,'call_count':gateway.calls,
                    'usage':None,'semantic_identity_status':'unverified'}
        if role in STRUCTURED_ROLES:
            from app.model_config.prompt_contracts import output_contract,probe_fixture
            schema=output_contract(role)
            response = await gateway.chat([{'role':'user','content':'Return this fixed test JSON exactly: '+
                json.dumps(probe_fixture(role),ensure_ascii=False)}],structured=schema,thinking=ROLE_THINKING.get(role))
            message = assistant_message(response,native)
            value = json.loads(message.get('content',''))
            if not Draft202012Validator(schema).is_valid(value):
                raise ProviderFailure('STRUCTURED_OUTPUT_INVALID')
            supported('structured_output')
            if native:
                usage.append(bounded_usage({'prompt_tokens':response.get('prompt_eval_count'),
                                            'completion_tokens':response.get('eval_count')}))
            else:
                usage.append(bounded_usage(response.get('usage')))
        else:
            content = 'Reply with a short capability probe acknowledgement.'
            if role=='vision':
                content = 'Briefly describe this test image.' if native else [
                    {'type':'text','text':'Briefly describe this test image.'},
                    {'type':'image_url','image_url':{'url':PROBE_IMAGE}}]
            message = {'role':'user','content':content}
            if role=='vision' and native:
                message['images'] = [PROBE_IMAGE.split(',',1)[1]]
            response = await gateway.chat([message],stream=True,thinking=ROLE_THINKING.get(role))
            usage.append(bounded_usage(response.get('usage')))
            supported('text_stream')
            if role=='vision':
                supported('image_input')
            if role=='chat_thinking':
                supported('thinking_control')
        if role in TOOL_ROLES:
            nonce = secrets.token_hex(8)
            schema = {'type':'object','properties':{'nonce':{'type':'string'}},
                      'required':['nonce'],'additionalProperties':False}
            tools = [{'type':'function','function':{'name':'echo_test',
                     'description':'Echo the exact test nonce locally.','parameters':schema}}]
            messages = [{'role':'user','content':'Call echo_test with nonce '+nonce+'. Then repeat the result.'}]
            response = await gateway.chat(messages,tools=tools)
            message = assistant_message(response,native)
            calls = message.get('tool_calls',[])
            if len(calls)!=1:
                raise ProviderFailure('TOOL_CALL_MISSING')
            call = calls[0]
            function = call.get('function',{})
            arguments = function.get('arguments')
            if not native:
                arguments = json.loads(arguments)
                if not isinstance(call.get('id'),str) or not call['id']:
                    raise ProviderFailure('TOOL_CALL_ID_MISSING')
            if function.get('name')!='echo_test' or arguments!={'nonce':nonce}:
                raise ProviderFailure('TOOL_CALL_INVALID')
            # Execute only this hard-coded echo, never application tools.
            followup = {'role':'tool','content':nonce}
            followup.update({'tool_name':'echo_test'} if native else {'tool_call_id':call['id']})
            clean = {'role':'assistant','content':message.get('content') or '', 'tool_calls':calls}
            response = await gateway.chat([*messages,clean,followup])
            answer = assistant_message(response,native).get('content','')
            if not isinstance(answer,str) or nonce not in answer:
                raise ProviderFailure('TOOL_RESULT_NOT_USED')
            # Two non-stream tool calls may lack usage; never report missing as zero.
            usage.extend([None,None])
            supported('tool_roundtrip')
        known = [item for item in usage if item is not None]
        return {'status':'passed','checks':checks,'call_count':gateway.calls,'role_schema_verified':role in STRUCTURED_ROLES,
            'usage':{'prompt_tokens':sum(item['prompt_tokens'] for item in known),
                     'completion_tokens':sum(item['completion_tokens'] for item in known),
                     'status':'complete' if len(known)==len(usage) else 'partial'} if known else None}
    except ProviderFailure as error:
        return {'status':'unverified','checks':checks,'reason':error.reason,
                'call_count':gateway.calls,'usage':None}
    except (KeyError,IndexError,TypeError,ValueError,AttributeError):
        return {'status':'failed','checks':checks,'reason':'CAPABILITY_RESPONSE_INVALID',
                'call_count':gateway.calls,'usage':None}
