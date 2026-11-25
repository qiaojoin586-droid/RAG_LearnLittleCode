"""
RAG 核心服务

实现完整的 RAG（Retrieval-Augmented Generation）管线：
- HyDE 技术：LLM 生成假设性回答提升检索准确率
- 双源检索：同时从知识库和笔记库检索
- 重排序：CrossEncoder 对检索结果重排序
- 分批总结：Top 3 文档并发总结后合并
- 思考过程推送：通过 callback 实时推送阶段事件
"""
from app.model_config.runtime import dispatch_fence, model_for, permit, template_for
from app.model_config.snapshots import current_snapshot, inference_actor
from app.model_config.contracts import content_hash
from app.model_config.task_budget import ModelBudgetDenied
from app.model_config.context_budget import ModelContextExceeded

import asyncio
from contextlib import contextmanager
import math
import os
import re
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional

from app.core.logger_handler import logger
from app.core.failed_response import BusinessError
from app.rag.blocking_io import BlockingPool
from app.rag.citations import assemble_evidence
from app.rag.keyword_search import KeywordHits, KeywordSearch, MAX_HITS
from app.rag.query_embeddings import share_query_embeddings
from app.rag.query_expansion import (GENERATION_SECONDS, MAX_OUTPUT_TOKENS, POLICY_REVISION,
                                     ExpansionGenerator, InvalidExpansion, skip_reason, validated_queries)
from app.rag.ranking import FUSION_POLICY, candidate_key, fuse_recall_lanes
from app.rag.read_tasks import retrieval_reads, run_until
from app.rag.visibility import CandidateVisibilityService
from app.rag.source_spans import KEYWORD_REVISION

# 重排序专用线程池：CrossEncoder 无原生异步，需 run_in_executor。
# 使用独立小线程池（而非默认线程池），避免与 LLM 等任务竞争线程，
# 且重排序是 CPU 密集计算，2 个 worker 即可。
_rerank_pool = BlockingPool('rerank', workers=2, queued=2)

TOTAL_SECONDS = FUSION_POLICY.total_seconds
RECALL_SECONDS = FUSION_POLICY.recall_seconds
HYDE_SECONDS = FUSION_POLICY.generation_seconds
RERANK_SECONDS = FUSION_POLICY.rerank_seconds
SUMMARY_SECONDS = FUSION_POLICY.summary_seconds


def retrieval_deadline(parent=None):
    end = asyncio.get_running_loop().time() + TOTAL_SECONDS
    return end if parent is None else min(end, parent)


def stage_deadline(end, seconds):
    now = asyncio.get_running_loop().time()
    reserve = min(0.25, max(0, end - now) / 5)
    return min(end - reserve, now + seconds)


def generation_deadline(end, seconds):
    now = asyncio.get_running_loop().time()
    deadline = stage_deadline(end, seconds)
    return deadline if deadline >= now + seconds else None


@contextmanager
def elapsed(timings, stage):
    started = perf_counter()
    try:
        yield
    finally:
        timings[stage] = round((perf_counter() - started) * 1000, 2)


