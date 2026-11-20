"""
聊天业务代理服务层

封装 RAG 查询和会话管理的业务逻辑，
作为 Router 层和底层 Service/RAG 之间的桥梁。
"""
from app.model_config.runtime import model_for, template_for
from app.model_config.snapshots import current_snapshot

from typing import Optional
from app.core.failed_response import BusinessError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger_handler import logger
from app.db.database import async_session_factory
from app.models.chat import ChatSession, ChatMessage
from app.services.database_session_manager import DatabaseSessionManager


class ChatService:
    """
    聊天业务代理层

    职责：
    - 协调会话管理和消息持久化
    - 集成 RAG 查询结果
    - 处理会话标题自动生成逻辑
    """

    @staticmethod
    def normalize_attachment(att) -> dict:
        """清洗历史消息附件（兼容 tool_file 旧格式，v1.6）。

        PPT 下载卡片持久化初版把原始 tool_file 事件直接存入 attachments_json
        （缺 file_type/original_name/file_id），导致 MessageListResponse 校验失败
        （历史对话 500）。读取与存量修复共用此函数。
        """
        if not isinstance(att, dict):
            return att
        att = dict(att)
        if "file_type" not in att:
            # 含 download_url → PPT 工具文件；否则按图片兜底（旧附件格式）
            att["file_type"] = "ppt" if att.get("download_url") else "image"
        if "original_name" not in att:
            att["original_name"] = att.get("title") or "附件"
        if "file_id" not in att:
            att["file_id"] = (att.get("download_url") or "").rsplit("/", 1)[-1] \
                or f"legacy-{abs(hash(str(att))) % 10 ** 8}"
        return att

    def __init__(self, session_manager: Optional[DatabaseSessionManager] = None):
        """
        初始化聊天服务
        
        Args:
            session_manager: 会话管理器实例
        """
        self.session_manager = session_manager or DatabaseSessionManager()
    
    async def get_or_create_session(
        self, db: AsyncSession, user_id: str, session_id: Optional[str] = None
    ) -> str:
        """
        获取或创建聊天会话
        
        如果 session_id 为空，自动创建新会话。
        
        Args:
            db: 数据库会话
            user_id: 用户 ID
            session_id: 现有会话 ID（可选）
            
        Returns:
            会话 ID
        """
        if session_id:
            # 验证现有会话
            session = await self.session_manager.get_session(db, session_id, user_id)
            return session.id
        else:
            # 创建新会话
            session = await self.session_manager.create_session(db, user_id)
            return session.id

    async def save_message_with_commit(
        self, session_id: str, user_id: str, role: str, content: str,
        idempotency_key: Optional[str] = None,
        attachments_json: Optional[list] = None,
        before_commit=None,
        require_commit: bool = False,
        *,
        before_message_commit=None,
    ) -> Optional[int]:
        """
        使用独立 session 保存消息并 commit（用于异步后台任务）

        不依赖请求级 db session，使用 async_session_factory() 创建独立会话，
        确保消息持久化到 MySQL + Redis 缓存。

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            role: 消息角色
            content: 消息内容
            idempotency_key: 幂等键
            attachments_json: 附件元数据列表（仅用户消息携带）

        Returns:
            消息 ID（保存失败返回 None；供附件绑定 message_id 使用）
        """
        try:
            async with async_session_factory() as db:
                cache_options = {'cache_immediately': False} if before_message_commit is not None else {}
                message = await self.session_manager.add_message(
                    db, session_id, user_id, role, content, idempotency_key,
                    attachments_json=attachments_json,
                    **cache_options,
                )
                if before_commit is not None:
                    await before_commit(db)
                if before_message_commit is not None:
                    await before_message_commit(db, message)
                await db.commit()
                if before_message_commit is not None and message is not None:
                    await self.session_manager.cache_message(session_id, message)
                logger.info(f"消息保存成功: session={session_id[:12]}, role={role}, len={len(content)}")
                return message.id if message else None
        except Exception as e:
            logger.error(f"消息持久化失败: role={role}, error_type={type(e).__name__}")
            if require_commit:
                if isinstance(e, BusinessError):
                    raise
                raise BusinessError(50001,message='消息持久化失败，请重试',http_status=500) from None
            return None

    async def generate_and_update_title(
        self, session_id: str, user_message: str, chat_model=None,
        attachment_names: Optional[list] = None
    ) -> None:
        """
        根据用户消息自动生成/更新会话标题

        策略：
        - 新会话（默认标题"新对话"）：直接用 LLM 生成标题
        - 已有标题的会话：检查消息数，前 3 轮内允许更新

        Args:
            session_id: 会话 ID
            user_message: 用户最新消息内容
            chat_model: LLM 模型实例
            attachment_names: 附件文件名列表（消息为空时标题兜底）
        """
        chat_model = model_for('session_title', chat_model)
        try:
            # 模型 trace 独立上下文（后台任务，无请求上下文；P0）
            # contextvar 按 asyncio task 隔离，任务结束自动清理，无需手动 clear
            from app.core.model_trace import set_trace_context, new_request_id
            set_trace_context(
                new_request_id(), user_id="", session_id=session_id, stage="title"
            )
            async with async_session_factory() as db:
                # 获取会话
                result = await db.execute(
                    select(ChatSession).where(ChatSession.id == session_id)
                )
                session = result.scalar_one_or_none()
                if not session:
                    return

                # 统计当前会话消息数
                msg_count_result = await db.execute(
                    select(ChatMessage).where(ChatMessage.session_id == session_id)
                )
                msg_count = len(msg_count_result.scalars().all())

                # 标题已是自定义的（非默认），不再自动更新
                if session.title != "新对话" and msg_count > 4:
                    return

                # 使用 LLM 生成标题
                if chat_model and user_message.strip():
                    title = await self._generate_title_with_llm(user_message, chat_model)
                elif not user_message.strip():
                    # 仅发附件：标题兜底为 "附件分析（文件名）"
                    title = f"附件分析（{', '.join(attachment_names[:2])}）" if attachment_names else "新对话"
                else:
                    # 降级：取用户消息前 20 字符作为标题
                    title = user_message[:20].strip()

                if title:
                    session.title = title
                    await db.commit()
                    logger.info(f"会话标题已更新: session_id={session_id}, title={title}")

        except Exception as e:
            logger.warning(f"更新会话标题失败: session_id={session_id}, error={e}")

    async def _generate_title_with_llm(self, user_message: str, chat_model) -> str:
        """
        使用 LLM 根据对话内容生成会话标题
        
        使用 astream 收集结果（ChatOllama 无 ainvoke 方法）。
        
        Args:
            user_message: 用户消息
            chat_model: LLM 模型实例
            
        Returns:
            生成的标题文本
        """
        chat_model = model_for('session_title', chat_model)
        try:
            from app.utils.prompt_loader import format_prompt
            from langchain_core.messages import HumanMessage
            prompt = format_prompt("session_title", conversation=user_message)
            
            # 使用 astream 收集完整响应（兼容 ChatOllama）
            title = ""
            async for chunk in chat_model.astream([HumanMessage(content=prompt)]):
                if hasattr(chunk, 'content') and chunk.content:
                    title += chunk.content
            
            title = title.strip().strip('"').strip("'")[:20]
            return title if title else user_message[:20]
        except Exception as e:
            logger.warning(f"LLM 生成标题失败: {e}")
            from app.model_config.runtime import require_failure_policy
            require_failure_policy('session_title','default_title')
            return user_message[:20].strip()
