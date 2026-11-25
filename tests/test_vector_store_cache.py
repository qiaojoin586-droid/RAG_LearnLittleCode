"""
Embedding 缓存 LRU 上限测试（生产风险分析·风险1）

验证：缓存无上限 → 加 LRU 淘汰后：
1. 超过 max_entries 时淘汰最久未使用的条目
2. 命中条目 move_to_end（保持 LRU 顺序，不被误淘汰）
"""

import hashlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.rag.vector_store import VectorStoreService


class FakeEmbedModel:
    """模拟 LangChain Embeddings（内容无关的确定性向量）"""

    async def aembed_documents(self, texts):
        return [[float(len(t))] * 8 for t in texts]


def _hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


@pytest.fixture
def service():
    vs = VectorStoreService()  # 单例（测试进程内首次创建）
    vs.embed_model = FakeEmbedModel()
    vs._embedding_cache.clear()
    # 用小上限验证淘汰逻辑
    vs._embedding_cache_max_entries = 5
    vs._embedding_cache_ttl = 300
    return vs


@pytest.mark.asyncio
async def test_cache_evicts_oldest_when_over_limit(service):
    await service._generate_embeddings([f"text-{i}" for i in range(6)])

    assert len(service._embedding_cache) == 5, "超出上限后应只保留 5 条"
    assert _hash("text-0") not in service._embedding_cache, "最旧的 text-0 应被淘汰"
    assert _hash("text-5") in service._embedding_cache, "最新的 text-5 应保留"


@pytest.mark.asyncio
async def test_cache_hit_keeps_entry_alive(service):
    # 写入 5 条填满缓存
    await service._generate_embeddings([f"text-{i}" for i in range(5)])

    # 命中 text-0 → move_to_end（成为最新）
    result = await service._generate_embeddings(["text-0"])
    assert result[0] == [6.0] * 8, "命中应返回缓存向量（'text-0' 长度为 6）"

    # 再写一条新文本 → 淘汰的应是最旧的 text-1，而非刚命中的 text-0
    await service._generate_embeddings(["text-new"])
    assert _hash("text-0") in service._embedding_cache, "刚命中的 text-0 不应被淘汰"
    assert _hash("text-1") not in service._embedding_cache, "最旧的 text-1 应被淘汰"
    assert len(service._embedding_cache) == 5


@pytest.mark.asyncio
async def test_sync_provider_uses_bounded_worker_and_retains_cache(service):
    import threading
    from langchain_core.embeddings import Embeddings

    calls = []
    class SyncProvider(Embeddings):
        def embed_documents(self, texts):
            calls.append(threading.current_thread().name)
            return [[1., 0.] for _ in texts]
        def embed_query(self, text):
            return self.embed_documents([text])[0]
    service.embed_model = SyncProvider()
    for _ in range(2):
        assert await service._generate_embeddings(['sync']) == [[1., 0.]]
    assert len(calls) == 1 and calls[0].startswith('rag-embedding')


@pytest.mark.asyncio
async def test_chroma_opens_existing_index_without_provider_and_rejects_wrong_dimensions(tmp_path, monkeypatch):
    import chromadb
    from chromadb.config import Settings
    from chromadb.errors import InvalidArgumentError

    for name in ('TEMP', 'TMP', 'SQLITE_TMPDIR'):
        monkeypatch.setenv(name, str(tmp_path))
    directory = tmp_path / 'chroma'
    directory.mkdir()
    relative = os.path.relpath(directory, Path.cwd()).replace('\\', '/')
    with chromadb.PersistentClient(path=relative, settings=Settings(anonymized_telemetry=False)) as client:
        collection = client.get_or_create_collection('rag_collection', metadata={'hnsw:space': 'cosine'})
        collection.upsert(ids=['old'], documents=['fixture'], embeddings=[[1, 0]], metadatas=[{'user_id': 'owner'}])
    embed = Mock(embed_documents=Mock(side_effect=AssertionError('Chroma initialization called an embedding provider')),
                 aembed_documents=AsyncMock(side_effect=ConnectionError('provider unavailable')))
    monkeypatch.setattr('app.rag.vector_store.get_chroma_config', lambda: {'persist_directory': str(directory)})
    monkeypatch.setattr(VectorStoreService, '_instance', None)
    vector = VectorStoreService(embed_model=embed)

    try:
        assert vector.get_collection_count() == 1
        embed.embed_documents.assert_not_called()
        embed.aembed_documents.assert_not_called()
        with pytest.raises(ConnectionError, match='provider unavailable'):
            await vector.search_documents('request without an available model', 'owner')
        assert vector.get_collection_count() == 1

        embed.aembed_documents.side_effect = None
        embed.aembed_documents.return_value = [[1, 0, 0]]
        with pytest.raises(InvalidArgumentError, match='dimension'):
            await vector.search_documents('incorrect query dimension', 'owner')
        with pytest.raises(InvalidArgumentError, match='dimension'):
            await vector.upsert_document(['incorrect document dimension'], [{'user_id': 'owner'}], ['new'])
        assert vector.get_collection_count() == 1
    finally:
        if vector._chroma_client is not None:
            vector._chroma_client.close()