class RetrievalUnavailable(RuntimeError):
    def __init__(self, reason, failed_sources=()):
        super().__init__(reason)
        self.failed_sources = list(failed_sources)
        self.timings_ms = {}

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
        visibility_checker=None,
        db_session_factory=None,
        *,
        keyword_search=None,
        enable_keyword: bool | None = None,
        expansion_generator: ExpansionGenerator | None = None,
        enable_published_expansion: bool = False,
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
        self.visibility = CandidateVisibilityService(db_session_factory)
        self.visibility_checker = visibility_checker or self.visibility.filter
        if enable_keyword is None:
            enable_keyword = os.getenv('RAG_KEYWORD_ENABLED', 'true').strip().lower() in ('1', 'true', 'yes')
        self.keyword_search = (keyword_search or KeywordSearch(db_session_factory)) if enable_keyword else None
        self.expansion_generator = expansion_generator
        self.enable_published_expansion = enable_published_expansion

    async def _search_pair(self, backend, query, owner, limit, end, slots, timings, prefix):
        async def search(method):
            async with slots:
                if asyncio.get_running_loop().time() >= end:
                    raise TimeoutError
                return await getattr(backend, method)(query, owner, top_k=limit)

        async def source(name, method):
            with elapsed(timings, prefix + name):
                if backend is None:
                    raise ConnectionError('Retrieval service is not initialized')
                return await retrieval_reads.until(end, search, method)

        return await asyncio.gather(source('knowledge', 'search_documents'), source('notes', 'search_notes'),
                                    return_exceptions=True)

    async def _vector_pair(self, query, owner, limit, end, slots, timings, prefix):
        async with share_query_embeddings(owner):
            return await self._search_pair(self.vector_store, query, owner, limit, end, slots, timings, prefix)

    async def _generate_rewrites(self, generator, query, end):
        try:
            reply = await retrieval_reads.until(end, generator, query, max_output_tokens=MAX_OUTPUT_TOKENS)
        except (ModelBudgetDenied, ModelContextExceeded, TimeoutError):
            raise
        except Exception as error:
            raise InvalidExpansion('generator_unavailable') from error
        return validated_queries(query, reply)

    async def _extra_recall(self, query, owner, limit, end, slots, use_hyde, callback, degraded, expansion, timings):
        generator = self.expansion_generator
        if generator is None and self.enable_published_expansion:
            from app.rag.query_expansion import published_generator
            generator = published_generator()
        if generator is not None:
            reason = (skip_reason(query) if self.keyword_search else 'keyword_disabled')
            if self.vector_store is None:
                reason = 'vector_unavailable'
            generate_end = generation_deadline(end, GENERATION_SECONDS)
            if not reason and generate_end is None:
                reason = 'insufficient_budget'
            if reason:
                expansion.update(status='skipped', reason=reason)
                return []
            try:
                with elapsed(timings, 'query_expansion'):
                    queries = await self._generate_rewrites(generator, query, generate_end)
            except TimeoutError:
                reason = 'timeout'
            except InvalidExpansion as error:
                reason = str(error)
            if reason:
                expansion.update(status='fallback', reason=reason)
                degraded.append('query_expansion')
                return []
            expansion.update(status='searched', query_count=len(queries))
            family = 'expanded'
        elif use_hyde and self.vector_store is not None and model_for('rag_summarize', self.chat_model):
            hyde_end = generation_deadline(end, HYDE_SECONDS)
            if hyde_end is None:
                degraded.append('hyde')
                return []
            try:
                await self._notify(callback, 'hyde', '生成假设性回答...', hyde_end)
                with elapsed(timings, 'hyde'):
                    alternate = await retrieval_reads.until(hyde_end, self._generate_hyde, query, owner)
            except (ModelBudgetDenied, ModelContextExceeded):
                raise
            except Exception as error:
                logger.warning(f'HyDE 不可用，采用原问题: {type(error).__name__}')
                degraded.append('hyde')
                return []
            if not isinstance(alternate, str) or not alternate.strip() or alternate.strip() == query.strip():
                return []
            family, queries = 'vector', [alternate]
        else:
            return []
        pairs = await asyncio.gather(*(self._vector_pair(q, owner, limit, end, slots, timings,
            f'expanded_{i}_' if family == 'expanded' else 'hyde_') for i, q in enumerate(queries, 1)))
        if family == 'expanded' and all(isinstance(result, BaseException) for pair in pairs for result in pair):
            expansion.update(status='fallback', reason='expanded_sources_unavailable')
            degraded.append('query_expansion')
        return [(family, i, pair) for i, pair in enumerate(pairs, 1)]

    def _source_limit(self, top_k):
        return MAX_HITS if self.keyword_search else min(MAX_HITS, top_k * 2)

    async def _recall(self, query, owner, top_k, use_hyde, callback, total_end, degraded, expansion, bindings, plan, timings):
        end = stage_deadline(total_end, RECALL_SECONDS)
        sources = ('knowledge', 'notes')
        limit = self._source_limit(top_k)
        slots = asyncio.Semaphore(FUSION_POLICY.concurrency)
        methods = ['vector']
        calls = [self._vector_pair(query, owner, limit, end, slots, timings, 'vector_')]
        if self.keyword_search:
            methods.append('keyword')
            calls.append(self._search_pair(self.keyword_search, query, owner, limit, end, slots, timings, 'keyword_'))
        # Schedule the original sources first. All waits, including the queue,
        # share one absolute deadline. Alternatives never call query recursively.
        calls.append(self._extra_recall(query, owner, limit, end, slots, use_hyde, callback, degraded, expansion, timings))
        *original, extra = await asyncio.gather(*calls, return_exceptions=True)
        if isinstance(extra, BaseException):
            raise extra
        lanes = [(method, 0, pair) for method, pair in zip(methods, original, strict=True)] + extra
        if extra and extra[0][0] == 'expanded':
            methods.append('expanded')
        flat, failed, successes = [], [], 0
        keyword_lag = set()
        for family, number, pair in lanes:
            if isinstance(pair, BaseException):
                pair = [pair] * len(sources)
            for source, rows in zip(sources, pair, strict=True):
                if isinstance(rows, (ModelBudgetDenied, ModelContextExceeded)):
                    raise rows
                if isinstance(rows, BaseException):
                    prefix = (f'expanded_{number}_' if family == 'expanded' else 'hyde_' if number else
                              'keyword_' if family == 'keyword' else '')
                    failed.append(prefix + source)
                    logger.warning(f'RAG source unavailable: {prefix}{source} ({type(rows).__name__})')
                    continue
                successes += 1
                if isinstance(rows, KeywordHits):
                    plan['keyword_index_versions'][source] = rows.revision
                    if rows.incomplete:
                        keyword_lag.add(source)
                take = min(limit, FUSION_POLICY.raw_hits - len(flat))
                flat.extend({**row, 'source': source, 'recall_method': family, 'subquery_id': number,
                             'keyword_index_revision': getattr(rows, 'revision', None),
                             'source_rank': rank} for rank, row in enumerate(rows[:take], 1))
        if not successes:
            raise RetrievalUnavailable('All retrieval sources unavailable', failed)

        # Authorize candidates from every source before they can vote, be
        # reranked or reach a model. An optional family contributes one list.
        versions = plan['keyword_index_versions']
        if len(set(versions.values())) > 1:
            keyword_lag.update(versions)
            flat = [row for row in flat if row['recall_method'] != 'keyword']
        verified = await retrieval_reads.until(total_end, self.visibility_checker, flat, owner)
        approved_hits = {(row['source'], row['source_rank']) for row in verified if row['recall_method'] == 'keyword'}
        keyword_lag.update(row['source'] for row in flat if row['recall_method'] == 'keyword'
                           and (row['source'], row['source_rank']) not in approved_hits)
        if keyword_lag:
            degraded.append('keyword_index_lag')
            failed.extend('keyword_' + source for source in sorted(keyword_lag))
        fused = fuse_recall_lanes(verified, limit, bindings)
        return fused, failed, methods
    
    async def query(self, query_text: str, user_id: str, top_k: int = 3, use_hyde: bool = False,
                    thinking_callback: Optional[Callable] = None, *, deadline: float | None = None) -> dict:
        end = retrieval_deadline(deadline)
        started = perf_counter()
        timings = {}
        status = 'error'
        try:
            result = await run_until(end, self._query, query_text, user_id, top_k, use_hyde, thinking_callback, end, timings)
            status = result['status']
            result['timings_ms'] = timings
            return result
        except TimeoutError as error:
            failure = RetrievalUnavailable('Retrieval deadline exceeded')
            failure.timings_ms = timings
            raise failure from error
        except RetrievalUnavailable as error:
            error.timings_ms = timings
            raise
        except BusinessError:
            raise
        except Exception as error:
            logger.warning(f'RAG 检索未完成: {type(error).__name__}')
            failure = RetrievalUnavailable('Retrieval could not complete')
            failure.timings_ms = timings
            raise failure from error
        finally:
            timings['total'] = round((perf_counter() - started) * 1000, 2)
            logger.debug(f'RAG 检索耗时 status={status} timings_ms={timings}')

    async def _query(
        self,  # 这个self是指当前类的实例对象
        query_text: str,    # 用户查询文本
        user_id: str,       # 用户 ID
        top_k: int,
        use_hyde: bool,
        thinking_callback: Optional[Callable],
        end: float,
        timings: dict,
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
        top_k = min(max(0, top_k), FUSION_POLICY.final)
        await retrieval_reads.until(end, self.visibility.authorize, user_id)
        snapshot = current_snapshot()
        bindings = snapshot.manifest.get('index_bindings', {}) if snapshot is not None else {}
        plan = {'config_release_id': snapshot.release_id if snapshot else None,
                'vector_index_versions': {key: row['index_version_id'] for key, row in bindings.items()},
                'keyword_index_versions': {}}
        await self._notify(thinking_callback, "thinking", "正在分析您的问题...", end)
        
        # 原问题始终参加召回，可选改写仅用于额外的向量检索。
        await self._notify(thinking_callback, "retrieval", "检索相关文档...", end)
        degraded = []
        expansion = {'policy_revision': POLICY_REVISION, 'status': 'off', 'query_count': 0}
        with elapsed(timings, 'recall_and_visibility'):
            all_results, failed_sources, methods = await self._recall(
                query_text, user_id, top_k, use_hyde, thinking_callback, end,
                degraded, expansion, bindings, plan, timings,
            )
        recall_info = {"failed_sources": failed_sources, "recall_methods": methods, "degraded_stages": degraded,
                       "retrieval_policy_revision_id": FUSION_POLICY.revision,
                       "query_expansion": expansion, "index_snapshot": plan}
        
        if not all_results:
            message = "部分检索来源暂不可用，当前没有可引用的资料" if failed_sources else "未找到相关文档"
            await self._notify(thinking_callback, "retrieval", message, end)
            return {"context": "", "sources": [], "route_score": float('inf'),
                    "status": "partial" if failed_sources else "empty", **recall_info}
        
        # 阶段 4: 重排序
        await self._notify(thinking_callback, "reorder", "对检索结果重排序...", end)
        rerank_state = {"status": "unavailable"}
        if self.rerank_model:
            all_results = await retrieval_reads.until(end, self._recheck, all_results[:FUSION_POLICY.rerank],
                                                      user_id, degraded)
            started = perf_counter()
            try:
                all_results = await run_until(stage_deadline(end, RERANK_SECONDS), self._rerank,
                                              query_text, all_results, top_k, rerank_state)
            except TimeoutError:
                rerank_state['status'] = 'timeout'
                all_results = all_results[:top_k]
            if rerank_state['status'] != 'success':
                degraded.append('rerank')
            timings['rerank'] = round((perf_counter() - started) * 1000, 2)
        else:
            all_results = all_results[:top_k]

        if self.enable_summarize and model_for('rag_summarize', self.chat_model):
            await self._notify(thinking_callback, "summarize", "正在总结文档内容...", end)
            summary_end = stage_deadline(end, SUMMARY_SECONDS)
            started = perf_counter()
            summaries = await asyncio.gather(
                *(retrieval_reads.until(summary_end, self._authorized_summary, summary_end, user_id, query_text, row)
                  for row in all_results),
                return_exceptions=True,
            )
            for summary in summaries:
                if isinstance(summary, (ModelBudgetDenied, ModelContextExceeded)):
                    raise summary
            rows = [{**row, 'summary': summary if isinstance(summary, str) else None}
                    for row, summary in zip(all_results, summaries, strict=True)]
            if any(not row['summary'] for row in rows):
                degraded.append('summarize')
            timings['summarize'] = round((perf_counter() - started) * 1000, 2)
            all_results = rows
        else:
            await self._notify(thinking_callback, "summarize", "截取文档内容...", end)
        all_results = await retrieval_reads.until(end, self._recheck, all_results, user_id, degraded)
        context, sources = assemble_evidence(all_results[:top_k])

        return {
            "context": context,
            "sources": sources,
            "route_score": 0.0,
            "status": ("partial" if failed_sources or 'evidence_changed' in degraded else "empty" if not all_results else
                       "degraded" if degraded else "success"),
            **recall_info,
            "rerank_status": rerank_state["status"],
        }
    
    async def _recheck(self, rows, owner, degraded=None):
        pinned = {candidate_key(row): row.get('source_locator') for row in rows}
        verified = await self.visibility_checker(rows, owner)
        accepted = []
        for row in verified:
            key = candidate_key(row)
            if key in pinned and row.get('source_locator') == pinned[key]:
                accepted.append(row)
        if len(accepted) < len(rows) and degraded is not None and 'evidence_changed' not in degraded:
            degraded.append('evidence_changed')
        return accepted

    async def _authorized_summary(self, end, owner, query, row):
        previous = dispatch_fence.get()
        async def authorize():
            if previous:
                await previous()
            if not await run_until(end, self._recheck, [row], owner):
                raise RetrievalUnavailable('Evidence no longer visible')
        await authorize()
        token = dispatch_fence.set(authorize)
        try:
            return await run_until(end, self._summarize_single, query, row['content'])
        finally:
            dispatch_fence.reset(token)

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
        role_model = model_for('rag_summarize', self.chat_model)
        # 先查 Redis 缓存（缓存异常静默降级，不影响主流程）
        cache_key = self._hyde_cache_key(query, user_id)
        cached = await self._get_hyde_cache(cache_key) if cache_key else None
        if cached:
            await permit(current_snapshot(), role_model.profile_id, self.visibility.session)
            return cached

        prompt = (
            "请根据以下问题，生成一个假设性的回答（不需要完全准确，"
            "用于帮助检索相关文档）：\n\n"
            f"问题：{query}\n\n"
            "假设性回答："
        )

        # ainvoke 走 ChatOpenAI 的共享异步 http 客户端（连接复用），
        # 同时避免 run_in_executor 的线程切换开销
        response = await role_model.ainvoke(prompt)
        result = response.content if hasattr(response, 'content') else str(response)
        if not isinstance(result, str) or not result.strip():
            raise ValueError('Empty HyDE response')

        # 仅缓存成功结果（LLM 失败回退原文不入缓存）
        if cache_key and not asyncio.current_task().cancelling():
            await self._set_hyde_cache(cache_key, result)

        return result

    def _hyde_cache_key(self, query: str, user_id: str) -> str | None:
        snapshot = current_snapshot()
        actor = inference_actor.get()
        if not snapshot or not actor or actor.get('user_id') != user_id:
            return None
        if re.search(r'今|昨|明[天年月]|近[期日来天周月年\d]|最近|最新|目前|当前|现在|上[个周月季度]|下[个周月季度]|本[周月季度年]|'
                     r'\b(today|yesterday|tomorrow|recent\w*|latest|current\w*|last|next|this|now|ago)\b', query, re.I):
            return None
        return 'hyde:v2:' + content_hash({'question': query, 'owner': user_id, 'session': actor.get('jti'),
            'release': snapshot.release_id, 'policy': FUSION_POLICY.revision, 'projection': KEYWORD_REVISION})

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
    
    async def _rerank(self, query: str, documents: List[dict], top_k: int,
                      state: Optional[dict] = None) -> List[dict]:
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
            def predict():
                scores = self.rerank_model.predict(pairs)
                if hasattr(scores, "tolist"):
                    scores = scores.tolist()
                if not isinstance(scores, (list, tuple)):
                    scores = [scores]
                if len(scores) != len(documents):
                    raise ValueError("Reranker score count mismatch")
                scores = [float(score) for score in scores]
                if not all(math.isfinite(score) for score in scores):
                    raise ValueError("Reranker produced non-finite scores")
                return scores

            scores = await _rerank_pool.run(predict)
            
            ranked = [{**doc, 'rerank_score': score} for doc, score in zip(documents, scores, strict=True)]
            ranked.sort(key=lambda row: row['rerank_score'], reverse=True)
            if state is not None:
                state["status"] = "success"
            
            return ranked[:top_k]
            
        except Exception as e:
            if state is not None:
                state["status"] = "failed"
            logger.warning(f"重排序失败，保留融合顺序: {type(e).__name__}")
            return documents[:top_k]
    
    async def _summarize_single(self, query: str, document: str) -> str | None:
        """
        总结单个文档
        
        Args:
            query: 用户查询
            document: 文档内容
            
        Returns:
            总结文本
        """
        role_model = model_for('rag_summarize', self.chat_model)
        try:
            prompt = (
                f"请根据以下文档内容，提取与问题相关的信息并简要总结：\n\n"
                f"问题：{query}\n\n"
                f"文档内容：\n{document[:2000]}\n\n"
                f"相关总结："
            )
            
            # ainvoke 走共享异步 http 客户端（连接复用），减少线程切换开销
            prompt = template_for("rag_summarize", prompt, query=query, document=document[:2000])
            response = await role_model.ainvoke(prompt)

            return response.content if hasattr(response, 'content') else str(response)
            
        except (ModelBudgetDenied, ModelContextExceeded):
            raise
        except Exception as e:
            logger.warning(f"文档总结失败: {type(e).__name__}")
            return None
    
    async def _notify(self, callback: Optional[Callable], event_type: str, message: str, end: float) -> None:
        """
        推送思考过程事件
        
        Args:
            callback: 回调函数
            event_type: 事件类型（thinking/hyde/retrieval/reorder/summarize）
            message: 事件消息
        """
        if callback:
            try:
                await retrieval_reads.until(end, callback, {"event": event_type, "message": message})
            except Exception as e:
                logger.debug(f"思考过程推送失败: {type(e).__name__}")
