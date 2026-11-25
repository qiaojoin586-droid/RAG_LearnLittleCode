"""
模型调用轻量 Trace（P0 可观测性）

用 LangChain CallbackHandler 在模型层统一埋点，一次覆盖所有经 factory 创建的模型实例：
- 每次 LLM 调用记录：请求 ID、用户、会话、阶段、模型名、输入/输出 token、延迟、成功/失败
- 输出为结构化 JSON 日志行（event=model_trace），便于 grep / 解析，后续可接 trace 服务
- 请求上下文（request_id / user_id / session_id / stage）用 contextvar 传递，
  由 chat_query 等入口设置，回调中自动读取；后台任务（标题/摘要）自行设置独立上下文

示例输出：
{"event": "model_trace", "request_id": "a1b2c3d4e5f6", "user_id": "ea475f05", "session_id": "0ea74769", "stage": "agent", "model": "qwen3.7-max-2026-06-08", "prompt_tokens": 312, "completion_tokens": 45, "total_tokens": 357, "latency_ms": 5234, "success": true}
"""

import asyncio
import contextvars
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler

from app.core.logger_handler import logger
from app.core.request_context import current_request_id

# 当前请求的追踪上下文（contextvar：每个请求独立，不跨请求污染）
_trace_ctx: contextvars.ContextVar[Optional[Dict[str, str]]] = contextvars.ContextVar(
    "model_trace_ctx", default=None
)


def set_trace_context(
    request_id: str,
    user_id: str = "",
    session_id: str = "",
    stage: str = "chat",
) -> None:
    """设置当前请求的追踪上下文（chat_query 等入口调用）"""
    _trace_ctx.set({
        "request_id": request_id,
        "user_id": user_id,
        "session_id": session_id,
        "stage": stage,
    })


def clear_trace_context() -> None:
    """清除当前请求的追踪上下文（入口 finally 调用）"""
    _trace_ctx.set(None)


def set_trace_stage(stage: str) -> None:
    """更新当前阶段标记（classify/agent/plan_execute/title/summary 等）"""
    ctx = _trace_ctx.get()
    if ctx:
        _trace_ctx.set({**ctx, "stage": stage})


@contextmanager
def trace_scope(**values):
    token = _trace_ctx.set({**(_trace_ctx.get() or {}), **values})
    try:
        yield
    finally:
        _trace_ctx.reset(token)


def get_trace_context() -> Optional[Dict[str, str]]:
    """获取当前请求的追踪上下文（供工具审计等复用 user_id/session_id）"""
    return _trace_ctx.get()


def new_request_id() -> str:
    """Reuse the HTTP request ID; standalone work receives a 32-hex ID."""
    return current_request_id()


# ============================================================
# Trace 输出通道（sink，P2-2 成本账单）
# ============================================================

class TraceSink:
    """Trace 输出目标（同步 emit 接口，由 _emit 分发）"""

    def emit(self, event: dict) -> None:
        raise NotImplementedError


class LogSink(TraceSink):
    """输出结构化 JSON 日志行（默认通道，始终启用）"""

    def emit(self, event: dict) -> None:
        logger.info(json.dumps({"event": "model_trace", **event}, ensure_ascii=False))


