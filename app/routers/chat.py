"""
聊天路由

端点：
- POST /chat/query - Agent 流式对话
- POST /chat/rag - RAG 查询
- GET /chat/sessions - 会话列表
- DELETE /chat/sessions/{session_id} - 删除会话
- GET /chat/{session_id}/messages - 消息历史（游标分页）
- PUT /chat/{session_id}/title - 修改会话标题
"""

import asyncio
from anyio import move_on_after
from app.model_config.runtime import model_for, template_for
from app.model_config.snapshots import current_snapshot
from app.model_config.task_budget import ModelBudgetDenied, current_model_budget
from app.model_config.context_budget import ModelContextExceeded
from app.model_config.prompt_contracts import FIXED_SAFETY
from contextlib import aclosing
import json
import os
import re
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, UploadFile, File, Header, Request, Response
from fastapi.responses import StreamingResponse, FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger_handler import logger
from app.core.success_response import success_response
from app.core.rate_limit import rate_limit
from app.core.task_runner import spawn_background_task
from app.core.failed_response import BusinessError, ErrorCode
from app.core.model_trace import set_trace_context, new_request_id
from app.db.database import get_db, async_session_factory
from app.schemas.chat import (
    ApprovalDecision, QueryRequest, RAGRequest, ChatSessionListResponse,
    MessageListResponse, SessionTitleUpdate, UploadResponse,
)
from app.utils.auth_utils import get_current_user_id
from app.utils.time_utils import to_utc_iso
from app.utils.file_handler import read_upload_limited
from app.ai_service.chat_route import ChatRouteContext
from app.ai_service.chat_graph import stream_chat_graph
from app.ai_service.detached_chat import cancel_detached_chat, find_detached_chat
from app.ai_service.detached_worker import enqueue_chat, lock_chat_admission
from app.ai_service.tool_operations import ToolTurn, authorize_turn
from app.ai_service.tool_approvals import decide_approval, list_approvals
from app.ai_service.email_dispatch import list_email_operations, mark_email_received
from app.ai_service.workflows import ChatWorkflow, read_workflow
from app.ai_service.workflow_events import RAG_NOTICES, WorkflowJournal, event_frame, read_workflow_events
from app.services.chat_service import ChatService
from app.services.database_session_manager import DatabaseSessionManager
from app.services.chat_attachment_service import ChatAttachmentService
from app.rag.citations import POLICY as CITATION_POLICY, CitationTurn, confirm_citations, current_citations, read_citation
from app.rag.rag_service import RagService, RetrievalUnavailable

router = APIRouter()

# 服务实例
chat_service = ChatService()
session_manager = DatabaseSessionManager()
chat_attachment_service = ChatAttachmentService()

# LLM 流式响应超时时间（秒）
LLM_STREAM_TIMEOUT = int(os.getenv("LLM_STREAM_TIMEOUT", "60"))
_CASUAL_GREETING = re.compile(
    r'(?:你好|您好|大家好|早上好|下午好|晚上好|嗨|哈喽|谢谢你?|hello|hi|hey|thanks|thank\s+you)[\s!！?？.。~～,，]*',
    re.IGNORECASE,
)

# SSE 单用户最大并发连接数
# 防止恶意用户通过大量 SSE 连接耗尽服务器资源
SSE_MAX_CONNECTIONS_PER_USER = 3
# 进程内兜底计数（Redis 不可用时降级；多进程部署以 Redis 计数为准，审查 M11）
_sse_active_counts: dict[str, int] = {}


async def _acquire_sse_slot(user_id: str, *, background: bool = False) -> bool:
    """
    获取 SSE 连接槽位（Redis 计数优先，多进程部署天然生效）

    返回 True=获得槽位。Redis 不可用时降级进程内计数（保证聊天可用）。
    """
    from app.db.redis_client import get_redis

    try:
        redis = get_redis()
        key = f"sse_conn:{user_id}"
        count = await redis.incr(key)
        if count > SSE_MAX_CONNECTIONS_PER_USER:
            await redis.decr(key)  # 回退计数
            logger.warning(f"SSE 连接数超限（Redis）: user={user_id[:8]}, count={count}")
            return False
        if count == 1 or background:
            await redis.expire(key, 240 if background else 120)
        return True
    except Exception as e:
        # Redis 故障降级：进程内计数
        logger.warning(f"SSE Redis 计数不可用，降级进程内: {e}")

    if _sse_active_counts.get(user_id, 0) >= SSE_MAX_CONNECTIONS_PER_USER:
        logger.warning(f"SSE 连接数超限（进程内）: user={user_id[:8]}")
        return False
    _sse_active_counts[user_id] = _sse_active_counts.get(user_id, 0) + 1
    return True


async def _release_sse_slot(user_id: str) -> None:
    """释放 SSE 连接槽位（与 _acquire_sse_slot 对称；Redis 计数由 TTL 兜底清理）"""
    from app.db.redis_client import get_redis

    try:
        await get_redis().decr(f"sse_conn:{user_id}")
        return
    except Exception as e:
        logger.debug(f"SSE Redis 计数释放失败（TTL 自动清理兜底）: {e}")
    _sse_active_counts[user_id] = max(0, _sse_active_counts.get(user_id, 1) - 1)


class ChatStreamingResponse(StreamingResponse):
    def __init__(self, stream, headers, release_slot):
        super().__init__(stream, media_type='text/event-stream', headers=headers)
        self.release_slot = release_slot

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                with move_on_after(5, shield=True):
                    await self.body_iterator.aclose()
            finally:
                await self.release_slot()


def _estimate_attachment_tokens(attachments) -> int:
    """
    估算当前消息附件的 token 消耗（用于历史配额扣减，优先保证当前消息）

    图片按分辨率估算；视频按抽帧数 × 2000 估算。
    """
    from app.services.token_budget import TokenCounter

    total = 0
    video_frames = int(os.getenv("VIDEO_FRAME_COUNT", "8"))
    for att in attachments:
        if att.file_type == "image":
            total += TokenCounter.count_image(att.width, att.height)
        elif att.file_type == "video":
            total += video_frames * 2000
    return total


