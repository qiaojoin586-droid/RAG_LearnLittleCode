"""
聊天会话与消息持久化服务

管理聊天会话和消息的 CRUD 操作：
- 会话创建 / 消息存储 / 历史查询 / 会话删除
- Redis 热缓存（最新 10 条消息）
- 游标分页（翻历史消息）
- 会话标题自动生成
- 会话列表缓存
"""

import json
import uuid
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import select, func, and_, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger_handler import logger
from app.core.failed_response import BusinessError, ErrorCode
from app.models.chat import ChatSession, ChatMessage, ChatSummary
from app.db.redis_client import get_redis, delete_pattern
from app.utils.time_utils import to_utc_iso, parse_db_time


class DatabaseSessionManager:
    """
    聊天会话管理器
    
    负责会话和消息的持久化，以及 Redis 缓存管理。
    
    消息写入路径：MySQL → Redis（Redis 失败不影响数据完整性）
    消息读取路径：Redis → MySQL（缓存 miss 时回填）
    """
    
    def __init__(self, chat_buffer_size: int = 10):
        """
        初始化会话管理器
        
        Args:
            chat_buffer_size: Redis 中缓存的最新消息条数（默认 10）
        """
        self.chat_buffer_size = chat_buffer_size
    
    async def create_session(self, db: AsyncSession, user_id: str, title: str = "新对话") -> ChatSession:
        """
        创建新的聊天会话
        
        Args:
            db: 数据库会话
            user_id: 用户 ID
            title: 会话标题
            
        Returns:
            创建的会话对象
        """
        session = ChatSession(
            id=str(uuid.uuid4()),
            user_id=user_id,
            title=title,
        )
        db.add(session)
        await db.flush()
        
        # 清除会话列表缓存
        await self._invalidate_session_list_cache(user_id)
        
        logger.info(f"会话创建: session_id={session.id}, user_id={user_id}")
        return session
    
    async def get_session(self, db: AsyncSession, session_id: str, user_id: str) -> ChatSession:
        """
        获取会话（带权限校验）
        
        Args:
            db: 数据库会话
            session_id: 会话 ID
            user_id: 用户 ID
            
        Returns:
            会话对象
            
        Raises:
            BusinessError: 会话不存在
        """
        result = await db.execute(
            select(ChatSession).where(
                and_(ChatSession.id == session_id, ChatSession.user_id == user_id)
            )
        )
        session = result.scalar_one_or_none()
        
        if not session:
            raise BusinessError(code=ErrorCode.SESSION_NOT_FOUND, http_status=404)
        
        return session
    
    async def list_sessions(self, db: AsyncSession, user_id: str) -> List[ChatSession]:
        """
        获取用户的会话列表（按更新时间倒序）
        
        优先从 Redis 缓存读取，miss 时查 MySQL 并回填缓存。
        
        Args:
            db: 数据库会话
            user_id: 用户 ID
            
        Returns:
            会话列表
        """
        # 尝试从 Redis 缓存读取
        try:
            redis = get_redis()
            cache_key = f"chat:sessions:{user_id}"
            cached = await redis.get(cache_key)
            if cached:
                # 缓存命中，反序列化返回
                return json.loads(cached)
        except Exception:
            pass
        
        # 缓存 miss，查 MySQL
        result = await db.execute(
            select(ChatSession)
            .where(ChatSession.user_id == user_id)
            .order_by(ChatSession.updated_at.desc())
        )
        sessions = list(result.scalars().all())
        
        # 回填 Redis 缓存（TTL 300s）
        try:
            redis = get_redis()
            cache_key = f"chat:sessions:{user_id}"
            session_data = [
                {
                    "id": s.id,
                    "title": s.title,
                    "updated_at": to_utc_iso(s.updated_at),
                    "created_at": to_utc_iso(s.created_at),
                }
                for s in sessions
            ]
            await redis.setex(cache_key, 300, json.dumps(session_data, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"会话列表缓存写入失败: {e}")
        
        return sessions
    
    async def delete_session(self, db: AsyncSession, session_id: str, user_id: str) -> None:
        """
        删除会话及其所有消息
        
        Args:
            db: 数据库会话
            session_id: 会话 ID
            user_id: 用户 ID
        """
        session = await self.get_session(db, session_id, user_id)
        
        await db.delete(session)
        await db.flush()
        
        # 清除缓存
        await self._invalidate_session_list_cache(user_id)
        
        try:
            redis = get_redis()
            await redis.delete(f"chat:msgs:{session_id}")
        except Exception:
            pass
        
        logger.info(f"会话删除: session_id={session_id}")
    
    async def add_message(
        self, db: AsyncSession, session_id: str, user_id: str,
        role: str, content: str, idempotency_key: Optional[str] = None,
        attachments_json: Optional[list] = None, *, cache_immediately: bool = True,
    ) -> ChatMessage:
        """
        添加消息到会话

        写入路径：MySQL → Redis
        支持幂等写入（idempotency_key 防重复）

        Args:
            db: 数据库会话
            session_id: 会话 ID
            user_id: 用户 ID
            role: 消息角色（user / assistant）
            content: 消息内容
            idempotency_key: 幂等键
            attachments_json: 附件元数据列表（仅用户消息携带）

        Returns:
            创建的消息对象
        """
        # 验证会话存在且属于当前用户
        await self.get_session(db, session_id, user_id)

        # 幂等检查
        if idempotency_key:
            existing = await db.execute(
                select(ChatMessage).where(ChatMessage.idempotency_key == idempotency_key)
            )
            if existing.scalar_one_or_none():
                raise BusinessError(
                    code=ErrorCode.IDEMPOTENCY_DUPLICATE,
                    http_status=409
                )

        # 写入 MySQL（显式设置 created_at，避免 flush 后访问触发 MissingGreenlet）
        now = datetime.utcnow()
        message = ChatMessage(
            session_id=session_id,
            role=role,
            content=content,
            idempotency_key=idempotency_key,
            attachments_json=attachments_json,
            created_at=now,
        )
        db.add(message)
        await db.flush()
        
        # 使用直接 SQL UPDATE 更新会话的 updated_at（避免 ORM 属性赋值触发 MissingGreenlet）
        await db.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(updated_at=datetime.utcnow())
        )
        
        # 异步更新 Redis 热缓存
        if cache_immediately:
            await self.cache_message(session_id, message)
        
        return message
    
    async def get_messages(
        self, db: AsyncSession, session_id: str, user_id: str,
        cursor: Optional[str] = None, limit: int = 20
    ) -> Tuple[List[ChatMessage], bool]:
        """
        获取消息历史（游标分页）
        
        不使用 LIMIT/OFFSET，改用游标分页保证翻页一致性。
        无游标时优先从 Redis 热缓存读取（命中缓存）。
        
        Args:
            db: 数据库会话
            session_id: 会话 ID
            user_id: 用户 ID
            cursor: 上一页最早消息的 created_at（ISO 格式字符串）
            limit: 每页数量
            
        Returns:
            (消息列表, has_more)
        """
        # 验证会话
        await self.get_session(db, session_id, user_id)
        
        # 无游标时优先尝试 Redis 热缓存（首页请求）
        if not cursor:
            try:
                redis = get_redis()
                cache_key = f"chat:msgs:{session_id}"
                cached = await redis.lrange(cache_key, 0, -1)
                if cached:
                    # Redis 缓存命中，反序列化返回
                    messages = []
                    for item in cached:
                        msg_data = json.loads(item)
                        # 构造轻量级对象，兼容 ChatMessage 的返回格式
                        msg = ChatMessage(
                            id=msg_data.get("id"),
                            session_id=session_id,
                            role=msg_data.get("role", ""),
                            content=msg_data.get("content", ""),
                            attachments_json=msg_data.get("attachments_json"),
                            citations_json=dict.fromkeys(msg_data.get("citation_ids", [])),
                        )
                        if msg_data.get("created_at"):
                            msg.created_at = parse_db_time(msg_data["created_at"])
                        messages.append(msg)
                    
                    # Redis 中最新在前，反转为正序
                    messages.reverse()
                    has_more = len(messages) >= self.chat_buffer_size
                    return messages, has_more
            except Exception as e:
                logger.debug(f"Redis 消息缓存读取失败，回退 MySQL: {e}")
        
        # 缓存 miss 或有游标，查 MySQL
        query = select(ChatMessage).where(ChatMessage.session_id == session_id)
        
        if cursor:
            # 游标分页：获取比 cursor 更早的消息（parse_db_time 兼容 naive/带偏移两种游标格式）
            cursor_time = parse_db_time(cursor)
            query = query.where(ChatMessage.created_at < cursor_time)
        
        query = query.order_by(ChatMessage.created_at.desc()).limit(limit + 1)
        
        result = await db.execute(query)
        messages = list(result.scalars().all())
        
        has_more = len(messages) > limit
        if has_more:
            messages = messages[:limit]
        
        # 按时间正序返回
        messages.reverse()
        
        return messages, has_more
    
    async def get_recent_messages(self, session_id: str, limit: int = 10) -> List[dict]:
        """
        获取最近消息（优先 Redis 缓存）
        
        Args:
            session_id: 会话 ID
            limit: 消息数量
            
        Returns:
            消息字典列表（最新在末尾）
        """
        # 尝试 Redis
        try:
            redis = get_redis()
            cache_key = f"chat:msgs:{session_id}"
            cached = await redis.lrange(cache_key, 0, limit - 1)
            if cached:
                messages = [json.loads(msg) for msg in cached]
                messages.reverse()  # Redis 中最新在前，需要反转
                return messages
        except Exception:
            pass
        
        # 缓存 miss，查 MySQL
        from app.db.database import async_session_factory
        async with async_session_factory() as db:
            result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(limit)
            )
            messages = result.scalars().all()
            
            msg_list = [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": m.content,
                    "created_at": to_utc_iso(m.created_at),
                    "attachments_json": m.attachments_json,
                    "citation_ids": m.citation_ids,
                }
                for m in reversed(messages)
            ]
            
            # 回填 Redis
            await self._rebuild_redis_cache(session_id, msg_list)
            
            return msg_list
    
    async def cache_message(self, session_id: str, message: ChatMessage) -> None:
        """
        将新消息推入 Redis 热缓存（LPUSH + LTRIM）
        
        Args:
            session_id: 会话 ID
            message: 消息对象
        """
        try:
            redis = get_redis()
            cache_key = f"chat:msgs:{session_id}"
            
            msg_data = json.dumps({
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "created_at": to_utc_iso(message.created_at),
                "attachments_json": message.attachments_json,
                "citation_ids": message.citation_ids,
            }, ensure_ascii=False)
            
            # LPUSH 添加到列表头部（最新在前）
            await redis.lpush(cache_key, msg_data)
            # LTRIM 保持列表长度
            await redis.ltrim(cache_key, 0, self.chat_buffer_size - 1)
            
        except Exception as e:
            logger.warning(f"Redis 消息缓存更新失败: {e}")
    
    async def _rebuild_redis_cache(self, session_id: str, messages: List[dict]) -> None:
        """
        从 MySQL 回填 Redis 缓存
        
        Args:
            session_id: 会话 ID
            messages: 消息字典列表
        """
        try:
            redis = get_redis()
            cache_key = f"chat:msgs:{session_id}"
            
            # 清空旧缓存
            await redis.delete(cache_key)
            
            # 输入按时间正序排列；逐条 LPUSH 后最新消息自然位于表头。
            # 先取最近窗口，避免回填时把较新的消息裁掉。
            for msg in messages[-self.chat_buffer_size:]:
                await redis.lpush(cache_key, json.dumps(msg, ensure_ascii=False))
            
            await redis.ltrim(cache_key, 0, self.chat_buffer_size - 1)
            
        except Exception as e:
            logger.warning(f"Redis 缓存回填失败: {e}")
    
    async def _invalidate_session_list_cache(self, user_id: str) -> None:
        """
        清除用户的会话列表缓存
        
        Args:
            user_id: 用户 ID
        """
        try:
            await delete_pattern(get_redis(), f"chat:sessions:{user_id}*")
        except Exception:
            pass
    
    # ========== 摘要管理 ==========
    
    async def get_summary(self, db: AsyncSession, session_id: str) -> Optional[ChatSummary]:
        """
        获取会话的里程碑摘要
        
        Args:
            db: 数据库会话
            session_id: 会话 ID
            
        Returns:
            ChatSummary 对象，不存在时返回 None
        """
        result = await db.execute(
            select(ChatSummary).where(ChatSummary.session_id == session_id)
        )
        return result.scalar_one_or_none()
    
    async def update_summary(
        self, db: AsyncSession, session_id: str,
        summary_text: str, last_message_id: int
    ) -> ChatSummary:
        """
        创建或更新会话摘要（增量更新）
        
        Args:
            db: 数据库会话
            session_id: 会话 ID
            summary_text: 新摘要文本
            last_message_id: 摘要覆盖到的最后一条消息 ID
            
        Returns:
            更新后的 ChatSummary 对象
        """
        from app.services.token_budget import TokenCounter
        
        # 查询已有摘要
        result = await db.execute(
            select(ChatSummary).where(ChatSummary.session_id == session_id)
        )
        existing = result.scalar_one_or_none()
        
        token_count = TokenCounter.count(summary_text)
        
        if existing:
            # 增量更新
            existing.summary_text = summary_text
            existing.last_message_id = last_message_id
            existing.version += 1
            existing.token_count = token_count
        else:
            # 新建
            summary = ChatSummary(
                session_id=session_id,
                summary_text=summary_text,
                last_message_id=last_message_id,
                version=1,
                token_count=token_count,
            )
            db.add(summary)
            existing = summary
        
        await db.flush()
        logger.debug(
            f"摘要已更新: session={session_id[:12]}, "
            f"version={existing.version}, tokens={token_count}"
        )
        return existing
    
    async def get_all_messages(
        self, db: AsyncSession, session_id: str, *, before_message_id: int | None = None,
    ) -> List[ChatMessage]:
        """
        获取会话的全部消息（按时间正序）
        
        用于记忆压缩场景，需要全量加载。
        
        Args:
            db: 数据库会话
            session_id: 会话 ID
            
        Returns:
            消息列表（时间升序）
        """
        query = select(ChatMessage).where(ChatMessage.session_id == session_id)
        if before_message_id is not None:
            query = query.where(ChatMessage.id < before_message_id)
        result = await db.execute(query.order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc()))
        return list(result.scalars().all())