class _QueueSink(TraceSink):
    """
    带 asyncio 队列与 worker 的批量输出 sink 基类（DbSink / LangfuseSink 共用）

    同步 emit 仅入队，不阻塞请求路径；worker 攒批后调用子类 _flush。
    队列满时丢弃并告警（可观测性数据不允许拖垮业务链路）。
    """

    _POLL_INTERVAL = 1.0   # 空闲轮询间隔（秒）
    _BATCH_SIZE = 100      # 单批最大条数

    def __init__(self, maxsize: int = 1000):
        self._queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=maxsize)
        self._stop = asyncio.Event()
        self._worker: Optional["asyncio.Task"] = None

    def start(self) -> None:
        """启动 worker（重复调用幂等）"""
        if self._worker is None or self._worker.done():
            self._stop.clear()
            self._worker = asyncio.create_task(
                self._run(), name=f"model_trace_{type(self).__name__}"
            )
            logger.debug(f"model_trace {type(self).__name__} worker 已启动")

    async def stop(self) -> None:
        """停止 worker：先排空队列再退出（超时 5s 强制取消）"""
        self._stop.set()
        if self._worker and not self._worker.done():
            try:
                await asyncio.wait_for(self._worker, timeout=5)
            except Exception:
                self._worker.cancel()
                logger.warning(f"model_trace {type(self).__name__} worker 停止超时，强制取消")

    def emit(self, event: dict) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                f"model_trace {type(self).__name__} 队列已满（maxsize={self._queue.maxsize}），丢弃 1 条 trace"
            )

    async def _run(self) -> None:
        """worker 主循环：攒批 → 调用 _flush；停止且队列空时退出"""
        while True:
            if self._stop.is_set() and self._queue.empty():
                break
            batch: List[dict] = []
            try:
                batch.append(
                    await asyncio.wait_for(self._queue.get(), timeout=self._POLL_INTERVAL)
                )
            except asyncio.TimeoutError:
                continue  # 空闲，回到循环头检查停止条件
            while len(batch) < self._BATCH_SIZE:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await self._flush(batch)

    async def _flush(self, batch: List[dict]) -> None:
        raise NotImplementedError


class DbSink(_QueueSink):
    """DB 落库通道（P2-2 成本账单）"""

    async def _flush(self, batch: List[dict]) -> None:
        # 延迟导入，避免 core 层与 db/models 层循环依赖
        from app.db.database import async_session_factory
        from app.models.model_trace import ModelTrace
        from app.model_config.pricing import price_trace_batch, trace_row

        try:
            async with async_session_factory() as db:
                try:
                    rows = await price_trace_batch(db, batch)
                except Exception as error:
                    await db.rollback()
                    logger.warning(f'模型价格查询失败，保留用量记录：{type(error).__name__}')
                    rows = [trace_row(event) for event in batch]
                db.add_all(ModelTrace(**row) for row in rows)
                await db.commit()
        except Exception as e:
            logger.warning(f"model_trace 落库失败（丢弃 {len(batch)} 条）: {type(e).__name__}")


class LangfuseSink(_QueueSink):
    """
    Langfuse trace 平台转发通道（P2-5）

    复用 _emit 事件（与 Log/Db 通道同源），worker 中批量调用 langfuse SDK：
    - trace_id = request_id，user_id/session_id/stage 进 metadata（v4 API 无 trace 级字段）
    - 未配置 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 时由 start_trace_bus 跳过
    - SDK 缺失/平台不可达时仅告警丢弃，不影响其他通道
    """

    async def _flush(self, batch: List[dict]) -> None:
        try:
            # langfuse 同步客户端内部自带缓冲线程，放到线程池执行避免阻塞事件循环
            await asyncio.to_thread(self._send_batch, batch)
        except Exception as e:
            logger.warning(f"model_trace Langfuse 转发失败（丢弃 {len(batch)} 条）: {e}")

    def _send_batch(self, batch: List[dict]) -> None:
        from langfuse import Langfuse
        from langfuse.types import TraceContext

        client = _langfuse_client()
        end_ts = int(time.time() * 1000)
        for e in batch:
            latency_ms = e.get("latency_ms")
            start_ts = end_ts - latency_ms if latency_ms else end_ts
            usage = {
                "input": e.get("prompt_tokens"),
                "output": e.get("completion_tokens"),
                "total": e.get("total_tokens"),
            }
            obs = client.start_observation(
                name=f"llm.{e.get('stage') or 'chat'}",
                as_type="generation",
                # 后台任务可能无 request_id，兜底生成保证 trace_id 非空
                trace_context=TraceContext(trace_id=e.get("request_id") or uuid.uuid4().hex[:12]),
                model=e.get("model"),
                metadata={
                    "stage": e.get("stage"),
                    "user_id": e.get("user_id"),
                    "session_id": e.get("session_id"),
                    "request_id": e.get("request_id"),
                },
                usage_details={k: v for k, v in usage.items() if v is not None},
                level="ERROR" if not e.get("success", True) else "DEFAULT",
                completion_start_time=datetime.fromtimestamp(start_ts / 1000),
            )
            obs.end(end_time=end_ts)
        client.flush()  # 本批立即提交（SDK 内部亦有定时 flush）