@router.post("/chat/query", summary="Agent 流式对话", dependencies=[Depends(rate_limit(endpoint_limit=10))])
async def chat_query(
    data: QueryRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """
    Agent 流式对话接口
    
    支持 SSE 流式输出，包含思考过程推送。
    如果 session_id 为空，自动创建新会话。
    
    注意：不使用请求级 db session（Depends(get_db)），
    因为 StreamingResponse 返回后请求上下文会关闭 db，
    导致后续异步任务中 db.execute() 报 NoneType 错误。
    所有数据库操作均使用独立 session。
    """
    background = data.continue_on_disconnect
    snapshot = current_snapshot()
    if background and snapshot is None:
        raise BusinessError(50305, message='后台对话需要先发布模型配置', http_status=503)
    if background and await request.is_disconnected():
        return Response(status_code=204)
    if not await _acquire_sse_slot(user_id, background=background):
        async def limit_stream():
            yield event_frame({'type': 'error', 'content': f'并发连接数已达上限（最多 {SSE_MAX_CONNECTIONS_PER_USER} 个）'})
        return StreamingResponse(limit_stream(), media_type='text/event-stream')

    released = handed_off = False

    async def release_slot():
        nonlocal released
        if not released:
            released = True
            with move_on_after(2, shield=True):
                await _release_sse_slot(user_id)

    try:
        headers = {'Cache-Control': 'no-store', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'}
        try:
            session_id, message_id, attachments, workflow_id = await _persist_chat_turn(
                data, user_id, snapshot.release_id if background else None,
            )
        except BusinessError as error:
            if background:
                raise
            async def user_message_error(code, message):
                yield event_frame({'type': 'error', 'code': code, 'content': message})
            return StreamingResponse(user_message_error(error.code, error.message), media_type='text/event-stream')
        except Exception as error:
            logger.warning(f'会话消息写入失败: error_type={type(error).__name__}')
            if background:
                raise BusinessError(ErrorCode.INTERNAL_ERROR, message='会话创建失败，请稍后重试', http_status=500) from None
            async def err_stream():
                yield event_frame({'type': 'error', 'content': '会话创建失败，请稍后重试'})
            return StreamingResponse(err_stream(), media_type='text/event-stream')

        set_trace_context(new_request_id(), user_id=user_id, session_id=session_id, stage='chat')
        if workflow_id is not None:
            headers['X-Workflow-Id'] = workflow_id
            stream = _queued_chat_frames(workflow_id, user_id)
        else:
            stream = _accepted_chat_frames(data, user_id, session_id, message_id, attachments)
        response = ChatStreamingResponse(stream, headers, release_slot)
        handed_off = True
        return response
    finally:
        if not handed_off:
            await release_slot()


async def _accepted_chat_frames(data, user_id, session_id, message_id, attachments):
    yield event_frame({'type': 'thinking', 'stage': 'accepted', 'content': '已收到问题，正在准备回答。'})
    try:
        stream = await _prepare_chat_stream(data, user_id, session_id, message_id, attachments)
    except (ModelBudgetDenied, ModelContextExceeded) as error:
        yield event_frame(error.stream_event())
        return
    except Exception as error:
        logger.warning(f'聊天准备失败: {type(error).__name__}')
        yield event_frame({'type': 'error', 'content': '生成失败，请稍后重试'})
        return
    async with aclosing(stream):
        async for frame in stream:
            yield frame


def _retrieval_service(manager):
    service = getattr(manager, 'rag_service', None)
    if service is not None:
        return service
    from app.rag.vector_store import VectorStoreService
    from app.utils.config import get_rag_config
    # The keyword index can already serve requests during vector initialization.
    vectors = VectorStoreService() if manager.stage2_complete.is_set() else None
    config = get_rag_config()
    return RagService(vectors, chat_model=manager.chat_model, rerank_model=getattr(manager, 'reorder_service', None),
                      enable_summarize=config.get('enable_summarize', False),
                      enable_published_expansion=config.get('query_expansion_enabled') is True)


async def _persist_chat_turn(data, user_id, release_id):
    async with async_session_factory() as db:
        async with db.begin():
            if release_id is not None:
                await lock_chat_admission(db, user_id)
            session_id = await chat_service.get_or_create_session(db, user_id, data.session_id)
            attachments = await chat_attachment_service.get_owned_list(db, user_id, data.attachment_ids)
            attachment_ids = [item.file_id for item in attachments]
            await chat_attachment_service.bind_session(db, user_id, attachment_ids, session_id)
            metadata = [{
                'file_id': item.file_id, 'file_type': item.file_type, 'original_name': item.original_name,
                'file_size': item.file_size, 'mime_type': item.mime_type, 'width': item.width,
                'height': item.height, 'duration_sec': item.duration_sec,
            } for item in attachments]
            attachment_only = bool(attachments) and not data.message.strip()
            content = data.message
            if attachment_only:
                content = '[视频]' if any(item.file_type == 'video' for item in attachments) else '[图片]'
            message = await chat_service.session_manager.add_message(
                db, session_id, user_id, 'user', content, data.idempotency_key,
                attachments_json=metadata or None, cache_immediately=False,
            )
            workflow_id = None
            if release_id is not None:
                await chat_attachment_service.bind_message(db, user_id, attachment_ids, session_id, message.id)
                workflow_id = await enqueue_chat(db, user_id, ToolTurn(session_id, message.id), release_id,
                                                 thinking=data.enable_thinking, attachment_only=attachment_only,
                                                 allow_actions=data.allow_background_actions)
        await chat_service.session_manager.cache_message(session_id, message)
        return session_id, message.id, attachments, workflow_id


async def _queued_chat_frames(workflow_id, user_id):
    # The first response carries the durable ID. A remote or backlogged Worker
    # can be followed through the same cursor API after this relay closes.
    yield ': accepted\n\n'
    for _ in range(15):
        run = find_detached_chat(workflow_id, user_id)
        if run is not None:
            async with aclosing(run.frames()) as frames:
                async for frame in frames:
                    yield frame
            return
        await asyncio.sleep(0.1)


async def stream_queued_chat(job, user_id, session_id):
    from datetime import timezone
    from app.models.chat import ChatMessage
    from sqlalchemy import select

    async with async_session_factory() as db:
        message = (await db.execute(select(ChatMessage).where(
            ChatMessage.id == job.turn_message_id, ChatMessage.session_id == session_id, ChatMessage.role == 'user',
        ))).scalar_one()
        file_ids = [item['file_id'] for item in (message.attachments_json or [])]
        attachments = await chat_attachment_service.get_owned_list(db, user_id, file_ids)
        if len(attachments) != len(file_ids) or any(item.session_id != session_id for item in attachments):
            raise BusinessError(ErrorCode.INVALID_PARAMETER, message='任务附件已不可用', http_status=409,
                                data={'reason': 'DETACHED_ATTACHMENT_UNAVAILABLE'})
    # Rehydrate only from the original message and its authoritative attachments.
    data = QueryRequest(message='' if job.options['attachment_only'] else message.content,
                        attachment_ids=file_ids, enable_thinking=job.options['thinking'],
                        continue_on_disconnect=True, memory_mode='off')
    prompt_time = job.created_at.replace(tzinfo=timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    stream = await _prepare_chat_stream(data, user_id, session_id, job.turn_message_id, attachments, job.workflow,
                                        prompt_time=prompt_time, parent_deadline=job.deadline)
    async with aclosing(stream):
        async for frame in stream:
            yield frame


async def _prepare_chat_stream(data, user_id, session_id, user_msg_id, attachments, admitted=None, *,
                               prompt_time=None, parent_deadline=None):
    from main import init_manager

    background = admitted is not None
    attachment_names = [attachment.original_name for attachment in attachments]
    
    # 获取 AI 模型（等待后台初始化完成）
    if not init_manager.stage1_complete.is_set():
        logger.info("等待 AI 模型初始化完成...")
        try:
            await asyncio.wait_for(init_manager.stage1_complete.wait(), timeout=30)
        except asyncio.TimeoutError:
            if admitted is not None:
                await admitted.finish('failed', 'DETACHED_MODEL_INIT_TIMEOUT')
            async def timeout_stream():
                yield f"data: {json.dumps({'type': 'error', 'content': 'AI 模型初始化超时，请稍后重试'})}\n\n"
            return timeout_stream()
    
    # 模型选择（两个实例分工）：
    # - chat_model：文本任务模型（会话标题 / RAG / 记忆摘要），始终用主模型，不受附件影响
    # - agent_model：执行 Agent 链路的模型——
    #     附件（图片/视频）请求 → 视觉模型（设计 §6.1：VISION_MODEL 与聊天主模型分离）
    #     纯文本 + 深度思考   → 思考模型实例
    #     默认              → 主模型
    # ⚠️ 附件 + 深度思考：深度思考对附件场景自动失效（视觉模型不支持思考，
    #    且 qwen3.8-max thinking + 多模态实测首 token 延迟 >60s 必超时）
    chat_model = model_for('chat', init_manager.chat_model)
    if attachments:
        vision_model = model_for('vision', getattr(init_manager, "vision_model", None))
        if vision_model:
            agent_model = vision_model
            logger.info(f"附件请求使用视觉模型: {getattr(vision_model, 'model_name', 'vision_model')} (user={user_id[:8]})")
        else:
            agent_model = chat_model if current_snapshot() is None else None
            logger.warning(f"视觉模型不可用，附件理解降级主模型: user={user_id[:8]}")
    elif data.enable_thinking:
        agent_model = model_for('chat_thinking', init_manager.chat_model_thinking)
        if agent_model is None and current_snapshot() is None:
            agent_model=chat_model
        logger.debug(f"深度思考模式已开启: user={user_id[:8]}")
    else:
        agent_model = chat_model

    if not agent_model:
        if admitted is not None:
            await admitted.finish('failed', 'DETACHED_MODEL_UNAVAILABLE')
        # 模型未初始化，返回错误 SSE
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'content': 'AI 模型未初始化'})}\n\n"
        return error_stream()
    
    memory_snapshot=None
    if not background and data.memory_mode!='off':
        from app.user_memory.router import service as user_memory
        try:
            async with async_session_factory() as memory_db:
                memory_budget=2048
                pinned=current_snapshot()
                if pinned is not None:
                    # Freeze one fragment for all possible assistant consumers.
                    ids={agent_model.profile_id}
                    for role in ('planner','reflection','synthesize'):
                        binding=pinned.manifest['resolved_static_bindings'].get(role)
                        if binding and binding['enabled']:ids.add(binding['profile_revision_id'])
                    memory_budget=min([2048,*[max(0,((pinned.profiles[ident]['context_window'] or 8192)-
                        (pinned.profiles[ident]['max_output_tokens'] or 2048)-1024)//5) for ident in ids]])
                memory_snapshot=await user_memory.snapshot(memory_db,user_id,budget=memory_budget)
        except BusinessError:
            raise
        except Exception:
            from app.user_memory.service import error as memory_error
            memory_error(50340,'MEMORY_STORE_UNAVAILABLE',503)
    # 标题生成模型（D 项：用轻量模型 flash，避免与主 Agent 抢 qwen3.7-max 并发导致
    # DashScope 连接不稳定；flash 不可用时降级主模型）。
    # 标题生成本身推迟到主 Agent 完成后执行（见 generate_stream 的 done 事件之后）
    title_model = model_for('session_title', getattr(init_manager, "classifier_model", None) or chat_model)
    
    # ===== 并行执行 RAG 检索 + 记忆压缩（两者无数据依赖）=====
    from datetime import datetime
    prompt_time = prompt_time or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    from app.utils.prompt_loader import load_prompt
    from app.utils.config import get_agent_config
    from langchain_core.messages import HumanMessage, AIMessage
    from app.services.token_budget import TokenBudget
    from app.services.memory_compressor import MemoryCompressor, check_and_summarize
    from app.utils.config import get_token_budget_config, get_memory_compression_config
    
    rag_context = ""
    rag_sources = []
    rag_status = None
    compressed_messages = []
    
    async def _run_rag():
        """Skip retrieval for empty text and greetings without a substantive request."""
        nonlocal rag_context, rag_sources, rag_status
        message = data.message.strip()
        if not message or _CASUAL_GREETING.fullmatch(message):
            return
        try:
            rag_result = await _retrieval_service(init_manager).query(
                query_text=data.message,
                user_id=user_id,
                top_k=3,
                use_hyde=False,
                deadline=parent_deadline,
            )
            rag_context = rag_result.get("context", "")
            rag_sources = rag_result.get("sources", [])
            rag_status = rag_result.get("status")
            
            if rag_context:
                logger.info(f"RAG 检索成功: {len(rag_sources)} 个来源, context 长度={len(rag_context)}")
            else:
                logger.debug("RAG 检索无结果")
        except (ModelBudgetDenied, ModelContextExceeded):
            raise
        except Exception as e:
            if isinstance(e, BusinessError) and e.http_status in (401, 403):
                raise
            rag_status = 'error'
            logger.warning(f"RAG 查询失败，跳过: {type(e).__name__}")
    
    async def _run_memory_compression():
        """记忆压缩协程"""
        nonlocal compressed_messages
        try:
            tb_config = get_token_budget_config()
            mc_config = get_memory_compression_config()
            
            # 并行执行两次 DB 查询（使用独立 session）
            async def _get_messages():
                async with async_session_factory() as db:
                    return await session_manager.get_all_messages(db, session_id, before_message_id=user_msg_id)
            
            async def _get_summary():
                async with async_session_factory() as db:
                    return await session_manager.get_summary(db, session_id)
            
            all_msgs, summary_obj = await asyncio.gather(
                _get_messages(), _get_summary()
            )
            
            all_messages = [
                {
                    "role": m.role,
                    "content": m.content,
                    "attachments": m.attachments_json or [],
                }
                for m in all_msgs
            ]
            existing_summary = summary_obj.summary_text if summary_obj and summary_obj.last_message_id < user_msg_id else ""
            prev_messages = all_messages

            # 预加载历史附件图片 base64（最近 2 条用户消息，支持"上一张图里…"追问）
            await _preload_history_images(prev_messages)

            # 先用估算值创建 budget（RAG 上下文长度未知时用默认值）
            budget = TokenBudget(
                model_context_size=tb_config.get("model_context_size", 32768),
                system_prompt=tb_config.get("system_prompt", 500),
                rag_context_max=tb_config.get("rag_context_max", 2000),
                summary_max=tb_config.get("summary_max", 800),
                agent_scratchpad_reserve=tb_config.get("agent_scratchpad_reserve", 4000),
                current_input_estimate=tb_config.get("current_input_estimate", 300),
                safety_margin=tb_config.get("safety_margin", 1000),
                image_tokens_per_msg=tb_config.get("image_tokens_per_msg", 3000),
                max_history_images=tb_config.get("max_history_images", 4),
            )
            # 用 RAG 最大配额估算（实际 RAG 结果未就绪时用上限值）
            history_quota = budget.allocate("" * tb_config.get("rag_context_max", 2000))
            # 当前消息附件 token 从历史配额中扣减（优先保证当前消息）
            history_quota = max(500, history_quota - _estimate_attachment_tokens(attachments))

            compressor = MemoryCompressor(budget)
            compressed_messages, history_tokens = compressor.build_context(
                all_messages=prev_messages,
                existing_summary=existing_summary,
                token_quota=history_quota,
            )
            
            logger.info(
                f"记忆压缩完成: session={session_id[:12]}, "
                f"total_msgs={len(prev_messages)}, window_msgs={len(compressed_messages)}, "
                f"history_tokens={history_tokens}, quota={history_quota}, "
                f"has_summary={bool(existing_summary)}"
            )
            
            if background:
                return
            # 异步触发里程碑摘要检查（非阻塞）
            threshold = mc_config.get("summarize_threshold", 40)
            min_interval = mc_config.get("min_summary_interval", 20)
            summary_task = spawn_background_task(
                check_and_summarize(
                    chat_model=chat_model,
                    session_id=session_id,
                    threshold=threshold,
                    min_interval=min_interval,
                ),
                name="chat_summary_check",
            )
            if summary_task is None:
                logger.warning("可选历史摘要任务因容量不足跳过")
        except Exception as e:
            logger.warning(f"记忆压缩管线失败，回退到简单截断: {e}")
            max_rounds = get_agent_config().get("max_history_rounds", 20)
            try:
                async with async_session_factory() as db:
                    earlier = await session_manager.get_all_messages(db, session_id, before_message_id=user_msg_id)
                recent = [{'role': item.role, 'content': item.content, 'attachments_json': item.attachments_json}
                          for item in earlier[-max_rounds * 2:]]
                for msg in recent:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role == "user":
                        # 附件以文本占位（降级路径不做图片预编码，保证 Agent 感知附件存在）
                        atts = msg.get("attachments_json") or []
                        if atts:
                            names = [
                                a.get("original_name", "附件")
                                for a in atts if isinstance(a, dict)
                            ]
                            content = f"{content}\n[附件: {', '.join(names[:3])}]"
                        compressed_messages.append(HumanMessage(content=content))
                    elif role == "assistant":
                        compressed_messages.append(AIMessage(content=content))
            except Exception:
                pass
    
    async def _preload_history_images(msgs: list) -> None:
        """
        预加载历史消息附件图片的 base64（多模态回放用）

        最多处理最近 max_user_msgs 条带图片附件的用户消息，每条最多 max_history_images 张；
        图片压缩走线程池（CPU 密集），结果按 file_id 缓存（与当前消息处理共享缓存）。
        attachments_json 不含存储路径，需查 chat_attachments 权威表。
        """
        from app.ai_service.multimodal_processor import process_image_to_base64

        max_user_msgs = 2
        max_images = int(os.getenv("CHAT_MAX_IMAGES_PER_MSG", "6"))
        processed = 0
        for msg in reversed(msgs):
            if processed >= max_user_msgs:
                break
            atts = msg.get("attachments") or []
            images = [
                a for a in atts
                if isinstance(a, dict) and a.get("file_type") == "image"
            ]
            if not images:
                continue
            processed += 1
            images = images[-max_images:]
            file_ids = [a["file_id"] for a in images]
            try:
                async with async_session_factory() as db:
                    rows = await chat_attachment_service.get_owned_list(db, user_id, file_ids)
            except Exception as e:
                logger.debug(f"历史附件路径查询失败: {e}")
                continue
            path_map = {r.file_id: r.stored_path for r in rows}
            b64s = []
            for img in images:
                rel_path = path_map.get(img["file_id"])
                if not rel_path:
                    continue
                abs_path = os.path.join(os.getcwd(), rel_path)
                if not os.path.isfile(abs_path):
                    continue
                try:
                    b64 = await asyncio.to_thread(
                        process_image_to_base64, abs_path, img["file_id"]
                    )
                    b64s.append(b64)
                except Exception as e:
                    logger.debug(f"历史图片预编码失败: file_id={img['file_id'][:8]}, err={e}")
            msg["image_b64s"] = b64s
            logger.debug(f"历史附件图片预编码: {len(b64s)} 张 (message attachments={len(images)})")

    # 并行执行 RAG + 记忆压缩
    await asyncio.gather(_run_rag(), _run_memory_compression())
    
    # ===== 解析用户引用的笔记（从消息中提取结构化引用块）=====
    referenced_notes_text = ""
    _ref_match = re.search(r'<referenced_notes>\s*(.*?)\s*</referenced_notes>', data.message, re.DOTALL)
    if _ref_match:
        ref_block = _ref_match.group(1).strip()
        # 解析每行："- ID: xxx | 标题: yyy"
        ref_lines = [l.strip() for l in ref_block.split('\n') if l.strip().startswith('- ID:')]
        if ref_lines:
            referenced_notes_text = (
                "\n\n## 用户当前引用的笔记（可直接使用 ID 操作）：\n"
                + "\n".join(ref_lines)
                + "\n提示：当用户要求修改/更新/编辑这些笔记时，直接使用上述 ID 调用 update_note_tool，无需再次搜索。"
            )
            logger.info(f"解析到引用笔记: {len(ref_lines)} 篇")

    # ===== 解析用户选择的 PPT 模板（v1.4，设计方案 §6.5）=====
    # 与 <referenced_notes> 同段解析，注入 system_prompt 让 LLM 把 template_id
    # 填入 generate_ppt_tool（工具内强校验归属，双保险）
    ppt_template_text = ""
    _pt_match = re.search(r'<ppt_template>\s*(.*?)\s*</ppt_template>', data.message, re.DOTALL)
    if _pt_match:
        pt_block = _pt_match.group(1).strip()
        pt_lines = [l.strip() for l in pt_block.split('\n') if l.strip().startswith('- ID:')]
        if pt_lines:
            ppt_template_text = (
                "\n\n## 用户当前选择的 PPT 模板（生成讲解 PPT 时使用）：\n"
                + "\n".join(pt_lines)
                + "\n提示：当用户要求生成讲解 PPT 时，将上述模板 ID 填入 generate_ppt_tool 的 template_id 参数。"
            )
            logger.info(f"解析到 PPT 模板选择: {len(pt_lines)} 个")
    
    # 构建 system_prompt（注入 RAG 上下文 + 引用笔记 ID）
    try:
        system_prompt = load_prompt("main")
        system_prompt = system_prompt.replace("{current_time}", prompt_time)
    except Exception:
        system_prompt = "你是一个智能笔记助手，帮助用户管理知识、检索信息和回答问题。"
    
    from app.ai_service.context_composer import set_assistant_data
    set_assistant_data(rag=rag_context,references=referenced_notes_text,ppt=ppt_template_text,memory=memory_snapshot)
    if current_snapshot() is not None:
        role=getattr(agent_model,'role_key','chat')
        task=template_for(role,'',current_time=prompt_time,
                          question=data.message,user_message=data.message)
        if task:compressed_messages.append(HumanMessage(content=task))
        system_prompt=''
    else:
        system_prompt=FIXED_SAFETY+'\n\n'+system_prompt
        if rag_sources:
            system_prompt += '\n' + CITATION_POLICY
    
    # ===== SSE 流式响应生成器 =====
    async def generate_stream():
        """生成 SSE 流式响应（RAG + 记忆压缩已并行完成，直接生成）"""
        workflow = admitted
        heartbeat = journal = None
        finished = False
        citation_turn = CitationTurn(rag_context, rag_sources, delivered=current_snapshot() is None,
                                     owner=user_id, session_factory=async_session_factory)
        current_citations.set(citation_turn)
        try:
            turn = ToolTurn(session_id, user_msg_id)
            snapshot = current_snapshot()
            if workflow is None:
                workflow = await ChatWorkflow.start(async_session_factory, user_id, turn, snapshot.release_id if snapshot else None)
                heartbeat = asyncio.create_task(workflow.heartbeat(), name='chat_workflow_heartbeat')
            journal = await WorkflowJournal.resume(workflow) if background else WorkflowJournal(workflow)
            # 发送思考事件
            if rag_status in RAG_NOTICES:
                yield await journal.live_frame({'type': 'thinking', 'stage': 'rag',
                                                'status': rag_status, 'content': RAG_NOTICES[rag_status]})
            elif rag_context:
                yield await journal.live_frame({'type': 'thinking', 'stage': 'rag',
                    'content': f'已从知识库检索到 {len(rag_sources)} 个相关文档'})
            yield await journal.live_frame({'type': 'thinking', 'stage': 'processing', 'content': '正在思考...'})

            # ===== 附件多模态内容组装（图片压缩 / 视频抽帧，CPU 密集 → 线程池）=====
            attachment_content = []
            if attachments:
                from app.ai_service.multimodal_processor import build_attachment_blocks_async
                if data.enable_thinking:
                    yield f"data: {json.dumps({'type': 'thinking', 'stage': 'attachment', 'content': '深度思考对附件场景自动关闭（视觉模型理解附件）'})}\n\n"
                if model_for('vision',getattr(init_manager, "vision_model", None)) is None:
                    yield f"data: {json.dumps({'type': 'thinking', 'stage': 'attachment', 'content': '视觉模型未就绪，当前只能基于文字回复'})}\n\n"
                if any(a.file_type == "video" for a in attachments):
                    yield f"data: {json.dumps({'type': 'thinking', 'stage': 'attachment', 'content': '正在解析视频（抽帧）...'})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'thinking', 'stage': 'attachment', 'content': '正在处理附件...'})}\n\n"
                try:
                    attachment_content = await build_attachment_blocks_async(attachments)
                except Exception as e:
                    logger.warning(f"附件多模态处理失败: {e}", exc_info=True)
                    attachment_content = []
                if attachment_content:
                    yield f"data: {json.dumps({'type': 'thinking', 'stage': 'attachment', 'content': f'附件解析完成（{len(attachment_content)} 张图像）'})}\n\n"

            # 获取笔记服务和回顾服务（等待阶段 2 完成，最多等待 5 秒）
            note_service = None
            review_service = None
            try:
                await asyncio.wait_for(init_manager.stage2_complete.wait(), timeout=5)
                note_service = init_manager.note_service
                if note_service:
                    from app.services.review_service import ReviewService
                    review_service = ReviewService()
                    logger.debug(f"Agent 工具服务注入成功: note_service={type(note_service).__name__}, review_service=ReviewService")
                else:
                    logger.warning("note_service 为 None，笔记相关工具将不可用")
            except asyncio.TimeoutError:
                logger.warning("等待 NoteService 初始化超时(5s)，笔记相关工具不可用")
            except Exception as e:
                logger.warning(f"获取 Agent 工具服务失败: {e}")
            
            # ===== 执行对话编排图（P2-1 第二步：LangGraph StateGraph）=====
            # 查询分类 / 路由决策 / ReAct | Plan 执行全部在图内完成；
            # 事件流经总线产出，SSE 契约与改造前一致
            ctx = ChatRouteContext(
                agent_model=agent_model,
                plan_model=model_for('planner', getattr(init_manager, "plan_model", None)),
                classifier_model=model_for('classifier', getattr(init_manager, "classifier_model", None)),
                user_id=user_id,
                user_message=data.message,
                system_prompt=system_prompt,
                compressed_messages=compressed_messages,
                db_session_factory=async_session_factory,
                note_service=note_service,
                review_service=review_service,
                email_service=init_manager.email_service,
                ppt_service=getattr(init_manager, "ppt_service", None),
                attachment_content=attachment_content,
                attachment_names=attachment_names or None,
                # 分层超时（审查 B）：单次 LLM 调用超时由模型 request_timeout 控制（120s，
                # factory.py）；此处为整轮 Agent 执行总预算。深度思考模式下单次 thinking
                # 调用可达 50-90s，整轮预算按模式放宽（与 plan_timeout 的 *2 策略对齐）
                react_timeout=LLM_STREAM_TIMEOUT * 2 if data.enable_thinking else LLM_STREAM_TIMEOUT,
                plan_timeout=LLM_STREAM_TIMEOUT * 2,
                tool_turn=turn,
                workflow=workflow,
            )

            # 执行 + 事件转发（error 后不发 done、不保存回复，与改造前一致）
            accumulated = ""
            errored = False
            generated_files = {}
            model_budget = current_model_budget.get()
            async with aclosing(stream_chat_graph(ctx)) as _owned_stream_0:
                async for event in _owned_stream_0:
                    if model_budget is not None:
                        model_budget.check_open()
                    if event.get("type") == "response":
                        accumulated += event.get("content", "")
                    elif event.get("type") == "error":
                        errored = True
                        reason = event.get('reason') if event.get('code') == ErrorCode.MODEL_BUDGET_EXCEEDED else None
                        await workflow.finish('failed', reason or 'AGENT_EXECUTION_FAILED')
                        finished = True
                    elif event.get("type") == "tool_file":
                        # 补全 AttachmentMeta 必填字段（file_type/original_name），
                        # 否则历史接口 MessageListResponse 校验 500（v1.6 修复）
                        if event.get("name") == "text_to_speech":
                            # TTS 语音：audio_url 随消息持久化，历史回放恢复播放/下载卡片
                            ppt_file_info = {
                                "file_id": event.get("file_id"),
                                "file_type": "tts",
                                "original_name": "语音朗读",
                                "download_url": event.get("audio_url"),
                                "duration_estimate": event.get("duration_estimate"),
                            }
                        else:
                            ppt_file_info = {
                                "file_id": event.get("file_id"),
                                "file_type": "ppt",
                                "original_name": event.get("title") or "讲解PPT",
                                "download_url": event.get("download_url"),
                                "title": event.get("title"),
                                "slide_count": event.get("slide_count"),
                            }
                        generated_files[ppt_file_info['file_id']] = ppt_file_info
                    yield await journal.live_frame(event)

            if model_budget is not None:
                model_budget.check_open()
            if errored:
                # 与现状一致：error 事件后不发送 done、不保存回复
                return

            # 异步保存 AI 回复到数据库 + Redis（非阻塞，不延迟 done 事件）
            # 回复保存成功后回填附件 message_id（绑定消息，禁止单独删除）
            citations = {}
            reply_id = None
            if accumulated:
                async def _save_reply_and_bind():
                    nonlocal reply_id, citations
                    async def persist_reply_links(db, message):
                        nonlocal citations
                        if background:
                            await authorize_turn(db, user_id, turn)
                        citations = await confirm_citations(db, user_id, citation_turn.cited(accumulated))
                        if citations:
                            message.citations_json = citations
                        await workflow.succeed_in(db, message.id)
                        if not background and memory_snapshot is not None and user_msg_id:
                            from app.user_memory.jobs import MemoryJobs
                            await MemoryJobs(user_memory).record_success(db,user_id,new_request_id(),user_msg_id)
                    reply_id = await chat_service.save_message_with_commit(
                        session_id, user_id, "assistant", accumulated,
                        # PPT 下载信息随消息持久化（复用 attachments_json，切换页面后下载卡片仍可恢复）
                        attachments_json=list(generated_files.values()) or None,
                        before_message_commit=persist_reply_links, require_commit=True,
                    )
                    if not background and user_msg_id and attachments:
                        try:
                            async with async_session_factory() as db:
                                await chat_attachment_service.bind_message(
                                    db, user_id,
                                    [a.file_id for a in attachments],
                                    session_id, user_msg_id,
                                )
                                await db.commit()
                        except Exception as e:
                            logger.warning(f"附件绑定消息失败: {e}")

                # The final done event acknowledges a committed reply, not merely
                # admission to an in-memory background queue.
                await _save_reply_and_bind()
            else:
                async with async_session_factory() as db:
                    async with db.begin():
                        if background:
                            await authorize_turn(db, user_id, turn)
                        await workflow.succeed_in(db)
            finished = True
            
            # 发送完成事件（包含 RAG 来源信息）
            done_data = {'type': 'done', 'session_id': session_id, 'workflow_id': workflow.id}
            if citations:
                done_data.update(reply_message_id=reply_id, citation_ids=list(citations))
            yield await journal.live_frame(done_data)

            if background:
                return

            # 主 Agent 完成后再生成标题（D 项：与主流程错峰执行，消除并发挤
            # DashScope 连接导致的 Connection error；轻量模型调用快，
            # 标题更新延迟几秒对用户无感。error 路径不触发——失败时标题无意义）
            title_task = spawn_background_task(
                chat_service.generate_and_update_title(
                    session_id, data.message, title_model,
                    attachment_names=attachment_names or None,
                ),
                name="chat_title_generate",
            )
            if title_task is None:
                logger.warning("可选会话标题任务因容量不足跳过")

        except (ModelBudgetDenied, ModelContextExceeded) as e:
            if workflow is not None and not finished:
                await workflow.finish('failed', e.data['reason'])
                finished = True
            yield event_frame(e.stream_event())
        except Exception as e:
            if workflow is not None and not finished:
                await workflow.finish('failed', 'CHAT_WORKFLOW_FAILURE')
                finished = True
            logger.error(f"AI 对话流生成失败: {type(e).__name__}: {e}", exc_info=True)
            # 对外统一文案（审查 M15：内部异常细节仅入日志，不直出客户端）
            yield event_frame({'type': 'error', 'content': '生成失败，请稍后重试'})
        finally:
            current_citations.set(None)
            if heartbeat is not None:
                heartbeat.cancel()
            with move_on_after(2, shield=True):
                if heartbeat is not None:
                    await asyncio.gather(heartbeat, return_exceptions=True)
                if admitted is None and workflow is not None and not finished:
                    await workflow.finish('cancelled', 'STREAM_CLOSED')
    return generate_stream()


@router.post("/chat/rag", summary="RAG 查询", dependencies=[Depends(rate_limit(endpoint_limit=10))])
async def rag_query(
    data: RAGRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    RAG 知识库查询接口
    
    使用原问题进行向量和关键词检索，融合后重排并组装证据。
    """
    from main import init_manager
    
    try:
        result = await _retrieval_service(init_manager).query(
            query_text=data.query,
            user_id=user_id,
            top_k=data.top_k,
            use_hyde=data.use_hyde,
        )
        model_budget = current_model_budget.get()
        if model_budget is not None:
            model_budget.check_open()
        
        return success_response(data={
            "answer": result.get("context", ""),
            "sources": result.get("sources", []),
            "status": result["status"],
            "failed_sources": result["failed_sources"],
            "recall_methods": result["recall_methods"],
            "retrieval_policy_revision_id": result["retrieval_policy_revision_id"],
            "index_snapshot": result["index_snapshot"],
            "degraded_stages": result.get("degraded_stages", []),
            "query_expansion": result["query_expansion"],
            "timings_ms": result.get("timings_ms", {}),
            "rerank_status": result.get("rerank_status", "unavailable"),
        })
    except (ModelBudgetDenied, ModelContextExceeded):
        raise
    except RetrievalUnavailable as error:
        raise BusinessError(ErrorCode.RAG_UNAVAILABLE, http_status=503,
                            data={'status': 'error', 'failed_sources': error.failed_sources,
                                  'timings_ms': error.timings_ms}) from error
    except BusinessError:
        raise
    except Exception as e:
        logger.error(f"RAG 查询失败: {type(e).__name__}")
        # 审查 M3/M15：不再把错误吞进 200 成功响应；对外统一文案走全局异常处理器
        raise BusinessError(
            code=ErrorCode.RAG_UNAVAILABLE, http_status=503, data={'status': 'error'},
        )


# ============================================================
# 聊天附件端点（图片/视频）
# ============================================================

@router.post("/chat/files", summary="上传聊天附件（图片/视频）", dependencies=[Depends(rate_limit(endpoint_limit=10))])
async def upload_chat_file(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
):
    """
    上传图片/视频附件

    校验：扩展名白名单 + magic bytes 双重校验、大小限制（图片 10MB / 视频 50MB）、
    用户配额（USER_STORAGE_QUOTA_MB）。
    上传后附件为孤儿状态（未绑定会话），24h 内未随消息发送会被定时清理。
    """
    # 分块限流读取（上限取图片/视频两者最大 50MB，防内存 DoS；
    # 具体类型限制由 chat_attachment_service 按 magic bytes 校验）
    content = await read_upload_limited(file, max_size_mb=50)
    async with async_session_factory() as db:
        attachment = await chat_attachment_service.save_upload(
            db, user_id, file.filename or "", content
        )
        # server_default 的 created_at 需显式刷新加载，且在 session 内取值
        # （commit 后 session 关闭，延迟属性访问会抛 DetachedInstanceError）
        await db.refresh(attachment)
        resp_data = UploadResponse(
            file_id=attachment.file_id,
            file_type=attachment.file_type,
            mime_type=attachment.mime_type,
            original_name=attachment.original_name,
            file_size=attachment.file_size,
            width=attachment.width,
            height=attachment.height,
            duration_sec=attachment.duration_sec,
            created_at=attachment.created_at,
        ).model_dump()
        await db.commit()

    return success_response(data=resp_data)


@router.get("/chat/files/{file_id}", summary="预览/下载聊天附件")
async def get_chat_file(
    file_id: str,
    authorization: Optional[str] = Header(None),
    token: str = Query(None, description="可选 JWT：<img>/<video> 标签无法携带 Header 时以 query 参数鉴权"),
):
    """
    附件预览/下载（JWT 鉴权 + 归属校验）

    文件不存在或不属于当前用户时统一返回 404（防枚举探测）。
    Starlette FileResponse 原生支持 HTTP Range（视频拖动进度条）。

    鉴权方式：
    1. 常规：Authorization: Bearer <JWT>（fetch 等场景）
    2. 回显兜底：?token=<JWT>（<img>/<video> 标签无法携带 Header）
       token 为短时效 access token（默认 30 分钟），仅用于当次回显，风险可控。
    """
    from app.utils.auth_utils import decode_token
    from app.core.failed_response import BusinessError as _BE
    from app.core.failed_response import ErrorCode as _EC

    # 解析 token：优先 Header，其次 query 参数
    token_str = None
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token_str = parts[1]
    if not token_str:
        token_str = token
    if not token_str:
        raise _BE(code=_EC.TOKEN_INVALID, message="缺少认证信息", http_status=401)

    payload = decode_token(token_str)
    if payload.get("type") != "access":
        raise _BE(code=_EC.TOKEN_INVALID, message="Token 类型错误", http_status=401)
    user_id = payload.get("sub", "")
    if not user_id:
        raise _BE(code=_EC.TOKEN_INVALID, message="Token 缺少用户标识", http_status=401)

    async with async_session_factory() as db:
        attachment = await chat_attachment_service.get_owned(db, user_id, file_id)

    file_path = os.path.join(os.getcwd(), attachment.stored_path)
    if not os.path.isfile(file_path):
        raise BusinessError(code=ErrorCode.ATTACHMENT_NOT_FOUND, http_status=404)

    return FileResponse(
        file_path,
        media_type=attachment.mime_type,
        filename=attachment.original_name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.delete("/chat/files/{file_id}", summary="删除未绑定附件")
async def delete_chat_file(
    file_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """
    删除附件（仅允许未绑定消息的附件）

    已随消息发送的附件不可单独删除（随会话删除级联清理）。
    """
    async with async_session_factory() as db:
        await chat_attachment_service.delete_unbound(db, user_id, file_id)
        await db.commit()
    return success_response(message="附件已删除")


@router.get("/chat/sessions", summary="获取会话列表")
async def list_sessions(
    user_id: str = Depends(get_current_user_id),
):
    """获取当前用户的所有聊天会话"""
    async with async_session_factory() as db:
        sessions = await session_manager.list_sessions(db, user_id)
    
    # 兼容 ORM 对象和 Redis 缓存返回的 dict
    def _to_dict(s):
        if isinstance(s, dict):
            return {
                "id": s.get("id"),
                "title": s.get("title"),
                "created_at": s.get("created_at"),
                "updated_at": s.get("updated_at"),
            }
        return {
            "id": s.id,
            "title": s.title,
            "created_at": to_utc_iso(getattr(s, 'created_at', None)),
            "updated_at": to_utc_iso(getattr(s, 'updated_at', None)),
        }
    
    return success_response(data={"sessions": [_to_dict(s) for s in sessions]})


@router.get('/chat/sessions/{session_id}/approvals', summary='读取本会话的待确认写入')
async def get_approvals(session_id: UUID, response: Response, user_id: str = Depends(get_current_user_id)):
    response.headers['Cache-Control'] = 'private, no-store'
    async with async_session_factory() as db:
        return success_response(data=await list_approvals(db, user_id, str(session_id)))


@router.post('/chat/approvals/{operation_id}', summary='确认或拒绝指定写入',
             dependencies=[Depends(rate_limit(endpoint_limit=20))])
async def post_approval(operation_id: UUID, data: ApprovalDecision, response: Response,
                        user_id: str = Depends(get_current_user_id)):
    from app.routers.note_router import _get_note_service
    from app.services.review_service import ReviewService

    def configured_email_service():
        from main import init_manager

        return init_manager.email_service

    response.headers['Cache-Control'] = 'private, no-store'
    return success_response(data=await decide_approval(async_session_factory, user_id, str(operation_id),
                            data.preview_hash, data.decision, _get_note_service(), ReviewService(),
                            email_service_factory=configured_email_service))


@router.get('/chat/sessions/{session_id}/email-operations', summary='读取本会话的邮件发送结果')
async def get_email_operations(session_id: UUID, response: Response, user_id: str = Depends(get_current_user_id)):
    response.headers['Cache-Control'] = 'private, no-store'
    async with async_session_factory() as db:
        return success_response(data=await list_email_operations(db, user_id, str(session_id)))


@router.post('/chat/approvals/{operation_id}/email-received', summary='记录已核实收件',
             dependencies=[Depends(rate_limit(endpoint_limit=20))])
async def post_email_received(operation_id: UUID, response: Response, user_id: str = Depends(get_current_user_id)):
    response.headers['Cache-Control'] = 'private, no-store'
    return success_response(data=await mark_email_received(async_session_factory, user_id, str(operation_id)))


@router.get("/chat/workflows/{workflow_id}", summary="读取对话运行检查点")
async def get_workflow(workflow_id: UUID, response: Response, user_id: str = Depends(get_current_user_id)):
    response.headers['Cache-Control'] = 'no-store'
    async with async_session_factory() as db:
        return success_response(data=await read_workflow(db, user_id, str(workflow_id)))


@router.post("/chat/workflows/{workflow_id}/cancel", summary="停止后台对话")
async def cancel_workflow(
    workflow_id: UUID,
    user_id: str = Depends(get_current_user_id),
    limited=Depends(rate_limit(endpoint_limit=20)),
):
    return success_response(data=await cancel_detached_chat(async_session_factory, str(workflow_id), user_id))


@router.get("/chat/workflows/{workflow_id}/events", summary="按序号补读对话事件",
            dependencies=[Depends(rate_limit(endpoint_limit=60))])
async def get_workflow_events(
    workflow_id: UUID,
    response: Response,
    after: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
):
    response.headers['Cache-Control'] = 'private, no-store'
    async with async_session_factory() as db:
        return success_response(data=await read_workflow_events(db, user_id, str(workflow_id), after, limit))


@router.delete("/chat/sessions/{session_id}", summary="删除会话")
async def delete_session(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """
    删除指定会话及其所有消息

    级联清理会话文件（图片/视频附件 + PPT/TTS 工具产出文件）：
    先清附件（路由层职责，DatabaseSessionManager.delete_session 保持纯 DB 职责），
    再删会话/消息（ORM cascade），最后统一 commit。
    """
    async with async_session_factory() as db:
        # ① 用户上传的图片/视频附件（chat_attachments 表 + 文件）
        await chat_attachment_service.cleanup_by_session(db, session_id, user_id)
        # ② 工具产出文件（PPT/TTS，仅经消息 attachments_json 引用，不落表）
        await chat_attachment_service.cleanup_session_tool_files(db, session_id, user_id)
        # ③ 删除会话/消息（ORM cascade）
        await session_manager.delete_session(db, session_id, user_id)
        await db.commit()
    return success_response(message="会话已删除")


@router.get("/chat/{session_id}/messages", summary="获取消息历史")
async def get_messages(
    session_id: str,
    cursor: str = Query(None, description="分页游标（上一页最早消息的 created_at）"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
    user_id: str = Depends(get_current_user_id),
):
    """
    获取消息历史（游标分页）
    
    使用 created_at 作为游标，避免 LIMIT/OFFSET 翻页不一致问题。
    """
    async with async_session_factory() as db:
        messages, has_more = await session_manager.get_messages(
            db, session_id, user_id, cursor=cursor, limit=limit
        )

    # 兼容 tool_file 旧格式附件（缺 file_type/original_name），
    # 防止历史消息校验失败（v1.6 修复：读取时清洗，不依赖存量数据已被修复）
    for msg in messages:
        if msg.attachments_json:
            msg.attachments_json = [
                chat_service.normalize_attachment(a) for a in msg.attachments_json
            ]

    next_cursor = None
    if has_more and messages:
        next_cursor = messages[0].created_at.isoformat()

    return success_response(data=MessageListResponse(
        messages=messages,
        has_more=has_more,
        next_cursor=next_cursor,
    ).model_dump())


@router.get('/chat/{session_id}/messages/{message_id}/citations/{citation_id}', summary='查看回答引用的原文')
async def get_reply_citation(session_id: str, message_id: int, citation_id: str, response: Response,
                             user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    response.headers['Cache-Control'] = 'private, no-store'
    return success_response(data=await read_citation(db, user_id, session_id, message_id, citation_id))


@router.put("/chat/{session_id}/title", summary="修改会话标题")
async def update_session_title(
    session_id: str,
    data: SessionTitleUpdate,
    user_id: str = Depends(get_current_user_id),
):
    """手动修改会话标题"""
    async with async_session_factory() as db:
        session = await session_manager.get_session(db, session_id, user_id)
        session.title = data.title
        await db.commit()
    
    return success_response(message="标题已更新")
