"""add note format column

Revision ID: e8a1b2c3d4f5
Revises: b7e2c9d4a5f6
Create Date: 2026-08-21

笔记文档类型功能（docs/笔记文档类型功能设计方案.md）：
- notes 表新增 format 列（VARCHAR(10)，默认 'md'）
- 取值：'md'（Markdown，默认）/ 'txt'（纯文本），VARCHAR 预留扩展
- DEFAULT 'md' 保证存量数据自动兼容为 Markdown（无需回填脚本）
- 类型仅新建时写入；NoteUpdate 不含 format（已创建笔记不可改类型）
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8a1b2c3d4f5'
down_revision: Union[str, None] = 'b7e2c9d4a5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增文档类型列（存量数据由列默认值自动填 'md'）"""
    op.add_column(
        'notes',
        sa.Column('format', sa.String(length=10), nullable=False, server_default='md'),
    )


def downgrade() -> None:
    """移除文档类型列"""
    op.drop_column('notes', 'format')