# langfuse 客户端单例（惰性创建，从 LANGFUSE_* 环境变量读取配置）
_langfuse_client_instance: Optional[object] = None


def _langfuse_client():
    """获取 langfuse 同步客户端单例（线程安全，内部自带缓冲线程）"""
    global _langfuse_client_instance
    if _langfuse_client_instance is None:
        from langfuse import Langfuse

        _langfuse_client_instance = Langfuse()
    return _langfuse_client_instance


# 默认通道：日志始终保留
_trace_sinks: List[TraceSink] = [LogSink()]


def _emit(event: dict) -> None:
    """分发 trace 事件到所有通道（单通道故障不影响其他通道）"""
    for sink in _trace_sinks:
        try:
            sink.emit(event)
        except Exception as e:
            logger.warning(f"model_trace sink 输出失败: {type(e).__name__}: {e}")


def price_time(latency_ms=0):
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(milliseconds=latency_ms or 0)).isoformat()


def record_config_test_call(context: dict, profile: dict, *, success: bool, usage: dict | None,
                            latency_ms: int, error: str | None, price_at=None):
    """Only fixed metadata and usage reach sinks; never prompts, headers or replies."""
    from app.model_config.endpoints import canonical_url
    prompt,completion = (usage.get('prompt_tokens'),usage.get('completion_tokens')) if isinstance(usage,dict) else (None,None)
    known = all(isinstance(value,int) and not isinstance(value,bool) and value>=0 for value in (prompt,completion))
    event = {key:context.get(key) for key in ('user_id','profile_revision_id','credential_revision_id',
                                           'credential_version','step_id')}
    event.update(request_id=current_request_id(),stage='config_test',model=profile['model'][:100],
        is_test=True,adapter_type=profile['adapter_type'],provider_origin=canonical_url(profile['base_url'])[0],
        success=success,prompt_tokens=prompt if known else None,completion_tokens=completion if known else None,
        total_tokens=prompt+completion if known else None,usage_status='complete' if known else 'unknown',
        latency_ms=latency_ms,error=error,cost_amount=None,price_version_id=None,pricing_status='unknown',
        _price_at=price_at or price_time(latency_ms))
    _emit(event)


def record_discarded_chat_attempt(context, profile, reason, latency_ms, price_at=None):
    """Account for the discarded attempt separately from the final model trace."""
    from app.model_config.endpoints import canonical_url
    from app.model_config.snapshots import inference_actor

    trace = _trace_ctx.get() or {}
    actor = inference_actor.get() or {}
    _emit({**context, 'request_id': trace.get('request_id') or current_request_id(),
        'user_id': actor.get('user_id', ''), 'session_id': trace.get('session_id'),
        'stage': 'model_retry', 'model': profile['model'][:100],
        'adapter_type': profile['adapter_type'], 'provider_origin': canonical_url(profile['base_url'])[0],
        'success': False, 'is_test': False, 'latency_ms': latency_ms, 'error': reason,
        'usage_status': 'unknown', 'pricing_status': 'unknown', '_price_at': price_at or price_time(latency_ms)})


def embedding_prompt_tokens(response,protocol):
    usage=response.get('usage',{}) if isinstance(response,dict) else {}
    tokens=usage.get('prompt_tokens') if isinstance(usage,dict) else None
    if protocol=='ollama_embed' and isinstance(response,dict):tokens=response.get('prompt_eval_count')
    return tokens if type(tokens) is int and tokens>=0 else None


