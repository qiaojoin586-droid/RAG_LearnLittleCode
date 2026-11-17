"""Admission against the dispatched profile's declared context window."""

from functools import cache
import hashlib
import json
import os
from pathlib import Path
import tempfile

from app.core.failed_response import BusinessError, ErrorCode


VOCAB_HASHES = {
    'cl100k_base': '223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7',
    'o200k_base': '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d',
}
IMAGE_RESERVE = 4096
VIDEO_RESERVE = 32768


class ModelContextExceeded(BusinessError):
    def __init__(self, window, estimated_total):
        super().__init__(ErrorCode.MODEL_CONTEXT_EXCEEDED, http_status=413, data={
            'reason': 'MODEL_CONTEXT_LIMIT_EXCEEDED', 'context_window': window,
            'estimated_total_tokens': estimated_total,
        })

    def stream_event(self):
        return {'type': 'error', 'code': self.code, **self.data, 'retryable': False, 'content': self.message}


@cache
def local_encoder(identifier, directory):
    import tiktoken

    url = f'https://openaipublic.blob.core.windows.net/encodings/{identifier}.tiktoken'
    name = hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest()
    raw = (Path(directory) / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != VOCAB_HASHES[identifier]:
        raise ValueError('Tokenizer vocabulary checksum mismatch')
    return tiktoken.get_encoding(identifier)


def text_counter(profile):
    identifier = profile.get('tokenizer_id')
    checksum = VOCAB_HASHES.get(identifier)
    if profile.get('token_counting_policy', {}).get('mode') == 'exact' and checksum:
        revision = profile.get('tokenizer_revision')
        directory = os.environ.get('TIKTOKEN_CACHE_DIR', os.environ.get(
            'DATA_GYM_CACHE_DIR', str(Path(tempfile.gettempdir()) / 'data-gym-cache')))
        if directory and revision in (None, 'sha256:' + checksum):
            try:
                encoder = local_encoder(identifier, directory)
                return lambda text: len(encoder.encode_ordinary(text))
            except (ImportError, OSError, ValueError):
                pass
    return lambda text: len(text.encode('utf-8'))


def serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def message_cost(message, count):
    envelope = dict(message)
    media = IMAGE_RESERVE * len(envelope.pop('images', ()))
    content = envelope.get('content')
    if isinstance(content, list):
        blocks = []
        for block in content:
            kind = block.get('type') if isinstance(block, dict) else None
            if kind == 'image_url':
                media += IMAGE_RESERVE
            elif kind == 'video_url':
                media += VIDEO_RESERVE
            else:
                blocks.append(block)
        envelope['content'] = blocks
    return 8 + media + count(serialize(envelope))


def history_groups(messages):
    group = []
    for index, message in enumerate(messages):
        marker = message.additional_kwargs.get('raglearn_history')
        if marker != 'turn' or message.type == 'human':
            if group:
                yield group
                group = []
        if marker == 'turn':
            group.append(index)
    if group:
        yield group


def fit_chat_context(profile, body, messages):
    """Adjust optional context in place and report whether personal memory was dropped."""
    window = profile.get('context_window')
    if window is None:
        return False

    from app.ai_service.context_composer import unpack_data, encode_data

    count = text_counter(profile)
    wire = body['messages']
    native = profile['api_protocol'] == 'ollama_chat'
    output_limit = body['options']['num_predict'] if native else body['max_tokens']
    auxiliary = {key: body[key] for key in ('tools', 'tool_choice', 'response_format', 'format') if key in body}
    fixed = output_limit + max(32, (window + 19) // 20) + count(serialize(auxiliary))
    costs = {index: message_cost(message, count) for index, message in enumerate(wire)}
    total = fixed + sum(costs.values())
    memory_omitted = False

    def discard(indices):
        nonlocal total
        for index in indices:
            total -= costs.pop(index, 0)

    for group in history_groups(messages):
        if total <= window:
            break
        discard(group)

    if total > window:
        data_blocks = {index: value for index, message in enumerate(messages)
                       if (value := unpack_data(message)) is not None}
        for field in ('retrieval', 'referenced_notes', 'ppt_selection', 'summary', 'personal_memory'):
            if total <= window:
                break
            if field == 'summary':
                discard(index for index, message in enumerate(messages)
                        if message.additional_kwargs.get('raglearn_history') == 'summary')
                continue
            for index, data in data_blocks.items():
                if total <= window:
                    break
                if index not in costs or field not in data:
                    continue
                data.pop(field)
                memory_omitted |= field == 'personal_memory'
                discard((index,))
                if any(data.values()):
                    wire[index] = {**wire[index], 'content': encode_data(data)}
                    costs[index] = message_cost(wire[index], count)
                    total += costs[index]

    if total > window:
        raise ModelContextExceeded(window, total)
    body['messages'] = [message for index, message in enumerate(wire) if index in costs]
    if native:
        body['options']['num_ctx'] = window
    return memory_omitted


def check_embedding_context(profile, texts):
    window = profile.get('context_window')
    if window is None:
        return
    count = text_counter(profile)
    margin = max(8, (window + 19) // 20)
    for text in texts:
        total = count(text) + margin
        if total > window:
            raise ModelContextExceeded(window, total)
