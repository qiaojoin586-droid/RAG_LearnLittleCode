"""Stable rank fusion over versioned retrieval candidates."""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class FusionPolicy:
    revision: str = 'hybrid-rrf-v2'
    per_source: int = 20
    per_family: int = 20
    raw_hits: int = 160
    fused: int = 60
    rerank: int = 30
    final: int = 5
    rank_constant: int = 60
    concurrency: int = 4
    total_seconds: float = 5.0
    recall_seconds: float = 2.5
    generation_seconds: float = 1.0
    rerank_seconds: float = 1.5
    summary_seconds: float = 1.0


FUSION_POLICY = FusionPolicy()
_FAMILY_ORDER = {'vector': 0, 'keyword': 1, 'expanded': 2}


def candidate_key(candidate: dict) -> tuple[str, str, str, str, str]:
    metadata = candidate.get("metadata") or {}
    source = candidate.get("source", "")
    locator = candidate.get("source_locator") or {}
    resource = metadata.get("document_id") if source == "knowledge" else metadata.get("note_id")
    version = locator.get("resource_version_id") or metadata.get("source_sha256") or metadata.get("index_revision")
    chunk = locator.get("chunk_id") or candidate.get("chunk_id")
    if chunk is None:
        chunk = metadata.get("chunk_index")
    if chunk is None:
        chunk = hashlib.sha256(candidate.get("content", "").encode()).hexdigest()
    return (str(metadata.get("user_id", "")), source, str(resource) if resource is not None else "",
            str(version) if version is not None else "", str(chunk))


def reciprocal_rank_fusion(lists: list[list[dict]], rank_constant: int = 60) -> list[dict]:
    """Each source contributes at most one rank per chunk; raw scores stay distinct."""
    if rank_constant < 1:
        raise ValueError("rank_constant must be positive")
    merged: dict[tuple, dict] = {}
    for candidates in lists:
        seen = set()
        for rank, candidate in enumerate(candidates, 1):
            key = candidate_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            row = merged.setdefault(key, {**candidate, "fusion_score": 0.0, "provenance": []})
            row["fusion_score"] += 1 / (rank_constant + rank)
            for origin in candidate.get("provenance", ()):
                if origin not in row["provenance"]:
                    row["provenance"].append(origin)
    return sorted(merged.values(), key=lambda row: (-row["fusion_score"], candidate_key(row)))


def fuse_recall_lanes(verified, limit, index_bindings):
    """Rank authorized source hits per query, then cast one ballot per family."""
    by_query = defaultdict(lambda: defaultdict(list))
    for row in verified:
        by_query[row['recall_method'], row['subquery_id']][row['source']].append(row)

    family_lists = defaultdict(list)
    origins = defaultdict(list)
    for family, number in sorted(by_query, key=lambda lane: (_FAMILY_ORDER[lane[0]], lane[1])):
        sources = by_query[family, number]
        lists = [sorted(rows, key=lambda row: row['source_rank']) for rows in sources.values()]
        ranked = reciprocal_rank_fusion(lists, FUSION_POLICY.rank_constant)
        for rank, row in enumerate(ranked, 1):
            source = row['source']
            algorithm = row['algorithm'] if family == 'keyword' else 'chroma_cosine'
            origin = {'method': family, 'rank': rank, 'source': source, 'source_rank': row['source_rank'],
                      'algorithm': algorithm, 'raw_score': row.get('keyword_score' if family == 'keyword' else 'score')}
            if number:
                origin['subquery_id'] = number
            key = candidate_key(row)
            if key[3]:
                origin['resource_version_id'] = key[3]
            if family != 'keyword':
                binding = index_bindings.get('rag' if source == 'knowledge' else 'notes')
                if binding:
                    origin['index_version_id'] = binding['index_version_id']
            elif row.get('keyword_index_revision'):
                origin['index_version_id'] = row['keyword_index_revision']
            row['provenance'] = [origin]
            origins[key].append(origin)
        family_lists[family].append(ranked[:limit])

    ballots = []
    for lists in family_lists.values():
        ballot = reciprocal_rank_fusion(lists, FUSION_POLICY.rank_constant)[:FUSION_POLICY.per_family]
        for family_rank, row in enumerate(ballot, 1):
            for origin in row['provenance']:
                origin['family_rank'] = family_rank
        ballots.append(ballot)
    fused = reciprocal_rank_fusion(ballots, FUSION_POLICY.rank_constant)[:FUSION_POLICY.fused]
    for rank, row in enumerate(fused, 1):
        row['fusion_rank'] = rank
        # A hit below another family's cutoff still explains how the evidence was found.
        # Only origins with a family rank contributed to that family's final ballot.
        row['provenance'] = origins[candidate_key(row)]
    return fused