def record_embedding_call(snapshot,index,profile,purpose,success,response,latency_ms,error,price_at=None,*,reported_tokens=None):
    """Vector attempt metadata only; no document/query text, vectors or Key."""
    from app.model_config.snapshots import inference_actor
    from app.model_config.endpoints import canonical_url
    tokens=reported_tokens if reported_tokens is not None else embedding_prompt_tokens(response,profile['api_protocol'])
    known=tokens is not None
    credential=snapshot.credentials.get(profile['credential_revision_id'])
    _emit({'request_id':current_request_id(),'user_id':(inference_actor.get() or {}).get('user_id',''),
        'stage':'embedding','model':profile['model'][:100],'release_id':snapshot.release_id,
        'profile_revision_id':profile['id'],'credential_revision_id':profile['credential_revision_id'],
        'credential_version':credential['credential_version'] if credential else None,
        'adapter_type':profile['adapter_type'],'provider_origin':canonical_url(profile['base_url'])[0],
        'index_context':{'index_version_id':index['id'],'semantic_fingerprint':index['semantic_fingerprint'],'purpose':purpose},
        'is_test':False,'success':success,'latency_ms':latency_ms,'error':error,
        'prompt_tokens':tokens if known else None,'completion_tokens':0 if known else None,
        'total_tokens':tokens if known else None,'usage_status':'complete' if known else 'unknown',
        'cost_amount':None,'price_version_id':None,'pricing_status':'unknown', '_price_at':price_at or price_time(latency_ms)})


def start_trace_bus() -> None:
    """
    启动 trace 输出总线（应用启动时调用）

    TRACE_SINK 环境变量控制通道，逗号分隔：log / db / langfuse（P2-5）。
    默认 "log,db"：日志 + 成本账单落库。
    langfuse 通道需配置 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY（缺失时跳过并告警）。
    """
    enabled = {s.strip() for s in os.getenv("TRACE_SINK", "log,db").split(",") if s.strip()}
    # 幂等：重复调用不追加重复 sink
    if "db" in enabled and not any(isinstance(s, DbSink) for s in _trace_sinks):
        _db_sink = DbSink()
        _db_sink.start()
        _trace_sinks.append(_db_sink)
    if "langfuse" in enabled and not any(isinstance(s, LangfuseSink) for s in _trace_sinks):
        if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
            try:
                _langfuse_sink = LangfuseSink()
                _langfuse_sink.start()
                _trace_sinks.append(_langfuse_sink)
                logger.info(
                    f"model_trace LangfuseSink 已启用（host={os.getenv('LANGFUSE_HOST', 'https://cloud.langfuse.com')}）"
                )
            except Exception as e:
                logger.warning(f"LangfuseSink 初始化失败，langfuse 通道不可用: {e}")
        else:
            logger.warning(
                "TRACE_SINK 含 langfuse 但未配置 LANGFUSE_PUBLIC_KEY/SECRET_KEY，langfuse 通道跳过"
            )
    logger.info(f"model_trace 输出总线已启动: TRACE_SINK={sorted(enabled)}")


async def stop_trace_bus() -> None:
    """停止 trace 输出总线（应用关闭时调用，先排空各队列）"""
    for sink in list(_trace_sinks):
        if isinstance(sink, _QueueSink):
            await sink.stop()


