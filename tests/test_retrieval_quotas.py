"""Frozen local M3-11 limits and rank provenance."""
import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.rag import rag_service
from app.rag.rag_service import RagService
from app.rag.ranking import candidate_key, fuse_recall_lanes


def hit(identifier, source, *, family='vector', number=0, rank=1, version='v1'):
    field = 'document_id' if source == 'knowledge' else 'note_id'
    return {'source': source, 'metadata': {'user_id': 'owner', field: identifier, 'source_sha256': version},
            'chunk_id': str(identifier) + '-0', 'content': 'same text', 'source_rank': rank,
            'recall_method': family, 'subquery_id': number, 'score': .99, 'algorithm': 'sql_literal',
            'keyword_score': 3.}


@pytest.mark.asyncio
@pytest.mark.parametrize('expanded,raw_cap,fused_cap', [(False, 80, 40), (True, 160, 60)])
async def test_full_entry_point_enforces_every_quota(expanded, raw_cap, fused_cap, monkeypatch, request):
    question = '冷却泵的安装步骤和维护方法有哪些？'
    rewrites = ['冷却泵的安装流程', '冷却泵的维护方式']
    queries = [question, *rewrites]
    calls, checks, fused_rows, reranked = [], [], [], []
    def search(family, source):
        async def run(text, owner, top_k):
            assert top_k == 20 and owner == 'owner'
            calls.append((family, source, text))
            prefix = f'{family}-{source}-{queries.index(text)}-'
            # A backend returning too many candidates must still respect server caps.
            return [hit(prefix + str(n), source) for n in range(25)]
        return run
    def backend(family):
        return SimpleNamespace(search_documents=search(family, 'knowledge'), search_notes=search(family, 'notes'))
    async def visible(rows, owner):
        checks.append(len(rows))
        return rows
    def fuse(rows, limit, bindings):
        result = fuse_recall_lanes(rows, limit, bindings)
        fused_rows.extend(result)
        return result
    def predict(pairs):
        reranked.extend(pairs)
        return list(range(len(pairs)))
    monkeypatch.setattr(rag_service, 'fuse_recall_lanes', fuse)
    generator = AsyncMock(return_value=json.dumps({'queries': rewrites})) if expanded else None
    service = RagService(backend('vector'), keyword_search=backend('keyword'), enable_keyword=True,
                         expansion_generator=generator, rerank_model=SimpleNamespace(predict=predict),
                         visibility_checker=visible)
    result = await service.query(question, 'owner', top_k=10)
    assert result['retrieval_policy_revision_id'] == 'hybrid-rrf-v2'
    assert result['status'] == 'success' and checks == [raw_cap, 30, 5]
    assert len(calls) == raw_cap // 20 and len(fused_rows) == fused_cap
    counts = Counter(row['provenance'][0]['method'] for row in fused_rows)
    assert counts == ({'vector': 20, 'keyword': 20, 'expanded': 20} if expanded else {'vector': 20, 'keyword': 20})
    assert len(reranked) == 30 and len(result['sources']) == 5
    assert [row['fusion_rank'] for row in result['sources']] == [30, 29, 28, 27, 26]
    request.node.user_properties.append(('quotas', json.dumps(dict(
        expanded=expanded, backend_calls=len(calls), raw_hits=checks[0], family_sizes=counts,
        fused=len(fused_rows), reranked=len(reranked), final=len(result['sources'])))))


def test_stable_scope_version_chunk_dedup_and_local_ranks():
    doc = hit('a', 'knowledge', rank=2)
    note = hit('a', 'notes')
    previous = hit('a', 'knowledge', version='v0', rank=1)
    other_scope = {**doc, 'source_rank': 4, 'metadata': {**doc['metadata'], 'user_id': 'other'}}
    other_chunk = {**doc, 'source_rank': 5, 'chunk_id': 'a-1'}
    keyword = hit('a', 'knowledge', family='keyword')
    extra1 = hit('a', 'knowledge', family='expanded', number=1)
    extra2 = hit('a', 'knowledge', family='expanded', number=2)
    rows = [previous, doc, {**doc, 'source_rank': 3}, other_scope, other_chunk, note, keyword, extra1, extra2]
    bindings = {'rag': {'index_version_id': 'index-A'}, 'notes': {'index_version_id': 'index-B'}}
    ranked = fuse_recall_lanes(rows, 20, bindings)
    assert ranked == fuse_recall_lanes(list(reversed(rows)), 20, bindings)
    assert len(ranked) == 5 and len({candidate_key(row) for row in ranked}) == 5
    best = ranked[0]
    original, literal, first, second = best['provenance']
    assert original['source_rank'] == 2 and original['index_version_id'] == 'index-A'
    assert original['resource_version_id'] == 'v1' and original['raw_score'] == .99
    assert literal['algorithm'] == 'sql_literal' and literal['raw_score'] == 3
    assert [(p['subquery_id'], p['family_rank']) for p in (first, second)] == [(1, 1), (2, 1)]
    assert best['fusion_score'] == pytest.approx(1 / (60 + original['family_rank']) + 2 / 61)


def test_unvoted_origin_is_retained_without_amplifying_score():
    # The shared candidate loses the vector top-20 cutoff after combining two
    # sources, but is the first keyword hit. The vector lane contributes no vote.
    rows = [hit(f'd{n}', 'knowledge', rank=n) for n in range(1, 21)]
    rows.extend(hit(f'n{n}', 'notes', rank=n) for n in range(1, 21))
    rows.append(hit('d20', 'knowledge', family='keyword'))
    fused = fuse_recall_lanes(rows, 20, {})
    shared = next(row for row in fused if row['chunk_id'] == 'd20-0')
    dense, keyword = shared['provenance']
    assert dense['source_rank'] == 20 and dense['rank'] > 20 and 'family_rank' not in dense
    assert keyword['family_rank'] == 1 and shared['fusion_score'] == pytest.approx(1 / 61)


def test_single_expanded_family_casts_one_ballot_when_other_sources_are_empty():
    rows = [hit('a', 'knowledge', family='expanded', number=i) for i in (1, 2)]
    candidate, = fuse_recall_lanes(rows, 20, {})
    assert len(candidate['provenance']) == 2 and candidate['fusion_score'] == pytest.approx(1 / 61)
