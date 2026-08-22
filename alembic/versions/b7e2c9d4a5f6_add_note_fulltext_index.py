"""add notes fulltext index

Revision ID: b7e2c9d4a5f6
Revises: a3f8d2c1b9e4
Create Date: 2026-08-21

为 notes 表添加 FULLTEXT 索引（title, content，ngram 双字分词）：
- 服务 keyword_search（POST /api/v1/note/search）的 MATCH AGAINST 查询提速
- ngram 解析器原生支持中文（双字 token）；英文按双字切分亦可命中子串
- 单字符关键词无法命中 ngram token → 业务层回退 LIKE（note_service._like_search）

⚠️ MySQL 专有语法（FULLTEXT ... WITH PARSER ngram），仅适用于 MySQL 部署。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7e2c9d4a5f6'
down_revision: Union[str, None] = 'a3f8d2c1b9e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """添加 FULLTEXT 索引（InnoDB 支持 FULLTEXT，MySQL 5.7+）"""
    op.execute(
        "ALTER TABLE notes ADD FULLTEXT INDEX ft_notes_title_content "
        "(title, content) WITH PARSER ngram"
    )


def downgrade() -> None:
    """移除 FULLTEXT 索引"""
    op.execute("ALTER TABLE notes DROP INDEX ft_notes_title_content")
