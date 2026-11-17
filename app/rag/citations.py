"""Bind answer references to published source spans for one chat turn."""
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import re
from app.rag.source_spans import CHUNKER_VERSION


POLICY = ('引用本轮 retrieval 中有编号的资料时，在相应陈述后写出 [E1] 形式的编号。'
          '只使用该条资料的编号。区分原文支持的结论和推断，资料不足时说明缺口。')
MARKER = re.compile(r'\[(E[1-9][0-9]?)\]')
ANSWER_ROLES = {'chat', 'chat_thinking', 'vision', 'synthesize'}


def locator_digest(locator):
    encoded = json.dumps(locator, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def assemble_evidence(rows):
    blocks, sources = [], []
    length = 2
    for rank, row in enumerate(rows, 1):
        locator = row.get('source_locator')
        summary = row.get('summary')
        if summary:
            text = summary[:800]
        elif locator and locator.get('chunker_version') == CHUNKER_VERSION:
            text = row['content']
        else:
            text = row['content'][:800]
        block = {'text': text}
        source = {
            'content': row['content'][:200], 'source': row['source'], 'score': row.get('score', 0),
            'metadata': row.get('metadata', {}), 'chunk_id': row.get('chunk_id'),
            'fusion_score': row.get('fusion_score'), 'fusion_rank': row.get('fusion_rank'),
            'provenance': row.get('provenance', []),
            'rerank_score': row.get('rerank_score'), 'retrieval_rank': rank,
        }
        if locator:
            block['id'] = f'E{rank}'
            source['source_locator'] = locator
        encoded = json.dumps(block, ensure_ascii=False)
        if length + len(encoded) + 2 <= 12000:
            blocks.append(encoded)
            length += len(encoded) + 2
            if locator:
                source['citation_id'] = block['id']
        sources.append(source)
    return ('[' + ', '.join(blocks) + ']' if blocks else ''), sources


@dataclass
class CitationTurn:
    context: str
    sources: list
    delivered: bool = False
    owner: str | None = None
    session_factory: object = None

    def cited(self, answer):
        used = set(MARKER.findall(answer)) if self.delivered else set()
        return self._references(used)

    def _references(self, used):
        result = {}
        for source in self.sources:
            ref = source.get('citation_id')
            if ref not in used:
                continue
            locator = source['source_locator']
            result[ref] = {
                **{key: locator[key] for key in ('source_kind', 'resource_id', 'resource_version_id', 'ordinal')},
                'locator_digest': locator_digest(locator), 'retrieval_rank': source['retrieval_rank'],
                'rerank_score': source.get('rerank_score'),
            }
        return result

    async def authorize(self):
        """Validate the exact dispatched source spans, including on subsequent model/tool calls."""
        if not self.owner or not self.context:
            return
        from app.core.failed_response import BusinessError, ErrorCode
        from app.rag.visibility import CandidateVisibilityService

        revoked = BusinessError(ErrorCode.RAG_UNAVAILABLE, message='检索资料已变更，请重新提问', http_status=409)
        blocks = json.loads(self.context)
        if not blocks or any(not block.get('id') for block in blocks):
            raise revoked
        refs = {block['id'] for block in blocks}
        expected = self._references(refs)
        if refs != expected.keys():
            raise revoked
        visibility = CandidateVisibilityService(self.session_factory)
        async with visibility.session() as db:
            await visibility.authorize(self.owner, db)
            if await confirm_citations(db, self.owner, expected) != expected:
                raise revoked


current_citations = ContextVar('rag_citations', default=None)


async def authorize_turn_evidence():
    turn = current_citations.get()
    if turn is not None:
        await turn.authorize()


async def confirm_citations(db, owner, citations):
    """Check current publication under lock in the reply's commit transaction."""
    from app.core.failed_response import BusinessError
    from app.rag.source_access import document_source, note_source

    confirmed = {}
    for ref, item in citations.items():
        identifier, version = item['resource_id'], item['resource_version_id']
        try:
            if item['source_kind'] == 'knowledge':
                _, locator = await document_source(db, owner, int(identifier), item['ordinal'], version, lock=True)
            elif item['source_kind'] == 'notes':
                _, locator = await note_source(db, owner, identifier, version, item['ordinal'])
            else:
                continue
        except BusinessError as error:
            if error.http_status != 404:
                raise
            continue
        if locator_digest(locator) == item['locator_digest']:
            confirmed[ref] = item
    return confirmed


async def read_citation(db, owner, session_id, message_id, ref):
    from sqlalchemy import select
    from app.models.chat import ChatMessage, ChatSession
    from app.core.failed_response import BusinessError, ErrorCode
    from app.rag.source_access import read_document_source, note_source

    missing = BusinessError(ErrorCode.DOCUMENT_NOT_FOUND, message='引用原文不可用', http_status=404)
    mapping = await db.scalar(select(ChatMessage.citations_json).join(ChatSession).where(
        ChatSession.user_id == owner, ChatSession.id == session_id,
        ChatMessage.id == message_id, ChatMessage.role == 'assistant',
    ))
    item = (mapping or {}).get(ref)
    if not item:
        raise missing
    identifier, version = item['resource_id'], item['resource_version_id']
    if item['source_kind'] == 'knowledge':
        source = await read_document_source(db, owner, int(identifier), item['ordinal'], version)
    elif item['source_kind'] == 'notes':
        note, locator = await note_source(db, owner, identifier, version, item['ordinal'])
        content = (note.content or note.title)[locator['char_start']:locator['char_end']]
        source = {'title': note.title, 'content': content, 'source_locator': locator}
    else:
        raise missing
    if locator_digest(source['source_locator']) != item['locator_digest']:
        raise missing
    return {**source, 'citation_id': ref, 'retrieval_rank': item['retrieval_rank'], 'rerank_score': item['rerank_score']}
