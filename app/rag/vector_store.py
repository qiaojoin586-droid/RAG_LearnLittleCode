"""
向量存储服务

管理 ChromaDB 向量库的所有操作：
- 线程安全单例模式（双重检查锁定）
- 知识库文档向量化与检索
- 笔记向量双写
- 混合检索（BM25 + 向量）
- 动态权重调整
- RAG 路由决策
- MD5 去重存储
- 用户隔离（通过 metadata 过滤）
"""

import os
import time
import hashlib
import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.logger_handler import logger
from app.utils.config import get_chroma_config


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
    - 提供混合检索能力（BM25 + 向量 EnsembleRetriever）
    - 根据查询特征动态调整检索权重
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
        self._embedding_cache: "OrderedDict[str, Tuple[List[float], float]]" = OrderedDict()
        self._embedding_cache_ttl = 300          # 5 分钟 TTL
        self._embedding_cache_max_entries = 10000  # LRU 上限（约 10000×8KB ≈ 80MB 封顶）

        logger.info("VectorStoreService 初始化（延迟嵌入模式）")
    
    def _ensure_initialized(self):
        """
        确保 ChromaDB 已初始化（延迟初始化）
        
        首次调用时才真正连接 ChromaDB 和加载 Collection。
        """
        if self._rag_collection is not None:
            return
        
        try:
            import chromadb
            from chromadb.config import Settings
            
            persist_dir = self.config.get("persist_directory", "data/chroma")
            
            # 创建持久化客户端
            self._chroma_client = chromadb.PersistentClient(
                path=persist_dir,
                settings=Settings(anonymized_telemetry=False)
            )
            
            # 获取或创建 Collection
            rag_name = self.config.get("collections", {}).get("rag", "rag_collection")
            notes_name = self.config.get("collections", {}).get("notes", "notes_collection")
            
            self._rag_collection = self._chroma_client.get_or_create_collection(
                name=rag_name, metadata={"hnsw:space": "cosine"}
            )
            self._notes_collection = self._chroma_client.get_or_create_collection(
                name=notes_name, metadata={"hnsw:space": "cosine"}
            )

            logger.info(
                f"ChromaDB 初始化完成: rag={rag_name}({self._rag_collection.count()}条), "
                f"notes={notes_name}({self._notes_collection.count()}条)"
            )

            # 维度漂移防护（2026-08-22）：集合维度与当前 embedding 模型输出不一致时
            # 直接给出可执行的明确错误，替代运行期晦涩的 InvalidArgumentError
            self._check_embedding_dimension()

        except Exception as e:
            logger.error(f"ChromaDB 初始化失败: {e}")
            raise

    def _check_embedding_dimension(self) -> None:
        """
        维度一致性检查（防 embedding 模型更换导致向量库维度漂移）

        - 非空集合取首条向量的实际维度，与当前 embed_model 的输出维度比对
        - 空集合跳过（首次写入时自然定维）
        - 不一致时抛 RuntimeError，提示运行迁移脚本或恢复原模型配置
        """
        if self.embed_model is None:
            return

        for name, col in (("rag", self._rag_collection), ("notes", self._notes_collection)):
            if col.count() == 0:
                continue
            peek = col.peek(1)
            stored = peek.get("embeddings")
            if stored is None or len(stored) == 0:
                continue
            stored_dim = stored[0].shape[0] if hasattr(stored[0], "shape") else len(stored[0])

            probe = self.embed_model.embed_documents(["__embedding_dim_probe__"])
            if not probe:
                continue
            model_dim = len(probe[0])
            model_name = getattr(self.embed_model, "model", "未知模型")

            if model_dim != stored_dim:
                raise RuntimeError(
                    f"向量库维度不匹配：集合 {name} 为 {stored_dim} 维，"
                    f"当前 embedding 模型 {model_name} 输出 {model_dim} 维。"
                    f"请运行 python scripts/manual/migrate_embeddings_2560.py 迁移向量库，"
                    f"或将 DASHSCOPE_EMBED_MODEL 恢复为建库时的模型。"
                )
            logger.info(f"Embedding 维度一致性检查通过: {name}={stored_dim} 维")
    
    async def upsert_document(
        self,
        documents: List[str],
        metadatas: List[dict],
        ids: List[str],
        collection: str = "rag"
    ) -> None:
        """
        批量写入/更新向量到 ChromaDB
        
        Args:
            documents: 文本内容列表
            metadatas: 元数据列表（包含 user_id 等）
            ids: 向量 ID 列表
            collection: 目标集合（"rag" 或 "notes"）
        """
        self._ensure_initialized()
        
        target = self._rag_collection if collection == "rag" else self._notes_collection
        
        # 生成嵌入向量
        embeddings = await self._generate_embeddings(documents)
        
        # 批量 upsert
        target.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        
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
        self._ensure_initialized()
        
        # 生成查询向量
        query_embedding = await self._generate_embeddings([query])
        
        # ChromaDB 查询
        results = self._rag_collection.query(
            query_embeddings=query_embedding,
            n_results=top_k,
            where={"user_id": user_id},
            include=["documents", "metadatas", "distances"]
        )
        
        # 格式化结果
        formatted = []
        if results and results["documents"]:
            for i, doc in enumerate(results["documents"][0]):
                formatted.append({
                    "content": doc,
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
        self._ensure_initialized()
        
        query_embedding = await self._generate_embeddings([query])
        
        results = self._notes_collection.query(
            query_embeddings=query_embedding,
            n_results=top_k,
            where={"user_id": user_id},
            include=["documents", "metadatas", "distances"]
        )
        
        formatted = []
        if results and results["documents"]:
            for i, doc in enumerate(results["documents"][0]):
                formatted.append({
                    "content": doc,
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "note_id": results["metadatas"][0][i].get("note_id", ""),
                    "score": 1 - (results["distances"][0][i] if results["distances"] else 1),
                })
        
        return formatted
    
    async def delete_document_vectors(self, document_id: str) -> None:
        """
        删除知识库文档的向量（按 document_id）
        
        Args:
            document_id: 文档 ID
        """
        self._ensure_initialized()
        
        # 查找匹配的向量 ID
        results = self._rag_collection.get(
            where={"document_id": str(document_id)},
            include=[]
        )
        
        if results and results["ids"]:
            self._rag_collection.delete(ids=results["ids"])
            logger.info(f"知识库向量删除: document_id={document_id}, count={len(results['ids'])}")
    
    async def delete_note_vectors(self, note_id: str) -> None:
        """
        删除笔记的向量
        
        Args:
            note_id: 笔记 ID
        """
        self._ensure_initialized()
        
        results = self._notes_collection.get(
            where={"note_id": note_id},
            include=[]
        )
        
        if results and results["ids"]:
            self._notes_collection.delete(ids=results["ids"])
            logger.info(f"笔记向量删除: note_id={note_id}")
    
    async def compute_route_score(self, query: str) -> float:
        """
        计算 RAG 路由决策分数
        
        通过查询向量与知识库 Top-1 的 L2 距离判断是否需要 RAG 管线。
        分数越低表示查询与知识库越相关。
        
        Args:
            query: 用户查询
            
        Returns:
            路由分数（L2 距离，越小越相关）
        """
        self._ensure_initialized()
        
        threshold = float(os.getenv("RAG_ROUTE_THRESHOLD", "0.5"))
        
        query_embedding = await self._generate_embeddings([query])
        
        results = self._rag_collection.query(
            query_embeddings=query_embedding,
            n_results=1,
            include=["distances"]
        )
        
        if results and results["distances"] and results["distances"][0]:
            return results["distances"][0][0]
        
        return float('inf')  # 无结果时不进入 RAG
    
    async def _generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """
        生成文本的嵌入向量（带 LRU 缓存）
        
        相同文本在 TTL 内直接返回缓存结果，避免重复 API 调用。
        
        Args:
            texts: 文本列表
            
        Returns:
            嵌入向量列表
        """
        if self.embed_model is None:
            raise RuntimeError("Embedding 模型未初始化")
        
        now = time.time()
        results: List[List[float]] = []
        texts_to_embed: List[str] = []
        indices_to_fill: List[int] = []  # 需要回填的索引
        
        # 检查缓存（命中时 move_to_end 维持 LRU 顺序）
        for i, text in enumerate(texts):
            cache_key = hashlib.md5(text.encode()).hexdigest()
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
                
                batch_embeddings = await self.embed_model.aembed_documents(batch_texts)
                new_embeddings.extend(batch_embeddings)
                
                logger.debug(
                    f"Embedding 分批生成: {batch_idx + 1}/{total_batches}, "
                    f"本批 {len(batch_texts)} 条"
                )
            
            for idx, text, emb in zip(indices_to_fill, texts_to_embed, new_embeddings):
                results[idx] = emb
                cache_key = hashlib.md5(text.encode()).hexdigest()
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
        self._ensure_initialized()
        target = self._rag_collection if collection == "rag" else self._notes_collection
        return target.count()

