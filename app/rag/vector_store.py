"""
向量存储服务

管理 ChromaDB 向量库的所有操作：
- 线程安全单例模式（双重检查锁定）
- 知识库文档向量化与检索
- 笔记向量双写
- 文档/笔记双源向量检索（关键词召回在 M3 接入）
- RAG 路由决策
- MD5 去重存储
- 用户隔离（通过 metadata 过滤）
"""

import time
import hashlib
import threading
from contextlib import contextmanager, nullcontext
from functools import partial
from pathlib import Path
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

from langchain_core.embeddings import Embeddings
from app.core.logger_handler import logger
from app.rag.blocking_io import BlockingPool
from app.rag.chroma_transport import ChromaTransportError, http_chroma, open_chroma, route_port, vector_io
from app.rag.query_embeddings import query_embedding as shared_query_embedding
from app.utils.config import get_chroma_config

_legacy_embedding_io = BlockingPool('rag-embedding', workers=4, queued=8)


class VectorStoreService:
    """
    向量存储服务（线程安全单例）

    使用双重检查锁定模式确保 ChromaDB 只有一个实例，
    避免多实例导致的并发写入冲突。

    ⚠️ 单例状态规范（生产风险分析·风险1）：本类是进程级全局单例，
    所有用户的请求共享同一实例。**禁止在实例上存储请求级状态**——
    Embedding 缓存为内容寻址（key=文本 MD5，相同文本 → 相同嵌入），
    跨用户命中不构成数据泄漏；新增字段前必须确认其语义与 user_id 无关。

    核心功能：
    - 管理两个 ChromaDB Collection：rag_collection（知识库）+ notes_collection（笔记）
    - 文档/笔记向量查询；关键词混合检索尚待 M3 实施
    - RAG 路由决策（判断是否需要走 RAG 管线）
    """

    # DashScope Embedding API 单次请求最大条数
    EMBEDDING_BATCH_SIZE = 20
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        """双重检查锁定单例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, embed_model=None):
        """
        初始化向量存储
        
        Args:
            embed_model: LangChain Embeddings 模型实例
        """
        # 避免重复初始化
        if hasattr(self, '_initialized'):
            # A durable worker may obtain the singleton before legacy model initialization.
            if self.embed_model is None and embed_model is not None:
                self.embed_model = embed_model
            return
        
        self._initialized = True
        self.config = get_chroma_config()
        self.embed_model = embed_model
        self._chroma_client = None
        self._rag_collection = None
        self._notes_collection = None
        
        # Embedding 缓存：{text_hash: (embedding, timestamp)}
        # 内容寻址（key=文本 MD5，相同文本 → 相同嵌入），跨用户命中无真实泄漏；
        # 使用 OrderedDict 实现 LRU 上限，防内存无限增长（生产风险分析·风险1）
        self._embedding_cache: "OrderedDict[str | tuple[str, str], Tuple[List[float], float]]" = OrderedDict()
        self._embedding_cache_ttl = 300          # 5 分钟 TTL
        self._embedding_cache_max_entries = 10000  # LRU 上限（约 10000×8KB ≈ 80MB 封顶）

        logger.info("VectorStoreService 初始化（延迟嵌入模式）")
    
    def _ensure_initialized(self):
        """
        延迟打开本地 Chroma 集合。实际 query/upsert 由 Chroma 校验向量维度。
        """
        if self._rag_collection is not None and self._notes_collection is not None:
            return
        
        try:
            persist_dir = str(Path(self.config.get("persist_directory", "data/chroma")).resolve())
            client = open_chroma(persist_dir)
            
            # 获取或创建 Collection
            rag_name = self.config.get("collections", {}).get("rag", "rag_collection")
            notes_name = self.config.get("collections", {}).get("notes", "notes_collection")
            
            rag = client.get_or_create_collection(
                name=rag_name, metadata={"hnsw:space": "cosine"}
            )
            notes = client.get_or_create_collection(
                name=notes_name, metadata={"hnsw:space": "cosine"}
            )

            logger.info(
                f"ChromaDB 初始化完成: rag={rag_name}({rag.count()}条), notes={notes_name}({notes.count()}条)"
            )
            self._chroma_client = client
            self._rag_collection = rag
            self._notes_collection = notes

        except Exception as e:
            logger.error(f"ChromaDB 初始化失败: {e}")
            raise

    @property
    def _legacy_directory(self):
        config = getattr(self, "config", None)
        return config.get("persist_directory", "data/chroma") if config is not None else None

    @contextmanager
    def _legacy_collection(self, key):
        port = route_port(self._legacy_directory)
        if port is None:
            self._ensure_initialized()
            yield self._rag_collection if key == "rag" else self._notes_collection
            return
        name = self.config.get("collections", {}).get(key, f"{key}_collection")
        with http_chroma(port) as client:
            collection = client.get_collection(name)
            if (collection.metadata or {}).get("hnsw:space") != "cosine":
                raise ChromaTransportError("Chroma collection metric mismatch")
            yield collection

    async def _legacy_io(self, key, operation, *, write=False):
        def call():
            with self._legacy_collection(key) as collection:
                return operation(collection)

        return await vector_io(self._legacy_directory, call, write=write)

    async def upsert_document(
        self,
        documents: List[str],
        metadatas: List[dict],
        ids: List[str],
        collection: str = "rag",
        *,
        write_guard=None,
    ) -> None:
        """
        批量写入/更新向量到 ChromaDB
        
        Args:
            documents: 文本内容列表
            metadatas: 元数据列表（包含 user_id 等）
            ids: 向量 ID 列表
            collection: 目标集合（"rag" 或 "notes"）
        """
        from app.model_config.snapshots import current_snapshot
        if current_snapshot() is not None:
            from app.model_config.vector_runtime import upsert
            return await upsert(documents, metadatas, ids, collection, write_guard=write_guard)
        # 生成嵌入向量
        embeddings = await self._generate_embeddings(documents)
        
        # 批量 upsert
        async with write_guard() if write_guard else nullcontext():
            await self._legacy_io(collection, lambda target: target.upsert(
                ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas), write=True)
        
        logger.debug(f"向量写入完成: collection={collection}, count={len(ids)}")
    
    async def search_documents(
        self, query: str, user_id: str, top_k: int = 5
    ) -> List[dict]:
        """
        搜索知识库文档
        
        按 user_id 过滤实现用户隔离。
        
        Args:
            query: 查询文本
            user_id: 用户 ID（用于过滤）
            top_k: 返回结果数量
            
        Returns:
            检索结果列表 [{"content": ..., "metadata": ..., "score": ...}, ...]
        """
        from app.model_config.snapshots import current_snapshot
        if current_snapshot() is not None:
            from app.model_config.vector_runtime import query as bound_query
            results=await bound_query(query,user_id,'rag',top_k)
        else:
            query_embedding = await shared_query_embedding((id(self), query), lambda: self._generate_embeddings([query]))
            results = await self._legacy_io("rag", lambda target: target.query(
                query_embeddings=query_embedding,n_results=top_k,where={"user_id": user_id},
                include=["documents", "metadatas", "distances"]))
        
        # 格式化结果
        formatted = []
        if results and results["documents"]:
            for i, doc in enumerate(results["documents"][0]):
                formatted.append({
                    "content": doc,
                    "chunk_id": results["ids"][0][i] if results.get("ids") else None,
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "score": 1 - (results["distances"][0][i] if results["distances"] else 1),
                })
        
        return formatted
    
    async def search_notes(
        self, query: str, user_id: str, top_k: int = 5
    ) -> List[dict]:
        """
        语义搜索笔记
        
        Args:
            query: 查询文本
            user_id: 用户 ID
            top_k: 返回结果数量
            
        Returns:
            检索结果列表
        """
        from app.model_config.snapshots import current_snapshot
        if current_snapshot() is not None:
            from app.model_config.vector_runtime import query as bound_query
            results=await bound_query(query,user_id,'notes',top_k)
        else:
            query_embedding = await shared_query_embedding((id(self), query), lambda: self._generate_embeddings([query]))
            results = await self._legacy_io("notes", lambda target: target.query(
                query_embeddings=query_embedding,n_results=top_k,where={"user_id": user_id},
                include=["documents", "metadatas", "distances"]))
        
        formatted = []
        if results and results["documents"]:
            for i, doc in enumerate(results["documents"][0]):
                formatted.append({
                    "content": doc,
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "note_id": results["metadatas"][0][i].get("note_id", ""),
                    "chunk_id": results["ids"][0][i] if results.get("ids") else None,
                    "score": 1 - (results["distances"][0][i] if results["distances"] else 1),
                })
        
        return formatted
    
    async def delete_document_vectors(self, document_id: str, *, user_id: str | None = None, write_guard=None) -> None:
        """
        删除知识库文档的向量（按 document_id）
        
        Args:
            document_id: 文档 ID
        """
        from app.model_config.snapshots import current_snapshot
        if current_snapshot() is not None:
            from app.model_config.vector_runtime import delete
            return await delete(document_id,'rag','document_id',user_id=user_id,write_guard=write_guard)
        
        # 查找匹配的向量 ID
        where = {'document_id': str(document_id)}
        if user_id is not None:
            where = {'$and': [where, {'user_id': user_id}]}
        def remove(target):
            results = target.get(where=where, include=[])
            if results and results["ids"]:
                target.delete(ids=results["ids"])
                logger.info(f"知识库向量删除: document_id={document_id}, count={len(results['ids'])}")

        async with write_guard() if write_guard else nullcontext():
            await self._legacy_io("rag", remove, write=True)
    
    async def delete_note_vectors(self, note_id: str, *, user_id: str | None = None, write_guard=None) -> None:
        """
        删除笔记的向量
        
        Args:
            note_id: 笔记 ID
        """
        from app.model_config.snapshots import current_snapshot
        if current_snapshot() is not None:
            from app.model_config.vector_runtime import delete
            return await delete(note_id, 'notes', 'note_id', user_id=user_id, write_guard=write_guard)
        where = {'note_id': note_id}
        if user_id is not None:
            where = {'$and': [where, {'user_id': user_id}]}
        async with write_guard() if write_guard else nullcontext():
            await self._legacy_io("notes", lambda target: target.delete(where=where), write=True)
    
    async def compute_route_score(self, query: str, user_id: str) -> float:
        """
        计算 RAG 路由决策分数
        
        同时检查本用户知识库与笔记的 Top-1 余弦距离。
        分数越低表示查询与知识库越相关。
        
        Args:
            query: 用户查询
            
        Returns:
            路由分数（余弦距离，越小越相关）；聊天入口不再使用前置距离门控。
        """
        from app.model_config.snapshots import current_snapshot
        if current_snapshot() is not None:
            from app.model_config.vector_runtime import query as bound_query
            distances=[]
            for key in ('rag','notes'):
                value=await bound_query(query,user_id,key,1)
                if value.get('distances') and value['distances'][0]:distances.append(value['distances'][0][0])
            return min(distances,default=float('inf'))
        if not user_id:
            raise ValueError("RAG routing requires an authenticated user")
        
        query_embedding = await self._generate_embeddings([query])
        
        distances = []
        for key in ("rag", "notes"):
            results = await self._legacy_io(key, lambda target: target.query(
                query_embeddings=query_embedding, n_results=1, where={"user_id": user_id}, include=["distances"]))
            if results and results["distances"] and results["distances"][0]:
                distances.append(results["distances"][0][0])
        return min(distances, default=float("inf"))
    
    async def _generate_embeddings(self, texts: List[str], *, index_key='rag', purpose='document') -> List[List[float]]:
        """
        生成文本的嵌入向量（带 LRU 缓存）
        
        相同文本在 TTL 内直接返回缓存结果，避免重复 API 调用。
        
        Args:
            texts: 文本列表
            
        Returns:
            嵌入向量列表
        """
        from app.model_config.snapshots import current_snapshot
        if current_snapshot() is not None:
            from app.model_config.vector_runtime import embeddings
            return await embeddings(texts,index_key,purpose=purpose)
        if self.embed_model is None:
            raise RuntimeError("Embedding 模型未初始化")
        
        now = time.time()
        results: List[List[float]] = []
        texts_to_embed: List[str] = []
        indices_to_fill: List[int] = []  # 需要回填的索引
        from app.rag.query_embeddings import query_owner
        owner = query_owner.get()
        keys = [(owner, hashlib.sha256(text.encode()).hexdigest()) if owner else hashlib.md5(text.encode()).hexdigest()
                for text in texts]
        
        # 检查缓存（命中时 move_to_end 维持 LRU 顺序）
        for i, text in enumerate(texts):
            cache_key = keys[i]
            cached = self._embedding_cache.get(cache_key)
            if cached and (now - cached[1]) < self._embedding_cache_ttl:
                results.append(cached[0])
                self._embedding_cache.move_to_end(cache_key)
            else:
                results.append([])  # 占位
                texts_to_embed.append(text)
                indices_to_fill.append(i)
        
        # 分批生成未缓存的 embedding（DashScope API 单次上限 20 条）
        if texts_to_embed:
            new_embeddings: List[List[float]] = []
            batch_size = self.EMBEDDING_BATCH_SIZE
            total_batches = (len(texts_to_embed) + batch_size - 1) // batch_size
            
            for batch_idx in range(total_batches):
                start = batch_idx * batch_size
                end = start + batch_size
                batch_texts = texts_to_embed[start:end]
                
                model = self.embed_model
                if getattr(type(model), 'aembed_documents', None) is Embeddings.aembed_documents:
                    # The base async method only submits a synchronous provider call to the default executor.
                    batch_embeddings = await _legacy_embedding_io.run(partial(model.embed_documents, batch_texts))
                else:
                    batch_embeddings = await model.aembed_documents(batch_texts)
                new_embeddings.extend(batch_embeddings)
                
                logger.debug(
                    f"Embedding 分批生成: {batch_idx + 1}/{total_batches}, "
                    f"本批 {len(batch_texts)} 条"
                )
            
            for idx, text, emb in zip(indices_to_fill, texts_to_embed, new_embeddings):
                results[idx] = emb
                cache_key = keys[idx]
                self._embedding_cache[cache_key] = (emb, now)
                self._embedding_cache.move_to_end(cache_key)
            # LRU 淘汰：超出上限时弹出最久未使用的条目
            while len(self._embedding_cache) > self._embedding_cache_max_entries:
                self._embedding_cache.popitem(last=False)
            logger.debug(
                f"Embedding 缓存命中 {len(texts) - len(texts_to_embed)}/{len(texts)}, "
                f"共 {total_batches} 批"
            )
        
        return results
    
    def get_collection_count(self, collection: str = "rag") -> int:
        """
        获取 Collection 中的向量数量
        
        Args:
            collection: 集合名称（"rag" 或 "notes"）
            
        Returns:
            向量数量
        """
        from app.model_config.snapshots import current_snapshot,inference_actor
        if current_snapshot() is not None:
            from app.model_config.vector_runtime import selected_index,bound_collection
            _,index,_=selected_index(collection)
            actor=inference_actor.get()
            if not actor:raise ValueError('Authenticated vector count required')
            with bound_collection(index) as target:return len(target.get(where={'user_id':actor['user_id']},include=[])['ids'])
        with self._legacy_collection(collection) as target:
            return target.count()

