"""Translate the explicit Responses protocol at the provider boundary."""
from app.model_config.provider import ProviderFailure, parse_json


def request_body(body):
    messages = body['messages']
    items = []
    for message in messages:
        role, content = message['role'], message['content']
        if role == 'tool':
            if not isinstance(content, str):
                raise ProviderFailure('RESPONSES_TOOL_OUTPUT_UNSUPPORTED')
            items.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': content})
            continue
        if isinstance(content, list):
            parts = []
            for part in content:
                if part['type'] == 'text':
                    if role == 'assistant':
                        parts.append({'type': 'output_text', 'text': part['text'], 'annotations': []})
                    else:
                        parts.append({'type': 'input_text', 'text': part['text']})
                elif part['type'] == 'image_url' and role == 'user':
                    image = part['image_url']
                    parts.append({'type': 'input_image', 'image_url': image['url'], 'detail': image.get('detail', 'auto')})
                else:
                    raise ProviderFailure('RESPONSES_CONTENT_UNSUPPORTED')
            content = parts
        if content or not message.get('tool_calls'):
            items.append({'role': role, 'content': content})
        for call in message.get('tool_calls', []):
            items.append({'type': 'function_call', 'call_id': call['id'], **call['function']})

    result = {key: body[key] for key in ('model', 'stream', 'temperature', 'top_p') if key in body}
    result.update(input=items, max_output_tokens=body['max_tokens'], store=False)
    if 'reasoning_effort' in body:
        result['reasoning'] = {'effort': body['reasoning_effort']}
    if 'tools' in body:
        result['tools'] = [{'type': 'function', **tool['function'], 'strict': tool['function'].get('strict', False)}
                           for tool in body['tools']]
    if 'tool_choice' in body:
        choice = body['tool_choice']
        result['tool_choice'] = {'type': 'function', 'name': choice['function']['name']} if isinstance(choice, dict) else choice
    if 'response_format' in body:
        result['text'] = {'format': {'type': 'json_schema', **body['response_format']['json_schema']}}
    return result


def decode_response(value, *, tools_allowed):
    """Use the gateway's existing message/usage contract for probes and runtime."""
    try:
        if value['status'] != 'completed' or value.get('error') or not isinstance(value['output'], list):
            raise ValueError
        parts, calls, call_ids = [], [], set()
        for item in value['output']:
            kind = item['type']
            if kind == 'reasoning':
                continue
            if kind == 'message' and item.get('role') == 'assistant':
                for part in item['content']:
                    if part['type'] == 'refusal':
                        raise ProviderFailure('PROVIDER_RESPONSE_REFUSED')
                    if part['type'] != 'output_text' or not isinstance(part['text'], str):
                        raise ValueError
                    parts.append(part['text'])
                continue
            if kind != 'function_call' or not tools_allowed:
                raise ValueError
            ident, name, arguments = item['call_id'], item['name'], item['arguments']
            if (not isinstance(ident, str) or not ident or ident in call_ids or not isinstance(name, str)
                    or not name or not isinstance(arguments, str) or not isinstance(parse_json(arguments), dict)):
                raise ValueError
            call_ids.add(ident)
            calls.append({'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': arguments}})
        content = ''.join(parts)
        if not content and not calls:
            raise ValueError
        usage = value.get('usage')
        normalized_usage = {'prompt_tokens': usage.get('input_tokens'),
                            'completion_tokens': usage.get('output_tokens')} if isinstance(usage, dict) else None
        return {'choices': [{'message': {'content': content, 'tool_calls': calls}}], 'usage': normalized_usage}
    except (KeyError, TypeError, ValueError):
        raise ProviderFailure('PROVIDER_RESPONSE_INVALID') from None


async def stream_parts(response, *, tools_allowed):
    """Yield public text and completed function calls. Never forward raw events."""
    async for line in response.aiter_lines():
        if not line.strip() or line.startswith((':', 'event:', 'id:', 'retry:')):
            continue
        if not line.startswith('data:'):
            raise ProviderFailure('PROVIDER_STREAM_INVALID')
        event = parse_json(line[5:].strip())
        if not isinstance(event, dict):
            raise ProviderFailure('PROVIDER_STREAM_INVALID')
        kind = event.get('type')
        if kind == 'response.output_text.delta':
            if not isinstance(event.get('delta'), str):
                raise ProviderFailure('PROVIDER_STREAM_INVALID')
            yield event['delta'], None
        elif kind == 'response.completed':
            yield None, decode_response(event.get('response'), tools_allowed=tools_allowed)
            return
        elif kind in ('error', 'response.failed', 'response.incomplete', 'response.refusal.delta', 'response.refusal.done'):
            raise ProviderFailure('PROVIDER_STREAM_INVALID')
    raise ProviderFailure('PROVIDER_STREAM_INCOMPLETE')
