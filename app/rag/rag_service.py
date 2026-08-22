"""
RAG 核心服务

实现完整的 RAG（Retrieval-Augmented Generation）管线：
- HyDE 技术：LLM 生成假设性回答提升检索准确率
- 双源检索：同时从知识库和笔记库检索
- 重排序：CrossEncoder 对检索结果重排序
- 分批总结：Top 3 文档并发总结后合并
- 思考过程推送：通过 callback 实时推送阶段事件
"""

import asyncio
import concurrent.futures
import hashlib
from typing import Any, Callable, Dict, List, Optional

from app.core.logger_handler import logger

# 重排序专用线程池：CrossEncoder 无原生异步，需 run_in_executor。
# 使用独立小线程池（而非默认线程池），避免与 LLM 等任务竞争线程，
# 且重排序是 CPU 密集计算，2 个 worker 即可。
_rerank_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="rerank"
)

# HyDE 响应缓存 TTL（秒）
_HYDE_CACHE_TTL = 3600



class RagService:
    """
    RAG 核心服务
    
    完整管线流程：
    1. 路由决策 → 判断是否需要 RAG
    2. HyDE → LLM 生成假设性回答
    3. 双源检索 → 知识库 + 笔记库
    4. 重排序 → CrossEncoder
    5. 分批总结 → Top 3 并发总结
    6. 注入 Agent → 作为 system_prompt 上下文
    """
    
    def __init__(
        self,
        vector_store=None,
        chat_model=None,
        rerank_model=None,
        enable_summarize: bool = False,
    ):
        """
        初始化 RAG 服务
        
        Args:
            vector_store: 向量存储服务实例
            chat_model: LLM Chat 模型
            rerank_model: CrossEncoder 重排序模型
            enable_summarize: 是否启用 LLM 文档总结（False 时用截断替代，大幅降低延迟）
        """
        self.vector_store = vector_store  # 向量存储服务
        self.chat_model = chat_model      # LLM Chat 模型
        self.rerank_model = rerank_model  # CrossEncoder 重排序模型
        self.enable_summarize = enable_summarize  # LLM 总结开关
    
    async def query(
        self,  # 这个self是指当前类的实例对象
        query_text: str,    # 用户查询文本
        user_id: str,       # 用户 ID
        top_k: int = 3,     # 返回文档数量
        use_hyde: bool = True,  # 是否使用 HyDE 技术
        thinking_callback: Optional[Callable] = None,  # 思考过程推送回调函数
    ) -> Dict[str, Any]:  # 返回值是一个字典，包含上下文、来源和路由分数
        """
        执行完整的 RAG 查询管线
        
        Args:
            query_text: 用户查询文本
            user_id: 用户 ID
            top_k: 返回文档数量
            use_hyde: 是否使用 HyDE 技术
            thinking_callback: 思考过程推送回调函数
            
        Returns:
            {
                "context": str,          # 总结后的上下文
                "sources": List[dict],   # 引用的文档来源
                "route_score": float,    # 路由分数
            }
        """
        # 阶段 1: 推送思考状态
        await self._notify(thinking_callback, "thinking", "正在分析您的问题...")
        
        # 阶段 2: HyDE - 生成假设性回答
        hyde_text = query_text
        if use_hyde and self.chat_model:
            await self._notify(thinking_callback, "hyde", "生成假设性回答...")
            hyde_text = await self._generate_hyde(query_text, user_id)
            logger.debug(f"HyDE 生成: {hyde_text[:100]}...")
        
        # 阶段 3: 双源检索
        await self._notify(thinking_callback, "retrieval", "检索相关文档...")
        rag_results, note_results = await asyncio.gather(
            self.vector_store.search_documents(hyde_text, user_id, top_k=top_k * 2),
            self.vector_store.search_notes(hyde_text, user_id, top_k=top_k * 2),
        )
        
        # 合并结果并标记来源
        all_results = []
        for r in rag_results:
            r["source"] = "knowledge"
            all_results.append(r)
        for r in note_results:
            r["source"] = "notes"
            all_results.append(r)
        
        if not all_results:
            await self._notify(thinking_callback, "retrieval", "未找到相关文档")
            return {"context": "", "sources": [], "route_score": float('inf')}
        
        # 阶段 4: 重排序
        await self._notify(thinking_callback, "reorder", "对检索结果重排序...")
        if self.rerank_model:
            all_results = await self._rerank(query_text, all_results, top_k)
        else:
            all_results = all_results[:top_k]
        
        # 阶段 5: 分批总结 / 轻量截断
        if self.enable_summarize:
            await self._notify(thinking_callback, "summarize", "正在总结文档内容...")
            context = await self._summarize_documents(query_text, all_results[:top_k])
        else:
            await self._notify(thinking_callback, "summarize", "截取文档内容...")
            context = self._truncate_documents(all_results[:top_k])
        
        # 构建返回结果
        sources = [
            {
                "content": r["content"][:200],
                "source": r["source"],
                "score": r.get("score", 0),
                "metadata": r.get("metadata", {}),
            }
            for r in all_results[:top_k]
        ]
        
        return {
            "context": context,
            "sources": sources,
            "route_score": 0.0,
        }
    
    async def _generate_hyde(self, query: str, user_id: str = "") -> str:
        """
        HyDE: 让 LLM 生成假设性回答

        用假设性回答去向量库检索，比直接用问题检索更准确。
        相同用户 + 相同查询命中 Redis 缓存时跳过 LLM 调用。

        Args:
            query: 用户原始查询
            user_id: 用户 ID（进入缓存键，避免跨用户命中）

        Returns:
            假设性回答文本
        """
        try:
            # 先查 Redis 缓存（缓存异常静默降级，不影响主流程）
            cache_key = self._hyde_cache_key(query, user_id)
            cached = await self._get_hyde_cache(cache_key)
            if cached:
                logger.debug(f"HyDE 命中缓存: {cache_key}")
                return cached

            prompt = (
                "请根据以下问题，生成一个假设性的回答（不需要完全准确，"
                "用于帮助检索相关文档）：\n\n"
                f"问题：{query}\n\n"
                "假设性回答："
            )

            # ainvoke 走 ChatOpenAI 的共享异步 http 客户端（连接复用），
            # 同时避免 run_in_executor 的线程切换开销
            response = await self.chat_model.ainvoke(prompt)
            result = response.content if hasattr(response, 'content') else str(response)

            # 仅缓存成功结果（LLM 失败回退原文不入缓存）
            if result and result.strip():
                await self._set_hyde_cache(cache_key, result)

            return result

        except Exception as e:
            logger.warning(f"HyDE 生成失败，使用原始查询: {e}")
            return query

    def _hyde_cache_key(self, query: str, user_id: str) -> str:
        """生成 HyDE 缓存键（含模型名，防止切换模型后命中旧缓存）"""
        model_name = getattr(self.chat_model, "model_name", "unknown")
        query_hash = hashlib.md5(query.encode("utf-8")).hexdigest()[:16]
        return f"hyde:v1:{model_name}:{user_id}:{query_hash}"

    async def _get_hyde_cache(self, key: str) -> Optional[str]:
        """读取 HyDE 缓存；Redis 异常时静默降级返回 None"""
        try:
            from app.db.redis_client import get_redis
            cached = await get_redis().get(key)
            return cached or None
        except Exception:
            return None

    async def _set_hyde_cache(self, key: str, value: str) -> None:
        """写入 HyDE 缓存；Redis 异常时静默忽略"""
        try:
            from app.db.redis_client import get_redis
            await get_redis().set(key, value, ex=_HYDE_CACHE_TTL)
        except Exception:
            pass
    
    async def _rerank(self, query: str, documents: List[dict], top_k: int) -> List[dict]:
        """
        使用 CrossEncoder 对检索结果重排序
        
        Args:
            query: 查询文本
            documents: 检索结果列表
            top_k: 返回数量
            
        Returns:
            重排序后的结果列表
        """
        if not documents or not self.rerank_model:
            return documents[:top_k]
        
        try:
            # 构建 (query, document) 对
            pairs = [(query, doc["content"]) for doc in documents]

            # CrossEncoder 无原生异步，使用专用线程池避免与默认线程池竞争
            loop = asyncio.get_event_loop()
            scores = await loop.run_in_executor(
                _rerank_pool,
                lambda: self.rerank_model.compute_score(pairs)
            )
            
            # 按重排序分数排序
            for i, doc in enumerate(documents):
                doc["rerank_score"] = scores[i] if isinstance(scores, list) else scores
            
            documents.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
            
            return documents[:top_k]
            
        except Exception as e:
            logger.warning(f"重排序失败，使用原始排序: {e}")
            return documents[:top_k]
    
    async def _summarize_documents(self, query: str, documents: List[dict]) -> str:
        """
        分批总结检索到的文档
        
        对 Top 文档并发总结，然后合并生成最终上下文。
        
        Args:
            query: 用户查询
            documents: 检索结果列表
            
        Returns:
            合并后的上下文文本
        """
        if not documents:
            return ""
        
        if not self.chat_model:
            # 无 LLM 时直接拼接文档内容
            return "\n\n---\n\n".join(doc["content"] for doc in documents)
        
        # 并发总结每个文档
        tasks = [
            self._summarize_single(query, doc["content"])
            for doc in documents
        ]
        summaries = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 过滤异常结果并合并
        valid_summaries = [
            s for s in summaries if isinstance(s, str) and s.strip()
        ]
        
        return "\n\n".join(valid_summaries)
    
    async def _summarize_single(self, query: str, document: str) -> str:
        """
        总结单个文档
        
        Args:
            query: 用户查询
            document: 文档内容
            
        Returns:
            总结文本
        """
        try:
            prompt = (
                f"请根据以下文档内容，提取与问题相关的信息并简要总结：\n\n"
                f"问题：{query}\n\n"
                f"文档内容：\n{document[:2000]}\n\n"
                f"相关总结："
            )
            
            # ainvoke 走共享异步 http 客户端（连接复用），减少线程切换开销
            response = await self.chat_model.ainvoke(prompt)

            return response.content if hasattr(response, 'content') else str(response)
            
        except Exception as e:
            logger.warning(f"文档总结失败: {e}")
            return document[:500]
    
    def _truncate_documents(self, documents: List[dict], max_chars: int = 800) -> str:
        """
        轻量级文档截断（替代 LLM 总结，大幅降低延迟）
        
        将每篇文档截断到 max_chars 字符后直接拼接，
        让 Agent 在生成回答时自行理解原始内容。
        
        Args:
            documents: 检索结果列表
            max_chars: 每篇文档最大字符数
            
        Returns:
            拼接后的上下文文本
        """
        if not documents:
            return ""
        
        parts = []
        for doc in documents:
            content = doc.get("content", "")
            if len(content) > max_chars:
                content = content[:max_chars]
            parts.append(content)
        
        return "\n\n---\n\n".join(parts)
    
    async def _notify(self, callback: Optional[Callable], event_type: str, message: str) -> None:
        """
        推送思考过程事件
        
        Args:
            callback: 回调函数
            event_type: 事件类型（thinking/hyde/retrieval/reorder/summarize）
            message: 事件消息
        """
        if callback:
            try:
                await callback({"event": event_type, "message": message})
            except Exception as e:
                logger.debug(f"思考过程推送失败: {e}")