class ModelTraceCallbackHandler(BaseCallbackHandler):
    """
    LangChain 回调：记录每次 LLM 调用的模型 / 延迟 / token / 成败

    通过 factory.py 创建模型时统一挂载（callbacks=[_TRACE_CALLBACK]），
    无需修改任何调用点。run_id 关联 start/end（asyncio 单线程下 dict 操作安全）。
    """

    def __init__(self) -> None:
        # run_id(id) -> 开始时间戳
        self._started: Dict[int, float] = {}
        # run_id(id) -> 模型名
        self._models: Dict[int, str] = {}

    # ---------- 私有 ----------

    @staticmethod
    def _run_key(kwargs: dict) -> int:
        return id(kwargs.get("run_id"))

    @staticmethod
    def _extract_model(serialized: dict, **kwargs) -> str:
        """从序列化信息提取模型名（kwargs.model / model_name / 类名 / run name 兜底）"""
        if serialized is None:
            return "unknown"
        serialized_kwargs = serialized.get("kwargs") or {}
        model = (
            serialized_kwargs.get("model")
            or serialized_kwargs.get("model_name")
            or serialized.get("name")
            or kwargs.get("name")  # 兜底：run name（如 ChatOpenAI 类名），避免 "unknown"
        )
        return str(model) if model else "unknown"

    def _snapshot(self, model: str, success: bool, usage: Optional[dict], latency_ms: Optional[int], error: Optional[str] = None) -> None:
        ctx = _trace_ctx.get() or {}
        _emit({
            "request_id": ctx.get("request_id"),
            # user_id/session_id 全量输出：DB 落库按用户/会话聚合（P2-2 账单），
            # 截断会导致前缀碰撞、费用串户（日志可读性不受影响）
            "user_id": ctx.get("user_id") or "",
            "session_id": ctx.get("session_id") or None,
            "stage": ctx.get("stage", "chat"),
            "model": ctx.get("model_override", model)[:100],
            **{key:ctx[key] for key in ('release_id','profile_revision_id','prompt_revision_id',
                'credential_revision_id','credential_version','adapter_type','provider_origin','workflow_id','step_id','memory_context') if key in ctx},
            **({'pricing_status':'unknown','cost_amount':None,'price_version_id':None,
                '_price_at':ctx.get('price_at') or price_time(latency_ms),
                'usage_status':'complete' if usage and all(usage.get(key) is not None for key in ('prompt_tokens','completion_tokens')) else 'unknown'}
               if ctx.get('release_id') else {}),
            "prompt_tokens": (usage or {}).get("prompt_tokens"),
            "completion_tokens": (usage or {}).get("completion_tokens"),
            "total_tokens": (usage or {}).get("total_tokens"),
            "latency_ms": latency_ms,
            "success": success,
            "error": error,
        })

    # ---------- LangChain 回调接口 ----------

    def on_llm_start(self, serialized: Dict, prompts: list, **kwargs) -> None:
        key = self._run_key(kwargs)
        self._started[key] = time.monotonic()
        self._models[key] = self._extract_model(serialized, **kwargs)

    def on_llm_end(self, response, **kwargs) -> None:
        key = self._run_key(kwargs)
        start = self._started.pop(key, None)
        latency_ms = round((time.monotonic() - start) * 1000) if start else None
        model = self._models.pop(key, "unknown")
        usage = self._extract_usage(response)
        self._snapshot(model=model, success=True, usage=usage, latency_ms=latency_ms)

    @staticmethod
    def _extract_usage(response) -> dict:
        """
        提取 token 用量（纯函数，便于单测）

        优先 `llm_output.token_usage`；为空时回退到消息的 `usage_metadata`。
        后者覆盖"模型 streaming=True 时 ainvoke 实际走 agenerate_from_stream"的场景
        ——此时 llm_output 不含 token_usage，此前会导致成本恒为 0
        （README「已知限制：流式 Token 统计」）。
        """
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("token_usage") or {}
        if usage:
            return usage

        try:
            message = response.generations[0][0].message
        except (AttributeError, IndexError, TypeError):
            return {}

        meta = getattr(message, "usage_metadata", None) or {}
        if not meta:
            return {}
        return {
            "prompt_tokens": meta.get("input_tokens"),
            "completion_tokens": meta.get("output_tokens"),
            "total_tokens": meta.get("total_tokens"),
        }

    def on_llm_error(self, error: Exception, *, usage=None, **kwargs) -> None:
        key = self._run_key(kwargs)
        start = self._started.pop(key, None)
        latency_ms = round((time.monotonic() - start) * 1000) if start else None
        model = self._models.pop(key, "unknown")
        self._snapshot(model=model, success=False, usage=usage, latency_ms=latency_ms, error=str(error)[:200])

    def discard_start(self, **kwargs) -> None:
        key = self._run_key(kwargs)
        self._started.pop(key, None)
        self._models.pop(key, None)


# 全局共享回调实例（factory 创建模型时挂载）
TRACE_CALLBACK = ModelTraceCallbackHandler()
