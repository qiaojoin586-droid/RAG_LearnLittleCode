"""Real SQLite visibility queries and model spies; no remote models/vector service."""
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeDocument
from app.models.note import Note
from app.models.note_index import note_index_jobs
from app.rag.rag_service import RagService
from app.rag.ranking import reciprocal_rank_fusion
from app.rag.visibility import CandidateVisibilityService
from app.rag.vector_store import VectorStoreService


def document(identifier, source="knowledge", content=None, user_id="a"):
    field = "document_id" if source == "knowledge" else "note_id"
    return {"content": content or f"text-{identifier}", "source": source,
            "metadata": {"user_id": user_id, field: str(identifier), "chunk_index": 0},
            "score": 0.8}


def test_rank_fusion_keeps_both_sources_and_deduplicates_same_list():
    docs = [document(i) for i in range(6)]
    notes = [document(i, "notes") for i in range(6)]
    fused = reciprocal_rank_fusion([docs, notes])
    assert {row["source"] for row in fused[:3]} == {"knowledge", "notes"}
    assert reciprocal_rank_fusion([[docs[0], docs[0]]])[0]["fusion_score"] == 1 / 61


@pytest.mark.asyncio
async def test_actual_db_filter_excludes_deleted_foreign_stale_and_missing_resources():
    engine = sa.create_engine("sqlite://")
    KnowledgeDocument.__table__.create(engine)
    Note.__table__.create(engine)
    note_index_jobs.create(engine)
    with engine.begin() as conn:
        conn.execute(KnowledgeDocument.__table__.insert(), [
            dict(id=i, user_id=user, filename="f", file_path="f", file_size=1,
                 file_type="text/plain", md5_hash=str(i), chunk_count=1)
            for i, user in [(1, "a"), (2, "b")]
        ])
        conn.execute(Note.__table__.insert(), [
            dict(id=identifier, user_id=user, title="title", content="current", tags=[],
                 deleted_at=deleted)
            for identifier, user, deleted in [("live", "a", None), ("gone", "a", datetime.now()),
                                               ("foreign", "b", None)]
        ])

    @asynccontextmanager
    async def factory():
        with Session(engine) as session:
            async def execute(statement):
                return session.execute(statement)
            yield SimpleNamespace(execute=execute)

    checker = CandidateVisibilityService(factory)
    candidates = [document(1), document(2), document(3), document(1, user_id="b"),
                  document("live", "notes", "current"), document("gone", "notes", "current"),
                  document("foreign", "notes", "current"), document("live", "notes", "stale")]
    # A legacy document without a published span cannot establish its version.
    visible_note, = await checker.filter(candidates, "a")
    assert visible_note['source_locator']['resource_id'] == 'live'
    assert {k: v for k, v in visible_note.items() if k != 'source_locator'} == candidates[4]
    engine.dispose()


@pytest.mark.asyncio
async def test_visibility_is_checked_before_rerank_and_context():
    valid, deleted = document(1), document(2)
    vector = SimpleNamespace(search_documents=AsyncMock(return_value=[valid, deleted]),
                             search_notes=AsyncMock(return_value=[]))
    checked = False

    async def visible(rows, user):
        nonlocal checked
        checked = True
        return [row for row in rows if row["metadata"]["document_id"] == "1"]

    def predict(pairs):
        assert checked and pairs == [("q", valid["content"])]
        return [0.9]

    service = RagService(vector, rerank_model=SimpleNamespace(predict=predict), visibility_checker=visible, enable_keyword=False)
    result = await service.query("q", "a", use_hyde=False)
    assert deleted["content"] not in result["context"]
    assert result["rerank_status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("scores", [RuntimeError("offline"), [float("nan")], [], [1, 2, 3, 4]])
async def test_reranker_failures_keep_fusion_ranking_and_report_degradation(scores):
    docs, notes = [document(i) for i in range(3)], [document(i, "notes") for i in range(3)]
    vector = SimpleNamespace(search_documents=AsyncMock(return_value=docs),
                             search_notes=AsyncMock(return_value=notes))
    reranker = SimpleNamespace(predict=Mock(side_effect=scores) if isinstance(scores, Exception)
                              else Mock(return_value=scores))
    service = RagService(vector, rerank_model=reranker, visibility_checker=AsyncMock(side_effect=lambda rows, user: rows),
                         enable_keyword=False)
    result = await service.query("q", "a", use_hyde=False)
    assert {row["source"] for row in result["sources"]} == {"knowledge", "notes"}
    assert result["status"] == "degraded" and result["rerank_status"] == "failed"


@pytest.mark.asyncio
async def test_predict_scores_actually_change_rank_and_numpy_like_arrays_are_supported():
    service = RagService(rerank_model=SimpleNamespace(predict=lambda pairs: SimpleNamespace(tolist=lambda: [0.1, 0.9])))
    result = await service._rerank("q", [document(1), document(2)], 2)
    assert result[0]["metadata"]["document_id"] == "2"


@pytest.mark.asyncio
async def test_one_failed_source_does_not_discard_other_source():
    vector = SimpleNamespace(search_documents=AsyncMock(side_effect=RuntimeError("offline")),
                             search_notes=AsyncMock(return_value=[document("n", "notes")]))
    service = RagService(vector, visibility_checker=AsyncMock(side_effect=lambda rows, user: rows), enable_keyword=False)
    result = await service.query("q", "a", use_hyde=False)
    assert result["sources"][0]["source"] == "notes"
    assert result["status"] == "partial" and result["failed_sources"] == ["knowledge"]


@pytest.mark.asyncio
async def test_control_store_failure_never_falls_back_to_unchecked_context():
    vector = SimpleNamespace(search_documents=AsyncMock(return_value=[document(1)]),
                             search_notes=AsyncMock(return_value=[]))
    reranker = SimpleNamespace(predict=Mock())
    service = RagService(vector, rerank_model=reranker,
                         visibility_checker=AsyncMock(side_effect=RuntimeError("db unavailable")), enable_keyword=False)
    with pytest.raises(RuntimeError, match="Retrieval could not complete") as failed:
        await service.query("q", "a", use_hyde=False)
    assert str(failed.value.__cause__) == "db unavailable"
    reranker.predict.assert_not_called()


@pytest.mark.asyncio
async def test_scoped_route_probe_covers_notes_without_cross_user_influence():
    vector = object.__new__(VectorStoreService)
    vector._ensure_initialized = lambda: None
    vector._generate_embeddings = AsyncMock(return_value=[[0.1]])
    vector._rag_collection = SimpleNamespace(query=Mock(return_value={"distances": [[0.9]]}))
    vector._notes_collection = SimpleNamespace(query=Mock(return_value={"distances": [[0.1]]}))
    assert await vector.compute_route_score("q", "a") == 0.1
    for collection in (vector._rag_collection, vector._notes_collection):
        assert collection.query.call_args.kwargs["where"] == {"user_id": "a"}
