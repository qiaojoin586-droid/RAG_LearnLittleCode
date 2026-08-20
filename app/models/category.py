"""
分类数据模型

对应 MySQL 表: note_categories
存储用户笔记分类，支持树形结构（最多 3 级）与软删除（回收站）。

设计要点（见 docs/笔记分类管理功能设计方案.md v4）：
- user_id 严格隔离：所有查询/写入均以 user_id 为必选条件
- parent_id 自引用（NULL = 顶级分类），ON DELETE CASCADE 作为物理清理兜底
- deleted_at 非 NULL 表示软删除（沿用笔记 14 天回收站惯例）
- 同级同名唯一由应用层校验（软删除记录需保留，不加 DB UNIQUE 约束）
"""

import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class NoteCategory(Base):
    """
    分类模型

    核心设计：
    - Python 侧 uuid4() 生成主键（与 Note 模型惯例一致）
    - sort_order 同级排序权重，由批量重排端点统一重写（0, 1, 2, ...）
    - icon/color 为可选个性化字段

    关联关系：
    - 多对一 → User（所属用户）
    - 自引用 → parent_id（父分类，NULL 表示顶级）
    """
    __tablename__ = "note_categories"

    # 主键：UUID
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # 所属用户 ID（外键，级联删除——用户注销时清除全部分类）
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.uuid", ondelete="CASCADE"), nullable=False, index=True
    )

    # 父分类 ID（自引用，NULL = 顶级；CASCADE 作为物理清理时子孙一并清除的兜底，
    # 软删除流程由业务层处理，不走外键级联）
    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("note_categories.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # 分类名称
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # 同级排序权重（同 parent 下从小到大排序）
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 分类图标（emoji 或图标名）
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 分类颜色（十六进制）
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)

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

    def __repr__(self) -> str:
        return f"<NoteCategory(id={self.id}, name={self.name})>"
