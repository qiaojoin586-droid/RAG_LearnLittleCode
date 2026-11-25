"""
聊天会话与消息数据模型

对应 MySQL 表:
- chat_sessions: 聊天会话
- chat_messages: 聊天消息
- chat_summaries: 聊天摘要（里程碑压缩）

会话标题支持自动生成（LLM）和手动修改。
消息使用游标分页，支持幂等写入（idempotency_key 防重复）。
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, Integer, BigInteger, Float, ForeignKey, JSON, func, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ChatSession(Base):
    """
    聊天会话模型
    
    存储会话元数据，消息通过 ChatMessage 关联。
    metadata 字段存储扩展信息：
    - title_generated_at: 标题生成时间（冻结机制）
    - title_frozen: 标题是否已冻结（不再自动更新）
    - model_name: 使用的模型名称
    - total_tokens: 累计 token 消耗
    - message_count: 消息总数
    
    关联关系：
    - 多对一 → User（所属用户）
    - 一对多 → ChatMessage（消息列表）
    """
    __tablename__ = "chat_sessions"
    
    # 主键：UUID
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    
    # 所属用户 ID（外键，级联删除）
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.uuid", ondelete="CASCADE"), nullable=False, index=True
    )
    
    # 会话标题（可自动生成或手动修改）
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="新对话")
    
    # 扩展元数据（JSON 格式）
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", JSON, nullable=True, default=dict
    )
    
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    
    # 更新时间（新消息写入时更新，用于会话列表排序）
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    
    # ========== 关联关系 ==========
    user = relationship("User", back_populates="chat_sessions")
    messages = relationship(
        "ChatMessage", back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at"
    )
    summary = relationship(
        "ChatSummary", back_populates="session",
        uselist=False, cascade="all, delete-orphan"
    )
    
    def __repr__(self) -> str:
        return f"<ChatSession(id={self.id}, title={self.title[:20]})>"


class ChatMessage(Base):
    """
    聊天消息模型
    
    存储单条聊天消息，支持：
    - 幂等写入：idempotency_key 有 UNIQUE 约束，防止网络重试导致重复消息
    - Token 计数：记录每条消息的 token 消耗
    - 角色区分：user / assistant / system
    
    关联关系：
    - 多对一 → ChatSession（所属会话）
    """
    __tablename__ = "chat_messages"
    
    # 主键：自增 ID
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    
    # 所属会话 ID（外键，级联删除）
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    
    # 消息角色：user / assistant / system
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    
    # 消息内容
    content: Mapped[str] = mapped_column(Text, nullable=False)
    
    # Token 消耗数量
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    
    # 幂等键（客户端生成 UUID，防止重复提交）
    idempotency_key: Mapped[str | None] = mapped_column(
        String(36), unique=True, nullable=True
    )
    
    # 创建时间（同时作为排序依据和游标分页的游标）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # 附件元数据（JSON 数组：file_id/file_type/original_name/width/height 等）
    # 冗余存储用于消息列表/SSE 回显，权威数据在 chat_attachments 表
    attachments_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    citations_json: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)

    @property
    def citation_ids(self) -> list[str]:
        return list(self.citations_json or {})

    # ========== 关联关系 ==========
    session = relationship("ChatSession", back_populates="messages")

    @property
    def attachments(self) -> list | None:
        """附件元数据（供 ChatMessageResponse.from_attributes 回显）"""
        return self.attachments_json if self.attachments_json else None

    def __repr__(self) -> str:
        return f"<ChatMessage(id={self.id}, role={self.role})>"

# 更新数据库中的每条对话的摘要信息
class ChatSummary(Base):
    """
    聊天摘要模型
    
    每个会话保持一条摘要记录，采用增量更新策略。
    last_message_id 记录摘要覆盖到的消息位置，
    下次摘要时仅需处理该 ID 之后的新消息。
    """
    __tablename__ = "chat_summaries"
    
    # 主键
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    
    # 所属会话 ID（唯一约束，每个会话仅一条摘要）
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False, unique=True
    )
    
    # 摘要文本
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    
    # 摘要覆盖到的最后一条消息 ID
    last_message_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    
    # 摘要版本号（增量更新递增）
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    
    # 摘要的 token 数（缓存）
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    
    # 更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    
    # ========== 关联关系 ==========
    session = relationship("ChatSession", back_populates="summary")

    def __repr__(self) -> str:
        return f"<ChatSummary(session_id={self.session_id}, version={self.version})>"


class ChatAttachment(Base):
    """
    聊天附件模型（AI 对话中上传的图片/视频）

    存储策略（与 chat_messages.attachments_json 冗余并存）：
    - 本表为权威数据：负责文件存储、用户配额、生命周期清理
    - chat_messages.attachments_json 为冗余元数据：消息列表/SSE 无需联表即可回显
    - 上传时 session_id/message_id 为空（孤儿状态，超时清理）；发送消息后回填绑定
    - 不建立外键：会话删除时由路由层显式级联清理（见设计方案 §8.5）

    对应 MySQL 表: chat_attachments
    """
    __tablename__ = "chat_attachments"

    # 主键：自增 ID
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 附件唯一标识（32 位 uuid4 hex，对外暴露，用于预览/删除）
    file_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)

    # 归属用户 ID（不建外键，与 chat_sessions 一致由路由层校验归属）
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    # 发送消息后回填绑定（未发送前为空 → 孤儿状态）
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    # 附件类型：image / video
    file_type: Mapped[str] = mapped_column(String(10), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(50), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # 存储路径（相对项目根目录，如 data/chat_files/{user_id}/{file_id}.png）
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)

    # 原始文件大小（字节，配额统计依据）
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 图片宽高 / 视频首帧宽高（可选）
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 视频时长（秒，可选）
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 文件 MD5（去重 + 完整性校验）
    md5: Mapped[str] = mapped_column(String(32), nullable=False)

    # 创建时间（孤儿清理依据）
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ChatAttachment(file_id={self.file_id[:8]}, type={self.file_type})>"
