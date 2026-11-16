"""
工具调用审计模型（P2-3 安全护栏）

对应 MySQL 表: tool_call_audit

记录每次 Agent 工具调用的归属、入参、结果与耗时，供安全追溯：
- 由 agent_middleware 的 before_tool/after_tool 钩子配对写入
- 入参/结果仅存截断预览（500 字符），不存完整敏感内容
- 仅插入与查询，无更新/删除操作
"""

from datetime import datetime

from sqlalchemy import (
    String, Text, DateTime, Integer, BigInteger, Boolean, func, Index,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ToolCallAudit(Base):
    """单次工具调用审计记录"""
    __tablename__ = "tool_call_audit"

    # 主键：自增 ID
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # 请求上下文（复用 model_trace 的 contextvar；后台任务可能为空）
    request_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 工具名 / 入参预览 / 结果预览（均截断 500 字符）
    tool_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_preview: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 耗时（毫秒）/ 成败
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)

    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # 按用户/时间追溯
        Index("idx_audit_user_created", "user_id", "created_at"),
        Index("idx_audit_tool", "tool_name"),
    )

    def __repr__(self) -> str:
        return f"<ToolCallAudit(id={self.id}, tool={self.tool_name}, success={self.success})>"
