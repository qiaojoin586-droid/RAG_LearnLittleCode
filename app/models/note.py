"""
笔记数据模型

对应 MySQL 表: notes
存储用户笔记内容，支持软删除、置顶、标签、分类。
笔记数据同时存在 MySQL（结构化查询）和 ChromaDB（语义检索）中，实现双写模式。
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, Boolean, ForeignKey, JSON, func, Index, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Note(Base):
    """
    笔记模型
    
    核心设计：
    - deleted_at 非 NULL 表示软删除（回收站机制）
    - tags 使用 JSON 字段存储标签列表
    - is_pinned 控制是否置顶
    - 向量数据存在 ChromaDB notes_collection 中，通过 note_id 关联
    
    关联关系：
    - 多对一 → User（所属用户）
    - 一对多 → ReviewRecord（回顾记录）
    """
    __tablename__ = "notes"

    # MySQL FULLTEXT 索引（ngram 双字分词）：keyword_search 的 MATCH AGAINST 提速
    # 存量库由 Alembic 迁移 b7e2c9d4a5f6 创建；此处保证新装库（create_all）结构一致
    __table_args__ = (
        Index(
            "ft_notes_title_content", "title", "content",
            mysql_prefix="FULLTEXT", mysql_with_parser="ngram",
        ),
    )

    # 主键：UUID
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    
    # 所属用户 ID（外键，级联删除）
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.uuid", ondelete="CASCADE"), nullable=False, index=True
    )
    
    # 笔记标题
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    
    # 笔记内容（Markdown 格式）
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 文档类型：md = Markdown（默认）/ txt = 纯文本
    # 仅新建时写入（NoteUpdate 不含该字段，已创建笔记不可改类型）；VARCHAR 预留扩展
    format: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="md"
    )

    # 标签列表（JSON 数组，如 ["Python", "FastAPI", "RAG"]）
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    
    # 分类
    category: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    # 分类 ID（关联 note_categories 表，双写期间与 category 字符串并行保留；
    # 分类物理删除时 SET NULL → 笔记变为"未分类"）
    category_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("note_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    
    # 是否置顶
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_indexed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    
    # 软删除时间戳（NULL 表示未删除，非 NULL 表示已删除）
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    
    # 更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    
    # ========== 关联关系 ==========
    user = relationship("User", back_populates="notes")
    review_records = relationship("ReviewRecord", back_populates="note", cascade="all, delete-orphan")
    
    def __repr__(self) -> str:
        return f"<Note(id={self.id}, title={self.title[:20]})>"
