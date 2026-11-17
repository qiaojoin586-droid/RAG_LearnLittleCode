"""Published source versions and exact character spans for retrieval candidates."""

from hashlib import sha256


CHUNKER_VERSION = 'structural-spans-v2'
KEYWORD_REVISION = 'published-chunks-v1'


def hash_text(text):
    return sha256(text.encode('utf-8')).hexdigest()


def make_document_manifest(extension, version, chunks, *, parser_version=None):
    return {
        'resource_version_id': version,
        'parser_version': parser_version or ('pymupdf-page-text-v1' if extension == 'pdf' else 'utf8-sig-v1'),
        'chunker_version': CHUNKER_VERSION,
        'keyword_projection_revision': KEYWORD_REVISION,
        'chunks': [{**span, 'text_sha256': hash_text(text)} for text, span in chunks],
    }


def document_locator(document_id, manifest, ordinal):
    if type(ordinal) is not int or not 0 <= ordinal < len(manifest['chunks']):
        return None
    return {
        'source_kind': 'knowledge', 'resource_id': str(document_id), 'chunk_id': f'{document_id}_{ordinal}',
        'ordinal': ordinal,
        'resource_version_id': manifest['resource_version_id'],
        'parser_version': manifest['parser_version'], 'chunker_version': manifest['chunker_version'],
        **manifest['chunks'][ordinal],
    }


def read_span(pages, span):
    text = pages.get(span.get('page_number'))
    start, end = span['char_start'], span['char_end']
    if text is None or not 0 <= start < end <= len(text):
        raise ValueError('Document source span is unavailable')
    excerpt = text[start:end]
    if hash_text(excerpt) != span['text_sha256']:
        raise ValueError('Document source span has changed')
    return excerpt


def note_chunk_id(identifier, ordinal):
    return f'note_{identifier}' if ordinal == 0 else f'note_{identifier}_{ordinal}'


def note_locator(note, revision, manifest=None, ordinal=0):
    text = note.content or note.title
    digest = hash_text(text)
    if manifest is not None:
        if manifest['resource_version_id'] != digest:
            return None
        locator = document_locator(note.id, manifest, ordinal)
        if locator is None or hash_text(text[locator['char_start']:locator['char_end']]) != locator['text_sha256']:
            return None
        return {**locator, 'source_kind': 'notes', 'chunk_id': note_chunk_id(note.id, ordinal),
                'source_field': 'content' if note.content else 'title',
                'resource_version_id': hash_text(f'{revision}:{digest}')}
    if ordinal != 0:
        return None
    return {
        'source_kind': 'notes', 'resource_id': note.id,
        'resource_version_id': hash_text(f'{revision}:{digest}') if revision else digest,
        'chunk_id': f'note_{note.id}', 'ordinal': 0, 'text_sha256': digest,
        'source_field': 'content' if note.content else 'title', 'char_start': 0, 'char_end': len(text),
    }
